"""Offline dependency bootstrapper for ser2net.

On first run this installs the bundled wheels from ``vendor/wheels/`` into a
local ``lib/`` directory (added to ``sys.path`` by ``ser2net.py``), so the app
runs on any machine that merely has a Python 3.11+ interpreter — no internet,
no virtualenv, no system-wide installs required.

It is idempotent: if every dependency already imports, it does nothing.

Usage (normally invoked automatically by ser2net.py / start.bat):
    python3 bootstrap.py            # offline install from vendor/wheels -> ./lib
    python3 bootstrap.py --online   # allow falling back to PyPI if a wheel is missing
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(ROOT, "lib")
WHEELS_DIR = os.path.join(ROOT, "vendor", "wheels")

# (import name, pip/requirement name) for the modules we must be able to import.
REQUIRED = [
    ("serial", "pyserial"),
    ("serial_asyncio_fast", "pyserial-asyncio-fast"),
    ("starlette", "starlette"),
    ("uvicorn", "uvicorn"),
    ("websockets", "websockets"),
    ("multipart", "python-multipart"),
    ("jinja2", "jinja2"),
    ("psutil", "psutil"),
]


def _ensure_lib_on_path() -> None:
    if os.path.isdir(LIB_DIR) and LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)


def missing_modules() -> list[tuple[str, str]]:
    _ensure_lib_on_path()
    missing = []
    for import_name, pip_name in REQUIRED:
        if importlib.util.find_spec(import_name) is None:
            missing.append((import_name, pip_name))
    return missing


def install(online: bool = False) -> None:
    """Install bundled wheels into ./lib. Falls back to PyPI only if online=True."""
    os.makedirs(LIB_DIR, exist_ok=True)
    req_file = os.path.join(ROOT, "requirements.txt")

    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", LIB_DIR,
        "--upgrade",
        "-r", req_file,
    ]
    if os.path.isdir(WHEELS_DIR):
        cmd += ["--find-links", WHEELS_DIR]
    if not online:
        cmd += ["--no-index"]

    print("[bootstrap] installing dependencies into ./lib ...")
    print("[bootstrap] $", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        if not online:
            print(
                "[bootstrap] offline install failed. Either the bundled wheels in\n"
                f"            {WHEELS_DIR}\n"
                "            do not match this platform/Python version, or they are\n"
                "            missing. Re-run with --online on a networked machine, or\n"
                "            populate vendor/wheels/ with matching wheels.\n"
                "            (See README.md > Offline installation.)",
                file=sys.stderr,
            )
        sys.exit(result.returncode)
    _ensure_lib_on_path()
    print("[bootstrap] done.")


def ensure(online: bool = False) -> None:
    """Make sure all required modules are importable; install if not."""
    if not missing_modules():
        return
    install(online=online)
    still_missing = missing_modules()
    if still_missing:
        names = ", ".join(p for _, p in still_missing)
        print(f"[bootstrap] ERROR: still missing after install: {names}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    ensure(online="--online" in sys.argv)
