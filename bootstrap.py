"""Offline dependency bootstrapper for ser2net.

On first run this installs the bundled wheels from ``vendor/wheels/`` into a
local ``lib/`` directory (added to ``sys.path`` by ``ser2net.py``), so the app
runs on any machine that merely has a Python 3.10+ interpreter — no internet,
no virtualenv, no system-wide installs required.

It is idempotent: if every dependency already imports, it does nothing.

Before installing, it checks every bundled wheel listed in ``vendor/wheels/SHA256SUMS``
against its recorded sha256 and refuses to install from a set where one differs (a
damaged or replaced file). Wheels that are not listed — e.g. ones you add for the optional
features — are installed as they are.

Usage (normally invoked automatically by ser2net.py / start.bat):
    python3 bootstrap.py            # offline install from vendor/wheels -> ./lib
    python3 bootstrap.py --online   # allow falling back to PyPI if a wheel is missing
"""
from __future__ import annotations

import hashlib
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

# Python 3.10 backports: ser2net uses these as stand-ins for the native 3.11+
# asyncio.TaskGroup / asyncio.Runner / except* (see app/engine/bridge.py and
# app/runtime.py). Include them in the self-check so a broken offline install on
# 3.10 fails here with a clear message instead of an ImportError at startup.
if sys.version_info < (3, 11):
    REQUIRED += [
        ("exceptiongroup", "exceptiongroup"),
        ("taskgroup", "taskgroup"),
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


def altered_wheels(wheels_dir: str = WHEELS_DIR) -> list[str]:
    """The wheels SHA256SUMS (tools/refresh_wheels.py writes it) lists whose content
    is not what it records."""
    sums = os.path.join(wheels_dir, "SHA256SUMS")
    if not os.path.isfile(sums):
        return []
    bad = []
    with open(sums, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            digest, name = line.split(maxsplit=1)
            path = os.path.join(wheels_dir, os.path.basename(name.strip().lstrip("*")))
            if not os.path.isfile(path):
                continue  # pip reports a wheel that is needed and missing
            h = hashlib.sha256()
            with open(path, "rb") as wf:
                for chunk in iter(lambda: wf.read(1 << 16), b""):
                    h.update(chunk)
            if h.hexdigest() != digest.lower():
                bad.append(os.path.basename(path))
    return bad


def install(online: bool = False) -> None:
    """Install bundled wheels into ./lib. Falls back to PyPI only if online=True."""
    altered = altered_wheels() if os.path.isdir(WHEELS_DIR) else []
    if altered:
        print(
            "[bootstrap] ERROR: these bundled wheels don't match vendor/wheels/SHA256SUMS\n"
            "            (damaged or replaced files): " + ", ".join(altered) + "\n"
            "            Restore them from the release / git checkout, or remove their\n"
            "            lines from SHA256SUMS if you replaced them on purpose.",
            file=sys.stderr,
        )
        sys.exit(1)
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
