"""CHANGELOG.md stays well-formed.

  * a version has at most one heading of each type (### Added, ### Fixed, ...), and only
    the types Keep a Changelog defines, so a new group can't be added next to an old one;
  * the newest released version is the one in app/__init__.py, so a release can't ship
    without its entry (or with a stale version number), and it has a YYYY-MM-DD date;
  * every version has its link line at the bottom of the file.

Run: python3 tests/test_changelog.py   (stdlib only)
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from app import __version__  # noqa: E402

CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
TYPES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_VERSION = re.compile(r"## \[(?P<v>[^\]]+)\](?: — (?P<date>.*))?")
_HEADING = re.compile(r"### (?P<h>.*?)\s*")
_LINK = re.compile(r"\[(?P<v>[^\]]+)\]: \S+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def sections(text: str) -> list:
    """[(version, date or None, [heading, ...]), ...] in file order."""
    out: list = []
    for line in text.splitlines():
        m = _VERSION.fullmatch(line)
        if m:
            out.append((m["v"], m["date"], []))
            continue
        h = _HEADING.fullmatch(line)
        if h and out:
            out[-1][2].append(h["h"])
    return out


def problems(text: str, version: str) -> list:
    found = []
    secs = sections(text)
    for v, _date, heads in secs:
        for h in sorted(set(heads)):
            if h not in TYPES:
                found.append(f"[{v}]: '### {h}' is not one of {', '.join(TYPES)}")
            elif heads.count(h) > 1:
                found.append(f"[{v}]: '### {h}' appears {heads.count(h)} times; merge them")
    released = [(v, d) for v, d, _ in secs if v != "Unreleased"]
    if not released:
        found.append("no released version")
    else:
        newest, date = released[0]
        if newest != version:
            found.append(f"the newest version is [{newest}], app/__init__.py says {version}")
        for v, d in released:
            if not (d and _DATE.fullmatch(d)):
                found.append(f"[{v}] has no YYYY-MM-DD date: {d!r}")
    linked = {m["v"] for m in map(_LINK.fullmatch, text.splitlines()) if m}
    found += [f"[{v}] has no link line at the bottom" for v, _, _ in secs if v not in linked]
    return found


def test_checker():
    good = ("# Changelog\n\n## [Unreleased]\n\n## [1.1] — 2026-01-02\n### Added\n- x\n"
            "### Fixed\n- y\n\n## [1.0] — 2026-01-01\nFirst release.\n\n"
            "[Unreleased]: https://e/compare\n[1.1]: https://e/1.1\n[1.0]: https://e/1.0\n")
    assert problems(good, "1.1") == [], problems(good, "1.1")
    bad = (good.replace("### Fixed", "### Added").replace("[1.0]: https://e/1.0\n", "")
           .replace("## [1.0] — 2026-01-01", "## [1.0] — soon\n### Docs"))
    got = problems(bad, "1.2")
    want = ["[1.1]: '### Added' appears 2 times; merge them",
            "[1.0]: '### Docs' is not one of " + ", ".join(TYPES),
            "the newest version is [1.1], app/__init__.py says 1.2",
            "[1.0] has no YYYY-MM-DD date: 'soon'",
            "[1.0] has no link line at the bottom"]
    assert got == want, got
    print("checker finds duplicate / unknown headings, version, date and link problems  OK")


def test_changelog():
    with open(CHANGELOG, encoding="utf-8") as f:
        found = problems(f.read(), __version__)
    assert not found, "CHANGELOG.md:\n  " + "\n  ".join(found)
    print(f"CHANGELOG.md: one heading of each type per version, newest = {__version__}  OK")


def main():
    test_checker()
    test_changelog()
    print("\nPASS: CHANGELOG.md")


if __name__ == "__main__":
    main()
