"""Hash-locked install sets of the Docker image and the standalone executables.

    python tools/refresh_locks.py            # rewrite both lock files from the pins (network)
    python tools/refresh_locks.py --check    # the locks pin what the requirement files pin
    python tools/refresh_locks.py --verify   # (network) every hash is a file PyPI publishes,
                                             # every dependency is locked, every target installs

The image and the executables run Python 3.12 (Linux x86_64 and aarch64, Windows x64) and
install with ``pip install --require-hashes --only-binary=:all:``, so pip takes no file
whose sha256 isn't listed (a file added to a release later, or one a mirror or proxy
serves instead, is refused) and fails when a dependency isn't locked:

  requirements-lock.txt   the pins of requirements.txt (the ones Python 3.12 selects) and
                          requirements-optional.txt: what the image and the executables run.
  requirements-build.txt  PyInstaller (its version is kept from this file) and its
                          dependencies: the standalone builds only.

requirements.txt stays hash-free (``pip install -r requirements.txt <extra>`` keeps working).
Each lock line lists the sha256 of every file PyPI publishes for that release, as
pip-compile and Dependabot write them; Dependabot reads both files and updates their
versions and hashes together with requirements.txt's. --check (tests/test_build_lock.py)
fails when a lock and the requirement files disagree; --verify runs in the CI job
"offline wheels match requirements.txt".

Markers are evaluated for each target environment (pip's --platform doesn't change marker
evaluation, so pip can't resolve the Windows set on Linux): a dependency only one OS
installs gets a platform_system marker.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refresh_wheels as rw  # tools/ is not a package

ROOT = rw.ROOT
LOCK = "requirements-lock.txt"
BUILD = "requirements-build.txt"
PYTHON = "3.12"
GLIBC = (2, 28)  # the Linux build container (AlmaLinux 8)
# the environments the image and the executables are built for, as markers see them
ENVS = {
    "Linux x86_64": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
                     "platform_machine": "x86_64"},
    "Linux aarch64": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
                      "platform_machine": "aarch64"},
    "Windows x64": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
                    "platform_machine": "AMD64"},
}
_COMMON_ENV = {"python_version": PYTHON, "python_full_version": PYTHON + ".0",
               "implementation_name": "cpython", "platform_python_implementation": "CPython",
               "implementation_version": PYTHON + ".0", "platform_release": "",
               "platform_version": "", "extra": ""}


def _packaging():
    """packaging's Marker, Requirement, SpecifierSet and Version: the installed
    package, else pip's copy (as tools/third_party.py)."""
    try:
        mods = [importlib.import_module(f"packaging.{m}")
                for m in ("markers", "requirements", "specifiers", "version")]
    except ImportError:
        mods = [importlib.import_module(f"pip._vendor.packaging.{m}")
                for m in ("markers", "requirements", "specifiers", "version")]
    return mods[0].Marker, mods[1].Requirement, mods[2].SpecifierSet, mods[3].Version


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def env_of(target: str) -> dict[str, str]:
    return {**_COMMON_ENV, **ENVS[target]}


# --------------------------------------------------------------------------
# what the requirement files pin for Python 3.12
# --------------------------------------------------------------------------
def runtime_pins(root: str = ROOT) -> dict[str, tuple[str, str]]:
    """{name: (version, marker)}: requirements.txt as Python 3.12 selects it (only a
    platform_system clause is kept as the marker) plus requirements-optional.txt."""
    out: dict[str, tuple[str, str]] = {}
    for fn in ("requirements.txt", "requirements-optional.txt"):
        for name, version, marker in rw.read_requirements(os.path.join(root, fn)):
            systems = [s for s in rw.SYSTEMS if rw.marker_applies(marker, PYTHON, s)]
            if not systems:
                continue
            keep = "" if len(systems) == len(rw.SYSTEMS) else f'platform_system == "{systems[0]}"'
            if name in out:
                raise ValueError(f"{name} is pinned twice for Python {PYTHON}")
            out[name] = (version, keep)
    return out


# --------------------------------------------------------------------------
# lock files
# --------------------------------------------------------------------------
def read_lock(path: str) -> dict[str, tuple[str, str, list[str]]]:
    """{canonical name: (version, marker, [sha256, ...])} of a lock file."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    out: dict[str, tuple[str, str, list[str]]] = {}
    for logical in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = logical.split("#", 1)[0].strip()
        if not line:
            continue
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})\b", line)
        spec = re.sub(r"\s--hash=\S+", "", " " + line).strip()
        req, _, marker = spec.partition(";")
        name, sep, version = req.strip().partition("==")
        if not sep:
            raise ValueError(f"{os.path.basename(path)}: not an exact pin: {line!r}")
        if canonical(name) in out:
            raise ValueError(f"{os.path.basename(path)}: {name} is locked twice")
        out[canonical(name)] = (version.strip(), marker.strip(), hashes)
    return out


def _lock_text(header: str, entries: list[tuple[str, str, str, list[str]]]) -> str:
    lines = [header.rstrip() + "\n"]
    for name, version, marker, hashes in sorted(entries, key=lambda e: canonical(e[0])):
        head = f"{name}=={version}" + (f" ; {marker}" if marker else "")
        lines.append(" \\\n    ".join([head] + [f"--hash=sha256:{h}" for h in hashes]) + "\n")
    return "".join(lines)


LOCK_HEADER = """\
# Hash-locked: what the Docker image and the standalone executables install (Python 3.12,
# Linux x86_64/aarch64 and Windows x64), with
#   pip install --require-hashes --only-binary=:all: -r requirements-lock.txt
# The pins of requirements.txt (as Python 3.12 selects them) and requirements-optional.txt,
# each with the sha256 of every file PyPI publishes for it. Generated by
# `python tools/refresh_locks.py` after a pin changes (Dependabot updates it together with
# requirements.txt); tests/test_build_lock.py checks that it matches.
"""
BUILD_HEADER = """\
# Hash-locked: PyInstaller and its dependencies, for the standalone builds only
# (tools/build_standalone_linux.sh, tools/build_standalone_windows.ps1), installed together
# with requirements-lock.txt by pip --require-hashes. Change the PyInstaller version here,
# then run `python tools/refresh_locks.py` to lock its dependencies (Dependabot updates
# them like any pin).
"""


# --------------------------------------------------------------------------
# PyPI
# --------------------------------------------------------------------------
_cache: dict[str, dict] = {}


def pypi(name: str, version: str | None = None) -> dict:
    url = (f"https://pypi.org/pypi/{name}/{version}/json" if version
           else f"https://pypi.org/pypi/{name}/json")
    if url not in _cache:
        with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (a fixed https:// URL)
            _cache[url] = json.load(r)
    return _cache[url]


def _glibc_ok(plat: str, machine: str) -> bool:
    m = re.fullmatch(r"manylinux_(\d+)_(\d+)_" + machine, plat)
    if m:
        return (int(m[1]), int(m[2])) <= GLIBC
    return plat in {f"manylinux1_{machine}", f"manylinux2010_{machine}",
                    f"manylinux2014_{machine}"}


def wheel_installs(filename: str, target: str) -> bool:
    """Whether pip on CPython 3.12 in the target environment can install this wheel."""
    parsed = rw.parse_wheel(filename)
    if not filename.endswith(".whl") or parsed is None:
        return False
    _n, _v, pys, abis, plats = parsed
    machine = ENVS[target]["platform_machine"]
    if target.startswith("Linux"):
        plat_ok = any(p == "any" or _glibc_ok(p, machine) for p in plats)
    else:
        plat_ok = any(p in ("any", "win_amd64") for p in plats)
    cp = "cp" + PYTHON.replace(".", "")
    py_ok = any(p in ("py3", "py2.py3", cp) or (p.startswith("cp3") and "abi3" in abis
                                                 and int(p[3:]) <= int(cp[3:])) for p in pys)
    abi_ok = any(a in ("none", "abi3", cp) for a in abis)
    return plat_ok and py_ok and abi_ok


def release_files(name: str, version: str) -> list[dict]:
    return [u for u in pypi(name, version)["urls"] if not u.get("yanked")]


def requires(name: str, version: str, target: str) -> list:
    """The Requirement objects name==version needs in the target (markers evaluated)."""
    _M, Requirement, _S, _V = _packaging()
    out = []
    for spec in pypi(name, version)["info"].get("requires_dist") or []:
        req = Requirement(spec)
        if req.marker is None or req.marker.evaluate(env_of(target)):
            out.append(req)
    return out


def newest(name: str, spec, targets: list[str]) -> str:
    """The newest release of name matching spec that installs on every target."""
    _M, _R, SpecifierSet, Version = _packaging()
    doc = pypi(name)
    for v in sorted(doc["releases"], key=Version, reverse=True):
        ver = Version(v)
        if ver.is_prerelease or not spec.contains(ver):
            continue
        files = [u for u in doc["releases"][v] if not u.get("yanked")]
        rp = next((u.get("requires_python") for u in files if u.get("requires_python")), None)
        if rp and not SpecifierSet(rp).contains(PYTHON + ".0"):
            continue
        if all(any(wheel_installs(u["filename"], t) for u in files) for t in targets):
            return v
    raise SystemExit(f"no release of {name} {spec} installs on {', '.join(targets)}")


def closure(roots: dict[str, str], pinned: dict[str, str], target: str,
            choose: bool, prefer: dict[str, str] | None = None) -> dict[str, str]:
    """{name: version} of roots and everything they need in the target. A dependency in
    pinned must satisfy its specifier; any other is chosen when choose (its version in
    prefer if that still matches, else the newest that does), else it is an error (an
    unlocked dependency)."""
    _M, _R, SpecifierSet, Version = _packaging()
    pins = {canonical(k): v for k, v in pinned.items()}
    kept = {canonical(k): v for k, v in (prefer or {}).items()}
    have = {canonical(k): v for k, v in roots.items()}
    todo = list(have)
    while todo:
        name = todo.pop()
        for req in requires(name, have[name], target):
            dep = canonical(req.name)
            if dep in have:
                version = have[dep]
            elif dep in pins:
                version = pins[dep]
            elif choose and dep in kept and req.specifier.contains(Version(kept[dep]),
                                                                    prereleases=True):
                version = kept[dep]
            elif choose:
                version = newest(dep, req.specifier, [target])
            else:
                raise SystemExit(f"{name} {have[name]} needs {req} on {target}, which no "
                                 "lock pins: run python tools/refresh_locks.py")
            if not req.specifier.contains(Version(version), prereleases=True):
                raise SystemExit(f"{name} {have[name]} needs {req} on {target}, but "
                                 f"{dep} {version} is pinned")
            if dep not in have:
                have[dep] = version
                todo.append(dep)
    return have


def _marker(present: set[str]) -> str:
    if present == set(ENVS):
        return ""
    systems = {ENVS[t]["platform_system"] for t in present}
    if len(systems) == 1 and present == {t for t in ENVS if ENVS[t]["platform_system"] in systems}:
        return f'platform_system == "{systems.pop()}"'
    raise SystemExit(f"a dependency only some targets of one OS need: {sorted(present)}")


def _hashes(name: str, version: str) -> list[str]:
    files = release_files(name, version)
    if not files:
        raise SystemExit(f"PyPI has no files for {name} {version}")
    return sorted(u["digests"]["sha256"] for u in files)


def _build_pins() -> dict[str, str]:
    path = os.path.join(ROOT, BUILD)
    return {n: v for n, (v, _m, _h) in read_lock(path).items()} if os.path.isfile(path) else {}


def refresh(pyinstaller: str | None = None) -> int:
    runtime = runtime_pins()
    flat = {n: v for n, (v, _m) in runtime.items()}
    kept = _build_pins()  # PyInstaller's dependencies keep their versions while they fit
    pyi = pyinstaller or kept.get("pyinstaller")
    if not pyi:
        raise SystemExit(f"{BUILD} pins no pyinstaller version: pass --pyinstaller X.Y.Z")
    # the runtime set must be complete: nothing chosen here, every dependency pinned
    runtime_on: dict[str, set[str]] = {}
    build_on: dict[str, set[str]] = {}
    build_versions: dict[str, str] = {}
    for target in ENVS:
        roots = {n: v for n, (v, m) in runtime.items()
                 if not m or m == f'platform_system == "{ENVS[target]["platform_system"]}"'}
        got = closure(roots, {}, target, choose=False)
        for n in got:
            runtime_on.setdefault(n, set()).add(target)
        tools = closure({"pyinstaller": pyi}, {**flat, **build_versions}, target, choose=True,
                        prefer=kept)
        for n, v in tools.items():
            if n in {canonical(k) for k in flat}:
                continue
            build_versions[n] = v
            build_on.setdefault(n, set()).add(target)
    names = {canonical(n): n for n in runtime}
    lock_entries = []
    for n, present in runtime_on.items():
        version, marker = runtime[names[n]]
        if _marker(present) != marker:
            raise SystemExit(f"{names[n]}: requirements mark it {marker or 'for every OS'}, "
                             f"but the targets that need it are {sorted(present)}")
        lock_entries.append((names[n], version, marker, _hashes(n, version)))
    missing = set(names) - set(runtime_on)
    if missing:
        raise SystemExit(f"pinned, but no target installs it: {sorted(missing)}")
    build_entries = [(n, build_versions[n], _marker(present), _hashes(n, build_versions[n]))
                     for n, present in build_on.items()]
    for fn, header, entries in ((LOCK, LOCK_HEADER, lock_entries),
                                (BUILD, BUILD_HEADER, build_entries)):
        with open(os.path.join(ROOT, fn), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_lock_text(header, entries))
        print(f"wrote {fn}: " + ", ".join(f"{n}=={v}" for n, v, _m, _h in
                                          sorted(entries, key=lambda e: canonical(e[0]))))
    return 0


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def check(root: str = ROOT) -> list[str]:
    """Offline: the locks pin exactly what the requirement files pin (Python 3.12)."""
    problems = []
    lock = read_lock(os.path.join(root, LOCK))
    build = read_lock(os.path.join(root, BUILD))
    want = {canonical(n): (v, m) for n, (v, m) in runtime_pins(root).items()}
    for n, (v, m) in want.items():
        if n not in lock:
            problems.append(f"{LOCK}: {n}=={v} is pinned but not locked")
        elif lock[n][:2] != (v, m):
            problems.append(f"{LOCK}: {n} is locked as {lock[n][0]} {lock[n][1]!r}, "
                            f"pinned as {v} {m!r}")
    problems += [f"{LOCK}: {n} is locked but not pinned" for n in sorted(set(lock) - set(want))]
    if "pyinstaller" not in build:
        problems.append(f"{BUILD}: no pyinstaller")
    problems += [f"{BUILD}: {n} is also in {LOCK}" for n in sorted(set(build) & set(lock))]
    for fn, entries in ((LOCK, lock), (BUILD, build)):
        problems += [f"{fn}: {n} has no --hash" for n, (_v, _m, h) in entries.items() if not h]
    return problems


def verify() -> list[str]:
    """Online: every listed hash is a file PyPI publishes for that release, each target
    finds an installable wheel among them, and each target's dependencies are all locked."""
    problems = check()
    entries = {**read_lock(os.path.join(ROOT, LOCK)), **read_lock(os.path.join(ROOT, BUILD))}
    for n, (v, _m, hashes) in sorted(entries.items()):
        published = {u["digests"]["sha256"]: u["filename"] for u in release_files(n, v)}
        unknown = [h for h in hashes if h not in published]
        if unknown:
            problems.append(f"{n} {v}: {len(unknown)} hash(es) PyPI doesn't publish for it")
    for target in ENVS:
        system = ENVS[target]["platform_system"]
        mine = {n: v for n, (v, m, _h) in entries.items()
                if not m or m == f'platform_system == "{system}"'}
        for n, v in mine.items():
            by_hash = {u["digests"]["sha256"]: u["filename"] for u in release_files(n, v)}
            names = [by_hash[h] for h in entries[n][2] if h in by_hash]
            if not any(wheel_installs(f, target) for f in names):
                problems.append(f"{n} {v}: no locked wheel installs on {target}")
        try:
            closure(mine, mine, target, choose=False)
        except SystemExit as e:
            problems.append(str(e))
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--verify", action="store_true")
    ap.add_argument("--pyinstaller", metavar="X.Y.Z", help="lock this PyInstaller version")
    args = ap.parse_args(argv)
    if args.check or args.verify:
        found = check() if args.check else verify()
        for p in found:
            print(f"error: {p}")
        if not found:
            print(f"{LOCK} and {BUILD} match the pins" + (" and PyPI" if args.verify else ""))
        return 1 if found else 0
    return refresh(args.pyinstaller)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
