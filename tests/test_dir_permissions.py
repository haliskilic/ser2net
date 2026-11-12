"""State directories must be owner-private on both platforms (H5).

config.json holds the session secret_key and the admin password hash; the logs
dir can hold raw serial traffic. Previously only POSIX got chmod 0700 — on
Windows the data dir kept its inherited ACLs, so other local users could read
the secrets (and the README's "0600" claim was false there). This verifies
lock_down_dir() restricts a directory to its owner. Windows: the account running it
gets its own entry (also on a folder an elevated administrator made private to itself,
which Windows Server has Administrators own), but a service's data directory as
--install-service leaves it, and what inherits from it, are left as they are.

Only a folder that is ser2net's own is locked: one it just made, or one holding only what
it puts there. ConfigStore used to lock whatever folder held the config file, so
`sudo ser2net --config /etc/ser2net.json` made /etc 0700 (and on Windows a shared tree lost
its inherited entries). Such a folder now keeps its permissions; the config file is made
private (also after every save), and the start-up log warns when other accounts can use the
folder. On POSIX the server creates its files owner-only (umask 077), so its logs are
private there too. Given both --data-dir and --config elsewhere, the state is kept next to
the config file (as always), and ser2net says so instead of following --data-dir silently.

Run: python3 tests/test_dir_permissions.py
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import free_port, server_log, show_log_on_failure, wait_port

from app import accounts, datalock, winservice
from app.config import (
    OWN_NAMES,
    AppConfig,
    ConfigStore,
    foreign_entries,
    lock_down_dir,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def touch(folder: str, *names: str) -> None:
    for name in names:
        path = os.path.join(folder, name)
        if name.endswith("/"):
            os.makedirs(path, exist_ok=True)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}" if name.endswith(".json") else "x")


def test_posix_mode_0700():
    with tempfile.TemporaryDirectory() as d:
        lock_down_dir(d)
        mode = stat.S_IMODE(os.stat(d).st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"
    print("POSIX: state dir locked to 0700  OK")


def icacls(*args: str) -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    p = subprocess.run([os.path.join(root, "System32", "icacls.exe"), *args],
                       capture_output=True, text=True, timeout=60)
    return p.stdout + p.stderr


def has_entry(path: str, sid: str) -> bool:
    """An entry that allows `sid` something on `path` itself."""
    from app import winservice

    sec = winservice.read_security(path)
    return sec is not None and any(
        s == sid and allow and not flags & winservice.INHERIT_ONLY_ACE
        for s, _mask, allow, flags in sec[1])


def mkdir_with_sddl(path: str, sddl: str) -> None:
    """Create the folder `path` with the security descriptor `sddl` (Windows)."""
    import ctypes

    from app import winservice

    s = winservice._secapi()
    psd = ctypes.c_void_p()
    assert s.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(psd), None), ctypes.get_last_error()
    try:
        sa = s.SECURITY_ATTRIBUTES(ctypes.sizeof(s.SECURITY_ATTRIBUTES), psd, False)
        assert s.kernel.CreateDirectoryW(path, ctypes.byref(sa)), ctypes.get_last_error()
    finally:
        s.kernel.LocalFree(psd)


def test_windows_owner_only_acl():
    from app import winservice

    me = winservice._current_user_sid()
    with tempfile.TemporaryDirectory() as d:
        lock_down_dir(d)
        out = icacls(d)
        # broad principals must be gone after /inheritance:r + explicit grants
        for broad in ("Everyone", "Authenticated Users", "\\Users:"):
            assert broad not in out, f"broad ACL principal still present: {broad!r}\n{out}"
        assert me and has_entry(d, me), f"current user not granted access\n{out}"
    print("Windows: state dir restricted to owner/SYSTEM/Administrators  OK")

    # icacls must be run by its full System32 path: an unqualified name is searched
    # in the program's own folder and the current directory BEFORE System32, so an
    # icacls.exe planted next to ser2net.exe (e.g. in Downloads) would run instead
    calls, real_run = [], subprocess.run
    subprocess.run = lambda cmd, *a, **kw: calls.append(cmd) or real_run(cmd, *a, **kw)
    try:
        with tempfile.TemporaryDirectory() as d:
            lock_down_dir(d)
    finally:
        subprocess.run = real_run
    exe = calls[0][0] if calls else ""
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32").lower()
    assert os.path.isabs(exe) and os.path.dirname(exe).lower() == system32, \
        f"icacls not run by its full System32 path: {exe!r}"
    print("Windows: icacls run by its full System32 path (no binary planting)  OK")


def test_windows_admin_owned_folders():
    """A service's data directory as --install-service leaves it (SYSTEM and
    Administrators only, owned by Administrators) and a folder created in it keep their
    permissions; a folder an elevated administrator made private to itself (Python's
    mkdir(0o700) on Windows Server: SYSTEM, Administrators, OWNER RIGHTS, owned by
    Administrators) gets that account's entry, so it can still use it unelevated."""
    from app import winservice as w

    me = w._current_user_sid()
    base = tempfile.mkdtemp(prefix="s2n_perm_")
    try:
        service = os.path.join(base, "service")
        w._mkdir_admin_only(service)
        w._harden(service, users_read=False)  # as install() leaves the data directory
        sec = w.read_security(service)
        if not me or sec is None or sec[0] != w.ADMINS_SID:
            print("SKIP admin-owned folders: making Administrators the owner needs an "
                  "elevated prompt")
            return
        lock_down_dir(service)
        assert not has_entry(service, me), icacls(service)
        backups = os.path.join(service, "backups")  # inherits it (the service makes it)
        os.makedirs(backups)
        icacls(backups, "/setowner", f"*{w.ADMINS_SID}")
        lock_down_dir(backups)
        assert not has_entry(backups, me), icacls(backups)
        sec = w.read_security(service)
        assert sec is not None and w.service_folder(*sec), icacls(service)

        # created with the security descriptor Python 3.12.4+ gives mkdir(mode=0o700), on
        # every Python here (icacls rewriting a folder's ACL into that shape left other
        # entries in it on the older Pythons' runners)
        private = os.path.join(base, "private")
        mkdir_with_sddl(private, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)")
        icacls(private, "/setowner", f"*{w.ADMINS_SID}")
        sec = w.read_security(private)
        assert sec is not None and w.admin_only(*sec) and not has_entry(private, me), \
            icacls(private)
        lock_down_dir(private)
        assert has_entry(private, me), f"current user not granted access\n{icacls(private)}"
    finally:
        icacls(base, "/reset", "/T", "/C", "/Q")
        shutil.rmtree(base, ignore_errors=True)
    print("Windows: a service's data directory (and a folder inside) kept as it is; a "
          "folder an elevated administrator made private gets that account's entry  OK")


def test_own_names():
    """The names ser2net puts in its data directory are the ones the code uses."""
    from app import state, watchdog
    from app.engine import sparkplug_node

    for name in (datalock.LOCK_NAME, accounts.REQUEST_DIR, state.SETUP_CODE_FILE,
                 sparkplug_node.BDSEQ_FILE, watchdog.HANG_LOG, winservice.SERVICE_LOG,
                 "backups", "logs", "traces", "tls"):
        assert name in OWN_NAMES, name
    with tempfile.TemporaryDirectory() as d:
        # with the older copies and temporary files they leave (a log trim's <log>.tmp)
        touch(d, "config.json", "all.log", "all.log.1", "all.log.5", "audit.log",
              "audit.log.1", "all.log.tmp", "audit.log.tmp", ".cfg.x1y2.tmp",
              "setup-code.txt.tmp", watchdog.HANG_LOG + ".1", winservice.SERVICE_LOG + ".1",
              sparkplug_node.BDSEQ_FILE + ".tmp", *sorted(OWN_NAMES))
        assert foreign_entries(d) == [], foreign_entries(d)
        touch(d, "all.log.bak", "config.json.old", "passwd", "my.json")
        assert foreign_entries(d) == ["all.log.bak", "config.json.old", "my.json", "passwd"]
        assert foreign_entries(d, "my.json") == ["all.log.bak", "config.json", "config.json.old",
                                                 "passwd"]
    assert foreign_entries(os.path.join(ROOT, "no-such-folder")) is None
    print("ser2net's own names: the lock, admin-requests/, setup-code.txt, the bdSeq file, "
          "hang.log, service.log, rotated logs and temporary files  OK")


def test_posix_own_folder_locked():
    base = tempfile.mkdtemp(prefix="s2n_perm_")
    try:
        new = os.path.join(base, "new", "data")  # (the parents are made too)
        store = ConfigStore(os.path.join(new, "config.json"))
        assert mode(new) == 0o700 and store.own_dir and store.notes == []
        for name, contents in (("empty", ()),
                               ("own", ("config.json", "all.log", "all.log.2", "audit.log.1",
                                        "backups/", "logs/", "traces/", "tls/",
                                        "admin-requests/", "ser2net.lock", "setup-code.txt",
                                        ".cfg.abc.tmp", "lost+found/")),
                               ("renamed", ("site-a.json", "all.log", "backups/"))):
            d = os.path.join(base, name)
            os.makedirs(d, mode=0o755)
            os.chmod(d, 0o755)
            touch(d, *contents)
            config = "site-a.json" if name == "renamed" else "config.json"
            store = ConfigStore(os.path.join(d, config))
            assert mode(d) == 0o700, f"{name}: {oct(mode(d))}"
            assert store.own_dir and store.load().load_notes == [], name
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("POSIX: a new folder and one with only ser2net's files are locked to 0700  OK")


def test_posix_shared_folder_left_alone():
    base = tempfile.mkdtemp(prefix="s2n_perm_")
    try:
        etc = os.path.join(base, "etc")
        os.makedirs(etc)
        os.chmod(etc, 0o755)
        touch(etc, "passwd", "hosts", "ser2net.json")
        cfg = os.path.join(etc, "ser2net.json")
        os.chmod(cfg, 0o644)
        store = ConfigStore(cfg)
        assert mode(etc) == 0o755, f"a folder that isn't ser2net's was changed: {oct(mode(etc))}"
        assert mode(cfg) == 0o600, oct(mode(cfg))
        assert not store.own_dir
        notes = store.load().load_notes
        assert len(notes) == 1 and notes[0].startswith(f"WARNING: {etc}, the folder of "
                                                       "ser2net.json, holds files"), notes
        assert "(hosts, passwd)" in notes[0] and "--data-dir" in notes[0], notes[0]
        config = AppConfig()
        store.save(config, validate=False)
        store.save(AppConfig(), validate=False)  # (a change: the first one is backed up)
        assert mode(etc) == 0o755 and mode(cfg) == 0o600, (oct(mode(etc)), oct(mode(cfg)))
        assert mode(store.backup_dir) == 0o700, oct(mode(store.backup_dir))
        assert sorted(os.listdir(etc)) == ["backups", "hosts", "passwd", "ser2net.json"]

        # private already: nothing to warn about; no config file yet: nothing to chmod
        os.chmod(etc, 0o700)
        assert ConfigStore(cfg).load().load_notes == []
        home = os.path.join(base, "home")
        os.makedirs(home)
        os.chmod(home, 0o751)
        touch(home, ".profile")
        store = ConfigStore(os.path.join(home, "config.json"))
        assert mode(home) == 0o751 and len(store.notes) == 1, (oct(mode(home)), store.notes)
        store.save(AppConfig(), validate=False)
        assert mode(os.path.join(home, "config.json")) == 0o600
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("POSIX: a folder with other files keeps its mode; the config file is 0600, the "
          "start-up log warns when others can use the folder  OK")


def _start(args: list[str], folder: str, port: int):
    log = server_log(folder)
    try:
        return subprocess.Popen(
            [sys.executable, "ser2net.py", "--no-bootstrap", *args], cwd=ROOT,
            env=dict(os.environ, SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(port)),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def test_posix_server_files_private():
    """A server started on a folder that isn't its own: the folder keeps its mode, what the
    server writes there is owner-only (umask 077), and all.log has the warning."""
    base = tempfile.mkdtemp(prefix="s2n_perm_")
    ok = False
    try:
        shared = os.path.join(base, "shared")
        os.makedirs(shared)
        os.chmod(shared, 0o755)
        touch(shared, "README.txt")
        port = free_port()
        proc = _start(["--config", os.path.join(shared, "ser2net.json")], base, port)
        try:
            assert wait_port(port), "the server did not start"
            log = os.path.join(shared, "all.log")
            deadline = time.monotonic() + 10
            while not os.path.isfile(log) and time.monotonic() < deadline:
                time.sleep(0.1)
        finally:
            _stop(proc)
        assert mode(shared) == 0o755, oct(mode(shared))
        for name in ("all.log", "ser2net.json"):
            assert mode(os.path.join(shared, name)) == 0o600, (name, oct(mode(os.path.join(shared, name))))
        for name in ("logs", "traces"):
            assert mode(os.path.join(shared, name)) == 0o700, name
        with open(os.path.join(shared, "all.log"), encoding="utf-8") as fh:
            text = fh.read()
        assert f"WARNING: {shared}, the folder of ser2net.json, holds files ser2net didn't " \
               "put there (README.txt)" in text, text[-2000:]
        with open(os.path.join(shared, "ser2net.json"), encoding="utf-8") as fh:
            assert json.load(fh)["admin_ui"]["port"] == port
        ok = True
    finally:
        if not ok:
            show_log_on_failure(base)
        shutil.rmtree(base, ignore_errors=True)
    print("POSIX: a server on a shared folder leaves it 0755, writes 0600 files, logs the "
          "warning  OK")


def test_split_layout_named():
    """--data-dir and a --config elsewhere: the state stays next to the config file (as
    always), and ser2net says so; the Windows service texts name that folder."""
    import ser2net as cli

    base = tempfile.mkdtemp(prefix="s2n_perm_")
    try:
        a, b = os.path.join(base, "a"), os.path.join(base, "b")
        cfg = os.path.join(b, "config.json")
        assert cli.split_layout_note(cli.parse_args(["--data-dir", a])) == ""
        assert cli.split_layout_note(cli.parse_args(["--config", cfg])) == ""
        assert cli.split_layout_note(cli.parse_args(["--data-dir", b, "--config", cfg])) == ""
        assert cli.split_layout_note(cli.parse_args(
            ["--data-dir", b + os.sep, "--config", cfg])) == ""
        note = cli.split_layout_note(cli.parse_args(["--data-dir", a, "--config", cfg]))
        assert note == (f"WARNING: --config {cfg} is not in --data-dir {a}: ser2net keeps the "
                        f"backups, logs and traces next to the config file, in {b}"), note
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--about",
                            "--data-dir", a, "--config", cfg], cwd=ROOT, capture_output=True,
                           text=True, timeout=120)
        assert p.returncode == 0 and f"ser2net: {note}" in p.stderr, (p.returncode, p.stderr)
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--about",
                            "--data-dir", b, "--config", cfg], cwd=ROOT, capture_output=True,
                           text=True, timeout=120)
        assert "WARNING" not in p.stderr, p.stderr
        assert winservice.state_dir(a, cfg) == b and winservice.state_dir(a, None) == a
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("--data-dir + a --config elsewhere: ser2net names the folder it uses  OK")


def test_windows_shared_folder_left_alone():
    """A folder other accounts may use (here: Users may read) and that holds other files
    keeps its entries; the config file loses the inherited ones (also after a save); a
    folder of ser2net's own is locked as before."""
    users = winservice.USERS_SID
    base = tempfile.mkdtemp(prefix="s2n_perm_")
    try:
        # what is made in it inherits a read entry for Users (as under C:\ProgramData)
        icacls(base, "/grant", f"*{users}:(OI)(CI)RX")
        shared = os.path.join(base, "shared")
        os.makedirs(shared)
        touch(shared, "notes.txt", "config.json")
        assert has_entry(os.path.join(shared, "config.json"), users)
        store = ConfigStore(os.path.join(shared, "config.json"))
        assert not store.own_dir and has_entry(shared, users), icacls(shared)
        cfg = os.path.join(shared, "config.json")
        assert not has_entry(cfg, users), icacls(cfg)
        notes = store.load().load_notes
        assert len(notes) == 1 and "(notes.txt)" in notes[0], notes
        store.save(AppConfig(), validate=False)
        assert not has_entry(cfg, users), f"after a save\n{icacls(cfg)}"
        assert has_entry(cfg, winservice._current_user_sid()), icacls(cfg)

        own = os.path.join(base, "own")
        os.makedirs(own)
        touch(own, "all.log", "backups/")
        assert has_entry(own, users), icacls(own)
        store = ConfigStore(os.path.join(own, "config.json"))
        assert store.own_dir and not has_entry(own, users), icacls(own)
    finally:
        icacls(base, "/reset", "/T", "/C", "/Q")
        shutil.rmtree(base, ignore_errors=True)
    print("Windows: a shared folder keeps its entries, the config file is made private; "
          "ser2net's own folder is locked  OK")


def main():
    test_own_names()
    test_split_layout_named()
    if os.name == "posix":
        test_posix_mode_0700()
        test_posix_own_folder_locked()
        test_posix_shared_folder_left_alone()
        test_posix_server_files_private()
    elif os.name == "nt":
        test_windows_owner_only_acl()
        test_windows_admin_owned_folders()
        test_windows_shared_folder_left_alone()
    else:
        print(f"skipped: unsupported os.name={os.name!r}")
        return
    print("\nPASS: state directory permissions (H5)")


if __name__ == "__main__":
    main()
