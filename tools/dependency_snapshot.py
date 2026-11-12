"""requirements.txt for the dependency graph, with the pins GitHub's own parse leaves out.

    python tools/dependency_snapshot.py [--out FILE]     # JSON on stdout or into FILE

GitHub builds the pip part of the dependency graph (and so the Dependabot alerts) by
parsing requirements.txt itself, but it evaluates `python_version` markers for one
interpreter: the newest Python it has, as the repository names no version. The pins only
an older Python installs (`python_version < "3.11"`: the Python 3.10 line of starlette,
websockets, python-multipart and the exceptiongroup / taskgroup backports) are left out,
so an advisory against one of them would raise no alert although every Python 3.10
install ships it. .github/workflows/dependency-graph.yml therefore submits every pin of
such a file, for all the Pythons and systems ser2netweb supports, through the dependency
submission API (POST /repos/{owner}/{repo}/dependency-graph/snapshots). GitHub shows one
source per manifest file and prefers a submission to its own parse, so the submitted
requirements.txt (complete, both lines of a pin split by Python version) takes the place
of the parse that lacks the Python 3.10 pins; a file the graph reads completely
(requirements-optional.txt, the hash locks) is not submitted.

The commit, ref and run come from the GitHub Actions environment (GITHUB_SHA, GITHUB_REF,
GITHUB_RUN_ID); run elsewhere, it only shows what would be submitted.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from collections.abc import Mapping

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refresh_wheels as rw  # tools/ is not a package

ROOT = rw.ROOT
REQUIREMENT_FILES = ("requirements.txt", "requirements-optional.txt")
# the interpreter GitHub's parser evaluates python_version markers for: its newest, which
# is at least the newest Python ser2netweb supports
GRAPH_PYTHON = rw.PYTHONS[-1]
CORRELATOR = "ser2net-requirements-markers"
DETECTOR = {"name": "ser2netweb tools/dependency_snapshot.py", "version": "1",
            "url": "https://github.com/haliskilic/ser2net/blob/main/tools/dependency_snapshot.py"}


def canonical(name: str) -> str:
    # PEP 503, as the PyPI purl type wants it: lower case, runs of "-", "_", "." as one "-"
    return re.sub(r"[-_.]+", "-", name).lower()


def graph_reads(marker: str) -> bool:
    """Whether GitHub's own parse of a requirements file keeps a pin with this marker:
    python_version is evaluated for GRAPH_PYTHON, every other marker counts as met."""
    return any(rw.marker_applies(marker, GRAPH_PYTHON, system) for system in rw.SYSTEMS)


def installed_pins(path: str) -> list[tuple[str, str, str]]:
    """[(name, version, marker)] for the pins of a requirements file that some supported
    Python on some supported system installs."""
    return [(name, version, marker) for name, version, marker in rw.read_requirements(path)
            if any(rw.marker_applies(marker, py, system)
                   for py in rw.PYTHONS for system in rw.SYSTEMS)]


def unread_pins(path: str) -> list[tuple[str, str, str]]:
    """The installed pins of a requirements file that the dependency graph leaves out."""
    return [pin for pin in installed_pins(path) if not graph_reads(pin[2])]


def manifests(root: str = ROOT) -> dict:
    """{file: manifest} for the requirements files the graph doesn't read completely,
    each with every pin it has (so the submission can stand in for GitHub's parse)."""
    out = {}
    for fn in REQUIREMENT_FILES:
        path = os.path.join(root, fn)
        if not unread_pins(path):
            continue
        resolved = {}
        for name, version, marker in installed_pins(path):
            entry: dict[str, object] = {"package_url": f"pkg:pypi/{canonical(name)}@{version}",
                                        "relationship": "direct", "scope": "runtime"}
            if marker:
                entry["metadata"] = {"marker": marker}
            resolved[f"{canonical(name)}@{version}"] = entry
        out[fn] = {
            "name": fn,
            "file": {"source_location": fn},
            "metadata": {"python_versions": f"{rw.PYTHONS[0]}-{rw.PYTHONS[-1]}",
                         "systems": ", ".join(rw.SYSTEMS)},
            "resolved": resolved,
        }
    return out


def snapshot(root: str = ROOT, env: Mapping[str, str] | None = None) -> dict:
    if env is None:
        env = os.environ
    return {
        "version": 0,
        "sha": env.get("GITHUB_SHA", ""),
        "ref": env.get("GITHUB_REF", ""),
        "job": {"correlator": CORRELATOR, "id": env.get("GITHUB_RUN_ID") or "local"},
        "detector": DETECTOR,
        "scanned": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifests": manifests(root),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="write the snapshot here instead of stdout")
    args = ap.parse_args(argv)
    text = json.dumps(snapshot(), indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        snap = json.loads(text)
        for key, m in snap["manifests"].items():
            print(f"{key}: " + ", ".join(r["package_url"] for r in m["resolved"].values()))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
