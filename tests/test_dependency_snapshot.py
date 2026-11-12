"""requirements.txt goes to the dependency graph complete, Python 3.10 pins included.

GitHub parses requirements.txt for the dependency graph (and the Dependabot alerts) with
its python_version markers evaluated for its newest Python, so the Python 3.10 pins are
missing there. tools/dependency_snapshot.py turns every pin of such a file into a
dependency-submission manifest for the same file (a submission takes the place of
GitHub's own parse of it), and .github/workflows/dependency-graph.yml submits it from main.
Files the graph reads completely are not submitted.

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


def test_requirements_submitted_complete():
    m = ds.manifests()
    assert list(m) == ["requirements.txt"], list(m)  # requirements-optional.txt has no markers
    manifest = m["requirements.txt"]
    assert manifest["name"] == "requirements.txt"
    assert manifest["file"] == {"source_location": "requirements.txt"}
    got = {v["package_url"] for v in manifest["resolved"].values()}
    want, py310 = set(), set()
    with open(os.path.join(HERE, "..", "requirements.txt"), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                name, version = line.split(";")[0].strip().split("==")
                want.add(f"pkg:pypi/{ds.canonical(name)}@{version}")
                if line.endswith('; python_version < "3.11"'):
                    py310.add(f"pkg:pypi/{ds.canonical(name)}@{version}")
    assert got == want, (sorted(got ^ want))
    assert len(py310) >= 5 and py310 <= got, "the pins GitHub's parse leaves out are in it"
    assert "pkg:pypi/starlette@1.10.0" in got and len([u for u in got if "/starlette@" in u]) == 2
    assert all(v["relationship"] == "direct" and v["scope"] == "runtime"
               for v in manifest["resolved"].values())
    marked = {v["package_url"]: v["metadata"]["marker"]
              for v in manifest["resolved"].values() if "metadata" in v}
    assert all(marked[u] == 'python_version < "3.11"' for u in py310), marked
    print(f"requirements.txt submitted with all {len(got)} pins, the {len(py310)} Python 3.10 "
          "ones included  OK")


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
        unread = ds.unread_pins(os.path.join(tmp, "requirements.txt"))
        os.remove(os.path.join(tmp, "requirements.txt"))
        with open(os.path.join(tmp, "requirements.txt"), "w", encoding="utf-8") as fh:
            fh.write('plain==1.0\nwinonly==3.0 ; platform_system == "Windows"\n')
        complete = ds.manifests(tmp)
    # read by GitHub: no marker, a marker met on the newest Python, a platform marker
    assert [n for n, _v, _m in unread] == ["Two_Names", "old.backport"], unread
    assert list(m) == ["requirements.txt"], m  # the optional file: read completely
    # every pin a supported Python installs (3.9 is not supported)
    assert set(m["requirements.txt"]["resolved"]) == {
        "plain@1.0", "two-names@2.0", "two-names@1.5", "winonly@3.0", "old-backport@0.1"}
    assert complete == {}, "a file the graph reads completely is left to it"
    print("submitted: a file with pins the graph leaves out, complete; others not  OK")


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
    test_requirements_submitted_complete()
    test_marker_rules()
    test_snapshot_document()
    test_workflow()
    print("\nPASS: dependency snapshot")
