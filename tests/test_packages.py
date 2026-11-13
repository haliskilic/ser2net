"""The .deb and .rpm packages of the Linux executable (packaging/, tools/build_packages.py).

  * the unit they install is systemd/ser2netweb-standalone.service running
    /usr/bin/ser2netweb --data-dir /var/lib/ser2netweb, nothing else changed (the user, the
    StateDirectory, the sandbox), and systemd-analyze verify accepts it (Linux, where
    systemd-analyze is installed);
  * packaging/nfpm.yaml installs exactly the executable, that unit, the sysusers.d and
    tmpfiles.d entries (the account ser2netweb with its home /var/lib/ser2netweb, the folder
    0700 and the account's) and the licences in /usr/share/doc/ser2netweb, takes its version
    from ser2netweb/__init__.py, and names no other package (no Provides, Conflicts,
    Replaces, Breaks: the classic ser2net daemon's package is unrelated);
  * the file names are the formats' own, with the version as tagged (no "~");
  * the scripts (POSIX sh) never enable or start the service: on an install and an upgrade
    they make the account (systemd-sysusers, else groupadd / useradd) and the folder
    (systemd-tmpfiles, else mkdir / chown / chmod) and reload systemd, an upgrade restarts
    it only when it runs (try-restart), a removal disables and stops it and leaves the data
    and the account; as dpkg and rpm call them (configure <old>, remove, upgrade; 1, 2, 0),
    run with stand-ins for those programs (Linux);
  * with SER2NETWEB_NFPM=<nfpm> (and dpkg-deb for a look inside), the packages of a stand-in
    executable: their files, version and dependencies.
What only an install shows (dpkg / rpm in clean Debian, Ubuntu, Fedora and AlmaLinux
containers, and on a real systemd) is CI's (tests/package_smoke.sh).

Run: python3 tests/test_packages.py   (binds nothing)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_packages
import release_guard
from test_systemd_units import parse

PKG = os.path.join(ROOT, "packaging")
SCRIPTS = ("postinstall.sh", "preremove.sh", "postremove.sh")
WANT_FILES = {
    "/usr/bin/ser2netweb", "/usr/lib/systemd/system/ser2netweb.service",
    "/usr/lib/sysusers.d/ser2netweb.conf", "/usr/lib/tmpfiles.d/ser2netweb.conf",
    *(f"/usr/share/doc/ser2netweb/{n}" for n in (
        "LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "THIRD-PARTY-NOTICES.md",
        "THIRD-PARTY-LICENSES.txt"))}


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def test_unit():
    text = build_packages.unit()
    unit = parse_text(text)
    standalone = parse(build_packages.STANDALONE_UNIT)
    start = dict(unit["Service"])["ExecStart"]
    assert start == "/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb", start
    assert "/usr/local/" not in text, "nothing of the install script's paths"
    swapped = [(k, v.replace("/usr/local/bin/", "/usr/bin/")) for k, v in standalone["Service"]]
    assert unit["Service"] == swapped and unit["Unit"] == standalone["Unit"] and \
        unit["Install"] == standalone["Install"], "only the executable's path differs"
    assert "sudo systemctl enable --now ser2netweb" in text
    with tempfile.TemporaryDirectory() as tmp:  # the same rules as the units in systemd/
        path = os.path.join(tmp, "ser2netweb.service")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        _verify(path)
    print("packaged unit: the standalone unit with ExecStart=/usr/bin/ser2netweb --data-dir "
          "/var/lib/ser2netweb, nothing else changed  OK")


def parse_text(text):
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(text)
    try:
        return parse(fh.name)
    finally:
        os.unlink(fh.name)


def _verify(path):
    exe = shutil.which("systemd-analyze")
    if not sys.platform.startswith("linux") or exe is None:
        print("SKIP systemd-analyze verify of the packaged unit: no systemd-analyze here")
        return
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not os.access("/usr/bin/ser2netweb", os.X_OK):  # (as test_systemd_units does)
        text = text.replace("ExecStart=/usr/bin/ser2netweb ", "ExecStart=/bin/sh ")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    p = subprocess.run([exe, "verify", "--man=no", path], capture_output=True, text=True,
                       timeout=60)
    own = [ln for ln in (p.stdout + p.stderr).splitlines() if "ser2netweb.service" in ln]
    assert p.returncode == 0 and not own, p.stdout + p.stderr
    print("packaged unit: systemd-analyze verify  OK")


def _config():
    """nfpm.yaml's top-level keys and its contents' (src, dst, packager) entries (the
    file's own simple YAML: block style, two-space indents)."""
    text = read(PKG, "nfpm.yaml")
    keys = re.findall(r"(?m)^([a-z_]+):", text)
    entries = []
    for block in re.split(r"(?m)^  - ", text.split("\ncontents:\n", 1)[1].split("\nscripts:", 1)[0])[1:]:
        fields = dict(re.findall(r"(?m)^\s*(src|dst|packager|expand): (\S+)", "    " + block))
        entries.append(fields)
    return text, keys, entries


def test_config():
    text, keys, entries = _config()
    assert re.search(r"(?m)^name: ser2netweb$", text)
    assert re.search(r"(?m)^version: \$\{SER2NETWEB_VERSION\}$", text)
    assert re.search(r"(?m)^version_schema: semver$", text), "3.0.0-rc.1 -> 3.0.0~rc.1"
    assert re.search(r"(?m)^arch: \$\{SER2NETWEB_ARCH\}$", text)
    assert re.search(r"(?m)^license: AGPL-3.0-only$", text)
    for key in ("provides", "conflicts", "replaces", "breaks"):
        assert not re.search(rf"(?m)^\s*{key}:", text), f"nfpm.yaml names other packages ({key})"
    assert not re.search(r"(?mi)^\s*-\s*ser2net\b(?!web)", text), "no other package by name"
    dsts = {e["dst"] for e in entries}
    assert dsts == WANT_FILES | {"/usr/share/doc/ser2netweb/copyright"}, sorted(dsts)
    for e in entries:
        src = e["src"]
        if src.startswith("${"):
            assert e.get("expand") == "true", e
            assert src[2:-1] in ("SER2NETWEB_BINARY", "SER2NETWEB_UNIT", "SER2NETWEB_LICENSES")
        else:
            assert os.path.isfile(os.path.join(ROOT, src)), src
    assert {e["dst"]: e.get("packager") for e in entries}[
        "/usr/share/doc/ser2netweb/copyright"] == "deb"
    for name in SCRIPTS:
        kind = name.removesuffix(".sh")
        assert f"  {kind}: packaging/{name}\n" in text, name
    assert read(PKG, "sysusers.conf").splitlines()[-1] == \
        'u ser2netweb - "ser2netweb serial-to-network bridge" /var/lib/ser2netweb'
    assert read(PKG, "tmpfiles.conf").splitlines()[-1] == \
        "d /var/lib/ser2netweb 0700 ser2netweb ser2netweb -"
    print("nfpm.yaml: the executable, the unit, sysusers.d / tmpfiles.d, the licences; no "
          "other package named  OK")


def test_names():
    assert release_guard.release_packages("3.0.0") == [
        "ser2netweb_3.0.0_amd64.deb", "ser2netweb-3.0.0-1.x86_64.rpm",
        "ser2netweb_3.0.0_arm64.deb", "ser2netweb-3.0.0-1.aarch64.rpm"]
    rc = release_guard.release_packages("3.0.0-rc.1")
    assert rc[0] == "ser2netweb_3.0.0-rc.1_amd64.deb" and \
        rc[1] == "ser2netweb-3.0.0-rc.1-1.x86_64.rpm", rc
    assert not any("~" in n for n in rc)
    assert build_packages.package_names("3.0.0")["arm64"] == (
        "ser2netweb_3.0.0_arm64.deb", "ser2netweb-3.0.0-1.aarch64.rpm")
    assert set(build_packages.ARCHES) == {exe.rsplit("-", 1)[1]
                                          for exe in release_guard.PACKAGE_ARCHES}
    for machine, digest in build_packages.NFPM_SHA256.items():
        assert re.fullmatch(r"[0-9a-f]{64}", digest), machine
    print("package file names: Debian's and RPM's own, the version as tagged  OK")


_STUBS = ("systemctl", "systemd-sysusers", "systemd-tmpfiles", "getent", "groupadd",
          "useradd", "mkdir", "chown", "chmod")


def _run_script(name, args, have=(), known=()):
    """Run packaging/<name> with stand-ins for the programs it calls (only those in
    `have`, of systemd's; getent knows the entries in `known`). Returns their calls."""
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "calls")
        for prog in _STUBS:
            if prog.startswith("systemd-") and prog not in have:
                continue
            body = f'echo "{prog} $*" >> "{log}"\n'
            if prog == "getent":  # getent passwd|group NAME: found when known
                body += 'case " $KNOWN " in *" $1:$2 "*) exit 0 ;; esac\nexit 2\n'
            with open(os.path.join(tmp, prog), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n" + body)
            os.chmod(os.path.join(tmp, prog), 0o755)
        env = {"PATH": tmp, "KNOWN": " ".join(known)}
        p = subprocess.run(["/bin/sh", os.path.join(PKG, name), *args], env=env,
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, (name, args, p.stdout, p.stderr)
        if not os.path.exists(log):
            return []
        with open(log, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if not ln.startswith("getent ")]


def test_scripts():
    for name in SCRIPTS:
        text = read(PKG, name)
        assert text.startswith("#!/bin/sh\n"), name
        assert os.access(os.path.join(PKG, name), os.X_OK) or os.name == "nt", name
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            assert not re.search(r"systemctl\b.*\b(enable|start|restart)\b", code) or \
                "try-restart" in code or "disable" in code, (name, line)
    if os.name == "nt" or not os.path.exists("/bin/sh"):
        print("SKIP the package scripts' runs: no /bin/sh here")
        return
    for name in SCRIPTS:
        subprocess.run(["/bin/sh", "-n", os.path.join(PKG, name)], check=True, timeout=30)
    running = os.path.isdir("/run/systemd/system")  # (the scripts check it)
    reload_ = ["systemctl daemon-reload"] if running else []
    restart = ["systemctl try-restart ser2netweb.service"] if running else []
    sysusers = ["systemd-sysusers /usr/lib/sysusers.d/ser2netweb.conf",
                "systemd-tmpfiles --create /usr/lib/tmpfiles.d/ser2netweb.conf"]
    by_hand = ["groupadd --system ser2netweb",
               "useradd --system --gid ser2netweb --home-dir /var/lib/ser2netweb "
               "--no-create-home --shell /usr/sbin/nologin --comment ser2netweb "
               "serial-to-network bridge ser2netweb",
               "mkdir -p /var/lib/ser2netweb", "chown ser2netweb:ser2netweb /var/lib/ser2netweb",
               "chmod 0700 /var/lib/ser2netweb"]
    have = ("systemd-sysusers", "systemd-tmpfiles")
    # an install (.deb: configure with no old version; .rpm: 1) and an upgrade (2.9.0; 2)
    for args, upgrade in ((["configure", ""], False), (["configure"], False), (["1"], False),
                          (["configure", "3.0.0"], True), (["2"], True)):
        got = _run_script("postinstall.sh", args, have)
        assert got == sysusers + reload_ + (restart if upgrade else []), (args, got)
    assert _run_script("postinstall.sh", ["configure", ""]) == by_hand + reload_
    kept = _run_script("postinstall.sh", ["2"], known=["passwd:ser2netweb"])
    assert kept == by_hand[2:] + reload_ + restart, kept  # (the account is there already)
    assert _run_script("postinstall.sh", ["1"], known=["group:ser2netweb"])[0].startswith(
        "useradd "), "an existing group is used"
    # removal stops and disables; an upgrade (the old package's scripts) touches nothing
    stop = (["systemctl --no-reload disable --now ser2netweb.service"] if running else
            ["systemctl --no-reload disable ser2netweb.service"])
    for args in (["remove"], ["0"]):
        assert _run_script("preremove.sh", args, have) == stop, args
        assert _run_script("postremove.sh", args, have) == reload_, args
    assert _run_script("postremove.sh", ["purge"], have) == reload_
    for args in (["upgrade", "3.0.1"], ["1"], ["failed-upgrade", "3.0.1"], ["deconfigure"]):
        assert _run_script("preremove.sh", args, have) == [], args
    for args in (["upgrade", "3.0.1"], ["1"], ["abort-install"]):
        assert _run_script("postremove.sh", args, have) == [], args
    print("package scripts: the account and the folder (sysusers / tmpfiles, else useradd / "
          "mkdir), daemon-reload, try-restart on an upgrade, disable --now on a removal; "
          f"never enable or start{'' if running else ' (systemd not running here)'}  OK")


def test_build_with_nfpm():
    nfpm = os.environ.get("SER2NETWEB_NFPM")
    if not nfpm:
        print("SKIP the packages of a stand-in executable: set SER2NETWEB_NFPM=<nfpm> "
              "(python3 tools/build_packages.py fetch-nfpm DIR)")
        return
    version = release_guard.app_version(ROOT)
    with tempfile.TemporaryDirectory() as tmp:
        art = os.path.join(tmp, "artifacts")
        for arch in build_packages.ARCHES:
            for rel, text in ((f"ser2netweb-linux-{arch}/ser2netweb", "#!/bin/sh\n"),
                              (f"meta-linux-{arch}/THIRD-PARTY-LICENSES.txt", "texts\n")):
                os.makedirs(os.path.dirname(os.path.join(art, rel)), exist_ok=True)
                with open(os.path.join(art, rel), "w", encoding="utf-8") as fh:
                    fh.write(text)
        made = build_packages.build(nfpm, art, os.path.join(tmp, "out"))
        assert sorted(map(os.path.basename, made)) == sorted(release_guard.release_packages())
        deb = os.path.join(tmp, "out", f"ser2netweb_{version}_amd64.deb")
        if shutil.which("dpkg-deb"):
            files = {ln.split()[-1].lstrip(".") for ln in subprocess.run(
                ["dpkg-deb", "-c", deb], capture_output=True, text=True, check=True).stdout
                .splitlines() if not ln.startswith("d")}
            assert files == WANT_FILES | {"/usr/share/doc/ser2netweb/copyright"}, sorted(files)
            fields = subprocess.run(["dpkg-deb", "-f", deb], capture_output=True, text=True,
                                    check=True).stdout
            assert f"\nVersion: {version.replace('-', '~')}\n" in fields, fields
            assert "\nDepends: libc6 (>= 2.28)\n" in fields, fields
            for key in ("Provides", "Conflicts", "Replaces", "Breaks"):
                assert f"\n{key}:" not in fields, fields
            print(f"{os.path.basename(deb)}: files, version, Depends  OK")
        else:
            print("SKIP a look inside the .deb: no dpkg-deb here")
    print("the packages of a stand-in executable, named as release_guard lists them  OK")


if __name__ == "__main__":
    test_unit()
    test_config()
    test_names()
    test_scripts()
    test_build_with_nfpm()
    print("\nPASS: .deb / .rpm packages")
