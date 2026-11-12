"""The published image is scanned weekly and rebuilt when its base image has moved.

* the Dockerfile's base is a codename tag (python:3.12-slim-<codename>), so a rebuild only
  picks up Debian point releases and 3.12.x patches; release.yml builds FROM that tag
  resolved to a digest and records it in the image's labels;
* .github/workflows/image-refresh.yml (Mondays + by hand) scans :latest with a pinned,
  sha256-checked Grype and uploads fixed HIGH/CRITICAL findings to code scanning, in a job
  that can't push; when the recorded base digest differs from the current one it rebuilds
  the latest release from its tag, pushes it by digest, smoke-tests that digest on amd64 and
  arm64, and only then attests it and moves X.Y and latest (never X.Y.Z) to it.

Run: python3 tests/test_image_refresh.py   (stdlib only)
"""
import os
import re

from _workflows import ROOT, jobs, needs, permissions, read, steps, transitive_needs


def test_dockerfile_base():
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
        docker = fh.read()
    m = re.search(r"(?m)^ARG BASE_IMAGE=python:3\.12-slim-([a-z]+)$", docker)
    assert m, "the base must be a codename tag (python:3.12-slim-<codename>)"
    froms = re.findall(r"(?m)^FROM (.*)$", docker)
    assert froms == ["${BASE_IMAGE}"], froms
    print(f"Dockerfile: FROM python:3.12-slim-{m.group(1)} (overridable by digest)  OK")


def test_release_records_the_base():
    build = jobs(read("release.yml"))["image-build"]
    base = next(s for s in steps(build) if s.startswith("      - id: base"))
    assert "sed -n 's/^ARG BASE_IMAGE=//p' Dockerfile" in base, base
    assert "imagetools inspect \"$base\" --format '{{json .Manifest}}'" in base, base
    assert "build-args: BASE_IMAGE=${{ steps.base.outputs.image }}@${{ steps.base.outputs.digest }}" in build
    assert "org.opencontainers.image.base.digest=${{ steps.base.outputs.digest }}" in build
    assert "org.opencontainers.image.base.name=${{ steps.base.outputs.name }}" in build
    print("release.yml builds FROM the base digest and records it in the image labels  OK")


def test_refresh_workflow():
    text = read("image-refresh.yml")
    on = text.split("\npermissions:", 1)[0]
    assert "\n  schedule:\n" in on and "\n  workflow_dispatch:\n" in on
    assert "push:" not in on and "pull_request" not in on, on
    block = text.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
    assert "cancel-in-progress: false" in block, block
    wf = jobs(text)

    scan = wf["scan"]
    assert permissions(scan) == {"contents": "read", "packages": "read",
                                 "security-events": "write"}, permissions(scan)
    assert re.search(r'(?m)^      GRYPE_VERSION: "\d+\.\d+\.\d+"$', scan), "Grype pinned"
    assert re.search(r"(?m)^      GRYPE_SHA256: [0-9a-f]{64}$", scan), "Grype's sha256 pinned"
    assert 'echo "${GRYPE_SHA256}  grype.tar.gz" | sha256sum -c -' in scan
    assert "--only-fixed" in scan and "select(.level == \"error\")" in scan, "fixed HIGH/CRITICAL"
    assert "github/codeql-action/upload-sarif@" in scan

    check = wf["check"]
    assert "write" not in permissions(check).values(), permissions(check)
    assert 'org.opencontainers.image.base.digest' in check
    assert 'if [ "$built" != "$now" ] || [ "$FORCE" = true ]; then rebuild=true; fi' in check
    assert 'echo "image-tags=${version%.*} latest"' in check, "X.Y and latest, never X.Y.Z"

    build = wf["build"]
    assert "if: needs.check.outputs.rebuild == 'true'" in build
    assert permissions(build) == {"contents": "read", "packages": "write"}, permissions(build)
    assert "ref: ${{ needs.check.outputs.tag }}" in build, "rebuilt from the release tag"
    assert "push-by-digest=true" in build and "\n          tags: ${{" not in build
    assert "build-args: BASE_IMAGE=${{ needs.check.outputs.base }}@${{ needs.check.outputs.base-digest }}" in build

    test = wf["test"]
    assert "write" not in permissions(test).values()
    assert "{ arch: amd64, runner: ubuntu-latest }" in test
    assert "{ arch: arm64, runner: ubuntu-24.04-arm }" in test
    assert 'tests/docker_smoke.sh "$REF"' in test and "ref: ${{ needs.check.outputs.tag }}" in test

    tag = wf["tag"]
    assert {"build", "test"} <= transitive_needs(wf, "tag"), needs(tag)
    assert "actions/attest-build-provenance@" in tag
    assert '[ "$got" = "$DIGEST" ]' in tag, "the tags must point at the tested digest"
    for job, block in wf.items():
        if job != "build" and "packages" in permissions(block):
            assert job == "tag" or permissions(block)["packages"] == "read", job
    print("image-refresh.yml: scan (report only), rebuild from the tag when the base moved, "
          "test on amd64 + arm64, then move X.Y / latest  OK")


if __name__ == "__main__":
    test_dockerfile_base()
    test_release_records_the_base()
    test_refresh_workflow()
    print("\nPASS: image refresh")
