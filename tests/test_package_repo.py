"""The APT and DNF / zypper repositories on GitHub Pages: tools/package_repo.py and
.github/workflows/package-repo.yml, without the network, gpg or a package manager.

* which releases the repositories hold: the newest three published ones with packages, by
  version (no drafts, pre-releases or other tags), or up to a given tag; each file checked
  against its release's SHA256SUMS and the digest GitHub lists;
* the files users get: ser2netweb.sources (deb822, Signed-By the dearmored keyring in
  /usr/share/keyrings), ser2netweb.repo (repo_gpgcheck=1 and gpgcheck=1, with the reason);
  the committed key packaging/ser2netweb-archive-keyring.asc is the documented one (its
  fingerprint and expiry, read from its packets);
* the commands: docs/PACKAGES.md, both READMEs and index.html give the tool's commands
  (the ones tests/package_repo_smoke.sh runs in containers), with the fingerprint;
* `verify` finds a Packages, Release, repomd.xml or primary that doesn't match its files
  (on small trees made here), reads InRelease's signed text and an armored key, and takes
  an .rpm only with a good signature by the site's key (rpmkeys -Kv as rpm 4 and rpm 6
  write it; an unsigned one passes rpm -K on its digests alone);
* `sign` signs every .rpm with rpmsign, the key's passphrase in a file of gpg's home (0600,
  removed afterwards; never an argument), then makes each architecture's repodata again;
* the workflow: its triggers, least privilege per job, the signing secrets in the signing
  step only and only for a release's run or one on main or a v* tag (never a pull
  request's or another branch's), the deployment only after the container tests and never
  from a pull request; release.yml starts it once a release is out.

Run: python3 tests/test_package_repo.py   (stdlib only)
"""
import base64
import datetime
import gzip
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap

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
            "type=rpm-md", "enabled=1", "repo_gpgcheck=1", "gpgcheck=1", "pkg_gpgcheck=1",
            f"gpgkey={pr.SITE_URL}/ser2netweb-archive-keyring.asc"}
    assert want == {line for line in lines if not line.startswith("#")}, lines
    reason = repo.split("[ser2netweb]", 1)[1].split("repo_gpgcheck=1", 1)[0]
    assert "SHA-256 of every package" in reason and "and of every package" in reason, reason
    assert "zypper by pkg_gpgcheck" in reason, "zypper skips packages without it"
    print("ser2netweb.sources pins the keyring; ser2netweb.repo checks the signed metadata "
          "and every package's signature  OK")


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


RPM4_GOOD = """ser2netweb-3.0.1-1.x86_64.rpm:
    Header V4 RSA/SHA512 Signature, key ID 711bb799: OK
    Header SHA256 digest: OK
    Header SHA1 digest: OK
    Payload SHA256 digest: OK
    MD5 digest: OK
"""
RPM6_GOOD = """ser2netweb-3.0.1-1.x86_64.rpm:
    Header OpenPGP V4 RSA/SHA512 signature, key fingerprint: b58760e7c0b0515e81d250f98164d694711bb799: OK
    Header SHA256 digest: OK
    Payload SHA256 digest: OK
"""
UNSIGNED = """ser2netweb-3.0.1-1.x86_64.rpm:
    Header SHA256 digest: OK
    Payload SHA256 digest: OK
"""


def test_rpm_signatures():
    keys = [pr.FINGERPRINT]
    for out in (RPM4_GOOD, RPM6_GOOD):
        assert pr.rpm_signature_problems("a.rpm", out, 0, keys) == [], out
    assert pr.rpm_signature_problems("a.rpm", UNSIGNED, 0, keys) == ["a.rpm: no signature"]
    other = RPM4_GOOD.replace("711bb799", "0badc0de")
    assert pr.rpm_signature_problems("a.rpm", other, 0, keys) == [
        f"a.rpm: signed by 0BADC0DE, not {pr.FINGERPRINT}"]
    nokey = RPM4_GOOD.replace("711bb799: OK", "711bb799: NOKEY")
    assert pr.rpm_signature_problems("a.rpm", nokey, 1, keys) == [
        "a.rpm: signature by 711BB799: NOKEY"]
    bad = RPM6_GOOD.replace("Payload SHA256 digest: OK", "Payload SHA256 digest: NOT OK")
    assert pr.rpm_signature_problems("a.rpm", bad, 1, keys) == [
        "a.rpm: Payload SHA256 digest: NOT OK"]
    assert pr.rpm_signature_problems("a.rpm", RPM4_GOOD, 1, keys) == [
        "a.rpm: rpmkeys -K exits 1"]
    # sign: rpmsign with the key, the passphrase in a file of gpg's home, each package at
    # its BUILDTIME (at least the key's creation); one signed by the key already is kept
    created = 1_700_000_000
    with tempfile.TemporaryDirectory() as tmp:
        site = os.path.join(tmp, "site")
        times = {"ser2netweb-3.0.0-1.x86_64.rpm": 1_600_000_000,   # before the key: clamped
                 "ser2netweb-3.0.1-1.x86_64.rpm": 1_800_000_000,
                 "ser2netweb-3.0.1-1.aarch64.rpm": 1_800_000_000}  # (signed already)
        for name, when in times.items():
            arch = name.rsplit(".", 2)[1]
            _put(os.path.join(site, "rpm", arch, name), _fake_rpm(when))
        home = os.path.join(tmp, "g")
        os.mkdir(home, 0o700)
        signs, indexed = [], []

        class Gpg:
            gpg = "/usr/bin/gpg"

            def created(self, key):
                assert key == pr.FINGERPRINT
                return created

        Gpg.home = home

        def run(cmd, **_kw):
            if cmd[0].endswith("rpmkeys"):
                signed = cmd[-1].endswith(".aarch64.rpm")
                return subprocess.CompletedProcess(cmd, 0, RPM4_GOOD if signed else UNSIGNED, "")
            arg = next(a for a in cmd if a.startswith("_gpg_sign_cmd_extra_args "))
            secret = arg.split("--passphrase-file ", 1)[1].split()[0]
            with open(secret, "rb") as fh:
                signs.append(dict(cmd=cmd, file=secret, passphrase=fh.read(),
                                  mode=os.stat(secret).st_mode & 0o777))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        saved = pr.subprocess.run, pr.program, pr.index_rpm
        pr.subprocess.run = run
        pr.program = lambda name: f"/usr/bin/{name}"
        pr.index_rpm = indexed.append
        try:
            signed, kept = pr.sign_rpms(site, Gpg(), pr.FINGERPRINT, b"s3cret", b"public key")
        finally:
            pr.subprocess.run, pr.program, pr.index_rpm = saved
        assert [os.path.basename(f) for f in kept] == ["ser2netweb-3.0.1-1.aarch64.rpm"], kept
        assert [os.path.basename(f) for f in signed] == [
            "ser2netweb-3.0.0-1.x86_64.rpm", "ser2netweb-3.0.1-1.x86_64.rpm"], signed
        assert [x["cmd"][-1] for x in signs] == signed, "one rpmsign per package"
        for seen, when in zip(signs, (created, 1_800_000_000)):
            cmd = seen["cmd"]
            assert cmd[:2] == ["/usr/bin/rpmsign", "--addsign"], cmd
            for define in (f"_gpg_name {pr.FINGERPRINT}", f"_gpg_path {home}",
                           "__gpg /usr/bin/gpg", "_gpg_digest_algo sha512"):
                assert define in cmd, define
            extra = next(a for a in cmd if a.startswith("_gpg_sign_cmd_extra_args "))
            assert extra.endswith(f" --faked-system-time {when}!"), (extra, when)
            assert seen["passphrase"] == b"s3cret", seen
            assert os.name != "posix" or seen["mode"] == 0o600, seen  # (no such modes on Windows)
            assert os.path.dirname(seen["file"]) == home and not os.path.exists(seen["file"])
            assert not any("s3cret" in a for a in cmd), "the passphrase is never an argument"
        assert indexed == [os.path.join(site, "rpm", a) for a in pr.RPM_ARCHES], indexed
        assert pr.rpm_buildtime(os.path.join(site, "rpm", "x86_64",
                                             "ser2netweb-3.0.1-1.x86_64.rpm")) == 1_800_000_000
        _put(os.path.join(tmp, "not.rpm"), b"\0" * 200)
        assert "not an rpm" in raises(pr.rpm_buildtime, os.path.join(tmp, "not.rpm"))
    print("verify takes an .rpm only with a good signature by the site's key (rpm 4 / 6); "
          "sign signs each with rpmsign (the passphrase in a file of gpg's home) at its "
          "BUILDTIME or the key's creation, keeps one signed by the key, then makes the "
          "repodata again  OK")


def _fake_rpm(buildtime: int) -> bytes:
    """A lead, an empty signature header and a main header with BUILDTIME, as
    rpm_buildtime reads them (no payload)."""
    def header(entries):
        index, store = b"", b""
        for tag, kind, value in entries:
            index += struct.pack(">iiii", tag, kind, len(store), 1)
            store += value
        return pr.RPM_HEADER_MAGIC + b"\0" * 4 + struct.pack(">II", len(entries), len(store)) \
            + index + store
    sig = header([])
    main = header([(1000, 6, b"ser2netweb\0"), (pr.RPMTAG_BUILDTIME, 4,
                                                  struct.pack(">I", buildtime))])
    return pr.RPM_LEAD_MAGIC + b"\0" * 92 + sig + b"\0" * (-len(sig) % 8) + main


def test_signing_twice_is_the_same_file():
    """Signing a package twice gives the same bytes (a cached primary.xml keeps matching
    the file), and one signed by the key already is left alone. With rpmsign, rpmkeys and
    gpg on PATH and SER2NETWEB_TEST_RPM naming a release's .rpm (package-repo.yml's build
    job sets it); elsewhere a SKIP."""
    rpm = os.environ.get("SER2NETWEB_TEST_RPM", "")
    tools = [shutil.which(t) for t in ("rpmsign", "rpmkeys", "gpg")]
    if not (rpm and os.path.isfile(rpm) and all(tools)):
        print("SKIP (part): signing twice needs rpmsign, rpmkeys, gpg and SER2NETWEB_TEST_RPM "
              "(package-repo.yml's build job)")
        return
    import time
    gpg = pr.GnuPG()
    saved = pr.index_rpm
    pr.index_rpm = lambda folder: None  # (createrepo_c isn't needed here)
    try:
        gpg.run("--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key",
                "ser2netweb test of deterministic signing", "rsa3072", "sign", "1d")
        (key,) = gpg.secret_fingerprints()
        public = gpg.run("--armor", "--export", key)
        with tempfile.TemporaryDirectory() as tmp:
            digests = []
            for n in (1, 2):
                site = os.path.join(tmp, f"site{n}")
                os.makedirs(os.path.join(site, "rpm", "x86_64"))
                shutil.copy(rpm, os.path.join(site, "rpm", "x86_64"))
                signed, kept = pr.sign_rpms(site, gpg, key, b"", public)
                assert len(signed) == 1 and not kept, (signed, kept)
                digests.append(_sha(open(signed[0], "rb").read()))
                time.sleep(1.5)  # (another second on the clock: the signature mustn't follow it)
            assert digests[0] == digests[1], f"signed twice, two files: {digests}"
            assert digests[0] != _sha(open(rpm, "rb").read()), "not signed"
            # signed by the key already: kept as it is
            signed, kept = pr.sign_rpms(site, gpg, key, b"", public)
            assert not signed and len(kept) == 1, (signed, kept)
            assert _sha(open(kept[0], "rb").read()) == digests[0]
            _put(os.path.join(site, f"{pr.KEYRING}.asc"), public)
            assert pr.check_rpms(site, [key]) == []
            assert pr.rpm_buildtime(kept[0]) == pr.rpm_buildtime(rpm), "the main header stays"
    finally:
        pr.index_rpm = saved
        gpg.close()
    print(f"rpmsign twice on {os.path.basename(rpm)}: the same file ({digests[0][:12]}...), "
          "signed by the key; a third time it is kept as it is  OK")


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
    # the secrets: in the signing step only, and only for a release or a run on main or a
    # v* tag (empty in a pull request's run and another branch's)
    secret_steps = [s for s in steps(build) if "secrets." in s]
    assert len(secret_steps) == 1 and "id: sign" in secret_steps[0], secret_steps
    sign = secret_steps[0]
    release_ref = ("github.event_name == 'release' || github.ref == 'refs/heads/main' || "
                   "startsWith(github.ref, 'refs/tags/v')")
    for name in ("PACKAGES_GPG_PRIVATE_KEY", "PACKAGES_GPG_PASSPHRASE", "PACKAGES_GPG_FINGERPRINT"):
        assert (f"{name}: ${{{{ ({release_ref}) && secrets.{name} || '' }}}}" in sign), name
    assert f"RELEASE_REF: ${{{{ {release_ref} }}}}" in sign
    assert wf.count("secrets.") == 3, "no other secret anywhere"
    assert 'if [ "$EVENT" = pull_request ]; then' in sign
    assert 'elif [ "$RELEASE_REF" != true ]; then' in sign
    assert sign.count("python3 tools/package_repo.py sign site --test-key") == 3, sign
    assert "--keyring packaging/ser2netweb-archive-keyring.asc" in sign
    assert sign.count('echo "deploy=false" >> "$GITHUB_OUTPUT"') == 3
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
    # rpmsign (sign) and rpmkeys (verify) come with the runner's rpm package
    assert "--no-install-recommends apt-utils createrepo-c rpm >/dev/null" in build
    # signing twice gives the same file, with the real tools and a release's rpm
    twice = next(s for s in steps(build) if "Signing an .rpm twice" in s)
    assert 'SER2NETWEB_TEST_RPM="$rpm" python3 tests/test_package_repo.py' in twice, twice
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


def _one_failing_start_keeps_the_rest(channels: str, loop: str) -> None:
    """The step's script with a stand-in for gh that can't start one workflow: the ones
    after it are started all the same, and a warning names the one that wasn't."""
    bash = shutil.which("bash")
    if not bash or os.name == "nt":
        print("SKIP (part): the channels step's script needs bash")
        return
    script = textwrap.dedent(channels.split("        run: |\n", 1)[1])
    workflows = re.findall(r"[\w.-]+\.yml", loop)
    with tempfile.TemporaryDirectory() as tmp:
        gh = os.path.join(tmp, "gh")
        with open(gh, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "$*" >> "$LOG"\n[ "$3" != "$FAILING" ]\n')
        os.chmod(gh, 0o755)
        log = os.path.join(tmp, "log")
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], LOG=log,
                   FAILING=workflows[1], GITHUB_REF_NAME="v9.9.9")
        # (as GitHub runs a step's script)
        p = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                           env=env, capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
        with open(log, encoding="utf-8") as fh:
            calls = fh.read().splitlines()
    assert calls == [f"workflow run {w} --ref main -f tag=v9.9.9" for w in workflows], calls
    out = p.stdout.splitlines()
    assert [ln for ln in out if ln.startswith("::warning::")] == [
        ln for ln in out if workflows[1] in ln] and len(out) == len(workflows), out
    assert sum(ln.startswith("started ") for ln in out) == len(workflows) - 1, out


def test_release_starts_it():
    release = jobs(read("release.yml"))
    channels = release["channels"]
    assert needs(channels) == ["verify", "publish"], needs(channels)
    assert key(channels, "if") == ("github.event_name == 'push' && "
                                   "needs.verify.outputs.prerelease != 'true'"), channels
    assert permissions(channels) == {"actions": "write"}, permissions(channels)
    assert "uses:" not in channels and "actions/checkout" not in channels
    loop = next(line for line in channels.splitlines() if "for workflow in" in line)
    assert "package-repo.yml" in loop.split(), loop
    assert 'gh workflow run "$workflow" --ref main -f tag="$GITHUB_REF_NAME"' in channels
    assert "channels" not in {n for job in release for n in needs(release[job])}
    _one_failing_start_keeps_the_rest(channels, loop)
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
    test_rpm_signatures()
    test_signing_twice_is_the_same_file()
    test_workflow()
    test_release_starts_it()
    print("\nPASS: package repositories")
