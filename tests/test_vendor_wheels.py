"""vendor/wheels/ must match requirements.txt (the offline install set).

For every pinned requirement and every Python 3.10–3.14 × Linux/Windows combination
its markers select, a compatible wheel of exactly that version must be bundled; and
no wheel may be left over from an old pin. Every wheel's sha256 is in
vendor/wheels/SHA256SUMS, the repacked pure-Python fallback rebuilds byte for byte from
its source wheel, and bootstrap.py refuses a listed wheel whose content differs.
Fix with: python tools/refresh_wheels.py   (CI also compares every wheel with PyPI:
python tools/refresh_wheels.py --verify)

Run: python3 tests/test_vendor_wheels.py   (stdlib only)

With SER2NET_SKIP_PIN_CHECKS=1 (CI's test matrix and coverage job) only the checks that
don't depend on the repository's current wheels run: a pin bump then fails one CI job,
"offline wheels match requirements.txt", instead of every leg.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import refresh_wheels as rw


def test_markers():
    assert rw.marker_applies('python_version < "3.11"', "3.10", "Linux")
    assert not rw.marker_applies('python_version < "3.11"', "3.11", "Linux")
    assert rw.marker_applies('python_version >= "3.11"', "3.14", "Windows")
    assert rw.marker_applies('platform_system == "Windows"', "3.12", "Windows")
    assert not rw.marker_applies('platform_system == "Windows"', "3.12", "Linux")
    try:
        rw.marker_applies('sys_platform == "linux"', "3.12", "Linux")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown marker must not be silently ignored")


def test_wheel_tags():
    fits = rw.wheel_fits
    assert fits("starlette-1.10.0-py3-none-any.whl", "3.13", "Windows")
    assert fits("pyserial-3.5-py2.py3-none-any.whl", "3.10", "Linux")
    assert fits("psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl",
                "3.14", "Linux")
    assert not fits("psutil-7.2.2-cp37-abi3-win_amd64.whl", "3.12", "Linux")
    assert fits("markupsafe-3.0.4-cp311-cp311-win_amd64.whl", "3.11", "Windows")
    assert not fits("markupsafe-3.0.4-cp311-cp311-win_amd64.whl", "3.12", "Windows")
    assert not fits("websockets-17.4-cp311-cp311-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl",
                    "3.11", "Windows")


def test_vendor_matches_requirements():
    missing, stale = rw.problems()
    msg = []
    if missing:
        msg.append("missing wheels:\n  " + "\n  ".join(missing))
    if stale:
        msg.append("stale wheels (no requirement uses them):\n  " + "\n  ".join(stale))
    assert not msg, "\n".join(msg) + "\nrun: python tools/refresh_wheels.py"


def test_digests_recorded():
    problems = rw.digest_problems()
    assert not problems, "\n".join(problems) + "\nrun: python tools/refresh_wheels.py"
    with tempfile.TemporaryDirectory() as tmp:
        small = sorted(f for f in os.listdir(rw.WHEELS) if f.endswith(".whl"))[:3]
        for f in small:
            shutil.copy2(os.path.join(rw.WHEELS, f), tmp)
        rw.write_sums(tmp)
        assert rw.digest_problems(tmp) == []
        with open(os.path.join(tmp, small[0]), "ab") as fh:
            fh.write(b"x")  # same name, other content
        shutil.copy2(os.path.join(rw.WHEELS, small[0]), os.path.join(tmp, "extra-1.0-py3-none-any.whl"))
        os.remove(os.path.join(tmp, small[1]))
        found = rw.digest_problems(tmp)
        assert f"{small[0]}: content differs from SHA256SUMS" in found, found
        assert "extra-1.0-py3-none-any.whl: not in SHA256SUMS" in found, found
        assert f"{small[1]}: in SHA256SUMS, but there is no such wheel" in found, found
    print(f"SHA256SUMS records all {len(rw.read_sums())} wheels; a changed one is reported  OK")


def test_repack_reproducible():
    files = os.listdir(rw.WHEELS)
    repacks = [f for f in files if f.endswith("-py3-none-any.whl")
               and rw.is_repack(os.path.join(rw.WHEELS, f))]
    assert repacks, "markupsafe's pure-Python fallback is a repack"
    for f in repacks:
        name, version, *_ = rw.parse_wheel(f)
        base = rw.repack_source(name, version, files)
        assert base and "cp311" in base and "manylinux" in base, base  # sorted: Linux first
        assert rw.repack_source(name, version, sorted(files, reverse=True)) == base
        with tempfile.TemporaryDirectory() as tmp:
            rebuilt = rw.repack_pure(os.path.join(rw.WHEELS, base), tmp)
            assert rw.sha256_of(rebuilt) == rw.sha256_of(os.path.join(rw.WHEELS, f)), \
                f"{f} is not the deterministic repack of {base}: run python tools/refresh_wheels.py"
    print(f"repacked wheels rebuild byte for byte from their source: {', '.join(repacks)}  OK")


def test_pypi_comparison():
    # verify_with_pypi() without the network: PyPI's digests stubbed from the files
    sums = rw.read_sums()
    published = {}
    for f, digest in sums.items():
        name, version, *_ = rw.parse_wheel(f)
        if not rw.is_repack(os.path.join(rw.WHEELS, f)):
            published.setdefault((name, version), {})[f] = digest
    real = rw.pypi_digests
    try:
        rw.pypi_digests = lambda name, version: published[(name, version)]
        quiet = lambda *_: None
        assert rw.verify_with_pypi(report=quiet) == []
        _key, files = next(iter(published.items()))
        victim = next(iter(files))
        files[victim] = "0" * 64
        found = rw.verify_with_pypi(report=quiet)
        assert found == [f"{victim}: differs from the file PyPI publishes"], found
        del files[victim]
        found = rw.verify_with_pypi(report=quiet)
        assert found == [f"{victim}: PyPI has no such file, and it is not a repacked wheel"], found
    finally:
        rw.pypi_digests = real
    print("every wheel is PyPI's file or the reproducible repack of one (stubbed PyPI)  OK")


def test_bootstrap_checks_digests():
    spec = importlib.util.spec_from_file_location("bootstrap", os.path.join(HERE, "..", "bootstrap.py"))
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    assert bootstrap.altered_wheels() == [], "the bundled set must pass its own check"
    with tempfile.TemporaryDirectory() as tmp:
        small = sorted(f for f in os.listdir(rw.WHEELS) if f.endswith(".whl"))[:2]
        for f in small:
            shutil.copy2(os.path.join(rw.WHEELS, f), tmp)
        rw.write_sums(tmp)
        # a wheel added by hand (e.g. an optional package) is not listed: allowed
        shutil.copy2(os.path.join(rw.WHEELS, small[0]), os.path.join(tmp, "added-1.0-py3-none-any.whl"))
        assert bootstrap.altered_wheels(tmp) == []
        with open(os.path.join(tmp, small[1]), "r+b") as fh:
            fh.seek(10)
            fh.write(b"\0")
        assert bootstrap.altered_wheels(tmp) == [small[1]]
        os.remove(os.path.join(tmp, small[1]))  # missing: pip reports it if it is needed
        assert bootstrap.altered_wheels(tmp) == []
    print("bootstrap.py refuses a listed wheel whose digest differs, keeps unlisted ones  OK")


if __name__ == "__main__":
    test_markers()
    test_wheel_tags()
    if os.environ.get("SER2NET_SKIP_PIN_CHECKS") == "1":
        print("SKIP: vendor/wheels against the pins (CI: job 'offline wheels match requirements.txt')")
        raise SystemExit(0)
    test_vendor_matches_requirements()
    test_digests_recorded()
    test_repack_reproducible()
    test_pypi_comparison()
    test_bootstrap_checks_digests()
    print("PASS: vendor/wheels matches requirements.txt for Python 3.10-3.14 on Linux + Windows")
