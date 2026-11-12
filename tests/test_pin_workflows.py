"""A pin bump fails one CI job, and Dependabot's branches get their wheels refreshed.

* The test matrix and the coverage job set SER2NET_SKIP_PIN_CHECKS, so the checks of
  vendor/wheels and the build locks against the pins run in the "offline wheels match
  requirements.txt" job alone (plus their comparison with PyPI); when it fails it attaches
  the refreshed files as a patch and says how to apply them.
* .github/workflows/refresh-pins.yml refreshes them on Dependabot's pull request branches
  of this repository (or on a branch it is started on by hand, never main), pushes the
  commit and starts CI on it, with no permission beyond that and GitHub's own actions.

Run: python3 tests/test_pin_workflows.py   (stdlib only)
"""
import re

from _workflows import jobs, key, permissions, read, steps, uses


def test_one_job_checks_the_pins():
    ci = jobs(read("ci.yml"))
    for job in ("test", "test-next", "coverage"):
        assert re.search(r'(?m)^      SER2NET_SKIP_PIN_CHECKS: "1"$', ci[job]), job
    vendor = ci["vendor"]
    assert key(vendor, "name") == "offline wheels match requirements.txt"
    assert "SER2NET_SKIP_PIN_CHECKS" not in vendor.split("\n    steps:\n", 1)[1]
    for cmd in ("python tests/test_vendor_wheels.py", "python tests/test_build_lock.py",
                "python tests/test_vendor_assets.py", "python tools/refresh_wheels.py --verify",
                "python tools/refresh_locks.py --verify", "python tools/refresh_assets.py --verify"):
        assert cmd in vendor, cmd
    on_failure = [s for s in steps(vendor) if "\n        if: failure()\n" in s]
    assert len(on_failure) == 2, on_failure
    assert "refreshed-pins.patch" in on_failure[0] and "GITHUB_STEP_SUMMARY" in on_failure[0]
    assert "name: refreshed-pins" in on_failure[1]
    assert permissions(vendor) == {}, "the vendor job stays read-only"
    print("pin checks: skipped in the matrix, run (and refreshed on failure) in the vendor job  OK")


def test_refresh_workflow():
    text = read("refresh-pins.yml")
    on = text.split("\npermissions:", 1)[0]
    assert "\n  pull_request:\n" in on and "pull_request_target" not in on, on
    assert "      - vendor/npm/package.json" in on, "Dependabot's npm updates too"
    assert "\n  workflow_dispatch:\n" in on
    assert "\npermissions:\n  contents: read\n" in text
    job = jobs(text)["refresh"]
    assert permissions(job) == {"contents": "write", "actions": "write"}, permissions(job)
    cond = job.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in cond
    assert "github.event.pull_request.head.repo.full_name == github.repository" in cond
    assert "github.ref != 'refs/heads/main'" in cond
    assert "github.actor" not in cond, "github.actor can be spoofed (zizmor bot-conditions)"
    refs = [ref.split("@")[0] for _l, ref, _r in uses(job)]
    assert all(r.startswith("actions/") for r in refs), refs
    assert "ref: ${{ github.head_ref || github.ref }}" in job, "the branch, not a merge commit"
    assert "persist-credentials: false" in job
    run = "".join(steps(job))
    for cmd in ("python tools/refresh_wheels.py\n", "python tools/refresh_wheels.py --verify",
                "python tools/refresh_locks.py --verify", "python tools/refresh_assets.py --verify",
                'push origin "HEAD:refs/heads/$BRANCH"',
                'gh workflow run ci.yml --ref "$BRANCH"'):
        assert cmd in run, cmd
    assert "${{" not in run.split("run: |", 1)[1].split("- id: push", 1)[0], "no expressions in run:"
    print("refresh-pins.yml: Dependabot's own branches or a manual run, push + CI, least privilege  OK")


if __name__ == "__main__":
    test_one_job_checks_the_pins()
    test_refresh_workflow()
    print("\nPASS: pin workflows")
