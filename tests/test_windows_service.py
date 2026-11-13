"""Windows service mode (--install-service / --service / --uninstall-service).

Portable part (any OS):
  * the command line the Service Control Manager runs is quoted correctly (spaces,
    quotes, trailing backslashes) for a frozen executable and a source checkout;
  * service names are validated; on Linux the options refuse with a hint (no files);
  * runtime.request_shutdown() — what the service's stop request calls, from another
    thread — stops a running server cleanly;
  * while a stop is under way the service reports STOP_PENDING again every few seconds
    (winservice.StopProgress), until the server has stopped;
  * the service is RUNNING only once the server is ready (START_PENDING with growing
    checkpoints until then), so --install-service reports a start that fails;
  * the service's stderr goes to service.log, and a service that stops on an error says
    why in all.log and the Application event log (the entry itself on Windows only).
Moving 2.x's service over (any OS, a stand-in Service Control Manager): the default
--install-service stops 2.x's 'ser2net' service (recognised by its registered command line),
moves its data from 2.x's default folder, starts 'ser2netweb' and only then deletes the old
one and its exe; a failure puts it all back; a custom data directory, and a 'ser2net' service
of another program, are left alone; --uninstall-service finds 2.x's service too.
Windows part (SER2NETWEB_SERVICE_TESTS=1, as Administrator; CI runs it): install a
service on a throwaway data directory, wait until the UI answers, set the admin's
password with --set-password while it runs (the service applies it; the request folder
leaves SYSTEM read and delete, and no other account an entry), kill the process
and see the SCM restart it, stop it (clean shutdown in the log), set the password again
while it is stopped (the data directory stays SYSTEM's and Administrators' only), start
it with that password, remove it. Uses SER2NETWEB_STANDALONE_BIN when set (the frozen exe),
else python + ser2netweb.py. With the exe it also registers a service as 2.x left it
('ser2net' from %ProgramFiles%\\ser2net, data in %ProgramData%\\ser2net) and moves it over
with --install-service.

Run: python3 tests/test_windows_service.py
"""
import io
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

from ser2netweb import envvars, runtime, winservice
from ser2netweb.web.auth import hash_password

WINDOWS = os.name == "nt"


def test_command_line():
    cmd = winservice.service_command("ser2netweb", r"C:\Program Files\ser2netweb\data", frozen=True,
                                     executable=r"C:\Program Files\ser2netweb\ser2netweb.exe")
    assert cmd == ('"C:\\Program Files\\ser2netweb\\ser2netweb.exe" --service --service-name '
                   'ser2netweb --data-dir "C:\\Program Files\\ser2netweb\\data"'), cmd
    cmd = winservice.service_command("plant-2", "D:\\s2n\\", config="D:\\cfg dir\\c.json",
                                     frozen=False, executable="C:\\Python312\\python.exe",
                                     script="C:\\src\\ser2netweb.py")
    assert cmd == ('C:\\Python312\\python.exe C:\\src\\ser2netweb.py --service --service-name plant-2 '
                   '--data-dir D:\\s2n\\ --config "D:\\cfg dir\\c.json"'), cmd
    assert winservice._quote('a "b"\\') == '"a \\"b\\"\\\\"'
    for bad in ("", "a b", "x" * 65, "a/b", "ser2netweb;calc"):
        try:
            winservice.check_name(bad)
        except winservice.ServiceError:
            pass
        else:
            raise AssertionError(f"service name {bad!r} accepted")
    assert winservice.DEFAULT_NAME == "ser2netweb"
    assert winservice.display_name("ser2netweb") == "ser2netweb"
    assert winservice.display_name("plant-2") == "ser2netweb (plant-2)"
    # what 2.x called them (its firewall rules have these names)
    assert winservice.old_display_name("ser2net") == "ser2net"
    assert winservice.old_display_name("plant-2") == "ser2net (plant-2)"
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
    if not WINDOWS:  # the ACL reader can't run and never blocks an install (Windows:
        # test_acl_reader, on folders with known permissions)
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

    # admin_only(): what lock_down_dir() leaves as it is. The data directory install()
    # hardens, and what an elevated command creates in it — admin-requests/ inherits it
    # (Python before 3.12.4 ignores mkdir's mode on Windows) or gets mkdir(0o700)'s own
    # protected ACL (SYSTEM, Administrators, OWNER RIGHTS): SYSTEM keeps full control of
    # the request files either way, so the service can read and delete them
    owner_rights = "S-1-3-4"
    data_dir = [(system, full, True, oi_ci), (admins, full, True, oi_ci)]
    inherited = [(sid, mask, True, inh | oi_ci) for sid, mask, _a, _f in data_dir]
    mkdir_700 = data_dir + [(owner_rights, full, True, oi_ci)]
    for aces in (data_dir, inherited, mkdir_700):
        assert w.admin_only(admins, aces) and w.admin_only(system, aces), aces
        assert any(sid == system and mask & 0x1 and mask & w._DELETE for sid, mask, *_r in aces)
    # ... but not with lock_down_dir()'s own grant, a user as owner (who may rewrite the
    # ACL), anyone else able to read it, or a NULL DACL (read_security: Everyone, all)
    assert not w.admin_only(admins, data_dir + [(user, full, True, oi_ci)])
    assert not w.admin_only(user, data_dir)
    assert not w.admin_only(admins, data_dir + [(users, rx, True, inh | oi_ci)])
    assert not w.admin_only(admins, [(everyone, w.GENERIC_ALL, True, 0)])
    # a user's own data directory (lock_down_dir's ACL, or ProgramData's defaults)
    assert not w.admin_only(user, [(user, full, True, oi_ci)] + data_dir)
    print("admin_only(): a hardened data directory and the request folder an elevated "
          "command makes in it (SYSTEM: read + delete); a user's grant, ownership or read "
          "access is not  OK")

    # service_folder(): what lock_down_dir() leaves as it is — the data directory as
    # install() leaves it and what inherits from it. A folder mkdir(0o700) made private to
    # its creator is that account's own, even an elevated administrator's that
    # Administrators own (Windows Server): lock_down_dir() gives the account its entry
    for owner in (admins, system):
        assert w.service_folder(owner, data_dir) and w.service_folder(owner, inherited)
        assert not w.service_folder(owner, mkdir_700)
        assert not w.service_folder(owner, [(sid, mask, True, inh | oi_ci)
                                            for sid, mask, _a, _f in mkdir_700])
    assert not w.service_folder(admins, data_dir + [(user, full, True, oi_ci)])
    assert not w.service_folder(user, data_dir)
    assert not w.service_folder(admins, data_dir + [(users, rx, True, inh | oi_ci)])
    print("service_folder(): the hardened data directory and what inherits from it; not "
          "a folder private to its creator (OWNER RIGHTS), a user's grant or ownership  OK")


def icacls(*args):
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return subprocess.run([os.path.join(root, "System32", "icacls.exe"), *args],
                          capture_output=True, text=True, timeout=60)


def test_acl_reader():
    """Windows: read_security() / writable_by_non_admins() / location_warning() on folders
    this test gives known permissions (who may change the checkout depends on the machine;
    on a CI runner it belongs to the runner's administrator account)."""
    if not WINDOWS:
        return
    w = winservice
    me = w._current_user_sid()
    base = tempfile.mkdtemp(prefix="s2n_acl_")
    folder = os.path.join(base, "program")
    exe = os.path.join(folder, "ser2net.exe")
    os.mkdir(folder)
    with open(exe, "w") as fh:
        fh.write("x")
    saved = w._program_paths
    try:
        w._harden(folder, users_read=True)  # what install() leaves on %ProgramFiles%\ser2net
        sec = w.read_security(folder)
        if not me or sec is None or sec[0] != w.ADMINS_SID:
            print("SKIP the ACL reader on real folders: making Administrators the owner "
                  "needs an elevated prompt")
            return
        assert w.writable_by_non_admins(folder) == [] and w.writable_by_non_admins(exe) == [], \
            (read_security_text(folder), read_security_text(exe))
        w._program_paths = lambda frozen: [exe, folder]
        assert w.location_warning() is None, w.location_warning()
        # owned by this (administrator's) account: its unelevated programs could change it
        assert icacls(folder, "/setowner", f"*{me}").returncode == 0
        assert w.writable_by_non_admins(folder) == [w.sid_name(me)], read_security_text(folder)
        assert w.writable_by_non_admins(exe) == [], read_security_text(exe)
        assert icacls(folder, "/setowner", f"*{w.ADMINS_SID}").returncode == 0
        # ... or given Modify (inherited by the exe)
        assert icacls(folder, "/grant", f"*{me}:(OI)(CI)M").returncode == 0
        assert w.writable_by_non_admins(exe) == [w.sid_name(me)], read_security_text(exe)
        warning = w.location_warning() or ""
        assert exe in warning and w.sid_name(me) in warning, warning
    finally:
        w._program_paths = saved
        icacls(base, "/reset", "/T", "/C", "/Q")
        shutil.rmtree(base, ignore_errors=True)
    print("ACL reader on real folders: the hardened program folder passes; this account as "
          "owner, or with Modify, is named, and the start-up warning says so  OK")


def test_registered_command_read_back():
    """An existing service's --data-dir / --config are read back from its command line
    (CommandLineToArgvW rules), for the 2.8.0 and the current layout alike."""
    w = winservice
    cases = [
        [r"C:\ser2net\ser2net.exe", "--service", "--service-name", "ser2net", "--data-dir",
         r"C:\ser2net\data"],                                                  # 2.8.0, frozen
        [r"C:\Program Files\ser2net\plant-2.exe", "--service", "--service-name", "plant-2",
         "--data-dir", r"C:\ProgramData\ser2net\plant 2" "\\", "--config", r'D:\cfg "x"\c.json'],
        [r"C:\Python312\python.exe", r"C:\src dir\ser2netweb.py", "--service", "--service-name",
         "s", "--data-dir", "D:\\s2n\\"],                                       # source
    ]
    for parts in cases:
        cmd = " ".join(w._quote(p) for p in parts)
        assert w.split_command_line(cmd) == parts, (cmd, w.split_command_line(cmd))
    got = w.parse_service_command(" ".join(w._quote(p) for p in cases[1]))
    assert got == {"executable": cases[1][0], "script": None, "service_name": "plant-2",
                   "data_dir": cases[1][5], "config": cases[1][7]}, got
    got = w.parse_service_command(" ".join(w._quote(p) for p in cases[2]))
    assert got["script"] == r"C:\src dir\ser2netweb.py" and got["data_dir"] == "D:\\s2n\\", got
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
        assert w.installed_exe_path("ser2netweb") == os.path.join(r"C:\PF", "ser2netweb",
                                                                  "ser2netweb.exe")
        assert w.installed_exe_path("plant-2") == os.path.join(r"C:\PF", "ser2netweb", "plant-2.exe")
        assert w.default_service_data_dir("ser2netweb") == os.path.join(r"C:\PD", "ser2netweb")
        assert w.default_service_data_dir("plant-2") == os.path.join(r"C:\PD", "ser2netweb",
                                                                     "plant-2")
        # 2.x's places, which an install moves away from
        assert w.old_installed_exe_path("ser2net") == os.path.join(r"C:\PF", "ser2net", "ser2net.exe")
        assert w.old_installed_exe_path("plant-2") == os.path.join(r"C:\PF", "ser2net", "plant-2.exe")
        assert w.old_default_data_dir("ser2net") == os.path.join(r"C:\PD", "ser2net")
        assert w.old_default_data_dir("plant-2") == os.path.join(r"C:\PD", "ser2net", "plant-2")
        # the SCM command points at the admin-only copy for a frozen install
        cmd = w.service_command("ser2netweb", r"C:\PD\ser2netweb", frozen=True,
                                executable=w.installed_exe_path("ser2netweb"))
        assert w.installed_exe_path("ser2netweb") in cmd and "--service" in cmd
        # the firewall hint is a program-scoped netsh rule named after the service
        fw = w.firewall_command("plant-2", exe=r"C:\PF\ser2netweb\plant-2.exe")
        assert fw.startswith("netsh advfirewall firewall add rule") and "ser2netweb (plant-2)" in fw
        assert r'program="C:\PF\ser2netweb\plant-2.exe"' in fw and "dir=in action=allow" in fw
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("frozen install path under %ProgramFiles%, data under %ProgramData%  OK")


class FakeSCM:
    """Just enough of advapi32 for install() and uninstall(): services by name (their
    command lines, display names, whether they run) and a start that can be made to fail
    once; every call is recorded in order. `existing_cmd` registers the service `name`,
    which `cmd` / `running` then describe."""

    def __init__(self, calls, existing_cmd=None, fail_start_once=False, name="plant-2",
                 services=None):
        self.calls, self.fail_start, self.name = calls, fail_start_once, name
        self.services = {n: dict(v) for n, v in (services or {}).items()}
        if existing_cmd is not None:
            self.services[name] = {"cmd": existing_cmd, "running": True, "display": "ser2net"}
        self.handles, self._next = {}, 10

    @property
    def cmd(self):
        return self.services.get(self.name, {}).get("cmd")

    @property
    def running(self):
        return self.services.get(self.name, {}).get("running", False)

    def state(self, svc):
        return self.services[self.handles[svc]]

    def _handle(self, name):
        self._next += 1
        self.handles[self._next] = name
        return self._next

    def OpenSCManagerW(self, *a):  # Win32 names
        return 1

    def OpenServiceW(self, scm, name, access):
        return self._handle(name) if name in self.services else 0

    def CreateServiceW(self, scm, name, display, access, kind, start, err, cmd, *rest):
        self.calls.append(("create", cmd))
        self.services[name] = {"cmd": cmd, "running": False, "display": display}
        return self._handle(name)

    def ChangeServiceConfigW(self, svc, kind, start, err, cmd, *rest):
        self.calls.append(("change", cmd))
        self.state(svc).update(cmd=cmd, display=rest[-1])
        return 1

    def StartServiceW(self, svc, *a):
        self.calls.append(("start", self.state(svc)["cmd"]))
        self.state(svc)["running"] = not self.fail_start  # the new build exits right away
        self.fail_start = False
        return 1

    def DeleteService(self, svc):
        self.calls.append(("delete", self.handles[svc]))
        self.services.pop(self.handles[svc], None)
        return 1

    def CloseServiceHandle(self, h):
        return 1


def fake_windows(scm, previous_cmd, calls):
    """Patch winservice so install() / uninstall() run against FakeSCM; returns an undo
    function. The Windows-only steps (ACLs, the lock test, stopping) only record that they
    ran. (previous_cmd: what FakeSCM registered; kept for the callers.)"""
    w = winservice
    saved = {n: getattr(w, n) for n in (
        "_advapi", "_win", "_query_registration", "_stop_and_wait", "_wait_lock_free",
        "_mkdir_admin_only", "_harden", "_require_protected", "_check_adoptable",
        "_exposed_ancestors", "_service_extras", "_wait_state", "_wait_started",
        "_unsafe_locations", "_parent_trusted")}

    def stop(api, svc, name, log):
        calls.append(("stop", scm.handles[svc]))
        state = scm.state(svc)
        was, state["running"] = state["running"], False
        return was

    def running(svc):
        return scm.handles.get(svc) in scm.services and scm.state(svc)["running"]

    w._advapi = lambda: SimpleNamespace(dll=scm, kernel=None)
    w._win = SimpleNamespace(get_last_error=lambda: w.ERROR_SERVICE_DOES_NOT_EXIST,
                             FormatError=lambda e: f"error {e}")
    w._query_registration = lambda api, svc: SimpleNamespace(
        service_type=0x10, start_type=2, error_control=1, binary_path=scm.state(svc)["cmd"],
        display_name=scm.state(svc)["display"])
    w._stop_and_wait = stop
    w._wait_lock_free = lambda d, timeout: calls.append(("lock-free", d))
    w._mkdir_admin_only = lambda d: os.makedirs(d, exist_ok=True)
    w._harden = lambda d, users_read: calls.append(("harden", d))
    w._require_protected = lambda paths, what: None
    w._check_adoptable = lambda d, config_path: calls.append(("adoptable", d))
    w._exposed_ancestors = lambda d: []
    w._service_extras = lambda *a: None
    w._wait_state = lambda api, svc, want, timeout=30.0: (
        w.SERVICE_RUNNING if running(svc) else w.SERVICE_STOPPED)
    w._wait_started = lambda api, svc, timeout=30.0: (
        (w.SERVICE_RUNNING, 0) if running(svc) else (w.SERVICE_STOPPED, 3))
    w._unsafe_locations = lambda paths, log: []
    w._parent_trusted = lambda path: True

    def undo():
        for n, v in saved.items():
            setattr(w, n, v)
    return undo


def test_install_flow_with_a_fake_scm():
    """install() against a stand-in Service Control Manager (any OS): re-running it while
    the service runs stops the service before the data directory's lock is tested and the
    configuration loaded; a (named) 2.8.0 service keeps its data directory; a failure after
    the stop puts the previous exe and command line back, starts it and is one line."""
    w = winservice
    work = tempfile.mkdtemp(prefix="s2n_flow_")
    env_saved = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramData")}
    exe_saved = sys.executable
    os.environ["ProgramFiles"] = os.path.join(work, "Program Files")
    os.environ["ProgramData"] = os.path.join(work, "ProgramData")
    try:
        new_build = os.path.join(work, "Downloads", "ser2netweb-windows-x64.exe")
        os.makedirs(os.path.dirname(new_build))
        with open(new_build, "w") as fh:
            fh.write("NEW")
        sys.executable = new_build
        old_dir = os.path.join(work, "ser2net")                 # where 2.8.0 ran from
        old_data = os.path.join(old_dir, "data")
        os.makedirs(old_data)
        with open(os.path.join(old_data, "config.json"), "w") as fh:
            json.dump({"users": [{"username": "admin"}], "mappings": [{"name": "PLC"}]}, fh)
        old_cmd = w.service_command("plant-2", old_data, frozen=True,
                                    executable=os.path.join(old_dir, "ser2net.exe"))
        dest = w.installed_exe_path("plant-2")

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
                result = w.install("plant-2", data_dir, None, prepare=prepare or default_prepare,
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
        installed_cmd = w.service_command("plant-2", old_data, frozen=True, executable=dest)
        scm = FakeSCM([], existing_cmd=installed_cmd, fail_start_once=True)
        try:
            run(scm, registered_cmd=installed_cmd)
        except w.ServiceError as e:
            msg = str(e)
            assert "\n" not in msg and "did not reach RUNNING" in msg, msg
            assert "stopped while starting (exit code 3)" in msg and "service.log" in msg, msg
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
            raise SystemExit("ser2netweb: config.json is not a valid configuration")
        scm = FakeSCM([], existing_cmd=old_cmd)
        try:
            run(scm, prepare=broken)
        except SystemExit as e:
            assert "not a valid configuration" in str(e.code)
        else:
            raise AssertionError("SystemExit was swallowed")
        assert scm.running and [c[0] for c in scm.calls][-1] == "start"
        print("a config that can't be loaded: the running version is started again  OK")

        # 5. a new service from the exe: %ProgramData%\ser2netweb, checked before it is adopted
        scm = FakeSCM([])
        calls, _result = run(scm)
        new_default = w.default_service_data_dir("plant-2")
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


def test_uninstall_says_what_is_kept():
    """--uninstall-service (against the stand-in SCM, any OS) names the data directory it
    keeps and what is in it, read from the service's registered command line."""
    calls, logs = [], []
    cmd = winservice.service_command("ser2netweb", r"C:\ProgramData\ser2netweb", frozen=True,
                                     executable=r"C:\Program Files\ser2netweb\ser2netweb.exe")
    scm = FakeSCM(calls, existing_cmd=cmd, name="ser2netweb")
    undo = fake_windows(scm, cmd, calls)
    saved = os.environ.get("ProgramFiles")
    os.environ["ProgramFiles"] = tempfile.mkdtemp(prefix="s2n_pf_")  # no exe copy to remove
    try:
        winservice.uninstall("ser2netweb", log=logs.append)
    finally:
        undo()
        shutil.rmtree(os.environ["ProgramFiles"], ignore_errors=True)
        if saved is None:
            del os.environ["ProgramFiles"]
        else:
            os.environ["ProgramFiles"] = saved
    assert ("delete", "ser2netweb") in calls, calls
    assert logs[-1] == winservice.kept_data_note("ser2netweb", r"C:\ProgramData\ser2netweb"), logs
    assert (r"Its data directory C:\ProgramData\ser2netweb is kept, with config.json" in logs[-1]
            and "audit.log" in logs[-1] and "traces/" in logs[-1]), logs[-1]
    assert "Its data directory is kept" in winservice.kept_data_note("x", None)
    print("--uninstall-service names the data directory it keeps and what is in it  OK")


def test_old_service_recognised():
    """is_ours() / plan_data_move(), the pure parts of moving a 2.x service over (any OS):
    what --install-service registered is recognised by its command line, never by the
    service's name alone; a data directory moves from 2.x's default place to 3.x's only
    when nothing else is in the way."""
    w = winservice

    def reg(name, exe, data, script=None, config=None):
        return {"executable": exe, "script": script, "service_name": name, "data_dir": data,
                "config": config}
    pd = r"C:\ProgramData"
    for ok in (reg("ser2net", r"C:\Program Files\ser2net\ser2net.exe", pd + r"\ser2net"),
               reg("ser2net", r"C:\Users\a\Downloads\ser2net-windows-x64.exe", r"C:\s2n\data"),
               reg("ser2net", r"C:\Python312\python.exe", r"C:\src\data", r"C:\src\ser2net.py"),
               reg("line1", r"C:\Program Files\ser2net\line1.exe", pd + r"\ser2net\line1")):
        assert w.is_ours(ok, ok["service_name"]), ok
    for bad in (None, reg("other", r"C:\Program Files\ser2net\ser2net.exe", pd + r"\ser2net"),
                reg("ser2net", r"C:\Tools\bridge.exe", r"C:\s2n\data"),
                reg("ser2net", r"C:\Python312\python.exe", r"C:\src\data", r"C:\src\tool.py"),
                reg("ser2net", r"C:\Program Files\ser2net\ser2net.exe", None)):
        assert not w.is_ours(bad, "ser2net"), bad
    assert w.parse_service_command(r'"C:\Program Files\ser2net\ser2net.exe" -c C:\x.yaml') is None

    saved = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramData")}
    os.environ["ProgramFiles"], os.environ["ProgramData"] = r"C:\PF", r"C:\PD"
    try:
        if os.name != "nt":  # (the decision compares paths of this OS: names only here)
            os.environ["ProgramFiles"], os.environ["ProgramData"] = "/PF", "/PD"
        old, new = w.old_default_data_dir("ser2net"), w.default_service_data_dir("ser2netweb")
        assert (old, new) == (os.path.join(os.environ["ProgramData"], "ser2net"),
                              os.path.join(os.environ["ProgramData"], "ser2netweb"))
        r = reg("ser2net", w.old_installed_exe_path("ser2net"), old)
        nothing, folders = (lambda p: False), (lambda p: ["backups", "logs", "tls"])
        got = w.plan_data_move(r, "ser2net", "ser2netweb", old, None, True, nothing, folders)
        assert got == (old, new, None, f"moving the data directory {old} to {new}"), got
        cfg = os.path.join(old, "config.json")
        assert w.plan_data_move(r, "ser2net", "ser2netweb", old, cfg, True, nothing,
                                folders)[2] == os.path.join(new, "config.json")
        for args, why in (((r, "ser2net", "ser2netweb", old, None, True, lambda p: p == new, folders),
                           "exists already"),
                          ((r, "ser2net", "ser2netweb", old, None, True, nothing,
                            lambda p: ["backups", "line1"]), "also holds line1"),
                          ((r, "ser2net", "ser2netweb", old, os.path.join("/elsewhere", "c.json"),
                            True, nothing, folders), "its configuration is")):
            got = w.plan_data_move(*args)
            assert got[:3] == (None, None, None) and why in got[3], (why, got)
        # not a candidate at all: a source install, a custom folder, --data-dir given
        for args in ((r, "ser2net", "ser2netweb", old, None, False, nothing, folders),
                     (reg("ser2net", "x.exe", "/srv/data"), "ser2net", "ser2netweb", "/srv/data",
                      None, True, nothing, folders),
                     (r, "ser2net", "ser2netweb", "/srv/other", None, True, nothing, folders),
                     (reg("ser2netweb", "x.exe", new), "ser2netweb", "ser2netweb", new, None, True,
                      nothing, folders)):
            assert w.plan_data_move(*args) == (None, None, None, ""), args
        named_old, named_new = w.old_default_data_dir("line1"), w.default_service_data_dir("line1")
        got = w.plan_data_move(reg("line1", "line1.exe", named_old), "line1", "line1", named_old,
                               None, True, nothing, folders)
        assert got[:2] == (named_old, named_new), got
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("2.x's services recognised by their command line (exe or script, name, data "
          "directory); 2.x's default data directory moves only when nothing is in the way  OK")


def test_move_from_2x_with_a_fake_scm():
    """--install-service of the default service where 2.x's 'ser2net' service is (against
    the stand-in SCM, any OS): it stops that one, moves its data from %ProgramData%\\ser2net,
    installs and starts 'ser2netweb' from %ProgramFiles%\\ser2netweb, and only then deletes
    the old service and its exe copy; a failed start puts everything back and starts the old
    one again. A custom data directory stays where it is; a 'ser2net' service that isn't
    ours is never touched. A named service is upgraded in place to the new folders.
    --uninstall-service finds 2.x's service when there is no new one."""
    w = winservice
    work = tempfile.mkdtemp(prefix="s2n_move_")
    env_saved = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramData")}
    exe_saved = sys.executable
    os.environ["ProgramFiles"] = os.path.join(work, "Program Files")
    os.environ["ProgramData"] = os.path.join(work, "ProgramData")
    try:
        new_build = os.path.join(work, "Downloads", "ser2netweb-windows-x64.exe")
        os.makedirs(os.path.dirname(new_build))
        with open(new_build, "w") as fh:
            fh.write("NEW")
        sys.executable = new_build

        def old_layout(name="ser2net", data=None):
            """What 2.8.1 / 2.9's --install-service left: its exe copy and data directory."""
            exe = w.old_installed_exe_path(name)
            data = data or w.old_default_data_dir(name)
            os.makedirs(os.path.dirname(exe), exist_ok=True)
            with open(exe, "w") as fh:
                fh.write("OLD")
            os.makedirs(os.path.join(data, "tls"), exist_ok=True)
            with open(os.path.join(data, "config.json"), "w") as fh:
                json.dump({"users": [{"username": "admin"}]}, fh)
            with open(os.path.join(data, "tls", "cert.pem"), "w") as fh:
                fh.write("CERT")
            return exe, data, w.service_command(name, data, frozen=True, executable=exe)

        def run(scm, name="ser2netweb", lines=None, data_dir=None):
            calls = []
            scm.calls = calls
            undo = fake_windows(scm, None, calls)
            try:
                w.install(name, data_dir, None, frozen=True,
                          prepare=lambda path: calls.append(("prepare", path)),
                          default_data_dir=os.path.join(work, "default-data"),
                          log=lines.append if lines is not None else (lambda *_: None))
            finally:
                undo()
            return calls

        def reset():
            for d in ("Program Files", "ProgramData"):
                shutil.rmtree(os.path.join(work, d), ignore_errors=True)

        pf_new, pd_new = w.installed_exe_path("ser2netweb"), w.default_service_data_dir("ser2netweb")
        # 1. 2.x's default service, data in its default place
        exe, data, cmd = old_layout()
        scm = FakeSCM([], services={"ser2net": {"cmd": cmd, "running": True, "display": "ser2net"}})
        lines = []
        calls = run(scm, lines=lines)
        kinds = [c[0] for c in calls]
        assert calls[:3] == [("stop", "ser2net"), ("lock-free", data),
                             ("prepare", os.path.join(pd_new, "config.json"))], calls
        assert "adoptable" not in kinds, "2.x's own data directory is not checked as a stranger's"
        created = next(c[1] for c in calls if c[0] == "create")
        assert w.parse_service_command(created) == {
            "executable": pf_new, "script": None, "service_name": "ser2netweb",
            "data_dir": pd_new, "config": None}, created
        assert kinds.index("start") < kinds.index("delete") and calls[-1] == ("delete", "ser2net")
        assert list(scm.services) == ["ser2netweb"] and scm.services["ser2netweb"]["running"]
        assert scm.services["ser2netweb"]["display"] == "ser2netweb"
        assert not os.path.exists(data) and not os.path.exists(w.old_install_dir())
        with open(os.path.join(pd_new, "tls", "cert.pem")) as fh:
            assert fh.read() == "CERT"
        with open(pf_new) as fh:
            assert fh.read() == "NEW"
        text = "\n".join(lines)
        for said in ("moving the 2.x service 'ser2net' over to 'ser2netweb'",
                     f"moved the data directory {data} to {pd_new}",
                     "removed the 2.x service 'ser2net'", f"removed {exe}",
                     'netsh advfirewall firewall delete rule name="ser2net"'):
            assert said in text, (said, text)
        print("2.x's service: stopped, data moved to %ProgramData%\\ser2netweb, ser2netweb "
              "installed and started, then the old service and its exe removed  OK")

        # 2. the new service doesn't start: all of it is put back, the old one runs again
        reset()
        exe, data, cmd = old_layout()
        scm = FakeSCM([], fail_start_once=True,
                      services={"ser2net": {"cmd": cmd, "running": True, "display": "ser2net"}})
        try:
            run(scm)
        except w.ServiceError as e:
            assert "did not reach RUNNING" in str(e) and \
                "the 2.x service 'ser2net' is running again" in str(e), str(e)
        else:
            raise AssertionError("a failed start was not reported")
        assert ("delete", "ser2netweb") in scm.calls and list(scm.services) == ["ser2net"], scm.calls
        assert scm.services["ser2net"]["running"] and scm.services["ser2net"]["cmd"] == cmd
        assert os.path.isfile(os.path.join(data, "config.json")) and not os.path.exists(pd_new)
        assert not os.path.exists(pf_new)
        with open(exe) as fh:
            assert fh.read() == "OLD"
        print("the new service doesn't start: it is removed, the data moved back, the 2.x "
              "service started again  OK")

        # 3. a custom data directory stays; 4. the new default place is taken; 5. other
        # services' folders in 2.x's: the data directory stays where it is
        for case in ("custom", "taken", "shared"):
            reset()
            custom = os.path.join(work, "plant", "data")
            shutil.rmtree(os.path.dirname(custom), ignore_errors=True)
            exe, data, cmd = old_layout(data=custom if case == "custom" else None)
            if case == "taken":
                os.makedirs(pd_new)
            if case == "shared":
                os.makedirs(os.path.join(data, "line1"))
            scm = FakeSCM([], services={"ser2net": {"cmd": cmd, "running": True,
                                                    "display": "ser2net"}})
            lines = []
            calls = run(scm, lines=lines)
            assert ("prepare", os.path.join(data, "config.json")) in calls, (case, calls)
            assert w.parse_service_command(next(c[1] for c in calls if c[0] == "create"))[
                "data_dir"] == data, case
            assert list(scm.services) == ["ser2netweb"], case
            if case != "custom":
                assert any("keeping the data directory" in ln for ln in lines), (case, lines)
        print("a custom data directory, a taken %ProgramData%\\ser2netweb, other services' "
              "folders in 2.x's: the data directory stays where it is  OK")

        # 6. a 'ser2net' service that isn't ours: left alone, ser2netweb is a new service
        for foreign in (r'"C:\Tools\ser2net\ser2net.exe" -c C:\Tools\ser2net.yaml',
                        w.service_command("other", r"C:\x", frozen=True,
                                          executable=r"C:\x\ser2net.exe")):
            reset()
            scm = FakeSCM([], services={"ser2net": {"cmd": foreign, "running": True,
                                                    "display": "ser2net"}})
            lines = []
            calls = run(scm, lines=lines)
            assert not [c for c in calls if c[0] in ("stop", "delete")], calls
            assert ("adoptable", pd_new) in calls, calls
            assert scm.services["ser2net"] == {"cmd": foreign, "running": True, "display": "ser2net"}
            assert any("is not one ser2net(web) --install-service registered" in ln
                       for ln in lines), lines
        print("a 'ser2net' service that isn't ours: never stopped or deleted  OK")

        # 7. a named 2.x service: upgraded in place, to the new folders
        reset()
        exe, data, cmd = old_layout("line1")
        scm = FakeSCM([], services={"line1": {"cmd": cmd, "running": True,
                                              "display": "ser2net (line1)"}})
        calls = run(scm, name="line1")
        change = next(c[1] for c in calls if c[0] == "change")
        assert w.parse_service_command(change)["executable"] == w.installed_exe_path("line1")
        assert w.parse_service_command(change)["data_dir"] == w.default_service_data_dir("line1")
        assert scm.services["line1"]["display"] == "ser2netweb (line1)"
        assert not os.path.exists(exe) and not os.path.exists(w.old_default_data_dir("ser2net"))
        assert os.path.isfile(os.path.join(w.default_service_data_dir("line1"), "config.json"))
        print("a named 2.x service: upgraded in place, exe and data moved to the new folders  OK")

        # 8. --uninstall-service: 2.x's service when there is no new one, never another's
        reset()
        exe, data, cmd = old_layout()
        scm = FakeSCM([], services={"ser2net": {"cmd": cmd, "running": True, "display": "ser2net"}})
        undo = fake_windows(scm, None, scm.calls)
        lines = []
        try:
            w.uninstall("ser2netweb", log=lines.append)
            try:
                w.uninstall("ser2netweb", log=lines.append)
            except w.ServiceError as e:
                assert "There is no service named 'ser2netweb'" in str(e), str(e)
            else:
                raise AssertionError("uninstalled twice")
            scm.services["ser2net"] = {"cmd": r'"C:\Tools\ser2net.exe" -c x', "running": True,
                                       "display": "ser2net"}
            try:
                w.uninstall("ser2netweb", log=lines.append)
            except w.ServiceError as e:
                assert "There is no service named 'ser2netweb'" in str(e), str(e)
            else:
                raise AssertionError("another program's service was uninstalled")
        finally:
            undo()
        assert ("delete", "ser2net") in scm.calls and "ser2net" in scm.services
        assert not os.path.exists(exe) and os.path.isfile(os.path.join(data, "config.json"))
        assert any("removing 2.x's 'ser2net'" in ln for ln in lines), lines
        print("--uninstall-service: 2.x's service and its exe copy when there is no new one "
              "(the data stays); another program's 'ser2net' never  OK")
    finally:
        sys.executable = exe_saved
        for k, v in env_saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(work, ignore_errors=True)


def test_run_reports_running_when_ready():
    """run() against a stand-in dispatcher (any OS): START_PENDING, its checkpoint counted
    up while the server starts, RUNNING only once the server is ready (when_ready), then
    STOPPED with the exit code; a start that fails never reports RUNNING; without the
    readiness hook RUNNING comes first, as before."""
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in (
            "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
            "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]

    class Times(type):
        def __mul__(cls, n):
            return lambda *entries: list(entries)

    class Entry(metaclass=Times):
        def __init__(self, name, fn=None):
            self.name, self.fn = name, fn

    reports = []

    class Dll:
        def RegisterServiceCtrlHandlerExW(self, name, handler, ctx):
            return 7

        def SetServiceStatus(self, h, ref):
            st = ref._obj
            reports.append((st.dwCurrentState, st.dwCheckPoint, st.dwServiceSpecificExitCode))
            return 1

        def StartServiceCtrlDispatcherW(self, table):
            table[0].fn(0, None)
            return 1

    api = SimpleNamespace(dll=Dll(), SERVICE_STATUS=Status, SERVICE_TABLE_ENTRYW=Entry,
                          handler_fn=lambda f: f, main_fn=lambda f=None: f)
    w = winservice
    saved = w._advapi, w.START_PROGRESS_S
    w._advapi, w.START_PROGRESS_S = (lambda: api), 0.1
    try:
        hooks = []

        def main_ready():
            time.sleep(0.35)       # starting: the mappings, the admin UI
            hooks.pop()()          # ready (runtime._announce_ready calls it)
            time.sleep(0.05)
            return 0
        assert w.run("ser2net", main_ready, lambda: None, on_ready=hooks.append) == 0
        states = [r[0] for r in reports]
        first_running = states.index(w.SERVICE_RUNNING)
        assert states[:first_running] == [w.SERVICE_START_PENDING] * first_running, states
        assert first_running >= 3, f"START_PENDING reported again while starting: {states}"
        checkpoints = [r[1] for r in reports[:first_running]]
        assert checkpoints == sorted(set(checkpoints)), checkpoints
        assert states[first_running:] == [w.SERVICE_RUNNING, w.SERVICE_STOPPED], states
        assert reports[-1][2] == 0

        reports.clear()

        def main_fails():
            time.sleep(0.15)
            raise SystemExit(3)    # e.g. the admin UI's port is in use
        assert w.run("ser2net", main_fails, lambda: None, on_ready=hooks.append) == 3
        states = [r[0] for r in reports]
        assert w.SERVICE_RUNNING not in states and states[-1] == w.SERVICE_STOPPED, states
        assert reports[-1][2] == 3
        hooks.clear()

        reports.clear()
        assert w.run("ser2net", lambda: 0, lambda: None) == 0
        assert [r[0] for r in reports] == [w.SERVICE_START_PENDING, w.SERVICE_RUNNING,
                                           w.SERVICE_STOPPED], reports
    finally:
        w._advapi, w.START_PROGRESS_S = saved
    print("service status: START_PENDING (checkpoints counted up) until ready, then RUNNING; "
          "a failed start never RUNNING, its exit code reported  OK")


def test_cli_leaves_the_choice_to_install():
    """ser2netweb.py no longer refuses before install() (which stops a running service
    first), and tells it whether --data-dir was given at all."""
    from ser2netweb import cli

    w = winservice
    saved = (w.require_windows, w.install)
    got = []
    w.require_windows = lambda: None
    w.install = lambda name, data_dir, config, **kw: got.append((data_dir, kw)) or None
    d = tempfile.mkdtemp(prefix="s2n_cli_")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; sys.path.insert(0, sys.argv[2]); "
         "from ser2netweb import datalock; assert datalock.acquire(sys.argv[1]) is None; "
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
        p = subprocess.run([sys.executable, "ser2netweb.py", "--no-bootstrap", "--data-dir",
                            os.path.join(d, "data"), flag], cwd=ROOT, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert p.returncode == 1 and "only on Windows" in p.stderr, (flag, p.stdout, p.stderr)
        # the systemd units: the source checkout's and the binary's
        assert "systemd/ser2netweb.service" in p.stderr, p.stderr
        assert "ser2netweb-standalone.service" in p.stderr, p.stderr
        assert "ser2netweb-install.sh installs" in p.stderr, p.stderr
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


def test_service_log_and_event():
    """The service's stderr goes to service.log (size-capped, the previous run's kept) instead
    of nowhere; a service that stops on an error writes the reason to the Application
    event log (Windows; elsewhere a no-op) and all.log."""
    d = tempfile.mkdtemp(prefix="s2n_svclog_")
    path = os.path.join(d, winservice.SERVICE_LOG)
    saved = sys.stdout, sys.stderr
    try:
        with open(path, "w") as fh:
            fh.write("previous run\n")
        sys.stdout = sys.stderr = None
        winservice.redirect_missing_stdio(path)
        log, out = sys.stderr, sys.stdout
        sys.stdout, sys.stderr = saved
        import faulthandler
        faulthandler.disable()
        assert isinstance(log, winservice.CappedLog) and out is not None
        print("Traceback (most recent call last):\n  boom", file=log)
        assert _read_text(path + ".1") == "previous run\n"
        text = _read_text(path)
        assert "service start (pid" in text and "boom" in text, text
        small = winservice.CappedLog(io.StringIO(), limit=50)
        small.encoding = "utf-8"
        for _ in range(10):
            small.write("0123456789")
        kept = small._fh.getvalue()
        assert kept.startswith("0123456789" * 5) and kept.count("service.log is full") == 1, kept
        out.close()
        log._fh.close()
        print("Windows service: stderr to service.log (previous run kept, size capped)  OK")
    finally:
        sys.stdout, sys.stderr = saved
        shutil.rmtree(d, ignore_errors=True)

    if os.name == "nt":
        ok = winservice.report_event("ser2net-tests", "ser2net test entry (can be ignored)")
        print(f"Application event log entry written: {ok}")
    else:
        assert winservice.report_event("ser2net", "x") is False
        print("SKIP: the event log entry itself is written on Windows only")

    # a service that stops on a configuration error: all.log and the event log say why
    d = tempfile.mkdtemp(prefix="s2n_svcevent_")
    events = []
    saved_report, saved_flag = winservice.report_event, runtime._as_service
    try:
        with open(os.path.join(d, "config.json"), "w") as fh:
            fh.write("{ not json")
        winservice.report_event = lambda source, text: events.append((source, text)) or True
        try:
            runtime.service_main(os.path.join(d, "config.json"), "plant-2")
        except SystemExit as e:
            assert "is not a valid configuration" in str(e.code), e.code
        else:
            raise AssertionError("the bad configuration was accepted")
        assert len(events) == 1 and events[0][0] == "plant-2", events
        assert "is not a valid configuration" in events[0][1] and "service.log" in events[0][1]
        assert "is not a valid configuration" in _read_text(os.path.join(d, "all.log"))
        print("service stops on a bad config.json: reason in all.log and the event log  OK")
    finally:
        winservice.report_event = saved_report
        runtime._as_service = saved_flag
        shutil.rmtree(d, ignore_errors=True)


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


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


def no_other_entries(path):
    """No entry that allows an account other than SYSTEM / Administrators anything (the
    CREATOR OWNER-style stand-ins aside), inherit-only ones included."""
    sec = winservice.read_security(path)
    return sec is not None and all(sid in winservice.SAFE_SIDS or sid.startswith("S-1-3-")
                                   or not allow for sid, _mask, allow, _flags in sec[1])


def system_rights(path):
    """What SYSTEM's own (not inherit-only) entries allow on `path`."""
    sec = winservice.read_security(path)
    rights = 0
    for sid, mask, allow, flags in (sec[1] if sec else []):
        if sid == winservice.SYSTEM_SID and allow and not flags & winservice.INHERIT_ONLY_ACE:
            rights |= mask
    return rights


def read_security_text(path):
    sec = winservice.read_security(path)
    if sec is None:
        return "unreadable"
    owner, aces = sec
    return f"owner {winservice.sid_name(owner)}: " + ", ".join(
        f"{winservice.sid_name(sid)} {mask:#x}{'' if allow else ' deny'}" for sid, mask, allow, _f in aces)


def test_stop_progress():
    reports = []
    progress = winservice.StopProgress(lambda: reports.append(time.monotonic()), every=0.05)
    progress.finish()
    progress.start()  # the server stopped on its own: nothing to report
    time.sleep(0.15)
    assert not reports, reports
    progress = winservice.StopProgress(lambda: reports.append(time.monotonic()), every=0.05)
    progress.start()
    progress.start()  # a second stop request (stop, then shutdown): one reporter
    time.sleep(0.4)
    progress.finish()
    seen = len(reports)
    assert 4 <= seen <= 10, seen
    time.sleep(0.15)
    assert len(reports) == seen, "reports after finish()"
    print(f"a stop under way is reported again every 50 ms here ({seen} times), and not "
          "after the server stopped  OK")


def test_real_service():
    if not WINDOWS or envvars.get("SER2NETWEB_SERVICE_TESTS") != "1":
        print("SKIP real Windows service test (Windows + Administrator + SER2NETWEB_SERVICE_TESTS=1)")
        return
    import psutil

    exe = envvars.get("SER2NETWEB_STANDALONE_BIN")
    base = [os.path.abspath(exe)] if exe else [sys.executable, os.path.join(ROOT, "ser2netweb.py")]
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
        assert p.returncode == 0 and "done by the running ser2netweb" in p.stdout, p.stdout + p.stderr
        Browser(f"http://127.0.0.1:{port}").login("admin", "svc-pass-2")
        # the request folder the (elevated) command made: SYSTEM may read and delete what
        # is in it, no other account has an entry (its owner is whatever the token gives)
        requests = os.path.join(data, "admin-requests")
        assert no_other_entries(requests) and system_rights(requests) & 0x10001 == 0x10001, \
            read_security_text(requests)
        assert not os.listdir(requests), os.listdir(requests)  # taken and answered
        print("--set-password while the service runs: the service applies it at once; only "
              "SYSTEM and Administrators have entries on the request folder  OK")

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

        # stopped: the command changes config.json itself, and the data directory stays
        # SYSTEM's and Administrators' only (no grant for the account that ran it)
        p = subprocess.run(base + ["--data-dir", data, "--set-password", "admin"],
                           input="svc-pass-3\n", capture_output=True, text=True, timeout=120)
        assert p.returncode == 0 and "new password set" in p.stdout, p.stdout + p.stderr
        assert "done by the running ser2netweb" not in p.stdout, p.stdout
        sec = winservice.read_security(data)
        assert sec is not None and winservice.service_folder(*sec), read_security_text(data)
        for path in (os.path.join(data, "config.json"), os.path.join(data, "backups")):
            assert no_other_entries(path), (path, read_security_text(path))
        r = sc("start", name)
        assert r.returncode == 0, r.stdout + r.stderr
        assert wait_for(lambda: healthy(port), 60), "the service did not start again"
        Browser(f"http://127.0.0.1:{port}").login("admin", "svc-pass-3")
        r = sc("stop", name)
        assert wait_for(lambda: "STOPPED" in sc("query", name).stdout, 60), sc("query", name).stdout
        print("--set-password while it is stopped: config.json changed, the data directory "
              "still admin-only, the service starts with the new password  OK")
    finally:
        p = subprocess.run(base + ["--uninstall-service", "--service-name", name],
                           capture_output=True, text=True, timeout=120)
        print(p.stdout.strip() or p.stderr.strip())
    assert "1060" in sc("query", name).stdout, "service still registered"
    assert os.path.isfile(os.path.join(data, "config.json")), "uninstall must keep the data"
    said = p.stdout + p.stderr
    assert "is kept, with config.json" in said and os.path.normcase(os.path.abspath(data)) \
        in os.path.normcase(said), said
    if exe:
        assert not os.path.exists(winservice.installed_exe_path(name)), "the exe copy was left"
    shutil.rmtree(work, ignore_errors=True)
    print("uninstalled; data directory kept  OK")


def _create_old_service(cmd: str) -> None:
    """Register 'ser2net' the way 2.8.1 / 2.9's --install-service did (display name
    'ser2net', auto start, LocalSystem), through the same Windows API calls."""
    w = winservice
    api = w._advapi()
    scm = api.dll.OpenSCManagerW(None, None, w.SC_MANAGER_CONNECT | w.SC_MANAGER_CREATE_SERVICE)
    assert scm, "no Service Control Manager"
    try:
        h = api.dll.CreateServiceW(scm, "ser2net", "ser2net", w.SERVICE_QUERY_STATUS | w.SERVICE_START,
                                   w.SERVICE_WIN32_OWN_PROCESS, w.SERVICE_AUTO_START,
                                   w.SERVICE_ERROR_NORMAL, cmd, None, None, None, None, None)
        assert h, f"CreateServiceW failed: {w._win.get_last_error()}"
        try:
            assert api.dll.StartServiceW(h, 0, None), f"StartServiceW: {w._win.get_last_error()}"
        finally:
            api.dll.CloseServiceHandle(h)
    finally:
        api.dll.CloseServiceHandle(scm)


def test_real_move_from_2x():
    """Windows, the standalone exe (SER2NETWEB_STANDALONE_BIN, SER2NETWEB_SERVICE_TESTS=1, as
    Administrator: CI's throwaway runner): a service as 2.8.1 / 2.9 left it — 'ser2net', its
    exe in %ProgramFiles%\\ser2net, its data in %ProgramData%\\ser2net — is moved over by a
    plain --install-service: 'ser2net' is gone, 'ser2netweb' runs from
    %ProgramFiles%\\ser2netweb with the same config.json, now in %ProgramData%\\ser2netweb;
    --uninstall-service removes it and its exe, the data stays."""
    exe = envvars.get("SER2NETWEB_STANDALONE_BIN")
    if not WINDOWS or envvars.get("SER2NETWEB_SERVICE_TESTS") != "1" or not exe:
        print("SKIP moving a 2.x service over (Windows + the standalone exe + "
              "SER2NETWEB_SERVICE_TESTS=1)")
        return
    w = winservice
    old_exe, old_data = w.old_installed_exe_path("ser2net"), w.old_default_data_dir("ser2net")
    new_data = w.default_service_data_dir("ser2netweb")
    taken = [p for p in (w.old_install_dir(), old_data, w.installed_exe_path("ser2netweb"), new_data)
             if os.path.lexists(p)]
    taken += [n for n in ("ser2net", "ser2netweb") if "1060" not in sc("query", n).stdout]
    if taken:
        print(f"SKIP moving a 2.x service over: {', '.join(taken)} exists (not a throwaway machine)")
        return
    port = free_port()
    try:
        os.makedirs(w.old_install_dir())
        shutil.copyfile(exe, old_exe)
        os.makedirs(old_data)
        config = {"admin_ui": {"bind_ip": "127.0.0.1", "port": port},
                  "users": [{"username": "admin", "password_hash": hash_password("move-pass-1"),
                             "role": "admin", "pwd_version": 1}]}
        with open(os.path.join(old_data, "config.json"), "w") as fh:
            json.dump(config, fh)
        _create_old_service(w.service_command("ser2net", old_data, frozen=True, executable=old_exe))
        assert wait_for(lambda: healthy(port), 60), "the 2.x-style service did not answer"
        print("a service as 2.x left it: 'ser2net' from %ProgramFiles%\\ser2net  OK")

        p = subprocess.run([os.path.abspath(exe), "--install-service"], capture_output=True,
                           text=True, timeout=180, stdin=subprocess.DEVNULL)
        print(p.stdout.strip())
        assert p.returncode == 0, p.stdout + p.stderr
        for said in ("moving the 2.x service 'ser2net' over", "moved the data directory",
                     "removed the 2.x service 'ser2net'", "is running"):
            assert said in p.stdout, (said, p.stdout)
        assert "1060" in sc("query", "ser2net").stdout, "the 2.x service is still registered"
        q = sc("qc", "ser2netweb").stdout
        assert w.installed_exe_path("ser2netweb") in q and new_data in q, q
        assert wait_for(lambda: healthy(port), 60), "ser2netweb did not answer"
        Browser(f"http://127.0.0.1:{port}").login("admin", "move-pass-1")
        assert not os.path.exists(old_data) and not os.path.exists(w.old_install_dir())
        sec = w.read_security(new_data)
        assert sec is not None and w.service_folder(*sec), read_security_text(new_data)
        print("--install-service: 'ser2net' moved over to 'ser2netweb' (exe and data in the new "
              "folders, same accounts, admin-only data)  OK")

        p = subprocess.run([os.path.abspath(exe), "--uninstall-service"], capture_output=True,
                           text=True, timeout=120)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "1060" in sc("query", "ser2netweb").stdout
        assert not os.path.exists(w.installed_exe_path("ser2netweb"))
        assert os.path.isfile(os.path.join(new_data, "config.json")), "uninstall must keep the data"
        print("--uninstall-service: ser2netweb and its exe removed, the data kept  OK")
    finally:
        for n in ("ser2netweb", "ser2net"):
            if "1060" not in sc("query", n).stdout:
                sc("stop", n)
                wait_for(lambda n=n: "STOPPED" in sc("query", n).stdout, 60)
                sc("delete", n)
        for folder in (w.old_install_dir(), old_data, w.install_dir(), new_data):
            if os.path.isdir(folder):
                icacls(folder, "/reset", "/T", "/C", "/Q")
                shutil.rmtree(folder, ignore_errors=True)


def main():
    test_command_line()
    test_acl_decision_table()
    test_acl_reader()
    test_registered_command_read_back()
    test_upgrade_paths()
    test_install_locations()
    test_install_flow_with_a_fake_scm()
    test_uninstall_says_what_is_kept()
    test_old_service_recognised()
    test_move_from_2x_with_a_fake_scm()
    test_run_reports_running_when_ready()
    test_cli_leaves_the_choice_to_install()
    test_not_windows()
    test_request_shutdown()
    test_stop_progress()
    test_service_log_and_event()
    test_real_service()
    test_real_move_from_2x()
    print("\nPASS: Windows service mode")


if __name__ == "__main__":
    main()
