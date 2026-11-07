"""Run ser2net as a Windows service, with the Windows API itself (ctypes) — neither
pywin32 nor a wrapper program (NSSM, Shawl) is needed.

    ser2net.exe --install-service [--data-dir D:\\ser2net\\data] [--service-name NAME]
    ser2net.exe --uninstall-service [--service-name NAME]

``--install-service`` registers the running executable (or python + ser2net.py from a
source checkout) to start at boot as LocalSystem with that data directory, restarts it
5 s after it dies or exits with an error, and starts it. The Service Control Manager
then runs ``ser2net.exe --service --data-dir ...``: ``run()`` hands the process to the
SCM dispatcher, runs the normal server in the service thread and stops it cleanly on a
stop request or at shutdown. Windows only; everything is imported lazily so the module
loads (and its pure helpers are testable) on any OS.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

# ctypes with its Windows-only parts (WinDLL, WINFUNCTYPE, get_last_error, FormatError)
_win: Any = ctypes

DEFAULT_NAME = "ser2net"
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# where a frozen build installs itself so only administrators can replace the exe a
# LocalSystem service runs (see install()); the folder name under %ProgramFiles%
INSTALL_SUBDIR = "ser2net"

# winsvc.h
SC_MANAGER_CONNECT = 0x0001
SC_MANAGER_CREATE_SERVICE = 0x0002
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
DELETE = 0x00010000
SERVICE_CHANGE_CONFIG = 0x0002
SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_AUTO_START = 0x00000002
SERVICE_ERROR_NORMAL = 0x00000001
SERVICE_NO_CHANGE = 0xFFFFFFFF
SERVICE_CONFIG_DESCRIPTION = 1
SERVICE_CONFIG_FAILURE_ACTIONS = 2
SERVICE_CONFIG_FAILURE_ACTIONS_FLAG = 4
SC_ACTION_RESTART = 1
SERVICE_CONTROL_STOP = 1
SERVICE_CONTROL_INTERROGATE = 4
SERVICE_CONTROL_SHUTDOWN = 5
SERVICE_ACCEPT_STOP = 0x1
SERVICE_ACCEPT_SHUTDOWN = 0x4
SERVICE_STOPPED = 1
SERVICE_START_PENDING = 2
SERVICE_STOP_PENDING = 3
SERVICE_RUNNING = 4
NO_ERROR = 0
ERROR_ACCESS_DENIED = 5
ERROR_CALL_NOT_IMPLEMENTED = 120
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
ERROR_SERVICE_SPECIFIC_ERROR = 1066
ERROR_SERVICE_EXISTS = 1073
ERROR_SERVICE_MARKED_FOR_DELETE = 1072

RESTART_DELAY_MS = 5000


class ServiceError(Exception):
    """A user-facing service installation / control problem."""


# --------------------------------------------------------------------------
# pure helpers (any OS)
# --------------------------------------------------------------------------
def check_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ServiceError("Service name: 1-64 letters, digits, '.', '_' or '-'.")
    return name


def _quote(arg: str) -> str:
    """Quote one command-line argument the way CommandLineToArgvW reads it."""
    if arg and not re.search(r'[\s"]', arg):
        return arg
    out, slashes = ['"'], 0
    for ch in arg:
        if ch == "\\":
            slashes += 1
            continue
        if ch == '"':
            out.append("\\" * (2 * slashes + 1) + '"')
        else:
            out.append("\\" * slashes + ch)
        slashes = 0
    out.append("\\" * (2 * slashes) + '"')
    return "".join(out)


def service_command(name: str, data_dir: str, config: Optional[str] = None,
                    frozen: Optional[bool] = None, executable: Optional[str] = None,
                    script: Optional[str] = None) -> str:
    """The command line the SCM starts: this executable (or python + ser2net.py) with
    --service and the absolute data directory."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    executable = executable or sys.executable
    parts = [executable]
    if not frozen:
        parts.append(script or os.path.abspath(sys.argv[0]))
    parts += ["--service", "--service-name", check_name(name), "--data-dir", data_dir]
    if config:
        parts += ["--config", config]
    return " ".join(_quote(p) for p in parts)


def display_name(name: str) -> str:
    return "ser2net" if name == DEFAULT_NAME else f"ser2net ({name})"


# --------------------------------------------------------------------------
# Where a frozen build installs itself (admin-only), and the ACL decision table
# --------------------------------------------------------------------------
def program_files() -> str:
    return os.environ.get("ProgramFiles") or r"C:\Program Files"


def program_data() -> str:
    return os.environ.get("ProgramData") or r"C:\ProgramData"


def install_dir() -> str:
    """The admin-only folder a frozen build copies itself into on --install-service."""
    return os.path.join(program_files(), INSTALL_SUBDIR)


def installed_exe_path(name: str) -> str:
    """Where the service's exe lives after install: ser2net.exe for the default service,
    <name>.exe for a named one, so several services share one admin-only folder but each
    upgrades to its own path."""
    return os.path.join(install_dir(), "ser2net.exe" if name == DEFAULT_NAME else f"{name}.exe")


def default_service_data_dir(name: str) -> str:
    """Admin-controlled data directory for a frozen service install (not next to the exe,
    which would otherwise sit under %ProgramFiles% read-only to the service)."""
    base = os.path.join(program_data(), INSTALL_SUBDIR)
    return base if name == DEFAULT_NAME else os.path.join(base, name)


def firewall_command(name: str, exe: Optional[str] = None) -> str:
    """The netsh rule that lets other hosts reach a LocalSystem service (which never sees
    the interactive Windows Firewall prompt). One program-scoped rule covers the admin UI,
    every mapping's listen port and the cluster's UDP discovery. Printed as a hint, not
    applied — the firewall stays closed by default."""
    if exe is None:
        exe = (installed_exe_path(name) if getattr(sys, "frozen", False)
               else os.path.abspath(sys.executable))
    return (f'netsh advfirewall firewall add rule name="{display_name(name)}" dir=in '
            f'action=allow program="{exe}" enable=yes')


# Access-right bits that let a principal replace or delete a file or add one to a folder
# (winnt.h). Any of these on the service's exe or its folder is a local-privilege-
# escalation path when the service runs as LocalSystem.
FILE_WRITE_DATA = 0x0002
FILE_APPEND_DATA = 0x0004
FILE_WRITE_EA = 0x0010
FILE_DELETE_CHILD = 0x0040
_DELETE = 0x00010000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
GENERIC_WRITE = 0x40000000
GENERIC_ALL = 0x10000000
DANGEROUS_RIGHTS = (FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_DELETE_CHILD
                    | _DELETE | WRITE_DAC | WRITE_OWNER | GENERIC_WRITE | GENERIC_ALL)

# SIDs that are expected to have full control of a system location: SYSTEM, LocalService,
# NetworkService, the Administrators group. S-1-5-80-* (per-service SIDs, incl.
# TrustedInstaller) are treated as safe by _is_safe_sid.
SAFE_SIDS = frozenset({"S-1-5-18", "S-1-5-19", "S-1-5-20", "S-1-5-32-544"})


def _is_safe_sid(sid: str) -> bool:
    return sid in SAFE_SIDS or sid.startswith("S-1-5-80-")


def acl_offenders(aces) -> list[str]:
    """Pure decision table (any OS; the Windows caller reads the ACEs). Given a file/dir's
    access-control entries as (sid, access_mask, allow), return the sorted SIDs of
    non-administrator principals left with a right that could replace or delete it —
    honouring a deny ACE for the same SID. Empty means only admins/SYSTEM can write."""
    allow: dict[str, int] = {}
    deny: dict[str, int] = {}
    for sid, mask, is_allow in aces:
        (allow if is_allow else deny)[sid] = (allow if is_allow else deny).get(sid, 0) | int(mask)
    offenders = [sid for sid, mask in allow.items()
                 if not _is_safe_sid(sid) and (mask & DANGEROUS_RIGHTS & ~deny.get(sid, 0))]
    return sorted(offenders)


# --------------------------------------------------------------------------
# Windows API (lazy)
# --------------------------------------------------------------------------
_api = None


def require_windows() -> None:
    if os.name != "nt":
        raise ServiceError("Windows services exist only on Windows "
                           "(on Linux, use systemd: see systemd/ser2net.service).")


def _advapi():
    global _api
    if _api is not None:
        return _api
    require_windows()
    from ctypes import wintypes as w

    class SERVICE_STATUS(ctypes.Structure):  # noqa: N801 (Win32 names)
        _fields_ = [("dwServiceType", w.DWORD), ("dwCurrentState", w.DWORD),
                    ("dwControlsAccepted", w.DWORD), ("dwWin32ExitCode", w.DWORD),
                    ("dwServiceSpecificExitCode", w.DWORD), ("dwCheckPoint", w.DWORD),
                    ("dwWaitHint", w.DWORD)]

    class SC_ACTION(ctypes.Structure):  # noqa: N801
        _fields_ = [("Type", w.DWORD), ("Delay", w.DWORD)]

    class SERVICE_FAILURE_ACTIONSW(ctypes.Structure):  # noqa: N801
        _fields_ = [("dwResetPeriod", w.DWORD), ("lpRebootMsg", w.LPWSTR),
                    ("lpCommand", w.LPWSTR), ("cActions", w.DWORD),
                    ("lpsaActions", ctypes.POINTER(SC_ACTION))]

    class SERVICE_FAILURE_ACTIONS_FLAG(ctypes.Structure):  # noqa: N801
        _fields_ = [("fFailureActionsOnNonCrashFailures", w.BOOL)]

    class SERVICE_DESCRIPTIONW(ctypes.Structure):  # noqa: N801
        _fields_ = [("lpDescription", w.LPWSTR)]

    main_fn = _win.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))
    handler_fn = _win.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID)

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):  # noqa: N801
        _fields_ = [("lpServiceName", w.LPWSTR), ("lpServiceProc", main_fn)]

    dll = _win.WinDLL("advapi32", use_last_error=True)
    sig = {
        "OpenSCManagerW": (w.HANDLE, [w.LPCWSTR, w.LPCWSTR, w.DWORD]),
        "CreateServiceW": (w.HANDLE, [w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD,
                                      w.DWORD, w.LPCWSTR, w.LPCWSTR, w.LPVOID, w.LPCWSTR,
                                      w.LPCWSTR, w.LPCWSTR]),
        "OpenServiceW": (w.HANDLE, [w.HANDLE, w.LPCWSTR, w.DWORD]),
        "ChangeServiceConfigW": (w.BOOL, [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR,
                                          w.LPCWSTR, w.LPDWORD, w.LPCWSTR, w.LPCWSTR,
                                          w.LPCWSTR, w.LPCWSTR]),
        "ChangeServiceConfig2W": (w.BOOL, [w.HANDLE, w.DWORD, w.LPVOID]),
        "StartServiceW": (w.BOOL, [w.HANDLE, w.DWORD, w.LPVOID]),
        "ControlService": (w.BOOL, [w.HANDLE, w.DWORD, ctypes.POINTER(SERVICE_STATUS)]),
        "QueryServiceStatus": (w.BOOL, [w.HANDLE, ctypes.POINTER(SERVICE_STATUS)]),
        "DeleteService": (w.BOOL, [w.HANDLE]),
        "CloseServiceHandle": (w.BOOL, [w.HANDLE]),
        "StartServiceCtrlDispatcherW": (w.BOOL, [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]),
        "RegisterServiceCtrlHandlerExW": (w.HANDLE, [w.LPCWSTR, handler_fn, w.LPVOID]),
        "SetServiceStatus": (w.BOOL, [w.HANDLE, ctypes.POINTER(SERVICE_STATUS)]),
    }
    for fname, (res, args) in sig.items():
        f = getattr(dll, fname)
        f.restype, f.argtypes = res, args

    _api = SimpleNamespace(dll=dll, SERVICE_STATUS=SERVICE_STATUS, SC_ACTION=SC_ACTION,
                           SERVICE_FAILURE_ACTIONSW=SERVICE_FAILURE_ACTIONSW,
                           SERVICE_FAILURE_ACTIONS_FLAG=SERVICE_FAILURE_ACTIONS_FLAG,
                           SERVICE_DESCRIPTIONW=SERVICE_DESCRIPTIONW, main_fn=main_fn,
                           handler_fn=handler_fn, SERVICE_TABLE_ENTRYW=SERVICE_TABLE_ENTRYW)
    return _api


def writable_by_non_admins(path: str) -> Optional[list[str]]:
    """The non-administrator principals (resolved to names where possible) that can
    replace or delete `path` — [] when only admins/SYSTEM can, None when it can't be
    determined (an unreadable ACL, or not Windows). Reads the DACL through the Windows
    API and feeds it to acl_offenders(); callers never hard-fail on None."""
    if sys.platform != "win32" or not os.path.exists(path):
        return None
    from ctypes import wintypes as w

    advapi = _win.WinDLL("advapi32", use_last_error=True)
    kernel = _win.WinDLL("kernel32", use_last_error=True)
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x4
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, ctypes.c_int, w.DWORD,
                                             ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p),
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAclInformation.restype = w.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_int]
    advapi.GetAce.restype = w.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]

    class ACL_SIZE_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD),
                    ("AclBytesFree", w.DWORD)]

    class ACE_HEADER(ctypes.Structure):  # noqa: N801
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", w.WORD)]

    class ALLOWED_ACE(ctypes.Structure):  # noqa: N801 (ACCESS_ALLOWED_ACE / _DENIED_ACE share a layout)
        _fields_ = [("Header", ACE_HEADER), ("Mask", w.DWORD), ("SidStart", w.DWORD)]

    pdacl = ctypes.c_void_p()
    psd = ctypes.c_void_p()
    rc = advapi.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                      None, None, ctypes.byref(pdacl), None, ctypes.byref(psd))
    if rc != NO_ERROR:
        return None
    try:
        if not pdacl:  # a NULL DACL grants everyone everything
            return ["Everyone"]
        info = ACL_SIZE_INFORMATION()
        if not advapi.GetAclInformation(pdacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            return None
        aces, sids = [], []
        for i in range(info.AceCount):
            pace = ctypes.c_void_p()
            if not advapi.GetAce(pdacl, i, ctypes.byref(pace)):
                return None
            ace = ctypes.cast(pace, ctypes.POINTER(ALLOWED_ACE)).contents
            if ace.Header.AceType not in (0, 1):  # ACCESS_ALLOWED_ACE_TYPE / _DENIED_ACE_TYPE
                continue
            psid = ctypes.c_void_p(pace.value + ALLOWED_ACE.SidStart.offset)
            sid_str = w.LPWSTR()
            if not advapi.ConvertSidToStringSidW(psid, ctypes.byref(sid_str)):
                return None
            sid = sid_str.value or ""
            kernel.LocalFree(sid_str)
            aces.append((sid, ace.Mask, ace.Header.AceType == 0))
            sids.append((sid, psid.value))
        offenders = acl_offenders(aces)
        return [_sid_display(advapi, dict(sids)[sid], sid) for sid in offenders]
    finally:
        kernel.LocalFree(psd)


def _sid_display(advapi, psid: int, sid: str) -> str:
    """A readable 'DOMAIN\\name' for a SID, falling back to the SID string."""
    from ctypes import wintypes as w

    advapi.LookupAccountSidW.restype = w.BOOL
    advapi.LookupAccountSidW.argtypes = [w.LPCWSTR, ctypes.c_void_p, w.LPWSTR,
                                         ctypes.POINTER(w.DWORD), w.LPWSTR,
                                         ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    name = ctypes.create_unicode_buffer(256)
    domain = ctypes.create_unicode_buffer(256)
    cch_name = w.DWORD(256)
    cch_domain = w.DWORD(256)
    use = w.DWORD()
    if advapi.LookupAccountSidW(None, ctypes.c_void_p(psid), name, ctypes.byref(cch_name),
                                domain, ctypes.byref(cch_domain), ctypes.byref(use)):
        return f"{domain.value}\\{name.value}" if domain.value else name.value
    return sid


def _unsafe_locations(paths, log: Callable[[str], None]) -> list[str]:
    """Of `paths`, those a non-administrator can replace or add files to, each as
    '<path> (<who>)'. A path whose ACL can't be read is noted via log and skipped."""
    out = []
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        who = writable_by_non_admins(p)
        if who is None:
            log(f"note: could not check who can modify {p}")
        elif who:
            out.append(f"{p} ({', '.join(who)})")
    return out


def _fail(what: str) -> ServiceError:
    err = _win.get_last_error()
    if err == ERROR_ACCESS_DENIED:
        return ServiceError(f"{what}: access denied — run this from an elevated "
                            "(\"Run as administrator\") command prompt.")
    return ServiceError(f"{what} failed: {_win.FormatError(err).strip()} (error {err})")


def _wait_state(api, svc, want: int, timeout: float = 30.0) -> int:
    st = api.SERVICE_STATUS()
    end = time.monotonic() + timeout
    while True:
        if not api.dll.QueryServiceStatus(svc, ctypes.byref(st)):
            raise _fail("QueryServiceStatus")
        if st.dwCurrentState == want or time.monotonic() > end:
            return st.dwCurrentState
        time.sleep(0.3)


def _copy_exe_to(dest: str, log: Callable[[str], None]) -> None:
    """Copy the running frozen exe to the admin-only install path (the caller stopped any
    service using dest first, so the file is not locked). Best effort to lock the folder
    down even though %ProgramFiles% is already admin-only."""
    import shutil

    from .config import lock_down_dir

    src = os.path.abspath(sys.executable)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    lock_down_dir(os.path.dirname(dest))
    for attempt in range(10):  # a just-stopped service can hold the old exe a moment longer
        try:
            shutil.copy2(src, dest)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)
    log(f"installed the program to {dest}")


def _service_extras(api, svc, name: str, data_dir: str) -> None:
    """Description + restart-on-failure actions, shared by a fresh install and an upgrade."""
    desc = api.SERVICE_DESCRIPTIONW(
        f"Serial-to-network bridge with a web UI. Data directory: {data_dir}")
    api.dll.ChangeServiceConfig2W(svc, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(desc))
    actions = (api.SC_ACTION * 3)(*[api.SC_ACTION(SC_ACTION_RESTART, RESTART_DELAY_MS)] * 3)
    fa = api.SERVICE_FAILURE_ACTIONSW(86400, None, None, 3, actions)
    if not api.dll.ChangeServiceConfig2W(svc, SERVICE_CONFIG_FAILURE_ACTIONS, ctypes.byref(fa)):
        raise _fail("Setting the restart-on-failure actions")
    flag = api.SERVICE_FAILURE_ACTIONS_FLAG(True)  # also after an exit with an error
    api.dll.ChangeServiceConfig2W(svc, SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, ctypes.byref(flag))


def _guard_location(frozen: bool, allow_unsafe_location: bool,
                    log: Callable[[str], None]) -> Optional[str]:
    """For a source checkout, refuse to install when a non-administrator can replace the
    interpreter, this checkout, app/ or lib/ — the service would run their code as
    LocalSystem. Returns the exe to register from (a frozen build's admin-only copy), or
    None for the source path (python + ser2net.py stay where they are)."""
    if frozen:
        return None  # install() relocates the exe itself
    root = os.path.dirname(os.path.abspath(sys.argv[0]))
    checked = [os.path.dirname(os.path.abspath(sys.executable)), root,
               os.path.join(root, "app"), os.path.join(root, "lib")]
    unsafe = _unsafe_locations(checked, log)
    if unsafe and not allow_unsafe_location:
        raise ServiceError(
            "a non-administrator can modify where this runs from, so the LocalSystem "
            "service would run their code:\n  " + "\n  ".join(unsafe) + "\nMove the "
            "checkout under an admin-only folder (e.g. %ProgramFiles%\\ser2net), or, if "
            "this location is trusted, pass --allow-unsafe-location. The standalone exe "
            "installs itself to an admin-only folder and needs no flag.")
    if unsafe:
        log("WARNING: installing although a non-administrator can modify:\n  "
            + "\n  ".join(unsafe))
    return None


def location_warning() -> Optional[str]:
    """A WARNING line for the service log when a non-administrator can replace the program
    this (LocalSystem) service runs — else None. Frozen: the exe itself; source: the
    interpreter's folder, the checkout and app/ / lib/. Returns None off Windows."""
    if getattr(sys, "frozen", False):
        targets = [os.path.abspath(sys.executable)]
    else:
        root = os.path.dirname(os.path.abspath(sys.argv[0]))
        targets = [os.path.dirname(os.path.abspath(sys.executable)), root,
                   os.path.join(root, "app"), os.path.join(root, "lib")]
    unsafe = _unsafe_locations(targets, lambda *_: None)
    if not unsafe:
        return None
    return ("WARNING: a non-administrator can modify where this service runs from, so "
            "they could run code as LocalSystem: " + "; ".join(unsafe) + " — reinstall "
            "the standalone exe (it installs itself to an admin-only folder) or move the "
            "checkout under %ProgramFiles%")


def install(name: str, data_dir: str, config: Optional[str] = None, start: bool = True,
            log: Callable[[str], None] = print, *, frozen: Optional[bool] = None,
            allow_unsafe_location: bool = False) -> None:
    api = _advapi()
    name = check_name(name)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    _guard_location(frozen, allow_unsafe_location, log)
    # A frozen build registers (and, when upgrading, copies itself to) an admin-only
    # path under %ProgramFiles%, so a non-administrator can't replace the exe the
    # LocalSystem service runs. A source checkout stays where it is (guarded above).
    dest = installed_exe_path(name) if frozen else None
    cmd = (service_command(name, data_dir, config, frozen=frozen, executable=dest) if dest
           else service_command(name, data_dir, config, frozen=frozen))
    already_installed = dest is not None and os.path.abspath(sys.executable) == os.path.abspath(dest)

    scm = api.dll.OpenSCManagerW(None, None, SC_MANAGER_CONNECT | SC_MANAGER_CREATE_SERVICE)
    if not scm:
        raise _fail("Opening the Service Control Manager")
    try:
        existing = api.dll.OpenServiceW(
            scm, name, SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP
            | SERVICE_CHANGE_CONFIG | DELETE)
        if existing:  # upgrade in place: stop it, replace the exe, point it at cmd, start
            try:
                _upgrade(api, existing, name, data_dir, config, cmd, dest, already_installed,
                         start, log)
            finally:
                api.dll.CloseServiceHandle(existing)
            return
        if dest and not already_installed:
            _copy_exe_to(dest, log)
        svc = api.dll.CreateServiceW(
            scm, name, display_name(name),
            SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP | SERVICE_CHANGE_CONFIG | DELETE,
            SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
            cmd, None, None, None, None, None)  # LocalSystem
        if not svc:
            raise _fail("Creating the service")
        try:
            _service_extras(api, svc, name, data_dir)
            log(f"service '{name}' installed: starts at boot, restarts {RESTART_DELAY_MS // 1000} s "
                f"after a failure\n  command: {cmd}")
            if start:
                _start_and_wait(api, svc, name, data_dir, log)
        finally:
            api.dll.CloseServiceHandle(svc)
    finally:
        api.dll.CloseServiceHandle(scm)


def _upgrade(api, svc, name: str, data_dir: str, config: Optional[str], cmd: str,
             dest: Optional[str], already_installed: bool, start: bool,
             log: Callable[[str], None]) -> None:
    """Re-point an existing service at the current build and restart it (no uninstall, so
    the service keeps its identity and dependents)."""
    st = api.SERVICE_STATUS()
    if api.dll.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st)):
        _wait_state(api, svc, SERVICE_STOPPED)
        log(f"service '{name}' stopped for the upgrade")
    elif _win.get_last_error() not in (ERROR_SERVICE_NOT_ACTIVE, NO_ERROR):
        raise _fail("Stopping the service to upgrade it")
    if dest and not already_installed:  # the old copy is now unlocked
        _copy_exe_to(dest, log)
    if not api.dll.ChangeServiceConfigW(svc, SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START,
                                        SERVICE_ERROR_NORMAL, cmd, None, None, None, None, None,
                                        display_name(name)):
        raise _fail("Updating the service configuration")
    _service_extras(api, svc, name, data_dir)
    log(f"service '{name}' upgraded in place\n  command: {cmd}")
    if start:
        _start_and_wait(api, svc, name, data_dir, log)


def _start_and_wait(api, svc, name: str, data_dir: str, log: Callable[[str], None]) -> None:
    if not api.dll.StartServiceW(svc, 0, None):
        raise _fail("Starting the service")
    state = _wait_state(api, svc, SERVICE_RUNNING)
    if state != SERVICE_RUNNING:
        raise ServiceError(f"The service did not reach RUNNING (state {state}); "
                           f"see {os.path.join(data_dir, 'all.log')}.")
    log(f"service '{name}' is running")


def uninstall(name: str, log: Callable[[str], None] = print) -> None:
    api = _advapi()
    name = check_name(name)
    scm = api.dll.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        raise _fail("Opening the Service Control Manager")
    try:
        svc = api.dll.OpenServiceW(scm, name, SERVICE_QUERY_STATUS | SERVICE_STOP | DELETE)
        if not svc:
            if _win.get_last_error() == ERROR_SERVICE_DOES_NOT_EXIST:
                raise ServiceError(f"There is no service named '{name}'.")
            raise _fail("Opening the service")
        try:
            st = api.SERVICE_STATUS()
            if api.dll.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st)):
                _wait_state(api, svc, SERVICE_STOPPED)
                log(f"service '{name}' stopped")
            elif _win.get_last_error() != ERROR_SERVICE_NOT_ACTIVE:
                raise _fail("Stopping the service")
            if not api.dll.DeleteService(svc):
                if _win.get_last_error() != ERROR_SERVICE_MARKED_FOR_DELETE:
                    raise _fail("Removing the service")
            log(f"service '{name}' removed (the data directory is kept)")
        finally:
            api.dll.CloseServiceHandle(svc)
    finally:
        api.dll.CloseServiceHandle(scm)
    # best effort: drop the exe this service's install copied into %ProgramFiles%\ser2net
    # (the running copy is unlocked once the service process has exited)
    dest = installed_exe_path(name)
    if os.path.isfile(dest):
        for _ in range(10):
            try:
                os.remove(dest)
                log(f"removed {dest}")
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(0.3)


def run(name: str, main: Callable[[], int], stop: Callable[[], None]) -> int:
    """For --service: hand the process to the SCM dispatcher (blocks until the service
    has stopped). ``main`` runs the server in the service thread; ``stop`` is called
    (from the SCM's thread) on a stop or shutdown request."""
    api = _advapi()
    name = check_name(name)
    redirect_missing_stdio()
    result = {"code": 0}
    handle = {"h": None}
    status = api.SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, SERVICE_START_PENDING, 0, 0, 0, 0, 0)

    def report(state: int, code: int = 0, wait_hint: int = 0) -> None:
        status.dwCurrentState = state
        status.dwControlsAccepted = (0 if state == SERVICE_START_PENDING
                                     else SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)
        status.dwWin32ExitCode = ERROR_SERVICE_SPECIFIC_ERROR if code else NO_ERROR
        status.dwServiceSpecificExitCode = code
        status.dwWaitHint = wait_hint
        status.dwCheckPoint = 0 if state in (SERVICE_RUNNING, SERVICE_STOPPED) else status.dwCheckPoint + 1
        if handle["h"]:
            api.dll.SetServiceStatus(handle["h"], ctypes.byref(status))

    def on_control(control, _event, _data, _ctx):
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            report(SERVICE_STOP_PENDING, wait_hint=30000)
            stop()
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    handler = api.handler_fn(on_control)

    def service_main(_argc, _argv):
        handle["h"] = api.dll.RegisterServiceCtrlHandlerExW(name, handler, None)
        report(SERVICE_START_PENDING, wait_hint=30000)
        report(SERVICE_RUNNING)
        code = 1
        try:
            code = int(main() or 0)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except BaseException:  # whatever happens, the SCM must still be told STOPPED
            import traceback
            traceback.print_exc()
        result["code"] = code
        report(SERVICE_STOPPED, code=code)

    entry = api.main_fn(service_main)
    table = (api.SERVICE_TABLE_ENTRYW * 2)(api.SERVICE_TABLE_ENTRYW(name, entry),
                                           api.SERVICE_TABLE_ENTRYW(None, api.main_fn()))
    if not api.dll.StartServiceCtrlDispatcherW(table):
        if _win.get_last_error() == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise ServiceError("--service is used by the Windows Service Control Manager. Run "
                               "ser2net without it, or install the service with --install-service.")
        raise _fail("Connecting to the Service Control Manager")
    return result["code"]


def redirect_missing_stdio() -> None:
    """A service has no console: print() / logging to a missing stdout must not fail."""
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
