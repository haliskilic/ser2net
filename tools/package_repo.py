"""The signed APT and DNF / zypper repositories of ser2netweb, served by GitHub Pages.

    python3 tools/package_repo.py fetch DIR [--tag vX.Y.Z]
        the .deb / .rpm files of the newest KEEP releases (not pre-releases) up to TAG
        (default: the newest release), each checked against its release's SHA256SUMS (and
        the digest GitHub lists for it), into DIR/<tag>/, with DIR/releases.json
    python3 tools/package_repo.py build DIR SITE
        the static tree in SITE: deb/ (apt-ftparchive: Packages, Packages.gz, by-hash,
        Release), rpm/<arch>/ (createrepo_c), ser2netweb.sources, ser2netweb.repo, index.html
    python3 tools/package_repo.py sign SITE (--keyring ASC | --test-key) [--fingerprint FPR]
        every .rpm (rpmsign: a header signature added, the package as released otherwise)
        and its repodata made again, then InRelease, Release.gpg and each
        repodata/repomd.xml.asc, and the public key as ser2netweb-archive-keyring.asc /
        .gpg: with --keyring the secret key in $PACKAGES_GPG_PRIVATE_KEY
        ($PACKAGES_GPG_PASSPHRASE), whose fingerprint must be FPR (default: FINGERPRINT),
        ASC's and $PACKAGES_GPG_FINGERPRINT's (when set), and ASC is what users get; with
        --test-key a throwaway key made for this run (pull requests)
    python3 tools/package_repo.py verify SITE [--fingerprint FPR | --any-key]
        every index against the files it lists, every signature (gpgv) against the site's
        keyring, whose key must be FPR (default: FINGERPRINT), and every .rpm's (rpmkeys
        -K, the same key)
    python3 tools/package_repo.py fingerprint ASC
        the fingerprint of the key in ASC (an armored public key)
    python3 tools/package_repo.py commands FAMILY [--part install|update|remove]
                                           [--base URL] [--batch] [--root]
        the commands docs/PACKAGES.md gives for FAMILY (apt, dnf, zypper): --base another
        address than SITE_URL, --batch without questions, --root without sudo
        (tests/package_repo_smoke.sh runs them in containers)

.github/workflows/package-repo.yml runs these (docs/PACKAGES.md). The packages are the
release files, never rebuilt: the .deb files as they are, the .rpm files with the package
signing key's signature added to their signature header (their header, payload and digests
stay the release's; each made at its package's build time, so a package signed again is the
same file). dnf / zypper check both, the signed repository metadata, which carries each
package's SHA-256, and each package's own signature (repo_gpgcheck=1, gpgcheck=1, and
pkg_gpgcheck=1, without which zypper skips a package's signature once the metadata's is
good). Standard library only; build needs apt-ftparchive (apt-utils) and
createrepo_c, sign gpg, rpmsign (rpm) and createrepo_c, verify gpgv and rpmkeys (rpm).
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, NoReturn

REPOSITORY = "haliskilic/ser2netweb"
SITE_URL = "https://haliskilic.github.io/ser2netweb"
# the package signing key (RSA 4096, until 2031-11-12): packaging/ser2netweb-archive-keyring.asc
FINGERPRINT = "B58760E7C0B0515E81D250F98164D694711BB799"
KEY_EXPIRES = "2031-11-12"
KEYRING = "ser2netweb-archive-keyring"
KEYRING_PATH = f"/usr/share/keyrings/{KEYRING}.gpg"
KEEP = 3  # releases in the repositories
SUITE, COMPONENT = "stable", "main"
DEB_ARCHES = ("amd64", "arm64")
RPM_ARCHES = ("x86_64", "aarch64")
POOL = "pool/main/s/ser2netweb"
DEB = re.compile(r"ser2netweb_(?P<version>[0-9][^_]*)_(?P<arch>amd64|arm64)\.deb")
RPM = re.compile(r"ser2netweb-(?P<version>[0-9][^-]*)-(?P<release>[0-9]+)\."
                 r"(?P<arch>x86_64|aarch64)\.rpm")
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
REPO_NS = "{http://linux.duke.edu/metadata/repo}"
COMMON_NS = "{http://linux.duke.edu/metadata/common}"

SOURCES = f"""\
Types: deb
URIs: {{base}}/deb
Suites: {SUITE}
Components: {COMPONENT}
Architectures: {" ".join(DEB_ARCHES)}
Signed-By: {KEYRING_PATH}
"""

REPO_FILE = f"""\
# ser2netweb, https://github.com/{REPOSITORY} (docs/PACKAGES.md)
[ser2netweb]
name=ser2netweb
baseurl={{base}}/rpm/$basearch
type=rpm-md
enabled=1
# dnf / zypper check with this key the signature of the repository's metadata
# (repodata/repomd.xml, which holds the SHA-256 of every package) and of every package:
# dnf by gpgcheck, zypper by pkg_gpgcheck (with signed metadata it checks packages only
# then; dnf 5 reads it as gpgcheck, dnf 4 passes it over)
repo_gpgcheck=1
gpgcheck=1
pkg_gpgcheck=1
gpgkey={{base}}/{KEYRING}.asc
"""

# The commands docs/PACKAGES.md, both READMEs and index.html give. "{yes}" is where --batch
# puts the package manager's "don't ask" option; {base} is the repository's address.
COMMANDS: dict[str, dict[str, tuple[str, ...]]] = {
    "apt": {
        "install": (
            f"sudo curl -fsSLo {KEYRING_PATH} {{base}}/{KEYRING}.gpg",
            "sudo curl -fsSLo /etc/apt/sources.list.d/ser2netweb.sources "
            "{base}/ser2netweb.sources",
            "sudo apt update",
            "sudo apt install{yes} ser2netweb",
        ),
        "update": (
            "sudo apt update",
            "sudo apt install{yes} --only-upgrade ser2netweb",
        ),
        "remove": (
            "sudo apt remove{yes} ser2netweb",
            f"sudo rm /etc/apt/sources.list.d/ser2netweb.sources {KEYRING_PATH}",
        ),
    },
    "dnf": {
        "install": (
            "sudo curl -fsSLo /etc/yum.repos.d/ser2netweb.repo {base}/ser2netweb.repo",
            "sudo dnf install{yes} ser2netweb",
        ),
        "update": (
            "sudo dnf upgrade{yes} --refresh ser2netweb",
        ),
        "remove": (
            "sudo dnf remove{yes} ser2netweb",
            "sudo rm /etc/yum.repos.d/ser2netweb.repo",
        ),
    },
    "zypper": {
        "install": (
            f"sudo rpm --import {{base}}/{KEYRING}.asc",
            "sudo zypper{yes} addrepo {base}/ser2netweb.repo",
            "sudo zypper{yes} refresh",
            "sudo zypper{yes} install ser2netweb",
        ),
        "update": (
            "sudo zypper{yes} refresh",
            "sudo zypper{yes} update ser2netweb",
        ),
        "remove": (
            "sudo zypper{yes} remove ser2netweb",
            "sudo zypper{yes} removerepo ser2netweb",
        ),
    },
}
BATCH = {"apt": " -y", "dnf": " -y", "zypper": " --non-interactive"}


def commands(family: str, part: str = "install", base: str = SITE_URL, *,
             batch: bool = False, root: bool = False) -> list[str]:
    out = []
    for line in COMMANDS[family][part]:
        line = line.format(base=base, yes=BATCH[family] if batch else "")
        out.append(line.removeprefix("sudo ") if root else line)
    return out


def spaced(fingerprint: str) -> str:
    """B587 60E7 ... as gpg and dnf print it (two halves of five groups)."""
    groups = [fingerprint[i:i + 4] for i in range(0, len(fingerprint), 4)]
    return " ".join(groups[:5]) + "  " + " ".join(groups[5:])


def fail(message: str) -> NoReturn:
    raise SystemExit(f"package_repo: {message}")


def program(name: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"{name} is not on PATH")
    return path


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- fetch: which releases, their files, SHA256SUMS ---------------------------------------

def version_key(tag: str) -> tuple[int, ...] | None:
    m = TAG.fullmatch(tag)
    return tuple(int(n) for n in m.groups()) if m else None


def package_assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    """The release's .deb / .rpm files of ser2netweb of its own version."""
    version = release["tag_name"].removeprefix("v")
    out = []
    for asset in release.get("assets", []):
        m = DEB.fullmatch(asset["name"]) or RPM.fullmatch(asset["name"])
        if m and m["version"] == version:
            out.append(asset)
    return out


def select(releases: list[dict[str, Any]], tag: str = "", keep: int = KEEP) -> list[dict[str, Any]]:
    """The newest `keep` releases with packages, newest first, by version: no drafts, no
    pre-releases, tags vX.Y.Z only. With `tag` the newest is that one (it must be such a
    release)."""
    usable = [r for r in releases
              if not r.get("draft") and not r.get("prerelease")
              and version_key(r["tag_name"]) and package_assets(r)]
    usable.sort(key=lambda r: version_key(r["tag_name"]) or (), reverse=True)
    if tag:
        tags = [r["tag_name"] for r in usable]
        if tag not in tags:
            fail(f"{tag} is not a published release (vX.Y.Z, not a pre-release) with "
                 f"packages; those are: {', '.join(tags) or 'none'}")
        usable = usable[tags.index(tag):]
    if not usable:
        fail("no published release has packages")
    return usable[:keep]


def read_sums(text: str) -> dict[str, str]:
    """sha256sum's output: name -> hex digest ("*name" in binary mode too)."""
    sums = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line.strip())
        if m:
            sums[m.group(2)] = m.group(1)
    return sums


def check_file(path: str, sums: dict[str, str], digest: str | None = None) -> str:
    name = os.path.basename(path)
    if name not in sums:
        fail(f"{name} is not in its release's SHA256SUMS")
    got = sha256(path)
    if got != sums[name]:
        fail(f"{name}: sha256 {got}, SHA256SUMS says {sums[name]}")
    if digest and digest != f"sha256:{got}":
        fail(f"{name}: sha256 {got}, GitHub lists {digest}")
    return got


def _get(url: str, token: str = "") -> urllib.request.Request:
    if not url.startswith("https://"):
        fail(f"not an https:// address: {url}")
    headers = {"User-Agent": "ser2netweb-package-repo", "Accept": "application/vnd.github+json"}
    if token:  # (the API only: the files' addresses redirect to another host)
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)  # noqa: S310 (https only)


def list_releases(repository: str, token: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 11):
        url = f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        with urllib.request.urlopen(_get(url, token), timeout=60) as r:  # noqa: S310 (https only)
            batch = json.load(r)
        out += batch
        if len(batch) < 100:
            break
    return out


def download(url: str, dest: str) -> None:
    with urllib.request.urlopen(_get(url), timeout=300) as r, \
            open(dest + ".part", "wb") as fh:  # noqa: S310 (https only)
        shutil.copyfileobj(r, fh, 1 << 20)
    os.replace(dest + ".part", dest)


def fetch(out: str, tag: str = "", repository: str = REPOSITORY, keep: int = KEEP) -> list[dict]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    chosen = select(list_releases(repository, token), tag, keep)
    os.makedirs(out, exist_ok=True)
    listed = []
    for release in chosen:
        folder = os.path.join(out, release["tag_name"])
        os.makedirs(folder, exist_ok=True)
        sums_asset = next((a for a in release["assets"] if a["name"] == "SHA256SUMS"), None)
        if sums_asset is None:
            fail(f"{release['tag_name']} has no SHA256SUMS")
        download(sums_asset["browser_download_url"], os.path.join(folder, "SHA256SUMS"))
        with open(os.path.join(folder, "SHA256SUMS"), encoding="utf-8") as fh:
            sums = read_sums(fh.read())
        files = []
        for asset in package_assets(release):
            path = os.path.join(folder, asset["name"])
            download(asset["browser_download_url"], path)
            got = check_file(path, sums, asset.get("digest"))
            print(f"{release['tag_name']}: {asset['name']} sha256 {got}  OK")
            files.append(asset["name"])
        listed.append({"tag": release["tag_name"],
                       "version": release["tag_name"].removeprefix("v"),
                       "files": sorted(files)})
    with open(os.path.join(out, "releases.json"), "w", encoding="utf-8") as fh:
        json.dump(listed, fh, indent=1)
        fh.write("\n")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"version={listed[0]['version']}\n")
            fh.write(f"versions={' '.join(r['version'] for r in listed)}\n")
    return listed


# --- build ---------------------------------------------------------------------------------

def write(path: str, data: str | bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data.encode("utf-8") if isinstance(data, str) else data)


def by_hash(folder: str) -> None:
    """Each index file of `folder` also as by-hash/SHA256/<its sha256> (Acquire-By-Hash):
    apt asks for the index its InRelease names by that name, which a cache in front of
    GitHub Pages can't answer with an older file."""
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            dest = os.path.join(folder, "by-hash", "SHA256", sha256(path))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(path, dest)


def build_deb(dl: str, releases: list[dict], site: str) -> None:
    deb = os.path.join(site, "deb")
    for release in releases:
        for name in release["files"]:
            if name.endswith(".deb"):
                dest = os.path.join(deb, *POOL.split("/"), name)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(os.path.join(dl, release["tag"], name), dest)
    ftparchive = program("apt-ftparchive")
    # SHA256 only: apt uses the strongest hash listed, and by-hash has that one
    hashes = ["--md5=no", "--sha1=no", "--sha512=no"]
    dists = os.path.join(deb, "dists", SUITE)
    for arch in DEB_ARCHES:
        folder = os.path.join(dists, COMPONENT, f"binary-{arch}")
        os.makedirs(folder, exist_ok=True)
        packages = subprocess.run([ftparchive, *hashes, "--arch", arch, "packages", "pool"],
                                  cwd=deb, check=True, capture_output=True).stdout
        write(os.path.join(folder, "Packages"), packages)
        write(os.path.join(folder, "Packages.gz"), gzip.compress(packages, 9, mtime=0))
        by_hash(folder)
    fields = {"Origin": "ser2netweb", "Label": "ser2netweb", "Suite": SUITE, "Codename": SUITE,
              "Architectures": " ".join(DEB_ARCHES), "Components": COMPONENT,
              "Description": f"ser2netweb packages, https://github.com/{REPOSITORY}",
              "Acquire-By-Hash": "yes"}
    options = [f"-oAPT::FTPArchive::Release::{k}={v}" for k, v in fields.items()]
    release_file = subprocess.run([ftparchive, *hashes, *options, "release", f"dists/{SUITE}"],
                                  cwd=deb, check=True, capture_output=True).stdout
    write(os.path.join(dists, "Release"), release_file)


def index_rpm(folder: str) -> None:
    """repodata/ for the RPMs in `folder` (made anew: sign() makes it again once they are
    signed)."""
    shutil.rmtree(os.path.join(folder, "repodata"), ignore_errors=True)
    # gzip: every dnf / zypper reads it (zstd needs a newer one); no sqlite databases
    # (only yum 3 used them, on systems older than the packages' glibc 2.28)
    subprocess.run([program("createrepo_c"), "--quiet", "--general-compress-type", "gz",
                    "--no-database", "--checksum", "sha256", folder], check=True)


def build_rpm(dl: str, releases: list[dict], site: str) -> None:
    for arch in RPM_ARCHES:
        folder = os.path.join(site, "rpm", arch)
        os.makedirs(folder, exist_ok=True)
        for release in releases:
            for name in release["files"]:
                m = RPM.fullmatch(name)
                if m and m["arch"] == arch:
                    shutil.copyfile(os.path.join(dl, release["tag"], name),
                                    os.path.join(folder, name))
        index_rpm(folder)


def build(dl: str, site: str) -> list[dict]:
    with open(os.path.join(dl, "releases.json"), encoding="utf-8") as fh:
        releases = json.load(fh)
    if os.path.exists(site) and os.listdir(site):
        fail(f"{site} is not empty")
    build_deb(dl, releases, site)
    build_rpm(dl, releases, site)
    write(os.path.join(site, "ser2netweb.sources"), SOURCES.format(base=SITE_URL))
    write(os.path.join(site, "ser2netweb.repo"), REPO_FILE.format(base=SITE_URL))
    write(os.path.join(site, "index.html"), index_html(releases))
    return releases


# --- the landing page ----------------------------------------------------------------------

PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ser2netweb package repositories</title>
<style>
:root {{ color-scheme: light dark; --fg: #1f2328; --bg: #ffffff; --muted: #59636e;
  --code: #f6f8fa; --line: #d1d9e0; --link: #0969da; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg: #f0f6fc; --bg: #0d1117;
  --muted: #9198a1; --code: #151b23; --line: #3d444d; --link: #4493f8; }} }}
body {{ margin: 0; background: var(--bg); color: var(--fg); line-height: 1.5;
  font: 16px/1.5 -apple-system, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif; }}
main {{ max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }}
h1 {{ font-size: 1.8rem; margin: .5rem 0; }}
h2 {{ font-size: 1.25rem; margin-top: 2rem; padding-bottom: .3rem;
  border-bottom: 1px solid var(--line); }}
a {{ color: var(--link); }}
p.lead, .muted {{ color: var(--muted); }}
pre {{ background: var(--code); border: 1px solid var(--line); border-radius: 6px;
  padding: .8rem 1rem; overflow-x: auto; }}
code {{ font: .9em/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
p code, li code {{ background: var(--code); border-radius: 4px; padding: .1em .3em; }}
</style>
</head>
<body>
<main>
<h1>ser2netweb package repositories</h1>
<p class="lead">Signed APT and DNF / zypper repositories of
<a href="https://github.com/{repository}">ser2netweb</a>, a serial-to-network bridge with a
web management UI (an independent project, not the classic <code>ser2net</code> daemon), for
x86_64 and arm64 Linux. They hold the <code>.deb</code> / <code>.rpm</code> files of the
newest releases, as released (the <code>.rpm</code> files signed): {versions}.</p>

<h2>Debian, Ubuntu, Raspberry Pi OS (64-bit)</h2>
<pre><code>{apt}</code></pre>

<h2>Fedora, RHEL, AlmaLinux, Rocky Linux</h2>
<pre><code>{dnf}</code></pre>
<p>On the first install dnf asks to import the signing key: check that its fingerprint is
the one below.</p>

<h2>openSUSE</h2>
<pre><code>{zypper}</code></pre>

<h2>Then</h2>
<p>Installing doesn't start the service: <code>sudo systemctl enable --now ser2netweb</code>
does (the web UI then listens on <code>127.0.0.1:8080</code>), and
<code>sudo -u ser2netweb ser2netweb --set-password admin</code> sets the first password.
The data directory is <code>/var/lib/ser2netweb</code>.</p>

<h2>Updating</h2>
<pre><code>{update}</code></pre>
<p>A newer package restarts a running service.</p>

<h2>Removing</h2>
<pre><code>{remove}</code></pre>
<p>Removing the package stops the service and keeps <code>/var/lib/ser2netweb</code> and
the account <code>ser2netweb</code>.</p>

<h2>Signing key</h2>
<p><code>InRelease</code> / <code>Release.gpg</code> and every
<code>repodata/repomd.xml.asc</code> are signed with the key<br>
<code>{fingerprint}</code><br>
(RSA 4096, &ldquo;ser2netweb package signing&rdquo;, valid until {expires}):
<a href="{keyring}.asc">{keyring}.asc</a>, dearmored for APT's <code>Signed-By</code>:
<a href="{keyring}.gpg">{keyring}.gpg</a>. The RPM packages carry its signature too
(<code>rpm -K</code>), and dnf and zypper check both, the metadata, which carries the SHA-256
of every package, and each package (<code>repo_gpgcheck=1</code>, <code>gpgcheck=1</code> for
dnf and <code>pkg_gpgcheck=1</code> for zypper in
<a href="ser2netweb.repo">ser2netweb.repo</a>). APT:
<a href="ser2netweb.sources">ser2netweb.sources</a>.</p>

<p class="muted"><a href="https://github.com/{repository}/blob/main/docs/PACKAGES.md">docs/PACKAGES.md</a>
&middot; <a href="https://github.com/{repository}/releases">releases</a>
&middot; <a href="https://github.com/{repository}">source</a></p>
</main>
</body>
</html>
"""


def index_html(releases: list[dict]) -> str:
    def block(lines: list[str]) -> str:
        return html.escape("\n".join(lines))

    update: list[str] = []
    remove: list[str] = []
    for family, label in (("apt", "Debian, Ubuntu"), ("dnf", "Fedora, RHEL, AlmaLinux, Rocky"),
                          ("zypper", "openSUSE")):
        update += [f"# {label}", *commands(family, "update")]
        remove += [f"# {label}", *commands(family, "remove")]
    return PAGE.format(
        repository=REPOSITORY, keyring=KEYRING, fingerprint=spaced(FINGERPRINT),
        expires=KEY_EXPIRES,
        versions=", ".join(html.escape(r["version"]) for r in releases),
        apt=block(commands("apt")), dnf=block(commands("dnf")),
        zypper=block(commands("zypper")), update=block(update), remove=block(remove))


# --- sign ----------------------------------------------------------------------------------

def dearmor(text: str) -> bytes:
    """The OpenPGP packets of an ASCII-armored block (its CRC-24 checked when present)."""
    lines = text.strip().splitlines()
    begin = next(i for i, line in enumerate(lines) if line.startswith("-----BEGIN PGP "))
    end = next(i for i, line in enumerate(lines) if line.startswith("-----END PGP "))
    body = lines[begin + 1:end]
    body = body[body.index("") + 1:] if "" in body else body  # after the armor headers
    crc = [line for line in body if line.startswith("=")]
    data = base64.b64decode("".join(line for line in body if not line.startswith("=")))
    if crc:
        if base64.b64decode(crc[0][1:]) != _crc24(data).to_bytes(3, "big"):
            fail("the armored key's checksum doesn't match")
    return data


def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def packets(data: bytes) -> list[tuple[int, bytes]]:
    """(tag, body) of each OpenPGP packet (RFC 4880 4.2)."""
    out, i = [], 0
    while i < len(data):
        head = data[i]
        if not head & 0x80:
            fail("not an OpenPGP packet")
        if head & 0x40:  # new format
            tag, first = head & 0x3F, data[i + 1]
            if first < 192:
                length, i = first, i + 2
            elif first < 224:
                length, i = ((first - 192) << 8) + data[i + 2] + 192, i + 3
            elif first == 255:
                length, i = int.from_bytes(data[i + 2:i + 6], "big"), i + 6
            else:
                fail("partial packet lengths aren't used in keys")
        else:
            tag, size = (head >> 2) & 0x0F, {0: 1, 1: 2, 2: 4}.get(head & 3)
            if size is None:
                fail("indeterminate packet length")
            length, i = int.from_bytes(data[i + 1:i + 1 + size], "big"), i + 1 + size
        out.append((tag, data[i:i + length]))
        i += length
    return out


def fingerprints(data: bytes) -> list[str]:
    """The v4 fingerprints of the primary keys (tag 6) of a keyring."""
    out = []
    for tag, body in packets(data):
        if tag == 6:
            if body[0] != 4:
                fail(f"a version {body[0]} key")
            prefix = b"\x99" + len(body).to_bytes(2, "big")
            out.append(hashlib.sha1(prefix + body, usedforsecurity=False).hexdigest().upper())
    return out


GPG_OPTIONS = {"--pinentry-mode", "--passphrase", "--passphrase-fd", "--yes", "--armor",
               "--with-colons", "--local-user", "--digest-algo", "--output"}


class GnuPG:
    """gpg in a home of its own, made in `parent` (a short path: gpg-agent's socket may
    live there) and deleted with every key in it by close()."""

    def __init__(self, parent: str = "") -> None:
        self.home = tempfile.mkdtemp(prefix="g", dir=parent or None)
        self.gpg = program("gpg")

    def run(self, *args: str, stdin: bytes = b"") -> bytes:
        env = dict(os.environ, GNUPGHOME=self.home)
        proc = subprocess.run([self.gpg, "--batch", "--no-tty", "--quiet", *args], env=env,
                              input=stdin, check=False, capture_output=True)
        if proc.returncode:  # (the passphrase goes in on stdin, never in the arguments)
            what = next((a for a in args if a.startswith("--") and a not in GPG_OPTIONS), "")
            fail(f"gpg {what} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def secret_fingerprints(self) -> list[str]:
        listing = self.run("--with-colons", "--list-secret-keys").decode()
        out, primary = [], False
        for line in listing.splitlines():
            fields = line.split(":")
            if fields[0] in ("sec", "ssb"):
                primary = fields[0] == "sec"
            elif fields[0] == "fpr" and primary:
                out.append(fields[9])
                primary = False
        return out

    def created(self, key: str) -> int:
        """When `key` and its subkeys were made (the newest, epoch seconds): a signature
        it makes can't be older than that."""
        listing = self.run("--with-colons", "--list-secret-keys", key).decode()
        times = [int(f[5]) for f in (ln.split(":") for ln in listing.splitlines())
                 if f[0] in ("sec", "ssb") and len(f) > 5 and f[5].isdigit()]
        if not times:
            fail(f"gpg lists no creation time for {key}")
        return max(times)

    def close(self) -> None:
        gpgconf = shutil.which("gpgconf")
        if gpgconf:
            subprocess.run([gpgconf, "--kill", "gpg-agent"], env=dict(os.environ,
                           GNUPGHOME=self.home), check=False, capture_output=True)
        shutil.rmtree(self.home, ignore_errors=True)


def rpm_files(site: str) -> list[str]:
    """The .rpm files of the site, every architecture's."""
    out = []
    for arch in RPM_ARCHES:
        folder = os.path.join(site, "rpm", arch)
        if os.path.isdir(folder):
            out += sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".rpm"))
    return out


RPM_LEAD_MAGIC = b"\xed\xab\xee\xdb"
RPM_HEADER_MAGIC = b"\x8e\xad\xe8\x01"
RPMTAG_BUILDTIME = 1006


def _rpm_header(fh: Any) -> tuple[bytes, bytes, int]:
    """(index, store, size) of the rpm header at the file's position."""
    intro = fh.read(16)
    if len(intro) != 16 or intro[:4] != RPM_HEADER_MAGIC:
        fail(f"{fh.name}: not an rpm header")
    nindex, hsize = struct.unpack(">II", intro[8:16])
    index, store = fh.read(16 * nindex), fh.read(hsize)
    if len(index) != 16 * nindex or len(store) != hsize:
        fail(f"{fh.name}: a short rpm header")
    return index, store, 16 + 16 * nindex + hsize


def rpm_buildtime(path: str) -> int:
    """The BUILDTIME of an .rpm, from its main header (after the lead and the signature
    header, which signing changes and the main header doesn't)."""
    with open(path, "rb") as fh:
        if fh.read(96)[:4] != RPM_LEAD_MAGIC:
            fail(f"{path}: not an rpm")
        _index, _store, size = _rpm_header(fh)  # the signature header,
        fh.read(-size % 8)  # padded to 8 bytes
        index, store, _size = _rpm_header(fh)
    for i in range(0, len(index), 16):
        tag, kind, offset, count = struct.unpack(">iiii", index[i:i + 16])
        if tag == RPMTAG_BUILDTIME and kind == 4 and count >= 1:  # (INT32)
            return int(struct.unpack(">I", store[offset:offset + 4])[0])
    fail(f"{path}: no BUILDTIME")


def rpmsign_command(gpg: GnuPG, key: str, passphrase_file: str, path: str,
                    when: int) -> list[str]:
    """rpmsign adding a signature by `key` made at the time `when` (epoch seconds) to the
    package `path`: gpg in gpg.home, the passphrase read from a file (gpg's standard input
    is what it signs), its clock stopped at `when`, so the same package signed again is
    the same file."""
    return [program("rpmsign"), "--addsign",
            "--define", f"_gpg_name {key}", "--define", f"_gpg_path {gpg.home}",
            "--define", f"__gpg {gpg.gpg}", "--define", "_gpg_digest_algo sha512",
            "--define", "_gpg_sign_cmd_extra_args --batch --no-tty --pinentry-mode loopback "
            f"--passphrase-file {passphrase_file} --faked-system-time {when}!", path]


class RpmKeys:
    """rpmkeys with an RPM database of its own holding the public key `asc` (a file)."""

    def __init__(self, asc: str) -> None:
        self.rpmkeys = program("rpmkeys")
        self.db = tempfile.mkdtemp(prefix="rpmdb-")
        imported = subprocess.run([self.rpmkeys, "--dbpath", self.db, "--import", asc],
                                  capture_output=True, text=True, check=False)
        if imported.returncode:
            self.close()
            fail(f"rpmkeys --import {os.path.basename(asc)}: {imported.stderr.strip()}")

    def problems(self, path: str, name: str, keys: list[str]) -> list[str]:
        proc = subprocess.run([self.rpmkeys, "--dbpath", self.db, "-Kv", path],
                              capture_output=True, text=True, check=False)
        return rpm_signature_problems(name, proc.stdout + proc.stderr, proc.returncode, keys)

    def close(self) -> None:
        shutil.rmtree(self.db, ignore_errors=True)


def sign_rpms(site: str, gpg: GnuPG, key: str, passphrase: bytes,
              public: bytes) -> tuple[list[str], list[str]]:
    """Every .rpm of the site signed by `key` (rpmsign: a header signature added; their
    header, payload and digests stay as released), each made at its package's BUILDTIME,
    or at the key's creation when that is later, so a package signed again is the same
    file (a cached repodata keeps matching); one with a good signature by `key` already is
    left as it is. Then each architecture's repodata made again. Returns (the files
    signed, the files kept)."""
    files = rpm_files(site)
    if not files:
        return [], []
    created = gpg.created(key)
    asc = os.path.join(gpg.home, "public.asc")
    write(asc, public)
    keys = RpmKeys(asc)
    passphrase_file = os.path.join(gpg.home, "passphrase")  # (gpg's home: 0700, deleted)
    signed, kept = [], []
    try:
        fd = os.open(passphrase_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(passphrase)
        for path in files:
            if not keys.problems(path, os.path.basename(path), [key]):
                kept.append(path)
                continue
            when = max(rpm_buildtime(path), created)
            proc = subprocess.run(rpmsign_command(gpg, key, passphrase_file, path, when),
                                  capture_output=True, text=True, check=False)
            if proc.returncode:
                fail(f"rpmsign {os.path.basename(path)} failed: {proc.stderr.strip()}")
            signed.append(path)
    finally:
        if os.path.exists(passphrase_file):
            os.remove(passphrase_file)
        keys.close()
    for arch in RPM_ARCHES:
        folder = os.path.join(site, "rpm", arch)
        if os.path.isdir(folder):
            index_rpm(folder)
    return signed, kept


def metadata_files(site: str) -> list[tuple[str, str, str]]:
    """(data, signature, kind) of each file to sign: kind clearsign, detach or armor."""
    release = os.path.join(site, "deb", "dists", SUITE, "Release")
    out = [(release, os.path.join(os.path.dirname(release), "InRelease"), "clearsign"),
           (release, release + ".gpg", "detach")]
    for arch in RPM_ARCHES:
        repomd = os.path.join(site, "rpm", arch, "repodata", "repomd.xml")
        out.append((repomd, repomd + ".asc", "detach"))
    return out


def sign(site: str, keyring: str = "", test_key: bool = False, fingerprint: str = FINGERPRINT,
         parent: str = "") -> str:
    gpg = GnuPG(parent)
    try:
        passphrase = b""
        if test_key:
            gpg.run("--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key",
                    "ser2netweb throwaway test key (pull requests, never published)",
                    "rsa4096", "sign", "1d")
            (key,) = gpg.secret_fingerprints()
            public = gpg.run("--armor", "--export", key)
        else:
            secret = os.environ.get("PACKAGES_GPG_PRIVATE_KEY", "")
            if not secret.strip():
                fail("PACKAGES_GPG_PRIVATE_KEY is empty")
            passphrase = os.environ.get("PACKAGES_GPG_PASSPHRASE", "").encode()
            key_file = os.path.join(gpg.home, "key.asc")
            write(key_file, secret)
            try:
                gpg.run("--pinentry-mode", "loopback", "--passphrase-fd", "0", "--import",
                        key_file, stdin=passphrase)
            finally:
                os.remove(key_file)
            with open(keyring, "rb") as fh:
                public = fh.read()
            want = {fingerprint.upper()}
            named = os.environ.get("PACKAGES_GPG_FINGERPRINT", "").replace(" ", "").upper()
            have = {"the secret key": gpg.secret_fingerprints(),
                    os.path.basename(keyring): fingerprints(dearmor(public.decode())),
                    "PACKAGES_GPG_FINGERPRINT": [named] if named else [fingerprint.upper()]}
            for what, fprs in have.items():
                if set(fprs) != want:
                    fail(f"{what} is {', '.join(fprs) or 'no key'}, not {fingerprint}")
            key = fingerprint.upper()
        signed, kept = sign_rpms(site, gpg, key, passphrase, public)
        for path in signed:
            print(f"{os.path.relpath(path, site)}: signed")
        for path in kept:
            print(f"{os.path.relpath(path, site)}: signed by {key} already, kept as it is")
        for data, signature, kind in metadata_files(site):
            mode = ["--clearsign"] if kind == "clearsign" else ["--armor", "--detach-sign"]
            gpg.run("--pinentry-mode", "loopback", "--passphrase-fd", "0", "--yes",
                    "--local-user", key, "--digest-algo", "SHA512", *mode,
                    "--output", signature, data, stdin=passphrase)
        write(os.path.join(site, f"{KEYRING}.asc"), public)
        write(os.path.join(site, f"{KEYRING}.gpg"), dearmor(public.decode()))
        print(f"signed with {key}")
        return key
    finally:
        gpg.close()


# --- verify --------------------------------------------------------------------------------

def deb822(text: str) -> list[dict[str, str]]:
    """The paragraphs of a Debian control-style text (continuation lines kept, joined
    with newlines)."""
    out: list[dict[str, str]] = []
    para: dict[str, str] = {}
    last = ""
    for line in text.splitlines():
        if not line.strip():
            if para:
                out.append(para)
            para, last = {}, ""
        elif line[0] in " \t" and last:
            para[last] += "\n" + line.strip()
        else:
            last, _, value = line.partition(":")
            para[last] = value.strip()
    if para:
        out.append(para)
    return out


def cleartext(signed: str) -> str:
    """The text an OpenPGP cleartext signature (InRelease) signs, with its line ends."""
    m = re.match(r"-----BEGIN PGP SIGNED MESSAGE-----\n(?:[^\n]+\n)*\n(.*?)\n"
                 r"-----BEGIN PGP SIGNATURE-----\n", signed, re.S)
    if not m:
        fail("InRelease is not a cleartext signature")
    return "".join((line[2:] if line.startswith("- ") else line) + "\n"
                   for line in m.group(1).split("\n"))


def gpgv(keyring: str, signature: str, data: str = "") -> set[tuple[str, str]]:
    """(signer's primary fingerprint, hash algorithm) of each good signature; raises
    SystemExit when gpgv refuses it."""
    home = tempfile.mkdtemp(prefix="gpgv-")
    try:
        args = [program("gpgv"), "--homedir", home, "--status-fd", "1",
                "--keyring", os.path.abspath(keyring), signature, *([data] if data else [])]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    good = set()
    for line in proc.stdout.splitlines():
        fields = line.split()
        if fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            good.add((fields[11] if len(fields) > 11 else fields[2], fields[9]))
    if proc.returncode or not good:
        fail(f"gpgv refuses {signature}:\n{proc.stderr}{proc.stdout}")
    return good


# rpmkeys -Kv's line for a signature: rpm 4 "Header V4 RSA/SHA512 Signature, key ID
# 711bb799: OK", rpm 6 "Header OpenPGP V4 RSA/SHA512 signature, key fingerprint: b587...: OK"
RPM_SIGNATURE = re.compile(r"(?i)\bsignature, key (?:id|fingerprint:) ([0-9a-f]+): (.*)$")


def rpm_signature_problems(name: str, output: str, returncode: int,
                           keys: list[str]) -> list[str]:
    """What rpmkeys -Kv says is wrong with the package `name`: each signature has to be
    good and by one of `keys` (fingerprints), and there has to be one (an unsigned package
    passes rpm -K on its digests alone)."""
    problems, signatures = [], 0
    for line in output.splitlines():
        m = RPM_SIGNATURE.search(line.strip())
        if not m:
            if re.search(r"(?i)\b(not ok|bad|nokey|notrusted)\b", line):
                problems.append(f"{name}: {line.strip()}")
            continue
        signatures += 1
        key_id, verdict = m.group(1).upper(), m.group(2).strip()
        if verdict != "OK":
            problems.append(f"{name}: signature by {key_id}: {verdict}")
        elif not any(k.upper().endswith(key_id) for k in keys):
            problems.append(f"{name}: signed by {key_id}, not {' / '.join(keys)}")
    if not signatures:
        problems.append(f"{name}: no signature")
    if returncode and not problems:
        problems.append(f"{name}: rpmkeys -K exits {returncode}")
    return problems


def check_rpms(site: str, keys: list[str]) -> list[str]:
    """Every .rpm's signature, rpmkeys -K against the site's key in an RPM database of
    its own."""
    files = rpm_files(site)
    if not files:
        return []
    rpmkeys = RpmKeys(os.path.join(site, f"{KEYRING}.asc"))
    try:
        problems = []
        for path in files:
            problems += rpmkeys.problems(path, os.path.relpath(path, site), keys)
        return problems
    finally:
        rpmkeys.close()


def check_release(deb: str) -> list[str]:
    """Release's SHA256 list against the files (and their by-hash copies), each Packages
    against the pool. Returns the problems."""
    problems = []
    dists = os.path.join(deb, "dists", SUITE)
    with open(os.path.join(dists, "Release"), encoding="utf-8", newline="") as fh:
        release = fh.read()
    (fields,) = deb822(release)
    for key, want in (("Suite", SUITE), ("Components", COMPONENT),
                      ("Architectures", " ".join(DEB_ARCHES)), ("Acquire-By-Hash", "yes")):
        if fields.get(key) != want:
            problems.append(f"Release: {key} is {fields.get(key)!r}, not {want!r}")
    listed = {}
    for line in fields.get("SHA256", "").splitlines():
        if line.strip():
            digest, length, name = line.split()
            listed[name] = (digest, int(length))
    for arch in DEB_ARCHES:
        for name in ("Packages", "Packages.gz"):
            rel = f"{COMPONENT}/binary-{arch}/{name}"
            if rel not in listed:
                problems.append(f"Release doesn't list {rel}")
    for rel, (digest, size) in listed.items():
        path = os.path.join(dists, *rel.split("/"))
        if not os.path.isfile(path):
            problems.append(f"Release lists {rel}, which is missing")
            continue
        if (sha256(path), os.path.getsize(path)) != (digest, size):
            problems.append(f"{rel} doesn't match Release")
        copy = os.path.join(os.path.dirname(path), "by-hash", "SHA256", digest)
        if not os.path.isfile(copy) or sha256(copy) != digest:
            problems.append(f"{rel} has no by-hash/SHA256/{digest}")
    pool = {n for n in os.listdir(os.path.join(deb, *POOL.split("/")))} \
        if os.path.isdir(os.path.join(deb, *POOL.split("/"))) else set()
    seen = set()
    for arch in DEB_ARCHES:
        folder = os.path.join(dists, COMPONENT, f"binary-{arch}")
        with open(os.path.join(folder, "Packages"), "rb") as fh:
            raw = fh.read()
        with gzip.open(os.path.join(folder, "Packages.gz"), "rb") as fz:
            if fz.read() != raw:
                problems.append(f"binary-{arch}: Packages.gz isn't Packages")
        for para in deb822(raw.decode("utf-8")):
            name = para.get("Filename", "")
            path = os.path.join(deb, *name.split("/"))
            m = DEB.fullmatch(os.path.basename(name))
            if para.get("Package") != "ser2netweb" or not m or m["arch"] != arch:
                problems.append(f"binary-{arch} lists {para.get('Package')} {name}")
            elif para.get("Architecture") != arch or para.get("Version") != m["version"]:
                problems.append(f"{name}: Architecture {para.get('Architecture')}, "
                                f"Version {para.get('Version')}")
            if not os.path.isfile(path):
                problems.append(f"binary-{arch} lists {name}, which is missing")
            elif (para.get("SHA256"), para.get("Size")) != (sha256(path),
                                                            str(os.path.getsize(path))):
                problems.append(f"{name} doesn't match binary-{arch}/Packages")
            seen.add(os.path.basename(name))
    if pool != seen:
        problems.append(f"the pool has {sorted(pool)}, the indexes list {sorted(seen)}")
    return problems


def check_repodata(folder: str) -> list[str]:
    """repomd.xml against its files, primary against the folder's RPMs."""
    problems = []
    repomd = ET.parse(os.path.join(folder, "repodata", "repomd.xml")).getroot()  # noqa: S314 (our own)
    primary = None
    for data in repomd.iter(f"{REPO_NS}data"):
        location = data.find(f"{REPO_NS}location")
        checksum = data.find(f"{REPO_NS}checksum")
        if location is None or checksum is None:
            problems.append(f"repomd.xml: {data.get('type')} has no location / checksum")
            continue
        href = location.get("href", "")
        path = os.path.join(folder, *href.split("/"))
        if not os.path.isfile(path):
            problems.append(f"repomd.xml lists {href}, which is missing")
        elif checksum.get("type") != "sha256" or checksum.text != sha256(path):
            problems.append(f"{href} doesn't match repomd.xml")
        elif not href.endswith(".gz") and data.get("type") in ("primary", "filelists", "other"):
            problems.append(f"{href} isn't gzip")
        if data.get("type") == "primary":
            primary = path
    if primary is None:
        return [*problems, "repomd.xml lists no primary"]
    rpms = {n for n in os.listdir(folder) if n.endswith(".rpm")}
    seen = set()
    with gzip.open(primary) as fh:
        root = ET.parse(fh).getroot()  # noqa: S314 (our own createrepo_c output)
    for package in root.iter(f"{COMMON_NS}package"):
        href = package.find(f"{COMMON_NS}location").get("href", "")  # type: ignore[union-attr]
        checksum = package.find(f"{COMMON_NS}checksum")
        size = package.find(f"{COMMON_NS}size")
        path = os.path.join(folder, *href.split("/"))
        if not os.path.isfile(path):
            problems.append(f"primary lists {href}, which is missing")
        elif (checksum is None or checksum.get("type") != "sha256"
              or checksum.text != sha256(path)
              or size is None or size.get("package") != str(os.path.getsize(path))):
            problems.append(f"{href} doesn't match primary")
        seen.add(href)
    if seen != rpms:
        problems.append(f"{folder} has {sorted(rpms)}, primary lists {sorted(seen)}")
    return problems


def verify(site: str, fingerprint: str | None = FINGERPRINT) -> list[str]:
    """Every check; raises SystemExit with the problems found."""
    deb = os.path.join(site, "deb")
    problems = check_release(deb)
    for arch in RPM_ARCHES:
        problems += check_repodata(os.path.join(site, "rpm", arch))
    keyring = os.path.join(site, f"{KEYRING}.gpg")
    with open(keyring, "rb") as fb:
        binary = fb.read()
    with open(os.path.join(site, f"{KEYRING}.asc"), encoding="ascii") as fh:
        if dearmor(fh.read()) != binary:
            problems.append(f"{KEYRING}.gpg isn't {KEYRING}.asc dearmored")
    keys = fingerprints(binary)
    if len(keys) != 1 or (fingerprint and keys != [fingerprint.upper()]):
        problems.append(f"the keyring holds {keys}, not {fingerprint or 'one key'}")
    for data, signature, kind in metadata_files(site):
        good = gpgv(keyring, signature, "" if kind == "clearsign" else data)
        for signer, algo in good:
            if signer not in keys or algo not in ("8", "10"):  # SHA256, SHA512
                problems.append(f"{signature}: signed by {signer} with hash algorithm {algo}")
    problems += check_rpms(site, keys)
    with open(os.path.join(deb, "dists", SUITE, "InRelease"), encoding="utf-8",
              newline="") as fh:
        signed = fh.read()
    with open(os.path.join(deb, "dists", SUITE, "Release"), encoding="utf-8",
              newline="") as fh:
        if cleartext(signed) != fh.read():
            problems.append("InRelease doesn't sign Release")
    for name, text in (("ser2netweb.sources", SOURCES), ("ser2netweb.repo", REPO_FILE)):
        with open(os.path.join(site, name), encoding="utf-8", newline="") as fh:
            if fh.read() != text.format(base=SITE_URL):
                problems.append(f"{name} isn't the one documented")
    if problems:
        fail("the repository tree has problems:\n  " + "\n  ".join(problems))
    return keys


# --- command line --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("dir")
    p.add_argument("--tag", default="")
    p.add_argument("--repository", default=REPOSITORY)
    p = sub.add_parser("build")
    p.add_argument("dir")
    p.add_argument("site")
    p = sub.add_parser("sign")
    p.add_argument("site")
    key = p.add_mutually_exclusive_group(required=True)
    key.add_argument("--keyring")
    key.add_argument("--test-key", action="store_true")
    p.add_argument("--fingerprint", default=FINGERPRINT)
    p.add_argument("--gnupg-parent", default="", help="where to make gpg's temporary home")
    p = sub.add_parser("verify")
    p.add_argument("site")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--fingerprint", default=FINGERPRINT)
    which.add_argument("--any-key", action="store_true")
    p = sub.add_parser("fingerprint")
    p.add_argument("asc")
    p = sub.add_parser("commands")
    p.add_argument("family", choices=sorted(COMMANDS))
    p.add_argument("--part", choices=("install", "update", "remove"), default="install")
    p.add_argument("--base", default=SITE_URL)
    p.add_argument("--batch", action="store_true")
    p.add_argument("--root", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        tag = args.tag.strip()
        if tag and not TAG.fullmatch(tag):
            fail(f"--tag {tag!r} is not vX.Y.Z")
        listed = fetch(args.dir, tag, args.repository)
        print("releases: " + ", ".join(r["tag"] for r in listed))
    elif args.command == "build":
        releases = build(args.dir, args.site)
        print(f"built {args.site} from " + ", ".join(r["tag"] for r in releases))
    elif args.command == "sign":
        sign(args.site, args.keyring or "", args.test_key, args.fingerprint, args.gnupg_parent)
    elif args.command == "verify":
        keys = verify(args.site, None if args.any_key else args.fingerprint)
        print(f"PASS: {args.site}: every index matches its files, every signature is "
              f"good, every package's too ({', '.join(keys)})")
    elif args.command == "fingerprint":
        with open(args.asc, encoding="ascii") as fh:
            print("\n".join(fingerprints(dearmor(fh.read()))))
    else:
        print("\n".join(commands(args.family, args.part, args.base.rstrip("/"),
                                 batch=args.batch, root=args.root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
