"""The release pipeline publishes only what every check passed, with pinned actions.

* every action in .github/workflows is pinned to a full commit SHA with its version in a
  comment (Dependabot updates both): a moved or replaced tag can't change what a job
  runs, and the repository's "require full-length SHA pins" policy can be switched on.
* release.yml checks the tag first (tools/release_guard.py tag, tests/test_release_guard.py;
  the tagged commit must be on main), and every other job waits for that check.
* it runs the whole CI (ci.yml, the checks ci-ok gates merges on) on the tagged commit,
  and nothing that writes (assets, attestations, the image) starts before all of it
  passed; its standalone job builds the released files. One release runs at a time.
* the image is pushed by digest only, as ghcr.io/<owner>/ser2netweb and under no other
  name (ghcr.io/<owner>/ser2net, its name up to 2.9.x, stopped at 3.0.0), that digest is
  smoke-tested on amd64 and arm64 natively and on arm/v7 under QEMU, and only then (after
  the release files) does it get its tags, unchanged, and its attestation. The tags come
  from the tag check: a pre-release never moves X.Y or latest.
* the jobs that can mint attestations or write the release run GitHub's own actions only;
  the attest job can't write the release, and the release is created by the runner's gh
  CLI from exactly the attested files (a pre-release marked as one).
* the PyPI package goes up last, once the release is out: the sdist and wheel ci.yml's job
  "pypi package (build)" made and checked (artifact pypi-dist), by trusted publishing
  (id-token only, the environment pypi, PyPI's own action pinned by SHA, no checkout); a
  failure there (no trusted publisher yet) leaves the release and the image as they are;
* once the GHCR tags are there, the same digest gets the same tags on Docker Hub
  (docker-hub.yml, also image-refresh.yml's and a manual run's): imagetools create from
  GHCR by digest, no rebuild, each tag checked to be that digest, only the two Docker Hub
  secrets handed over, read-only otherwise, skipped with a notice without them; nothing
  waits for it;
* a manual run (workflow_dispatch) is a dry run: it checks, builds and tests the same way
  but attests, releases, tags and uploads nothing, and neither waits for nor blocks a
  release.
* the release files are the ser2netweb-* names tools/release_guard.py lists (ser2net-* up
  to 2.9.x, none of which a release has any more) and the .deb / .rpm packages
  standalone.yml built from the released binaries, checked by its `assets` and `sboms`
  guards before anything is attested; every executable gets its provenance and SBOM
  attestation, every package its provenance.

Run: python3 tests/test_release_pipeline.py   (stdlib only)
"""
import os
import re
import sys

from _workflows import (ROOT, jobs, key, names, needs, permissions, read, steps,
                        transitive_needs, uses)

sys.path.insert(0, os.path.join(ROOT, "tools"))

import release_guard

_PINNED = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")
# builds and pushes the image by digest while CI runs: nothing points at it until it
# passed its own test, CI and the release files are out
UNTAGGED_PUSH = "image-build"


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
    assert 'if git merge-base --is-ancestor "$GITHUB_SHA" origin/main; then' in verify, verify
    assert 'is not a commit of main"; exit 1' in verify, verify
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
    writers = [job for job, block in release.items()
               if _writes(block) and job != UNTAGGED_PUSH]
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


def test_image_published_as_tested():
    text = read("release.yml")
    release = jobs(text)
    build = release[UNTAGGED_PUSH]
    assert permissions(build) == {"contents": "read", "packages": "write"}, permissions(build)
    assert ("outputs: type=image,name=${{ needs.verify.outputs.image }},push-by-digest=true,"
            "name-canonical=true,push=true") in build, build
    assert "\n          tags: ${{" not in build and "\n          push: true" not in build, \
        "the build pushes no tag"
    # before anything is published: tagging won't push another manifest
    assert 'docker buildx imagetools create --dry-run "$IMAGE@$DIGEST"' in build, build
    assert "value=latest" not in text and "{{major}}" not in text, \
        "image tags come from the tag check, not metadata-action"
    test = release["image-test"]
    assert "REF: ${{ needs.image-build.outputs.image }}@${{ needs.image-build.outputs.digest }}" \
        in test, test
    assert '      - run: tests/docker_smoke.sh "$REF"\n' in test, test
    assert "{ arch: amd64, runner: ubuntu-latest }" in test
    assert "{ arch: arm64, runner: ubuntu-24.04-arm }" in test
    assert "{ arch: arm/v7, runner: ubuntu-latest, platform: linux/arm/v7 }" in test
    assert "platforms: linux/amd64,linux/arm64,linux/arm/v7\n" in build, "every tested platform"
    assert "DOCKER_DEFAULT_PLATFORM: ${{ matrix.platform }}" in test, "the emulated leg's runs"
    for job, block in release.items():
        if _writes(block) and job != UNTAGGED_PUSH:
            assert "image-test" in transitive_needs(release, job), f"{job} before image-test"
    tag = release["image"]
    assert {"image-test", "publish"} <= transitive_needs(release, "image"), needs(tag)
    assert "TAGS: ${{ needs.verify.outputs.image-tags }}" in tag, tag
    assert 'docker buildx imagetools create "${refs[@]}" "$IMAGE@$DIGEST"' in tag, tag
    assert '[ "$got" = "$DIGEST" ]' in tag, "the tags must point at the tested digest"
    attested = re.findall(r"subject-name: \$\{\{ needs\.image-build\.outputs\.(\S+) \}\}", tag)
    assert attested == ["image"], attested
    assert tag.count("actions/attest-build-provenance@") == 1, tag
    print("the pushed digest is tested on amd64, arm64 and arm/v7, then tagged unchanged  OK")


def test_image_names():
    """The image is ghcr.io/<owner>/ser2netweb and image-refresh.yml refreshes that one. Its
    name up to 2.9.x, ghcr.io/<owner>/ser2net, stopped at 3.0.0: no workflow names it, so
    nothing is pushed, tagged or attested there any more."""
    verify = jobs(read("release.yml"))["verify"]
    assert 'owner=${GITHUB_REPOSITORY_OWNER,,}' in verify, verify
    assert 'echo "image=ghcr.io/$owner/ser2netweb" >> "$GITHUB_OUTPUT"' in verify, verify
    build = jobs(read("release.yml"))["image-build"]
    assert "org.opencontainers.image.title=ser2netweb\n" in build, build
    assert ("org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
            in build), "the new package links to the repository by this label"
    check = jobs(read("image-refresh.yml"))["check"]
    assert "\n          image=ghcr.io/$owner/ser2netweb\n" in check, check
    for name in names():
        text = read(name)
        assert "${GITHUB_REPOSITORY_OWNER,,}/ser2net" not in text, name
        # the old name only in comments (that it stopped), never in a step
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            assert not re.search(r"/ser2net(?!web)\b", code), (name, line)
            assert not re.search(r"(?i)old[-_](image|ref)", code), (name, line)
    print("image ghcr.io/<owner>/ser2netweb in release.yml and image-refresh.yml alike, "
          "nothing under ghcr.io/<owner>/ser2net  OK")


def test_privileged_jobs_run_only_github_actions():
    release = jobs(read("release.yml"))
    privileged = {job: block for job, block in release.items()
                  if {"contents", "id-token", "attestations"}
                  & {s for s, level in permissions(block).items() if level == "write"}
                  and job != "pypi"}  # (test_pypi_published_last)
    assert set(privileged) == {"attest", "publish", "image"}, sorted(privileged)
    for job, block in privileged.items():
        for _line, ref, _rest in uses(block):
            assert ref.startswith("actions/"), f"{job} runs {ref}"
    attest, publish = release["attest"], release["publish"]
    assert permissions(attest).get("contents") == "read", permissions(attest)
    assert "actions/attest-build-provenance@" in attest and "actions/attest-sbom@" in attest
    assert "name: release-files" in attest, "attest hands the files on"
    assert permissions(publish) == {"contents": "write"}, permissions(publish)
    assert needs(publish) == ["verify", "attest"], needs(publish)
    assert "actions/checkout" not in publish, "no repository code runs where releases are written"
    assert "name: release-files" in publish, publish
    assert 'gh release create "$GITHUB_REF_NAME" out/* --verify-tag' in publish, publish
    assert "PRERELEASE: ${{ needs.verify.outputs.prerelease }}" in publish, publish
    assert "flags+=(--prerelease)" in publish, publish
    for name in names():
        assert "action-gh-release" not in read(name), name
    print("attest / publish / image run GitHub's own actions; gh uploads the attested files  OK")


def test_dry_run_publishes_nothing():
    text = read("release.yml")
    assert "\n  workflow_dispatch:\n" in text.split("\npermissions:", 1)[0]
    release = jobs(text)
    push_only = "github.event_name == 'push'"
    for job in ("publish", "image", "pypi", "docker-hub"):
        assert key(release[job], "if") == push_only, f"{job} must not run in a dry run"
    attestations = [s for s in steps(release["attest"]) if "uses: actions/attest-" in s]
    assert len(attestations) == 4, attestations
    for step in attestations:
        assert f"\n        if: {push_only}\n" in step, step
    assert "      publish: ${{ github.event_name == 'push' }}" in release["ci"], "dry runs don't sign"
    verify = release["verify"]
    assert 'else  # a dry run: the tag the current version gets\n            python3 ' \
           'tools/release_guard.py tag\n' in verify, verify
    group = re.search(r"(?m)^  group: (.*)$", text.split("\nconcurrency:\n", 1)[1]).group(1)
    assert group == ("${{ github.event_name == 'push' && 'release' || "
                     "format('release-dry-run-{0}', github.run_id) }}"), group
    print("a dry run checks, builds and tests, but attests, releases and tags nothing  OK")


def test_release_files_named():
    attest = jobs(read("release.yml"))["attest"]
    name_step = next(st for st in steps(attest) if "Name the release assets" in st)
    made = set(re.findall(r'(?m)^\s+cp \S+ "?out/([^"\s]+)"?$', name_step))
    made = {m.replace("$arch", arch) for m in made for arch in ("x64", "arm64")}
    # the packages: as standalone.yml's packages job names them (its artifact, whole)
    assert "\n          cp artifacts/packages/* out/\n" in name_step, name_step
    made |= set(release_guard.release_packages())
    download = next(st for st in steps(attest) if "name: packages-linux" in st)
    assert "path: artifacts/packages\n" in download, download
    assert "name: packages-linux\n" in jobs(read("standalone.yml"))["packages"]
    want = set(release_guard.release_assets())
    assert made | {"SHA256SUMS"} == want, sorted(made ^ want)
    assert "sha256sum * > SHA256SUMS" in name_step, name_step
    order = [st for st in steps(attest)]
    guard = next(i for i, st in enumerate(order) if "release_guard.py assets out" in st)
    sboms = next(i for i, st in enumerate(order)
                 if "release_guard.py sboms out/ser2netweb-*.cdx.json" in st)
    first_attest = next(i for i, st in enumerate(order) if "uses: actions/attest-" in st)
    assert order.index(name_step) < guard < first_attest and sboms < first_attest
    provenance = next(st for st in order if "actions/attest-build-provenance@" in st)
    for exe in release_guard.RELEASE_EXECUTABLES:
        assert f"            out/{exe}\n" in provenance, exe
    for pattern in ("out/ser2netweb_*.deb", "out/ser2netweb-*.rpm"):
        assert f"            {pattern}\n" in provenance, pattern
        stem = exe.removesuffix(".exe")
        assert any(f"subject-path: out/{exe}\n          sbom-path: out/{stem}.cdx.json\n" in st
                   for st in order), f"no SBOM attestation for {exe}"
    print(f"the release files are release_guard's {len(want)} names, checked before they are "
          "attested; every executable has both attestations, every package its provenance  OK")


def test_pypi_published_last():
    release = jobs(read("release.yml"))
    pypi = release["pypi"]
    assert permissions(pypi) == {"id-token": "write"}, permissions(pypi)
    assert "publish" in needs(pypi), "after the GitHub release"
    assert "pypi" not in {n for job in release for n in needs(release[job])}, \
        "nothing waits for PyPI: a missing trusted publisher can't hold the release up"
    assert "\n    environment:\n      name: pypi\n" in pypi, pypi
    assert "actions/checkout" not in pypi, "no repository code runs with the id-token"
    refs = [ref for _line, ref, _rest in uses(pypi)]
    assert [r.split("@")[0] for r in refs] == ["actions/download-artifact",
                                               "pypa/gh-action-pypi-publish"], refs
    download = steps(pypi)[0]
    assert "name: pypi-dist\n" in download and "path: dist\n" in download, download
    # the artifact: built and checked by ci.yml, which the release ran on the tagged commit
    ci = jobs(read("ci.yml"))
    build = ci["pypi-build"]
    assert "name: pypi-dist\n" in build and "twine check --strict pypi-dist/*" in build
    assert "pip install --require-hashes --only-binary=:all: -r requirements-build.txt" in build
    assert "python -m build --no-isolation --outdir pypi-dist" in build
    assert "pypi-build" in needs(ci["pypi-install"])
    print("PyPI: after the release, the sdist and wheel CI built and tested, trusted "
          "publishing (id-token only, environment pypi), nothing waits for it  OK")


def test_docker_hub_copy():
    release = jobs(read("release.yml"))
    hub = release["docker-hub"]
    assert key(hub, "uses") == "./.github/workflows/docker-hub.yml", hub
    assert "image" in needs(hub), "after the GHCR tags"
    assert permissions(hub) == {"contents": "read", "packages": "read"}, permissions(hub)
    assert "docker-hub" not in {n for job in release for n in needs(release[job])}, \
        "nothing waits for Docker Hub"
    assert "      digest: ${{ needs.image-build.outputs.digest }}\n" in hub, "the tested digest"
    assert "      tags: ${{ needs.verify.outputs.image-tags }}\n" in hub, "the release's tags"
    passed = re.findall(r"(?m)^      (\w+): \$\{\{ secrets\.(\w+) \}\}$", hub)
    assert passed == [("DOCKERHUB_USERNAME", "DOCKERHUB_USERNAME"),
                      ("DOCKERHUB_TOKEN", "DOCKERHUB_TOKEN")], passed
    assert "secrets: inherit" not in hub
    refresh = jobs(read("image-refresh.yml"))["docker-hub"]
    assert key(refresh, "uses") == "./.github/workflows/docker-hub.yml"
    assert "tag" in needs(refresh) and "      digest: ${{ needs.build.outputs.digest }}\n" in refresh
    assert "      tags: ${{ needs.check.outputs.image-tags }}\n" in refresh, "X.Y and latest"
    assert key(refresh, "if") is None, "every refresh is copied"

    text = read("docker-hub.yml")
    on = text.split("\npermissions:", 1)[0]
    assert "\n  workflow_call:\n" in on and "\n  workflow_dispatch:\n" in on, on
    assert re.search(r"(?m)^      tag:\n        description: .*\n        required: true", on)
    mirror = jobs(text)["mirror"]
    assert permissions(mirror) == {"contents": "read", "packages": "read"}, permissions(mirror)
    assert not uses(text), "only the runner's docker CLI"
    st = steps(mirror)
    assert '[ -z "$HUB_USER" ] || [ -z "$HUB_TOKEN" ]' in st[0] and "::notice::" in st[0]
    assert all("        if: steps.hub.outputs.copy == 'true'\n" in x for x in st[1:]), st
    assert "source=ghcr.io/$owner/ser2netweb" in mirror
    assert "namespace=${HUB_NAMESPACE:-$owner}" in mirror
    assert 'docker buildx imagetools create "${refs[@]}" "$SOURCE@$DIGEST"' in mirror
    assert '[ "$got" = "$DIGEST" ]' in mirror, "each tag checked to be the digest"
    assert "--password-stdin" in mirror and "build-push-action" not in text
    print("Docker Hub: after the GHCR tags, the same digest and tags (imagetools, checked), "
          "two secrets only, skipped without them; image-refresh.yml and by hand too  OK")


if __name__ == "__main__":
    test_actions_pinned_by_sha()
    test_tag_checked_first()
    test_release_runs_the_whole_ci()
    test_one_release_at_a_time()
    test_image_published_as_tested()
    test_privileged_jobs_run_only_github_actions()
    test_dry_run_publishes_nothing()
    test_release_files_named()
    test_image_names()
    test_pypi_published_last()
    test_docker_hub_copy()
    print("\nPASS: release pipeline")
