"""Move the nixpkgs, Void Linux, Gentoo (GURU), FreeBSD ports and MacPorts recipes of
ser2netweb (packaging/nix, packaging/void, packaging/gentoo, packaging/freebsd,
packaging/macports) to a release: its version and its source tarball's checksums.

    python3 tools/bump_recipes_2.py X.Y.Z [--tarball FILE] [--dry-run | --check]

Every recipe builds the release's git tag tarball, GitHub's archive of the tag vX.Y.Z
(https://github.com/haliskilic/ser2netweb/archive/refs/tags/vX.Y.Z.tar.gz; the PyPI sdist has
no tests). The tag has to exist: the tarball is downloaded (or read from --tarball, a copy of
it) and each recipe gets the checksums its distribution records:

  * nixpkgs (package.nix): fetchFromGitHub's hash, the SHA-256 of the NAR serialisation of
    the unpacked tree without its top folder, as an SRI string (sha256-<base64>);
  * Void (srcpkgs/ser2netweb/template): checksum=, the SHA-256 of the file;
  * Gentoo (net-misc/ser2netweb): the ebuild renamed to the new version, the Manifest's DIST
    line (size, BLAKE2B, SHA512);
  * FreeBSD (net/ser2netweb): DISTVERSION, PORTREVISION dropped, distinfo (TIMESTAMP, SHA256,
    SIZE);
  * MacPorts (net/ser2netweb/Portfile): github.setup's version, revision 0, checksums
    (rmd160, sha256, size).

The revisions start again (Void revision=1, FreeBSD without PORTREVISION, MacPorts revision 0).
The dependency recipes next to them (pyserial-asyncio-fast and the like) keep their own
versions. --dry-run prints the checksums and changes nothing; --check compares the recipes
with what they would be (distinfo's TIMESTAMP aside) and exits 1 when one differs, which
distro-packages-2.yml runs against the tag's real tarball. Afterwards: git diff, then
`python3 tests/test_distro_recipes_2.py`; CI's distro-packages-2.yml builds each recipe.
Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import glob
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "haliskilic/ser2netweb"
PACKAGING = os.path.join(ROOT, "packaging")
NIX = os.path.join(PACKAGING, "nix", "package.nix")
VOID = os.path.join(PACKAGING, "void", "srcpkgs", "ser2netweb", "template")
GENTOO = os.path.join(PACKAGING, "gentoo", "net-misc", "ser2netweb")
FREEBSD = os.path.join(PACKAGING, "freebsd", "net", "ser2netweb")
MACPORTS = os.path.join(PACKAGING, "macports", "net", "ser2netweb", "Portfile")
_VERSION = re.compile(r"\d+\.\d+\.\d+")  # a release (no distribution packages a pre-release)
_MAX_TARBALL = 64 * 1024 * 1024


def tarball_url(version: str) -> str:
    return f"https://github.com/{REPO}/archive/refs/tags/v{version}.tar.gz"


def distfiles(version: str) -> dict[str, str]:
    """The name each distribution saves the tarball under."""
    return {"gentoo": f"ser2netweb-{version}.gh.tar.gz",
            "freebsd": f"haliskilic-ser2netweb-v{version}_GH0.tar.gz",  # (USE_GITHUB's)
            "macports": f"ser2netweb-{version}.tar.gz"}


def download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SystemExit(f"{url}: not an https:// URL")
    headers = {"User-Agent": "ser2netweb-bump-recipes"}
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (https only)
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (https only)
        data = resp.read(_MAX_TARBALL + 1)
    if len(data) > _MAX_TARBALL:
        raise SystemExit(f"{url}: larger than {_MAX_TARBALL} bytes")
    return data


# --- fetchFromGitHub's hash: the NAR serialisation of the unpacked tree --------------------

def _nar_str(data: bytes) -> bytes:
    return struct.pack("<Q", len(data)) + data + b"\0" * (-len(data) % 8)


def _nar_node(node, out: list[bytes]) -> None:
    out.append(_nar_str(b"("))
    kind = node[0]
    if kind == "dir":
        out += [_nar_str(b"type"), _nar_str(b"directory")]
        for name in sorted(node[1], key=lambda n: n.encode()):
            out += [_nar_str(b"entry"), _nar_str(b"("), _nar_str(b"name"),
                    _nar_str(name.encode()), _nar_str(b"node")]
            _nar_node(node[1][name], out)
            out.append(_nar_str(b")"))
    elif kind == "file":
        out += [_nar_str(b"type"), _nar_str(b"regular")]
        if node[2]:
            out += [_nar_str(b"executable"), _nar_str(b"")]
        out += [_nar_str(b"contents"), _nar_str(node[1])]
    else:  # symlink
        out += [_nar_str(b"type"), _nar_str(b"symlink"), _nar_str(b"target"),
                _nar_str(node[1].encode())]
    out.append(_nar_str(b")"))


def nar_sri(data: bytes) -> str:
    """The hash fetchFromGitHub (fetchzip) checks: the tarball unpacked, its one top folder
    stripped, serialised as a NAR (a file is executable when its owner may execute it, as
    the Nix store keeps it), SHA-256, as sha256-<base64>. Read in memory: nothing is
    written to disk, and a member outside the top folder is refused."""
    root: tuple = ("dir", {})
    top = None

    def folder(parts: list[str]) -> dict:
        here = root[1]
        for part in parts:
            node = here.setdefault(part, ("dir", {}))
            if node[0] != "dir":
                raise ValueError(f"{'/'.join(parts)}: not a folder in the tarball")
            here = node[1]
        return here

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        contents = {}
        for m in members:
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if not parts or ".." in parts:
                raise ValueError(f"{m.name!r}: not a path inside the tarball's folder")
            if top is None:
                top = parts[0]
            if parts[0] != top:
                raise ValueError(f"{m.name!r}: outside the top folder {top}/")
            rel = parts[1:]
            if not rel:
                continue
            if m.isdir():
                folder(rel)
            elif m.isreg():
                fh = tar.extractfile(m)
                body = fh.read() if fh else b""
                contents["/".join(rel)] = body
                folder(rel[:-1])[rel[-1]] = ("file", body, bool(m.mode & 0o100))
            elif m.issym():
                folder(rel[:-1])[rel[-1]] = ("link", m.linkname)
            elif m.islnk():  # (git archive makes none; a hard link is a copy for Nix)
                target = "/".join(p for p in m.linkname.split("/")[1:] if p)
                folder(rel[:-1])[rel[-1]] = ("file", contents[target], bool(m.mode & 0o100))
            else:
                raise ValueError(f"{m.name!r}: not a file, folder or symbolic link")
    out = [_nar_str(b"nix-archive-1")]
    _nar_node(root, out)
    digest = hashlib.sha256(b"".join(out)).digest()
    return "sha256-" + base64.b64encode(digest).decode()


def rmd160(data: bytes) -> str:
    try:
        return hashlib.new("ripemd160", data).hexdigest()
    except ValueError:  # (an OpenSSL 3 without its legacy provider)
        out = subprocess.run(["openssl", "dgst", "-rmd160", "-r"], input=data,  # noqa: S607
                             capture_output=True, check=True)
        return out.stdout.split()[0].decode()


def checksums(data: bytes) -> dict[str, object]:
    return {"size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "sha512": hashlib.sha512(data).hexdigest(),
            "blake2b": hashlib.blake2b(data).hexdigest(),
            "rmd160": rmd160(data),
            "nar": nar_sri(data)}


# --- the recipes ---------------------------------------------------------------------------

def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _sub(text: str, pattern: str, repl: str, path: str, count: int = 1) -> str:
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n != count:
        raise SystemExit(f"{os.path.relpath(path, ROOT)}: expected {count} match(es) of "
                         f"{pattern!r}, found {n}")
    return new


def nix(text: str, version: str, sums: dict) -> str:
    text = _sub(text, r'^(  version = ")[^"]*(";)$', rf"\g<1>{version}\g<2>", NIX)
    # the src's hash (fetchFromGitHub's is the only one in the file)
    return _sub(text, r'^(    hash = ")sha256-[^"]*(";)$', rf"\g<1>{sums['nar']}\g<2>", NIX)


def void(text: str, version: str, sums: dict) -> str:
    text = _sub(text, r"^version=.*$", f"version={version}", VOID)
    text = _sub(text, r"^revision=.*$", "revision=1", VOID)
    return _sub(text, r"^checksum=.*$", f"checksum={sums['sha256']}", VOID)


def gentoo_manifest(text: str, version: str, sums: dict) -> str:
    line = (f"DIST {distfiles(version)['gentoo']} {sums['size']} BLAKE2B {sums['blake2b']} "
            f"SHA512 {sums['sha512']}")
    return _sub(text, r"^DIST ser2netweb-\S+\.gh\.tar\.gz .*$", line, GENTOO)


def freebsd_makefile(text: str, version: str) -> str:
    text = _sub(text, r"^(DISTVERSION=\t)\S+$", rf"\g<1>{version}", FREEBSD)
    return re.sub(r"(?m)^PORTREVISION=.*\n", "", text)


def freebsd_distinfo(version: str, sums: dict, now: int) -> str:
    name = distfiles(version)["freebsd"]
    return (f"TIMESTAMP = {now}\n"
            f"SHA256 ({name}) = {sums['sha256']}\n"
            f"SIZE ({name}) = {sums['size']}\n")


def macports(text: str, version: str, sums: dict) -> str:
    text = _sub(text, r"^(github\.setup\s+haliskilic ser2netweb )\S+( v)$",
                rf"\g<1>{version}\g<2>", MACPORTS)
    text = _sub(text, r"^(revision\s+)\d+$", r"\g<1>0", MACPORTS)
    return _sub(text,
                r"^(checksums\s+)rmd160  [0-9a-f]+ \\\n(\s+)sha256  [0-9a-f]+ \\\n"
                r"(\s+)size    \d+$",
                rf"\g<1>rmd160  {sums['rmd160']} \\\n\g<2>sha256  {sums['sha256']} \\\n"
                rf"\g<3>size    {sums['size']}", MACPORTS)


def _untimed(text: str) -> str:
    return re.sub(r"(?m)^TIMESTAMP = \d+\n", "", text)


def plan(version: str, sums: dict, now: int) -> tuple[dict[str, str], tuple[str, str]]:
    """{path: new text} and the ebuild's (old path, new path)."""
    ebuilds = glob.glob(os.path.join(GENTOO, "ser2netweb-*.ebuild"))
    if len(ebuilds) != 1:
        raise SystemExit(f"{os.path.relpath(GENTOO, ROOT)}: expected one ebuild, found "
                         f"{len(ebuilds)}")
    manifest = os.path.join(GENTOO, "Manifest")
    makefile = os.path.join(FREEBSD, "Makefile")
    files = {NIX: nix(_read(NIX), version, sums),
             VOID: void(_read(VOID), version, sums),
             manifest: gentoo_manifest(_read(manifest), version, sums),
             makefile: freebsd_makefile(_read(makefile), version),
             os.path.join(FREEBSD, "distinfo"): freebsd_distinfo(version, sums, now),
             MACPORTS: macports(_read(MACPORTS), version, sums)}
    return files, (ebuilds[0], os.path.join(GENTOO, f"ser2netweb-{version}.ebuild"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", help="the release, X.Y.Z (its tag vX.Y.Z must exist)")
    ap.add_argument("--tarball", help="a downloaded copy of the tag's tarball")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the checksums only")
    mode.add_argument("--check", action="store_true",
                      help="exit 1 if a recipe differs from what this would write")
    args = ap.parse_args(argv)
    if not _VERSION.fullmatch(args.version):
        ap.error(f"{args.version!r} is not X.Y.Z")
    if args.tarball:
        with open(args.tarball, "rb") as fh:
            data = fh.read()
    else:
        print(f"downloading {tarball_url(args.version)}")
        data = download(tarball_url(args.version))
    sums = checksums(data)
    for key, value in sums.items():
        print(f"{key:8} {value}")
    if args.dry_run:
        return 0
    files, (old_ebuild, new_ebuild) = plan(args.version, sums, int(
        datetime.datetime.now(datetime.timezone.utc).timestamp()))
    if args.check:
        differ = sorted(os.path.relpath(path, ROOT) for path, text in files.items()
                        if _untimed(text) != _untimed(_read(path)))
        if old_ebuild != new_ebuild:
            differ.append(f"{os.path.relpath(old_ebuild, ROOT)} (not {args.version})")
        for name in differ:
            print(f"differs: {name}")
        print("the recipes are those of" if not differ else "NOT the recipes of",
              f"{tarball_url(args.version)}")
        return 1 if differ else 0
    for path, text in files.items():
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {os.path.relpath(path, ROOT)}")
    if old_ebuild != new_ebuild:
        shutil.move(old_ebuild, new_ebuild)
        print(f"renamed {os.path.relpath(old_ebuild, ROOT)} -> "
              f"{os.path.relpath(new_ebuild, ROOT)} (git add both)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
