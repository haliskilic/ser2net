"""systemd/ser2netweb-install.sh installs the Linux binary as a service (staged here).

The script runs as root on the target; here it only ever runs with --root <scratch folder>
(a staged tree: no user is created and systemd is left alone), under sh and bash, with a
stand-in binary. tests/install_smoke.sh runs it for real on CI's throwaway runner
(standalone.yml, job "install script (binary)"). Checked here:
- the binary, the unit and the data directory land where the unit and the README's manual
  steps put them (/usr/local/bin/ser2netweb, /etc/systemd/system/ser2netweb.service,
  /var/lib/ser2netweb 0700); with no --binary / --unit it takes the release's files next to it;
- the files are checked against the SHA256SUMS next to them (a mismatch installs nothing),
  and the binary must run and say it is ser2netweb (a 2.9.x binary, "ser2net 2.9.0", is not);
- --listen writes the address drop-in; a bad address installs nothing;
- running it again upgrades: the binary is replaced, the data stays, a unit changed in place
  is kept aside;
- it refuses to hide another ser2netweb.service (a package's, a source install in
  /opt/ser2netweb) unless --replace-unit; --uninstall removes only its own files;
- release.yml attaches it to every release, covered by SHA256SUMS;
- it moves a 2.9.x install over, as 2.9.0's own ser2net-install.sh (tests/fixtures) made it
  with --listen: /var/lib/ser2net becomes /var/lib/ser2netweb (moved, not copied), the
  address goes into the new listen drop-in, the user's own drop-ins move to
  ser2netweb.service.d, 2.9.x's noexec drop-in and cache go, and the old unit and binary are
  removed; a binary that isn't 2.9.x's stays, and so does data when /var/lib/ser2netweb
  holds some too (refused, nothing changed);
- a ser2net.service it can't tell is 2.9.x's by its content (the classic ser2net daemon's
  in /etc or /usr/lib, a hand-edited copy, a source install's) is never touched, nor its
  binary or /var/lib/ser2net; --uninstall also removes a 2.9.x install not moved over yet,
  and only that.

Run: python3 tests/test_install_script.py   (POSIX)
"""
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SCRIPT = os.path.join(ROOT, "systemd", "ser2netweb-install.sh")
# 2.9.0's script and the binary unit it installed, as released (the old names)
OLD_SCRIPT = os.path.join(HERE, "fixtures", "ser2net-install-2.9.0.sh")
OLD_UNIT = os.path.join(HERE, "fixtures", "ser2net-standalone-2.9.0.service")
OLD_BIN, OLD_UNIT_DEST = "usr/local/bin/ser2net", "etc/systemd/system/ser2net.service"
OLD_DATA, OLD_DROPIN = "var/lib/ser2net", "etc/systemd/system/ser2net.service.d"
UNIT = os.path.join(ROOT, "systemd", "ser2netweb-standalone.service")
BIN, UNIT_DEST = "usr/local/bin/ser2netweb", "etc/systemd/system/ser2netweb.service"
DATA, DROPIN = "var/lib/ser2netweb", "etc/systemd/system/ser2netweb.service.d"
ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().lower())


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fake_binary(path: str, version: str = "3.0.0", says: str = "", name: str = "ser2netweb") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + (f"echo {says}\n" if says else
                                  f'[ "$1" = --version ] && echo "{name} {version}"\n'))
    os.chmod(path, 0o644)  # as downloaded: not executable yet
    return path


def sha256sums(folder: str, *names: str) -> None:
    with open(os.path.join(folder, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        for name in names:
            with open(os.path.join(folder, name), "rb") as f:
                fh.write(f"{hashlib.sha256(f.read()).hexdigest()}  {name}\n")


class Release:
    """A folder as a release download leaves it (binary, unit, script, SHA256SUMS) and a
    staged root to install into."""

    def __init__(self, shell: str):
        self.shell = shell
        self.tmp = tempfile.mkdtemp(prefix="s2n_inst_")
        self.dl = os.path.join(self.tmp, "download")
        self.root = os.path.join(self.tmp, "root")
        os.makedirs(self.dl)
        os.makedirs(self.root)
        self.binary = fake_binary(os.path.join(self.dl, f"ser2netweb-linux-{ARCH or 'x64'}"))
        shutil.copy(UNIT, self.dl)
        shutil.copy(SCRIPT, self.dl)
        self.sums()

    def sums(self) -> None:
        sha256sums(self.dl, os.path.basename(self.binary), "ser2netweb-standalone.service",
                   "ser2netweb-install.sh")

    def run(self, *args: str, root: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.shell, os.path.join(self.dl, "ser2netweb-install.sh")]
        if ARCH is None:  # (no default binary name for this machine)
            cmd += ["--binary", self.binary]
        cmd += [*(["--root", self.root] if root else []), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                              cwd=self.tmp, stdin=subprocess.DEVNULL)

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def files(self) -> list[str]:
        out = []
        for folder, _dirs, names in os.walk(self.root):
            out += [os.path.relpath(os.path.join(folder, n), self.root) for n in names]
        return sorted(out)

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def ok(p: subprocess.CompletedProcess) -> str:
    assert p.returncode == 0, (p.returncode, p.stdout, p.stderr)
    return p.stdout


def refused(p: subprocess.CompletedProcess, *words: str) -> str:
    assert p.returncode != 0, (p.stdout, p.stderr)
    for w in words:
        assert w in p.stderr, (w, p.stderr)
    return p.stderr


def test_names_match_the_unit():
    unit, script = read(UNIT), read(SCRIPT)
    exec_start = re.search(r"(?m)^ExecStart=(\S+) --data-dir (\S+)$", unit)
    assert exec_start, unit
    assert "\nNAME=ser2netweb\n" in script
    script = script.replace("$NAME", "ser2netweb")
    assert f"BIN_DEST={exec_start.group(1)}\n" in script
    assert f"DATA={exec_start.group(2)}\n" in script
    assert "StateDirectory=ser2netweb\n" in unit and "DATA=/var/lib/ser2netweb\n" in script
    assert re.search(r"(?m)^User=ser2netweb$", unit) and "USER_NAME=ser2netweb\n" in script
    # the README's manual steps use the same names
    for readme in ("README.md", "README.en.md"):
        text = read(os.path.join(ROOT, readme))
        assert "sudo useradd --system --user-group --home-dir /var/lib/ser2netweb -M -s " \
               "/usr/sbin/nologin ser2netweb" in text, readme
        assert "/etc/systemd/system/ser2netweb.service" in text and "ser2netweb-install.sh" in text
    mode = os.stat(SCRIPT).st_mode
    assert mode & stat.S_IXUSR, "systemd/ser2netweb-install.sh is not executable"
    print("the script installs what the unit and the README name  OK")


def test_release_attaches_it():
    release = read(os.path.join(ROOT, ".github", "workflows", "release.yml"))
    assert "cp systemd/ser2netweb-install.sh out/ser2netweb-install.sh" in release
    files = release.index("cp systemd/ser2netweb-install.sh")
    assert files < release.index("sha256sum * > SHA256SUMS"), "listed in SHA256SUMS"
    print("release.yml attaches ser2netweb-install.sh, covered by SHA256SUMS  OK")


def test_fresh_install(shell: str):
    rel = Release(shell)
    try:
        out = ok(rel.run())
        assert rel.files() == sorted([BIN, UNIT_DEST]), rel.files()
        assert read(rel.path(BIN)) == read(rel.binary)
        assert stat.S_IMODE(os.stat(rel.path(BIN)).st_mode) == 0o755
        assert read(rel.path(UNIT_DEST)) == read(UNIT)
        assert stat.S_IMODE(os.stat(rel.path(UNIT_DEST)).st_mode) == 0o644
        assert os.path.isdir(rel.path(DATA))
        assert stat.S_IMODE(os.stat(rel.path(DATA)).st_mode) == 0o700
        name = os.path.basename(rel.binary)
        assert f"{name}: sha256 matches SHA256SUMS" in out, out
        assert "ser2netweb-standalone.service: sha256 matches" in out, out
        assert "binary: ser2netweb 3.0.0" in out, out
        assert "systemctl daemon-reload && systemctl enable --now ser2netweb" in out, out
        assert not os.path.exists(rel.path(BIN + ".new"))
    finally:
        rel.close()
    print(f"{shell}: a staged install puts the binary, the unit and a 0700 data directory "
          "in place (release files checked against SHA256SUMS)  OK")


def test_checks_before_installing(shell: str):
    rel = Release(shell)
    try:
        with open(rel.binary, "a", encoding="utf-8") as fh:
            fh.write("# changed\n")
        refused(rel.run(), "doesn't match", "SHA256SUMS", "download it again")
        assert rel.files() == [], rel.files()

        rel.sums()
        os.remove(os.path.join(rel.dl, "SHA256SUMS"))
        out = ok(rel.run())
        assert "no SHA256SUMS next to" in out and "not verified" in out, out

        for binary, words in ((fake_binary(rel.binary, says="hello"), ("is not ser2netweb",)),
                              (fake_binary(rel.binary, "2.9.0", name="ser2net"),
                               ("is not ser2netweb", "ser2net 2.9.0")),
                              (rel.binary, ())):
            shutil.rmtree(rel.root)
            os.makedirs(rel.root)
            if not words:  # a file that doesn't run at all
                with open(binary, "wb") as fh:
                    fh.write(b"\x7fELF\x00not really")
                words = ("doesn't run here",)
            refused(rel.run(), *words)
            assert rel.files() == [], rel.files()
        fake_binary(rel.binary)

        p = rel.run("--unit", os.path.join(ROOT, "systemd", "ser2netweb.service"))
        refused(p, "is not the standalone binary's unit")
        for bad in ("0.0.0.0", "0.0.0.0:99999", "host name:80", "1.2.3.4:http"):
            refused(rel.run("--listen", bad), "--listen")
        assert rel.files() == [], rel.files()
        refused(rel.run("--bogus"), "unknown option")
    finally:
        rel.close()
    print(f"{shell}: a changed file, a binary that isn't ser2netweb (or 2.9.x's) or doesn't run, the "
          "source unit, a bad --listen: nothing installed  OK")


def test_listen(shell: str):
    rel = Release(shell)
    try:
        out = ok(rel.run("--listen", "0.0.0.0:8080"))
        assert "listens on 0.0.0.0:8080" in out, out
        conf = read(rel.path(DROPIN + "/listen.conf"))
        assert "[Service]\nEnvironment=SER2NETWEB_BIND_IP=0.0.0.0 SER2NETWEB_PORT=8080\n" in conf, conf
        ok(rel.run("--listen", "[::]:9443"))
        conf = read(rel.path(DROPIN + "/listen.conf"))
        assert "Environment=SER2NETWEB_BIND_IP=:: SER2NETWEB_PORT=9443\n" in conf, conf
    finally:
        rel.close()
    print(f"{shell}: --listen writes the address drop-in (IPv4, [IPv6])  OK")


def test_upgrade(shell: str):
    rel = Release(shell)
    try:
        ok(rel.run())
        with open(rel.path(DATA + "/config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"kept": true}')
        fake_binary(rel.binary, version="3.0.1")
        rel.sums()
        out = ok(rel.run())
        assert "binary: ser2netweb 3.0.1" in out and "exists: kept as it is" in out, out
        assert "3.0.1" in read(rel.path(BIN))
        assert read(rel.path(DATA + "/config.json")) == '{"kept": true}'
        assert "differs" not in out, "an unchanged unit is not kept aside"

        with open(rel.path(UNIT_DEST), "a", encoding="utf-8") as fh:
            fh.write("# edited in place\n")
        out = ok(rel.run())
        kept = [f for f in rel.files() if re.fullmatch(re.escape(UNIT_DEST) + r"\.\d{14}", f)]
        assert len(kept) == 1 and "edited in place" in read(rel.path(kept[0])), rel.files()
        assert "the old one is kept as /" + kept[0] in out, out
        assert read(rel.path(UNIT_DEST)) == read(UNIT)
    finally:
        rel.close()
    print(f"{shell}: running it again upgrades the binary, keeps the data, keeps a unit "
          "edited in place aside  OK")


def test_other_units(shell: str):
    rel = Release(shell)
    try:
        packaged = rel.path("usr/lib/systemd/system/ser2netweb.service")
        os.makedirs(os.path.dirname(packaged))
        with open(packaged, "w", encoding="utf-8") as fh:
            fh.write("[Service]\nExecStart=/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb\n")
        refused(rel.run(), "the ser2netweb package", "systemctl enable --now ser2netweb",
                "--replace-unit")
        assert rel.files() == ["usr/lib/systemd/system/ser2netweb.service"], rel.files()
        out = ok(rel.run("--replace-unit"))
        assert "is hidden by /etc/systemd/system/ser2netweb.service" in out, out
        os.remove(packaged)

        source = "[Service]\nExecStart=/usr/bin/python3 /opt/ser2netweb/ser2netweb.py\n"
        with open(rel.path(UNIT_DEST), "w", encoding="utf-8") as fh:
            fh.write(source)
        refused(rel.run(), "a source install of ser2netweb (/opt/ser2netweb)", "--replace-unit")
        refused(rel.run("--uninstall"), "not the binary's unit")
        assert read(rel.path(UNIT_DEST)) == source
        ok(rel.run("--replace-unit"))
        assert read(rel.path(UNIT_DEST + ".replaced")) == source
        assert read(rel.path(UNIT_DEST)) == read(UNIT)
    finally:
        rel.close()
    print(f"{shell}: another ser2netweb.service (a package's, a source install's) is "
          "replaced only with --replace-unit  OK")


def test_uninstall(shell: str):
    rel = Release(shell)
    try:
        ok(rel.run("--listen", "127.0.0.1:8080"))
        with open(rel.path(DATA + "/config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        other = rel.path(DROPIN + "/override.conf")  # (the user's own: stays)
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("[Service]\n")
        out = ok(rel.run("--uninstall"))
        assert rel.files() == [DROPIN + "/override.conf", DATA + "/config.json"], rel.files()
        assert "kept /var/lib/ser2netweb and the user ser2netweb" in out, out
        os.remove(other)
        ok(rel.run("--uninstall"))  # nothing left: still fine
        assert rel.files() == [DATA + "/config.json"], rel.files()
    finally:
        rel.close()
    print(f"{shell}: --uninstall removes the binary, the unit and its drop-ins, keeps the "
          "data and other drop-ins  OK")


def write(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def old_install(rel: Release, *args: str) -> None:
    """What 2.9.0's ser2net-install.sh put in the staged root (its own script, its unit and
    a 2.9.0 binary that answers --version), a data directory as its service left it, plus
    the noexec drop-in it writes on a live system, a user's drop-in and the cache."""
    old = os.path.join(rel.tmp, "old")
    os.makedirs(old)
    fake_binary(os.path.join(old, "ser2net-linux-x64"), "2.9.0", name="ser2net")
    shutil.copy(OLD_UNIT, os.path.join(old, "ser2net-standalone.service"))
    p = subprocess.run([rel.shell, OLD_SCRIPT, "--root", rel.root, "--binary",
                        os.path.join(old, "ser2net-linux-x64"), "--unit",
                        os.path.join(old, "ser2net-standalone.service"), *args],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert read(rel.path(OLD_UNIT_DEST)) == read(OLD_UNIT)
    write(rel.path(OLD_DATA + "/config.json"), '{"admin_ui": {"port": 8080}, "kept": 1}')
    write(rel.path(OLD_DATA + "/tls/cert.pem"), "CERT")
    write(rel.path(OLD_DROPIN + "/tmpdir.conf"),
          "# written by ser2net-install.sh: /tmp is mounted noexec here, and the binary\n"
          "# unpacks itself on every start (into its cache directory instead)\n[Service]\n"
          "CacheDirectory=ser2net\nCacheDirectoryMode=0700\nEnvironment=TMPDIR=/var/cache/ser2net\n")
    write(rel.path(OLD_DROPIN + "/override.conf"), "[Service]\nSupplementaryGroups=uucp\n")
    write(rel.path("var/cache/ser2net/_MEIabc123/x"), "unpacked")


def snapshot(rel: Release) -> dict[str, str]:
    out = {}
    for f in rel.files():
        with open(rel.path(f), "rb") as fh:
            out[f] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_migration(shell: str):
    rel = Release(shell)
    try:
        old_install(rel, "--listen", "0.0.0.0:8080")
        out = ok(rel.run())
        files = rel.files()
        for gone in (OLD_UNIT_DEST, OLD_BIN, OLD_DATA, OLD_DROPIN, "var/cache/ser2net"):
            assert not os.path.lexists(rel.path(gone)), (gone, files)
        assert read(rel.path(DATA + "/config.json")) == '{"admin_ui": {"port": 8080}, "kept": 1}'
        assert read(rel.path(DATA + "/tls/cert.pem")) == "CERT"
        assert stat.S_IMODE(os.stat(rel.path(DATA)).st_mode) == 0o700
        assert read(rel.path(BIN)) == read(rel.binary) and read(rel.path(UNIT_DEST)) == read(UNIT)
        conf = read(rel.path(DROPIN + "/listen.conf"))
        assert "Environment=SER2NETWEB_BIND_IP=0.0.0.0 SER2NETWEB_PORT=8080\n" in conf, conf
        assert conf.startswith("# written by ser2netweb-install.sh --listen"), conf
        assert read(rel.path(DROPIN + "/override.conf")) == "[Service]\nSupplementaryGroups=uucp\n"
        assert not os.path.exists(rel.path(DROPIN + "/tmpdir.conf")), "2.9.x's noexec drop-in"
        assert sorted(f for f in files if f.startswith("etc/")) == sorted(
            [UNIT_DEST, DROPIN + "/listen.conf", DROPIN + "/override.conf"]), files
        for said in ("found 2.9.x's install (/etc/systemd/system/ser2net.service, /var/lib/ser2net)",
                     "the web UI listens on 0.0.0.0:8080, as 2.9.x's service did",
                     "moved /var/lib/ser2net to /var/lib/ser2netweb",
                     "moved the drop-in /etc/systemd/system/ser2net.service.d/override.conf to "
                     "/etc/systemd/system/ser2netweb.service.d/override.conf",
                     "removed 2.9.x's /etc/systemd/system/ser2net.service and /usr/local/bin/ser2net",
                     "  systemctl disable --now ser2net ", "  chown -R ser2netweb:ser2netweb /var/lib/ser2netweb"):
            assert said in out, (said, out)
        # once moved, a second run is an upgrade like any other
        out = ok(rel.run())
        assert "2.9.x" not in out and "exists: kept as it is" in out, out
        assert read(rel.path(DROPIN + "/listen.conf")) == conf
    finally:
        rel.close()

    rel = Release(shell)  # an IPv6 address; --listen given now wins over the old one
    try:
        old_install(rel, "--listen", "[::]:9443")
        ok(rel.run())
        assert "Environment=SER2NETWEB_BIND_IP=:: SER2NETWEB_PORT=9443\n" in read(
            rel.path(DROPIN + "/listen.conf"))
        shutil.rmtree(rel.root)
        os.makedirs(rel.root)
        old = os.path.join(rel.tmp, "old")
        shutil.rmtree(old)
        old_install(rel, "--listen", "0.0.0.0:8080")
        out = ok(rel.run("--listen", "127.0.0.1:8090"))
        assert "Environment=SER2NETWEB_BIND_IP=127.0.0.1 SER2NETWEB_PORT=8090\n" in read(
            rel.path(DROPIN + "/listen.conf")), out
        assert "as 2.9.x's service did" not in out, out
    finally:
        rel.close()

    rel = Release(shell)  # both data directories hold files: refused, nothing changed
    try:
        old_install(rel)
        write(rel.path(DATA + "/config.json"), "{}")
        before = snapshot(rel)
        refused(rel.run(), "both /var/lib/ser2net", "/var/lib/ser2netweb hold files")
        assert snapshot(rel) == before
    finally:
        rel.close()

    rel = Release(shell)  # a binary there that doesn't say it is 2.9.x's: kept
    try:
        old_install(rel)
        write(rel.path(OLD_BIN), "#!/bin/sh\necho ser2net version 4.3.11\n", 0o755)
        out = ok(rel.run())
        assert read(rel.path(OLD_BIN)) == "#!/bin/sh\necho ser2net version 4.3.11\n"
        assert not os.path.exists(rel.path(OLD_UNIT_DEST))
        assert "kept /usr/local/bin/ser2net: it doesn't say it is 2.9.x's binary" in out, out
        assert "removed 2.9.x's /etc/systemd/system/ser2net.service\n" in out, out
    finally:
        rel.close()
    print(f"{shell}: a 2.9.x install is moved over (data moved, address and drop-ins kept, old "
          "unit, binary, noexec drop-in and cache removed); a second run is a plain upgrade; "
          "both data folders in use: refused; a binary that isn't 2.9.x's stays  OK")


CLASSIC_UNIT = """[Unit]
Description=Serial port to network proxy
After=network.target

[Service]
EnvironmentFile=-/etc/default/ser2net
ExecStart=/usr/sbin/ser2net -n -c /etc/ser2net/ser2net.yaml
User=ser2net
Group=dialout

[Install]
WantedBy=multi-user.target
"""


def test_foreign_ser2net_untouched(shell: str):
    """The classic C ser2net daemon (and anything else called ser2net that 2.9.x's script
    didn't install) is left exactly as it is, by an install and by --uninstall."""
    edited = read(OLD_UNIT).replace("--data-dir /var/lib/ser2net", "--data-dir /srv/plant")
    source = "[Service]\nUser=ser2net\nExecStart=/usr/bin/python3 /opt/ser2net/ser2net.py " \
             "--data-dir /opt/ser2net/data\n"
    for unit, said in ((CLASSIC_UNIT, "is not the one ser2net-install.sh of 2.9.x installed"),
                       (edited, "is not the one ser2net-install.sh of 2.9.x installed"),
                       (source, "runs a source install in /opt/ser2net")):
        rel = Release(shell)
        try:
            write(rel.path(OLD_UNIT_DEST), unit)
            write(rel.path("usr/lib/systemd/system/ser2net.service"), CLASSIC_UNIT)
            write(rel.path("usr/sbin/ser2net"), "#!/bin/sh\necho ser2net version 4.3.11\n", 0o755)
            write(rel.path(OLD_BIN), "#!/bin/sh\necho ser2net 2.9.0\n", 0o755)
            write(rel.path(OLD_DATA + "/config.json"), '{"admin_ui": {}}')
            write(rel.path(OLD_DROPIN + "/listen.conf"),
                  "# written by ser2net-install.sh --listen\n[Service]\n"
                  "Environment=SER2NET_BIND_IP=0.0.0.0 SER2NET_PORT=8080\n")
            theirs = snapshot(rel)
            out = ok(rel.run("--remove-old-user"))
            assert said in out, out
            assert {f: h for f, h in snapshot(rel).items() if f in theirs} == theirs, out
            assert not os.listdir(rel.path(DATA)), "nothing of /var/lib/ser2net was taken"
            assert not os.path.exists(rel.path(DROPIN + "/listen.conf")), "nor its address"
            out = ok(rel.run("--uninstall"))
            assert {f: h for f, h in snapshot(rel).items() if f in theirs} == theirs, out
            assert not os.path.exists(rel.path(BIN)) and "2.9.x" not in out, out
        finally:
            rel.close()
    print(f"{shell}: a ser2net.service that isn't 2.9.x's binary unit (the classic daemon's, a "
          "hand-edited copy, a source install's), its binary and /var/lib/ser2net are left "
          "alone by an install and --uninstall  OK")


def test_uninstall_old(shell: str):
    """--uninstall before the move: 2.9.x's unit, the drop-ins its script wrote and its
    binary go too; both data directories and the user's own drop-in stay."""
    rel = Release(shell)
    try:
        old_install(rel, "--listen", "0.0.0.0:8080")
        out = ok(rel.run("--uninstall"))
        files = rel.files()
        # (the user's own drop-in stays, as with ser2netweb.service's)
        assert [f for f in files if f.startswith(("etc/", "usr/"))] == [
            OLD_DROPIN + "/override.conf"], files
        assert read(rel.path(OLD_DATA + "/config.json")).startswith('{"admin_ui"'), files
        assert ("removed /usr/local/bin/ser2netweb and /etc/systemd/system/ser2netweb.service, "
                "2.9.x's /etc/systemd/system/ser2net.service and /usr/local/bin/ser2net; kept "
                "/var/lib/ser2netweb and the user ser2netweb, and /var/lib/ser2net and the user "
                "ser2net") in out, out
    finally:
        rel.close()
    print(f"{shell}: --uninstall removes a 2.9.x install not moved over yet too, keeps both "
          "data directories  OK")


def test_live_needs_root():
    if os.geteuid() == 0:
        print("SKIP the not-root refusal: running as root")
        return
    rel = Release("sh")
    try:
        refused(rel.run(root=False), "run it as root")
        assert rel.files() == []
    finally:
        rel.close()
    print("without --root it needs root (and systemd)  OK")


def main():
    if os.name != "posix" or sys.platform == "darwin":
        print("SKIP: the install script is for Linux")
        return
    test_names_match_the_unit()
    test_release_attaches_it()
    test_live_needs_root()
    shells = [s for s in ("sh", "dash", "bash") if shutil.which(s)]
    for shell in dict.fromkeys(os.path.realpath(shutil.which(s)) for s in shells):
        test_fresh_install(shell)
        test_checks_before_installing(shell)
        test_listen(shell)
        test_upgrade(shell)
        test_other_units(shell)
        test_uninstall(shell)
        test_migration(shell)
        test_foreign_ser2net_untouched(shell)
        test_uninstall_old(shell)
    print("\nPASS: the Linux install script")


if __name__ == "__main__":
    main()
