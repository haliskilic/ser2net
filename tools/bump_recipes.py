#!/usr/bin/env python3
"""Move the distribution recipes in packaging/ to a release of ser2netweb:

    python3 tools/bump_recipes.py 3.0.1                  # the tag v3.0.1's tarball, from GitHub
    python3 tools/bump_recipes.py 3.0.1 --tarball FILE   # a tarball at hand (CI: git archive)
    python3 tools/bump_recipes.py --check                # anything out of date? (offline)

What it changes (docs/PACKAGING.md, "Recipes for distributions"):

  packaging/debian/debian/changelog      a new entry <version>+ds-1 ("New upstream release")
  packaging/debian/debian/copyright      the versions of the browser libraries it names
  packaging/fedora/ser2netweb.spec       Version, Release 1, the browser libraries' versions
                                         (its npm sources, bundled() provides), %changelog
  packaging/opensuse/ser2netweb.spec     Version, Release 0, the bundled() versions
  packaging/opensuse/ser2netweb.changes  a new entry
  packaging/arch/ser2netweb/PKGBUILD     pkgver, pkgrel 1, sha256sums of the tarball and of the
                                         files beside it
  packaging/arch/ser2netweb/.SRCINFO     what makepkg --printsrcinfo says of that PKGBUILD
  packaging/alpine/ser2netweb/APKBUILD   pkgver, pkgrel 0, sha512sums

The tarball is GitHub's archive of the tag (the recipes' Source), the browser libraries'
versions are those of its vendor/npm/package.json. With the version the recipes already
have, only the checksums (and versions that drifted) change, no changelog entry. A changed
dependency, a new file or test is not this tool's: the recipes are edited by hand then, and
tests/test_distro_recipes.py checks them against pyproject.toml and the repository's unit.
--check changes nothing and fails when a file would change (the tarball's checksums are
left out: no download). Not run by release.yml; the maintainer runs it after a release and
commits the result. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import io
import json
import os
import re
import shlex
import sys
import tarfile
import urllib.request
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(ROOT, "packaging")
TARBALL_URL = "https://github.com/haliskilic/ser2netweb/archive/refs/tags/v{version}.tar.gz"
VERSION = re.compile(r"\d+\.\d+\.\d+")  # a release (distributions package no pre-releases)
DEBIAN_SUFFIX = "+ds"  # the tarball repacked without vendor/wheels (debian/copyright)

DEBIAN_CHANGELOG = "packaging/debian/debian/changelog"
DEBIAN_COPYRIGHT = "packaging/debian/debian/copyright"
FEDORA_SPEC = "packaging/fedora/ser2netweb.spec"
SUSE_SPEC = "packaging/opensuse/ser2netweb.spec"
SUSE_CHANGES = "packaging/opensuse/ser2netweb.changes"
PKGBUILD = "packaging/arch/ser2netweb/PKGBUILD"
SRCINFO = "packaging/arch/ser2netweb/.SRCINFO"
APKBUILD = "packaging/alpine/ser2netweb/APKBUILD"
# the npm packages of the static files, as the recipes name them
NPM = {"htmx.org": "htmx", "@xterm/xterm": "xterm", "@xterm/addon-fit": "xterm_fit"}


def read(rel: str) -> str:
    """A file of the checkout ("" for one that isn't there yet)."""
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def need(m: Optional[re.Match], what: str) -> re.Match:
    """`m`, or stop with `what` (a recipe that is not as this tool knows it)."""
    if m is None:
        raise SystemExit(what)
    return m


def current_version() -> str:
    """The version ser2netweb/__init__.py says (what the recipes should have)."""
    return need(re.search(r'(?m)^__version__ = "([^"]+)"', read("ser2netweb/__init__.py")),
                "ser2netweb/__init__.py has no __version__").group(1)


# ------------------------------------------------------------------ what the recipes say

def debian_version(text: str) -> str:
    """The upstream version of the changelog's newest entry (3.0.0 of 3.0.0+ds-1)."""
    return need(re.match(r"ser2netweb \((\d[^)+~-]*)(?:\+ds\d*)?-[^)]+\) ", text),
                "debian/changelog: no 'ser2netweb (<version>+ds-<revision>)' entry first").group(1)


def spec_field(text: str, name: str) -> str:
    return need(re.search(rf"(?m)^{name}:\s*(\S+)\s*$", text), f"no {name}: line").group(1)


def shell_var(text: str, name: str) -> str:
    """A shell assignment's value (a PKGBUILD's or APKBUILD's name=value, unquoted)."""
    m = need(re.search(rf"(?m)^{name}=(.*)$", text), f"no {name}=")
    return " ".join(shlex.split(m.group(1)))


def shell_array(text: str, name: str) -> list[str]:
    """A PKGBUILD array name=(...) (several lines, quoted words, comments)."""
    m = need(re.search(rf"(?ms)^{name}=\((.*?)\)\s*$", text), f"no {name}=(...)")
    return shlex.split(m.group(1), comments=True)


def apk_list(text: str, name: str) -> list[str]:
    """An APKBUILD list name="..." (several lines)."""
    return need(re.search(rf'(?ms)^{name}="(.*?)"', text), f'no {name}="..."').group(1).split()


def expand(word: str, values: dict[str, str]) -> str:
    """$name and ${name} of a PKGBUILD / APKBUILD word."""
    return re.sub(r"\$\{?(\w+)\}?", lambda m: values[m.group(1)], word)


def sources(text: str, kind: str) -> list[tuple[str, Optional[str]]]:
    """(file name, URL or None for a file beside the recipe) of each source entry."""
    if kind == "arch":
        values = {n: shell_var(text, n) for n in ("pkgname", "pkgver", "url")}
        words = shell_array(text, "source")
    else:
        values = {n: shell_var(text, n) for n in ("pkgname", "pkgver")}
        words = apk_list(text, "source")
    out: list[tuple[str, Optional[str]]] = []
    for word in words:
        word = expand(word, values)
        name, sep, url = word.partition("::")
        if sep:
            out.append((name, url))
        elif "://" in word:
            out.append((word.rsplit("/", 1)[1], word))
        else:
            out.append((word, None))
    return out


def npm_versions(package_json: str) -> dict[str, str]:
    """{recipe name: version} of the browser libraries in a vendor/npm/package.json."""
    packages = json.loads(package_json)["ser2net"]["packages"]
    return {short: packages[name]["version"] for name, short in NPM.items()}


# ------------------------------------------------------------------ the new texts

def _sub(pattern: str, repl: str, text: str, what: str, count: int = 1) -> str:
    new, n = re.subn(pattern, repl, text, flags=re.M)
    if n != count:
        raise SystemExit(f"{what}: {n} matches of {pattern!r}, expected {count}")
    return new


def _maintainer(text: str, pattern: str, what: str) -> str:
    m = re.search(pattern, text, re.M)
    if not m:
        raise SystemExit(f"{what}: no maintainer in its newest entry")
    return m.group(1)


def debian_changelog(text: str, version: str, now: dt.datetime) -> str:
    if debian_version(text) == version:
        return text
    who = _maintainer(text, r"^ -- (.+?)  ", DEBIAN_CHANGELOG)
    entry = (f"ser2netweb ({version}{DEBIAN_SUFFIX}-1) unstable; urgency=medium\n\n"
             "  * New upstream release.\n\n"
             f" -- {who}  {email.utils.format_datetime(now)}\n\n")
    return entry + text


def debian_copyright(text: str, npm: dict[str, str]) -> str:
    text = _sub(r"htmx \d+\.\d+\.\d+", f"htmx {npm['htmx']}", text, DEBIAN_COPYRIGHT)
    text = _sub(r"xterm\.js \d+\.\d+\.\d+", f"xterm.js {npm['xterm']}", text, DEBIAN_COPYRIGHT)
    return _sub(r"@xterm/addon-fit \d+\.\d+\.\d+", f"@xterm/addon-fit {npm['xterm_fit']}",
                text, DEBIAN_COPYRIGHT)


def fedora_spec(text: str, version: str, npm: dict[str, str], now: dt.datetime) -> str:
    for short, v in npm.items():
        text = _sub(rf"^(%global {short}_version) \S+$", rf"\g<1> {v}", text, FEDORA_SPEC)
    if spec_field(text, "Version") == version:
        return text
    text = _sub(r"^(Version:\s*)\S+$", rf"\g<1>{version}", text, FEDORA_SPEC)
    text = _sub(r"^(Release:\s*)\S+$", r"\g<1>1%{?dist}", text, FEDORA_SPEC)
    who = _maintainer(text, r"^%changelog\n\* \w{3} \w{3} [ \d]\d \d{4} (.+?) - ", FEDORA_SPEC)
    entry = f"* {now.strftime('%a %b %d %Y')} {who} - {version}-1\n- Update to {version}\n\n"
    return _sub(r"^%changelog\n", "%changelog\n" + entry.replace("\\", r"\\"), text, FEDORA_SPEC)


def suse_spec(text: str, version: str, npm: dict[str, str]) -> str:
    for name, short in (("js-htmx", "htmx"), ("js-xterm", "xterm"),
                        ("js-xterm-addon-fit", "xterm_fit")):
        text = _sub(rf"^(Provides:\s*bundled\({re.escape(name)}\) = )\S+$",
                    rf"\g<1>{npm[short]}", text, SUSE_SPEC)
    if spec_field(text, "Version") == version:
        return text
    text = _sub(r"^(Version:\s*)\S+$", rf"\g<1>{version}", text, SUSE_SPEC)
    return _sub(r"^(Release:\s*)\S+$", r"\g<1>0", text, SUSE_SPEC)


def suse_changes(text: str, version: str, now: dt.datetime) -> str:
    m = re.search(r"^- (?:Update to version|Initial package of ser2netweb) (\S+?):?$", text, re.M)
    if m and m.group(1) == version:
        return text
    who = _maintainer(text, r"^\w{3} \w{3} [ \d]\d [\d:]{8} UTC \d{4} - (.+)$", SUSE_CHANGES)
    stamp = now.astimezone(dt.timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
    entry = ("-" * 67 + f"\n{stamp} - {who}\n\n"
             f"- Update to version {version}:\n"
             f"  * https://github.com/haliskilic/ser2netweb/blob/v{version}/CHANGELOG.md\n\n")
    return entry + text


def _sums(entries: list[tuple[str, Optional[str]]], folder: str, algo: str,
          tarball: Optional[bytes], old: list[str]) -> list[str]:
    out = []
    for i, (name, url) in enumerate(entries):
        if url is None:
            with open(os.path.join(ROOT, folder, name), "rb") as fh:
                out.append(hashlib.new(algo, fh.read()).hexdigest())
        elif tarball is not None and url.endswith(".tar.gz") and "/archive/refs/tags/v" in url:
            out.append(hashlib.new(algo, tarball).hexdigest())
        elif i < len(old):
            out.append(old[i])  # (--check: the tarball's stays as it is)
        else:
            raise SystemExit(f"{folder}: no checksum for {url}")
    return out


def pkgbuild(text: str, version: str, tarball: Optional[bytes]) -> str:
    if shell_var(text, "pkgver") != version:
        text = _sub(r"^pkgver=.*$", f"pkgver={version}", text, PKGBUILD)
        text = _sub(r"^pkgrel=.*$", "pkgrel=1", text, PKGBUILD)
    sums = _sums(sources(text, "arch"), os.path.dirname(PKGBUILD), "sha256", tarball,
                 shell_array(text, "sha256sums"))
    block = "sha256sums=(" + "\n            ".join(f"'{s}'" for s in sums) + ")"
    return _sub(r"^sha256sums=\([^)]*\)$", block, text, PKGBUILD)


def srcinfo(text: str) -> str:
    """.SRCINFO of a PKGBUILD with one package and no per-architecture arrays, as makepkg
    --printsrcinfo writes it."""
    v = {n: shell_var(text, n) for n in ("pkgname", "pkgver", "pkgrel", "pkgdesc", "url")}
    lines = [f"pkgbase = {v['pkgname']}"]
    for name in ("pkgdesc", "pkgver", "pkgrel"):
        lines.append(f"\t{name} = {v[name]}")
    lines.append(f"\turl = {v['url']}")
    if re.search(r"(?m)^install=", text):
        lines.append(f"\tinstall = {shell_var(text, 'install')}")
    for name in ("arch", "groups", "license", "checkdepends", "makedepends", "depends",
                 "optdepends", "provides", "conflicts", "replaces", "backup", "options"):
        if re.search(rf"(?m)^{name}=\(", text):
            lines += [f"\t{name} = {expand(w, v)}" for w in shell_array(text, name)]
    lines += [f"\tsource = {expand(w, v)}" for w in shell_array(text, "source")]
    lines += [f"\tsha256sums = {s}" for s in shell_array(text, "sha256sums")]
    return "\n".join(lines) + f"\n\npkgname = {v['pkgname']}\n"


def apkbuild(text: str, version: str, tarball: Optional[bytes]) -> str:
    if shell_var(text, "pkgver") != version:
        text = _sub(r"^pkgver=.*$", f"pkgver={version}", text, APKBUILD)
        text = _sub(r"^pkgrel=.*$", "pkgrel=0", text, APKBUILD)
    entries = sources(text, "alpine")
    m = re.search(r'(?ms)^sha512sums="\n(.*?)"', text)
    old = [ln.split()[0] for ln in m.group(1).splitlines() if ln.strip()] if m else []
    sums = _sums(entries, os.path.dirname(APKBUILD), "sha512", tarball, old)
    block = 'sha512sums="\n' + "".join(f"{s}  {name}\n" for s, (name, _u) in
                                         zip(sums, entries, strict=True)) + '"'
    if not m:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    return _sub(r'(?s)^sha512sums="\n.*?"', block, text, APKBUILD)


def planned(version: str, tarball: Optional[bytes], now: dt.datetime,
            npm: Optional[dict[str, str]] = None) -> dict[str, str]:
    """{recipe file: its text for `version`} (tarball None: keep its checksums; npm None:
    the browser libraries of this checkout's vendor/npm/package.json)."""
    if npm is None:
        npm = npm_versions(read("vendor/npm/package.json"))
    out = {
        DEBIAN_CHANGELOG: debian_changelog(read(DEBIAN_CHANGELOG), version, now),
        DEBIAN_COPYRIGHT: debian_copyright(read(DEBIAN_COPYRIGHT), npm),
        FEDORA_SPEC: fedora_spec(read(FEDORA_SPEC), version, npm, now),
        SUSE_SPEC: suse_spec(read(SUSE_SPEC), version, npm),
        SUSE_CHANGES: suse_changes(read(SUSE_CHANGES), version, now),
        APKBUILD: apkbuild(read(APKBUILD), version, tarball),
    }
    out[PKGBUILD] = pkgbuild(read(PKGBUILD), version, tarball)
    out[SRCINFO] = srcinfo(out[PKGBUILD])
    return out


def tarball_npm(data: bytes) -> dict[str, str]:
    """The browser libraries' versions in a release tarball's vendor/npm/package.json."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name.split("/", 1)[-1] == "vendor/npm/package.json" and member.isfile():
                fh = tar.extractfile(member)
                if fh is not None:
                    return npm_versions(fh.read().decode("utf-8"))
    raise SystemExit("the tarball has no vendor/npm/package.json")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("version", nargs="?", help="the release, X.Y.Z (default with --check: "
                    "ser2netweb/__init__.py's)")
    ap.add_argument("--tarball", help="the tag's tarball, instead of a download from GitHub")
    ap.add_argument("--check", action="store_true", help="change nothing; fail if a file would")
    ap.add_argument("--date", help="the changelog entries' time (RFC 2822; default: now)")
    ap.add_argument("--out", help="write the recipe files there (DIR/packaging/...), every one, "
                    "instead of changing the checkout's (CI: tests/distro_build.sh prepare)")
    args = ap.parse_args(argv)
    version = args.version or (current_version() if args.check else None)
    if not version or not VERSION.fullmatch(version):
        ap.error("give the release's version, X.Y.Z")
    now = (email.utils.parsedate_to_datetime(args.date) if args.date
           else dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
    data = npm = None
    if args.tarball:
        with open(args.tarball, "rb") as fh:
            data = fh.read()
    elif not args.check:
        url = TARBALL_URL.format(version=version)
        with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (a fixed https:// URL)
            data = r.read()
        print(f"{url}: {len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()}")
    if data is not None:
        npm = tarball_npm(data)
    changed = []
    for rel, text in planned(version, data, now, npm).items():
        if text != read(rel):
            changed.append(rel)
        if args.check or (args.out is None and text == read(rel)):
            continue
        path = os.path.join(args.out or ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    for rel in changed:
        print(f"{'out of date' if args.check else 'updated'}: {rel}"
              + (f" (in {args.out})" if args.out else ""))
    if args.check and changed:
        print(f"run: python3 tools/bump_recipes.py {version}")
        return 1
    if not changed:
        print(f"the recipes are at {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
