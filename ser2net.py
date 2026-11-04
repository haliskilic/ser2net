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
        default=os.path.join(_app_dir(), "data"),
        help="Directory for config.json, backups and logs (default: ./data).",
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
    return p.parse_args(argv)


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

    lines = [f"ser2net {__version__}",
             f"Python {platform.python_version()} ({platform.python_implementation()}), "
             f"{ssl.OPENSSL_VERSION}",
             f"Platform: {platform.platform()} ({platform.machine()})",
             f"Build: {'standalone executable' if FROZEN else 'source'}"]
    for dist, module in (("pyserial", "serial"), ("starlette", "starlette"),
                         ("uvicorn", "uvicorn"), ("paho-mqtt", "paho.mqtt"),
                         ("ldap3", "ldap3"), ("authlib", "authlib"),
                         ("cryptography", "cryptography")):
        try:
            mod = __import__(module, fromlist=["__name__"])
        except Exception:
            lines.append(f"  {dist:13} not installed")
            continue
        version = getattr(mod, "__version__", None)
        if not isinstance(version, str):
            try:
                version = metadata.version(dist)
            except metadata.PackageNotFoundError:
                version = "?"
        where = os.path.dirname(getattr(mod, "__file__", "") or "")
        lines.append(f"  {dist:13} {version}  ({where})")
    _write_utf8("\n".join(lines) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_lib_override()
    if args.licenses:
        return print_licenses()

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

    # 3. hand off
    from app import runtime

    return runtime.main(config_path=config_path, reconfigure=args.reconfigure)


if __name__ == "__main__":
    raise SystemExit(main())
