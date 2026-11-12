"""systemd/ser2net-install.sh installs the Linux binary as a service (staged here).

The script runs as root on the target; here it only ever runs with --root <scratch folder>
(a staged tree: no user is created and systemd is left alone), under sh and bash, with a
stand-in binary. tests/install_smoke.sh runs it for real on CI's throwaway runner
(standalone.yml, job "install script (binary)"). Checked here:
- the binary, the unit and the data directory land where the unit and the README's manual
  steps put them (/usr/local/bin/ser2net, /etc/systemd/system/ser2net.service,
  /var/lib/ser2net 0700); with no --binary / --unit it takes the release's files next to it;
- the files are checked against the SHA256SUMS next to them (a mismatch installs nothing),
  and the binary must run and say it is ser2net;
- --listen writes the address drop-in; a bad address installs nothing;
- running it again upgrades: the binary is replaced, the data stays, a unit changed in place
  is kept aside;
- it refuses to hide another ser2net.service (the classic ser2net daemon's package, a source
  install in /opt/ser2net) unless --replace-unit; --uninstall removes only its own files;
- release.yml attaches it to every release, covered by SHA256SUMS.

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
SCRIPT = os.path.join(ROOT, "systemd", "ser2net-install.sh")
UNIT = os.path.join(ROOT, "systemd", "ser2net-standalone.service")
BIN, UNIT_DEST = "usr/local/bin/ser2net", "etc/systemd/system/ser2net.service"
DATA, DROPIN = "var/lib/ser2net", "etc/systemd/system/ser2net.service.d"
ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
    platform.machine().lower())


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fake_binary(path: str, version: str = "2.9.0", says: str = "") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + (f"echo {says}\n" if says else
                                  f'[ "$1" = --version ] && echo "ser2net {version}"\n'))
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
        self.binary = fake_binary(os.path.join(self.dl, f"ser2net-linux-{ARCH or 'x64'}"))
        shutil.copy(UNIT, self.dl)
        shutil.copy(SCRIPT, self.dl)
        self.sums()

    def sums(self) -> None:
        sha256sums(self.dl, os.path.basename(self.binary), "ser2net-standalone.service",
                   "ser2net-install.sh")

    def run(self, *args: str, root: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.shell, os.path.join(self.dl, "ser2net-install.sh")]
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
    assert f"BIN_DEST={exec_start.group(1)}\n" in script
    assert f"DATA={exec_start.group(2)}\n" in script
    assert "StateDirectory=ser2net\n" in unit and "DATA=/var/lib/ser2net\n" in script
    assert re.search(r"(?m)^User=ser2net$", unit) and "USER_NAME=ser2net\n" in script
    # the README's manual steps use the same names
    for readme in ("README.md", "README.en.md"):
        text = read(os.path.join(ROOT, readme))
        assert "sudo useradd --system --user-group --home-dir /var/lib/ser2net -M -s " \
               "/usr/sbin/nologin ser2net" in text, readme
        assert "/etc/systemd/system/ser2net.service" in text and "ser2net-install.sh" in text
    mode = os.stat(SCRIPT).st_mode
    assert mode & stat.S_IXUSR, "systemd/ser2net-install.sh is not executable"
    print("the script installs what the unit and the README name  OK")


def test_release_attaches_it():
    release = read(os.path.join(ROOT, ".github", "workflows", "release.yml"))
    assert "cp systemd/ser2net-install.sh out/ser2net-install.sh" in release
    files = release.index("cp systemd/ser2net-install.sh")
    assert files < release.index("sha256sum * > SHA256SUMS"), "listed in SHA256SUMS"
    print("release.yml attaches ser2net-install.sh, covered by SHA256SUMS  OK")


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
        assert "ser2net-standalone.service: sha256 matches" in out, out
        assert "binary: ser2net 2.9.0" in out, out
        assert "systemctl daemon-reload && systemctl enable --now ser2net" in out, out
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

        for binary, words in ((fake_binary(rel.binary, says="hello"), ("is not ser2net",)),
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

        p = rel.run("--unit", os.path.join(ROOT, "systemd", "ser2net.service"))
        refused(p, "is not the standalone binary's unit")
        for bad in ("0.0.0.0", "0.0.0.0:99999", "host name:80", "1.2.3.4:http"):
            refused(rel.run("--listen", bad), "--listen")
        assert rel.files() == [], rel.files()
        refused(rel.run("--bogus"), "unknown option")
    finally:
        rel.close()
    print(f"{shell}: a changed file, a binary that isn't ser2net or doesn't run, the "
          "source unit, a bad --listen: nothing installed  OK")


def test_listen(shell: str):
    rel = Release(shell)
    try:
        out = ok(rel.run("--listen", "0.0.0.0:8080"))
        assert "listens on 0.0.0.0:8080" in out, out
        conf = read(rel.path(DROPIN + "/listen.conf"))
        assert "[Service]\nEnvironment=SER2NET_BIND_IP=0.0.0.0 SER2NET_PORT=8080\n" in conf, conf
        ok(rel.run("--listen", "[::]:9443"))
        conf = read(rel.path(DROPIN + "/listen.conf"))
        assert "Environment=SER2NET_BIND_IP=:: SER2NET_PORT=9443\n" in conf, conf
    finally:
        rel.close()
    print(f"{shell}: --listen writes the address drop-in (IPv4, [IPv6])  OK")


def test_upgrade(shell: str):
    rel = Release(shell)
    try:
        ok(rel.run())
        with open(rel.path(DATA + "/config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"kept": true}')
        fake_binary(rel.binary, version="2.9.1")
        rel.sums()
        out = ok(rel.run())
        assert "binary: ser2net 2.9.1" in out and "exists: kept as it is" in out, out
        assert "2.9.1" in read(rel.path(BIN))
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
        classic = rel.path("usr/lib/systemd/system/ser2net.service")
        os.makedirs(os.path.dirname(classic))
        with open(classic, "w", encoding="utf-8") as fh:
            fh.write("[Service]\nExecStart=/usr/sbin/ser2net -n -c /etc/ser2net/ser2net.yaml\n")
        refused(rel.run(), "classic ser2net daemon", "systemctl disable --now ser2net",
                "--replace-unit")
        assert rel.files() == ["usr/lib/systemd/system/ser2net.service"], rel.files()
        out = ok(rel.run("--replace-unit"))
        assert "is hidden by /etc/systemd/system/ser2net.service" in out, out
        os.remove(classic)

        source = "[Service]\nExecStart=/usr/bin/python3 /opt/ser2net/ser2net.py\n"
        with open(rel.path(UNIT_DEST), "w", encoding="utf-8") as fh:
            fh.write(source)
        refused(rel.run(), "a source install of ser2net (/opt/ser2net)", "--replace-unit")
        refused(rel.run("--uninstall"), "not the binary's unit")
        assert read(rel.path(UNIT_DEST)) == source
        ok(rel.run("--replace-unit"))
        assert read(rel.path(UNIT_DEST + ".replaced")) == source
        assert read(rel.path(UNIT_DEST)) == read(UNIT)
    finally:
        rel.close()
    print(f"{shell}: another ser2net.service (the classic daemon's, a source install's) "
          "is replaced only with --replace-unit  OK")


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
        assert "kept /var/lib/ser2net and the user ser2net" in out, out
        os.remove(other)
        ok(rel.run("--uninstall"))  # nothing left: still fine
        assert rel.files() == [DATA + "/config.json"], rel.files()
    finally:
        rel.close()
    print(f"{shell}: --uninstall removes the binary, the unit and its drop-ins, keeps the "
          "data and other drop-ins  OK")


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
    print("\nPASS: the Linux install script")


if __name__ == "__main__":
    main()
