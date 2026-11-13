#!/usr/bin/env python3
"""The sources of the browser libraries in ser2netweb/web/static/, for the Debian package's
debian/missing-sources/ (Debian ships the source of every minified file it distributes;
lintian's source-is-missing):

    python3 packaging/debian/missing_sources.py SOURCE_TREE

SOURCE_TREE is the unpacked source package (the release's tree with debian/ in it). From the
npm releases vendor/npm/package.json records (their sha512 checked against its "integrity",
and the files in ser2netweb/web/static/ against its sha256), it writes:

  debian/missing-sources/htmx.js                htmx.org's dist/htmx.js (htmx is written in
                                                JavaScript: the unminified file is its source)
  debian/missing-sources/xterm.js/              @xterm/xterm's src/ (TypeScript), LICENSE and
                                                package.json
  debian/missing-sources/xterm-addon-fit.js/    the same of @xterm/addon-fit

They are not kept in ser2netweb's repository (about 1 MB); the CI build
(.github/workflows/distro-packages.yml) and the maintainer, before an upload, make them
with this script. Standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request

REGISTRY = "https://registry.npmjs.org/{name}/-/{base}-{version}.tgz"
# npm package -> (what goes into debian/missing-sources, the one file of the package it is,
# or "" for the package's src/ tree with its LICENSE and package.json)
WANT = {
    "htmx.org": ("htmx.js", "package/dist/htmx.js"),
    "@xterm/xterm": ("xterm.js", ""),
    "@xterm/addon-fit": ("xterm-addon-fit.js", ""),
}
TREE_FILES = ("package/LICENSE", "package/package.json")


def fetch(name: str, version: str, integrity: str) -> bytes:
    url = REGISTRY.format(name=name, base=name.rsplit("/", 1)[-1], version=version)
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (a fixed https:// URL)
        data = r.read()
    algo, _, want = integrity.partition("-")
    if algo != "sha512" or base64.b64encode(hashlib.sha512(data).digest()).decode() != want:
        raise SystemExit(f"{url}: not the release vendor/npm/package.json records ({integrity})")
    return data


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit(__doc__)
    tree = os.path.abspath(argv[0])
    with open(os.path.join(tree, "vendor", "npm", "package.json"), encoding="utf-8") as fh:
        record = json.load(fh)["ser2net"]
    out = os.path.join(tree, "debian", "missing-sources")
    if not os.path.isdir(os.path.dirname(out)):
        raise SystemExit(f"{tree} has no debian/")
    tarballs: dict[str, tarfile.TarFile] = {}
    for name, (target, member) in WANT.items():
        pkg = record["packages"][name]
        tarballs[name] = tarfile.open(fileobj=io.BytesIO(
            fetch(name, pkg["version"], pkg["integrity"])), mode="r:gz")
        path = os.path.join(out, target)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        tar = tarballs[name]
        if member:
            members = [tar.getmember(member)]
            strip = os.path.dirname(member) + "/"
        else:
            members = [m for m in tar.getmembers()
                       if m.isfile() and (m.name.startswith("package/src/") or m.name in TREE_FILES)]
            strip = "package/"
            if not any(m.name.startswith("package/src/") for m in members):
                raise SystemExit(f"{name} {pkg['version']}: no src/ in the npm package")
        for m in members:
            rel = m.name[len(strip):]
            if not m.isfile() or rel.startswith("/") or ".." in rel.split("/"):
                raise SystemExit(f"{name}: unexpected member {m.name}")
            dest = path if member else os.path.join(path, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            source = tar.extractfile(m)
            if source is None:
                raise SystemExit(f"{name}: cannot read {m.name}")
            with open(dest, "wb") as fh:
                fh.write(source.read())
        print(f"debian/missing-sources/{target}: {name} {pkg['version']} "
              f"({len(members)} file{'s' if len(members) != 1 else ''})")
    # the static files are the npm releases' own (what the sources above are of)
    for path, entry in record["files"].items():
        if entry["package"] not in tarballs:
            raise SystemExit(f"{path}: from {entry['package']}, which has no source here")
        theirs = tarballs[entry["package"]].extractfile("package/" + entry["path"])
        if theirs is None:
            raise SystemExit(f"{entry['package']}: cannot read {entry['path']}")
        with open(os.path.join(tree, path), "rb") as fh:
            ours = fh.read()
        if hashlib.sha256(ours).hexdigest() != entry["sha256"] or ours != theirs.read():
            raise SystemExit(f"{path} is not {entry['package']}'s {entry['path']}")
    print("the files in ser2netweb/web/static/ are those releases' own")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
