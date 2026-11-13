"""pyproject.toml (the PyPI package ser2netweb) agrees with the pins and the package.

  * its dependencies are ranges the pins of requirements.txt fall into on every supported
    Python (3.10 to 3.14) and OS, each package ser2netweb imports has one, and every range
    allows no older release than the pin (the one the tests ran with); the extras bring
    requirements-optional.txt's packages (mqtt, ldap, oidc, tls, all), as the hints of
    ser2netweb/optional.py name them;
  * the version comes from ser2netweb/__init__.py (read without importing it), the command
    is ser2netweb = ser2netweb.cli:main, Python 3.10+, AGPL-3.0-only;
  * the package data covers every file of ser2netweb/ that isn't Python (templates, static
    files, translations), the licence files are those the /licenses page shows, MANIFEST.in
    keeps the tests and README.md out of the sdist;
  * the build backend (setuptools) and the build frontend (build) are in the hash-locked
    requirements-build.txt, at a version the backend line allows.
What a build shows (the files in the sdist and the wheel, an install) is CI's job "pypi
package" (tests/pypi_smoke.py).

Run: python3 tests/test_pyproject.py   (stdlib only, binds nothing)

With SER2NETWEB_SKIP_PIN_CHECKS=1 (CI's test matrix and coverage job) the comparison with the
pins is left to the job "offline wheels match requirements.txt", so a pin bump outside a
range fails there alone.
"""
import fnmatch
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import refresh_locks as rl
import refresh_wheels as rw

from ser2netweb import envvars, optional

try:
    import tomllib
except ImportError:  # Python 3.10: pip's copy
    from pip._vendor import tomli as tomllib

# the packages ser2netweb imports itself (the rest of requirements.txt comes with them)
DIRECT = {"pyserial", "pyserial-asyncio-fast", "starlette", "uvicorn", "h11", "websockets",
          "python-multipart", "jinja2", "markupsafe", "psutil", "exceptiongroup", "taskgroup"}
TRANSITIVE = {"anyio", "click", "idna", "typing-extensions", "colorama"}


def project():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)


def _env(python, system):
    return {**rl.env_of("Linux x86_64" if system == "Linux" else "Windows x64"),
            "python_version": python, "python_full_version": python + ".0"}


def test_dependencies():
    _M, Requirement, _S, Version = rl._packaging()
    deps = [Requirement(d) for d in project()["project"]["dependencies"]]
    pins = rw.read_requirements(os.path.join(ROOT, "requirements.txt"))
    names = {rl.canonical(r.name) for r in deps}
    assert names == DIRECT, sorted(names ^ DIRECT)
    assert {rl.canonical(n) for n, _v, _m in pins} == DIRECT | TRANSITIVE, "a new pin: which?"
    checked = 0
    for python in rw.PYTHONS:
        for system in rw.SYSTEMS:
            env = _env(python, system)
            mine = [r for r in deps if r.marker is None or r.marker.evaluate(env)]
            for name, version, marker in pins:
                if not rw.marker_applies(marker, python, system) or \
                        rl.canonical(name) not in DIRECT:
                    continue
                ranges = [r for r in mine if rl.canonical(r.name) == rl.canonical(name)]
                assert len(ranges) == 1, (name, python, system, ranges)
                spec = ranges[0].specifier
                assert spec.contains(Version(version)), \
                    f"{name}=={version} (Python {python}) is outside pyproject.toml's {spec}"
                floor = [s.version for s in spec if s.operator == ">="]
                assert floor == [version], f"{name}: the range starts at {floor}, the pin is {version}"
                checked += 1
    print(f"pyproject.toml's dependencies hold requirements.txt's pins ({checked} checks over "
          f"Python {rw.PYTHONS[0]}-{rw.PYTHONS[-1]}, Linux and Windows), each from the pin on  OK")


def test_extras():
    _M, Requirement, _S, Version = rl._packaging()
    extras = project()["project"]["optional-dependencies"]
    assert set(extras) == {"mqtt", "ldap", "oidc", "tls", "all"}, sorted(extras)
    assert extras["all"] == ["ser2netweb[mqtt,ldap,oidc,tls]"], extras["all"]
    pins = {rl.canonical(n): v for n, v, _m in
            rw.read_requirements(os.path.join(ROOT, "requirements-optional.txt"))}
    for dist, extra in optional.EXTRAS.items():
        reqs = [Requirement(r) for r in extras[extra]]
        assert [rl.canonical(r.name) for r in reqs] == [dist], (extra, reqs)
        assert reqs[0].specifier.contains(Version(pins[dist])), (dist, pins[dist], reqs[0])
    # paho-mqtt from 1.6 (as requirements.txt says for a source install, CI's paho1 job)
    assert Requirement(extras["mqtt"][0]).specifier.contains(Version("1.6.1"))
    print("extras mqtt / ldap / oidc / tls / all hold requirements-optional.txt's pins  OK")


def test_metadata():
    data = project()
    proj = data["project"]
    assert proj["name"] == "ser2netweb" and proj["dynamic"] == ["version"]
    assert proj["requires-python"] == ">=3.10" and proj["license"] == "AGPL-3.0-only"
    assert proj["scripts"] == {"ser2netweb": "ser2netweb.cli:main"}
    attr = data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    module, _, name = attr.rpartition(".")
    path = os.path.join(ROOT, *module.split(".")) + ".py"
    assert path == os.path.join(ROOT, "ser2netweb", "__init__.py") and name == "__version__", attr
    with open(path, encoding="utf-8") as fh:
        assert re.search(r'(?m)^__version__ = "[^"]+"$', fh.read()), "a plain assignment"
    assert os.path.isfile(os.path.join(ROOT, proj["readme"])), proj["readme"]
    for py in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {py}" in proj["classifiers"], py
    print("name ser2netweb, version from ser2netweb/__init__.py, the command ser2netweb, "
          "Python 3.10+, AGPL-3.0-only  OK")


def test_files():
    data = project()
    setup = data["tool"]["setuptools"]
    assert setup["packages"]["find"]["include"] == ["ser2netweb", "ser2netweb.*"]
    assert setup["include-package-data"] is False
    globs = setup["package-data"]["ser2netweb"]
    pkg = os.path.join(ROOT, "ser2netweb")
    count = 0
    for folder, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        has_init = "__init__.py" in files
        for name in files:
            if name.endswith((".py", ".pyc")):
                assert has_init, f"{folder} has Python files but no __init__.py"
                continue
            rel = os.path.relpath(os.path.join(folder, name), pkg).replace(os.sep, "/")
            assert any(fnmatch.fnmatchcase(rel, g) and rel.count("/") == g.count("/")
                       for g in globs), f"ser2netweb/{rel} is not in the package data {globs}"
            count += 1
    licences = data["project"]["license-files"]
    assert licences[:4] == ["LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md",
                            "THIRD-PARTY-NOTICES.md"], licences
    assert "tools/licenses/npm/*.txt" in licences  # (ser2netweb/licences.py reads them there)
    for pattern in licences:
        assert glob.glob(os.path.join(ROOT, *pattern.split("/"))), pattern
    with open(os.path.join(ROOT, "MANIFEST.in"), encoding="utf-8") as fh:
        manifest = [ln.split() for ln in fh if ln.strip() and not ln.startswith("#")]
    assert ["prune", "tests"] in manifest and ["exclude", "README.md"] in manifest, manifest
    print(f"package data: every one of ser2netweb/'s {count} other files; licence files; "
          "MANIFEST.in keeps the tests out  OK")


def test_build_tools():
    _M, Requirement, _S, Version = rl._packaging()
    system = project()["build-system"]
    assert system["build-backend"] == "setuptools.build_meta", system
    build = rl.read_lock(os.path.join(ROOT, rl.BUILD))
    for spec in system["requires"]:
        req = Requirement(spec)
        locked = build[rl.canonical(req.name)][0]
        assert req.specifier.contains(Version(locked)), (spec, locked)
    assert "build" in build and set(rl.BUILD_TOOLS) <= set(build), sorted(build)
    print(f"build backend setuptools {build['setuptools'][0]} and build {build['build'][0]}, "
          "hash-locked in requirements-build.txt  OK")


if __name__ == "__main__":
    if envvars.get("SER2NETWEB_SKIP_PIN_CHECKS") == "1":
        print("SKIP: the ranges against the pins (CI: job 'offline wheels match "
              "requirements.txt')")
    else:
        test_dependencies()
        test_extras()
        test_build_tools()
    test_metadata()
    test_files()
    print("\nPASS: pyproject.toml")
