"""CI's merge gate covers every job, and the required check names stay.

The ci-ok job in .github/workflows/ci.yml is the one check branch protection needs: it
passes only when every job it needs passed. A job added to ci.yml but not to ci-ok's
needs would not gate a merge, so this test fails until it is listed there (test-next,
the newest Python, is the one job left out on purpose; it must be continue-on-error).
Until branch protection requires ci-ok instead, it also asks for the 14 check names it
requires today: a renamed or removed one would block every pull request.

Run: python3 tests/test_ci_gate.py   (stdlib only)
"""
import os
import re

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
NOT_GATING = {"test-next"}  # may fail (newest Python); not a merge condition


def _read(name: str) -> str:
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as fh:
        return fh.read()


def _jobs(text: str) -> dict[str, str]:
    """Job id -> its block, for the top-level jobs: mapping of a workflow file."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_ci_ok_needs_every_job():
    jobs = _jobs(_read("ci.yml"))
    assert "ci-ok" in jobs, "ci.yml has no ci-ok job"
    gate = jobs["ci-ok"]
    m = re.search(r"(?m)^    needs: \[([^\]]*)\]", gate)
    assert m, "ci-ok must list its needs as [a, b, ...]"
    needs = {n.strip() for n in m.group(1).split(",") if n.strip()}
    want = set(jobs) - {"ci-ok"} - NOT_GATING
    assert needs == want, (f"ci-ok's needs must be every job of ci.yml: missing "
                           f"{sorted(want - needs)}, unknown {sorted(needs - want)}")
    assert re.search(r"(?m)^    if: always\(\)$", gate), "ci-ok must run when a job failed"
    assert "all(.[]; .result == \"success\")" in gate, "ci-ok must fail on any other result"
    for job in NOT_GATING:
        assert re.search(r"(?m)^    continue-on-error: true$", jobs[job]), job
    print(f"ci-ok needs every CI job ({len(needs)}), fails on any other result  OK")


def test_required_check_names_kept():
    ci = _read("ci.yml")
    jobs = _jobs(ci)
    for job, name in (("lint", "ruff"), ("typecheck", "mypy"),
                      ("vendor", "offline wheels match requirements.txt"),
                      ("ui", "ui layout (Chromium)")):
        assert f"\n    name: {name}\n" in "\n" + jobs[job], (job, name)
    test = jobs["test"]
    assert "\n    name: test ${{ matrix.os }} py${{ matrix.python-version }}\n" in "\n" + test
    assert re.search(r"os: \[ubuntu-latest, windows-latest\]", test)
    versions = re.search(r"python-version: \[([^\]]*)\]", test).group(1)
    for v in ("3.10", "3.11", "3.12", "3.13"):
        assert f'"{v}"' in versions, v
    # "standalone / build linux (glibc 2.28 baseline)" and "standalone / build windows"
    assert "uses: ./.github/workflows/standalone.yml" in jobs["standalone"]
    standalone = _read("standalone.yml")
    assert "'build linux (glibc 2.28 baseline)'" in standalone
    assert "\n    name: build windows\n" in standalone
    print("the 14 check names main requires today still exist  OK")


if __name__ == "__main__":
    test_ci_ok_needs_every_job()
    test_required_check_names_kept()
    print("\nPASS: CI merge gate")
