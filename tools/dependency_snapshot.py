"""The pins GitHub's dependency graph doesn't read, as a dependency-submission snapshot.

    python tools/dependency_snapshot.py [--out FILE]     # JSON on stdout or into FILE

GitHub builds the pip part of the dependency graph (and so the Dependabot alerts) by
parsing requirements.txt itself, but it evaluates `python_version` markers for one
interpreter: the newest Python it has, as the repository names no version. The pins only
an older Python installs (`python_version < "3.11"`: the Python 3.10 line of starlette,
websockets, python-multipart and the exceptiongroup / taskgroup backports) are left out,
so an advisory against one of them would raise no alert although every Python 3.10
install ships it. .github/workflows/dependency-graph.yml submits exactly those pins through
the dependency submission API (POST /repos/{owner}/{repo}/dependency-graph/snapshots), one
manifest per requirements file ("requirements.txt (Python 3.10)"); Dependabot alerts then
cover them like the pins the graph reads. Pins the graph already has are not submitted
again, so an advisory doesn't raise the same alert twice.

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
# is at least the newest Python ser2net supports
GRAPH_PYTHON = rw.PYTHONS[-1]
CORRELATOR = "ser2net-requirements-markers"
DETECTOR = {"name": "ser2net tools/dependency_snapshot.py", "version": "1",
            "url": "https://github.com/haliskilic/ser2net/blob/main/tools/dependency_snapshot.py"}


def canonical(name: str) -> str:
    # PEP 503, as the PyPI purl type wants it: lower case, runs of "-", "_", "." as one "-"
    return re.sub(r"[-_.]+", "-", name).lower()


def graph_reads(marker: str) -> bool:
    """Whether GitHub's own parse of a requirements file keeps a pin with this marker:
    python_version is evaluated for GRAPH_PYTHON, every other marker counts as met."""
    return any(rw.marker_applies(marker, GRAPH_PYTHON, system) for system in rw.SYSTEMS)


def unread_pins(path: str) -> list[tuple[str, str, list[str]]]:
    """[(name, version, the Pythons that install it)] for the pins of a requirements file
    that a supported Python installs but the dependency graph leaves out."""
    out = []
    for name, version, marker in rw.read_requirements(path):
        pythons = [py for py in rw.PYTHONS
                   if any(rw.marker_applies(marker, py, system) for system in rw.SYSTEMS)]
        if pythons and not graph_reads(marker):
            out.append((name, version, pythons))
    return out


def manifests(root: str = ROOT) -> dict:
    out = {}
    for fn in REQUIREMENT_FILES:
        pins = unread_pins(os.path.join(root, fn))
        if not pins:
            continue
        pythons = sorted({py for _n, _v, pys in pins for py in pys}, key=rw._version_tuple)
        key = f"{fn} (Python {', '.join(pythons)})"
        out[key] = {
            "name": key,
            "file": {"source_location": fn},
            "metadata": {"python_versions": ", ".join(pythons)},
            "resolved": {
                canonical(name): {
                    "package_url": f"pkg:pypi/{canonical(name)}@{version}",
                    "relationship": "direct",
                    "scope": "runtime",
                } for name, version, _pys in pins
            },
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
