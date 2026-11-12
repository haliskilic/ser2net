"""The command line of ser2netweb: what ser2netweb.py (and ser2net.py, its name up to 2.9.x)
runs once the Python version is known to be new enough.

Responsibilities kept deliberately thin:
  1. Make bundled dependencies importable (offline bootstrap into ./lib).
  2. Parse CLI args and locate the config/data directory.
  3. Hand off to ser2netweb.runtime, which owns the event loop and orchestration.

The start scripts stay outside the package (a script and a package of the same name side by
side: `import ser2netweb` finds the package), and this module only imports the standard
library until the dependencies are loaded.
"""
import argparse
import contextlib
import os
import sys

from ser2netweb import __version__

# the source tree (the folder that holds ser2netweb/, bootstrap.py and ./lib), or a frozen
# build's unpacked files
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
                     help="Stop and remove the Windows service. Its data directory is kept "
                          "(config.json, backups, logs, the audit log, traces): delete it to "
                          "remove those too.")
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
                     help="Set a new password for the local account USER, sign its "
                          "sessions out and turn its two-factor sign-in off (USER is "
                          "created as an admin if it doesn't exist), then exit. The password "
                          "is asked on the terminal, or read from the first line of stdin.")
    one.add_argument("--sign-out-all", action="store_true",
                     help="End every web UI session (new session key), then exit.")
    run = p.add_argument_group("a running ser2net")
    run.add_argument("--reload", action="store_true",
                     help="Have the ser2net running with this data directory read config.json "
                          "again and apply it without a restart (mappings that didn't change "
                          "keep running with their clients), then exit. On Linux/macOS "
                          "SIGHUP does the same (systemctl reload ser2net).")
    args = p.parse_args(argv)
    if args.reload and (args.set_password is not None or args.sign_out_all):
        p.error("--reload can't be combined with --set-password or --sign-out-all")
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


# what --about writes for the folders it shortens (a bug report shows which copy of a
# package is loaded, not the account name in the path)
_ROOT_LABELS = {"<override>": "SER2NET_LIB_OVERRIDE", "<bundle>": "the executable's files",
                "<ser2net>": "ser2net's folder", "<python>": "the Python installation",
                "~": "the home folder"}


def _known_roots(env=None) -> list[tuple[str, str]]:
    """(label, folder) of the folders --about writes as a short label (_ROOT_LABELS)."""
    env = os.environ if env is None else env
    roots = [("<override>", (env.get("SER2NET_LIB_OVERRIDE") or "").strip()),
             ("<bundle>", getattr(sys, "_MEIPASS", "") or ""),
             ("<ser2net>", ROOT), ("<python>", sys.prefix),
             ("~", os.path.expanduser("~"))]
    out = []
    for label, folder in roots:
        folder = os.path.abspath(folder) if folder and folder != "~" else ""
        if folder and os.path.dirname(folder) != folder:  # never a whole drive or "/"
            out.append((label, folder))
    return out


def _where(path: str, roots: list[tuple[str, str]]) -> str:
    """`path` with the most specific of `roots` that holds it written as its label
    (compared as the OS does: letter case aside on Windows), else as it is."""
    full = os.path.abspath(path)
    best: tuple[str, str] | None = None
    for label, root in roots:
        try:
            inside = (os.path.commonpath([os.path.normcase(full), os.path.normcase(root)])
                      == os.path.normcase(root))
        except ValueError:  # another drive, or one relative and one absolute
            continue
        if inside and (best is None or len(root) > len(best[1])):
            best = (label, root)
    if best is None:
        return full
    rest = full[len(best[1]):].lstrip("\\/")
    return best[0] + (os.sep + rest if rest else "")


def print_about() -> int:
    import platform
    import ssl
    from importlib import metadata

    from ser2netweb import optional

    roots, used = _known_roots(), set()
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
        if where:
            where = _where(where, roots)
            used.add(where.split(os.sep, 1)[0])
        lines.append(f"  {dist:{width}} {version}  ({where})")
    legend = [f"{label} = {text}" for label, text in _ROOT_LABELS.items() if label in used]
    if legend:
        lines.append(f"  ({', '.join(legend)})")
    _write_utf8("\n".join(lines) + "\n")
    return 0


# what a server started with the old script name logs once (start-up notes)
OLD_SCRIPT_NOTE = ("{script} is the old name of ser2netweb.py (up to 2.9.x) and keeps working; "
                   "new set-ups use ser2netweb.py")


def main(argv: list[str] | None = None, started_as: str = "") -> int:
    """The command line. ``started_as``: the name of the start script the user ran when it
    is not ser2netweb.py (the old ser2net.py), for one line in the start-up log."""
    args = parse_args(argv)
    apply_lib_override()
    if args.licenses:
        return print_licenses()
    if args.uninstall_service:
        return _service_call(lambda ws: ws.uninstall(args.service_name))
    if args.healthcheck:  # stdlib only: no bootstrap needed
        from ser2netweb import healthcheck

        ok, msg = healthcheck.check(args.config or os.path.join(args.data_dir, "config.json"))
        print(msg)
        return 0 if ok else 1

    config_path = args.config or os.path.join(args.data_dir, "config.json")
    split = split_layout_note(args)
    if split:
        print(f"ser2net: {split}", file=sys.stderr)
        from ser2netweb import standalone  # (its notes are the lines all.log gets at start-up)

        standalone.notes.append(split)
    if started_as:
        from ser2netweb import standalone

        standalone.notes.append(OLD_SCRIPT_NOTE.format(script=started_as))
    if args.service:
        return _service(args, config_path)

    # 1. dependencies. The one-off commands may run beside a running ser2net, so they
    # leave an out-of-date ./lib as it is (with a warning); a server start refreshes it.
    one_off = (args.about or args.set_password is not None or args.sign_out_all
               or args.reload or args.install_service)
    load_dependencies(args, refresh=not one_off)

    if args.about:
        return print_about()

    if args.set_password is not None or args.sign_out_all or args.reload:
        from ser2netweb import accounts

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
    sys.path.insert(0, ROOT)  # ensure local packages (ser2netweb/, bootstrap) import first
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
        from ser2netweb import standalone  # (its notes are the lines all.log gets at start-up)

        standalone.notes.extend(bootstrap.notes)


def _import_runtime():
    """ser2netweb.runtime, which imports the dependencies. When they come from a ./lib an older
    release installed that could not be refreshed (the bootstrap said why) and lack what
    this version needs, the start stops with that instead of a traceback."""
    try:
        from ser2netweb import runtime
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

    from ser2netweb import winservice

    stop_asked = threading.Event()
    data_dir = os.path.dirname(os.path.abspath(config_path))
    ready_hooks: list = []  # winservice.run's on_ready: runtime.when_ready, once imported

    def serve() -> int:
        try:
            load_dependencies(args, refresh=True)
            runtime = _import_runtime()
        except SystemExit as e:  # a service has no console: all.log and the event log
            if isinstance(e.code, str):
                from ser2netweb import clock

                log = os.path.join(data_dir, "all.log")
                with contextlib.suppress(OSError), open(log, "a", encoding="utf-8") as fh:
                    fh.write(f"{clock.stamp()} {e.code}\n")
                winservice.report_event(args.service_name, (
                    f"The ser2net service '{args.service_name}' stopped: {e.code}\n\n"
                    f"Details: {log}"))
            raise
        for hook in ready_hooks:  # RUNNING once the server is ready
            runtime.when_ready(hook)
        if stop_asked.is_set():  # the SCM asked to stop while they loaded
            runtime.request_shutdown()
        return runtime.service_main(config_path, args.service_name)

    def stop() -> None:
        stop_asked.set()
        runtime = sys.modules.get("ser2netweb.runtime")
        if runtime is not None and hasattr(runtime, "request_shutdown"):
            runtime.request_shutdown()

    return winservice.run(args.service_name, serve, stop,
                          log_path=os.path.join(data_dir, winservice.SERVICE_LOG),
                          on_ready=ready_hooks.append)


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
    from ser2netweb import winservice

    _write_utf8(
        "\nWindows Firewall: this service listens for other hosts, which the firewall "
        "blocks until you allow it (a service gets no prompt). From an elevated prompt:\n  "
        + winservice.firewall_command(service_name) + "\n  (remove it later with: "
        f'netsh advfirewall firewall delete rule name="{winservice.display_name(service_name)}")'
        "\nSee docs/WINDOWS.md.\n")


def _service_call(fn) -> int:
    from ser2netweb import winservice

    try:
        fn(winservice)
    except winservice.ServiceError as e:
        print(f"ser2net: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
