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
from __future__ import annotations

import argparse
import os
import sys

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
        help="Directory for config.json, backups and logs (default: ./data; for "
             "--install-service: the data directory the service already uses, or for a "
             "new service from the standalone exe %%ProgramData%%\\ser2net).",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.json (default: <data-dir>/config.json).",
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
    _write_utf8(
        f"ser2net {__version__} is licensed under the GNU AGPL-3.0-only (LICENSE) or a "
        "commercial license (COMMERCIAL_LICENSE.md).\nThird-party components: "
        f"{os.path.join(ROOT, 'THIRD-PARTY-NOTICES.md')}\n")
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
                  ("starlette", "starlette"), ("uvicorn", "uvicorn"),
                  ("paho-mqtt", "paho.mqtt"), ("ldap3", "ldap3"), ("pyasn1", "pyasn1"),
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

    # 1. dependencies — a frozen build has everything bundled, so skip the bootstrap
    if not FROZEN:
        sys.path.insert(0, ROOT)  # ensure local packages (app/, bootstrap) import first
        if not args.no_bootstrap:
            import bootstrap

            bootstrap.ensure(online=args.online)
        else:
            lib = os.path.join(ROOT, "lib")
            if os.path.isdir(lib):
                sys.path.insert(0, lib)

    if args.about:
        return print_about()

    # 2. resolve config path
    config_path = args.config or os.path.join(args.data_dir, "config.json")

    if args.set_password is not None or args.sign_out_all:
        from app import accounts

        return accounts.main(args, config_path)

    # 3. hand off
    from app import runtime

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
    if args.service:
        from app import winservice

        return winservice.run(args.service_name,
                              lambda: runtime.service_main(config_path, args.service_name),
                              runtime.request_shutdown,
                              log_path=os.path.join(os.path.dirname(os.path.abspath(config_path)),
                                                    winservice.SERVICE_LOG))
    return runtime.main(config_path=config_path, reconfigure=args.reconfigure)


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
