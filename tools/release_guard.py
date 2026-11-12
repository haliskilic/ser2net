"""Release guards (release.yml and the build scripts of the standalone executables).

Checks that fail the release rather than ship a mis-versioned release, or an executable
with an unsupported or mismatched crypto stack:

  tag [vX.Y.Z]
      Run by release.yml before anything is built: the tag must be vX.Y.Z (vX.Y.Z-rc.N
      for a pre-release), equal "v" + app.__version__, and CHANGELOG.md must have the
      version's dated "## [X.Y.Z] — YYYY-MM-DD" section. Without a tag (a dry run) it
      checks the tag the current version gets. Prints, and with $GITHUB_OUTPUT set also
      writes, the version, whether it is a pre-release and the image tags to publish:
      X.Y.Z, X.Y and latest, or for a pre-release only its own.

  interpreter --min-openssl 3.5
      Run with the interpreter that builds an executable: its ssl.OPENSSL_VERSION_INFO
      must be at least the given series (OpenSSL 3.0 reached end of life in 2026-09).

  sboms OUT/*.cdx.json --min-openssl 3.5
      Run at publish time over the per-platform SBOMs: every one must report the same
      CPython and OpenSSL version, and OpenSSL must be at least the given series — so a
      platform can't silently fall back to an older interpreter (as a frozen setup-python
      minor once did on Windows).

Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# vX.Y.Z, or vX.Y.Z-rc.N for a pre-release (no leading zeros, as in semver)
_N = r"(?:0|[1-9][0-9]*)"
_TAG = re.compile(rf"v(?P<version>(?P<major>{_N})\.(?P<minor>{_N})\.{_N}(?P<rc>-rc\.{_N})?)")


def app_version(root: str) -> str:
    """app.__version__, read from app/__init__.py without importing the package."""
    path = os.path.join(root, "app", "__init__.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__version__"
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value
    raise SystemExit(f"release guard: no __version__ = \"...\" in {path}")


def check_tag(tag: str | None, root: str = ROOT, github_output: str | None = None) -> int:
    version = app_version(root)
    if tag is None:
        tag = f"v{version}"
        print(f"release guard: no tag given (dry run), checking {tag}")
    problems: list[str] = []
    m = _TAG.fullmatch(tag)
    if not m:
        problems.append(f"the tag {tag!r} is not vX.Y.Z or vX.Y.Z-rc.N")
    elif m["version"] != version:
        problems.append(f"the tag {tag} does not match app/__init__.py, which says {version}: "
                        f"bump __version__ (and CHANGELOG.md) or tag v{version}")
    heading = re.compile(rf"## \[{re.escape(version)}\] — \d{{4}}-\d{{2}}-\d{{2}}")
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as fh:
        if not any(heading.fullmatch(line.rstrip()) for line in fh):
            problems.append(f"CHANGELOG.md has no '## [{version}] — YYYY-MM-DD' section")
    if problems or m is None:
        print(f"release guard: not releasing {tag}:\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 1
    prerelease = m["rc"] is not None
    # as docker/metadata-action's semver tags: a pre-release moves neither X.Y nor latest
    image_tags = [version] if prerelease else [version, f"{m['major']}.{m['minor']}", "latest"]
    print(f"release guard OK: {tag} is the {'pre-release' if prerelease else 'release'} of "
          f"{version}; image tags {' '.join(image_tags)}")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"version={version}\nprerelease={'true' if prerelease else 'false'}\n"
                     f"image-tags={' '.join(image_tags)}\n")
    return 0


def _series(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


def check_interpreter(min_openssl: str) -> int:
    import ssl

    want = _series(min_openssl)
    have = ssl.OPENSSL_VERSION_INFO[:len(want)]
    pyver = ".".join(str(n) for n in sys.version_info[:3])
    if have < want:
        print(f"release guard: {ssl.OPENSSL_VERSION} is older than the minimum supported "
              f"OpenSSL {min_openssl} series — build with python-build-standalone "
              f"(tools/build_standalone_*), not a setup-python minor that pins an EOL "
              f"OpenSSL.", file=sys.stderr)
        return 1
    print(f"release guard OK: Python {pyver}, {ssl.OPENSSL_VERSION}")
    return 0


def _component(bom: dict, name: str) -> str | None:
    for comp in bom.get("components", []):
        if comp.get("name") == name:
            return comp.get("version")
    return None


def check_sboms(paths: list[str], min_openssl: str) -> int:
    want = _series(min_openssl)
    seen: dict[str, tuple[str | None, str | None]] = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            bom = json.load(fh)
        seen[path] = (_component(bom, "cpython"), _component(bom, "openssl"))
    pairs = set(seen.values())
    if len(pairs) != 1 or None in next(iter(pairs)):
        lines = "\n".join(f"  {p}: CPython {c}, OpenSSL {o}" for p, (c, o) in seen.items())
        print("release guard: the SBOMs do not all report the same CPython and OpenSSL:\n"
              + lines, file=sys.stderr)
        return 1
    cpython, openssl = next(iter(pairs))
    if _series(openssl or "")[:len(want)] < want:  # (None returned above)
        print(f"release guard: OpenSSL {openssl} is older than the minimum supported "
              f"{min_openssl} series", file=sys.stderr)
        return 1
    print(f"release guard OK: every SBOM reports CPython {cpython}, OpenSSL {openssl}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tag", help="check a release tag against app/__init__.py and CHANGELOG.md")
    t.add_argument("tag", nargs="?", help="vX.Y.Z or vX.Y.Z-rc.N (default: the current "
                   "version's tag, a dry run)")
    t.add_argument("--root", default=ROOT, help="the source tree (default: this checkout)")
    i = sub.add_parser("interpreter", help="check the building interpreter's OpenSSL")
    i.add_argument("--min-openssl", default="3.5")
    s = sub.add_parser("sboms", help="check the per-platform SBOMs agree and are supported")
    s.add_argument("sbom", nargs="+")
    s.add_argument("--min-openssl", default="3.5")
    args = ap.parse_args(argv)
    if args.cmd == "tag":
        return check_tag(args.tag, args.root, os.environ.get("GITHUB_OUTPUT"))
    if args.cmd == "interpreter":
        return check_interpreter(args.min_openssl)
    return check_sboms(args.sbom, args.min_openssl)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
