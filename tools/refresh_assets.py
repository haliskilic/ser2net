"""The browser libraries vendored in ser2netweb/web/static/, from their npm releases.

    python tools/refresh_assets.py            # copy them from the npm packages (network)
    python tools/refresh_assets.py --check    # the files are the ones vendor/npm/package.json records
    python tools/refresh_assets.py --verify   # (network) ... and the npm packages' own files

vendor/npm/package.json is the record: "dependencies" pins each package's version (GitHub's
dependency graph reads it, so an advisory against one raises a Dependabot alert, and
Dependabot proposes updates); its "ser2net" section maps every vendored file to the file
inside the package with its sha256, and names each package's npm integrity (sha512 of the
release tarball) and its license (SPDX id, the license text kept in tools/licenses/npm/).
A refresh downloads each tarball, refuses one whose sha512 differs from what the npm
registry publishes for that version, and copies the listed files and the license text out
of it; nothing in the package runs. tests/test_vendor_assets.py checks the record offline,
tools/third_party.py puts the packages into the executables' SBOM and --licenses text.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "vendor", "npm", "package.json")
REGISTRY = "https://registry.npmjs.org"
LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: str = MANIFEST) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(doc: dict, path: str = MANIFEST) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def packages(doc: dict) -> list[dict]:
    """[{name, version, purl, integrity, license, license_file, files: {dest: path}}]."""
    meta = doc["ser2net"]
    out = []
    for name, version in sorted(doc["dependencies"].items()):
        pkg = meta["packages"].get(name, {})
        files = {dest: f["path"] for dest, f in meta["files"].items() if f["package"] == name}
        out.append({"name": name, "version": version,
                    "purl": "pkg:npm/" + urllib.parse.quote(name, safe="/") + "@" + version,
                    "integrity": pkg.get("integrity", ""), "license": pkg.get("license", ""),
                    "license_file": pkg.get("license_file", ""), "files": files})
    return out


def check(root: str = ROOT, doc: dict | None = None) -> list[str]:
    """Offline: every vendored file has the recorded sha256, and the record is complete."""
    doc = load(os.path.join(root, "vendor", "npm", "package.json")) if doc is None else doc
    meta = doc["ser2net"]
    problems = []
    for dest, f in sorted(meta["files"].items()):
        path = os.path.join(root, dest)
        if f["package"] not in doc["dependencies"]:
            problems.append(f"{dest}: from {f['package']}, which isn't in dependencies")
        if not os.path.isfile(path):
            problems.append(f"{dest}: missing")
            continue
        with open(path, "rb") as fh:
            if sha256_of(fh.read()) != f["sha256"]:
                problems.append(f"{dest}: differs from the recorded sha256")
    for pkg in packages(doc):
        if not pkg["files"]:
            problems.append(f"{pkg['name']}: no vendored file comes from it")
        if not pkg["integrity"].startswith("sha512-") or not pkg["license"]:
            problems.append(f"{pkg['name']}: integrity or license not recorded")
        if pkg["integrity"] and meta["packages"][pkg["name"]].get("version") != pkg["version"]:
            problems.append(f"{pkg['name']}: recorded for {meta['packages'][pkg['name']].get('version')}, "
                            f"pinned at {pkg['version']}")
        lic = os.path.join(root, pkg["license_file"]) if pkg["license_file"] else ""
        if not lic or not os.path.isfile(lic):
            problems.append(f"{pkg['name']}: license text {pkg['license_file'] or '(none)'} missing")
    return problems


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (https:// registry URLs)
        return r.read()


def tarball(name: str, version: str) -> tuple[tarfile.TarFile, str]:
    """The release tarball, checked against the registry's sha512, and that integrity."""
    meta = json.loads(_fetch(f"{REGISTRY}/{urllib.parse.quote(name, safe='@')}/{version}"))
    dist = meta["dist"]
    if not dist["tarball"].startswith(REGISTRY + "/"):
        raise SystemExit(f"{name} {version}: unexpected tarball location {dist['tarball']}")
    data = _fetch(dist["tarball"])
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if integrity != dist["integrity"]:
        raise SystemExit(f"{name} {version}: the tarball's sha512 is not the registry's")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"), integrity


def _member(tar: tarfile.TarFile, path: str) -> bytes | None:
    try:
        f = tar.extractfile("package/" + path)
    except KeyError:
        return None
    return f.read() if f else None


def refresh() -> int:
    doc = load()
    meta = doc["ser2net"]
    for pkg in packages(doc):
        tar, integrity = tarball(pkg["name"], pkg["version"])
        with tar:
            for dest, path in pkg["files"].items():
                data = _member(tar, path)
                if data is None:
                    raise SystemExit(f"{pkg['name']} {pkg['version']} has no {path}")
                with open(os.path.join(ROOT, dest), "wb") as fh:
                    fh.write(data)
                meta["files"][dest]["sha256"] = sha256_of(data)
                print(f"{dest} <- {pkg['name']}@{pkg['version']}/{path}")
            text = next((t for n in LICENSE_NAMES if (t := _member(tar, n)) is not None), None)
            if text is None:
                raise SystemExit(f"{pkg['name']} {pkg['version']} ships no license file")
            lic = os.path.join(ROOT, pkg["license_file"])
            os.makedirs(os.path.dirname(lic), exist_ok=True)
            with open(lic, "wb") as fh:
                fh.write(text.replace(b"\r\n", b"\n"))
        meta["packages"][pkg["name"]].update(version=pkg["version"], integrity=integrity)
    save(doc)
    problems = check()
    for p in problems:
        print(f"error: {p}")
    return 1 if problems else 0


def verify() -> list[str]:
    """Online: each recorded integrity is the registry's, and each vendored file is the
    package's own file."""
    doc = load()
    problems = check(doc=doc)
    for pkg in packages(doc):
        try:
            tar, integrity = tarball(pkg["name"], pkg["version"])
        except SystemExit as e:
            problems.append(str(e))
            continue
        with tar:
            if integrity != pkg["integrity"]:
                problems.append(f"{pkg['name']} {pkg['version']}: recorded integrity is not the registry's")
            for dest, path in pkg["files"].items():
                with open(os.path.join(ROOT, dest), "rb") as fh:
                    if _member(tar, path) != fh.read():
                        problems.append(f"{dest}: not {pkg['name']}@{pkg['version']}/{path}")
    return problems


def main(argv: list[str]) -> int:
    if "--check" in argv or "--verify" in argv:
        found = check() if "--check" in argv else verify()
        for p in found:
            print(f"error: {p}")
        if not found:
            print("ser2netweb/web/static matches vendor/npm/package.json"
                  + (" and the npm packages" if "--verify" in argv else ""))
        return 1 if found else 0
    return refresh()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
