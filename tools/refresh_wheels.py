"""Re-download vendor/wheels/ (the offline install set) to match requirements.txt.

    python tools/refresh_wheels.py           # update vendor/wheels/, drop stale wheels
    python tools/refresh_wheels.py --check   # only report what is missing / stale / altered
    python tools/refresh_wheels.py --verify  # (online) every wheel is the one PyPI publishes

bootstrap.py installs from vendor/wheels/ without network access, on Linux x86_64
and Windows x64, Python 3.10–3.14. For every pinned requirement (with its markers)
the set holds:
  * the cp311 + cp312 platform wheels when the package has compiled parts (or an
    abi3 wheel, which covers every version), and
  * a pure-Python ``py3-none-any`` wheel for the other Python versions — downloaded
    when the project publishes one, otherwise repacked from its cp311 wheel without
    the compiled speed-ups (markupsafe falls back to its Python implementation).
tests/test_vendor_wheels.py checks the result for every Python/OS combination.

vendor/wheels/SHA256SUMS (``sha256sum`` format) records every wheel's digest, so a
wheel replaced under the same name shows up as a changed line in a pull request's diff
(``--check`` and the test fail until it matches), and bootstrap.py refuses a listed wheel
whose content differs. ``--verify`` (the CI job "offline wheels match requirements.txt")
compares each wheel with the sha256 that PyPI's JSON API publishes for that file; a
repacked wheel is not on PyPI, so it is rebuilt from its (verified) source wheel and has to
come out byte for byte the same: the repack is deterministic (entry times and attributes
taken from the source wheel, entries stored uncompressed so the zlib build doesn't matter,
the source chosen by sorted name).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHEELS = os.path.join(ROOT, "vendor", "wheels")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")
SUMS = "SHA256SUMS"
REPACK_GENERATOR = "ser2net-repack (pure-python fallback)"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"

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


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_sums(wheel_dir: str = WHEELS) -> dict[str, str]:
    """{file name: sha256} from wheel_dir/SHA256SUMS ({} when there is none)."""
    path = os.path.join(wheel_dir, SUMS)
    out = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    digest, name = line.split(maxsplit=1)
                    out[name.strip().lstrip("*")] = digest.lower()
    return out


def write_sums(wheel_dir: str = WHEELS) -> None:
    names = sorted(f for f in os.listdir(wheel_dir) if f.endswith(".whl"))
    with open(os.path.join(wheel_dir, SUMS), "w", encoding="utf-8", newline="\n") as fh:
        for name in names:
            fh.write(f"{sha256_of(os.path.join(wheel_dir, name))}  {name}\n")


def digest_problems(wheel_dir: str = WHEELS) -> list[str]:
    """Wheels whose digest SHA256SUMS doesn't record, or records differently."""
    sums = read_sums(wheel_dir)
    files = sorted(f for f in os.listdir(wheel_dir) if f.endswith(".whl")) \
        if os.path.isdir(wheel_dir) else []
    out = [f"{f}: not in {SUMS}" for f in files if f not in sums]
    out += [f"{f}: content differs from {SUMS}" for f in files
            if f in sums and sha256_of(os.path.join(wheel_dir, f)) != sums[f]]
    out += [f"{f}: in {SUMS}, but there is no such wheel" for f in sorted(set(sums) - set(files))]
    return out


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
    """Rewrite a CPython wheel as py3-none-any without its compiled extension modules.

    Deterministic: the same source wheel gives the same bytes on any machine and Python.
    Every entry keeps the source entry's time and attributes, and is stored uncompressed
    (deflate output differs between zlib builds, e.g. zlib-ng)."""
    _name, ver, *_ = parse_wheel(os.path.basename(wheel_path))
    dist = os.path.basename(wheel_path).split("-")[0]
    out_path = os.path.join(dest_dir, f"{dist}-{ver}-py3-none-any.whl")

    def entry(name: str, like: zipfile.ZipInfo) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=like.date_time)
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3  # Unix, whatever OS repacks it
        info.external_attr = like.external_attr
        return info

    def record_line(name: str, data: bytes) -> str:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        return f"{name},sha256={digest},{len(data)}"

    with zipfile.ZipFile(wheel_path) as src, zipfile.ZipFile(out_path, "w") as dst:
        record = next(i for i in src.infolist() if i.filename.endswith(".dist-info/RECORD"))
        wheel_name = record.filename[:-len("RECORD")] + "WHEEL"
        wheel = src.getinfo(wheel_name)
        records = []
        for item in src.infolist():
            if item.filename in (record.filename, wheel_name) or item.is_dir():
                continue
            if item.filename.endswith((".so", ".pyd", ".dylib")):
                continue
            data = src.read(item.filename)
            dst.writestr(entry(item.filename, item), data)
            records.append(record_line(item.filename, data))
        wheel_meta = (f"Wheel-Version: 1.0\nGenerator: {REPACK_GENERATOR}\n"
                      "Root-Is-Purelib: true\nTag: py3-none-any\n").encode()
        dst.writestr(entry(wheel_name, wheel), wheel_meta)
        records.append(record_line(wheel_name, wheel_meta))
        records.append(f"{record.filename},,")
        dst.writestr(entry(record.filename, record), ("\n".join(records) + "\n").encode())
    return out_path


def repack_source(name: str, version: str, candidates: list[str]) -> str | None:
    """The wheel a pure-Python fallback is repacked from: the first cp311 wheel of the
    package by sorted name (else the first of all), so every machine picks the same."""
    have = sorted(f for f in candidates if (p := parse_wheel(f))
                  and p[0] == normalize(name) and p[1] == version and "py3-none-any" not in f)
    return next((f for f in have if "cp311" in f), have[0] if have else None)


def is_repack(path: str) -> bool:
    with zipfile.ZipFile(path) as z:
        wheel = next(n for n in z.namelist() if n.endswith(".dist-info/WHEEL"))
        return f"Generator: {REPACK_GENERATOR}" in z.read(wheel).decode("utf-8", "replace")


def pypi_digests(name: str, version: str) -> dict[str, str]:
    """{file name: sha256} of every file PyPI publishes for name==version."""
    url = PYPI_JSON.format(name=name, version=version)
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (a fixed https:// URL)
        doc = json.load(r)
    return {u["filename"]: u["digests"]["sha256"] for u in doc["urls"]}


def verify_with_pypi(wheel_dir: str = WHEELS, report=print) -> list[str]:
    """Problems found comparing every vendored wheel with PyPI (network)."""
    out = []
    files = sorted(f for f in os.listdir(wheel_dir) if f.endswith(".whl"))
    cache: dict[tuple[str, str], dict[str, str]] = {}
    for f in files:
        parsed = parse_wheel(f)
        if parsed is None:
            out.append(f"{f}: not a wheel file name")
            continue
        key = (parsed[0], parsed[1])
        if key not in cache:
            cache[key] = pypi_digests(*key)
        published = cache[key]
        path = os.path.join(wheel_dir, f)
        if f in published:
            if sha256_of(path) != published[f]:
                out.append(f"{f}: differs from the file PyPI publishes")
            else:
                report(f"ok      {f} (PyPI sha256 {published[f][:16]}…)")
            continue
        if not is_repack(path):
            out.append(f"{f}: PyPI has no such file, and it is not a repacked wheel")
            continue
        base = repack_source(parsed[0], parsed[1], files)
        if base is None or published.get(base) != sha256_of(os.path.join(wheel_dir, base)):
            out.append(f"{f}: its source wheel {base} is missing or not PyPI's")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            rebuilt = repack_pure(os.path.join(wheel_dir, base), tmp)
            if sha256_of(rebuilt) != sha256_of(path):
                out.append(f"{f}: is not the repack of {base} (rebuilt differently)")
            else:
                report(f"ok      {f} (rebuilt byte for byte from {base})")
    return out


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
                    base = repack_source(name, version, have)
                    if base is None:
                        print(f"error: nothing to repack for {req} (Python {python})")
                        return 1
                    made = repack_pure(os.path.join(tmp, base), tmp)
                    print(f"repacked {os.path.basename(made)} from {base} (pure-Python fallback)")
        os.makedirs(WHEELS, exist_ok=True)
        for f in sorted(os.listdir(tmp)):
            dest = os.path.join(WHEELS, f)
            if not os.path.exists(dest):
                print(f"added   {f}")
            elif sha256_of(dest) != sha256_of(os.path.join(tmp, f)):
                print(f"updated {f}")
            else:
                continue
            shutil.copy2(os.path.join(tmp, f), dest)
    missing, stale = problems()
    for f in stale:
        os.remove(os.path.join(WHEELS, f))
        print(f"removed {f}")
    write_sums()
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
        altered = digest_problems()
        for p in altered:
            print(f"digest:  {p}")
        return 1 if (missing or stale or altered) else 0
    if "--verify" in argv:
        found = digest_problems() + verify_with_pypi()
        for p in found:
            print(f"error: {p}")
        return 1 if found else 0
    return refresh()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
