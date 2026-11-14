"""The conda-forge recipe (packaging/conda-forge/recipe.yaml, rattler-build's v1 format, as
submitted to conda-forge's staged-recipes) and .github/workflows/conda-forge.yml, which
lints and builds it:

  * it builds a release of ser2netweb from PyPI's sdist (at or before ser2netweb/__init__.py's
    version: a release pull request raises the version before PyPI has it), with a SHA-256;
  * the dependencies are pyproject.toml's with their floors, but for pyserial-asyncio-fast,
    which conda-forge doesn't have (its patch takes the line out of pyproject.toml, so pip
    check agrees; ser2netweb runs on its built-in copy of the transport), and the Python 3.10
    backports (conda-forge's python_min is newer); the extras' packages are run_constraints
    with their floors; setuptools as pyproject.toml's build-system asks;
  * noarch: python with python_min (host and tests on it, run >= it), the console script
    pyproject.toml declares as an entry point, every licence file of pyproject.toml's, the
    other recipes' licence expression, the maintainer;
  * the workflow: pull requests touching it, by hand, weekly; read-only; a Miniforge
    container pinned by digest; conda-smithy's lint, rattler-build with conda-forge's
    pinning, then the built package installed and started.
Run: python3 tests/test_conda_recipe.py   (stdlib only; binds nothing)
"""
from __future__ import annotations

import glob
import os
import re
import sys

from _workflows import jobs, read as read_workflow, steps

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
FOLDER = os.path.join(ROOT, "packaging", "conda-forge")
PATCH = "0001-Leave-out-pyserial-asyncio-fast.patch"
LEFT_OUT = "pyserial-asyncio-fast"

try:
    import tomllib
except ImportError:  # Python 3.10: pip's copy
    from pip._vendor import tomli as tomllib


def text(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


RECIPE = text(FOLDER, "recipe.yaml")


def section(name: str, body: str = RECIPE, indent: str = "") -> str:
    """The lines under `name:` at `indent`, up to the next line indented no deeper."""
    m = re.search(rf"(?ms)^{indent}{re.escape(name)}:\n(.*?)(?=^ {{0,{len(indent)}}}\S|\Z)",
                  body)
    assert m, f"recipe.yaml has no {indent}{name}:"
    return m.group(1)


def items(block: str) -> list[str]:
    return [m.strip() for m in re.findall(r"(?m)^\s*- (.+?)\s*$", block)
            if not m.lstrip().startswith("#")]


def specs(block: str) -> dict[str, str]:
    out = {}
    for item in items(block):
        name, _, spec = item.partition(" ")
        out[name] = spec
    return out


def pyproject() -> dict:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)


def _requirement(req: str) -> tuple[str, str, str]:
    m = re.fullmatch(r"\s*([A-Za-z0-9._-]+)\s*>=\s*([0-9][\w.]*)\s*(?:;\s*(.+))?", req)
    assert m, req
    return m.group(1).lower(), m.group(2), m.group(3) or ""


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def test_release_from_pypi():
    version = re.search(r'(?m)^context:\n  version: "(\d+\.\d+\.\d+)"$', RECIPE).group(1)
    with open(os.path.join(ROOT, "ser2netweb", "__init__.py"), encoding="utf-8") as fh:
        ours = re.search(r'(?m)^__version__ = "(.*)"$', fh.read()).group(1)
    assert version_tuple(version) <= version_tuple(re.match(r"[\d.]+\d", ours).group(0)), \
        (version, ours)
    source = section("source")
    assert "  url: https://files.pythonhosted.org/packages/source/s/ser2netweb/" \
           "ser2netweb-${{ version }}.tar.gz\n" in source, source
    assert re.search(r"(?m)^  sha256: [0-9a-f]{64}$", source), source
    assert items(section("patches", source, "  ")) == [PATCH]
    assert re.search(r"(?m)^  number: 0$", section("build"))
    print(f"recipe.yaml: ser2netweb {version} from PyPI's sdist, build 0  OK")


def test_dependencies():
    project = pyproject()
    floors, py310 = {}, set()
    for req in project["project"]["dependencies"]:
        name, floor, marker = _requirement(req)
        if marker:
            assert marker == 'python_version < "3.11"', marker
            py310.add(name)
        elif name != LEFT_OUT:
            floors[name] = floor
    assert py310 == {"exceptiongroup", "taskgroup"}, py310
    reqs = section("requirements")
    run = specs(section("run", reqs, "  "))
    assert run.pop("python") == ">=${{ python_min }}", run
    assert run == {name: f">={floor}" for name, floor in floors.items()}, run
    extras = {}
    for extra, reqs_ in project["project"]["optional-dependencies"].items():
        if extra != "all":
            for req in reqs_:
                name, floor, _m = _requirement(req)
                extras[name] = f">={floor}"
    assert specs(section("run_constraints", reqs, "  ")) == extras
    host = specs(section("host", reqs, "  "))
    (backend,) = project["build-system"]["requires"]
    assert host == {"python": "${{ python_min }}.*", "pip": "",
                    "setuptools": backend.removeprefix("setuptools")}, host
    # the patch: pyproject.toml's line for pyserial-asyncio-fast, nothing else
    patch = text(FOLDER, PATCH)
    removed = [ln[1:] for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    added = [ln for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert removed == ['    "pyserial-asyncio-fast>=0.16",'] and not added, (removed, added)
    assert "\n--- a/pyproject.toml\n+++ b/pyproject.toml\n" in patch
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        assert removed[0] + "\n" in fh.read(), "the patch's line is pyproject.toml's"
    print(f"recipe.yaml: pyproject.toml's {len(floors)} dependencies with their floors (not "
          f"{LEFT_OUT}: its patch; not the Python 3.10 backports), the extras as "
          "run_constraints  OK")


def test_noarch_entry_point_licence():
    project = pyproject()
    build = section("build")
    assert "  noarch: python\n" in build and "  script: ${{ PYTHON }} -m pip install . -vv\n" \
        in build, build
    (script, target), = project["project"]["scripts"].items()
    assert items(section("entry_points", section("python", build, "  "), "    ")) == \
        [f"{script} = {target}"]
    tests = section("tests")
    assert "      pip_check: true\n" in tests
    assert items(section("python_version", tests, "      ")) == ['${{ python_min }}.*', '"*"']
    assert {"ser2netweb", "ser2netweb.cli"} <= set(items(section("imports", tests, "      ")))
    for command in ("ser2netweb --version", "ser2netweb --about", "ser2netweb --help"):
        assert f"      - {command}\n" in tests, command
    about = section("about")
    spec = text(ROOT, "packaging", "fedora", "ser2netweb.spec")
    licence = re.search(r"(?m)^License:\s+(.+)$", spec).group(1)
    assert f"  license: {licence}\n" in about, (licence, about)
    files = []
    for pattern in project["project"]["license-files"]:
        files += sorted(os.path.relpath(p, ROOT).replace(os.sep, "/")
                        for p in glob.glob(os.path.join(ROOT, *pattern.split("/"))))
    assert items(section("license_file", about, "  ")) == files, files
    assert items(section("recipe-maintainers", section("extra"), "  ")) == ["haliskilic"]
    assert RECIPE.splitlines()[4] == "schema_version: 1", "the v1 format"
    print("recipe.yaml: noarch python on python_min, the entry point, the tests, every licence "
          "file, the licence expression, the maintainer  OK")


def test_workflow():
    wf = read_workflow("conda-forge.yml")
    on = wf.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.findall(r"(?m)^  ([a-z_]+):", on) == ["pull_request", "workflow_dispatch",
                                                     "schedule"], on
    for path in (".github/workflows/conda-forge.yml", "packaging/conda-forge/**",
                 "tests/test_conda_recipe.py"):
        assert f"      - {path}\n" in on, path
    assert re.search(r"(?m)^permissions:\n  contents: read\n", wf)
    (build,) = jobs(wf).values()
    assert re.search(r"(?m)^      image: condaforge/miniforge3:[\w.-]+@sha256:[0-9a-f]{64}$",
                     build), "a pinned container"
    body = "\n".join(steps(build))
    assert "persist-credentials: false" in body
    for needle in ("conda-smithy recipe-lint --conda-forge packaging/conda-forge",
                   "rattler-build build --recipe packaging/conda-forge/recipe.yaml",
                   '--variant-config "$(conda info --base)/conda_build_config.yaml"',
                   "conda-forge-pinning", '--healthcheck --data-dir "$data"'):
        assert needle in body, needle
    assert "secrets." not in wf and "write" not in wf.split("\njobs:", 1)[0]
    assert "conda-forge.yml" not in read_workflow("ci.yml"), "not a merge condition"
    print("conda-forge.yml: pull requests, by hand, weekly; read-only; a pinned Miniforge "
          "container; conda-smithy's lint, rattler-build with conda-forge's pinning, the "
          "package installed and started  OK")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    test_release_from_pypi()
    test_dependencies()
    test_noarch_entry_point_licence()
    test_workflow()
    print("\nPASS: conda-forge recipe")
