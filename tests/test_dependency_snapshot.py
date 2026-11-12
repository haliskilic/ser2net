"""The pins GitHub's dependency graph leaves out are submitted, and only those.

GitHub parses requirements.txt for the dependency graph (and the Dependabot alerts) with
its python_version markers evaluated for its newest Python, so the Python 3.10 pins are
missing there. tools/dependency_snapshot.py turns exactly those into a dependency-
submission snapshot, and .github/workflows/dependency-graph.yml submits it from main.

Run: python3 tests/test_dependency_snapshot.py   (stdlib only)
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import dependency_snapshot as ds
from _workflows import jobs, permissions, read, steps, uses


def test_python310_pins_submitted():
    m = ds.manifests()
    assert list(m) == ["requirements.txt (Python 3.10)"], list(m)
    manifest = m["requirements.txt (Python 3.10)"]
    assert manifest["file"] == {"source_location": "requirements.txt"}
    pins = {k: v["package_url"] for k, v in manifest["resolved"].items()}
    # every python_version < "3.11" line of requirements.txt, nothing else
    want = {}
    with open(os.path.join(HERE, "..", "requirements.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line.endswith('; python_version < "3.11"'):
                name, version = line.split(";")[0].strip().split("==")
                want[ds.canonical(name)] = f"pkg:pypi/{ds.canonical(name)}@{version}"
    assert len(want) >= 5 and pins == want, (pins, want)
    assert all(v["relationship"] == "direct" and v["scope"] == "runtime"
               for v in manifest["resolved"].values())
    print(f"the {len(pins)} Python 3.10 pins are submitted: {', '.join(sorted(pins))}  OK")


def test_marker_rules():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "requirements.txt"), "w", encoding="utf-8") as fh:
            fh.write("# comment\n"
                     "plain==1.0\n"
                     'Two_Names==2.0 ; python_version >= "3.11"\n'
                     'Two_Names==1.5 ; python_version < "3.11"\n'
                     'winonly==3.0 ; platform_system == "Windows"\n'
                     'old.backport==0.1 ; python_version < "3.11"  # trailing comment\n'
                     'nowhere==9.9 ; python_version < "3.9"\n')
        with open(os.path.join(tmp, "requirements-optional.txt"), "w", encoding="utf-8") as fh:
            fh.write("extra==1.0  # no markers: in the graph\n")
        m = ds.manifests(tmp)
    assert list(m) == ["requirements.txt (Python 3.10)"], m
    resolved = m["requirements.txt (Python 3.10)"]["resolved"]
    # read by GitHub: no marker, a marker met on the newest Python, a platform marker;
    # never installed (3.9 is not supported): nothing to submit
    assert set(resolved) == {"two-names", "old-backport"}, resolved
    assert resolved["two-names"]["package_url"] == "pkg:pypi/two-names@1.5"
    print("submitted: older-Python pins only (not the platform or >= 3.11 ones)  OK")


def test_snapshot_document():
    env = {"GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "123"}
    snap = ds.snapshot(env=env)
    assert snap["version"] == 0 and snap["sha"] == "a" * 40 and snap["ref"] == "refs/heads/main"
    assert snap["job"] == {"correlator": ds.CORRELATOR, "id": "123"}, snap["job"]
    assert set(snap["detector"]) == {"name", "version", "url"}
    assert snap["scanned"].endswith("Z") and "T" in snap["scanned"]
    for manifest in snap["manifests"].values():
        assert all(isinstance(v, (str, int, float, bool)) for v in manifest["metadata"].values())
    # the command line writes the same document
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "snap.json")
        subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "dependency_snapshot.py"),
                        "--out", out], check=True, capture_output=True,
                       env=dict(os.environ, **env))
        with open(out, encoding="utf-8") as fh:
            written = json.load(fh)
    assert written["manifests"] == snap["manifests"] and written["sha"] == "a" * 40
    print("snapshot: version, sha, ref, job, detector, scanned, manifests  OK")


def test_workflow():
    text = read("dependency-graph.yml")
    on = text.split("\npermissions:", 1)[0]
    assert "\n  push:\n    branches: [main]\n" in on and "pull_request" not in on, on
    assert "      - requirements.txt\n" in on and "      - tools/dependency_snapshot.py\n" in on
    job = jobs(text)["submit"]
    assert permissions(job) == {"contents": "write"}, permissions(job)
    assert "if: github.ref == 'refs/heads/main'" in job
    assert [ref.split("@")[0] for _l, ref, _r in uses(job)] == ["actions/checkout"]
    run = "".join(steps(job))
    assert "python3 tools/dependency_snapshot.py --out snapshot.json" in run
    assert 'gh api "repos/$GITHUB_REPOSITORY/dependency-graph/snapshots" --input snapshot.json' in run
    print("dependency-graph.yml: main only, contents: write only, submits the snapshot  OK")


if __name__ == "__main__":
    test_python310_pins_submitted()
    test_marker_rules()
    test_snapshot_document()
    test_workflow()
    print("\nPASS: dependency snapshot")
