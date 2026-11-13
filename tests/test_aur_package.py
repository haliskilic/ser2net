"""The AUR package ser2netweb-bin: packaging/aur/make_pkgbuild.py and .github/workflows/aur.yml.

  * the unit is the .deb's (tools/build_packages.py unit()) with Arch's serial group uucp
    instead of dialout (Arch has none) and its own header, nothing else changed;
  * for a release the script takes everything from it: the executables' and their licence
    lists' sha256 from SHA256SUMS, the unit from its ser2netweb-standalone.service (which
    must match SHA256SUMS), the account files and licence texts from the tag; it refuses a
    pre-release tag and a release that lacks a file;
  * the PKGBUILD installs what the .deb does (/usr/bin/ser2netweb, the unit, sysusers.d,
    tmpfiles.d, the licences in Arch's place) unstripped, names no package but ser2netweb
    (not the classic ser2net), and .SRCINFO says the same (bash expands the PKGBUILD here
    when it is there; CI compares with makepkg --printsrcinfo);
  * packaging/aur/ser2netweb-bin is what the script makes for its version (sha256 as recorded;
    CI checks them against the release);
  * aur.yml: on a release (not a pre-release; release.yml's channels job starts it with the
    tag), by hand with a tag, and on pull requests that
    change the package; makepkg / namcap / pacman in an archlinux container before anything
    is pushed; the push needs AUR_SSH_PRIVATE_KEY (skipped with a notice without it), checks
    the AUR's pinned host keys and has no GitHub permission.

Run: python3 tests/test_aur_package.py   (stdlib only, no network)
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys

from _workflows import ROOT, jobs, key, needs, read, steps

sys.path[:0] = [os.path.join(ROOT, "packaging", "aur"), os.path.join(ROOT, "tools")]

import build_packages
import make_pkgbuild as mp

COMMITTED = os.path.join(ROOT, "packaging", "aur", "ser2netweb-bin")


def _text(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


def test_unit():
    deb, arch = build_packages.unit(), mp.unit()
    assert arch.startswith("[Unit]\n" + mp.UNIT_HEADER), arch[:200]
    body = arch[len("[Unit]\n" + mp.UNIT_HEADER):]
    assert body == deb[len("[Unit]\n" + build_packages.PACKAGE_HEADER):].replace(
        "\nSupplementaryGroups=dialout\n", "\nSupplementaryGroups=uucp\n").replace(
        "a member of the 'dialout' group\n",
        "a member of the 'uucp' group (Arch's; others call it dialout)\n"), "only the group"
    for line in ("ExecStart=/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb",
                 "User=ser2netweb", "SupplementaryGroups=uucp", "StateDirectory=ser2netweb"):
        assert f"\n{line}\n" in arch, line
    assert "SupplementaryGroups=dialout" not in arch
    print("unit: the .deb's, with the group uucp (Arch's serial group)  OK")


def _release(version: str, unit_text: str = ""):
    """A stand-in release of `version`: (get, its SHA256SUMS text)."""
    unit = (unit_text or _text("systemd", "ser2netweb-standalone.service")).encode()
    assets = {"ser2netweb-standalone.service": unit}
    for exe in mp.ARCHES.values():
        assets[exe] = f"ELF {exe}".encode()
        assets[f"{exe}.THIRD-PARTY-LICENSES.txt"] = f"licences of {exe}".encode()
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                   for name, data in sorted(assets.items()))
    tag = "v" + version

    def get(url):
        for name, data in assets.items():
            if url == mp.RELEASE.format(tag=tag, name=name):
                return data
        if url == mp.RELEASE.format(tag=tag, name="SHA256SUMS"):
            return sums.encode()
        prefix = mp.RAW.format(tag=tag, name="")
        assert url.startswith(prefix), url
        return _text(*url[len(prefix):].split("/")).encode()
    return get, sums, assets


def _bash_arrays(pkgbuild: str) -> dict:
    """The PKGBUILD's variables and arrays as bash expands them (None without bash)."""
    bash = shutil.which("bash")
    if not bash or os.name == "nt":
        return {}
    names = ["pkgname", "pkgver", "pkgrel", "pkgdesc", "url", "install", "arch", "license",
             "depends", "provides", "conflicts", "options", "source", "sha256sums",
             *(f"{a}_{arch}" for arch in mp.ARCHES for a in ("source", "sha256sums"))]
    script = "set -e; source /dev/stdin; " + "; ".join(
        f'for v in "${{{n}[@]}}"; do printf "%s\\t%s\\n" {n} "$v"; done' for n in names)
    out = subprocess.run([bash, "-c", script], input=pkgbuild, capture_output=True, text=True,
                         check=True, timeout=60).stdout
    values: dict = {}
    for line in out.splitlines():
        name, value = line.split("\t", 1)
        values.setdefault(name, []).append(value)
    return values


def _srcinfo(text: str) -> dict:
    values: dict = {}
    head, _sep, tail = text.partition("\n\n")
    assert head.startswith("pkgbase = ser2netweb-bin\n") and tail == "pkgname = ser2netweb-bin\n"
    for line in head.splitlines()[1:]:
        assert line.startswith("\t"), line
        name, _eq, value = line[1:].partition(" = ")
        values.setdefault(name, []).append(value)
    return values


def test_make():
    version = "3.4.5"
    get, sums, assets = _release(version)
    files = mp.make("v" + version, get)
    assert set(files) == {"PKGBUILD", ".SRCINFO", "ser2netweb.service", "ser2netweb.sysusers",
                          "ser2netweb.tmpfiles", "ser2netweb-bin.install"}, sorted(files)
    assert files["ser2netweb.service"] == mp.unit()
    assert files["ser2netweb.sysusers"] == _text("packaging", "sysusers.conf")
    assert files["ser2netweb.tmpfiles"] == _text("packaging", "tmpfiles.conf")
    pkgbuild = files["PKGBUILD"]
    for line in ("pkgname=ser2netweb-bin", f"pkgver={version}", "pkgrel=1",
                 "arch=('x86_64' 'aarch64')", "license=('AGPL-3.0-only')",
                 "depends=('glibc' 'zlib')", "provides=('ser2netweb')",
                 "conflicts=('ser2netweb')", "options=('!strip' '!debug')",
                 "install=ser2netweb-bin.install"):
        assert f"\n{line}\n" in pkgbuild, line
    assert not re.search(r"'ser2net'|\bser2net\b(?!web)", pkgbuild), "no classic ser2net"
    released = mp.sha256sums(sums)
    for exe in mp.ARCHES.values():
        assert f"'{released[exe]}'" in pkgbuild and f"'{released[exe + '.THIRD-PARTY-LICENSES.txt']}'" \
            in pkgbuild, exe
    for dest in ("/usr/bin/ser2netweb", "/usr/lib/systemd/system/ser2netweb.service",
                 "/usr/lib/sysusers.d/ser2netweb.conf", "/usr/lib/tmpfiles.d/ser2netweb.conf",
                 *(f"/usr/share/licenses/$pkgname/{n}" for n in (*mp.LICENCES,
                                                                "THIRD-PARTY-LICENSES.txt"))):
        assert f'"$pkgdir{dest}"' in pkgbuild, dest
    assert 'install -Dm755 "$exe-$pkgver" "$pkgdir/usr/bin/ser2netweb"' in pkgbuild
    info = _srcinfo(files[".SRCINFO"])
    assert info["pkgver"] == [version] and info["sha256sums_x86_64"][0] == \
        released["ser2netweb-linux-x64"]
    licence = hashlib.sha256(_text("LICENSE").encode()).hexdigest()
    assert licence in info["sha256sums"], "a licence text's sha256 as GitHub serves it"
    expanded = _bash_arrays(pkgbuild)
    if expanded:
        for name in ("pkgdesc", "pkgver", "pkgrel", "url", "install", "arch", "license",
                     "depends", "provides", "conflicts", "options", "source", "sha256sums",
                     "source_x86_64", "sha256sums_x86_64", "source_aarch64", "sha256sums_aarch64"):
            assert expanded[name] == info[name], (name, expanded[name], info[name])
        print("(.SRCINFO checked against the PKGBUILD as bash expands it)")
    for bad in ("v3.4.5-rc.1", "3.4.5", "v3.4"):
        try:
            mp.make(bad, get)
        except mp.Error:
            continue
        raise AssertionError(bad)
    get2, _sums, _assets = _release(version)
    try:
        mp.make("v" + version, get2, sums_text=sums.replace(
            hashlib.sha256(assets["ser2netweb-standalone.service"]).hexdigest(), "0" * 64))
    except mp.Error as e:
        assert "doesn't match" in str(e), e
    else:
        raise AssertionError("a unit that doesn't match SHA256SUMS")
    try:
        mp.make("v" + version, get, sums_text="".join(
            line + "\n" for line in sums.splitlines() if "arm64 " not in line + " "))
    except mp.Error as e:
        assert "has no ser2netweb-linux-arm64" in str(e), e
    else:
        raise AssertionError("a release without the arm64 executable")
    print("make: the release's executables, unit and SHA256SUMS, the tag's texts; the "
          "PKGBUILD installs what the .deb does, .SRCINFO says the same  OK")


def test_committed():
    pkgbuild = _text("packaging", "aur", "ser2netweb-bin", "PKGBUILD")
    version = re.search(r"(?m)^pkgver=(\S+)$", pkgbuild).group(1)
    common, per_arch = mp.sources(version)
    arrays = {name: re.findall(r"'([0-9a-f]{64})'", body) for name, body in
              re.findall(r"(?ms)^(sha256sums\w*)=\((.*?)\)$", pkgbuild)}
    sums = dict(zip([loc for loc, _u in common], arrays["sha256sums"]))
    for arch, entries in per_arch.items():
        sums.update(zip([loc for loc, _u in entries], arrays[f"sha256sums_{arch}"]))
    want, srcinfo = mp.render(version, sums)
    assert pkgbuild == want, "PKGBUILD is not what make_pkgbuild.py makes"
    assert _text("packaging", "aur", "ser2netweb-bin", ".SRCINFO") == srcinfo
    assert _text("packaging", "aur", "ser2netweb-bin", "ser2netweb-bin.install") == mp.INSTALL_SCRIPT
    for name in mp.LOCAL:
        text = _text("packaging", "aur", "ser2netweb-bin", name)
        assert sums[name] == hashlib.sha256(text.encode()).hexdigest(), name
    unit = _text("packaging", "aur", "ser2netweb-bin", "ser2netweb.service")
    assert unit.startswith("[Unit]\n" + mp.UNIT_HEADER) and "\nSupplementaryGroups=uucp\n" in unit
    assert "u ser2netweb - " in _text("packaging", "aur", "ser2netweb-bin", "ser2netweb.sysusers")
    assert "d /var/lib/ser2netweb 0700 ser2netweb ser2netweb -" in \
        _text("packaging", "aur", "ser2netweb-bin", "ser2netweb.tmpfiles")
    print(f"packaging/aur/ser2netweb-bin: what make_pkgbuild.py makes for {version}  OK")


def _started_by_release(workflow: str) -> None:
    """release.yml's channels job starts `workflow` on main with the tag (a release made with
    the GITHUB_TOKEN starts no workflow by itself)."""
    channels = jobs(read("release.yml"))["channels"]
    loop = next(line for line in channels.splitlines() if "for workflow in" in line)
    assert workflow in re.findall(r"[\w.-]+\.yml", loop), loop
    assert 'gh workflow run "$workflow" --ref main -f tag="$GITHUB_REF_NAME"' in channels


def test_workflow():
    text = read("aur.yml")
    on = text.split("\npermissions:", 1)[0]
    assert "\n  release:\n    types: [published]\n" in on
    assert re.search(r"(?m)^      tag:\n        description: .*\n        required: true", on)
    assert "\n  pull_request:\n    paths:\n" in on and "      - packaging/aur/**\n" in on
    assert "\npermissions:\n  contents: read\n" in text
    wf = jobs(text)
    package, publish = wf["package"], wf["publish"]
    assert key(package, "if") == "${{ !github.event.release.prerelease }}"
    assert key(package, "container") == "archlinux:base-devel"
    assert "python3 -I packaging/aur/make_pkgbuild.py \"$TAG\" --out build/aur" in package
    assert '--check' in package and "sh tests/aur_smoke.sh build/aur" in package
    assert "include-hidden-files: true" in package, ".SRCINFO is a hidden file"
    smoke = _text("tests", "aur_smoke.sh")
    for part in ("su builder -s /bin/sh", "makepkg --printsrcinfo", "makepkg --noconfirm",
                 "namcap", "pacman -U --noconfirm", "ser2netweb --version",
                 "SupplementaryGroups=uucp", "pacman -R --noconfirm ser2netweb-bin"):
        assert part in smoke, part
    assert needs(publish) == ["package"] and key(publish, "if") == "github.event_name != 'pull_request'"
    assert "\n    permissions: {}\n" in publish, "no GitHub permission where the AUR key is"
    st = steps(publish)
    check = next(s for s in st if "AUR_SSH_PRIVATE_KEY" in s and "::notice::" in s)
    assert 'if [ -z "$AUR_KEY" ]; then' in check
    push = next(s for s in st if "git push" in s)
    assert "        if: steps.key.outputs.push == 'true'\n" in push
    assert "-o StrictHostKeyChecking=yes" in push and "UserKnownHostsFile=" in push
    assert "ssh://aur@aur.archlinux.org/ser2netweb-bin.git" in push
    # the pinned keys are the ones whose fingerprints the comment names (the AUR's own)
    import base64
    keys = re.findall(r"(?m)^ +aur\.archlinux\.org (\S+) (\S+)$", push)
    assert [k for k, _v in keys] == ["ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"], keys
    for _kind, blob in keys:
        fp = base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip("=")
        assert f"SHA256:{fp}" in push, fp
    _started_by_release("aur.yml")
    print("aur.yml: releases (release.yml starts it), by hand and pull requests; archlinux "
          "build and install first; the push only with the key, to the pinned host keys, no "
          "GitHub permission  OK")


if __name__ == "__main__":
    test_unit()
    test_make()
    test_committed()
    test_workflow()
    print("\nPASS: AUR package")
