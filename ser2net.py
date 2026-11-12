#!/usr/bin/env python3
"""ser2net — expose local serial ports over the network, managed from a web UI.

Entry point. Responsibilities kept deliberately thin:
  1. Make bundled dependencies importable (offline bootstrap into ./lib).
  2. Parse CLI args and locate the config/data directory.
  3. Hand off to app.runtime, which owns the event loop and orchestration.

Run:
    python3 ser2net.py                 # normal start (first run asks bind IP on console)
    python3 ser2net.py --reconfigure   # re-pick the admin UI bind IP/port on the console
    python3 ser2net.py --about         # versions of the bundled components (for bug reports)
    python3 ser2net.py --set-password admin   # lost the password? (also while it runs)
    python3 ser2net.py --help
"""
# No `from __future__ import annotations` and nothing newer than Python 3.6 syntax in this
# file: an old python3 (RHEL 8's 3.6, RHEL 9's 3.9) gets the message below instead of a
# SyntaxError or a TypeError. app/ is imported only after it.
import argparse
import contextlib
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("ser2net needs Python 3.10 or newer; this is Python {} ({}). Run it with a "
             "newer one (e.g. python3.11 ser2net.py; RHEL / AlmaLinux / Rocky: sudo dnf "
             "install python3.11), or use the standalone binary or the Docker image "
             "(README.md > Install & run).".format(sys.version.split()[0], sys.executable))

# The script's own directory is on sys.path, so the `app` package imports here even
# before the explicit sys.path setup below (and PyInstaller bundles it in frozen builds).
from app import __version__

ROOT = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)  # True inside a PyInstaller build


def _app_dir() -> str:
    """Where config/logs live: next to the executable in a frozen build (so they
    survive and aren't written into the temp extraction dir), else the source tree."""
    return os.path.dirname(sys.executable) if FROZEN else ROOT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ser2net.py",
        description="Serial-to-network bridge with a web config UI.",
    )
    p.add_argument("--version", action="version", version=f"ser2net {__version__}")
    p.add_argument(
        "--data-dir",
        default=None,  # see below: whether it was given matters to --install-service
        help="Directory for config.json, its backups, the logs and traces (default: ./data; "
             "for --install-service: the data directory the service already uses, or for a "
             "new service from the standalone exe %%ProgramData%%\\ser2net). Give ser2net "
             "a folder of its own: it makes it private to the account it runs as.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.json (default: <data-dir>/config.json). Its folder is the data "
             "directory then: the backups, logs and traces are kept next to it.",
    )
    p.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-run the console picker for the admin UI bind IP/port, then start.",
    )
    p.add_argument(
        "--online",
        action="store_true",
        help="Allow the dependency bootstrap to fall back to PyPI if a wheel is missing.",
    )
    p.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Skip the dependency bootstrap (assume dependencies are already importable).",
    )
    p.add_argument(
        "--about",
        action="store_true",
        help="Print the versions of Python, OpenSSL and the optional components, then exit.",
    )
    p.add_argument(
        "--licenses",
        action="store_true",
        help="Print the licenses of ser2net and every bundled third-party component, then exit.",
    )
    svc = p.add_argument_group("Windows service")
    svc.add_argument("--install-service", action="store_true",
                     help="Install ser2net as a Windows service (starts at boot, restarts after "
                          "a failure) with this --data-dir, and start it. Needs Administrator.")
    svc.add_argument("--uninstall-service", action="store_true",
                     help="Stop and remove the Windows service (the data directory is kept).")
    svc.add_argument("--service-name", default="ser2net",
                     help="Name of the Windows service (default: ser2net); lets several "
                          "instances with different data directories run side by side.")
    svc.add_argument("--allow-unsafe-location", action="store_true",
                     help="Install the service from a source checkout even if a "
                          "non-administrator can modify it (the standalone exe never needs "
                          "this: it installs itself to an admin-only folder).")
    svc.add_argument("--service", action="store_true", help=argparse.SUPPRESS)  # used by the SCM
    p.add_argument(
        "--healthcheck",
        action="store_true",
        help="Exit 0 if this data directory's running instance answers on /healthz, else 1 "
             "(Docker HEALTHCHECK, monitoring).",
    )
    acc = p.add_argument_group("account recovery (works while ser2net runs, too)")
    one = acc.add_mutually_exclusive_group()
    one.add_argument("--set-password", metavar="USER",
                     help="Set a new password for the local account USER and sign its "
                          "sessions out (USER is created as an admin if it doesn't exist), "
                          "then exit. The password is asked on the terminal, or read from "
                          "the first line of stdin.")
    one.add_argument("--sign-out-all", action="store_true",
                     help="End every web UI session (new session key), then exit.")
    args = p.parse_args(argv)
    args.data_dir_given = args.data_dir is not None
    if args.data_dir is None:
        args.data_dir = os.path.join(_app_dir(), "data")
    return args


def apply_lib_override() -> None:
    """SER2NET_LIB_OVERRIDE=<dir>: import Python modules from <dir> before the bundled
    ones. The standalone executable keeps the LGPL-licensed ldap3 as plain source files
    for this, so it can be replaced by another (e.g. modified) version without
    rebuilding ser2net. Only an explicitly set variable is honoured — a directory is
    never picked up just because it exists next to the executable."""
    d = os.environ.get("SER2NET_LIB_OVERRIDE", "").strip()
    if d:
        if not os.path.isdir(d):
            raise SystemExit(f"SER2NET_LIB_OVERRIDE: not a directory: {d}")
        sys.path.insert(0, os.path.abspath(d))


def _write_utf8(text: str) -> None:
    # bytes, so a legacy console code page (Windows cp1252) can't fail on a character
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def print_licenses() -> int:
    bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "THIRD-PARTY-LICENSES.txt")
    if FROZEN and os.path.isfile(bundled):
        with open(bundled, encoding="utf-8") as fh:
            _write_utf8(fh.read())
        return 0
    # an executable built without the license file has no THIRD-PARTY-NOTICES.md next to
    # it either (ROOT is its temporary unpack folder): name this version's copy online
    notices = (f"https://github.com/haliskilic/ser2net/blob/v{__version__}/THIRD-PARTY-NOTICES.md"
               if FROZEN else os.path.join(ROOT, "THIRD-PARTY-NOTICES.md"))
    _write_utf8(
        f"ser2net {__version__} is licensed under the GNU AGPL-3.0-only (LICENSE) or a "
        f"commercial license (COMMERCIAL_LICENSE.md).\nThird-party components: {notices}\n")
    return 0


def print_about() -> int:
    import platform
    import ssl
    from importlib import metadata

    from app import optional

    lines = [f"ser2net {__version__}",
             f"Python {platform.python_version()} ({platform.python_implementation()}), "
             f"{ssl.OPENSSL_VERSION}",
             f"Platform: {platform.platform()} ({platform.machine()})",
             f"Build: {'standalone executable' if FROZEN else 'source'}"]
    components = (("pyserial", "serial"), ("pyserial-asyncio-fast", "serial_asyncio_fast"),
                  ("starlette", "starlette"), ("uvicorn", "uvicorn"), ("anyio", "anyio"),
                  ("psutil", "psutil"), ("paho-mqtt", "paho.mqtt"), ("ldap3", "ldap3"), ("pyasn1", "pyasn1"),
                  ("authlib", "authlib"), ("cryptography", "cryptography"))
    width = max(len(dist) for dist, _module in components)
    for dist, module in components:
        mod, problem = optional.load(module, dist, module)
        if problem is not None:
            # "not installed" only when it really isn't; else the error importing it raised
            lines.append(f"  {dist:{width}} " + ("not installed" if problem.missing else
                                                 f"failed to import: {problem.error}"))
            continue
        version = getattr(mod, "__version__", None)
        if not isinstance(version, str):
            try:
                version = metadata.version(dist)
            except metadata.PackageNotFoundError:
                version = "?"
        where = os.path.dirname(getattr(mod, "__file__", "") or "")
        lines.append(f"  {dist:{width}} {version}  ({where})")
    _write_utf8("\n".join(lines) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_lib_override()
    if args.licenses:
        return print_licenses()
    if args.uninstall_service:
        return _service_call(lambda ws: ws.uninstall(args.service_name))
    if args.healthcheck:  # stdlib only: no bootstrap needed
        from app import healthcheck

        ok, msg = healthcheck.check(args.config or os.path.join(args.data_dir, "config.json"))
        print(msg)
        return 0 if ok else 1

    config_path = args.config or os.path.join(args.data_dir, "config.json")
    split = split_layout_note(args)
    if split:
        print(f"ser2net: {split}", file=sys.stderr)
        from app import standalone  # (its notes are the lines all.log gets at start-up)

        standalone.notes.append(split)
    if args.service:
        return _service(args, config_path)

    # 1. dependencies. The one-off commands may run beside a running ser2net, so they
    # leave an out-of-date ./lib as it is (with a warning); a server start refreshes it.
    one_off = (args.about or args.set_password is not None or args.sign_out_all
               or args.install_service)
    load_dependencies(args, refresh=not one_off)

    if args.about:
        return print_about()

    if args.set_password is not None or args.sign_out_all:
        from app import accounts

        return accounts.main(args, config_path)

    # 3. hand off
    runtime = _import_runtime()

    if args.install_service:
        if _service_call(lambda ws: ws.require_windows()):
            return 1
        def prepare(service_config: str):
            # runs once an existing service is stopped (the data directory then has no
            # running instance). First run: asks for the UI address; warnings (e.g. a
            # setting that was not saved) would otherwise only reach the service's log
            _store, loaded = runtime.prepare_config(service_config, args.reconfigure)
            for note in loaded.load_notes:
                _write_utf8(note + "\n")
            runtime.require_admin_tls(loaded.admin_ui)  # the service could not start without it
            return loaded

        # an upgrade keeps the data directory the service has unless --data-dir is given
        done: dict = {}
        rc = _service_call(lambda ws: done.update(config=ws.install(
            args.service_name, args.data_dir if args.data_dir_given else None, args.config,
            prepare=prepare, default_data_dir=os.path.abspath(args.data_dir),
            allow_unsafe_location=args.allow_unsafe_location)))
        if rc == 0 and done.get("config") is not None:
            _print_firewall_hint(done["config"], args.service_name)
        return rc
    if os.name == "posix":
        # what the server creates (all.log, audit.log, traces, a mapping's log, tls/) is
        # for this account only, also in a data directory it doesn't make private
        os.umask(0o077)
    return runtime.main(config_path=config_path, reconfigure=args.reconfigure)


def split_layout_note(args: argparse.Namespace) -> str:
    """A warning when both --data-dir and --config are given and the config file is not in
    that directory: ser2net keeps its state next to the config file, not in --data-dir."""
    if not (args.config and args.data_dir_given):
        return ""
    folder = os.path.dirname(os.path.abspath(args.config))
    if os.path.normcase(os.path.abspath(args.data_dir)) == os.path.normcase(folder):
        return ""
    return (f"WARNING: --config {args.config} is not in --data-dir {args.data_dir}: ser2net "
            f"keeps the backups, logs and traces next to the config file, in {folder}")


def load_dependencies(args: argparse.Namespace, refresh: bool) -> None:
    """Make the bundled dependencies importable: the offline bootstrap into ./lib (a
    frozen build has everything inside). With ``refresh``, a ./lib installed for other
    pins (an upgrade) is brought up to date too. Its warnings go to the start-up log."""
    if FROZEN:
        return
    sys.path.insert(0, ROOT)  # ensure local packages (app/, bootstrap) import first
    if not args.no_bootstrap:
        import bootstrap

        bootstrap.ensure(online=args.online, refresh_lib=refresh)
    else:
        lib = os.path.join(ROOT, "lib")
        if os.path.isdir(lib):
            sys.path.insert(0, lib)
        try:
            import bootstrap
        except ImportError:  # a package of ser2net without it: nothing to check
            return
        bootstrap.warn_if_stale()
    if bootstrap.notes:
        from app import standalone  # (its notes are the lines all.log gets at start-up)

        standalone.notes.extend(bootstrap.notes)


def _import_runtime():
    """app.runtime, which imports the dependencies. When they come from a ./lib an older
    release installed that could not be refreshed (the bootstrap said why) and lack what
    this version needs, the start stops with that instead of a traceback."""
    try:
        from app import runtime
    except ImportError as e:
        bootstrap = sys.modules.get("bootstrap")
        if bootstrap is None or not bootstrap.lib_is_stale():
            raise
        raise SystemExit(f"ser2net: {e} — ./lib holds the dependencies of an older release, "
                         "and they could not be updated (see the warning before): run "
                         f"python3 bootstrap.py in {ROOT} as the account ser2net runs "
                         "as") from None
    return runtime


def _service(args: argparse.Namespace, config_path: str) -> int:
    """--service, as the Windows Service Control Manager starts it. The SCM waits about
    30 s for the process to connect (winservice.run), and refreshing ./lib after an
    upgrade can take longer, so the dependencies and the server load in the service's
    thread, once it is connected."""
    import threading

    from app import winservice

    stop_asked = threading.Event()

    def serve() -> int:
        try:
            load_dependencies(args, refresh=True)
            runtime = _import_runtime()
        except SystemExit as e:  # a service has no console: all.log says why
            if isinstance(e.code, str):
                import time

                log = os.path.join(os.path.dirname(os.path.abspath(config_path)), "all.log")
                with contextlib.suppress(OSError), open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e.code}\n")
            raise
        if stop_asked.is_set():  # the SCM asked to stop while they loaded
            runtime.request_shutdown()
        return runtime.service_main(config_path)

    def stop() -> None:
        stop_asked.set()
        runtime = sys.modules.get("app.runtime")
        if runtime is not None and hasattr(runtime, "request_shutdown"):
            runtime.request_shutdown()

    return winservice.run(args.service_name, serve, stop)


def _print_firewall_hint(config, service_name: str) -> None:
    """A LocalSystem service never sees the Windows Firewall prompt, so other hosts can't
    reach the admin UI, the mapping ports or the cluster until a rule is added. Print the
    exact command when anything listens beyond loopback (the firewall is not opened for
    the user: secure by default)."""
    admin = config.admin_ui
    beyond_loopback = admin.bind_ip not in ("127.0.0.1", "::1", "localhost") or any(
        m.enabled and getattr(m.network, "listens", False)
        and m.network.bind_ip not in ("127.0.0.1", "::1") for m in config.mappings)
    if not beyond_loopback:
        return
    from app import winservice

    _write_utf8(
        "\nWindows Firewall: this service listens for other hosts, which the firewall "
        "blocks until you allow it (a service gets no prompt). From an elevated prompt:\n  "
        + winservice.firewall_command(service_name) + "\n  (remove it later with: "
        f'netsh advfirewall firewall delete rule name="{winservice.display_name(service_name)}")'
        "\nSee docs/WINDOWS.md.\n")


def _service_call(fn) -> int:
    from app import winservice

    try:
        fn(winservice)
    except winservice.ServiceError as e:
        print(f"ser2net: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
