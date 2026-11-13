"""The Windows package managers' files for a release: winget's manifests
(packaging/winget/make_manifests.py), Scoop's manifest (packaging/scoop/ser2netweb.json, also
in the haliskilic/scoop-bucket repository) and Chocolatey's package (packaging/chocolatey).
They take the release's own ser2netweb-windows-x64.exe with its SHA-256 from the release's
SHA256SUMS: nothing is rebuilt.

    python tools/windows_channels.py sha256 vX.Y.Z [--sums FILE]
    python tools/windows_channels.py scoop vX.Y.Z [--sums FILE] [--out FILE]
    python tools/windows_channels.py chocolatey vX.Y.Z [--sums FILE] [--dir DIR]
    python tools/windows_channels.py validate [PATH ...]

`--sums` reads a downloaded SHA256SUMS instead of the release's. `scoop` writes the manifest
(default: packaging/scoop/ser2netweb.json); `chocolatey` sets the version and the download of
the package in packaging/chocolatey (or a copy of that folder). `validate` checks winget
manifests (*.yaml) and Scoop manifests (*.json) against the package managers' JSON schemas,
downloaded from the pinned commits below and checked against their SHA-256 (default: the
committed manifests); it needs jsonschema and PyYAML, everything else the standard library.
.github/workflows/winget.yml and chocolatey.yml run these for a release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "haliskilic/ser2netweb"
HOMEPAGE = f"https://github.com/{REPO}"
ASSET = "ser2netweb-windows-x64.exe"  # the release's Windows executable
COMMAND = "ser2netweb"                # what each package manager puts on PATH
WINGET_ID = "haliskilic.ser2netweb"   # (ser2netweb/layout.py knows its folder by it)
LICENSE = "AGPL-3.0-only"
DISAMBIGUATION = "ser2netweb is an independent project, unrelated to Corey Minyard's ser2net."
SUMMARY = ("Serial-to-network bridge with a web UI: raw TCP, telnet, RFC 2217, UDP, Modbus "
           "gateway, MQTT")
WINDOWS_GUIDE = f"{HOMEPAGE}/blob/main/docs/WINDOWS.md"
_TAG = re.compile(r"v(\d+\.\d+\.\d+)")  # a release (no pre-release: no package manager track)

SCOOP_MANIFEST = os.path.join(ROOT, "packaging", "scoop", "ser2netweb.json")
CHOCOLATEY_DIR = os.path.join(ROOT, "packaging", "chocolatey")
WINGET_DIR = os.path.join(ROOT, "packaging", "winget", "manifests")

# the JSON schemas, by the commit they are taken from and their SHA-256: winget's 1.28.0
# (microsoft/winget-cli, schemas/JSON/manifests/v1.28.0) and Scoop's (ScoopInstaller/Scoop)
_WINGET_SCHEMAS = ("https://raw.githubusercontent.com/microsoft/winget-cli/"
                   "dafe5e7795c6b36e2a8cea86aa9fddec898df795/schemas/JSON/manifests/v1.28.0/")
SCHEMAS = {
    "installer": (_WINGET_SCHEMAS + "manifest.installer.1.28.0.json",
                  "2caa4cd3a630c284b5ef750522e2e5861f751f376180b89cd29873eb30504ef4"),
    "defaultLocale": (_WINGET_SCHEMAS + "manifest.defaultLocale.1.28.0.json",
                      "734c069c82470e3436d274fb381bfbe03e22f8fbfae2d2dd50df7dc055a3e061"),
    "locale": (_WINGET_SCHEMAS + "manifest.locale.1.28.0.json",
               "7d55396c4b88ce2165692d71685eab93189ba24cb20368b3903965d6f91c267a"),
    "version": (_WINGET_SCHEMAS + "manifest.version.1.28.0.json",
                "5c9fa37ebe70dd410114157d31e4d9e84451b2bb8890e1c4345b4c690406ee1a"),
    "scoop": ("https://raw.githubusercontent.com/ScoopInstaller/Scoop/"
              "79669717fe3e53ba642ec073a45bbe53acb77b8e/schema.json",
              "64ec829c70758d3fba2c8c168a5901a64408a6493efda25f172b0fe4505caac0"),
}
WINGET_MANIFEST_VERSION = "1.28.0"


def version_of(tag: str) -> str:
    """X.Y.Z of the release tag vX.Y.Z."""
    m = _TAG.fullmatch(tag)
    if not m:
        raise ValueError(f"not a release tag (vX.Y.Z): {tag!r}")
    return m.group(1)


def release_url(tag: str, name: str = ASSET) -> str:
    """A file of the release `tag`."""
    version_of(tag)
    return f"{HOMEPAGE}/releases/download/{tag}/{name}"


def _download(url: str, limit: int = 1 << 20) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(url)
    req = urllib.request.Request(url, headers={"User-Agent": "ser2netweb-packaging"})  # noqa: S310 (https only, above)
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: larger than {limit} bytes")
    return data


def parse_sums(text: str) -> dict[str, str]:
    """name -> SHA-256 (lower case) of a SHA256SUMS file (`sha256sum` lines; a '*' before the
    name, binary mode, is dropped)."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9A-Fa-f]{64}) [ *](\S.*)", line.strip())
        if m:
            out[m.group(2).strip()] = m.group(1).lower()
    return out


def exe_sha256(tag: str, sums: Optional[str] = None) -> str:
    """The SHA-256 of the release's Windows executable, from its SHA256SUMS (the release's,
    or the file `sums`)."""
    if sums:
        with open(sums, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = _download(release_url(tag, "SHA256SUMS")).decode("utf-8")
    digest = parse_sums(text).get(ASSET)
    if not digest:
        raise ValueError(f"SHA256SUMS of {tag} has no {ASSET}")
    return digest


# ---------------------------------------------------------------- Scoop
def scoop_manifest(tag: str, sha256: str) -> dict[str, Any]:
    """Scoop's manifest (bucket/ser2netweb.json): the release's exe, saved as ser2netweb.exe
    and put on PATH; data\\ next to it is persisted (<scoop>\\persist\\ser2netweb\\data, which
    updates keep: ser2netweb/layout.py keeps a Scoop copy's data there). Scoop's Excavator
    finds new releases on GitHub and takes the hash from their SHA256SUMS."""
    url = release_url(tag) + "#/ser2netweb.exe"
    return {
        "version": version_of(tag),
        "description": f"{SUMMARY}. {DISAMBIGUATION}",
        "homepage": HOMEPAGE,
        "license": {"identifier": LICENSE, "url": f"{HOMEPAGE}/blob/main/LICENSE"},
        "notes": [
            "Data (config.json, backups, logs): $persist_dir\\data, kept by updates; "
            "'scoop uninstall --purge ser2netweb' deletes it.",
            "As a Windows service: 'ser2netweb --install-service' from an elevated prompt, and "
            "again after each update (the service runs its own copy, in "
            "C:\\Program Files\\ser2netweb).",
            f"Guide: {WINDOWS_GUIDE}",
        ],
        "architecture": {"64bit": {"url": url, "hash": sha256.lower()}},
        "bin": "ser2netweb.exe",
        "persist": "data",
        "checkver": {"github": HOMEPAGE},
        "autoupdate": {
            "architecture": {"64bit": {
                "url": f"{HOMEPAGE}/releases/download/v$version/{ASSET}#/ser2netweb.exe"}},
            "hash": {"url": "$baseurl/SHA256SUMS", "regex": "$sha256\\s+$basename"},
        },
    }


def scoop_json(manifest: dict[str, Any]) -> str:
    """The manifest as Scoop's bucket keeps it (4-space indentation, UTF-8)."""
    return json.dumps(manifest, indent=4, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- Chocolatey
NUSPEC = "ser2netweb.nuspec"
INSTALL_SCRIPT = os.path.join("tools", "chocolateyinstall.ps1")


def _replace_once(text: str, pattern: str, new: str, what: str) -> str:
    out, n = re.subn(pattern, lambda _m: new, text, flags=re.M)
    if n != 1:
        raise ValueError(f"{what}: expected one match of {pattern!r}, found {n}")
    return out


def chocolatey_files(tag: str, sha256: str, nuspec: str, install: str) -> tuple[str, str]:
    """The package's ser2netweb.nuspec and tools/chocolateyinstall.ps1 for the release `tag`:
    its version and release notes, the exe's URL and SHA-256."""
    version = version_of(tag)
    nuspec = _replace_once(nuspec, r"<version>[^<]*</version>", f"<version>{version}</version>",
                           NUSPEC)
    nuspec = _replace_once(nuspec, r"<releaseNotes>[^<]*</releaseNotes>",
                           f"<releaseNotes>{HOMEPAGE}/releases/tag/{tag}</releaseNotes>", NUSPEC)
    # (the line's end, also a CRLF one: a Windows checkout with core.autocrlf, kept as it is)
    install = _replace_once(install, r"^\$url64 = '[^']*'(?=\r?$)",
                            f"$url64 = '{release_url(tag)}'", INSTALL_SCRIPT)
    install = _replace_once(install, r"^\$checksum64 = '[^']*'(?=\r?$)",
                            f"$checksum64 = '{sha256.lower()}'", INSTALL_SCRIPT)
    return nuspec, install


def update_chocolatey(tag: str, sha256: str, folder: str = CHOCOLATEY_DIR) -> None:
    paths = (os.path.join(folder, NUSPEC), os.path.join(folder, INSTALL_SCRIPT))
    texts = []
    for path in paths:
        with open(path, encoding="utf-8", newline="") as fh:
            texts.append(fh.read())
    for path, text in zip(paths, chocolatey_files(tag, sha256, *texts), strict=True):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


# ---------------------------------------------------------------- schema validation
def load_schema(name: str) -> dict[str, Any]:
    url, digest = SCHEMAS[name]
    data = _download(url)
    got = hashlib.sha256(data).hexdigest()
    if got != digest:
        raise ValueError(f"{url}: SHA-256 {got}, not the pinned {digest}")
    return json.loads(data)


def manifest_files(paths: list[str]) -> list[str]:
    """The winget (*.yaml) and Scoop (*.json) manifests in these files and folders."""
    out = []
    for path in paths:
        if os.path.isdir(path):
            for folder, _dirs, files in os.walk(path):
                out += [os.path.join(folder, f) for f in sorted(files)
                        if f.endswith((".yaml", ".json"))]
        else:
            out.append(path)
    return sorted(out)


def validate(paths: list[str]) -> list[str]:
    """Problems of these manifests against their schemas ([] when they conform)."""
    import jsonschema
    import yaml

    schemas: dict[str, dict] = {}
    problems = []
    for path in manifest_files(paths):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if path.endswith(".json"):
            kind, doc = "scoop", json.loads(text)
        else:
            if text.startswith("\ufeff"):
                problems.append(f"{path}: a byte order mark")
            doc = yaml.safe_load(text)
            found = doc.get("ManifestType") if isinstance(doc, dict) else None
            if found not in ("installer", "defaultLocale", "locale", "version"):
                problems.append(f"{path}: no known ManifestType ({found!r})")
                continue
            kind = str(found)
            if doc.get("ManifestVersion") != WINGET_MANIFEST_VERSION:
                problems.append(f"{path}: ManifestVersion {doc.get('ManifestVersion')!r}, "
                                f"not {WINGET_MANIFEST_VERSION}")
        if kind not in schemas:
            schemas[kind] = load_schema(kind)
        cls = jsonschema.validators.validator_for(schemas[kind])
        checker = cls(schemas[kind], format_checker=cls.FORMAT_CHECKER)
        for err in sorted(checker.iter_errors(doc), key=lambda e: list(e.absolute_path)):
            where = "/".join(str(p) for p in err.absolute_path) or "(top)"
            problems.append(f"{path}: {where}: {err.message}")
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="windows_channels.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("sha256", "scoop", "chocolatey"):
        cmd = sub.add_parser(name)
        cmd.add_argument("tag", help="the release, vX.Y.Z")
        cmd.add_argument("--sums", help="a downloaded SHA256SUMS of that release")
        if name == "scoop":
            cmd.add_argument("--out", default=SCOOP_MANIFEST)
        if name == "chocolatey":
            cmd.add_argument("--dir", default=CHOCOLATEY_DIR)
    check = sub.add_parser("validate")
    check.add_argument("paths", nargs="*", default=[WINGET_DIR, SCOOP_MANIFEST])
    args = p.parse_args(argv)
    try:
        if args.cmd == "validate":
            files = manifest_files(args.paths)
            problems = validate(files) if files else ["no manifests found"]
            for line in problems:
                print(line, file=sys.stderr)
            if not problems:
                print(f"{len(files)} manifests conform to their schemas")
            return 1 if problems else 0
        digest = exe_sha256(args.tag, args.sums)
        if args.cmd == "sha256":
            print(digest)
        elif args.cmd == "scoop":
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(scoop_json(scoop_manifest(args.tag, digest)))
            print(f"wrote {args.out}")
        else:
            update_chocolatey(args.tag, digest, args.dir)
            print(f"{args.dir}: {version_of(args.tag)}, {ASSET} {digest}")
    except (OSError, ValueError) as e:
        print(f"windows_channels.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
