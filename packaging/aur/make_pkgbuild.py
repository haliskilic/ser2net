#!/usr/bin/env python3
"""The AUR package ser2netweb-bin (Arch Linux) of a ser2netweb release:

    python3 packaging/aur/make_pkgbuild.py v3.0.0            # into packaging/aur/ser2netweb-bin
    python3 packaging/aur/make_pkgbuild.py v3.0.0 --out DIR
    python3 packaging/aur/make_pkgbuild.py v3.0.0 --check    # are the files there current?

It writes what the AUR's git repository holds (ssh://aur@aur.archlinux.org/ser2netweb-bin.git):

  PKGBUILD                the release's executables for x86_64 and aarch64
                          (ser2netweb-linux-x64 / -arm64) with each one's
                          THIRD-PARTY-LICENSES.txt, sha256 from the release's SHA256SUMS, and
                          ser2netweb's licence texts at the tag (raw.githubusercontent.com,
                          sha256 of what GitHub serves); installed as the .deb / .rpm
                          (packaging/nfpm.yaml) installs them: /usr/bin/ser2netweb, the unit,
                          sysusers.d and tmpfiles.d, the licences (in
                          /usr/share/licenses/ser2netweb-bin, Arch's place). Not stripped (a
                          PyInstaller executable carries its archive after the ELF part).
  .SRCINFO                what makepkg --printsrcinfo says of it (the AUR reads this)
  ser2netweb.service      the release's ser2netweb-standalone.service (checked against
                          SHA256SUMS) made into the .deb's unit (tools/build_packages.py
                          unit()), with Arch's group of the serial ports, uucp, instead of
                          dialout (Arch has no dialout group)
  ser2netweb.sysusers     the tag's packaging/sysusers.conf, the account ser2netweb
  ser2netweb.tmpfiles     the tag's packaging/tmpfiles.conf, /var/lib/ser2netweb (0700)
  ser2netweb-bin.install  what to run after installing (nothing enables or starts it)

No provides / conflicts with the classic ser2net daemon's package: they are unrelated and
install side by side (only with ser2netweb itself). Everything comes from the release and
its tag, whichever commit this file runs from (.github/workflows/aur.yml runs main's for any
release). Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_packages

REPO = "haliskilic/ser2netweb"
URL = f"https://github.com/{REPO}"
RELEASE = URL + "/releases/download/{tag}/{name}"
RAW = f"https://raw.githubusercontent.com/{REPO}" + "/{tag}/{name}"
PKGNAME = "ser2netweb-bin"
OUT = os.path.join(HERE, PKGNAME)
# makepkg's CARCH -> the release's executable
ARCHES = {"x86_64": "ser2netweb-linux-x64", "aarch64": "ser2netweb-linux-arm64"}
# ser2netweb's licence texts (as the .deb has them), from the tag
LICENCES = ("LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "THIRD-PARTY-NOTICES.md")
LOCAL = ("ser2netweb.service", "ser2netweb.sysusers", "ser2netweb.tmpfiles")
UNIT_ASSET = "ser2netweb-standalone.service"
ACCOUNT_FILES = {"ser2netweb.sysusers": "packaging/sysusers.conf",
                 "ser2netweb.tmpfiles": "packaging/tmpfiles.conf"}
INSTALL = f"{PKGNAME}.install"
TAG = re.compile(r"v(\d+\.\d+\.\d+)")  # a release, not a pre-release
PKGDESC = ("Serial-to-network bridge with a web UI: raw TCP, telnet, RFC 2217, UDP, Modbus, "
           "MQTT (standalone executable)")
# the executable needs glibc and zlib (readelf -d: libc, libdl, libpthread, libz)
DEPENDS = ("glibc", "zlib")

UNIT_HEADER = """\
# The ser2netweb-bin package's service (Arch Linux, AUR): the standalone executable
# /usr/bin/ser2netweb with its data in /var/lib/ser2netweb, as the account ser2netweb the
# package created. Installing the package doesn't start it; to run it now and at boot:
#   sudo systemctl enable --now ser2netweb
# An upgrade of the package replaces this file: put changes into a drop-in
# (sudo systemctl edit ser2netweb), which stays. Made from
# systemd/ser2netweb-standalone.service by packaging/aur/make_pkgbuild.py: the .deb's unit
# with uucp, Arch's group of the serial ports, instead of dialout.
"""
INSTALL_SCRIPT = """\
post_install() {
  echo "==> ser2netweb: to run the service now and at boot: sudo systemctl enable --now ser2netweb"
  echo "    (the web UI on 127.0.0.1:8080; the first admin password:"
  echo "    sudo -u ser2netweb ser2netweb --set-password admin)"
}

post_upgrade() {
  echo "==> ser2netweb: a running service keeps the old version until: sudo systemctl try-restart ser2netweb"
}
"""


class Error(Exception):
    """Why the package can't be made."""


Fetch = Callable[[str], bytes]


def fetch(url: str) -> bytes:
    if not url.startswith("https://"):
        raise Error(f"not an https:// address: {url}")
    request = urllib.request.Request(  # noqa: S310 (https:// only)
        url, headers={"User-Agent": "ser2netweb-make-pkgbuild"})
    try:
        with urllib.request.urlopen(request, timeout=120) as r:  # noqa: S310 (https:// URLs)
            data: bytes = r.read()
            return data
    except urllib.error.URLError as e:
        raise Error(f"{url}: {e}") from None


def unit(standalone: Optional[str] = None) -> str:
    """The unit: the .deb's, made from `standalone` (a ser2netweb-standalone.service; default
    this checkout's), with the header above and the group uucp."""
    text = build_packages.unit(standalone)
    for old, new in ((build_packages.PACKAGE_HEADER, UNIT_HEADER),
                     ("\nSupplementaryGroups=dialout\n", "\nSupplementaryGroups=uucp\n"),
                     ("a member of the 'dialout' group\n",
                      "a member of the 'uucp' group (Arch's; others call it dialout)\n")):
        if text.count(old) != 1:
            raise Error(f"the .deb's unit has no single {old.strip()[:60]!r}")
        text = text.replace(old, new)
    return text


def sha256sums(text: str) -> dict[str, str]:
    """name -> sha256 of a SHA256SUMS file (sha256sum's format)."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-f]{64}) [ *](\S+)", line.strip())
        if m:
            out[m.group(2)] = m.group(1)
    return out


def sources(version: str) -> tuple[list[tuple[str, str]], dict[str, list[tuple[str, str]]]]:
    """(local name, url) of the sources: for every architecture, and per architecture.
    The downloads get the version in their name, so makepkg's cache never mixes two."""
    tag = "v" + version
    common = [(name, name) for name in LOCAL]
    for name in LICENCES:
        stem, dot, ext = name.partition(".")
        common.append((f"{stem}-{version}{dot}{ext}", RAW.format(tag=tag, name=name)))
    per_arch = {}
    for arch, exe in ARCHES.items():
        per_arch[arch] = [(f"{exe}-{version}", RELEASE.format(tag=tag, name=exe)),
                          (f"{exe}-{version}.THIRD-PARTY-LICENSES.txt",
                           RELEASE.format(tag=tag, name=f"{exe}.THIRD-PARTY-LICENSES.txt"))]
    return common, per_arch


def _source_entry(local: str, url: str, version: str) -> str:
    """A source= entry as the PKGBUILD writes it (with $pkgver, $_release, $_raw)."""
    if local == url:
        return local
    tag = "v" + version
    url = (url.replace(RELEASE.format(tag=tag, name=""), "$_release/")
              .replace(RAW.format(tag=tag, name=""), "$_raw/"))
    return f"{local.replace(version, '$pkgver')}::{url}"


def _array(name: str, values: list[str]) -> str:
    pad = " " * (len(name) + 2)
    return f"{name}=(" + f"\n{pad}".join(f"'{v}'" if "$" not in v else f'"{v}"'
                                         for v in values) + ")\n"


def render(version: str, sums: dict[str, str], pkgrel: int = 1) -> tuple[str, str]:
    """(PKGBUILD, .SRCINFO) of `version`. `sums`: the sha256 of every source by its local
    name (sources())."""
    common, per_arch = sources(version)
    missing = [local for local, _url in common + [s for v in per_arch.values() for s in v]
               if local not in sums]
    if missing:
        raise Error(f"no sha256 for {', '.join(missing)}")
    licence_dir = "$pkgdir/usr/share/licenses/$pkgname"
    lines = [
        "# Maintainer: Halis KILIÇ <mail at haliskilic dot com dot tr>",
        f"# Made by packaging/aur/make_pkgbuild.py of {REPO}; change it there.",
        f"pkgname={PKGNAME}",
        f"pkgver={version}",
        f"pkgrel={pkgrel}",
        f"pkgdesc='{PKGDESC}'",
        "arch=('x86_64' 'aarch64')",
        f"url='{URL}'",
        "license=('AGPL-3.0-only')",
        "depends=(" + " ".join(f"'{d}'" for d in DEPENDS) + ")",
        "provides=('ser2netweb')",
        "conflicts=('ser2netweb')",
        "# a PyInstaller executable: its archive follows the ELF part, which strip would cut off",
        "options=('!strip' '!debug')",
        f"install={INSTALL}",
        '_release="$url/releases/download/v$pkgver"',
        f'_raw="https://raw.githubusercontent.com/{REPO}/v$pkgver"',
    ]
    text = "\n".join(lines) + "\n"
    text += _array("source", [_source_entry(loc, url, version) for loc, url in common])
    for arch in ARCHES:
        text += _array(f"source_{arch}",
                       [_source_entry(loc, url, version) for loc, url in per_arch[arch]])
    text += _array("sha256sums", [sums[loc] for loc, _url in common])
    for arch in ARCHES:
        text += _array(f"sha256sums_{arch}", [sums[loc] for loc, _url in per_arch[arch]])
    text += f"""
package() {{
  local exe=ser2netweb-linux-x64
  [[ $CARCH == aarch64 ]] && exe=ser2netweb-linux-arm64
  install -Dm755 "$exe-$pkgver" "$pkgdir/usr/bin/ser2netweb"
  install -Dm644 ser2netweb.service "$pkgdir/usr/lib/systemd/system/ser2netweb.service"
  install -Dm644 ser2netweb.sysusers "$pkgdir/usr/lib/sysusers.d/ser2netweb.conf"
  install -Dm644 ser2netweb.tmpfiles "$pkgdir/usr/lib/tmpfiles.d/ser2netweb.conf"
  install -Dm644 "LICENSE-$pkgver" "{licence_dir}/LICENSE"
  install -Dm644 "NOTICE-$pkgver" "{licence_dir}/NOTICE"
  install -Dm644 "COMMERCIAL_LICENSE-$pkgver.md" "{licence_dir}/COMMERCIAL_LICENSE.md"
  install -Dm644 "THIRD-PARTY-NOTICES-$pkgver.md" "{licence_dir}/THIRD-PARTY-NOTICES.md"
  install -Dm644 "$exe-$pkgver.THIRD-PARTY-LICENSES.txt" "{licence_dir}/THIRD-PARTY-LICENSES.txt"
}}
"""
    # .SRCINFO, in makepkg --printsrcinfo's order
    info = [f"pkgbase = {PKGNAME}", f"\tpkgdesc = {PKGDESC}", f"\tpkgver = {version}",
            f"\tpkgrel = {pkgrel}", f"\turl = {URL}", f"\tinstall = {INSTALL}",
            *(f"\tarch = {a}" for a in ARCHES), "\tlicense = AGPL-3.0-only",
            *(f"\tdepends = {d}" for d in DEPENDS), "\tprovides = ser2netweb",
            "\tconflicts = ser2netweb", "\toptions = !strip", "\toptions = !debug"]
    info += [f"\tsource = {_srcinfo_source(loc, url)}" for loc, url in common]
    info += [f"\tsha256sums = {sums[loc]}" for loc, _url in common]
    for arch in ARCHES:
        info += [f"\tsource_{arch} = {_srcinfo_source(loc, url)}" for loc, url in per_arch[arch]]
        info += [f"\tsha256sums_{arch} = {sums[loc]}" for loc, _url in per_arch[arch]]
    srcinfo = "\n".join(info) + f"\n\npkgname = {PKGNAME}\n"
    return text, srcinfo


def _srcinfo_source(local: str, url: str) -> str:
    return local if local == url else f"{local}::{url}"


def make(tag: str, get: Fetch = fetch, sums_text: Optional[str] = None) -> dict[str, str]:
    """Every file of the AUR repository for release `tag` (vX.Y.Z)."""
    m = TAG.fullmatch(tag)
    if not m:
        raise Error(f"{tag!r} is not a release tag (vX.Y.Z)")
    version = m.group(1)
    if sums_text is None:
        sums_text = get(RELEASE.format(tag=tag, name="SHA256SUMS")).decode("utf-8")
    released = sha256sums(sums_text)
    if UNIT_ASSET not in released:
        raise Error(f"the release's SHA256SUMS has no {UNIT_ASSET}")
    standalone = get(RELEASE.format(tag=tag, name=UNIT_ASSET))
    if hashlib.sha256(standalone).hexdigest() != released[UNIT_ASSET]:
        raise Error(f"{UNIT_ASSET} doesn't match the release's SHA256SUMS")
    # (the files of the AUR repository besides PKGBUILD and .SRCINFO)
    files = {"ser2netweb.service": unit(standalone.decode("utf-8")), INSTALL: INSTALL_SCRIPT}
    for name, rel in ACCOUNT_FILES.items():
        files[name] = get(RAW.format(tag=tag, name=rel)).decode("utf-8")
    common, per_arch = sources(version)
    sums = {}
    for loc, url in common:
        if loc in files:
            sums[loc] = hashlib.sha256(files[loc].encode("utf-8")).hexdigest()
        else:  # a licence text at the tag
            sums[loc] = hashlib.sha256(get(url)).hexdigest()
    for loc, url in (s for v in per_arch.values() for s in v):
        name = url.rsplit("/", 1)[1]
        if name not in released:
            raise Error(f"the release's SHA256SUMS has no {name}")
        sums[loc] = released[name]
    pkgbuild, srcinfo = render(version, sums)
    return dict(files, **{"PKGBUILD": pkgbuild, ".SRCINFO": srcinfo})


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("tag", help="the release tag, vX.Y.Z")
    p.add_argument("--out", default=OUT, help=f"the folder to write to (default: {OUT})")
    p.add_argument("--sums", help="the release's SHA256SUMS, already downloaded")
    p.add_argument("--check", action="store_true",
                   help="change nothing; exit 1 when a file in --out differs")
    args = p.parse_args(argv)
    try:
        sums_text = None
        if args.sums:
            with open(args.sums, encoding="utf-8") as fh:
                sums_text = fh.read()
        files = make(args.tag, sums_text=sums_text)
    except Error as e:
        print(f"make_pkgbuild: {e}", file=sys.stderr)
        return 1
    stale = []
    for name, text in sorted(files.items()):
        path = os.path.join(args.out, name)
        if args.check:
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    if fh.read() == text:
                        continue
            except FileNotFoundError:
                pass
            stale.append(path)
            continue
        os.makedirs(args.out, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(path)
    if stale:
        print("make_pkgbuild: not current (run python3 packaging/aur/make_pkgbuild.py "
              f"{args.tag}): {', '.join(stale)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
