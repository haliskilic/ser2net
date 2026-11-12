"""The published image is scanned weekly and rebuilt when its base image has moved.

* the Dockerfile's base is a codename tag (python:3.12-slim-<codename>), so a rebuild only
  picks up Debian point releases and 3.12.x patches; release.yml builds FROM that tag
  resolved to a digest and records it in the image's labels;
* .github/workflows/image-refresh.yml (Mondays + by hand) scans :latest with a pinned,
  sha256-checked Grype and uploads fixed HIGH/CRITICAL findings to code scanning, in a job
  that can't push; when the recorded base digest differs from the current one it rebuilds
  the latest release from its tag for the platforms :latest has (2.9.0 added linux/arm/v7,
  which an older tag can't build), pushes it by digest, smoke-tests that digest on each of
  them (arm/v7 under QEMU), and only then attests it and moves X.Y and latest (never X.Y.Z)
  to it.

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


def test_dockerfile_arm_v7():
    """linux/arm/v7 builds the two packages PyPI has no wheel of from their hash-locked
    sources (with setuptools from requirements-build.txt); every other platform installs
    the locked wheels only, as before."""
    with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
        docker = fh.read()
    run = docker.split("COPY requirements-lock.txt requirements-build.txt ./\nRUN ", 1)[1]
    run = run.split("\n\n", 1)[0]
    arm, other = run.split('if [ "$TARGETARCH" = arm ]; then', 1)[1].split("else", 1)
    assert "--require-hashes --only-binary=:all: \\\n            --no-binary=cffi,psutil " \
           "--no-build-isolation -r requirements-lock.txt" in arm, arm
    assert "-r /tmp/setuptools.txt" in arm and "pip uninstall -y setuptools" in arm, arm
    assert "apt-get purge -y --auto-remove gcc" in arm, "the compiler leaves the layer"
    assert "pip install --no-cache-dir --require-hashes --only-binary=:all: -r " \
           "requirements-lock.txt; \\\n    fi" in other, other
    assert "ARG TARGETARCH\n" in docker
    # what its sed takes from requirements-build.txt: the setuptools pin with its hashes
    with open(os.path.join(ROOT, "requirements-build.txt"), encoding="utf-8") as fh:
        build = fh.read()
    assert "sed -n '/^setuptools==/,/[^\\\\]$/p' requirements-build.txt" in arm, arm
    block = re.search(r"(?m)^setuptools==\S+ \\\n(?:.*\\\n)*.*[^\\]$", build)
    assert block and block.group(0).count("--hash=sha256:") >= 1, "no hashed setuptools pin"
    with open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8") as fh:
        assert "\n!requirements-build.txt\n" in fh.read()
    with open(os.path.join(ROOT, "requirements-lock.txt"), encoding="utf-8") as fh:
        lock = fh.read()
    for name in ("cffi", "psutil"):
        assert re.search(rf"(?m)^{name}==", lock), name
    print("Dockerfile: arm/v7 builds cffi and psutil from the locked sources, the rest "
          "installs the locked wheels  OK")


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
    assert "platforms: ${{ steps.base.outputs.platforms }}" in check
    assert 'docker buildx imagetools inspect "$image:latest" --format \'{{json .Manifest}}\'' \
        in check and 'select(.os != "unknown")' in check, "the published platforms"

    build = wf["build"]
    assert "if: needs.check.outputs.rebuild == 'true'" in build
    assert permissions(build) == {"contents": "read", "packages": "write"}, permissions(build)
    assert "ref: ${{ needs.check.outputs.tag }}" in build, "rebuilt from the release tag"
    assert "push-by-digest=true" in build and "\n          tags: ${{" not in build
    assert "build-args: BASE_IMAGE=${{ needs.check.outputs.base }}@${{ needs.check.outputs.base-digest }}" in build
    assert "platforms: ${{ needs.check.outputs.platforms }}" in build, "what :latest has"

    test = wf["test"]
    assert "write" not in permissions(test).values()
    assert "{ arch: amd64, runner: ubuntu-latest, platform: linux/amd64 }" in test
    assert "{ arch: arm64, runner: ubuntu-24.04-arm, platform: linux/arm64 }" in test
    assert "{ arch: arm/v7, runner: ubuntu-latest, platform: linux/arm/v7, qemu: true }" in test
    assert 'tests/docker_smoke.sh "$REF"' in test and "ref: ${{ needs.check.outputs.tag }}" in test
    # a leg runs exactly when the rebuild has its platform
    assert ("BUILT: ${{ contains(format(',{0},', needs.check.outputs.platforms), "
            "format(',{0},', matrix.platform)) }}") in test
    for step in steps(test):
        assert "if: env.BUILT == 'true'" in step, step

    tag = wf["tag"]
    assert {"build", "test"} <= transitive_needs(wf, "tag"), needs(tag)
    assert "actions/attest-build-provenance@" in tag
    assert '[ "$got" = "$DIGEST" ]' in tag, "the tags must point at the tested digest"
    for job, block in wf.items():
        if job != "build" and "packages" in permissions(block):
            assert job == "tag" or permissions(block)["packages"] == "read", job
    print("image-refresh.yml: scan (report only), rebuild from the tag when the base moved, "
          "test on each published platform, then move X.Y / latest  OK")


if __name__ == "__main__":
    test_dockerfile_base()
    test_dockerfile_arm_v7()
    test_release_records_the_base()
    test_refresh_workflow()
    print("\nPASS: image refresh")
