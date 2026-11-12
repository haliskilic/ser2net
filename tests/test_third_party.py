"""The executables' SBOM and --licenses text name what they carry, with SPDX licenses.

tools/third_party.py (run by the standalone builds) must
* give every component an SPDX expression where one can be known: the metadata's
  License-Expression, a License field that is one, or LICENSE_OVERRIDES — an override only
  for the version it was checked at (a newer one falls back to the metadata, with a warning,
  and this test fails in CI's vendor job until the entry is checked again);
* list the native libraries in the interpreter with their versions and CPEs (OpenSSL, zlib;
  expat / mpdecimal / SQLite when the executable carries their modules), the OpenSSL that
  cryptography links in, and the browser libraries of app/web/static;
* put the browser libraries' license texts into the --licenses file.
ser2net.spec writes that file itself when a plain `pyinstaller ser2net.spec` runs without it,
and an executable without it points at this version's THIRD-PARTY-NOTICES.md online.

Run: python3 tests/test_third_party.py
"""
import contextlib
import email.message
import io
import json
import os
import ssl
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import refresh_locks
import third_party as tp


class FakeDist:
    def __init__(self, name, version, license_expression=None, license=None, classifiers=()):
        self.version = version
        self.metadata = email.message.Message()
        self.metadata["Name"] = name
        if license_expression:
            self.metadata["License-Expression"] = license_expression
        if license:
            self.metadata["License"] = license
        for c in classifiers:
            self.metadata["Classifier"] = c


def test_spdx_detection():
    assert tp.is_spdx("MIT") and tp.is_spdx("Apache-2.0 OR BSD-3-Clause")
    assert tp.is_spdx("GPL-2.0-or-later WITH Bootloader-exception AND Apache-2.0")
    assert tp.is_spdx("(MIT OR 0BSD) AND Zlib")
    for not_spdx in ("BSD", "LGPL v3", "MIT License", "", "OSI Approved", "GPLv2-or-later with a"):
        assert not tp.is_spdx(not_spdx), not_spdx
    print("SPDX expressions recognised; 'BSD', 'LGPL v3', 'MIT License' are not  OK")


def test_license_order():
    warned = []
    lic = lambda d: tp.license_of(d, warn=warned.append)
    assert lic(FakeDist("a", "1", license_expression="MIT", license="BSD")) == ("MIT", True)
    name, (version, expr) = next(iter(tp.LICENSE_OVERRIDES.items()))
    assert lic(FakeDist(name, version, license="BSD", classifiers=["License :: OSI Approved :: BSD License"])) \
        == (expr, True)
    assert not warned
    # a version the override wasn't checked at: the metadata's own words, and a warning
    got = lic(FakeDist(name, version + ".1", license="BSD"))
    assert got == ("BSD", False) and len(warned) == 1 and "LICENSE_OVERRIDES" in warned[0], (got, warned)
    assert lic(FakeDist("b", "1", license="BSD-2-Clause",
                        classifiers=["License :: OSI Approved :: BSD License"])) == ("BSD-2-Clause", True)
    # the License field before bare classifiers (classifiers lose "or later" / exceptions)
    assert lic(FakeDist("c", "1", license="GPLv2-or-later with a special exception",
                        classifiers=["License :: OSI Approved :: GNU General Public License v2 (GPLv2)"])) \
        == ("GPLv2-or-later with a special exception", False)
    assert lic(FakeDist("d", "1", classifiers=["License :: OSI Approved :: MIT License"])) == ("MIT License", False)
    assert lic(FakeDist("e", "1", license="line one\nline two")) == ("line one", False)
    assert lic(FakeDist("f", "1")) == ("see license text", False)
    print("license: License-Expression, checked override, SPDX License field, its text, classifiers  OK")


def test_overrides_match_the_pins():
    locked = {}
    for fn in (refresh_locks.LOCK, refresh_locks.BUILD):
        locked.update({n: v for n, (v, _m, _h) in refresh_locks.read_lock(os.path.join(ROOT, fn)).items()})
    stale = [f"{name}: checked at {checked}, pinned at {locked.get(name)}"
             for name, (checked, _expr) in tp.LICENSE_OVERRIDES.items() if locked.get(name) != checked]
    assert not stale, ("check these licenses at the pinned version and update LICENSE_OVERRIDES "
                       "in tools/third_party.py:\n  " + "\n  ".join(stale))
    print(f"all {len(tp.LICENSE_OVERRIDES)} license overrides are for the pinned versions  OK")


def test_sbom_components():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "s.cdx.json")
        with contextlib.redirect_stderr(io.StringIO()):
            tp.write_sbom(out, binaries=["lib-dynload/pyexpat.cpython-312-x86_64-linux-gnu.so",
                                         "_sqlite3.pyd", "libffi-8.dll"])
        with open(out, encoding="utf-8") as fh:
            bom = json.load(fh)
        with contextlib.redirect_stderr(io.StringIO()):
            tp.write_sbom(out)  # no binaries list: only what every build has
        with open(out, encoding="utf-8") as fh:
            plain = {c["name"] for c in json.load(fh)["components"]}
    comps = bom["components"]
    by_name = {}
    for c in comps:
        by_name.setdefault(c["name"], []).append(c)
    assert len({c["bom-ref"] for c in comps}) == len(comps), "bom-refs must be unique"
    # the interpreter's OpenSSL first (tools/release_guard.py sboms reads the first one)
    first = next(c for c in comps if c["name"] == "openssl")
    assert first["version"] == ssl.OPENSSL_VERSION.split()[1] and first["cpe"].startswith(
        "cpe:2.3:a:openssl:openssl:"), first
    for name, cpe in (("cpython", "cpe:2.3:a:python:python:"), ("zlib", "cpe:2.3:a:zlib:zlib:"),
                      ("expat", "cpe:2.3:a:libexpat_project:libexpat:"),
                      ("sqlite", "cpe:2.3:a:sqlite:sqlite:")):
        assert name in by_name and by_name[name][0]["cpe"].startswith(cpe), (name, by_name.get(name))
    assert "expat" not in plain and "sqlite" not in plain and {"openssl", "zlib"} <= plain
    for c in comps:
        lic = c["licenses"][0]
        assert "expression" in lic or "name" in lic.get("license", {}), c
    npm = {c["name"]: c for c in comps if c["purl"].startswith("pkg:npm/")}
    assert set(npm) == {"htmx.org", "@xterm/xterm", "@xterm/addon-fit"}, sorted(npm)
    assert npm["@xterm/xterm"]["purl"].startswith("pkg:npm/%40xterm/xterm@")
    assert npm["htmx.org"]["licenses"] == [{"expression": "0BSD"}]
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("SKIP: cryptography's own OpenSSL (cryptography not installed)")
    else:
        assert any("cryptography" in c["description"] for c in by_name["openssl"]), by_name["openssl"]
    print(f"SBOM: {len(comps)} components, native libraries with CPEs, npm packages  OK")


def test_licenses_include_browser_libraries():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "l.txt")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            tp.write_licenses(out, [])
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
    for pkg in tp.npm_packages():
        assert f"{pkg['name']} {pkg['version']} — {pkg['license']} (browser library:" in text, pkg["name"]
    assert "The xterm.js authors" in text and "Zero-Clause BSD" in text
    print("--licenses: htmx and xterm.js license texts included  OK")


def test_spec_writes_what_is_missing():
    calls = []

    def fake_run(cmd, check):
        calls.append(cmd)
        os.makedirs("build", exist_ok=True)
        with open(cmd[-1], "w", encoding="utf-8") as fh:
            fh.write("licenses")

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda package: ([], [], [])
    fakes = {"PyInstaller": types.ModuleType("PyInstaller"),
             "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
             "PyInstaller.utils.hooks": hooks}
    seen = {}

    def analysis(scripts, **kw):
        seen.update(kw)
        return types.SimpleNamespace(scripts=scripts, pure=[], datas=list(kw["datas"]),
                                     binaries=[("lib-dynload/pyexpat.so", "/x/pyexpat.so", "EXTENSION"),
                                               ("libgcc_s.so.1", "/lib/libgcc_s.so.1", "BINARY")])

    import subprocess
    saved = {name: sys.modules.get(name) for name in fakes}
    real_run, cwd = subprocess.run, os.getcwd()
    with open(os.path.join(ROOT, "ser2net.spec"), encoding="utf-8") as fh:
        spec = compile(fh.read(), "ser2net.spec", "exec")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            sys.modules.update(fakes)
            subprocess.run = fake_run
            os.chdir(tmp)
            exec(spec, {"Analysis": analysis, "PYZ": lambda *a, **k: None,
                        "EXE": lambda *a, **k: None, "SPECPATH": tmp})
            with open(os.path.join("build", "bundled-binaries.txt"), encoding="utf-8") as fh:
                listed = fh.read().split()
        finally:
            os.chdir(cwd)
            subprocess.run = real_run
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
    assert len(calls) == 1 and calls[0][1:] == [os.path.join("tools", "third_party.py"), "licenses",
                                                os.path.join("build", "THIRD-PARTY-LICENSES.txt")], calls
    assert (os.path.join("build", "THIRD-PARTY-LICENSES.txt"), ".") in seen["datas"], seen["datas"]
    assert listed == ["lib-dynload/pyexpat.so"], listed  # libgcc_s is left to the system
    print("ser2net.spec writes a missing license file and the list of bundled binaries  OK")


def test_frozen_fallback_names_the_notices():
    import ser2net

    buf = io.BytesIO()
    fake_stdout = types.SimpleNamespace(buffer=buf, flush=lambda: None)
    saved = ser2net.FROZEN, sys.stdout
    try:
        ser2net.FROZEN, sys.stdout = True, fake_stdout
        assert ser2net.print_licenses() == 0
    finally:
        ser2net.FROZEN, sys.stdout = saved
    text = buf.getvalue().decode("utf-8")
    from app import __version__
    want = f"https://github.com/haliskilic/ser2net/blob/v{__version__}/THIRD-PARTY-NOTICES.md"
    assert want in text, text
    print("an executable without the license file points at THIRD-PARTY-NOTICES.md online  OK")


if __name__ == "__main__":
    test_spdx_detection()
    test_license_order()
    if os.environ.get("SER2NET_SKIP_PIN_CHECKS") == "1":
        print("SKIP: license overrides against the pins (CI: job 'offline wheels match requirements.txt')")
    else:
        test_overrides_match_the_pins()
    test_sbom_components()
    test_licenses_include_browser_libraries()
    test_spec_writes_what_is_missing()
    test_frozen_fallback_names_the_notices()
    print("\nPASS: SBOM and license texts")
