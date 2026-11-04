"""Re-download vendor/wheels/ (the offline install set) to match requirements.txt.

    python tools/refresh_wheels.py           # update vendor/wheels/, drop stale wheels
    python tools/refresh_wheels.py --check   # only report what is missing / stale

bootstrap.py installs from vendor/wheels/ without network access, on Linux x86_64
and Windows x64, Python 3.10–3.14. For every pinned requirement (with its markers)
the set holds:
  * the cp311 + cp312 platform wheels when the package has compiled parts (or an
    abi3 wheel, which covers every version), and
  * a pure-Python ``py3-none-any`` wheel for the other Python versions — downloaded
    when the project publishes one, otherwise repacked from its cp311 wheel without
    the compiled speed-ups (markupsafe falls back to its Python implementation).
tests/test_vendor_wheels.py checks the result for every Python/OS combination.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHEELS = os.path.join(ROOT, "vendor", "wheels")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")

PYTHONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
SYSTEMS = ("Linux", "Windows")
PLATFORM_WHEEL_PYTHONS = ("3.11", "3.12")  # the rest use pure / abi3 wheels
PIP_PLATFORMS = {
    "Linux": ["manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64",
              "manylinux_2_12_x86_64", "manylinux2010_x86_64", "manylinux_2_5_x86_64",
              "manylinux1_x86_64"],
    "Windows": ["win_amd64"],
}


# --------------------------------------------------------------------------
# requirements.txt (only the forms it uses: name==version ; simple markers)
# --------------------------------------------------------------------------
def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


_MARKER = re.compile(r'^(python_version|platform_system)\s*(==|!=|<=|>=|<|>)\s*"([^"]+)"$')


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def marker_applies(marker: str, python: str, system: str) -> bool:
    """Evaluate the 'and'-joined python_version / platform_system markers used in
    requirements.txt. Anything else is an error, so a new marker can't be ignored."""
    if not marker:
        return True
    for clause in (c.strip() for c in marker.split(" and ")):
        m = _MARKER.match(clause)
        if not m:
            raise ValueError(f"unsupported marker in requirements.txt: {clause!r}")
        key, op, want = m.groups()
        if key == "platform_system":
            have, want_v = system, want
            ok = {"==": have == want_v, "!=": have != want_v}.get(op)
            if ok is None:
                raise ValueError(f"unsupported platform_system comparison: {clause!r}")
        else:
            have_t, want_t = _version_tuple(python), _version_tuple(want)
            ok = {"==": have_t == want_t, "!=": have_t != want_t, "<": have_t < want_t,
                  "<=": have_t <= want_t, ">": have_t > want_t, ">=": have_t >= want_t}[op]
        if not ok:
            return False
    return True


def read_requirements(path: str = REQUIREMENTS) -> list[tuple[str, str, str]]:
    """[(name, version, marker)] for every pinned line."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            spec, _, marker = line.partition(";")
            name, sep, version = spec.strip().partition("==")
            if not sep:
                raise ValueError(f"requirements.txt must pin exact versions: {line!r}")
            out.append((name.strip(), version.strip(), marker.strip()))
    return out


# --------------------------------------------------------------------------
# wheel tags
# --------------------------------------------------------------------------
_WHEEL = re.compile(r"^(?P<name>.+?)-(?P<ver>[^-]+)(-\d[^-]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")


def parse_wheel(filename: str):
    m = _WHEEL.match(filename)
    if not m:
        return None
    return (normalize(m["name"]), m["ver"], m["py"].split("."), m["abi"].split("."),
            m["plat"].split("."))


def wheel_fits(filename: str, python: str, system: str) -> bool:
    parsed = parse_wheel(filename)
    if parsed is None:
        return False
    _name, _ver, pys, abis, plats = parsed
    if system == "Linux":
        plat_ok = any(p == "any" or (p.startswith("manylinux") and p.endswith("_x86_64"))
                      for p in plats)
    else:
        plat_ok = any(p in ("any", "win_amd64") for p in plats)
    if not plat_ok:
        return False
    cp = "cp" + python.replace(".", "")
    minor = int(python.split(".")[1])
    for abi in abis:
        if abi == "none" and any(p in ("py3", "py2.py3") or p == cp for p in pys):
            return True
        if abi == cp and cp in pys:
            return True
        if abi == "abi3" and any(p.startswith("cp3") and int(p[3:]) <= minor for p in pys):
            return True
    return False


def problems(wheel_dir: str = WHEELS, path: str = REQUIREMENTS) -> tuple[list[str], list[str]]:
    """(missing, stale): requirement/environment pairs without a wheel, and wheel
    files no requirement uses."""
    files = sorted(f for f in os.listdir(wheel_dir) if f.endswith(".whl")) \
        if os.path.isdir(wheel_dir) else []
    used: set[str] = set()
    missing = []
    for name, version, marker in read_requirements(path):
        for python in PYTHONS:
            for system in SYSTEMS:
                if not marker_applies(marker, python, system):
                    continue
                fits = [f for f in files if (p := parse_wheel(f)) and p[0] == normalize(name)
                        and p[1] == version and wheel_fits(f, python, system)]
                if fits:
                    used.update(fits)
                else:
                    missing.append(f"{name}=={version} for Python {python} on {system}")
    stale = [f for f in files if f not in used]
    return missing, stale


# --------------------------------------------------------------------------
# download / repack
# --------------------------------------------------------------------------
def _pip_download(req: str, dest: str, python: str, platforms: list[str], abis: list[str],
                  implementation: str = "cp") -> bool:
    cmd = [sys.executable, "-m", "pip", "download", req, "--no-deps", "--only-binary=:all:",
           "--disable-pip-version-check", "-q", "-d", dest, "--python-version", python,
           "--implementation", implementation]
    for plat in platforms:
        cmd += ["--platform", plat]
    for abi in abis:
        cmd += ["--abi", abi]
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def repack_pure(wheel_path: str, dest_dir: str) -> str:
    """Rewrite a CPython wheel as py3-none-any without its compiled extension modules."""
    name, ver, *_ = parse_wheel(os.path.basename(wheel_path))
    dist = os.path.basename(wheel_path).split("-")[0]
    out_path = os.path.join(dest_dir, f"{dist}-{ver}-py3-none-any.whl")
    with zipfile.ZipFile(wheel_path) as src, \
            zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as dst:
        record_name = next(n for n in src.namelist() if n.endswith(".dist-info/RECORD"))
        wheel_name = record_name[:-len("RECORD")] + "WHEEL"
        records = []
        for item in src.infolist():
            if item.filename in (record_name, wheel_name):
                continue
            if item.filename.endswith((".so", ".pyd", ".dylib")):
                continue
            data = src.read(item.filename)
            dst.writestr(item.filename, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append(f"{item.filename},sha256={digest},{len(data)}")
        wheel_meta = ("Wheel-Version: 1.0\nGenerator: ser2net-repack (pure-python fallback)\n"
                      "Root-Is-Purelib: true\nTag: py3-none-any\n").encode()
        dst.writestr(wheel_name, wheel_meta)
        digest = base64.urlsafe_b64encode(hashlib.sha256(wheel_meta).digest()).rstrip(b"=").decode()
        records.append(f"{wheel_name},sha256={digest},{len(wheel_meta)}")
        records.append(f"{record_name},,")
        dst.writestr(record_name, "\n".join(records) + "\n")
    return out_path


def refresh() -> int:
    reqs = read_requirements()
    with tempfile.TemporaryDirectory() as tmp:
        for name, version, marker in reqs:
            req = f"{name}=={version}"
            for python in PLATFORM_WHEEL_PYTHONS:
                for system in SYSTEMS:
                    if marker_applies(marker, python, system):
                        cp = "cp" + python.replace(".", "")
                        if not _pip_download(req, tmp, python, PIP_PLATFORMS[system] + ["any"],
                                             [cp, "abi3", "none"]):
                            print(f"error: no wheel for {req} (Python {python}, {system})")
                            return 1
            for python in PYTHONS:
                for system in SYSTEMS:
                    if not marker_applies(marker, python, system):
                        continue
                    have = [f for f in os.listdir(tmp) if (p := parse_wheel(f))
                            and p[0] == normalize(name) and p[1] == version]
                    if any(wheel_fits(f, python, system) for f in have):
                        continue
                    if _pip_download(req, tmp, python, ["any"], ["none"], implementation="py"):
                        continue
                    base = [f for f in have if "cp311" in f] or have
                    if not base:
                        print(f"error: nothing to repack for {req} (Python {python})")
                        return 1
                    made = repack_pure(os.path.join(tmp, base[0]), tmp)
                    print(f"repacked {os.path.basename(made)} (pure-Python fallback)")
        os.makedirs(WHEELS, exist_ok=True)
        wanted = set(os.listdir(tmp))
        for f in sorted(wanted - set(os.listdir(WHEELS))):
            shutil.copy2(os.path.join(tmp, f), os.path.join(WHEELS, f))
            print(f"added   {f}")
    missing, stale = problems()
    for f in stale:
        os.remove(os.path.join(WHEELS, f))
        print(f"removed {f}")
    for m in missing:
        print(f"MISSING {m}")
    return 1 if missing else 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        missing, stale = problems()
        for m in missing:
            print(f"missing: {m}")
        for f in stale:
            print(f"stale:   {f}")
        return 1 if (missing or stale) else 0
    return refresh()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
