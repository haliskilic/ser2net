"""tools/release_guard.py tag: a release tag must match the version and the CHANGELOG.

release.yml runs it before anything is built, so a tag that is not vX.Y.Z / vX.Y.Z-rc.N,
differs from ser2netweb.__version__ or has no dated CHANGELOG section fails the release instead
of shipping binaries, an SBOM and an image with the wrong version. It also decides the
image tags: a pre-release gets only its own, never X.Y or latest.

Run: python3 tests/test_release_guard.py   (stdlib only)
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import release_guard

DATED = "## [{v}] — 2026-11-20"


def _tree(tmp: str, version: str, heading: str = DATED) -> str:
    os.makedirs(os.path.join(tmp, "ser2netweb"), exist_ok=True)
    with open(os.path.join(tmp, "ser2netweb", "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write(f'"""ser2netweb."""\n\n# the version\n__version__ = "{version}"\n')
    with open(os.path.join(tmp, "CHANGELOG.md"), "w", encoding="utf-8") as fh:
        fh.write("# Changelog\n\n## [Unreleased]\n\n" + heading.format(v=version)
                 + "\n### Fixed\n- x\n\n## [2.8.1] — 2026-11-12\n### Fixed\n- y\n")
    return tmp


def _check(tag, root):
    """(exit code, stdout + stderr, what it wrote to $GITHUB_OUTPUT)."""
    out = io.StringIO()
    with tempfile.NamedTemporaryFile("r", suffix=".out", delete=False) as gh:
        path = gh.name
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = release_guard.check_tag(tag, root, path)
        with open(path, encoding="utf-8") as fh:
            written = dict(line.split("=", 1) for line in fh.read().splitlines())
    finally:
        os.unlink(path)
    return rc, out.getvalue(), written


def test_release_and_pre_release():
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, written = _check("v2.9.0", _tree(tmp, "2.9.0"))
        assert rc == 0, text
        assert written == {"version": "2.9.0", "prerelease": "false",
                           "image-tags": "2.9.0 2.9 latest"}, written
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, written = _check("v2.9.0-rc.1", _tree(tmp, "2.9.0-rc.1"))
        assert rc == 0, text
        assert written == {"version": "2.9.0-rc.1", "prerelease": "true",
                           "image-tags": "2.9.0-rc.1"}, written
    print("v2.9.0 -> 2.9.0, 2.9, latest; v2.9.0-rc.1 -> its own image tag only  OK")


def test_refused_tags():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp, "2.9.0")
        for tag in ("2.9.0", "v2.9", "v2.9.0.1", "v2.9.0-beta", "v2.9.0rc1", "v2.9.0-rc",
                    "v02.9.0", "v2.09.0", "V2.9.0", "v2.9.0 ", "latest", ""):
            rc, text, written = _check(tag, root)
            assert rc == 1 and "is not vX.Y.Z or vX.Y.Z-rc.N" in text, (tag, text)
            assert written == {}, (tag, written)
        rc, text, _ = _check("v2.9.1", root)
        assert rc == 1 and "does not match ser2netweb/__init__.py, which says 2.9.0" in text, text
        rc, text, _ = _check("v2.9.0-rc.1", root)
        assert rc == 1 and "does not match" in text, text
    print("other tag forms and a tag that isn't ser2netweb.__version__ are refused  OK")


def test_changelog_section_needed():
    for heading in ("## [2.9.0]", "## [2.9.0] — TBD", "## [2.9.0] — 2026-11", "## 2.9.0",
                    "### [2.9.0] — 2026-11-20", "## [2.9.0-rc.1] — 2026-11-20"):
        with tempfile.TemporaryDirectory() as tmp:
            rc, text, written = _check("v2.9.0", _tree(tmp, "2.9.0", heading))
            assert rc == 1 and "CHANGELOG.md has no '## [2.9.0] — YYYY-MM-DD' section" in text, \
                (heading, text)
            assert written == {}, written
    with tempfile.TemporaryDirectory() as tmp:  # both problems at once
        rc, text, _ = _check("v3.0.0", _tree(tmp, "2.9.0", "## [Unreleased]"))
        assert rc == 1 and "does not match" in text and "CHANGELOG.md has no" in text, text
    print("a missing or undated CHANGELOG section is refused  OK")


def test_dry_run_and_this_tree():
    with tempfile.TemporaryDirectory() as tmp:
        rc, text, written = _check(None, _tree(tmp, "2.9.0"))
        assert rc == 0 and "no tag given (dry run), checking v2.9.0" in text, text
        assert written["version"] == "2.9.0", written
    # the current version has its dated section, so a dry run of the release passes
    rc, text, written = _check(None, ROOT)
    assert rc == 0, text
    print(f"a dry run checks the current version's tag ({written['version']})  OK")


def test_command_line():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp, "2.9.0")
        out = os.path.join(tmp, "github_output")
        env = dict(os.environ, GITHUB_OUTPUT=out)
        tool = os.path.join(ROOT, "tools", "release_guard.py")
        p = subprocess.run([sys.executable, tool, "tag", "v2.9.0", "--root", root], env=env,
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stdout + p.stderr
        with open(out, encoding="utf-8") as fh:
            assert "image-tags=2.9.0 2.9 latest\n" in fh.read()
        p = subprocess.run([sys.executable, tool, "tag", "v2.8.1", "--root", root], env=env,
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 1 and "not releasing v2.8.1" in p.stderr, p.stdout + p.stderr
    print("release_guard.py tag: exit status and $GITHUB_OUTPUT  OK")


if __name__ == "__main__":
    test_release_and_pre_release()
    test_refused_tags()
    test_changelog_section_needed()
    test_dry_run_and_this_tree()
    test_command_line()
    print("\nPASS: release guard (tag)")
