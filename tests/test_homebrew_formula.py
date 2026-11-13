"""The Homebrew formula's generator (packaging/homebrew/make_formula.py), offline.

  * it reads PEP 508 markers and requirements as package metadata writes them, and the
    pins of requirements.txt / requirements-optional.txt / requirements-lock.txt;
  * from this checkout's pins it makes a resource of every package ser2netweb needs on macOS
    and Linux with the formula's Python (also the optional ones, and what they need by their
    metadata), at the lock's versions, with a sha256 the lock records; cryptography (and
    cffi / pycparser, which it brings) is the Homebrew formula's, a depends_on; nothing
    Windows- or Python-3.10-only;
  * it refuses what it can't vouch for: a dependency the lock doesn't pin or pins at a
    version outside the requirement, an sdist whose sha256 the lock doesn't record, a
    pre-release tag, a tarball of another version;
  * the formula: the tag's tarball and its sha256, python@3.14 (the newest Python every CI
    test leg runs), the service on <var>/ser2netweb, a test that checks --version, --about's
    Build: line (Homebrew's from the version that knows it) and /healthz.

PyPI's answers are stand-ins here; the tap's CI builds and tests the real formula.

Run: python3 tests/test_homebrew_formula.py   (stdlib only, no network)
"""
import hashlib
import io
import json
import os
import re
import sys
import tarfile

from _workflows import ROOT, jobs, read

sys.path[:0] = [os.path.join(ROOT, "packaging", "homebrew"), ROOT]

import make_formula as mf

from ser2netweb import __version__

# what the packages of this checkout's pins require (their PyPI metadata, abridged: the
# extras and Windows-only parts are what the generator must leave out)
REQUIRES = {
    "anyio": ['exceptiongroup>=1.0.2; python_version < "3.11"', "idna>=2.8",
              'typing_extensions>=4.16.0; python_version < "3.15"', 'trio>=0.32.0; extra == "trio"'],
    "authlib": ["cryptography>=45.0.1", "joserfc>=1.6.8"],
    "joserfc": ["cryptography>=45.0.1", 'pycryptodome; extra == "drafts"'],
    "jinja2": ["MarkupSafe>=2.0", 'Babel>=2.7; extra == "i18n"'],
    "ldap3": ["pyasn1 (>=0.4.6)"],
    "paho-mqtt": ['pysocks; extra == "proxy"'],
    "psutil": ['pywin32; (os_name == "nt" and implementation_name != "pypy") and extra == "dev"'],
    "pyserial": ["hidapi; extra == 'cp2110'"],
    "pyserial-asyncio-fast": ["pyserial"],
    "starlette": ["anyio<5,>=4.0.0", 'typing-extensions>=4.10.0; python_version < "3.13"',
                  'httpx<0.29.0,>=0.27.0; extra == "full"'],
    "uvicorn": ["click>=7.0", "h11>=0.8", 'typing-extensions>=4.0; python_version < "3.11"',
                'uvloop>=0.15.1; (sys_platform != "win32" and (sys_platform != "cygwin" and '
                'platform_python_implementation != "PyPy")) and extra == "standard"'],
}
WANT = {"anyio", "authlib", "click", "h11", "idna", "jinja2", "joserfc", "ldap3", "markupsafe",
        "paho-mqtt", "psutil", "pyasn1", "pyserial", "pyserial-asyncio-fast", "python-multipart",
        "starlette", "typing-extensions", "uvicorn", "websockets"}


def _text(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


LOCK = _text("requirements-lock.txt")
HASHES = mf.lock_hashes(LOCK)
# the sdist file name PyPI has for each (some use the normalized name)
SDIST = {"paho-mqtt": "paho_mqtt", "pyserial-asyncio-fast": "pyserial_asyncio_fast",
         "python-multipart": "python_multipart", "typing-extensions": "typing_extensions"}


def fake_pypi(requires=REQUIRES, digest=None):
    """A stand-in for PyPI's JSON API: (get, the URLs asked for)."""
    asked = []

    def get(url):
        asked.append(url)
        m = re.fullmatch(r"https://pypi\.org/pypi/([^/]+)/([^/]+)/json", url)
        assert m, url
        name, version = m.groups()
        stem = SDIST.get(name, name)
        sha = digest(name) if digest else sorted(HASHES[name])[0]
        return json.dumps({
            "info": {"name": name, "version": version, "requires_dist": requires.get(name)},
            "urls": [{"packagetype": "bdist_wheel", "filename": f"{stem}-{version}-py3-none-any.whl",
                      "url": "https://files.pythonhosted.org/x.whl", "digests": {"sha256": "0" * 64}},
                     {"packagetype": "sdist", "filename": f"{stem}-{version}.tar.gz",
                      "url": f"https://files.pythonhosted.org/packages/ab/cd/{stem}-{version}.tar.gz",
                      "digests": {"sha256": sha}, "yanked": False}]}).encode()
    return get, asked


def test_markers():
    env = dict(mf.ENVIRONMENTS[0], extra="")
    holds = ['python_version >= "3.11"', 'python_version < "3.15"', 'python_full_version >= "3.14"',
             'platform_python_implementation != "PyPy"', 'implementation_name != "PyPy"',
             'sys_platform != "win32" and (sys_platform != "cygwin" and os_name == "posix")',
             '"3.11" <= python_version', "python_version == '3.14'", 'python_version == "3.*"',
             'python_version ~= "3.10"', 'sys_platform in "darwin linux"',
             'extra == "x" or python_version > "3.9"']
    fails = ['python_version < "3.11"', 'platform_system == "Windows"', 'os_name == "nt"',
             'extra == "dev"', 'extra == "trio" and python_version > "3"',
             '(os_name == "nt" and implementation_name != "pypy") and extra == "dev"',
             'python_version ~= "3.15.0"', 'sys_platform not in "darwin linux"']
    for m in holds:
        assert mf.marker_holds(m, env), m
    for m in fails:
        assert not mf.marker_holds(m, env), m
    assert mf.marker_holds('extra == "Some_Extra"', dict(env, extra="some-extra"))
    for bad in ('python_version <', 'python_version < "3" and', "(python_version > '3'",
                'os_name < "nt"', "python_version @ '3'"):
        try:
            mf.marker_holds(bad, env)
        except mf.Error:
            continue
        raise AssertionError(f"read {bad!r}")
    for marker, want in (("", True), ('platform_system == "Windows"', False),
                         ('sys_platform == "linux"', True), ('platform_machine == "arm64"', True)):
        assert mf.needed(marker) is want, marker
    print(f"markers: {len(holds) + len(fails)} as package metadata writes them, the "
          "environments of the formula (macOS and Linux, Python 3.14)  OK")


def test_requirements():
    assert mf.parse_requirement("pyasn1 (>=0.4.6)") == ("pyasn1", ">=0.4.6", "")
    assert mf.parse_requirement("anyio<5,>=4.0.0") == ("anyio", "<5,>=4.0.0", "")
    assert mf.parse_requirement('typing_extensions>=4.16.0; python_version < "3.15"') == \
        ("typing-extensions", ">=4.16.0", 'python_version < "3.15"')
    assert mf.parse_requirement("validate-pyproject[all]; extra == 'dev'") == \
        ("validate-pyproject", "", "extra == 'dev'")
    assert mf.parse_requirement("MarkupSafe>=2.0") == ("markupsafe", ">=2.0", "")
    assert mf.satisfies("4.16.0", "<5,>=4.0.0") and not mf.satisfies("5.0", "<5,>=4.0.0")
    assert mf.satisfies("0.0.33", ">=0.0.18") and mf.satisfies("2.9.1", "~=2.9")
    assert not mf.satisfies("3.0", "~=2.9") and mf.satisfies("1.0", "")
    req = {name: (version, marker) for name, version, marker in mf.pins(_text("requirements.txt"))}
    assert req["starlette"] == ("1.7.0", 'python_version < "3.11"'), "(the last line wins here)"
    assert req["pyserial"] == ("3.5", "") and req["colorama"][1] == 'platform_system == "Windows"'
    optional = dict((n, v) for n, v, _m in mf.pins(_text("requirements-optional.txt")))
    assert set(optional) == {"paho-mqtt", "ldap3", "pyasn1", "authlib", "joserfc",
                             "cryptography", "cffi", "pycparser"}, optional
    locked = {n: v for n, v, _m in mf.pins(LOCK)}
    assert locked["typing-extensions"] and "colorama" in locked
    assert all(len(HASHES[n]) >= 1 for n in locked), "every pin has its hashes"
    for bad in ("pyserial>=3.5\n", "--hash=sha256:00\n-r other.txt\nx\n"):
        try:
            mf.pins(bad)
        except mf.Error:
            continue
        raise AssertionError(bad)
    print("requirements, specifiers and the pins / hashes of the requirement files  OK")


def test_resolve():
    get, asked = fake_pypi()
    resources, formulae = mf.resolve(_text("requirements.txt"), _text("requirements-optional.txt"),
                                     LOCK, get)
    names = [r["name"] for r in resources]
    assert names == sorted(WANT), names
    assert formulae == ["cryptography"], formulae
    locked = {n: v for n, v, _m in mf.pins(LOCK)}
    for r in resources:
        name = mf.normalize(r["name"])
        assert r["version"] == locked[name], r
        assert r["sha256"] in HASHES[name], r
        assert r["url"].startswith("https://files.pythonhosted.org/packages/"), r
        assert r["url"].endswith(f"-{r['version']}.tar.gz"), r
    assert not any("cryptography" in u or "cffi" in u or "colorama" in u for u in asked), asked
    assert len(asked) == len(WANT), "each package asked for once"

    def fails(why, **kw):
        get_, _ = fake_pypi(**kw)
        try:
            mf.resolve(_text("requirements.txt"), _text("requirements-optional.txt"), LOCK, get_)
        except mf.Error as e:
            assert why in str(e), e
            return
        raise AssertionError(f"no error ({why})")

    fails("doesn't pin", requires=dict(REQUIRES, click=["colorlib>=1"]))
    fails("the lock has", requires=dict(REQUIRES, click=["idna>=9"]))
    fails("is not one requirements-lock.txt records", digest=lambda n: "f" * 64)
    print(f"resources: the {len(WANT)} packages ser2netweb needs on macOS / Linux with Python "
          "3.14, at the lock's versions and hashes; cryptography from its formula  OK")


def _tarball(version: str = __version__) -> bytes:
    """A source tarball as GitHub makes it (one top folder) with the files the generator
    reads, from this checkout."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in ("requirements.txt", "requirements-optional.txt", "requirements-lock.txt",
                    "ser2netweb/__init__.py", "ser2netweb/layout.py", "README.md"):
            data = _text(rel).encode()
            if rel == "ser2netweb/__init__.py":
                data = re.sub(rb'(?m)^__version__ = "[^"]+"', f'__version__ = "{version}"'.encode(),
                              data)
            info = tarfile.TarInfo(f"ser2netweb-{version}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_formula():
    tag = "v" + __version__.split("-")[0]
    data = _tarball(tag[1:])
    get, _ = fake_pypi()
    text = mf.make(tag, data, get)
    assert text.startswith("# Made by packaging/homebrew/make_formula.py"), text[:80]
    assert "\nclass Ser2netweb < Formula\n  include Language::Python::Virtualenv\n" in text
    assert (f'  url "https://github.com/haliskilic/ser2netweb/archive/refs/tags/{tag}.tar.gz"\n'
            f'  sha256 "{hashlib.sha256(data).hexdigest()}"\n') in text
    assert '  license "AGPL-3.0-only"\n' in text
    assert ('  depends_on "cryptography" => :no_linkage\n'
            f'  depends_on "python@{mf.PYTHON}"\n') in text
    assert text.count('\n  resource "') == len(WANT)
    for name in sorted(WANT):
        assert f'\n  resource "{name}" do\n    url "https://files.pythonhosted.org/' in text, name
    assert "  def install\n    virtualenv_install_with_resources\n  end\n" in text
    assert '    run [opt_bin/"ser2netweb", "--data-dir", var/"ser2netweb"]\n' in text
    assert "    keep_alive true\n" in text and '    log_path var/"log/ser2netweb.log"\n' in text
    assert '    environment_variables SER2NETWEB_LOG_STDOUT: "off"\n' in text
    assert f'    assert_equal "ser2netweb {tag[1:]}", shell_output("#{{bin}}/ser2netweb --version").strip\n' \
        in text
    assert '    assert_match "Build: Homebrew formula", about\n' in text, "this version knows it"
    assert "/healthz" in text and "Process.kill(\"TERM\", pid)" in text
    # 3.0.0 took the formula's virtual environment for a pip install
    old = re.sub(r'HOMEBREW: "[^"]+"', "", _text("ser2netweb/layout.py"))
    assert mf.build_line(old) == "Python package (pip / pipx)"
    assert mf.build_line(_text("ser2netweb/layout.py")) == "Homebrew formula"
    # a resource block's name is the one Homebrew's audit wants (the sdist's, - for _ and .)
    assert '\n  resource "typing-extensions" do\n' in text and "typing_extensions-" in text
    for bad_tag in ("v3.0.0-rc.1", "3.0.0", "v3.0"):
        try:
            mf.make(bad_tag, data, get)
        except mf.Error:
            continue
        raise AssertionError(bad_tag)
    try:
        mf.make("v9.9.9", data, get)
    except mf.Error as e:
        assert "is version" in str(e), e
    else:
        raise AssertionError("another version's tarball")
    print("the formula: the tag's tarball and sha256, python@3.14, the resources, "
          "cryptography's formula, the service, the test  OK")


def test_formula_python():
    """python@PYTHON is the newest Python of CI's required test matrix."""
    test = jobs(read("ci.yml"))["test"]
    versions = re.search(r"python-version: \[([^\]]*)\]", test).group(1)
    newest = max((v.strip().strip('"') for v in versions.split(",")),
                 key=lambda v: tuple(int(n) for n in v.split(".")))
    assert mf.PYTHON == newest, (mf.PYTHON, newest)
    print(f"python@{mf.PYTHON}: the newest Python CI's test matrix requires  OK")


if __name__ == "__main__":
    test_markers()
    test_requirements()
    test_resolve()
    test_formula()
    test_formula_python()
    print("\nPASS: Homebrew formula")
