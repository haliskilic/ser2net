"""The vendored browser libraries are recorded, current and cached by content.

* vendor/npm/package.json pins htmx.org, @xterm/xterm and @xterm/addon-fit (GitHub's
  dependency graph and Dependabot read it) and records, for every file in ser2netweb/web/static
  copied from them, the package file and its sha256, plus each package's npm integrity
  and license; the license texts are in tools/licenses/npm/ (tools/refresh_assets.py
  writes all of it from the npm releases);
* xterm comes from the maintained @xterm/* packages (the unscoped xterm / xterm-addon-fit
  ones are deprecated upstream), with the globals app.js uses (Terminal, FitAddon);
* xterm.js and its fit addon are one release (published from one commit, which the record
  names): one of them bumped alone is reported, by the offline check and by a refresh;
* the pages link the assets with ?v=<hash of ser2netweb/web/static>, and a request carrying the
  current one gets an immutable Cache-Control (other requests are revalidated as before).

With SER2NETWEB_SKIP_PIN_CHECKS=1 (CI's test legs) the record is left to the job "offline
wheels match requirements.txt", so a Dependabot bump fails that one job.

Run: python3 tests/test_vendor_assets.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import refresh_assets as ra

from ser2netweb import envvars


def test_record_matches_the_files():
    problems = ra.check()
    assert not problems, "\n".join(problems) + "\nrun: python tools/refresh_assets.py"
    doc = ra.load()
    pkgs = {p["name"]: p for p in ra.packages(doc)}
    assert set(pkgs) == {"htmx.org", "@xterm/xterm", "@xterm/addon-fit"}, sorted(pkgs)
    assert "xterm" not in doc["dependencies"] and "xterm-addon-fit" not in doc["dependencies"], \
        "the unscoped xterm packages are deprecated"
    assert pkgs["@xterm/xterm"]["purl"] == f"pkg:npm/%40xterm/xterm@{pkgs['@xterm/xterm']['version']}"
    assert pkgs["@xterm/xterm"]["version"].startswith("5."), "6.x changes the console: plan it"
    assert pkgs["@xterm/xterm"]["commit"] and \
        pkgs["@xterm/xterm"]["commit"] == pkgs["@xterm/addon-fit"]["commit"], "one xterm.js release"
    assert {p["license"] for p in pkgs.values()} == {"0BSD", "MIT"}
    with open(os.path.join(ROOT, pkgs["@xterm/xterm"]["license_file"]), encoding="utf-8") as fh:
        assert "The xterm.js authors" in fh.read()
    statics = {f for f in os.listdir(os.path.join(ROOT, "ser2netweb", "web", "static"))}
    recorded = {os.path.basename(d) for d in doc["ser2net"]["files"]}
    # ser2netweb's own: the page script and style, the theme switch, the Modbus points editor
    assert statics - recorded == {"app.css", "app.js", "theme.js", "points.js"}, \
        "every third-party file is recorded"
    print(f"vendor/npm/package.json records {len(recorded)} files of {len(pkgs)} packages  OK")


def test_a_changed_file_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        doc = ra.load()
        for dest in doc["ser2net"]["files"]:
            os.makedirs(os.path.dirname(os.path.join(tmp, dest)), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, dest), os.path.join(tmp, dest))
        for pkg in ra.packages(doc):
            os.makedirs(os.path.dirname(os.path.join(tmp, pkg["license_file"])), exist_ok=True)
            shutil.copy2(os.path.join(ROOT, pkg["license_file"]), os.path.join(tmp, pkg["license_file"]))
        assert ra.check(tmp, doc) == []
        with open(os.path.join(tmp, "ser2netweb/web/static/htmx.min.js"), "ab") as fh:
            fh.write(b"\n")
        doc["dependencies"]["htmx.org"] = "9.9.9"  # a bump without its refresh
        found = ra.check(tmp, json.loads(json.dumps(doc)))
    assert "ser2netweb/web/static/htmx.min.js: differs from the recorded sha256" in found, found
    assert any(p.startswith("htmx.org: recorded for") for p in found), found
    print("an altered file and a version bump without a refresh are reported  OK")


def test_xterm_and_its_addons_are_one_release():
    """@xterm/xterm and @xterm/addon-* are published together, from one commit; an addon
    with another xterm.js than its own (fit 0.11 with xterm.js 5.5: it takes the
    scrollbar's width from an option 5.5 doesn't have) is refused, whichever was bumped."""
    one, other = "f447274f430fd22513f6adbf9862d19524471c04", "9ba6c00a195c95fcf8292a2b9084d91450e5daae"
    assert ra.unpaired({"@xterm/xterm": one, "@xterm/addon-fit": one, "htmx.org": "x"}) == ""
    assert ra.unpaired({"htmx.org": ""}) == "", "only xterm.js's packages are paired"
    for commits in ({"@xterm/xterm": other, "@xterm/addon-fit": one},
                    {"@xterm/xterm": one, "@xterm/addon-fit": other}):
        why = ra.unpaired(commits)
        assert "not of one release" in why and "bump them together" in why, why
    assert "no commit known for @xterm/addon-fit" in ra.unpaired(
        {"@xterm/xterm": one, "@xterm/addon-fit": ""})
    # the offline check reads the record's commits
    doc = ra.load()
    assert ra.unpaired({p["name"]: p["commit"] for p in ra.packages(doc)}) == ""
    doc["ser2net"]["packages"]["@xterm/addon-fit"]["commit"] = "0" * 40
    assert any("not of one release" in p for p in ra.check(doc=doc)), ra.check(doc=doc)

    # a refresh of such a set stops before it writes anything
    def fake(name, version):
        return None, "sha512-x", other if name == "@xterm/addon-fit" else one

    real, ra.tarball = ra.tarball, fake
    try:
        try:
            ra.refresh()
        except SystemExit as e:
            assert "not of one release" in str(e), e
        else:
            raise AssertionError("a refresh took an addon of another xterm.js release")
    finally:
        ra.tarball = real
    assert ra.check() == [], "the refused refresh changed nothing"
    print("xterm.js and its addons: one release (one commit), or the refresh and the "
          "check refuse  OK")


def test_console_globals():
    with open(os.path.join(ROOT, "ser2netweb/web/static/xterm.js"), encoding="utf-8") as fh:
        xterm = fh.read()
    with open(os.path.join(ROOT, "ser2netweb/web/static/xterm-addon-fit.js"), encoding="utf-8") as fh:
        fit = fh.read()
    with open(os.path.join(ROOT, "ser2netweb/web/static/app.js"), encoding="utf-8") as fh:
        app_js = fh.read()
    # UMD bundles: Terminal is copied onto the global object, FitAddon set as e.FitAddon
    assert "e.Terminal=" in xterm and "e.FitAddon=t()" in fit
    assert "new Terminal(" in app_js and "new FitAddon.FitAddon()" in app_js
    print("xterm.js / addon-fit export the Terminal and FitAddon globals app.js uses  OK")


def test_cache_headers():
    from _server import Browser, Server

    from ser2netweb.web.server import asset_version

    version = asset_version()
    with Server() as srv:
        br = Browser(srv.base)
        st, headers, body = br.get(f"/static/htmx.min.js?v={version}", raw=True)
        assert st == 200 and headers.get("Cache-Control") == "public, max-age=31536000, immutable", \
            (st, headers.get("Cache-Control"))
        with open(os.path.join(ROOT, "ser2netweb/web/static/htmx.min.js"), "rb") as fh:
            assert body == fh.read()
        for path in ("/static/htmx.min.js", "/static/htmx.min.js?v=2.8.1",
                     f"/static/htmx.min.js?v={version}&v=x"):
            st, headers, _ = br.get(path, raw=True)
            assert st == 200 and headers.get("Cache-Control") is None, (path, headers.get("Cache-Control"))
        st, headers, _ = br.get(f"/static/nope.js?v={version}", raw=True)
        assert st == 404 and headers.get("Cache-Control") is None, (st, headers.get("Cache-Control"))
        st, _h, page = br.get("/setup")
        assert f"/static/app.js?v={version}" in page and "xterm" not in page, \
            "the setup page links the hashed assets, without xterm"
    print("?v=<current hash> is cached immutably; other asset requests revalidate  OK")


if __name__ == "__main__":
    if envvars.get("SER2NETWEB_SKIP_PIN_CHECKS") == "1":
        print("SKIP: the record against the files (CI: job 'offline wheels match requirements.txt')")
    else:
        test_record_matches_the_files()
        test_a_changed_file_is_reported()
        test_xterm_and_its_addons_are_one_release()
    test_console_globals()
    test_cache_headers()
    print("\nPASS: vendored browser assets")
