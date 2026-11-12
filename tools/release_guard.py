"""Release guards for the standalone executables (release.yml / the build scripts).

Two checks that fail the build rather than ship an executable with an unsupported or
mismatched crypto stack:

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
import json
import re
import sys


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
    i = sub.add_parser("interpreter", help="check the building interpreter's OpenSSL")
    i.add_argument("--min-openssl", default="3.5")
    s = sub.add_parser("sboms", help="check the per-platform SBOMs agree and are supported")
    s.add_argument("sbom", nargs="+")
    s.add_argument("--min-openssl", default="3.5")
    args = ap.parse_args(argv)
    if args.cmd == "interpreter":
        return check_interpreter(args.min_openssl)
    return check_sboms(args.sbom, args.min_openssl)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
