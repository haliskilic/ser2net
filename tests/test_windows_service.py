"""Windows service mode (--install-service / --service / --uninstall-service).

Portable part (any OS):
  * the command line the Service Control Manager runs is quoted correctly (spaces,
    quotes, trailing backslashes) for a frozen executable and a source checkout;
  * service names are validated; on Linux the options refuse with a hint (no files);
  * runtime.request_shutdown() — what the service's stop request calls, from another
    thread — stops a running server cleanly.
Windows part (SER2NET_SERVICE_TESTS=1, as Administrator; CI runs it): install a
service on a throwaway data directory, wait until the UI answers, set the admin's
password with --set-password while it runs (the service applies it), kill the process
and see the SCM restart it, stop it (clean shutdown in the log), remove it. Uses
SER2NET_STANDALONE_BIN when set (the frozen exe), else python + ser2net.py.

Run: python3 tests/test_windows_service.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from types import SimpleNamespace

from _server import ROOT, Browser, free_port, wait_port

from app import runtime, winservice
from app.web.auth import hash_password

WINDOWS = os.name == "nt"


def test_command_line():
    cmd = winservice.service_command("ser2net", r"C:\Program Files\ser2net\data", frozen=True,
                                     executable=r"C:\Program Files\ser2net\ser2net.exe")
    assert cmd == ('"C:\\Program Files\\ser2net\\ser2net.exe" --service --service-name ser2net '
                   '--data-dir "C:\\Program Files\\ser2net\\data"'), cmd
    cmd = winservice.service_command("plant-2", "D:\\s2n\\", config="D:\\cfg dir\\c.json",
                                     frozen=False, executable="C:\\Python312\\python.exe",
                                     script="C:\\src\\ser2net.py")
    assert cmd == ('C:\\Python312\\python.exe C:\\src\\ser2net.py --service --service-name plant-2 '
                   '--data-dir D:\\s2n\\ --config "D:\\cfg dir\\c.json"'), cmd
    assert winservice._quote('a "b"\\') == '"a \\"b\\"\\\\"'
    for bad in ("", "a b", "x" * 65, "a/b", "ser2net;calc"):
        try:
            winservice.check_name(bad)
        except winservice.ServiceError:
            pass
        else:
            raise AssertionError(f"service name {bad!r} accepted")
    assert winservice.display_name("ser2net") == "ser2net"
    assert winservice.display_name("plant-2") == "ser2net (plant-2)"
    print("SCM command line quoted for exe / source; names validated  OK")


def test_acl_decision_table():
    """acl_offenders() is the pure core of the exe/folder permission check (the Windows
    caller reads the ACEs through the API). Runs on any OS."""
    w = winservice
    everyone, auth_users = "S-1-1-0", "S-1-5-11"
    user = "S-1-5-21-1-2-3-1001"
    system, admins = "S-1-5-18", "S-1-5-32-544"
    trusted_installer = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    modify = w.FILE_WRITE_DATA | w.FILE_APPEND_DATA | w._DELETE
    read = 0x00020000 | 0x1  # READ_CONTROL | FILE_READ_DATA

    # a Program Files-style ACL: only SYSTEM / Administrators / TrustedInstaller may write
    assert w.acl_offenders([(system, w.GENERIC_ALL, True), (admins, w.GENERIC_ALL, True),
                            (trusted_installer, w.GENERIC_ALL, True), (auth_users, read, True)]) == []
    # a C:\ser2net-style ACL: Authenticated Users inherit Modify -> escalation path
    assert w.acl_offenders([(system, w.GENERIC_ALL, True), (admins, w.GENERIC_ALL, True),
                            (auth_users, modify, True)]) == [auth_users]
    # any specific non-admin user with write, or just the right to take ownership, counts
    assert w.acl_offenders([(user, w.GENERIC_WRITE, True)]) == [user]
    assert w.acl_offenders([(user, w.WRITE_OWNER, True)]) == [user]
    assert w.acl_offenders([(everyone, modify, True)]) == [everyone]
    # a deny ACE for the same SID cancels its allowed write
    assert w.acl_offenders([(auth_users, modify, True), (auth_users, modify, False)]) == []
    # read-only access is fine; several offenders come back sorted
    assert w.acl_offenders([(auth_users, read, True)]) == []
    assert w.acl_offenders([(user, w.GENERIC_WRITE, True),
                            (everyone, modify, True)]) == sorted([user, everyone])
    # off Windows the ACL reader can't run and never blocks an install
    assert w.writable_by_non_admins(__file__) is None
    assert w.location_warning() is None
    print("ACL decision table: non-admin write on the exe/folder is flagged, deny honoured  OK")

    # real DACLs as the reader hands them over (sid, mask, allow, ace flags), and owners
    oi_ci, io, inh = 0x01 | 0x02, w.INHERIT_ONLY_ACE, w.INHERITED_ACE
    full, rx, gr_ge = 0x1F01FF, 0x1200A9, 0x80000000 | 0x20000000
    creator_owner, packages = "S-1-3-0", "S-1-15-2-1"
    users = "S-1-5-32-545"
    # an untouched folder under C:\Program Files (icacls: TrustedInstaller:(I)(F) ...
    # CREATOR OWNER:(I)(OI)(CI)(IO)(F)): the inherit-only entries grant nothing on it
    program_files_dir = [
        (trusted_installer, full, True, inh), (trusted_installer, w.GENERIC_ALL, True, inh | 0x02 | io),
        (system, full, True, inh), (system, w.GENERIC_ALL, True, inh | oi_ci | io),
        (admins, full, True, inh), (admins, w.GENERIC_ALL, True, inh | oi_ci | io),
        (users, rx, True, inh), (users, gr_ge, True, inh | oi_ci | io),
        (creator_owner, w.GENERIC_ALL, True, inh | oi_ci | io),
        (packages, rx, True, inh), (packages, gr_ge, True, inh | oi_ci | io)]
    assert w.acl_offenders(program_files_dir, owner=trusted_installer) == []
    # ... but a folder an administrator created there on Windows 10/11 is owned by that
    # account, and CREATOR OWNER turned into a full-control entry of its own
    created = program_files_dir[:-3] + [(user, full, True, inh)]
    assert w.acl_offenders(created, owner=user) == [user]
    # what install() leaves on %ProgramFiles%\ser2net (and the exe inherits): no entry for
    # the installing account, owned by Administrators
    hardened = [(system, full, True, oi_ci), (admins, full, True, oi_ci), (users, rx, True, oi_ci)]
    assert w.acl_offenders(hardened, owner=admins) == []
    assert w.acl_offenders([(s, m, a, inh) for s, m, a, _f in hardened], owner=admins) == []
    # the per-user grant lock_down_dir() adds would keep that account able to replace the
    # exe from an unelevated program — so it must not be used on the install folder
    assert w.acl_offenders(hardened + [(user, full, True, oi_ci)], owner=admins) == [user]
    # a placeholder is never an account, even where it isn't inherit-only
    assert w.acl_offenders([(creator_owner, full, True, 0)], owner=system) == []
    # LocalService / NetworkService are not trusted to change what SYSTEM runs
    assert w.acl_offenders([("S-1-5-19", full, True, 0)], owner=admins) == ["S-1-5-19"]
    # folders above the data directory: only what lets someone rename it counts
    c_root = [(auth_users, 0x4, True, 0),                      # (AD): may add folders
              (auth_users, 0x1301BF, True, oi_ci | io), (admins, full, True, oi_ci)]
    assert w.acl_offenders(c_root, owner=trusted_installer, rights=w.RENAME_RIGHTS) == []
    c_ser2net = [(auth_users, 0x1301BF, True, inh | oi_ci), (admins, full, True, inh | oi_ci)]
    assert w.acl_offenders(c_ser2net, owner=admins, rights=w.RENAME_RIGHTS) == [auth_users]
    # whose leftovers a new service won't take over
    assert not w.foreign_owner(admins, user) and not w.foreign_owner(system, user)
    assert not w.foreign_owner(user, user) and w.foreign_owner("S-1-5-21-1-2-3-1002", user)
    print("ACL decision table: inherit-only/CREATOR OWNER skipped, owners and the installer's "
          "own grant counted, Program Files passes, ancestors judged on rename rights  OK")


def test_registered_command_read_back():
    """An existing service's --data-dir / --config are read back from its command line
    (CommandLineToArgvW rules), for the 2.8.0 and the current layout alike."""
    w = winservice
    cases = [
        [r"C:\ser2net\ser2net.exe", "--service", "--service-name", "ser2net", "--data-dir",
         r"C:\ser2net\data"],                                                  # 2.8.0, frozen
        [r"C:\Program Files\ser2net\plant-2.exe", "--service", "--service-name", "plant-2",
         "--data-dir", r"C:\ProgramData\ser2net\plant 2" "\\", "--config", r'D:\cfg "x"\c.json'],
        [r"C:\Python312\python.exe", r"C:\src dir\ser2net.py", "--service", "--service-name",
         "s", "--data-dir", "D:\\s2n\\"],                                       # source
    ]
    for parts in cases:
        cmd = " ".join(w._quote(p) for p in parts)
        assert w.split_command_line(cmd) == parts, (cmd, w.split_command_line(cmd))
    got = w.parse_service_command(" ".join(w._quote(p) for p in cases[1]))
    assert got == {"executable": cases[1][0], "script": None, "service_name": "plant-2",
                   "data_dir": cases[1][5], "config": cases[1][7]}, got
    got = w.parse_service_command(" ".join(w._quote(p) for p in cases[2]))
    assert got["script"] == r"C:\src dir\ser2net.py" and got["data_dir"] == "D:\\s2n\\", got
    assert w.parse_service_command(r'"C:\Windows\System32\svchost.exe" -k netsvcs') is None
    assert w.split_command_line("  ") == []
    print("registered command line read back: data dir / config / exe, any quoting  OK")


def test_upgrade_paths():
    """choose_install_paths(): an upgrade keeps the service's data directory; a new
    service gets the default; moving to an empty directory is refused."""
    w = winservice
    base = os.path.join(tempfile.gettempdir(), "s2n-choose")
    old = os.path.join(base, "ser2net", "data")             # a 2.8.0 service: data next to the exe
    new = os.path.join(base, "ProgramData", "ser2net")
    other = os.path.join(base, "elsewhere")
    reg = {"executable": os.path.join(base, "ser2net", "ser2net.exe"), "script": None,
           "service_name": "ser2net", "data_dir": old, "config": None}
    files = {os.path.join(old, "config.json")}
    exists = files.__contains__
    # the documented upgrade (no --data-dir): the service's own data directory is kept
    d, c, notes = w.choose_install_paths(None, None, reg, new, exists)
    assert (d, c) == (old, None) and "keeping" in notes[0], (d, c, notes)
    # a registered --config is kept too
    d, c, _n = w.choose_install_paths(None, None, dict(reg, config=os.path.join(base, "c.json")),
                                      new, exists)
    assert c == os.path.join(base, "c.json")
    # passing the same directory explicitly (the old README command) changes nothing
    assert w.choose_install_paths(old, None, reg, new, exists)[:2] == (old, None)
    # moving it to a directory without a configuration would start over empty: refused
    try:
        w.choose_install_paths(other, None, reg, new, exists)
    except w.ServiceError as e:
        assert "Copy" in str(e) and old in str(e) and other in str(e), str(e)
    else:
        raise AssertionError("moving the service to an empty data directory was accepted")
    # ... allowed once the folder was copied there
    files.add(os.path.join(other, "config.json"))
    d, _c, notes = w.choose_install_paths(other, None, reg, new, exists)
    assert d == other and "moving" in notes[0]
    # a new service: the default, or what was passed
    assert w.choose_install_paths(None, None, None, new, exists) == (new, None, [])
    assert w.choose_install_paths(other, None, None, new, exists)[0] == other
    # a registration that names no data directory can't be upgraded blindly
    try:
        w.choose_install_paths(None, None, dict(reg, data_dir=None), new, exists)
    except w.ServiceError:
        pass
    else:
        raise AssertionError("a registration without --data-dir was guessed")
    print("upgrade keeps the service's data dir; new service -> default; empty move refused  OK")


def test_install_locations():
    """Where a frozen build installs itself and keeps a service's data (any OS)."""
    w = winservice
    old = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramData")}
    os.environ["ProgramFiles"], os.environ["ProgramData"] = r"C:\PF", r"C:\PD"
    try:
        assert w.installed_exe_path("ser2net") == os.path.join(r"C:\PF", "ser2net", "ser2net.exe")
        assert w.installed_exe_path("plant-2") == os.path.join(r"C:\PF", "ser2net", "plant-2.exe")
        assert w.default_service_data_dir("ser2net") == os.path.join(r"C:\PD", "ser2net")
        assert w.default_service_data_dir("plant-2") == os.path.join(r"C:\PD", "ser2net", "plant-2")
        # the SCM command points at the admin-only copy for a frozen install
        cmd = w.service_command("ser2net", r"C:\PD\ser2net", frozen=True,
                                executable=w.installed_exe_path("ser2net"))
        assert w.installed_exe_path("ser2net") in cmd and "--service" in cmd
        # the firewall hint is a program-scoped netsh rule named after the service
        fw = w.firewall_command("plant-2", exe=r"C:\PF\ser2net\plant-2.exe")
        assert fw.startswith("netsh advfirewall firewall add rule") and "ser2net (plant-2)" in fw
        assert r'program="C:\PF\ser2net\plant-2.exe"' in fw and "dir=in action=allow" in fw
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("frozen install path under %ProgramFiles%, data under %ProgramData%  OK")


class FakeSCM:
    """Just enough of advapi32 for install(): one service, its command line, and a start
    that can be made to fail once; every call is recorded in order."""

    def __init__(self, calls, existing_cmd=None, fail_start_once=False):
        self.calls, self.cmd, self.fail_start = calls, existing_cmd, fail_start_once
        self.running = existing_cmd is not None

    def OpenSCManagerW(self, *a):  # noqa: N802 (Win32 names)
        return 1

    def OpenServiceW(self, scm, name, access):  # noqa: N802
        return 2 if self.cmd is not None else 0

    def CreateServiceW(self, scm, name, display, access, kind, start, err, cmd, *rest):  # noqa: N802
        self.calls.append(("create", cmd))
        self.cmd = cmd
        return 3

    def ChangeServiceConfigW(self, svc, kind, start, err, cmd, *rest):  # noqa: N802
        self.calls.append(("change", cmd))
        self.cmd = cmd
        return 1

    def StartServiceW(self, svc, *a):  # noqa: N802
        self.calls.append(("start", self.cmd))
        self.running = not self.fail_start  # the new build exits right away
        self.fail_start = False
        return 1

    def CloseServiceHandle(self, h):  # noqa: N802
        return 1


def fake_windows(scm, previous_cmd, calls):
    """Patch winservice so install() runs against FakeSCM; returns an undo function. The
    Windows-only steps (ACLs, the lock test, stopping) only record that they ran."""
    w = winservice
    saved = {n: getattr(w, n) for n in (
        "_advapi", "_win", "_query_registration", "_stop_and_wait", "_wait_lock_free",
        "_mkdir_admin_only", "_harden", "_require_protected", "_check_adoptable",
        "_exposed_ancestors", "_service_extras", "_wait_state", "_unsafe_locations")}

    def stop(api, svc, name, log):
        calls.append(("stop",))
        was, scm.running = scm.running, False
        return was

    w._advapi = lambda: SimpleNamespace(dll=scm, kernel=None)
    w._win = SimpleNamespace(get_last_error=lambda: w.ERROR_SERVICE_DOES_NOT_EXIST,
                             FormatError=lambda e: f"error {e}")
    w._query_registration = lambda api, svc: SimpleNamespace(
        service_type=0x10, start_type=2, error_control=1, binary_path=previous_cmd,
        display_name="ser2net")
    w._stop_and_wait = stop
    w._wait_lock_free = lambda d, timeout: calls.append(("lock-free", d))
    w._mkdir_admin_only = lambda d: os.makedirs(d, exist_ok=True)
    w._harden = lambda d, users_read: calls.append(("harden", d))
    w._require_protected = lambda paths, what: None
    w._check_adoptable = lambda d, config_path: calls.append(("adoptable", d))
    w._exposed_ancestors = lambda d: []
    w._service_extras = lambda *a: None
    w._wait_state = lambda api, svc, want, timeout=30.0: (
        w.SERVICE_RUNNING if scm.running else w.SERVICE_STOPPED)
    w._unsafe_locations = lambda paths, log: []

    def undo():
        for n, v in saved.items():
            setattr(w, n, v)
    return undo


def test_install_flow_with_a_fake_scm():
    """install() against a stand-in Service Control Manager (any OS): re-running it while
    the service runs stops the service before the data directory's lock is tested and the
    configuration loaded; a 2.8.0 service keeps its data directory; a failure after the
    stop puts the previous exe and command line back, starts it and is one line."""
    w = winservice
    work = tempfile.mkdtemp(prefix="s2n_flow_")
    env_saved = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramData")}
    exe_saved = sys.executable
    os.environ["ProgramFiles"] = os.path.join(work, "Program Files")
    os.environ["ProgramData"] = os.path.join(work, "ProgramData")
    try:
        new_build = os.path.join(work, "Downloads", "ser2net-windows-x64.exe")
        os.makedirs(os.path.dirname(new_build))
        with open(new_build, "w") as fh:
            fh.write("NEW")
        sys.executable = new_build
        old_dir = os.path.join(work, "ser2net")                 # where 2.8.0 ran from
        old_data = os.path.join(old_dir, "data")
        os.makedirs(old_data)
        with open(os.path.join(old_data, "config.json"), "w") as fh:
            json.dump({"users": [{"username": "admin"}], "mappings": [{"name": "PLC"}]}, fh)
        old_cmd = w.service_command("ser2net", old_data, frozen=True,
                                    executable=os.path.join(old_dir, "ser2net.exe"))
        dest = w.installed_exe_path("ser2net")

        def run(scm, registered_cmd=old_cmd, data_dir=None, prepare=None, lines=None):
            calls = []
            scm.calls = calls
            undo = fake_windows(scm, registered_cmd, calls)
            seen = []

            def default_prepare(path):
                calls.append(("prepare", path))
                seen.append(path)
                return SimpleNamespace(path=path)
            try:
                result = w.install("ser2net", data_dir, None, prepare=prepare or default_prepare,
                                   default_data_dir=os.path.join(work, "default-data"),
                                   frozen=True, log=(lines.append if lines is not None
                                                     else (lambda *_: None)))
            finally:
                undo()
            return calls, result

        # 1. upgrading the running 2.8.0 service the documented way (no --data-dir)
        scm = FakeSCM([], existing_cmd=old_cmd)
        lines = []
        calls, result = run(scm, lines=lines)
        kinds = [c[0] for c in calls]
        assert kinds[:3] == ["stop", "lock-free", "prepare"], kinds  # stopped before the lock
        assert calls[2][1] == os.path.join(old_data, "config.json"), calls  # its own config
        change = next(c for c in calls if c[0] == "change")[1]
        assert w.split_command_line(change)[0] == dest and old_data in change, change
        assert kinds[-1] == "start" and scm.running
        with open(dest) as fh:
            assert fh.read() == "NEW"
        assert any("keeping the service's data directory" in ln for ln in lines), lines
        assert any("no longer runs" in ln for ln in lines), lines
        assert not os.path.exists(dest + ".old"), "the previous copy is dropped once it worked"
        print("re-run while running: stop -> lock test -> load its config -> re-point -> start; "
              "2.8.0 data dir kept  OK")

        # 2. the new build doesn't start: the previous exe and command line come back
        with open(dest, "w") as fh:
            fh.write("PREVIOUS")
        installed_cmd = w.service_command("ser2net", old_data, frozen=True, executable=dest)
        scm = FakeSCM([], existing_cmd=installed_cmd, fail_start_once=True)
        try:
            run(scm, registered_cmd=installed_cmd)
        except w.ServiceError as e:
            msg = str(e)
            assert "\n" not in msg and "did not reach RUNNING" in msg, msg
            assert "previous version is running again" in msg, msg
        else:
            raise AssertionError("a start failure was not reported")
        changes = [c[1] for c in scm.calls if c[0] == "change"]
        assert len(changes) == 2 and changes[1] == installed_cmd, changes  # command restored
        with open(dest) as fh:
            assert fh.read() == "PREVIOUS", "the previous exe was not put back"
        assert scm.running and scm.calls[-1] == ("start", installed_cmd)
        print("new build fails to start: previous exe + command line restored and running, "
              "one-line error  OK")

        # 3. copying the new exe fails (not just 'in use'): one line, the old one restarted
        import shutil as shutil_module
        real_copy = shutil_module.copyfile

        def full_disk(src, dst, *a, **k):
            raise OSError(28, "No space left on device")
        shutil_module.copyfile = full_disk
        try:
            scm = FakeSCM([], existing_cmd=old_cmd)
            run(scm)
        except w.ServiceError as e:
            assert "No space left on device" in str(e) and "running again" in str(e), str(e)
        else:
            raise AssertionError("a failed copy was not reported")
        finally:
            shutil_module.copyfile = real_copy
        assert not any(c[0] == "change" for c in scm.calls) and scm.running
        print("copying the new exe fails: one line, the previous version restarted  OK")

        # 4. loading the configuration stops the install: the service is started again
        def broken(path):
            raise SystemExit("ser2net: config.json is not a valid configuration")
        scm = FakeSCM([], existing_cmd=old_cmd)
        try:
            run(scm, prepare=broken)
        except SystemExit as e:
            assert "not a valid configuration" in str(e.code)
        else:
            raise AssertionError("SystemExit was swallowed")
        assert scm.running and [c[0] for c in scm.calls][-1] == "start"
        print("a config that can't be loaded: the running version is started again  OK")

        # 5. a new service from the exe: %ProgramData%\ser2net, checked before it is adopted
        scm = FakeSCM([])
        calls, _result = run(scm)
        new_default = w.default_service_data_dir("ser2net")
        assert ("adoptable", new_default) in calls and ("lock-free", new_default) in calls, calls
        assert calls.index(("adoptable", new_default)) < calls.index(
            next(c for c in calls if c[0] == "prepare"))
        created = next(c for c in calls if c[0] == "create")[1]
        assert new_default in created and "stop" not in [c[0] for c in calls], calls
        print("new service: default data dir under %ProgramData%, checked before it is used  OK")
    finally:
        sys.executable = exe_saved
        for k, v in env_saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(work, ignore_errors=True)


def test_cli_leaves_the_choice_to_install():
    """ser2net.py no longer refuses before install() (which stops a running service
    first), and tells it whether --data-dir was given at all."""
    import ser2net as cli

    w = winservice
    saved = (w.require_windows, w.install)
    got = []
    w.require_windows = lambda: None
    w.install = lambda name, data_dir, config, **kw: got.append((data_dir, kw)) or None
    d = tempfile.mkdtemp(prefix="s2n_cli_")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; sys.path.insert(0, sys.argv[2]); "
         "from app import datalock; assert datalock.acquire(sys.argv[1]) is None; "
         "print('held', flush=True); time.sleep(60)", d, ROOT],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"  # "the running service"
        assert cli.main(["--install-service", "--no-bootstrap", "--data-dir", d]) == 0
        assert cli.main(["--install-service", "--no-bootstrap"]) == 0
    finally:
        holder.kill()
        holder.wait(10)
        w.require_windows, w.install = saved
        shutil.rmtree(d, ignore_errors=True)
    assert [g[0] for g in got] == [d, None], got  # None: keep what the service uses
    assert got[0][1]["default_data_dir"] and callable(got[0][1]["prepare"])
    print("CLI: install() runs while the data dir is locked; an omitted --data-dir stays None  OK")


def test_not_windows():
    if WINDOWS:
        return
    d = tempfile.mkdtemp(prefix="s2n_svc_")
    for flag in ("--install-service", "--uninstall-service"):
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir",
                            os.path.join(d, "data"), flag], cwd=ROOT, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert p.returncode == 1 and "only on Windows" in p.stderr, (flag, p.stdout, p.stderr)
    assert not os.path.exists(os.path.join(d, "data")), "nothing may be created on Linux"
    print("Linux: --install-service / --uninstall-service refuse with a systemd hint  OK")


def test_request_shutdown():
    """The service stop path: request_shutdown() from another thread ends runtime.main()."""
    d = tempfile.mkdtemp(prefix="s2n_stop_")
    port = free_port()
    cfg = os.path.join(d, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": port}, "users": []}, fh)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", runtime.main(cfg)), daemon=True)
    t.start()
    try:
        assert wait_port(port), "server did not start"
        t0 = time.time()
        runtime.request_shutdown()
        t.join(30)
        assert not t.is_alive(), "request_shutdown() did not stop the server"
        assert result.get("code") == 0, result
        with open(os.path.join(d, "all.log"), encoding="utf-8") as fh:
            assert "shutting down — stopping all mappings" in fh.read()
        print(f"request_shutdown() from another thread: clean stop in {time.time() - t0:.1f}s  OK")
    finally:
        runtime._shutdown_requested.clear()
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- Windows, real SCM
def sc(*args):
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=60)


def healthy(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.read() == b"ok"
    except OSError:
        return False


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False


def test_real_service():
    if not WINDOWS or os.environ.get("SER2NET_SERVICE_TESTS") != "1":
        print("SKIP real Windows service test (Windows + Administrator + SER2NET_SERVICE_TESTS=1)")
        return
    import psutil

    exe = os.environ.get("SER2NET_STANDALONE_BIN")
    base = [os.path.abspath(exe)] if exe else [sys.executable, os.path.join(ROOT, "ser2net.py")]
    name = "s2n-test-" + uuid.uuid4().hex[:6]
    work = tempfile.mkdtemp(prefix="s2n svc ")  # a space in the path, on purpose
    data = os.path.join(work, "data dir")
    os.makedirs(data)
    port = free_port()
    with open(os.path.join(data, "config.json"), "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": port},
                   "users": [{"username": "admin", "password_hash": hash_password("svc-pass-1"),
                              "role": "admin", "pwd_version": 1}]}, fh)
    # a CI runner's workspace is writable by non-admins, so a source-checkout install
    # needs the override; the frozen exe installs itself to an admin-only folder and
    # ignores it. Pass it either way so this test covers both legs.
    install_cmd = base + ["--install-service", "--service-name", name, "--data-dir", data,
                          "--allow-unsafe-location"]
    try:
        p = subprocess.run(install_cmd, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        print(p.stdout.strip())
        assert p.returncode == 0, p.stdout + p.stderr
        assert "is running" in p.stdout
        assert wait_for(lambda: healthy(port), 60), "the service's UI did not answer"
        q = sc("qc", name).stdout
        assert "AUTO_START" in q and "--service" in q and f'"{data}"' in q, q
        print(f"installed + running as service '{name}' (auto start), UI answers  OK")

        p = subprocess.run(base + ["--data-dir", data, "--set-password", "admin"],
                           input="svc-pass-2\n", capture_output=True, text=True, timeout=120)
        assert p.returncode == 0 and "done by the running ser2net" in p.stdout, p.stdout + p.stderr
        Browser(f"http://127.0.0.1:{port}").login("admin", "svc-pass-2")
        print("--set-password while the service runs: the service applies it at once  OK")

        # re-running --install-service while it runs upgrades it in place (stops it first,
        # keeps its name and — without --data-dir, as documented — the data directory it
        # has, read back from its registration) instead of failing with 'already exists'
        rerun = [a for a in install_cmd if a not in ("--data-dir", data)]
        p = subprocess.run(rerun, capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "upgraded in place" in p.stdout, p.stdout + p.stderr
        assert "keeping the service's data directory" in p.stdout, p.stdout
        assert wait_for(lambda: healthy(port), 60), "the service did not come back after the upgrade"
        q = sc("qc", name).stdout
        assert f'"{data}"' in q, q
        with open(os.path.join(data, "config.json"), encoding="utf-8") as fh:
            assert [u["username"] for u in json.load(fh)["users"]] == ["admin"], "config lost"
        print("re-run --install-service while running: upgraded in place, same data dir  OK")
        if exe:  # the copy in %ProgramFiles%\ser2net may be changed by administrators only
            with open(os.path.join(data, "all.log"), encoding="utf-8") as fh:
                log = fh.read()
            assert "accounts other than SYSTEM and Administrators can change" not in log, log[-2000:]
            print("installed exe: no start-up warning about who can change it  OK")

        def service_procs():
            return [pr for pr in psutil.process_iter(["cmdline"])
                    if name in " ".join(pr.info["cmdline"] or [])]
        for pr in service_procs():
            pr.kill()
        assert wait_for(lambda: not healthy(port), 20)
        assert wait_for(lambda: healthy(port), 60), "the SCM did not restart the service"
        print("killed: the SCM restarted it (failure actions)  OK")

        r = sc("stop", name)
        assert r.returncode == 0, r.stdout + r.stderr
        assert wait_for(lambda: "STOPPED" in sc("query", name).stdout, 60), sc("query", name).stdout
        assert not healthy(port)
        with open(os.path.join(data, "all.log"), encoding="utf-8") as fh:
            assert "shutting down — stopping all mappings" in fh.read(), "no clean shutdown"
        print("sc stop: clean shutdown (mappings stopped, logged)  OK")
    finally:
        p = subprocess.run(base + ["--uninstall-service", "--service-name", name],
                           capture_output=True, text=True, timeout=120)
        print(p.stdout.strip() or p.stderr.strip())
    assert "1060" in sc("query", name).stdout, "service still registered"
    assert os.path.isfile(os.path.join(data, "config.json")), "uninstall must keep the data"
    if exe:
        assert not os.path.exists(winservice.installed_exe_path(name)), "the exe copy was left"
    shutil.rmtree(work, ignore_errors=True)
    print("uninstalled; data directory kept  OK")


def main():
    test_command_line()
    test_acl_decision_table()
    test_registered_command_read_back()
    test_upgrade_paths()
    test_install_locations()
    test_install_flow_with_a_fake_scm()
    test_cli_leaves_the_choice_to_install()
    test_not_windows()
    test_request_shutdown()
    test_real_service()
    print("\nPASS: Windows service mode")


if __name__ == "__main__":
    main()
