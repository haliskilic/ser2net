"""winget's, Scoop's and Chocolatey's packages of ser2netweb (packaging/winget, packaging/scoop,
packaging/chocolatey; tools/windows_channels.py, packaging/winget/make_manifests.py).

  * the committed winget manifests (microsoft/winget-pkgs' layout) and the Scoop manifest
    are what the generators make for their release, and the Chocolatey package is what
    tools/windows_channels.py sets for it; all three name the same release, the same
    ser2netweb-windows-x64.exe and SHA-256, the licence, the project's URLs and the sentence
    that keeps ser2netweb apart from Corey Minyard's ser2net;
  * winget: haliskilic.ser2netweb, a portable x64 installer, the command ser2netweb, en-US
    (the default) and tr-TR; the folder winget installs it in is the one ser2netweb/layout.py
    recognises (data in the user's folder), and the YAML writer quotes what YAML would read
    as something else;
  * Scoop: the exe saved as ser2netweb.exe on PATH, data\\ persisted (where layout.py keeps a
    Scoop copy's data), checkver on GitHub, autoupdate with the hash from the release's
    SHA256SUMS (Scoop's extraction of it, rebuilt here, finds the exe's line);
  * Chocolatey: the nuspec's metadata, the install script's download checked by SHA-256 into
    the tools folder (where the shim comes from), the version updates for a new release;
  * the workflows: winget.yml and chocolatey.yml run on a published release (not a
    pre-release) and by hand for a tag, test pull requests that change the packaging,
    give the secret only to the step that uses it and skip with a notice without it;
    Chocolatey pushes only a version that recognises its folder;
  * opt-in, SER2NETWEB_SCHEMA_TESTS=1 (needs jsonschema, PyYAML and the network; winget.yml
    sets it): the manifests conform to winget's and Scoop's JSON schemas, downloaded from the
    pinned commits and checked against their SHA-256; the YAML reads back as generated; a
    broken manifest is reported; the committed SHA-256 is the one in the release's SHA256SUMS.

Run: python3 tests/test_windows_channels.py   (stdlib only; binds nothing)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

from _server import ROOT

from _workflows import jobs, permissions, read, steps
from ser2netweb import envvars, layout

ROOT = os.path.abspath(ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "packaging", "winget"))

import make_manifests
import windows_channels as wc

SCHEMA_TESTS = envvars.get("SER2NETWEB_SCHEMA_TESTS") == "1"
WINGET = os.path.join(ROOT, "packaging", "winget", "manifests", "h", "haliskilic", "ser2netweb")
CHOCO = os.path.join(ROOT, "packaging", "chocolatey")
NS = {"n": "http://schemas.microsoft.com/packaging/2015/06/nuspec.xsd"}


def text(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def committed_release() -> tuple[str, str]:
    """(tag, SHA-256) of the committed winget manifests: the one version there."""
    versions = os.listdir(WINGET)
    assert len(versions) == 1, f"one release's manifests are kept, not {versions}"
    tag = "v" + versions[0]
    installer = text(os.path.join(WINGET, versions[0], f"{wc.WINGET_ID}.installer.yaml"))
    m = re.search(r"(?m)^  InstallerSha256: ([0-9A-F]{64})$", installer)
    assert m, installer
    return tag, m.group(1).lower()


def test_tags_and_sums():
    assert wc.version_of("v3.0.0") == "3.0.0"
    for bad in ("3.0.0", "v3.0", "v3.0.0-rc.1", "v3.0.0\n", "v3.0.0/../x"):
        try:
            wc.version_of(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert wc.release_url("v3.0.0") == ("https://github.com/haliskilic/ser2netweb/releases/"
                                        "download/v3.0.0/ser2netweb-windows-x64.exe")
    sums = wc.parse_sums("A" * 64 + "  ser2netweb-windows-x64.exe\n"
                         + "b" * 64 + " *ser2netweb-windows-x64.cdx.json\nnot a line\n")
    assert sums == {"ser2netweb-windows-x64.exe": "a" * 64,
                    "ser2netweb-windows-x64.cdx.json": "b" * 64}, sums
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "SHA256SUMS")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("c" * 64 + "  ser2netweb-linux-x64\n")
        try:
            wc.exe_sha256("v3.0.0", path)
        except ValueError as e:
            assert "has no ser2netweb-windows-x64.exe" in str(e)
        else:
            raise AssertionError("a SHA256SUMS without the exe was taken")
    print("release tags vX.Y.Z only; SHA256SUMS read, the Windows exe's line required  OK")


def test_winget_manifests():
    tag, sha = committed_release()
    folder = os.path.join(WINGET, wc.version_of(tag))
    assert make_manifests.folder(tag) == folder
    want = make_manifests.files(tag, sha)
    assert sorted(os.listdir(folder)) == sorted(want), os.listdir(folder)
    for name, body in want.items():
        assert text(os.path.join(folder, name)) == body, \
            f"{name} is not what make_manifests.py makes: run it for {tag}"
    ms = make_manifests.manifests(tag, sha)
    inst = ms[f"{wc.WINGET_ID}.installer.yaml"]
    assert inst["InstallerType"] == "portable" and inst["Commands"] == ["ser2netweb"], inst
    assert inst["Installers"] == [{"Architecture": "x64", "InstallerUrl": wc.release_url(tag),
                                   "InstallerSha256": sha.upper()}], inst
    en, tr = ms[f"{wc.WINGET_ID}.locale.en-US.yaml"], ms[f"{wc.WINGET_ID}.locale.tr-TR.yaml"]
    assert ms[f"{wc.WINGET_ID}.yaml"]["DefaultLocale"] == "en-US"
    assert en["ManifestType"] == "defaultLocale" and tr["ManifestType"] == "locale"
    assert en["License"] == tr["License"] == "AGPL-3.0-only"
    assert wc.DISAMBIGUATION in en["Description"], en["Description"]
    assert "Corey Minyard'ın ser2net'iyle bir bağlantısı yoktur" in tr["Description"]
    assert en["ReleaseNotesUrl"] == f"{wc.HOMEPAGE}/releases/tag/{tag}"
    tags = en["Tags"]
    assert len(tags) <= 16 and len(set(tags)) == len(tags), tags
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", t) for t in tags), tags
    for m in ms.values():
        assert m["PackageIdentifier"] == wc.WINGET_ID and m["PackageVersion"] == tag[1:]
        assert m["ManifestVersion"] == wc.WINGET_MANIFEST_VERSION
    for name, body in want.items():
        schema = re.search(r"winget-manifest\.(\w+)\.", body).group(1)
        assert schema == ms[name]["ManifestType"], name
        assert "\t" not in body and not re.search(r"(?m) $", body), name
    # the folder winget installs it in is the one ser2netweb recognises
    assert wc.WINGET_ID == layout.WINGET_ID
    portable = (r"C:\Users\ayse\AppData\Local\Microsoft\WinGet\Packages"
                + "\\" + wc.WINGET_ID + r"_Microsoft.Winget.Source_8wekyb3d8bbwe\ser2netweb.exe")
    assert layout.windows_package_manager(portable) == layout.WINGET
    print(f"winget {tag}: the committed manifests are make_manifests.py's (portable x64, "
          "command ser2netweb, en-US + tr-TR, the licence and the disambiguation)  OK")


def test_yaml_scalars():
    plain = ["3.0.0", "1.28.0", "haliskilic.ser2netweb", "https://github.com/x/y#readme",
             "Halis KILIÇ", "Copyright (C) 2026 Halis KILIÇ", "x64", "Corey Minyard's ser2net",
             "079F7943ADC1D446026FA0D42DD9BA5B95FF91FA010E571923F4D0237D48B632"]
    for value in plain:
        assert make_manifests.scalar(value) == value, value
    quoted = {"yes": "'yes'", "No": "'No'", "null": "'null'", "3.1": "'3.1'", "0x1F": "'0x1F'",
              "123": "'123'", "a: b": "'a: b'", "x #y": "'x #y'", "trail:": "'trail:'",
              "'q": "'''q'", "-x": "'-x'", "@a": "'@a'", "*a": "'*a'", "[a]": "'[a]'",
              "{a}": "'{a}'", "!a": "'!a'", "%a": "'%a'", "|a": "'|a'", ">a": "'>a'",
              "1" * 63 + "E": "'" + "1" * 63 + "E'"}  # (an upper-case digest that reads as a float)
    for value, want in quoted.items():
        assert make_manifests.scalar(value) == want, (value, make_manifests.scalar(value))
    body = make_manifests.dump({"A": "x\n\ny", "B": ["a", "b"], "C": [{"K": "v", "L": "w"}],
                                "ManifestType": "version"}, "version")
    assert body.endswith("A: |-\n  x\n\n  y\nB:\n- a\n- b\nC:\n- K: v\n  L: w\n"
                         "ManifestType: version\n"), body
    print("YAML: plain scalars only where they read back as the same string; texts as blocks  OK")


def scoop_find_hash(manifest: dict, version: str, sums: str) -> str:
    """Scoop's autoupdate (lib/autoupdate.ps1, find_hash_in_textfile): the URL for `version`,
    $baseurl / $basename from it (the remote name, not the #/ one), the hash file's URL and
    regex with the templates and the (escaped) substitutions, matched without case."""
    auto = manifest["autoupdate"]
    url = auto["architecture"]["64bit"]["url"].replace("$version", version)
    origin = url.split("#", 1)[0]
    base, basename = origin.rsplit("/", 1)
    hash_url = auto["hash"]["url"].replace("$baseurl", base)
    assert hash_url == f"{wc.HOMEPAGE}/releases/download/v{version}/SHA256SUMS", hash_url
    regex = auto["hash"]["regex"].replace("$sha256", "([a-fA-F0-9]{64})")
    regex = regex.replace("$basename", re.escape(basename))
    m = re.search(regex, sums, re.I)
    assert m, regex
    return m.group(1).lower()


def test_scoop_manifest():
    tag, sha = committed_release()
    path = os.path.join(ROOT, "packaging", "scoop", "ser2netweb.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest["version"] == wc.version_of(tag), "Scoop and winget name one release"
    assert manifest["architecture"]["64bit"]["hash"] == sha
    assert text(path) == wc.scoop_json(wc.scoop_manifest(tag, sha)), \
        f"{path} is not what tools/windows_channels.py scoop {tag} makes"
    assert manifest["architecture"]["64bit"]["url"] == wc.release_url(tag) + "#/ser2netweb.exe"
    assert manifest["bin"] == "ser2netweb.exe" and manifest["persist"] == "data"
    assert manifest["license"]["identifier"] == "AGPL-3.0-only"
    assert wc.DISAMBIGUATION in manifest["description"]
    assert manifest["checkver"] == {"github": wc.HOMEPAGE}
    assert any("$persist_dir\\data" in note for note in manifest["notes"]), manifest["notes"]
    # where Scoop puts the exe, ser2netweb keeps its data in data\ next to it: the folder
    # the manifest persists
    with tempfile.TemporaryDirectory() as tmp:
        app = os.path.join(tmp, "scoop", "apps", "ser2netweb", "current")
        os.makedirs(app)
        for name in ("manifest.json", "install.json"):
            shutil.copy(path, os.path.join(app, name))
        assert layout.windows_package_manager(os.path.join(app, manifest["bin"])) == layout.SCOOP
        assert layout.default_data_dir(app, layout.SCOOP) == os.path.join(app, manifest["persist"])
    # Scoop's Excavator: the new release's hash from its SHA256SUMS, not another file's line
    lines = {f"ser2netweb-{v}": c for v, c in (("3.1.0-1.x86_64.rpm", "1"), ("linux-x64", "2"))}
    lines.update({"ser2netweb-windows-x64.THIRD-PARTY-LICENSES.txt": "3",
                  "ser2netweb-windows-x64.cdx.json": "4", "ser2netweb-windows-x64.exe": "5",
                  "ser2netweb_3.1.0_amd64.deb": "6"})
    sums = "".join(f"{c * 64}  {n}\n" for n, c in lines.items())
    assert scoop_find_hash(manifest, "3.1.0", sums) == "5" * 64
    print(f"Scoop {tag}: generated; ser2netweb.exe on PATH, data\\ persisted (where ser2netweb "
          "keeps it), checkver github, autoupdate's hash from SHA256SUMS  OK")


def test_chocolatey_package():
    tag, sha = committed_release()
    nuspec_text = text(os.path.join(CHOCO, "ser2netweb.nuspec"))
    install = text(os.path.join(CHOCO, "tools", "chocolateyinstall.ps1"))
    meta = ET.fromstring(nuspec_text.encode("utf-8")).find("n:metadata", NS)
    field = {child.tag.split("}", 1)[1]: (child.text or "").strip() for child in meta}
    assert field["id"] == "ser2netweb" and field["version"] == wc.version_of(tag), field
    assert field["requireLicenseAcceptance"] == "false"
    assert field["releaseNotes"] == f"{wc.HOMEPAGE}/releases/tag/{tag}"
    for key, url in (("projectUrl", wc.HOMEPAGE), ("projectSourceUrl", wc.HOMEPAGE),
                     ("licenseUrl", f"{wc.HOMEPAGE}/blob/main/LICENSE"),
                     ("docsUrl", wc.WINDOWS_GUIDE), ("bugTrackerUrl", f"{wc.HOMEPAGE}/issues"),
                     ("packageSourceUrl", f"{wc.HOMEPAGE}/tree/main/packaging/chocolatey")):
        assert field[key] == url, (key, field[key])
    assert wc.DISAMBIGUATION in field["description"]
    assert field["summary"] == wc.SUMMARY
    tags = field["tags"].split()
    assert "ser2netweb" in tags and "chocolatey" not in tags and len(set(tags)) == len(tags)
    assert "--" not in re.sub(r"(?s)<!\[CDATA\[.*?\]\]>", "", nuspec_text).split("<package", 1)[0] \
        .replace("<!--", "").replace("-->", ""), "a comment must not hold --"
    assert re.search(r'<file src="tools\\\*\*" target="tools" />', nuspec_text)
    assert f"$url64 = '{wc.release_url(tag)}'\n" in install
    assert f"$checksum64 = '{sha}'\n" in install, "the same exe as winget's and Scoop's"
    for needle in ("Get-ChocolateyWebFile @packageArgs", "checksumType64 = 'sha256'",
                   "fileFullPath   = Join-Path $toolsDir 'ser2netweb.exe'",
                   "$ErrorActionPreference = 'Stop'"):
        assert needle in install, needle
    before = text(os.path.join(CHOCO, "tools", "chocolateyBeforeModify.ps1"))
    assert "Where-Object { $_.Path -eq $exe }" in before and "Stop-Process" in before
    for name in ("ser2netweb.nuspec", os.path.join("tools", "chocolateyinstall.ps1"),
                 os.path.join("tools", "chocolateyBeforeModify.ps1")):
        assert not text(os.path.join(CHOCO, name)).startswith("\ufeff"), name
    # the committed files are the release's, and a new release changes only its four values
    assert wc.chocolatey_files(tag, sha, nuspec_text, install) == (nuspec_text, install)
    other = "d" * 64
    new_nuspec, new_install = wc.chocolatey_files("v9.8.7", other, nuspec_text, install)
    assert new_nuspec == nuspec_text.replace(f"<version>{tag[1:]}</version>",
                                             "<version>9.8.7</version>") \
        .replace(f"releases/tag/{tag}<", "releases/tag/v9.8.7<")
    assert new_install == install.replace(f"/download/{tag}/", "/download/v9.8.7/") \
        .replace(sha, other)
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "chocolatey")
        shutil.copytree(CHOCO, copy)
        wc.update_chocolatey("v9.8.7", other, copy)
        assert text(os.path.join(copy, "ser2netweb.nuspec")) == new_nuspec
        assert text(os.path.join(copy, "tools", "chocolateyinstall.ps1")) == new_install
    try:
        wc.chocolatey_files(tag, sha, nuspec_text.replace("<version>", "<v>"), install)
    except ValueError as e:
        assert "expected one match" in str(e)
    else:
        raise AssertionError("a nuspec without <version> was taken")
    print(f"Chocolatey {tag}: nuspec metadata, the exe downloaded by SHA-256 into tools, a "
          "new release changes the version and the download only  OK")


def _on(workflow: str) -> str:
    return workflow.split("\npermissions:", 1)[0]


def test_workflows():
    for name, job, secret, paths in (
            ("winget.yml", "submit", "WINGET_TOKEN",
             ("packaging/winget/**", "packaging/scoop/**", "tools/windows_channels.py")),
            ("chocolatey.yml", "push", "CHOCOLATEY_API_KEY",
             ("packaging/chocolatey/**", "tools/windows_channels.py"))):
        wf = read(name)
        on = _on(wf)
        assert "\n  release:\n    types: [published]\n" in on, name
        assert re.search(r"\n  workflow_dispatch:\n    inputs:\n      tag:\n", on), name
        assert "\n  pull_request:\n    paths:\n" in on and "pull_request_target" not in wf, name
        for p in (*paths, f".github/workflows/{name}"):
            assert f"      - {p}\n" in on, (name, p)
        assert "\npermissions:\n  contents: read\n" in wf, name
        all_jobs = jobs(wf)
        first = next(iter(all_jobs))
        assert "if: github.event_name != 'release' || !github.event.release.prerelease" \
            in all_jobs[first], f"{name}: a pre-release is not published"
        publish = all_jobs[job]
        assert f"    needs: {first}\n" in publish and \
            "    if: github.event_name != 'pull_request'\n" in publish, (name, job)
        assert permissions(publish) == {}, f"{name}: {job} needs no GITHUB_TOKEN permission"
        assert f"    environment: {name.split('.')[0]}\n" in publish
        assert "actions/checkout" not in publish, f"{name}: no repository code where the secret is"
        for block_name, block in all_jobs.items():
            if block_name != job:
                assert "secrets." not in block, f"{name}: {block_name} sees a secret"
            if "actions/checkout" in block:
                assert "persist-credentials: false" in block, (name, block_name)
        uses_secret = [s for s in steps(publish) if f"secrets.{secret}" in s]
        assert uses_secret and all(re.search(rf"\n          \w+: \$\{{\{{ secrets\.{secret} \}}\}}\n", s)
                                   for s in uses_secret), f"{name}: the secret only in env:"
        check = uses_secret[0]
        assert "::notice title=" in check and "not set" in check, f"{name}: no notice"
        for st in steps(publish)[1:]:
            assert re.search(r"\n        if: steps\.\w+\.outputs\.\w+ == 'true'\n", st), \
                f"{name}: a step runs without the secret check:\n{st}"
        for block in all_jobs.values():
            for st in steps(block):
                run = st.split("run: ", 1)[1] if "run: " in st else ""
                assert "${{" not in run, f"{name}: an expression inside run:\n{st}"
    winget = jobs(read("winget.yml"))
    assert "pip install jsonschema==4.26.0 pyyaml==6.0.3" in winget["manifests"]
    assert 'SER2NETWEB_SCHEMA_TESTS: "1"' in winget["manifests"]
    assert 'python packaging/winget/make_manifests.py "$TAG" --out out' in winget["manifests"]
    assert "python tools/windows_channels.py validate out" in winget["manifests"]
    submit = winget["submit"]
    assert re.search(r"echo \"[0-9a-f]{64}  komac\.tar\.gz\" \| sha256sum -c -", submit)
    assert "/releases/download/v2.17.2/komac-2.17.2-x86_64-unknown-linux-gnu.tar.gz" in submit
    assert "./komac/komac submit manifests --yes" in submit
    choco = jobs(read("chocolatey.yml"))
    package = choco["package"]
    assert "    runs-on: windows-latest\n" in package
    for needle in ('python tools/windows_channels.py chocolatey "$env:TAG"',
                   "choco pack packaging/chocolatey/ser2netweb.nuspec --outputdirectory out",
                   'choco install ser2netweb --version $env:VERSION --source "$((Resolve-Path out).Path)"',
                   "& ser2netweb --version", "'bin\\ser2netweb.exe'",
                   "'Build: standalone executable, Chocolatey package'",
                   "choco uninstall ser2netweb -y"):
        assert needle in package, needle
    assert layout.LABELS[layout.CHOCOLATEY] == "standalone executable, Chocolatey package"
    push = choco["push"]
    assert "AWARE: ${{ needs.package.outputs.aware }}" in push
    assert "--source https://push.chocolatey.org/ --api-key $env:CHOCOLATEY_API_KEY" in push
    print("winget.yml / chocolatey.yml: release (no pre-release) + by hand for a tag, pull "
          "requests tested; the secret only where it is used, skipped with a notice  OK")


def test_schemas():
    if not SCHEMA_TESTS:
        print("SKIP the manifests against winget's and Scoop's JSON schemas: set "
              "SER2NETWEB_SCHEMA_TESTS=1 (needs jsonschema, PyYAML, the network; winget.yml does)")
        return
    import yaml

    tag, sha = committed_release()
    folder = os.path.join(WINGET, wc.version_of(tag))
    scoop = os.path.join(ROOT, "packaging", "scoop", "ser2netweb.json")
    problems = wc.validate([folder, scoop])
    assert not problems, "\n".join(problems)
    for name, manifest in make_manifests.manifests(tag, sha).items():
        assert yaml.safe_load(text(os.path.join(folder, name))) == manifest, name
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, f"{wc.WINGET_ID}.installer.yaml")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write(text(os.path.join(folder, f"{wc.WINGET_ID}.installer.yaml"))
                     .replace("InstallerType: portable", "InstallerType: unknown"))
        assert any("InstallerType" in p for p in wc.validate([broken])), "not reported"
    assert wc.exe_sha256(tag) == sha, f"the committed SHA-256 is not {tag}'s"
    print(f"the {tag} manifests conform to winget's {wc.WINGET_MANIFEST_VERSION} and Scoop's "
          "schemas (pinned, SHA-256 checked) and read back as made; the SHA-256 is the "
          "release's  OK")


if __name__ == "__main__":
    test_tags_and_sums()
    test_winget_manifests()
    test_yaml_scalars()
    test_scoop_manifest()
    test_chocolatey_package()
    test_workflows()
    test_schemas()
    print("\nPASS: Windows package managers")
