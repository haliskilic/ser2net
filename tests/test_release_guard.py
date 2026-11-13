"""tools/release_guard.py: a release tag must match the version and the CHANGELOG, and the
release files must be exactly the ones a release has.

`tag`: release.yml runs it before anything is built, so a tag that is not vX.Y.Z /
vX.Y.Z-rc.N, differs from ser2netweb.__version__ or has no dated CHANGELOG section fails the
release instead of shipping binaries, an SBOM and an image with the wrong version. It also
decides the image tags: a pre-release gets only its own, never X.Y or latest.
`assets`: the folder release.yml uploads holds the ser2netweb-* files (each executable with
its SBOM and license texts, the install script, the unit) and SHA256SUMS over all of them,
nothing missing, nothing else (no ser2net-* file of the names up to 2.9.x).
`sboms`: every platform's SBOM describes the same ser2netweb version, CPython and OpenSSL.

Run: python3 tests/test_release_guard.py   (stdlib only)
"""
import contextlib
import io
import json
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


def _release_folder(tmp: str, names) -> str:
    out = os.path.join(tmp, "out")
    os.makedirs(out)
    for name in names:
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(name)
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        fh.write("".join(f"{'0' * 64}  {n}\n" for n in sorted(names)))
    return out


def _assets(folder):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = release_guard.check_assets(folder)
    return rc, out.getvalue()


def test_release_assets():
    want = release_guard.release_assets()
    assert "SHA256SUMS" in want and len(want) == 12, want
    for name in ("ser2netweb-linux-x64", "ser2netweb-linux-arm64.cdx.json",
                 "ser2netweb-windows-x64.exe", "ser2netweb-windows-x64.THIRD-PARTY-LICENSES.txt",
                 "ser2netweb-install.sh", "ser2netweb-standalone.service"):
        assert name in want, name
    files = [n for n in want if n != "SHA256SUMS"]
    with tempfile.TemporaryDirectory() as tmp:
        rc, text = _assets(_release_folder(tmp, files))
        assert rc == 0 and "12 release files" in text, text
    for change, words in (
            (lambda f: f[1:], ("missing: ",)),
            (lambda f: f + ["ser2net-linux-x64"], ("not a release file: ser2net-linux-x64",)),
            (lambda f: [n.replace("ser2netweb-install", "ser2net-install") for n in f],
             ("missing: ser2netweb-install.sh", "not a release file: ser2net-install.sh"))):
        with tempfile.TemporaryDirectory() as tmp:
            rc, text = _assets(_release_folder(tmp, change(list(files))))
            assert rc == 1 and all(w in text for w in words), (words, text)
    with tempfile.TemporaryDirectory() as tmp:  # SHA256SUMS that leaves a file out
        out = _release_folder(tmp, files)
        with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8") as fh:
            fh.write("".join(f"{'0' * 64} *{n}\n" for n in files[1:]))
        rc, text = _assets(out)
        assert rc == 1 and "SHA256SUMS lists" in text, text
    print("release_guard.py assets: exactly the ser2netweb-* files, each in SHA256SUMS; a "
          "missing one, an old ser2net-* name or an incomplete SHA256SUMS fails  OK")


def _sbom(tmp: str, name: str, product: str, version: str, cpython="3.12.16",
          openssl="3.5.4") -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"metadata": {"component": {"name": product, "version": version}},
                   "components": [{"name": "cpython", "version": cpython},
                                  {"name": "openssl", "version": openssl}]}, fh)
    return path


def test_sboms():
    def check(*paths):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = release_guard.check_sboms(list(paths), "3.5")
        return rc, out.getvalue()
    with tempfile.TemporaryDirectory() as tmp:
        a = _sbom(tmp, "a.cdx.json", "ser2netweb", "3.0.0")
        b = _sbom(tmp, "b.cdx.json", "ser2netweb", "3.0.0")
        rc, text = check(a, b)
        assert rc == 0 and "ser2netweb 3.0.0 with CPython 3.12.16, OpenSSL 3.5.4" in text, text
        for bad in (_sbom(tmp, "c.cdx.json", "ser2netweb", "2.9.0"),
                    _sbom(tmp, "d.cdx.json", "ser2net", "3.0.0")):
            rc, text = check(a, bad)
            assert rc == 1 and "same ser2netweb version" in text, text
        rc, text = check(a, _sbom(tmp, "e.cdx.json", "ser2netweb", "3.0.0", openssl="3.0.18"))
        assert rc == 1 and "same CPython and OpenSSL" in text, text
        rc, text = check(_sbom(tmp, "f.cdx.json", "ser2netweb", "3.0.0", openssl="3.0.18"))
        assert rc == 1 and "older than the minimum" in text, text
    print("release_guard.py sboms: the same ser2netweb version, CPython and a supported "
          "OpenSSL on every platform  OK")


if __name__ == "__main__":
    test_release_and_pre_release()
    test_refused_tags()
    test_changelog_section_needed()
    test_dry_run_and_this_tree()
    test_command_line()
    test_release_assets()
    test_sboms()
    print("\nPASS: release guard")
