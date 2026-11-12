"""Offline dependency bootstrapper for ser2net.

On first run this installs the bundled wheels from ``vendor/wheels/`` into a
local ``lib/`` directory (added to ``sys.path`` by ``ser2net.py``), so the app
runs on any machine that merely has a Python 3.10+ interpreter — no internet,
no virtualenv, no system-wide installs required.

Each install records what it was made for in ``lib/.ser2net-deps.json``: the pins of
requirements.txt, the Python version and the platform. A ser2net start (not a one-off
command such as --set-password, which may run beside a running server) refreshes a lib/
that was installed for anything else — an upgrade that changed a pin, a lib/ from before
this record existed, a new Python — once, offline, in place: packages added to lib/ by
hand stay, the old versions' metadata goes, and a refresh that can't be done (no matching
wheels, lib/ not writable, another running ser2net using it) keeps the old lib/ in use with
a warning instead of stopping the start. Otherwise, if every dependency imports, it does
nothing (a virtualenv or system install without lib/ is never touched).

Before installing, it checks every bundled wheel listed in ``vendor/wheels/SHA256SUMS``
against its recorded sha256 and refuses to install from a set where one differs (a
damaged or replaced file). Wheels that are not listed — e.g. ones you add for the optional
features — are installed as they are.

Usage (normally invoked automatically by ser2net.py / start.bat):
    python3 bootstrap.py            # (re)install from vendor/wheels -> ./lib, offline
    python3 bootstrap.py --online   # allow falling back to PyPI if a wheel is missing
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig

ROOT = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(ROOT, "lib")
WHEELS_DIR = os.path.join(ROOT, "vendor", "wheels")
REQ_FILE = os.path.join(ROOT, "requirements.txt")
STAMP = ".ser2net-deps.json"  # in LIB_DIR: what its packages were installed for

# Warnings for the start-up log: ser2net.py hands them to all.log (printed here too).
notes: list[str] = []

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


def altered_wheels(wheels_dir: str | None = None) -> list[str]:
    """The wheels SHA256SUMS (tools/refresh_wheels.py writes it) lists whose content
    is not what it records."""
    wheels_dir = WHEELS_DIR if wheels_dir is None else wheels_dir
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


def _warn(message: str) -> None:
    notes.append(f"WARNING: {message}")
    sys.stdout.flush()  # (in order with the lines before it, also in a pipe or the journal)
    print(f"[bootstrap] WARNING: {message}", file=sys.stderr)


# ---------------------------------------------------------------- what lib/ is for
def wanted_stamp() -> dict[str, str]:
    """What lib/ must have been installed for: requirements.txt's requirement lines
    (a comment-only change refreshes nothing), this Python and this platform."""
    lines = []
    try:
        with open(REQ_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    lines.append(" ".join(line.split()))
    except OSError:
        pass
    tag = sys.implementation.cache_tag or (
        f"{sys.implementation.name}-{sys.version_info[0]}{sys.version_info[1]}")
    return {"requirements": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
            "python": tag + getattr(sys, "abiflags", ""),
            "platform": sysconfig.get_platform()}


def read_stamp() -> dict | None:
    try:
        with open(os.path.join(LIB_DIR, STAMP), encoding="utf-8") as fh:
            stamp = json.load(fh)
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


def _write_stamp() -> None:
    path = os.path.join(LIB_DIR, STAMP)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(wanted_stamp(), fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        _warn(f"could not record what ./lib was installed for ({e}): it is installed "
              "again on the next start")
        try:
            os.remove(tmp)
        except OSError:
            pass


def lib_is_stale() -> bool:
    """lib/ exists and was installed for other pins, another Python or platform, or by a
    release before 2.9 (no record, but packages pip put there). An empty lib/ or none at
    all is not: then the dependencies come from elsewhere (a virtualenv) or are missing."""
    if not os.path.isdir(LIB_DIR):
        return False
    stamp = read_stamp()
    if stamp is None:
        try:
            return any(n.endswith(".dist-info") for n in os.listdir(LIB_DIR))
        except OSError:
            return False
    return {k: stamp.get(k) for k in ("requirements", "python", "platform")} != wanted_stamp()


# ---------------------------------------------------------------- installing
def _project(dist_info: str) -> str:
    """The normalized project name of a ``<name>-<version>.dist-info`` folder."""
    base = dist_info[: -len(".dist-info")]
    return re.sub(r"[-_.]+", "-", base.rsplit("-", 1)[0] if "-" in base else base).lower()


def _dist_infos() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    try:
        names = os.listdir(LIB_DIR)
    except OSError:
        return out
    for name in names:
        if name.endswith(".dist-info"):
            try:
                st = os.stat(os.path.join(LIB_DIR, name))
            except OSError:
                continue
            out[name] = (st.st_ino, st.st_ctime_ns)
    return out


def _drop_old_metadata(before: dict[str, tuple[int, int]]) -> list[str]:
    """pip --target --upgrade replaces a package's folders, but its old version's
    <name>-<old>.dist-info stays next to the new one. Remove those: the metadata of a
    project pip (re)installed just now (new, or replaced) that it didn't write. Projects
    it didn't install (added to lib/ by hand) are left alone."""
    after = _dist_infos()
    fresh = {n for n, st in after.items() if before.get(n) != st}
    installed = {_project(n) for n in fresh}
    removed = []
    for name in sorted(after):
        if name not in fresh and _project(name) in installed:
            shutil.rmtree(os.path.join(LIB_DIR, name), ignore_errors=True)
            removed.append(name)
    return removed


def _pip_install(online: bool) -> bool:
    """pip install --target lib --upgrade the pinned set: pip builds it in a temporary
    folder first and then replaces each package's folders in lib/, keeping anything else
    there. True when it worked (then the old metadata is gone and the record written)."""
    os.makedirs(LIB_DIR, exist_ok=True)
    before = _dist_infos()
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", LIB_DIR,
        "--upgrade",
        "-r", REQ_FILE,
    ]
    if os.path.isdir(WHEELS_DIR):
        cmd += ["--find-links", WHEELS_DIR]
    if not online:
        cmd += ["--no-index"]

    print("[bootstrap] installing dependencies into ./lib ...")
    print("[bootstrap] $", " ".join(cmd), flush=True)  # (before pip's own output)
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
        return False
    for name in _drop_old_metadata(before):
        print(f"[bootstrap] removed the old version's metadata {name}")
    _write_stamp()
    _ensure_lib_on_path()
    print("[bootstrap] done.", flush=True)
    return True


def _refuse_altered() -> list[str]:
    return altered_wheels() if os.path.isdir(WHEELS_DIR) else []


def install(online: bool = False) -> None:
    """Install bundled wheels into ./lib. Falls back to PyPI only if online=True."""
    altered = _refuse_altered()
    if altered:
        print(
            "[bootstrap] ERROR: these bundled wheels don't match vendor/wheels/SHA256SUMS\n"
            "            (damaged or replaced files): " + ", ".join(altered) + "\n"
            "            Restore them from the release / git checkout, or remove their\n"
            "            lines from SHA256SUMS if you replaced them on purpose.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not _pip_install(online):
        sys.exit(1)


def lib_in_use() -> str:
    """Another running program that has files of lib/ loaded, or "" when none is seen.
    pip replaces the package folders, which must not happen under a running ser2net:
    on Windows a loaded extension can't be deleted, so its folder would be left half
    removed; on Linux the running server would import new files into old code.
    Best effort: Windows (an extension module that can't be opened for writing) and
    Linux (the memory maps of the processes this account may read)."""
    if os.name == "nt":
        for folder, _dirs, files in os.walk(LIB_DIR):
            for name in files:
                if name.lower().endswith((".pyd", ".dll")):
                    path = os.path.join(folder, name)
                    try:
                        with open(path, "r+b"):
                            pass
                    except PermissionError:
                        return f"a running program has {path} loaded"
                    except OSError:
                        continue
        return ""
    if not sys.platform.startswith("linux"):
        return ""
    lib = os.path.realpath(LIB_DIR) + os.sep
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit() and int(p) != os.getpid()]
    except OSError:
        return ""
    for pid in pids:
        try:
            with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as fh:
                if any(lib in line for line in fh):
                    return f"process {pid} has files of it loaded"
        except OSError:
            continue  # gone, or another account's
    return ""


def refresh(online: bool = False) -> bool:
    """Bring an existing lib/ up to the pins (an upgrade, another Python). Never stops
    the start: when it can't be done the old lib/ stays in use, with a warning (False)."""
    print("[bootstrap] ./lib was installed for other dependency versions (an upgrade?), "
          "another Python or platform: installing the pinned ones into it")
    keep = ("ser2net keeps using the old ./lib: run python3 bootstrap.py in "
            f"{ROOT} once it can, as the account ser2net runs as")
    altered = _refuse_altered()
    if altered:
        _warn("not refreshing ./lib: these bundled wheels don't match "
              f"vendor/wheels/SHA256SUMS: {', '.join(altered)}. {keep}")
        return False
    if importlib.util.find_spec("pip") is None:
        _warn(f"not refreshing ./lib: this Python ({sys.executable}) has no pip. {keep}")
        return False
    try:
        folders = [LIB_DIR] + [os.path.join(LIB_DIR, n) for n in os.listdir(LIB_DIR)]
    except OSError:
        folders = [LIB_DIR]
    unwritable = [p for p in folders
                  if os.path.isdir(p) and not os.path.islink(p) and not os.access(p, os.W_OK)]
    if unwritable:
        _warn(f"not refreshing ./lib: this account can't change {unwritable[0]}. {keep}")
        return False
    busy = lib_in_use()
    if busy:
        _warn(f"not refreshing ./lib: {busy} (another ser2net?). Stop it, then start "
              f"ser2net again or run python3 bootstrap.py in {ROOT}")
        return False
    if not _pip_install(online):
        _warn(f"refreshing ./lib failed (see above). {keep}")
        return False
    return True


def ensure(online: bool = False, refresh_lib: bool = True) -> None:
    """Make sure all required modules are importable; install if not. A lib/ installed
    for anything else is refreshed first when refresh_lib (a server start); a one-off
    command only warns."""
    kept = False
    if lib_is_stale():
        if refresh_lib:
            kept = not refresh(online)
        else:
            _warn("./lib was installed for other dependency versions (an upgrade?), "
                  "another Python or platform; the next ser2net start installs the pinned "
                  f"ones (or run python3 bootstrap.py in {ROOT})")
    if not missing_modules():
        return
    if kept:  # (not a second try: it may be in use)
        names = ", ".join(p for _, p in missing_modules())
        print(f"[bootstrap] ERROR: missing: {names}, and ./lib could not be refreshed "
              "(see above)", file=sys.stderr)
        sys.exit(1)
    install(online=online)
    still_missing = missing_modules()
    if still_missing:
        names = ", ".join(p for _, p in still_missing)
        print(f"[bootstrap] ERROR: still missing after install: {names}", file=sys.stderr)
        sys.exit(1)


def warn_if_stale() -> None:
    """For --no-bootstrap: say so when lib/ doesn't match the pins (nothing is changed)."""
    if lib_is_stale():
        _warn("./lib was installed for other dependency versions (an upgrade?), another "
              f"Python or platform, and --no-bootstrap leaves it as it is: run python3 "
              f"bootstrap.py in {ROOT} (as the account ser2net runs as)")


def main(argv: list[str]) -> int:
    """python3 bootstrap.py: (re)install the pinned set into lib/ now, whatever is there.
    Refuses while another program uses lib/ (stop ser2net first)."""
    online = "--online" in argv
    if os.path.isdir(LIB_DIR) and (busy := lib_in_use()):
        print(f"[bootstrap] ERROR: ./lib is in use: {busy} (a running ser2net?). Stop it "
              "first.", file=sys.stderr)
        return 1
    install(online=online)
    still_missing = missing_modules()
    if still_missing:
        names = ", ".join(p for _, p in still_missing)
        print(f"[bootstrap] ERROR: still missing after install: {names}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
