"""The APT and DNF / zypper repositories on GitHub Pages: tools/package_repo.py and
.github/workflows/package-repo.yml, without the network, gpg or a package manager.

* which releases the repositories hold: the newest three published ones with packages, by
  version (no drafts, pre-releases or other tags), or up to a given tag; each file checked
  against its release's SHA256SUMS and the digest GitHub lists;
* the files users get: ser2netweb.sources (deb822, Signed-By the dearmored keyring in
  /usr/share/keyrings), ser2netweb.repo (repo_gpgcheck=1, gpgcheck=0 with its reason);
  the committed key packaging/ser2netweb-archive-keyring.asc is the documented one (its
  fingerprint and expiry, read from its packets);
* the commands: docs/PACKAGES.md, both READMEs and index.html give the tool's commands
  (the ones tests/package_repo_smoke.sh runs in containers), with the fingerprint;
* `verify` finds a Packages, Release, repomd.xml or primary that doesn't match its files
  (on small trees made here), reads InRelease's signed text and an armored key;
* the workflow: its triggers, least privilege per job, the signing secrets in the signing
  step only and never in a pull request's run, the deployment only after the container
  tests and never from a pull request; release.yml starts it once a release is out.

Run: python3 tests/test_package_repo.py   (stdlib only)
"""
import base64
import datetime
import gzip
import hashlib
import os
import re
import sys
import tempfile

from _workflows import ROOT, jobs, key, needs, permissions, read, steps

sys.path.insert(0, os.path.join(ROOT, "tools"))

import package_repo as pr


def text(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def raises(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except SystemExit as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} didn't fail")


def release(tag, *, draft=False, prerelease=False, packages=True, version=None):
    v = version or tag.removeprefix("v")
    names = [f"ser2netweb_{v}_amd64.deb", f"ser2netweb_{v}_arm64.deb",
             f"ser2netweb-{v}-1.x86_64.rpm", f"ser2netweb-{v}-1.aarch64.rpm"] if packages else []
    assets = [{"name": n, "browser_download_url": f"https://example.invalid/{n}"}
              for n in [*names, "SHA256SUMS", "ser2netweb-linux-x64"]]
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": assets}


def test_select():
    releases = [release("v3.0.1"), release("v3.1.0-rc.1", prerelease=True), release("v2.9.0",
                packages=False), release("v3.2.0", draft=True), release("v3.0.0"),
                release("v3.1.0"), release("v3.0.2"), release("nightly"),
                release("v3.0.3", version="3.0.2")]  # (its files are another version's)
    got = [r["tag_name"] for r in pr.select(releases)]
    assert got == ["v3.1.0", "v3.0.2", "v3.0.1"], got
    assert [r["tag_name"] for r in pr.select(releases, "v3.0.1")] == ["v3.0.1", "v3.0.0"]
    assert [r["tag_name"] for r in pr.select(releases, keep=1)] == ["v3.1.0"]
    for tag in ("v3.1.0-rc.1", "v2.9.0", "v3.2.0", "v9.9.9", "v3.0.3"):
        assert "is not a published release" in raises(pr.select, releases, tag), tag
    assert "no published release has packages" in raises(pr.select, [release("v2.9.0",
                                                                             packages=False)])
    names = [a["name"] for a in pr.package_assets(release("v3.0.0"))]
    assert sorted(names) == ["ser2netweb-3.0.0-1.aarch64.rpm", "ser2netweb-3.0.0-1.x86_64.rpm",
                             "ser2netweb_3.0.0_amd64.deb", "ser2netweb_3.0.0_arm64.deb"], names
    print("the newest three published releases with packages, by version (or up to a tag)  OK")


def test_checksums():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ser2netweb_3.0.0_amd64.deb")
        with open(path, "wb") as fh:
            fh.write(b"a package")
        digest = hashlib.sha256(b"a package").hexdigest()
        sums = pr.read_sums(f"{digest}  ser2netweb_3.0.0_amd64.deb\n{'0' * 64} *other.rpm\n"
                            "not a line\n")
        assert sums == {"ser2netweb_3.0.0_amd64.deb": digest, "other.rpm": "0" * 64}, sums
        assert pr.check_file(path, sums, f"sha256:{digest}") == digest
        assert "GitHub lists" in raises(pr.check_file, path, sums, "sha256:" + "1" * 64)
        assert "SHA256SUMS says" in raises(pr.check_file, path,
                                           {"ser2netweb_3.0.0_amd64.deb": "2" * 64})
        assert "not in its release's SHA256SUMS" in raises(pr.check_file, path, {})
    print("every file is checked against SHA256SUMS and GitHub's digest  OK")


def _expiry(data: bytes) -> datetime.date:
    """The primary key's expiry: its creation time plus the key expiration time of its
    self-signature (RFC 4880 5.2.3.6)."""
    pkts = pr.packets(data)
    created = int.from_bytes(pkts[0][1][1:5], "big")
    for tag, body in pkts:
        if tag == 2 and body[0] == 4 and body[1] == 0x13:
            hashed = body[6:6 + int.from_bytes(body[4:6], "big")]
            i = 0
            while i < len(hashed):
                n = hashed[i]
                if n < 192:
                    size, i = n, i + 1
                elif n < 255:
                    size, i = ((n - 192) << 8) + hashed[i + 1] + 192, i + 2
                else:
                    size, i = int.from_bytes(hashed[i + 1:i + 5], "big"), i + 5
                if hashed[i] & 0x7F == 9:
                    seconds = int.from_bytes(hashed[i + 1:i + 5], "big")
                    when = datetime.datetime.fromtimestamp(created + seconds, datetime.timezone.utc)
                    return when.date()
                i += size
    raise AssertionError("the key doesn't expire")


def test_committed_key():
    asc = text(os.path.join("packaging", "ser2netweb-archive-keyring.asc"))
    assert asc.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----\n"), "a public key"
    assert "PRIVATE" not in asc
    data = pr.dearmor(asc)
    assert pr.fingerprints(data) == [pr.FINGERPRINT], pr.fingerprints(data)
    tags = [tag for tag, _body in pr.packets(data)]
    assert tags == [6, 13, 2], f"one key, one user ID, its self-signature: {tags}"
    key_body = pr.packets(data)[0][1]
    assert key_body[5] == 1 and int.from_bytes(key_body[6:8], "big") == 4096, "RSA 4096"
    assert str(_expiry(data)) == pr.KEY_EXPIRES, (_expiry(data), pr.KEY_EXPIRES)
    # a damaged armor is refused
    lines = asc.splitlines()
    crc = next(i for i, line in enumerate(lines) if line.startswith("="))
    lines[crc] = "=" + base64.b64encode(b"\0\0\0").decode()
    assert "checksum" in raises(pr.dearmor, "\n".join(lines))
    assert pr.spaced(pr.FINGERPRINT) == "B587 60E7 C0B0 515E 81D2  50F9 8164 D694 711B B799"
    print(f"packaging/{pr.KEYRING}.asc is {pr.FINGERPRINT}, RSA 4096, until {pr.KEY_EXPIRES}  OK")


def test_repository_files():
    sources = pr.SOURCES.format(base=pr.SITE_URL)
    (para,) = pr.deb822(sources)
    assert para == {"Types": "deb", "URIs": f"{pr.SITE_URL}/deb", "Suites": "stable",
                    "Components": "main", "Architectures": "amd64 arm64",
                    "Signed-By": "/usr/share/keyrings/ser2netweb-archive-keyring.gpg"}, para
    repo = pr.REPO_FILE.format(base=pr.SITE_URL)
    lines = repo.splitlines()
    want = {"[ser2netweb]", "name=ser2netweb", f"baseurl={pr.SITE_URL}/rpm/$basearch",
            "type=rpm-md", "enabled=1", "repo_gpgcheck=1", "gpgcheck=0",
            f"gpgkey={pr.SITE_URL}/ser2netweb-archive-keyring.asc"}
    assert want == {line for line in lines if not line.startswith("#")}, lines
    reason = repo.split("[ser2netweb]", 1)[1].split("repo_gpgcheck=1", 1)[0]
    assert "aren't signed" in reason and "SHA-256 of every package" in reason, reason
    print("ser2netweb.sources pins the keyring; ser2netweb.repo checks the signed metadata  OK")


def _blocks(markdown: str) -> list[list[str]]:
    return [b.strip("\n").splitlines() for b in re.findall(r"```(?:bash|sh)?\n(.*?)```",
                                                          markdown, re.S)]


def test_documented_commands():
    doc = text(os.path.join("docs", "PACKAGES.md"))
    blocks = _blocks(doc)
    for family in pr.COMMANDS:
        for part in ("install", "update", "remove"):
            want = pr.commands(family, part)
            assert want in blocks, f"docs/PACKAGES.md has no block of {family} {part}: {want}"
    assert pr.FINGERPRINT in doc and pr.spaced(pr.FINGERPRINT) in doc
    assert pr.KEY_EXPIRES in doc
    for readme in ("README.md", "README.en.md"):
        body = text(readme)
        for family in pr.COMMANDS:
            for line in pr.commands(family):
                assert line in body, f"{readme} doesn't give `{line}`"
        assert pr.spaced(pr.FINGERPRINT) in body, readme
        assert "](docs/PACKAGES.md" in body, readme
    page = pr.index_html([{"version": "3.0.1"}, {"version": "3.0.0"}])
    for family in pr.COMMANDS:
        for part in ("install", "update", "remove"):
            for line in pr.commands(family, part):
                assert line.replace("&", "&amp;") in page, line
    assert pr.spaced(pr.FINGERPRINT) in page and "3.0.1, 3.0.0" in page
    assert "<script" not in page and "http://" not in page, "a static page, https links"
    batch = pr.commands("zypper", base="http://127.0.0.1:1/good", batch=True, root=True)
    assert batch[1] == "zypper --non-interactive addrepo http://127.0.0.1:1/good/ser2netweb.repo"
    assert pr.commands("apt", batch=True)[-1] == "sudo apt install -y ser2netweb"
    print("docs/PACKAGES.md, both READMEs and index.html give the tested commands  OK")


# --- verify on small trees -------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _deb_tree(deb: str) -> None:
    dists = os.path.join(deb, "dists", "stable")
    release = ["Suite: stable", "Components: main", "Architectures: amd64 arm64",
               "Acquire-By-Hash: yes", "SHA256:"]
    for arch in pr.DEB_ARCHES:
        name = f"ser2netweb_3.0.0_{arch}.deb"
        package = f"a {arch} package".encode()
        _put(os.path.join(deb, *pr.POOL.split("/"), name), package)
        stanza = (f"Package: ser2netweb\nArchitecture: {arch}\nVersion: 3.0.0\n"
                  f"Filename: {pr.POOL}/{name}\nSize: {len(package)}\nSHA256: {_sha(package)}\n"
                  "Description: x\n more\n").encode()
        folder = os.path.join(dists, "main", f"binary-{arch}")
        for index, data in (("Packages", stanza), ("Packages.gz", gzip.compress(stanza))):
            _put(os.path.join(folder, index), data)
            _put(os.path.join(folder, "by-hash", "SHA256", _sha(data)), data)
            release.append(f" {_sha(data)} {len(data)} main/binary-{arch}/{index}")
    _put(os.path.join(dists, "Release"), ("\n".join(release) + "\n").encode())


def _rpm_tree(folder: str) -> None:
    rpm = b"an rpm"
    _put(os.path.join(folder, "ser2netweb-3.0.0-1.x86_64.rpm"), rpm)
    primary = gzip.compress(
        f'<metadata xmlns="http://linux.duke.edu/metadata/common" packages="1"><package>'
        f'<checksum type="sha256" pkgid="YES">{_sha(rpm)}</checksum>'
        f'<size package="{len(rpm)}"/><location href="ser2netweb-3.0.0-1.x86_64.rpm"/>'
        f'</package></metadata>'.encode())
    name = f"repodata/{_sha(primary)}-primary.xml.gz"
    _put(os.path.join(folder, *name.split("/")), primary)
    _put(os.path.join(folder, "repodata", "repomd.xml"),
         f'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary">'
         f'<checksum type="sha256">{_sha(primary)}</checksum><location href="{name}"/>'
         f'</data></repomd>'.encode())


def test_verify_finds_mismatches():
    with tempfile.TemporaryDirectory() as tmp:
        deb = os.path.join(tmp, "deb")
        _deb_tree(deb)
        assert pr.check_release(deb) == [], pr.check_release(deb)
        packages = os.path.join(deb, "dists", "stable", "main", "binary-arm64", "Packages")
        with open(packages, "rb") as fh:
            data = fh.read()
        _put(packages, data.replace(b"Version: 3.0.0", b"Version: 3.0.1"))
        problems = pr.check_release(deb)
        assert "main/binary-arm64/Packages doesn't match Release" in problems, problems
        assert any("Version 3.0.1" in p for p in problems), problems
        _put(packages, data)
        _put(os.path.join(deb, *pr.POOL.split("/"), "ser2netweb_3.0.0_amd64.deb"), b"changed")
        assert pr.check_release(deb) == [f"{pr.POOL}/ser2netweb_3.0.0_amd64.deb doesn't match "
                                         "binary-amd64/Packages"], pr.check_release(deb)
        _put(os.path.join(deb, *pr.POOL.split("/"), "ser2netweb_3.0.1_amd64.deb"), b"extra")
        assert any("the pool has" in p for p in pr.check_release(deb))

        rpm = os.path.join(tmp, "rpm", "x86_64")
        _rpm_tree(rpm)
        assert pr.check_repodata(rpm) == [], pr.check_repodata(rpm)
        _put(os.path.join(rpm, "ser2netweb-3.0.0-1.x86_64.rpm"), b"another rpm")
        assert pr.check_repodata(rpm) == ["ser2netweb-3.0.0-1.x86_64.rpm doesn't match "
                                          "primary"], pr.check_repodata(rpm)
        repomd = os.path.join(rpm, "repodata", "repomd.xml")
        with open(repomd, encoding="utf-8") as fh:
            body = fh.read()
        _put(repomd, re.sub(r"[0-9a-f]{64}</checksum>", "0" * 64 + "</checksum>",
                            body).encode())
        assert any("doesn't match repomd.xml" in p for p in pr.check_repodata(rpm))
    signed = ("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\nOrigin: x\n- -dashed\n"
              "SHA256:\n a 1 b\n-----BEGIN PGP SIGNATURE-----\n\nxx\n-----END PGP SIGNATURE-----\n")
    assert pr.cleartext(signed) == "Origin: x\n-dashed\nSHA256:\n a 1 b\n"
    assert "not a cleartext signature" in raises(pr.cleartext, "Origin: x\n")
    print("verify finds a changed Packages, package, repomd.xml or RPM; reads InRelease  OK")


# --- the workflows ---------------------------------------------------------------------------

def test_workflow():
    wf = read("package-repo.yml")
    on = wf.split("\npermissions:", 1)[0]
    assert "\n  release:\n    types: [published]\n" in on, on
    assert "\n  workflow_dispatch:\n    inputs:\n      tag:\n" in on, on
    paths = on.split("\n  pull_request:\n    paths:\n", 1)[1]
    for path in (".github/workflows/package-repo.yml", "packaging/**", "tools/package_repo.py",
                 "tests/package_repo_smoke.sh"):
        assert f"      - {path}\n" in paths, path
    assert "pull_request_target" not in wf and "workflow_run" not in wf
    assert "\npermissions:\n  contents: read\n" in wf
    all_jobs = jobs(wf)
    assert set(all_jobs) == {"build", "test", "deploy", "live"}, sorted(all_jobs)
    build, test, deploy, live = (all_jobs[j] for j in ("build", "test", "deploy", "live"))
    assert key(build, "if") == "github.event_name != 'release' || !github.event.release.prerelease"
    assert permissions(build) == {"contents": "read", "attestations": "read"}, permissions(build)
    assert permissions(test) == {} and permissions(live) == {}, "the workflow's contents: read"
    assert permissions(deploy) == {"pages": "write", "id-token": "write"}, permissions(deploy)
    writers = [j for j, b in all_jobs.items() if "write" in permissions(b).values()]
    assert writers == ["deploy"], writers
    # the secrets: in the signing step only, and empty in a pull request's run
    secret_steps = [s for s in steps(build) if "secrets." in s]
    assert len(secret_steps) == 1 and "id: sign" in secret_steps[0], secret_steps
    sign = secret_steps[0]
    for name in ("PACKAGES_GPG_PRIVATE_KEY", "PACKAGES_GPG_PASSPHRASE", "PACKAGES_GPG_FINGERPRINT"):
        assert (f"{name}: ${{{{ github.event_name != 'pull_request' && secrets.{name} || '' }}}}"
                in sign), name
    assert wf.count("secrets.") == 3, "no other secret anywhere"
    assert 'if [ "$EVENT" = pull_request ]; then' in sign
    assert sign.count("python3 tools/package_repo.py sign site --test-key") == 2, sign
    assert "--keyring packaging/ser2netweb-archive-keyring.asc" in sign
    assert sign.count('echo "deploy=false" >> "$GITHUB_OUTPUT"') == 2
    assert sign.count('echo "deploy=true" >> "$GITHUB_OUTPUT"') == 1
    assert "::notice::The secret PACKAGES_GPG_PRIVATE_KEY is not set" in sign
    order = steps(build)
    idx = {name: next(i for i, s in enumerate(order) if marker in s) for name, marker in (
        ("fetch", "package_repo.py fetch packages"), ("provenance", "gh attestation verify"),
        ("build", "package_repo.py build packages site"), ("sign", "id: sign"),
        ("verify", "package_repo.py verify site"), ("upload", "name: package-repo"),
        ("pages", "actions/upload-pages-artifact@"))}
    assert list(idx.values()) == sorted(idx.values()), idx
    assert "if: steps.sign.outputs.deploy == 'true'" in order[idx["pages"]]
    assert '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release.yml"' in build
    assert "      deploy: ${{ steps.sign.outputs.deploy }}" in build
    # every tree is tested in the containers before it is deployed, never from a PR
    assert needs(test) == ["build"] and needs(deploy) == ["build", "test"], needs(deploy)
    assert key(deploy, "if") == ("github.event_name != 'pull_request' && "
                                 "needs.build.outputs.deploy == 'true'"), key(deploy, "if")
    assert "\n    environment:\n      name: github-pages\n" in deploy
    assert [s for s in steps(deploy)] and "actions/checkout" not in deploy
    assert "actions/deploy-pages@" in deploy
    images = re.search(r'image: \[([^\]]*)\]', test).group(1)
    for image in ("debian:12", "ubuntu:24.04", "fedora:latest", "almalinux:9", "opensuse/leap:15.6"):
        assert f'"{image}"' in images, image
    assert 'run: sh tests/package_repo_smoke.sh site "$IMAGE" "$VERSION"' in test
    assert needs(live) == ["build", "deploy"]
    assert 'run: sh tests/package_repo_smoke.sh --live site "$IMAGE" "$VERSION"' in live
    for block in all_jobs.values():
        if "actions/checkout@" in block:
            assert "persist-credentials: false" in block
        for step in steps(block):
            run = step.split("        run: ", 1)[1] if "        run: " in step else ""
            assert "${{" not in run, f"an expression in run: {step}"
    concurrency = wf.split("\nconcurrency:\n", 1)[1].split("\n\n", 1)[0]
    assert "|| 'package-repo' }}" in concurrency
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in concurrency
    print("package-repo.yml: secrets in the signing step only (never a PR's), pages write in the "
          "deploy job only, after the container tests  OK")


def test_release_starts_it():
    release = jobs(read("release.yml"))
    channels = release["channels"]
    assert needs(channels) == ["verify", "publish"], needs(channels)
    assert key(channels, "if") == ("github.event_name == 'push' && "
                                   "needs.verify.outputs.prerelease != 'true'"), channels
    assert permissions(channels) == {"actions": "write"}, permissions(channels)
    assert "uses:" not in channels and "actions/checkout" not in channels
    assert "for workflow in package-repo.yml; do" in channels
    assert 'gh workflow run "$workflow" --ref main' in channels
    assert "channels" not in {n for job in release for n in needs(release[job])}
    # what it starts: on main, which the github-pages environment allows
    assert "\n  workflow_dispatch:\n" in read("package-repo.yml").split("\npermissions:", 1)[0]
    smoke = text(os.path.join("tests", "package_repo_smoke.sh"))
    assert 'python3 tools/package_repo.py commands "$family"' in smoke
    assert "InRelease" in smoke and "repomd.xml" in smoke and "--network host" in smoke
    print("release.yml starts package-repo.yml on main once a release (not a pre-release) is out  OK")


if __name__ == "__main__":
    test_select()
    test_checksums()
    test_committed_key()
    test_repository_files()
    test_documented_commands()
    test_verify_finds_mismatches()
    test_workflow()
    test_release_starts_it()
    print("\nPASS: package repositories")
