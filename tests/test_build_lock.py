"""The Docker image and the standalone builds install hash-locked sets that match the pins.

requirements-lock.txt (the runtime + optional pins for Python 3.12) and
requirements-build.txt (PyInstaller and its dependencies) list the sha256 of every file
of each release; the Dockerfile and both build scripts install them with
pip --require-hashes --only-binary=:all:, so pip refuses any other file and any unlocked
dependency. tools/refresh_locks.py writes them; this test checks they pin exactly what
requirements.txt / requirements-optional.txt pin, and the resolver's rules offline (CI's
vendor job also compares every hash with PyPI: python tools/refresh_locks.py --verify).

Run: python3 tests/test_build_lock.py   (stdlib only)

With SER2NETWEB_SKIP_PIN_CHECKS=1 (CI's test matrix and coverage job) the comparison with the
repository's current pins is left to the job "offline wheels match requirements.txt".
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import refresh_locks as rl

from ser2netweb import envvars


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_locks_match_the_pins():
    problems = rl.check()
    assert not problems, "\n".join(problems) + "\nrun: python tools/refresh_locks.py"
    lock = rl.read_lock(os.path.join(ROOT, rl.LOCK))
    build = rl.read_lock(os.path.join(ROOT, rl.BUILD))
    assert lock["colorama"][1] == 'platform_system == "Windows"', lock["colorama"]
    assert "pyinstaller" in build and "pyinstaller-hooks-contrib" in build, sorted(build)
    assert build["pefile"][1] == 'platform_system == "Windows"', build["pefile"]
    for entries in (lock, build):
        for name, (_v, _m, hashes) in entries.items():
            assert len(hashes) == len(set(hashes)), f"{name}: a hash listed twice"
    print(f"{rl.LOCK} ({len(lock)}) and {rl.BUILD} ({len(build)}) pin what the requirement "
          "files pin, every line hashed  OK")


def test_a_changed_pin_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        for fn in ("requirements.txt", "requirements-optional.txt", rl.LOCK, rl.BUILD):
            shutil.copy2(os.path.join(ROOT, fn), tmp)
        path = os.path.join(tmp, "requirements-optional.txt")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        pin = rl.rw.read_requirements(path)[0]  # e.g. paho-mqtt==2.2.0
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace(f"{pin[0]}=={pin[1]}", f"{pin[0]}=={pin[1]}.1", 1)
                     + "newpkg==1.0\n")
        found = rl.check(tmp)
    assert any(f"{rl.canonical(pin[0])} is locked as {pin[1]} " in p for p in found), found
    assert any("newpkg==1.0 is pinned but not locked" in p for p in found), found
    print("a pin changed without its lock is reported  OK")


def test_lock_syntax():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lock.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# header\n"
                     "a-b==1.0 \\\n    --hash=sha256:" + "1" * 64 + " \\\n    --hash=sha256:" + "2" * 64 + "\n"
                     'C_D==2.0 ; platform_system == "Windows" \\\n    --hash=sha256:' + "3" * 64 + "\n"
                     "e==3.0\n")
        got = rl.read_lock(path)
        assert got == {"a-b": ("1.0", "", ["1" * 64, "2" * 64]),
                       "c-d": ("2.0", 'platform_system == "Windows"', ["3" * 64]),
                       "e": ("3.0", "", [])}, got
        entries = [(n, v, m, h) for n, (v, m, h) in got.items()]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(rl._lock_text("# again", entries))
        assert rl.read_lock(path) == got, "the writer and the reader agree"
    print("lock files: continuation lines, markers and hashes round-trip  OK")


def test_wheel_rules():
    ok = rl.wheel_installs
    assert ok("pyserial-3.5-py2.py3-none-any.whl", "Windows x64")
    assert ok("psutil-7.2.2-cp36-abi3-manylinux2010_x86_64.manylinux_2_12_x86_64.manylinux_2_28_x86_64.whl",
              "Linux x86_64")
    assert ok("cryptography-50.0.2-cp311-abi3-manylinux_2_28_aarch64.whl", "Linux aarch64")
    assert not ok("cryptography-50.0.2-cp311-abi3-manylinux_2_34_aarch64.whl", "Linux aarch64"), \
        "the Linux build container has glibc 2.28"
    assert not ok("websockets-17.4-cp313-cp313-win_amd64.whl", "Windows x64")
    assert not ok("pyinstaller-6.22.3-py3-none-win32.whl", "Windows x64")
    assert not ok("pyinstaller-6.22.3-py3-none-musllinux_1_1_x86_64.whl", "Linux x86_64")
    assert not ok("pyinstaller-6.22.3.tar.gz", "Linux x86_64"), "wheels only"
    print("which wheels install on Python 3.12: Linux (glibc <= 2.28) x86_64/aarch64, Windows x64  OK")


def test_resolver_markers():
    # a stubbed PyPI: app needs lib on every OS, winlib on Windows only, nothing on Linux
    meta = {("app", "1.0"): ["lib>=2", 'winlib ; sys_platform == "win32"',
                             'docs ; extra == "doc"'],
            ("lib", "2.1"): [], ("winlib", "0.3"): [], ("winlib", "0.2"): []}
    releases = {"winlib": {"0.3": [{"filename": "winlib-0.3-py3-none-any.whl"}],
                           "0.4b1": [{"filename": "winlib-0.4b1-py3-none-any.whl"}]}}
    real = rl.pypi
    try:
        rl.pypi = lambda name, version=None: (
            {"info": {"requires_dist": meta[(name, version)]}} if version
            else {"releases": releases[name]})
        assert rl.closure({"app": "1.0"}, {"lib": "2.1"}, "Windows x64", choose=True,
                          prefer={"winlib": "0.2"}) == {"app": "1.0", "lib": "2.1", "winlib": "0.2"}
        win = rl.closure({"app": "1.0"}, {"lib": "2.1"}, "Windows x64", choose=True)
        lin = rl.closure({"app": "1.0"}, {"lib": "2.1"}, "Linux x86_64", choose=True)
        assert win == {"app": "1.0", "lib": "2.1", "winlib": "0.3"}, win  # no pre-release
        assert lin == {"app": "1.0", "lib": "2.1"}, lin
        try:
            rl.closure({"app": "1.0"}, {}, "Linux x86_64", choose=False)
        except SystemExit as e:
            assert "needs lib>=2 on Linux x86_64, which no lock pins" in str(e), e
        else:
            raise AssertionError("an unlocked dependency must be an error")
        try:
            rl.closure({"app": "1.0"}, {"lib": "1.9"}, "Linux x86_64", choose=False)
        except SystemExit as e:
            assert "but lib 1.9 is pinned" in str(e), e
        else:
            raise AssertionError("a pin outside the specifier must be an error")
    finally:
        rl.pypi = real
    assert rl._marker(set(rl.ENVS)) == ""
    assert rl._marker({"Windows x64"}) == 'platform_system == "Windows"'
    assert rl._marker({"Linux x86_64", "Linux aarch64"}) == 'platform_system == "Linux"'
    print("dependencies resolved per target, platform markers derived from where they're needed  OK")


def test_builds_install_the_locks():
    flags = "--require-hashes --only-binary=:all:"
    docker = read("Dockerfile")
    assert f"pip install --no-cache-dir {flags} -r requirements-lock.txt" in docker
    assert "!requirements-lock.txt" in read(".dockerignore").splitlines()
    linux = read("tools/build_standalone_linux.sh")
    assert f"{flags} -r requirements-lock.txt -r requirements-build.txt" in linux, linux
    windows = read("tools/build_standalone_windows.ps1")
    assert f"{flags} -r requirements-lock.txt -r requirements-build.txt" in windows, windows
    for text in (docker, linux, windows):
        assert "pyinstaller==" not in text and "-r requirements.txt" not in text
    print("Dockerfile and both standalone builds install the hash-locked sets  OK")


if __name__ == "__main__":
    if envvars.get("SER2NETWEB_SKIP_PIN_CHECKS") == "1":
        print("SKIP: the locks against the pins (CI: job 'offline wheels match requirements.txt')")
    else:
        test_locks_match_the_pins()
        test_a_changed_pin_is_reported()
    test_lock_syntax()
    test_wheel_rules()
    test_resolver_markers()
    test_builds_install_the_locks()
    print("\nPASS: build lock")
