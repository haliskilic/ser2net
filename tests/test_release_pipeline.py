"""The release pipeline publishes only what every check passed, with pinned actions.

* every action in .github/workflows is pinned to a full commit SHA with its version in a
  comment (Dependabot updates both): a moved or replaced tag can't change what a job
  runs, and the repository's "require full-length SHA pins" policy can be switched on.
* release.yml checks the tag first (tools/release_guard.py tag, tests/test_release_guard.py;
  the tagged commit must be on main), and every other job waits for that check.
* it runs the whole CI (ci.yml, the checks ci-ok gates merges on) on the tagged commit,
  and nothing that writes (assets, attestations, the image) starts before all of it
  passed; its standalone job builds the released files. One release runs at a time.

Run: python3 tests/test_release_pipeline.py   (stdlib only)
"""
import re

from _workflows import jobs, key, names, permissions, read, transitive_needs, uses

_PINNED = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")


def test_actions_pinned_by_sha():
    count = 0
    for name in names():
        for line, ref, rest in uses(read(name)):
            if ref.startswith("./.github/workflows/"):
                continue  # a reusable workflow of this repository, same commit
            assert _PINNED.fullmatch(ref), f"{name}:{line}: {ref} is not pinned to a commit SHA"
            assert re.match(r"\s+# v\d", rest), (
                f"{name}:{line}: {ref} needs its version as a '# vX.Y.Z' comment")
            count += 1
    assert count > 40, count
    print(f"all {count} action references are pinned to a commit SHA  OK")


def test_tag_checked_first():
    release = jobs(read("release.yml"))
    verify = release["verify"]
    assert 'python3 tools/release_guard.py tag "$GITHUB_REF_NAME"' in verify, verify
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in verify, verify
    assert "fetch-depth: 0" in verify, "the ancestry check needs main's history"
    for job in release:
        if job != "verify":
            assert "verify" in transitive_needs(release, job), f"{job} doesn't wait for verify"
    print(f"every release job ({len(release) - 1}) waits for the tag check  OK")


def _writes(block: str) -> bool:
    return "write" in permissions(block).values()


def test_release_runs_the_whole_ci():
    text = read("release.yml")
    release = jobs(text)
    assert key(release["ci"], "uses") == "./.github/workflows/ci.yml", release["ci"]
    assert re.search(r"(?m)^      publish: ", release["ci"]), "ci needs the publish input"
    for other in ("standalone.yml", "docker.yml", "audit.yml"):
        # through ci.yml only, so the release can't skip a check CI has
        assert f"uses: ./.github/workflows/{other}" not in text, other
    writers = [job for job, block in release.items() if _writes(block)]
    assert writers, "no publishing job found"
    for job in writers:
        assert "ci" in transitive_needs(release, job), f"{job} publishes before CI passed"
    ci = read("ci.yml")
    call = ci.split("\n  workflow_call:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.search(r"(?m)^      publish:\n(?:        .*\n)*        type: boolean$", call), call
    for secret in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                   "SIGNPATH_API_TOKEN"):
        assert f"\n      {secret}:\n        required: false" in call, secret
        # handed on only for a release build, not to pull requests or main
        assert (f"      {secret}: ${{{{ inputs.publish && secrets.{secret} || '' }}}}"
                in jobs(ci)["standalone"]), secret
    assert "      publish: ${{ inputs.publish == true }}" in jobs(ci)["standalone"]
    # the newest Python's leg may fail; it must not hold a release up
    assert (key(jobs(ci)["test-next"], "if")
            == "${{ !contains(github.workflow_ref, '/.github/workflows/release.yml@') }}")
    print(f"the release runs ci.yml; its {len(writers)} publishing jobs wait for all of it  OK")


def test_one_release_at_a_time():
    text = read("release.yml")
    block = text.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
    group = re.search(r"(?m)^  group: (.*)$", block).group(1)
    assert "release" in group and not group.startswith("ci-"), group
    assert re.search(r"(?m)^  cancel-in-progress: false$", block), block
    print(f"release concurrency group {group}, never cancelled  OK")


if __name__ == "__main__":
    test_actions_pinned_by_sha()
    test_tag_checked_first()
    test_release_runs_the_whole_ci()
    test_one_release_at_a_time()
    print("\nPASS: release pipeline")
