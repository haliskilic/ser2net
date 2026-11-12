"""Third-party component inventory of a ser2net build, from the build environment.

    python tools/third_party.py licenses OUT.txt [--extra DIR]... [--strict]
    python tools/third_party.py sbom OUT.cdx.json [--binaries LIST] [--strict]

Run it with the interpreter that builds the standalone executable (PyInstaller). It
writes every bundled component's license text into one file, which ser2net.spec
packs into the executable (``ser2net --licenses`` prints it) and the release attaches,
and a CycloneDX SBOM of the same components. Components:
  * ser2net itself (AGPL-3.0-only, or a commercial license),
  * CPython and the native libraries in it: OpenSSL, zlib, and — when the executable
    carries their extension modules (``--binaries``: the list ser2net.spec writes) —
    expat, mpdecimal and SQLite, each with the version the interpreter reports and a CPE
    where NVD has one; the OpenSSL that cryptography links into its own module too,
  * every Python distribution the app imports at run time: the pins in
    requirements.txt + requirements-optional.txt and their dependencies,
  * the PyInstaller bootloader and runtime hooks embedded in the executable,
  * the browser libraries in app/web/static (vendor/npm/package.json, their license
    texts in tools/licenses/npm/),
  * ``--extra DIR``: further license texts (*.txt), e.g. the native libraries
    python-build-standalone links into the interpreter.

Licenses are SPDX expressions where the package's metadata gives one (License-Expression,
or a License field that is one); for the packages whose metadata doesn't, LICENSE_OVERRIDES
names it, tied to the version it was checked at: a newer version falls back to the
metadata's wording (and a warning) until the entry is checked again
(tests/test_third_party.py fails in CI's vendor job while one is out of date).
"""
from __future__ import annotations

import argparse
import importlib
import os
import platform
import re
import sys
import sysconfig
from importlib import metadata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENT_FILES = ("requirements.txt", "requirements-optional.txt")
# embedded in the executable although the app never imports them
BUILD_EMBEDDED = {"pyinstaller": "bootloader + runtime hooks",
                  "pyinstaller-hooks-contrib": "runtime hooks"}
_LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.IGNORECASE)
# license texts for distributions whose wheels ship none (tools/licenses/<name>.txt,
# copied from the project's source tree at the pinned version)
FALLBACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "licenses")
NPM_MANIFEST = os.path.join(ROOT, "vendor", "npm", "package.json")
# SPDX license expressions for the distributions whose metadata has none (no
# License-Expression, a License field that isn't one), checked against their license
# files at this version: {canonical name: (version, expression)}
LICENSE_OVERRIDES = {
    "jinja2": ("3.1.6", "BSD-3-Clause"),            # classifier "BSD License" only
    "ldap3": ("2.9.1", "LGPL-3.0-or-later"),        # "LGPL v3"; its sources say "or later"
    "pyserial": ("3.5", "BSD-3-Clause"),            # "BSD"
    # the bootloader and the pyimod modules: GPL with the bootloader exception (any
    # license for the program it builds); the runtime hooks: Apache-2.0
    "pyinstaller": ("6.22.3", "GPL-2.0-or-later WITH Bootloader-exception AND Apache-2.0"),
    # the part in the executable: its runtime hooks (the build-time hooks are GPL-2.0+)
    "pyinstaller-hooks-contrib": ("2026.10", "Apache-2.0"),
}
# the SPDX ids a License field may consist of to count as an expression
_SPDX_IDS = {"0BSD", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "EPL-2.0", "ISC",
             "LGPL-2.1-or-later", "LGPL-3.0-or-later", "GPL-2.0-or-later", "MIT", "MIT-0",
             "MPL-2.0", "PSF-2.0", "Unlicense", "Zlib", "Bootloader-exception"}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _packaging():
    """packaging's Marker and Requirement: the installed package, else pip's copy."""
    try:
        markers = importlib.import_module("packaging.markers")
        requirements = importlib.import_module("packaging.requirements")
    except ImportError:  # pip always vendors it
        markers = importlib.import_module("pip._vendor.packaging.markers")
        requirements = importlib.import_module("pip._vendor.packaging.requirements")
    return markers.Marker, requirements.Requirement


def _field(md, name: str) -> str | None:
    """A metadata field's first value, None when it is missing."""
    values = md.get_all(name)
    return values[0] if values else None


def top_level_names() -> list[str]:
    """Distribution names pinned in the requirement files whose markers match this
    interpreter."""
    Marker, _ = _packaging()
    names = []
    for fn in REQUIREMENT_FILES:
        with open(os.path.join(ROOT, fn), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                spec, _, marker = line.partition(";")
                if marker.strip() and not Marker(marker.strip()).evaluate():
                    continue
                names.append(re.split(r"[=<>!~ \[]", spec.strip(), maxsplit=1)[0])
    return names


def runtime_distributions(strict: bool = False) -> list[metadata.Distribution]:
    """The installed distributions behind top_level_names(), plus their run-time
    dependencies (markers evaluated, extras ignored). A pinned package that is not
    installed is skipped — or, with strict (release builds), an error."""
    _, Requirement = _packaging()
    seen: dict[str, metadata.Distribution] = {}
    top = {canonical(n) for n in top_level_names()}
    todo = list(top)
    while todo:
        name = canonical(todo.pop())
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            if strict and name in top:
                raise SystemExit(f"{name} is pinned in the requirement files but not installed") from None
            continue
        seen[name] = dist
        for req_s in dist.requires or []:
            req = Requirement(req_s)
            if req.marker is not None and not req.marker.evaluate({"extra": ""}):
                continue
            todo.append(req.name)
    return sorted(seen.values(), key=lambda d: canonical(d.metadata["Name"]))


def is_spdx(text: str) -> bool:
    """Whether text is an SPDX expression made of the ids this tool knows."""
    tokens = text.replace("(", " ( ").replace(")", " ) ").split()
    words = [t for t in tokens if t not in ("(", ")", "AND", "OR", "WITH")]
    return bool(words) and all(w in _SPDX_IDS for w in words)


def license_of(dist: metadata.Distribution, warn=None) -> tuple[str, bool]:
    """(license, whether it is an SPDX expression): the metadata's License-Expression, else
    the checked override for this version, else a License field that is an SPDX
    expression, else the License field's first line, else the trove classifiers."""
    md = dist.metadata
    expr = _field(md, "License-Expression")
    if expr:
        return expr, True
    name = canonical(md["Name"])
    if name in LICENSE_OVERRIDES:
        checked, override = LICENSE_OVERRIDES[name]
        if dist.version == checked:
            return override, True
        (warn or (lambda m: print(m, file=sys.stderr)))(
            f"warning: {name} {dist.version}: its license override was checked at {checked}; "
            "check the license and update LICENSE_OVERRIDES in tools/third_party.py")
    lic = (_field(md, "License") or "").strip()
    if lic and is_spdx(lic):
        return lic, True
    if lic and "\n" not in lic and len(lic) <= 120:
        return lic, False
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers), False
    return (lic.splitlines()[0][:120] if lic else "see license text"), False


def npm_packages() -> list[dict]:
    """The browser libraries vendored in app/web/static (vendor/npm/package.json)."""
    import json

    if not os.path.isfile(NPM_MANIFEST):
        return []
    with open(NPM_MANIFEST, encoding="utf-8") as fh:
        doc = json.load(fh)
    out = []
    for name, version in sorted(doc["dependencies"].items()):
        meta = doc["ser2net"]["packages"][name]
        files = sorted(os.path.basename(d) for d, f in doc["ser2net"]["files"].items()
                       if f["package"] == name)
        scoped = name.replace("@", "%40", 1) if name.startswith("@") else name
        out.append({"name": name, "version": version, "license": meta["license"],
                    "license_file": os.path.join(ROOT, meta["license_file"]),
                    "purl": f"pkg:npm/{scoped}@{version}", "files": files})
    return out


def native_libraries(binaries: list[str] | None) -> list[dict]:
    """The native libraries in the interpreter (and cryptography) that the executable
    carries, with the versions they report. binaries: the executable's binary entries
    (ser2net.spec's list); None: only what every build has (OpenSSL, zlib)."""
    import ssl
    import zlib

    def carried(module: str) -> bool:
        # built into libpython (python-build-standalone on Linux), or a bundled extension
        return binaries is not None and (module in sys.builtin_module_names or any(
            os.path.basename(b).split(".")[0] == module for b in binaries))

    libs = []
    m = re.search(r"OpenSSL (\S+)", ssl.OPENSSL_VERSION)
    if m:
        libs.append({"name": "openssl", "version": m.group(1), "license": "Apache-2.0",
                     "cpe": "cpe:2.3:a:openssl:openssl:{v}:*:*:*:*:*:*:*",
                     "description": "linked into the bundled interpreter (ssl, hashlib)"})
    libs.append({"name": "zlib", "version": zlib.ZLIB_RUNTIME_VERSION, "license": "Zlib",
                 "cpe": "cpe:2.3:a:zlib:zlib:{v}:*:*:*:*:*:*:*",
                 "description": "linked into the bundled interpreter"})
    if carried("pyexpat"):
        import pyexpat
        libs.append({"name": "expat", "version": pyexpat.EXPAT_VERSION.split("_")[-1],
                     "license": "MIT", "cpe": "cpe:2.3:a:libexpat_project:libexpat:{v}:*:*:*:*:*:*:*",
                     "description": "the interpreter's pyexpat module"})
    if carried("_decimal"):
        import decimal
        libs.append({"name": "mpdecimal", "version": getattr(decimal, "__libmpdec_version__", ""),
                     "license": "BSD-2-Clause", "cpe": "",
                     "description": "the interpreter's _decimal module"})
    if carried("_sqlite3"):
        import sqlite3
        libs.append({"name": "sqlite", "version": sqlite3.sqlite_version, "license": "blessing",
                     "cpe": "cpe:2.3:a:sqlite:sqlite:{v}:*:*:*:*:*:*:*",
                     "description": "the interpreter's _sqlite3 module"})
    try:
        crypto = metadata.distribution("cryptography")
        from cryptography.hazmat.backends.openssl import backend
        m = re.search(r"OpenSSL (\S+)", backend.openssl_version_text())
        if m:
            libs.append({"name": "openssl", "version": m.group(1), "license": "Apache-2.0",
                         "cpe": "cpe:2.3:a:openssl:openssl:{v}:*:*:*:*:*:*:*",
                         "description": f"statically linked into cryptography {crypto.version}",
                         "ref": f"pkg:generic/openssl@{m.group(1)}?package=cryptography"})
    except Exception:  # not installed or not importable: not in the build
        pass
    return [lib for lib in libs if lib["version"]]


def license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        p = str(f).replace("\\", "/")
        if ".dist-info/" not in p and ".egg-info/" not in p:
            continue
        if not _LICENSE_FILE.search(p):
            continue
        try:
            out.append((os.path.basename(p), f.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    if not out:
        fallback = os.path.join(FALLBACK_DIR, canonical(dist.metadata["Name"]) + ".txt")
        if os.path.isfile(fallback):
            with open(fallback, encoding="utf-8") as fh:
                out.append(("LICENSE (from the project's source tree)", fh.read()))
    return out


def cpython_license() -> str:
    for cand in (os.path.join(sys.base_prefix, "LICENSE.txt"),
                 os.path.join(sysconfig.get_path("stdlib"), "LICENSE.txt")):
        if os.path.isfile(cand):
            with open(cand, encoding="utf-8", errors="replace") as fh:
                return fh.read()
    raise SystemExit("CPython LICENSE.txt not found next to this interpreter")


def _section(title: str, body: str) -> str:
    bar = "=" * 78
    return f"{bar}\n{title}\n{bar}\n\n{body.rstrip()}\n\n"


def write_licenses(out_path: str, extra_dirs: list[str], strict: bool = False) -> None:
    sys.path.insert(0, ROOT)
    from app import __version__

    import ssl

    parts = [
        "Third-party licenses of the ser2net standalone executable\n"
        f"ser2net {__version__} for {platform.system()} {platform.machine()}, "
        f"built with Python {platform.python_version()}\n\n"
        "ser2net itself is licensed under the GNU AGPL-3.0-only (full text below) or a\n"
        "separate commercial license (COMMERCIAL_LICENSE.md). The components below keep\n"
        "their own licenses. Source code: https://github.com/haliskilic/ser2net\n\n",
    ]
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
        parts.append(_section(f"ser2net {__version__} — AGPL-3.0-only", fh.read()))
    parts.append(_section(
        f"CPython {platform.python_version()} — PSF-2.0 (bundled interpreter; "
        f"{ssl.OPENSSL_VERSION})", cpython_license()))
    dists = runtime_distributions(strict)
    for name, what in BUILD_EMBEDDED.items():
        try:
            dists.append(metadata.distribution(name))
        except metadata.PackageNotFoundError:
            print(f"note: {name} not installed ({what} not covered)", file=sys.stderr)
    for dist in dists:
        name, version = dist.metadata["Name"], dist.version
        role = BUILD_EMBEDDED.get(canonical(name))
        title = f"{name} {version} — {license_of(dist)[0]}" + (f" ({role})" if role else "")
        texts = license_texts(dist)
        if not texts:
            raise SystemExit(f"no license file in the {name} {version} distribution")
        parts.append(_section(title, "\n\n".join(f"--- {fn} ---\n{t}" for fn, t in texts)))
    npm = npm_packages()
    for pkg in npm:
        if not os.path.isfile(pkg["license_file"]):
            raise SystemExit(f"no license text for {pkg['name']}: {pkg['license_file']}")
        with open(pkg["license_file"], encoding="utf-8") as fh:
            parts.append(_section(f"{pkg['name']} {pkg['version']} — {pkg['license']} "
                                  f"(browser library: {', '.join(pkg['files'])})", fh.read()))
    for d in extra_dirs:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".txt"):
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as fh:
                    parts.append(_section(f"{fn[:-4]} (native library in the interpreter)",
                                          fh.read()))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(parts))
    print(f"wrote {out_path}: {len(dists)} Python distributions, {len(npm)} browser libraries, "
          f"{sum(len(os.listdir(d)) for d in extra_dirs)} extra license files")


def write_sbom(out_path: str, strict: bool = False, binaries: list[str] | None = None) -> None:
    """A CycloneDX 1.5 SBOM of the executable: ser2net, the interpreter (CPython) and the
    native libraries in it, every bundled Python distribution, the PyInstaller bootloader
    and the browser libraries."""
    import datetime
    import json
    import uuid

    sys.path.insert(0, ROOT)
    from app import __version__

    def lic(dist: metadata.Distribution) -> list:
        text, spdx = license_of(dist)
        return [{"expression": text}] if spdx else [{"license": {"name": text}}]

    components = []
    py = platform.python_version()
    # CPE ids as well as the purl: CPE-based vulnerability scanners don't match pkg:generic
    # purls, so without these they would miss the interpreter and its libraries entirely.
    components.append({"type": "library", "bom-ref": f"pkg:generic/cpython@{py}", "name": "cpython",
                       "version": py, "purl": f"pkg:generic/cpython@{py}",
                       "cpe": f"cpe:2.3:a:python:python:{py}:*:*:*:*:*:*:*",
                       "licenses": [{"expression": "PSF-2.0"}],
                       "description": "bundled Python interpreter and standard library"})
    for native in native_libraries(binaries):
        purl = f"pkg:generic/{native['name']}@{native['version']}"
        comp = {"type": "library", "bom-ref": native.get("ref", purl), "name": native["name"],
                "version": native["version"], "purl": purl,
                "licenses": [{"expression": native["license"]}],
                "description": native["description"]}
        if native["cpe"]:
            comp["cpe"] = native["cpe"].format(v=native["version"])
        components.append(comp)
    dists = runtime_distributions(strict)
    for name in BUILD_EMBEDDED:
        try:
            dists.append(metadata.distribution(name))
        except metadata.PackageNotFoundError:
            pass
    for dist in dists:
        name, version = canonical(dist.metadata["Name"]), dist.version
        purl = f"pkg:pypi/{name}@{version}"
        comp = {"type": "library", "bom-ref": purl, "name": name, "version": version,
                "purl": purl, "licenses": lic(dist)}
        if name in BUILD_EMBEDDED:
            comp["description"] = BUILD_EMBEDDED[name] + " embedded in the executable"
        components.append(comp)
    for pkg in npm_packages():
        components.append({"type": "library", "bom-ref": pkg["purl"], "name": pkg["name"],
                           "version": pkg["version"], "purl": pkg["purl"],
                           "licenses": [{"expression": pkg["license"]}],
                           "description": "browser library in the admin UI: "
                                          + ", ".join(pkg["files"])})
    root = f"pkg:github/haliskilic/ser2net@v{__version__}"
    bom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
        "metadata": {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": {"components": [{"type": "application", "name": "ser2net tools/third_party.py"}]},
            "component": {"type": "application", "bom-ref": root, "name": "ser2net",
                          "version": __version__, "purl": root,
                          "licenses": [{"expression": "AGPL-3.0-only"}],
                          "description": f"standalone executable for {platform.system()} "
                                         f"{platform.machine()}"},
        },
        "components": components,
        "dependencies": [{"ref": root, "dependsOn": [c["bom-ref"] for c in components]}],
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(bom, fh, indent=2)
        fh.write("\n")
    print(f"wrote {out_path}: {len(components)} components")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lic = sub.add_parser("licenses", help="write the combined license file")
    lic.add_argument("out")
    lic.add_argument("--extra", action="append", default=[], metavar="DIR")
    lic.add_argument("--strict", action="store_true",
                     help="fail when a pinned package is not installed (release builds)")
    bom = sub.add_parser("sbom", help="write a CycloneDX SBOM")
    bom.add_argument("out")
    bom.add_argument("--strict", action="store_true")
    bom.add_argument("--binaries", metavar="LIST",
                     help="the executable's binaries, one per line (ser2net.spec writes "
                          "build/bundled-binaries.txt): which native libraries it carries")
    args = ap.parse_args(argv)
    if args.cmd == "licenses":
        write_licenses(args.out, args.extra, args.strict)
    else:
        binaries = None
        if args.binaries:
            with open(args.binaries, encoding="utf-8") as fh:
                binaries = [line.strip() for line in fh if line.strip()]
        write_sbom(args.out, args.strict, binaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
