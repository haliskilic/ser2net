"""CI's merge gate covers every job, and the required check names stay.

The ci-ok job in .github/workflows/ci.yml is the one check branch protection needs: it
passes only when every job it needs passed. A job added to ci.yml but not to ci-ok's
needs would not gate a merge, so this test fails until it is listed there (test-next,
the newest Python, and min-deps, the oldest dependencies pyproject.toml allows, are left
out on purpose; they must be continue-on-error). latest-deps.yml, the newest release of
every dependency once a week, gates nothing either: a workflow of its own, scheduled or
started by hand only, which no other workflow runs.
Until branch protection requires ci-ok instead, it also asks for the 14 check names it
requires today: a renamed or removed one would block every pull request. And every push
to main gets a run of its own (no shared concurrency group that drops waiting runs).

Run: python3 tests/test_ci_gate.py   (stdlib only)
"""
import re

from _workflows import jobs as _jobs
from _workflows import names as _names
from _workflows import read as _read

# may fail (the newest Python; the floors of pyproject.toml): not merge conditions
NOT_GATING = {"test-next", "min-deps"}


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


def test_every_main_push_gets_a_run():
    # GitHub keeps one waiting run per concurrency group and cancels the one before it, so
    # a group shared by all main pushes (ci-refs/heads/main) skipped commits of a quick
    # series of merges. Only a pull request's runs share a group (and cancel each other).
    ci = _read("ci.yml")
    block = ci.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
    group = re.search(r"(?m)^  group: (.*)$", block).group(1)
    assert group == "ci-${{ github.event.pull_request.number || github.run_id }}", group
    assert re.search(r"(?m)^  cancel-in-progress: \$\{\{ github.event_name == "
                     r"'pull_request' \}\}$", block), block
    print("pull requests share a concurrency group; every other run has its own  OK")


def test_latest_deps_never_gates():
    text = _read("latest-deps.yml")
    on = text.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    triggers = re.findall(r"(?m)^  ([a-z_]+):", on)
    assert triggers == ["schedule", "workflow_dispatch"], triggers  # no push / pull_request
    jobs = _jobs(text)
    assert list(jobs) == ["latest-deps"], list(jobs)
    job = jobs["latest-deps"]
    assert re.search(r"(?m)^    continue-on-error: true$", job), "latest-deps may fail"
    assert "latest-deps" not in _jobs(_read("ci.yml")), "a job of its own workflow only"
    for name in _names():
        assert "workflows/latest-deps.yml" not in _read(name), f"{name} runs latest-deps.yml"
    # the newest releases, by name (no requirements file, no version), then the whole suite
    assert "pip install --upgrade --upgrade-strategy eager" in job and "requirements" \
        not in job.replace("latest-requirements.txt", ""), job
    assert "python -m pip check" in job and "python tests/run_all.py --socat" in job, job
    print("latest-deps.yml: scheduled and manual only, continue-on-error, run by no other "
          "workflow; the newest releases, pip check, the whole suite  OK")


if __name__ == "__main__":
    test_ci_ok_needs_every_job()
    test_required_check_names_kept()
    test_every_main_push_gets_a_run()
    test_latest_deps_never_gates()
    print("\nPASS: CI merge gate")
