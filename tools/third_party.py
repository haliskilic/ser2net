"""Third-party component inventory of a ser2net build, from the build environment.

    python tools/third_party.py licenses OUT.txt [--extra DIR]...

Run it with the interpreter that builds the standalone executable (PyInstaller). It
writes every bundled component's license text into one file, which ser2net.spec
packs into the executable (``ser2net --licenses`` prints it) and the release attaches.
Components:
  * ser2net itself (AGPL-3.0-only, or a commercial license),
  * CPython (its LICENSE.txt — on Windows it also covers the bundled OpenSSL, libffi,
    bzip2, xz, zlib, SQLite, Tcl/Tk… of the python.org build),
  * every Python distribution the app imports at run time: the pins in
    requirements.txt + requirements-optional.txt and their dependencies,
  * the PyInstaller bootloader and runtime hooks embedded in the executable,
  * ``--extra DIR``: further license texts (*.txt), e.g. the native libraries
    python-build-standalone links into the Linux interpreter.
"""
from __future__ import annotations

import argparse
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


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _packaging():
    try:
        from packaging.markers import Marker
        from packaging.requirements import Requirement
    except ImportError:  # pip always vendors it
        from pip._vendor.packaging.markers import Marker  # type: ignore[no-redef]
        from pip._vendor.packaging.requirements import Requirement  # type: ignore[no-redef]
    return Marker, Requirement


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


def license_of(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    lic = (md.get("License") or "").strip()
    return lic.splitlines()[0][:120] if lic else "see license text"


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
    for name, role in BUILD_EMBEDDED.items():
        try:
            dists.append(metadata.distribution(name))
        except metadata.PackageNotFoundError:
            print(f"note: {name} not installed ({role} not covered)", file=sys.stderr)
    for dist in dists:
        name, version = dist.metadata["Name"], dist.version
        role = BUILD_EMBEDDED.get(canonical(name))
        title = f"{name} {version} — {license_of(dist)}" + (f" ({role})" if role else "")
        texts = license_texts(dist)
        if not texts:
            raise SystemExit(f"no license file in the {name} {version} distribution")
        parts.append(_section(title, "\n\n".join(f"--- {fn} ---\n{t}" for fn, t in texts)))
    for d in extra_dirs:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".txt"):
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as fh:
                    parts.append(_section(f"{fn[:-4]} (native library in the interpreter)",
                                          fh.read()))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(parts))
    print(f"wrote {out_path}: {len(dists)} Python distributions, "
          f"{sum(len(os.listdir(d)) for d in extra_dirs)} extra license files")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lic = sub.add_parser("licenses", help="write the combined license file")
    lic.add_argument("out")
    lic.add_argument("--extra", action="append", default=[], metavar="DIR")
    lic.add_argument("--strict", action="store_true",
                     help="fail when a pinned package is not installed (release builds)")
    args = ap.parse_args(argv)
    if args.cmd == "licenses":
        write_licenses(args.out, args.extra, args.strict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
