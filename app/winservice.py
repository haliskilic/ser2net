"""Run ser2net as a Windows service, with the Windows API itself (ctypes) — neither
pywin32 nor a wrapper program (NSSM, Shawl) is needed.

    ser2net.exe --install-service [--data-dir DIR] [--service-name NAME] [--reconfigure]
    ser2net.exe --uninstall-service [--service-name NAME]

``--install-service`` registers ser2net to start at boot as LocalSystem, restart 5 s
after it dies or exits with an error, and starts it. A standalone exe copies itself to
%ProgramFiles%\\ser2net first and runs from there; a new service keeps its data in
%ProgramData%\\ser2net. Both folders are left to SYSTEM and Administrators alone: nobody
else may change what the service runs. A source checkout (python + ser2net.py) can't
move, so it is refused where another account could change it. Run again on an existing
service, it upgrades it in place: stops it, keeps its data directory, puts the current
build in place, starts it — and on a failure puts the previous version back.

The Service Control Manager then runs ``ser2net.exe --service --data-dir ...``: ``run()``
hands the process to the SCM dispatcher, runs the normal server in the service thread and
stops it cleanly on a stop request or at shutdown. Windows only; everything is imported
lazily so the module loads (and its pure decision helpers are testable) on any OS.
"""
from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

from . import clock

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
SERVICE_QUERY_CONFIG = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SC_STATUS_PROCESS_INFO = 0
SYNCHRONIZE = 0x00100000
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


def split_command_line(cmd: str) -> list[str]:
    """Split a command line the way CommandLineToArgvW does (the inverse of _quote), so an
    existing service's registered --data-dir / --config can be read back. argv[0] is a
    program path: quotes delimit it, backslashes are literal."""
    args: list[str] = []
    i, n = 0, len(cmd)
    while i < n and cmd[i] in " \t":
        i += 1
    if i >= n:
        return args
    if cmd[i] == '"':
        j = cmd.find('"', i + 1)
        j = n if j < 0 else j
        args.append(cmd[i + 1:j])
        i = j + 1
    else:
        j = i
        while j < n and cmd[j] not in " \t":
            j += 1
        args.append(cmd[i:j])
        i = j
    while True:
        while i < n and cmd[i] in " \t":
            i += 1
        if i >= n:
            return args
        buf: list[str] = []
        quoted = False
        while i < n:
            ch = cmd[i]
            if ch == "\\":
                j = i
                while j < n and cmd[j] == "\\":
                    j += 1
                if j < n and cmd[j] == '"':  # 2n (+1) backslashes before a quote
                    buf.append("\\" * ((j - i) // 2))
                    if (j - i) % 2:
                        buf.append('"')
                        j += 1
                else:
                    buf.append("\\" * (j - i))
                i = j
            elif ch == '"':
                quoted = not quoted
                i += 1
            elif ch in " \t" and not quoted:
                break
            else:
                buf.append(ch)
                i += 1
        args.append("".join(buf))


def parse_service_command(cmd: str) -> Optional[dict]:
    """What a registered command line says (executable, script, --service-name, --data-dir,
    --config), or None when it isn't one ser2net installed (no --service)."""
    argv = split_command_line(cmd or "")
    if "--service" not in argv:
        return None

    def value(flag: str) -> Optional[str]:
        i = argv.index(flag) if flag in argv else -1
        return argv[i + 1] if 0 <= i < len(argv) - 1 else None

    script = argv[1] if len(argv) > 1 and argv[1].lower().endswith(".py") else None
    return {"executable": argv[0], "script": script, "service_name": value("--service-name"),
            "data_dir": value("--data-dir"), "config": value("--config")}


def choose_install_paths(data_dir: Optional[str], config: Optional[str],
                         registered: Optional[dict], new_default: str,
                         exists: Callable[[str], bool] = os.path.exists):
    """(data_dir, config, notes) for --install-service:
      * what the user passed (--data-dir / --config);
      * else, for an existing service, the ones it already uses — an upgrade never starts
        over in another directory with an empty configuration;
      * else (a new service) new_default.
    Moving an existing service to a --data-dir without a config.json while its current
    one has one is refused: copy the folder there first."""
    notes = []
    old_dir = registered.get("data_dir") if registered else None
    old_cfg = registered.get("config") if registered else None
    if registered is not None and not old_dir:
        raise ServiceError("The existing service's command line names no --data-dir; give "
                           "--data-dir to say which data directory it should use.")
    if config:
        config = os.path.abspath(config)
    if data_dir is None:
        if old_dir:
            data_dir, config = old_dir, config or old_cfg
            notes.append(f"keeping the service's data directory {data_dir}")
        else:
            data_dir = new_default
    else:
        data_dir = os.path.abspath(data_dir)
        if old_dir and os.path.normcase(os.path.abspath(old_dir)) != os.path.normcase(data_dir):
            if (not exists(config or os.path.join(data_dir, "config.json"))
                    and exists(old_cfg or os.path.join(old_dir, "config.json"))):
                raise ServiceError(
                    f"The service keeps its configuration in {old_dir}; {data_dir} has none. "
                    f"Copy {old_dir} to {data_dir} first (keep the old folder until the service "
                    f"works), or leave out --data-dir to keep using {old_dir}.")
            notes.append(f"moving the service from {old_dir} to {data_dir}")
    return data_dir, config, notes


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
# on a folder above the data directory: what lets someone rename it (and so swap the
# data directory for one of their own)
RENAME_RIGHTS = _DELETE | FILE_DELETE_CHILD | WRITE_DAC | WRITE_OWNER | GENERIC_ALL
# ACE flags (winnt.h)
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10

SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"
USERS_SID = "S-1-5-32-545"
EVERYONE_SID = "S-1-1-0"
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
OWNER_RIGHTS_SID = "S-1-3-4"
# The only accounts that may be able to change what a LocalSystem service runs. Any other
# account — an administrator's own user account too, whose programs normally run without
# elevation — counts as one that could.
SAFE_SIDS = frozenset({SYSTEM_SID, ADMINS_SID, TRUSTED_INSTALLER_SID})


def _is_safe_sid(sid: str) -> bool:
    return sid in SAFE_SIDS


def _is_placeholder_sid(sid: str) -> bool:
    """CREATOR OWNER / CREATOR GROUP / OWNER RIGHTS (S-1-3-*): stand-ins that are replaced
    by the creating account (an entry of its own) or by the owner (checked as owner)."""
    return sid.startswith("S-1-3-")


def acl_offenders(aces, owner: Optional[str] = None, rights: int = DANGEROUS_RIGHTS) -> list[str]:
    """Pure decision table (any OS; the Windows caller reads the ACEs). Given a file/dir's
    access-control entries as (sid, access_mask, allow[, ace_flags]) and its owner, return
    the sorted SIDs of accounts other than SYSTEM / Administrators / TrustedInstaller that
    hold one of `rights` on it, honouring a deny entry for the same SID. Inherit-only
    entries are skipped (they apply only to what is created inside later), and so are the
    CREATOR OWNER-style stand-ins. The owner counts too: it can always rewrite the
    permissions. Empty means only administrators and the system can change it."""
    allow: dict[str, int] = {}
    deny: dict[str, int] = {}
    for ace in aces:
        sid, mask, is_allow = ace[0], int(ace[1]), ace[2]
        flags = ace[3] if len(ace) > 3 else 0
        if flags & INHERIT_ONLY_ACE or _is_placeholder_sid(sid):
            continue
        target = allow if is_allow else deny
        target[sid] = target.get(sid, 0) | mask
    out = {sid for sid, mask in allow.items()
           if not _is_safe_sid(sid) and mask & rights & ~deny.get(sid, 0)}
    if owner and not _is_safe_sid(owner) and not _is_placeholder_sid(owner):
        out.add(owner)
    return sorted(out)


ANY_ACCESS = 0xFFFFFFFF


def admin_only(owner: Optional[str], aces) -> bool:
    """Pure decision (any OS): only SYSTEM / Administrators / TrustedInstaller own the
    file or folder or have any access to it, as a service's data directory once
    --install-service has hardened it."""
    return not acl_offenders(aces, owner, rights=ANY_ACCESS)


def service_folder(owner: Optional[str], aces) -> bool:
    """Pure decision (any OS): a folder as --install-service leaves a service's data
    directory, or one inheriting from it: admin_only(), and no OWNER RIGHTS entry.
    lock_down_dir() leaves it as it is (its grant for the account running ser2net would
    let that account's unelevated programs change what the service runs). A folder made
    private to the account that created it — Python's mkdir(mode=0o700) gives SYSTEM,
    Administrators and OWNER RIGHTS — is that account's own, even when an elevated
    administrator made it and Administrators own it (Windows Server's default for
    them): lock_down_dir() gives it that account's entry, so the folder stays usable
    from its unelevated programs."""
    return admin_only(owner, aces) and not any(
        ace[0] == OWNER_RIGHTS_SID and ace[2] for ace in aces)


def foreign_owner(owner: Optional[str], me: Optional[str]) -> bool:
    """True for an owner that is neither SYSTEM / Administrators / TrustedInstaller nor the
    installing account: whatever it put in a data directory can't be taken on trust."""
    return bool(owner) and not _is_safe_sid(owner or "") and owner != me


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

    class QUERY_SERVICE_CONFIGW(ctypes.Structure):  # noqa: N801
        _fields_ = [("dwServiceType", w.DWORD), ("dwStartType", w.DWORD),
                    ("dwErrorControl", w.DWORD), ("lpBinaryPathName", w.LPWSTR),
                    ("lpLoadOrderGroup", w.LPWSTR), ("dwTagId", w.DWORD),
                    ("lpDependencies", w.LPWSTR), ("lpServiceStartName", w.LPWSTR),
                    ("lpDisplayName", w.LPWSTR)]

    class SERVICE_STATUS_PROCESS(ctypes.Structure):  # noqa: N801
        _fields_ = [("dwServiceType", w.DWORD), ("dwCurrentState", w.DWORD),
                    ("dwControlsAccepted", w.DWORD), ("dwWin32ExitCode", w.DWORD),
                    ("dwServiceSpecificExitCode", w.DWORD), ("dwCheckPoint", w.DWORD),
                    ("dwWaitHint", w.DWORD), ("dwProcessId", w.DWORD),
                    ("dwServiceFlags", w.DWORD)]

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
        "QueryServiceStatusEx": (w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                          ctypes.POINTER(w.DWORD)]),
        "QueryServiceConfigW": (w.BOOL, [w.HANDLE, ctypes.c_void_p, w.DWORD,
                                         ctypes.POINTER(w.DWORD)]),
        "DeleteService": (w.BOOL, [w.HANDLE]),
        "CloseServiceHandle": (w.BOOL, [w.HANDLE]),
        "StartServiceCtrlDispatcherW": (w.BOOL, [ctypes.POINTER(SERVICE_TABLE_ENTRYW)]),
        "RegisterServiceCtrlHandlerExW": (w.HANDLE, [w.LPCWSTR, handler_fn, w.LPVOID]),
        "SetServiceStatus": (w.BOOL, [w.HANDLE, ctypes.POINTER(SERVICE_STATUS)]),
    }
    for fname, (res, args) in sig.items():
        f = getattr(dll, fname)
        f.restype, f.argtypes = res, args
    kernel = _win.WinDLL("kernel32", use_last_error=True)
    for fname, (res, args) in {
        "OpenProcess": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
        "WaitForSingleObject": (w.DWORD, [w.HANDLE, w.DWORD]),
        "CloseHandle": (w.BOOL, [w.HANDLE]),
    }.items():
        f = getattr(kernel, fname)
        f.restype, f.argtypes = res, args

    _api = SimpleNamespace(dll=dll, kernel=kernel, SERVICE_STATUS=SERVICE_STATUS,
                           SC_ACTION=SC_ACTION,
                           SERVICE_FAILURE_ACTIONSW=SERVICE_FAILURE_ACTIONSW,
                           SERVICE_FAILURE_ACTIONS_FLAG=SERVICE_FAILURE_ACTIONS_FLAG,
                           SERVICE_DESCRIPTIONW=SERVICE_DESCRIPTIONW,
                           QUERY_SERVICE_CONFIGW=QUERY_SERVICE_CONFIGW,
                           SERVICE_STATUS_PROCESS=SERVICE_STATUS_PROCESS, main_fn=main_fn,
                           handler_fn=handler_fn, SERVICE_TABLE_ENTRYW=SERVICE_TABLE_ENTRYW)
    return _api


_sec = None


def _secapi():
    """advapi32 / kernel32 security calls (lazy, Windows only)."""
    global _sec
    if _sec is not None:
        return _sec
    from ctypes import wintypes as w

    class ACL_SIZE_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD),
                    ("AclBytesFree", w.DWORD)]

    class ACE_HEADER(ctypes.Structure):  # noqa: N801
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", w.WORD)]

    class ALLOWED_ACE(ctypes.Structure):  # noqa: N801 (ACCESS_ALLOWED/DENIED_ACE share a layout)
        _fields_ = [("Header", ACE_HEADER), ("Mask", w.DWORD), ("SidStart", w.DWORD)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):  # noqa: N801
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", w.BOOL)]

    vp, pvp = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    advapi = _win.WinDLL("advapi32", use_last_error=True)
    kernel = _win.WinDLL("kernel32", use_last_error=True)
    for dll, sig in ((advapi, {
        "GetNamedSecurityInfoW": (w.DWORD, [w.LPCWSTR, ctypes.c_int, w.DWORD, pvp, pvp, pvp,
                                            pvp, pvp]),
        "GetAclInformation": (w.BOOL, [vp, vp, w.DWORD, ctypes.c_int]),
        "GetAce": (w.BOOL, [vp, w.DWORD, pvp]),
        "ConvertSidToStringSidW": (w.BOOL, [vp, ctypes.POINTER(w.LPWSTR)]),
        "ConvertStringSidToSidW": (w.BOOL, [w.LPCWSTR, pvp]),
        "LookupAccountSidW": (w.BOOL, [w.LPCWSTR, vp, w.LPWSTR, ctypes.POINTER(w.DWORD),
                                       w.LPWSTR, ctypes.POINTER(w.DWORD), vp]),
        "OpenProcessToken": (w.BOOL, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]),
        "GetTokenInformation": (w.BOOL, [w.HANDLE, ctypes.c_int, vp, w.DWORD,
                                         ctypes.POINTER(w.DWORD)]),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (w.BOOL, [w.LPCWSTR, w.DWORD,
                                                                          pvp, vp]),
    }), (kernel, {
        "LocalFree": (vp, [vp]),
        "GetCurrentProcess": (w.HANDLE, []),
        "CloseHandle": (w.BOOL, [w.HANDLE]),
        "CreateDirectoryW": (w.BOOL, [w.LPCWSTR, vp]),
    })):
        for fname, (res, args) in sig.items():
            f = getattr(dll, fname)
            f.restype, f.argtypes = res, args
    _sec = SimpleNamespace(advapi=advapi, kernel=kernel, ACL_SIZE_INFORMATION=ACL_SIZE_INFORMATION,
                           ACE_HEADER=ACE_HEADER, ALLOWED_ACE=ALLOWED_ACE,
                           SECURITY_ATTRIBUTES=SECURITY_ATTRIBUTES)
    return _sec


def _sid_string(s, psid) -> Optional[str]:
    from ctypes import wintypes as w

    text = w.LPWSTR()
    if not s.advapi.ConvertSidToStringSidW(psid, ctypes.byref(text)):
        return None
    try:
        return text.value or ""
    finally:
        s.kernel.LocalFree(text)


def read_security(path: str):
    """(owner SID, [(sid, access_mask, allow, ace_flags), ...]) of a file or folder, read
    through the Windows API; None when it can't be read (or not on Windows)."""
    if sys.platform != "win32" or not os.path.lexists(path):
        return None
    s = _secapi()
    SE_FILE_OBJECT, OWNER_INFO, DACL_INFO = 1, 0x1, 0x4
    powner, pdacl, psd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if s.advapi.GetNamedSecurityInfoW(path, SE_FILE_OBJECT, OWNER_INFO | DACL_INFO,
                                      ctypes.byref(powner), None, ctypes.byref(pdacl), None,
                                      ctypes.byref(psd)) != NO_ERROR:
        return None
    try:
        owner = _sid_string(s, powner) if powner else ""
        if owner is None:
            return None
        if not pdacl:  # a NULL DACL grants everyone everything
            return owner, [(EVERYONE_SID, GENERIC_ALL, True, 0)]
        info = s.ACL_SIZE_INFORMATION()
        if not s.advapi.GetAclInformation(pdacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            return None
        aces = []
        for i in range(info.AceCount):
            pace = ctypes.c_void_p()
            if not s.advapi.GetAce(pdacl, i, ctypes.byref(pace)):
                return None
            header = ctypes.cast(pace, ctypes.POINTER(s.ACE_HEADER)).contents
            # 0 allowed, 1 denied, 9 allowed-callback (same layout; counted as allowed)
            if header.AceType not in (0, 1, 9):
                continue
            ace = ctypes.cast(pace, ctypes.POINTER(s.ALLOWED_ACE)).contents
            sid = _sid_string(s, ctypes.c_void_p(pace.value + s.ALLOWED_ACE.SidStart.offset))
            if sid is None:
                return None
            aces.append((sid, ace.Mask, header.AceType != 1, header.AceFlags))
        return owner, aces
    finally:
        s.kernel.LocalFree(psd)


_names: dict[str, str] = {}


def sid_name(sid: str) -> str:
    """'DOMAIN\\name' for a SID string, else the SID itself."""
    if sid in _names or sys.platform != "win32":
        return _names.get(sid, sid)
    from ctypes import wintypes as w

    s = _secapi()
    psid = ctypes.c_void_p()
    _names[sid] = sid
    if s.advapi.ConvertStringSidToSidW(sid, ctypes.byref(psid)):
        try:
            name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            n, d, use = w.DWORD(256), w.DWORD(256), w.DWORD()
            if s.advapi.LookupAccountSidW(None, psid, name, ctypes.byref(n), domain,
                                          ctypes.byref(d), ctypes.byref(use)):
                _names[sid] = f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            s.kernel.LocalFree(psid)
    return _names[sid]


def writable_by_non_admins(path: str, rights: int = DANGEROUS_RIGHTS) -> Optional[list[str]]:
    """Accounts other than SYSTEM / Administrators / TrustedInstaller that can change `path`
    (resolved to names) — [] when none can, None when it can't be determined (an
    unreadable ACL, or not Windows). Decided by acl_offenders(); callers treat None as
    'not checked', never as a failure of the check itself."""
    sec = read_security(path)
    if sec is None:
        return None
    owner, aces = sec
    return [sid_name(sid) for sid in acl_offenders(aces, owner=owner, rights=rights)]


def _unsafe_locations(paths, log: Callable[[str], None]) -> list[str]:
    """Of `paths`, those another account can change, each as '<path> (<who>)'. A path
    whose ACL can't be read is noted via log and skipped."""
    out = []
    for p in paths:
        if not p or not os.path.lexists(p):
            continue
        who = writable_by_non_admins(p)
        if who is None:
            log(f"note: could not check who can modify {p}")
        elif who:
            out.append(f"{p} ({', '.join(who)})")
    return out


def _current_user_sid() -> Optional[str]:
    """The SID of the account running this process (the installing administrator)."""
    from ctypes import wintypes as w

    s = _secapi()
    token = w.HANDLE()
    if not s.advapi.OpenProcessToken(s.kernel.GetCurrentProcess(), 0x0008,  # TOKEN_QUERY
                                     ctypes.byref(token)):
        return None
    try:
        size = w.DWORD()
        s.advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # 1 = TokenUser
        buf = ctypes.create_string_buffer(size.value)
        if not s.advapi.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            return None
        return _sid_string(s, ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents)
    finally:
        s.kernel.CloseHandle(token)


def _icacls(*args: str) -> int:
    """Run icacls by its full System32 path (an unqualified name is searched in the
    program's own folder first)."""
    import subprocess

    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    try:
        return subprocess.run([os.path.join(root, "System32", "icacls.exe"), *args],
                              capture_output=True, timeout=120).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


# SYSTEM and Administrators only, not inherited: what a new service data directory gets
# from the moment it exists (no window in which ProgramData's "Users may create files"
# applies to it)
_ADMIN_ONLY_SDDL = f"D:P(A;OICI;FA;;;{SYSTEM_SID})(A;OICI;FA;;;{ADMINS_SID})"


def _mkdir_admin_only(path: str) -> None:
    """Create `path` and any missing parent with an admin-only, non-inherited ACL."""
    missing = []
    p = os.path.abspath(path)
    while not os.path.exists(p):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if not missing:
        return
    s = _secapi()
    psd = ctypes.c_void_p()
    if not s.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            _ADMIN_ONLY_SDDL, 1, ctypes.byref(psd), None):
        raise _fail("Preparing the data directory's permissions")
    try:
        sa = s.SECURITY_ATTRIBUTES(ctypes.sizeof(s.SECURITY_ATTRIBUTES), psd, False)
        for d in reversed(missing):
            if not s.kernel.CreateDirectoryW(d, ctypes.byref(sa)) and not os.path.isdir(d):
                raise _fail(f"Creating {d}")
    finally:
        s.kernel.LocalFree(psd)


def _harden(path: str, users_read: bool) -> None:
    """Leave the folder `path` owned by Administrators, with SYSTEM and Administrators as
    the only accounts that may change it or anything in it (plus read for Users when
    users_read) — no entry for the installing account, whose unelevated programs would
    otherwise keep write access. Everything inside only inherits from it."""
    keep = {SYSTEM_SID, ADMINS_SID} | ({USERS_SID} if users_read else set())
    args = [path, "/inheritance:r", "/grant:r", f"*{SYSTEM_SID}:(OI)(CI)F",
            "/grant:r", f"*{ADMINS_SID}:(OI)(CI)F"]
    if users_read:
        args += ["/grant:r", f"*{USERS_SID}:(OI)(CI)RX"]
    _icacls(*args, "/C", "/Q")
    sec = read_security(path)
    for sid in sorted({sid for sid, _mask, _allow, flags in (sec[1] if sec else [])
                       if not flags & INHERITED_ACE and sid not in keep}):
        _icacls(path, "/remove", f"*{sid}", "/C", "/Q")  # e.g. a per-user grant from earlier
    _icacls(path, "/setowner", f"*{ADMINS_SID}", "/T", "/C", "/Q")
    if os.listdir(path):
        _icacls(os.path.join(path, "*"), "/reset", "/T", "/C", "/Q")


def _harden_file(path: str) -> None:
    """_harden() for a single file (a --config outside the data directory)."""
    _icacls(path, "/inheritance:r", "/grant:r", f"*{SYSTEM_SID}:F", "/grant:r",
            f"*{ADMINS_SID}:F", "/C", "/Q")
    sec = read_security(path)
    for sid in sorted({sid for sid, _mask, _allow, flags in (sec[1] if sec else [])
                       if not flags & INHERITED_ACE and sid not in (SYSTEM_SID, ADMINS_SID)}):
        _icacls(path, "/remove", f"*{sid}", "/C", "/Q")
    _icacls(path, "/setowner", f"*{ADMINS_SID}", "/C", "/Q")


def _inside(path: str, folder: str) -> bool:
    p, f = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def _require_protected(paths, what: str) -> None:
    """Refuse (ServiceError) when another account can still change one of `paths`."""
    problems = []
    for p in paths:
        if not os.path.lexists(p):
            continue
        who = writable_by_non_admins(p)
        if who is None:
            problems.append(f"{p} (its permissions can't be read)")
        elif who:
            problems.append(f"{p} ({', '.join(who)})")
    if problems:
        raise ServiceError(f"{what} could not be restricted to SYSTEM and Administrators: "
                           + "; ".join(problems) + ". Take it over and run --install-service "
                           f'again: takeown /f "{paths[0]}" /r /d y, then icacls "{paths[0]}" '
                           f"/setowner *{ADMINS_SID} /t /c")


def _exposed_ancestors(path: str) -> list[str]:
    """The folders above `path` that another account can rename or re-permission (and so
    swap the directory for one of its own), each as '<folder> (<who>)'."""
    out = []
    p = os.path.dirname(os.path.abspath(path))
    while True:
        who = writable_by_non_admins(p, rights=RENAME_RIGHTS)
        if who:
            out.append(f"{p} ({', '.join(who)})")
        parent = os.path.dirname(p)
        if parent == p:
            return out
        p = parent


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


def _retry(action: Callable[[], Any], what: str, timeout: float = 30.0) -> Any:
    """Run a file operation, retrying while the file is in use (a just-stopped service's
    process, an antivirus scan); then a one-line ServiceError instead of a traceback."""
    end = time.monotonic() + timeout
    while True:
        try:
            return action()
        except OSError as e:
            if time.monotonic() > end or not isinstance(e, PermissionError):
                raise ServiceError(f"{what}: {e.strerror or e}") from e
            time.sleep(0.5)


def _place_exe(dest: str, log: Callable[[str], None]) -> Optional[str]:
    """Copy the running frozen exe to the admin-only install path. The file it replaces
    is kept as <dest>.old (returned) until the upgrade has succeeded, for the rollback."""
    import shutil

    src = os.path.abspath(sys.executable)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    new, backup = dest + ".new", None
    _retry(lambda: shutil.copyfile(src, new), f"Copying the program to {new}")
    if os.path.exists(dest):
        backup = dest + ".old"
        if os.path.exists(backup):
            _retry(lambda: os.remove(backup), f"Removing {backup}")
        _retry(lambda: os.replace(dest, backup), f"Moving the previous {dest} aside")
    _retry(lambda: os.replace(new, dest), f"Putting the program in place at {dest}")
    log(f"installed the program to {dest}")
    return backup


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


def _program_paths(frozen: bool) -> list[str]:
    """What the service runs, so what nobody but administrators may change: a frozen
    build's exe and its folder (DLLs are looked up there first); for a source checkout the
    interpreter and its library folders, the checkout, its scripts, app/ and lib/."""
    if frozen:
        exe = os.path.abspath(sys.executable)
        return [exe, os.path.dirname(exe)]
    root = os.path.dirname(os.path.abspath(sys.argv[0]))
    base = sys.base_prefix
    candidates = [sys.executable, os.path.dirname(os.path.abspath(sys.executable)), sys.prefix,
                  base, os.path.join(base, "Lib"), os.path.join(base, "DLLs"), root,
                  os.path.join(root, "ser2net.py"), os.path.join(root, "bootstrap.py"),
                  os.path.join(root, "app"), os.path.join(root, "lib")]
    seen, out = set(), []
    for p in candidates:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen and os.path.lexists(p):
            seen.add(key)
            out.append(p)
    return out


def _guard_location(frozen: bool, allow_unsafe_location: bool,
                    log: Callable[[str], None]) -> None:
    """For a source checkout, refuse to install when an account other than SYSTEM and
    Administrators can change what the service runs (the interpreter, the checkout, app/,
    lib/): the LocalSystem service would run that account's code. A frozen build needs no
    check here: install() copies it to an admin-only folder."""
    if frozen:
        return
    unsafe = _unsafe_locations(_program_paths(False), log)
    if unsafe and not allow_unsafe_location:
        raise ServiceError(
            "accounts other than SYSTEM and Administrators can change what this service "
            "would run as LocalSystem (an administrator's own account counts: its programs "
            "usually run unelevated):\n  " + "\n  ".join(unsafe) + "\nPut the checkout and "
            "Python in an admin-only folder (e.g. under %ProgramFiles%); for a folder you "
            f'have: icacls "<folder>" /setowner *{ADMINS_SID} /t /c, then icacls "<folder>" '
            "/reset /t /c. If this location is trusted, pass --allow-unsafe-location. The "
            "standalone exe installs itself to an admin-only folder and needs neither.")
    if unsafe:
        log("WARNING: installing although accounts other than SYSTEM and Administrators can "
            "change:\n  " + "\n  ".join(unsafe))


def location_warning() -> Optional[str]:
    """A WARNING line for the service log when an account other than SYSTEM and
    Administrators can change the program this (LocalSystem) service runs — else None.
    Returns None off Windows."""
    unsafe = _unsafe_locations(_program_paths(getattr(sys, "frozen", False)), lambda *_: None)
    if not unsafe:
        return None
    return ("WARNING: accounts other than SYSTEM and Administrators can change what this "
            "service runs, so they could run code as LocalSystem: " + "; ".join(unsafe)
            + " — reinstall the standalone exe (it installs itself to an admin-only folder) "
            "or move the checkout under %ProgramFiles%")


def _check_adoptable(data_dir: str, config_path: str) -> None:
    """A new service doesn't take over a data directory (or the shared %ProgramData%\\ser2net
    above a named one, or a --config file elsewhere) that holds anything owned by another
    account: a standard user can create folders in %ProgramData% and leave a config.json
    of their own there."""
    me = _current_user_sid()
    base = os.path.join(program_data(), INSTALL_SUBDIR)
    roots = [data_dir]
    if os.path.normcase(os.path.dirname(os.path.abspath(data_dir))) == os.path.normcase(base):
        roots.append(base)
    if not _inside(config_path, data_dir):
        roots.append(config_path)
    for root in roots:
        if not os.path.lexists(root):
            continue
        entries = [root] if root != data_dir else [root] + [
            os.path.join(d, f) for d, dirs, files in os.walk(root) for f in dirs + files]
        for p in entries:
            sec = read_security(p)
            if sec is not None and foreign_owner(sec[0], me):
                raise ServiceError(
                    f"{p} belongs to {sid_name(sec[0])}, not to an administrator, so ser2net "
                    f"won't take over {data_dir}: someone else may have put a configuration "
                    f"there. Check it; if it is yours, take it over (takeown /f \"{root}\" /r "
                    f"/d y, then icacls \"{root}\" /setowner *{ADMINS_SID} /t /c), otherwise "
                    "delete it; then install again.")


def _wait_lock_free(data_dir: str, timeout: float) -> None:
    """--install-service only tests the data directory's lock (app.datalock): the service
    it starts must be the one that holds it."""
    from . import datalock

    end = time.monotonic() + timeout
    while True:
        pid = datalock.holder(data_dir)
        if pid is None:
            return
        if time.monotonic() >= end:
            who = f"process {pid}" if pid else "another process"
            raise ServiceError(f"{who} is running with the data directory {data_dir}. Stop it "
                               "first, or install with a different --data-dir.")
        time.sleep(0.5)


def _query_registration(api, svc) -> SimpleNamespace:
    from ctypes import wintypes as w

    needed = w.DWORD()
    api.dll.QueryServiceConfigW(svc, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(max(needed.value, ctypes.sizeof(api.QUERY_SERVICE_CONFIGW)))
    if not api.dll.QueryServiceConfigW(svc, buf, len(buf), ctypes.byref(needed)):
        raise _fail("Reading the service's configuration")
    q = ctypes.cast(buf, ctypes.POINTER(api.QUERY_SERVICE_CONFIGW)).contents
    return SimpleNamespace(service_type=q.dwServiceType, start_type=q.dwStartType,
                           error_control=q.dwErrorControl, binary_path=q.lpBinaryPathName or "",
                           display_name=q.lpDisplayName or "")


def _service_status(api, svc) -> tuple[int, int]:
    """(state, process id) of a service."""
    from ctypes import wintypes as w

    ssp, needed = api.SERVICE_STATUS_PROCESS(), w.DWORD()
    if not api.dll.QueryServiceStatusEx(svc, SC_STATUS_PROCESS_INFO, ctypes.byref(ssp),
                                        ctypes.sizeof(ssp), ctypes.byref(needed)):
        raise _fail("QueryServiceStatusEx")
    return ssp.dwCurrentState, ssp.dwProcessId


def _stop_and_wait(api, svc, name: str, log: Callable[[str], None]) -> bool:
    """Stop a service and wait until its process has exited (only then are its exe and its
    data directory's lock free: a one-file exe's launcher still cleans up after STOPPED).
    True when it was running."""
    state, pid = _service_status(api, svc)
    if state == SERVICE_STOPPED:
        return False
    st = api.SERVICE_STATUS()
    if not api.dll.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st)):
        if _win.get_last_error() == ERROR_SERVICE_NOT_ACTIVE:
            return False
        raise _fail("Stopping the service")
    if _wait_state(api, svc, SERVICE_STOPPED, 60) != SERVICE_STOPPED:
        raise ServiceError(f"The service '{name}' did not stop within 60 s.")
    if pid:
        h = api.kernel.OpenProcess(SYNCHRONIZE, False, pid)
        if h:  # (gone already otherwise)
            try:
                api.kernel.WaitForSingleObject(h, 60000)
            finally:
                api.kernel.CloseHandle(h)
    log(f"service '{name}' stopped")
    return True


def install(name: str, data_dir: Optional[str] = None, config: Optional[str] = None, *,
            prepare: Callable[[str], Any], default_data_dir: str, start: bool = True,
            log: Callable[[str], None] = print, frozen: Optional[bool] = None,
            allow_unsafe_location: bool = False) -> Any:
    """Install the service — or, when it exists, upgrade it in place: stop it, keep its
    data directory (unless --data-dir names another), put the current build in place and
    start it again; on a failure after the stop the previous version is put back and
    started. `prepare(config_path)` loads (on a first run: creates) the configuration
    while no instance runs on it, and its result is returned."""
    api = _advapi()
    name = check_name(name)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    # first, so a prompt that isn't elevated is told so before anything is written
    scm = api.dll.OpenSCManagerW(None, None, SC_MANAGER_CONNECT | SC_MANAGER_CREATE_SERVICE)
    if not scm:
        raise _fail("Opening the Service Control Manager")
    svc = None
    try:
        # (a NULL handle comes back as None or 0: one value for every check below)
        svc = api.dll.OpenServiceW(scm, name, SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS
                                   | SERVICE_START | SERVICE_STOP | SERVICE_CHANGE_CONFIG
                                   | DELETE) or None
        previous = registered = None
        if svc is not None:
            previous = _query_registration(api, svc)
            registered = parse_service_command(previous.binary_path)
            if registered is None:
                raise ServiceError(f"A service named '{name}' exists but doesn't run ser2net "
                                   f"({previous.binary_path}). Pick another --service-name.")
        elif _win.get_last_error() != ERROR_SERVICE_DOES_NOT_EXIST:
            raise _fail("Opening the service")
        new_default = default_service_data_dir(name) if frozen else default_data_dir
        data_dir, config, notes = choose_install_paths(data_dir, config, registered, new_default)
        for note in notes:
            log(note)
        _guard_location(frozen, allow_unsafe_location, log)  # before anything is stopped
        if svc is None:
            _check_adoptable(data_dir, config or os.path.join(data_dir, "config.json"))
            _wait_lock_free(data_dir, 0)
        return _install_steps(api, scm, svc, previous, registered, name, data_dir, config,
                              prepare, frozen, start, log)
    finally:
        if svc:
            api.dll.CloseServiceHandle(svc)
        api.dll.CloseServiceHandle(scm)


def _install_steps(api, scm, svc, previous, registered, name, data_dir, config, prepare,
                   frozen, start, log):
    done = SimpleNamespace(stopped=False, placed=None, backup=None, reconfigured=False,
                           created=None)
    config_path = config or os.path.join(data_dir, "config.json")
    try:
        if svc:
            done.stopped = _stop_and_wait(api, svc, name, log)
            _wait_lock_free(data_dir, 30)
        _mkdir_admin_only(data_dir)
        loaded = prepare(config_path)
        # after prepare: loading the config may have added a grant for the installing
        # account (lock_down_dir, on a folder that was not a service folder yet)
        _harden(data_dir, users_read=False)
        _require_protected([data_dir, os.path.join(data_dir, "config.json")], "The data directory")
        if not _inside(config_path, data_dir):
            _harden_file(config_path)
            _require_protected([config_path], "The configuration file")
        exe = None
        if frozen:
            exe = installed_exe_path(name)
            if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(exe):
                done.backup = _place_exe(exe, log)
                done.placed = exe
            _harden(install_dir(), users_read=True)
            _require_protected([install_dir(), exe], "The program folder")
        cmd = service_command(name, data_dir, config, frozen=frozen, executable=exe)
        if svc:
            if not api.dll.ChangeServiceConfigW(svc, SERVICE_WIN32_OWN_PROCESS,
                                                SERVICE_AUTO_START, SERVICE_ERROR_NORMAL, cmd,
                                                None, None, None, None, None, display_name(name)):
                raise _fail("Updating the service configuration")
            done.reconfigured = True
            _service_extras(api, svc, name, data_dir)
            log(f"service '{name}' upgraded in place\n  command: {cmd}")
            target = svc
        else:
            target = api.dll.CreateServiceW(
                scm, name, display_name(name),
                SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP | SERVICE_CHANGE_CONFIG | DELETE,
                SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
                cmd, None, None, None, None, None)  # LocalSystem
            if not target:
                raise _fail("Creating the service")
            done.created = target
            _service_extras(api, target, name, data_dir)
            log(f"service '{name}' installed: starts at boot, restarts {RESTART_DELAY_MS // 1000} s "
                f"after a failure\n  command: {cmd}")
        if start:
            _start_and_wait(api, target, name, data_dir, log)
    except BaseException as e:
        if svc is None and done.created is not None:
            # a new service that doesn't start stays registered, as before (see all.log)
            if isinstance(e, (ServiceError, SystemExit, KeyboardInterrupt)):
                raise
            raise ServiceError(f"{type(e).__name__}: {e}") from e
        note = _roll_back(api, svc, previous, done, name)
        if isinstance(e, (SystemExit, KeyboardInterrupt)):
            if note:
                log(note)
            raise
        message = str(e) if isinstance(e, ServiceError) else f"{type(e).__name__}: {e}"
        raise ServiceError(message + (f" ({note})" if note else "")) from e
    finally:
        if done.created is not None:
            api.dll.CloseServiceHandle(done.created)
    if done.backup:
        with contextlib.suppress(OSError):
            os.remove(done.backup)
    if registered and exe and os.path.normcase(registered["executable"]) != os.path.normcase(exe):
        log(f"note: the service no longer runs {registered['executable']}; you can delete it")
    exposed = _exposed_ancestors(data_dir)
    if not _inside(config_path, data_dir):
        exposed += [a for a in _exposed_ancestors(config_path) if a not in exposed]
    if exposed:
        log("WARNING: other accounts can rename or re-permission a folder above the data "
            f"directory, and so swap it for one of their own: {'; '.join(exposed)}. Move the "
            f"data to {default_service_data_dir(name)} (stop the service, copy the folder, then "
            "--install-service --data-dir <new folder>; keep the old one until it works).")
    return loaded


def _roll_back(api, svc, previous, done, name: str) -> str:
    """Undo a failed (re)install as far as it got; returns what happened, for the message."""
    try:
        if svc is None:  # a new service, never created: drop the program copy
            if done.placed and done.backup is None:
                with contextlib.suppress(OSError):
                    os.remove(done.placed)
            return ""
        if done.reconfigured:
            api.dll.ChangeServiceConfigW(svc, previous.service_type, previous.start_type,
                                         previous.error_control, previous.binary_path, None,
                                         None, None, None, None, previous.display_name)
        if done.placed:
            _stop_and_wait(api, svc, name, lambda *_: None)  # the new build may have started
            if done.backup:
                _retry(lambda: os.replace(done.backup, done.placed),
                       f"Putting the previous {done.placed} back")
            else:
                with contextlib.suppress(OSError):
                    os.remove(done.placed)
        if not done.stopped:
            return "the previous version was left as it was" if done.placed or done.reconfigured else ""
        if api.dll.StartServiceW(svc, 0, None) and \
                _wait_state(api, svc, SERVICE_RUNNING) == SERVICE_RUNNING:
            return "the previous version is running again"
        return "the previous version was put back but did not start; see its all.log"
    except Exception as e:  # report it; the first failure stays the error
        return f"putting the previous version back failed too: {e}; check it with sc qc {name}"


START_TIMEOUT_S = 90.0  # how long --install-service waits for the service to be running


def _wait_started(api, svc, timeout: float = START_TIMEOUT_S) -> tuple[int, int]:
    """Wait while a service starts: (its state then, its exit code). It is RUNNING once
    its admin UI listens (run), STOPPED when the start failed, still START_PENDING after
    ``timeout``."""
    st = api.SERVICE_STATUS()
    end = time.monotonic() + timeout
    while True:
        if not api.dll.QueryServiceStatus(svc, ctypes.byref(st)):
            raise _fail("QueryServiceStatus")
        if st.dwCurrentState in (SERVICE_RUNNING, SERVICE_STOPPED) or time.monotonic() > end:
            code = (st.dwServiceSpecificExitCode
                    if st.dwWin32ExitCode == ERROR_SERVICE_SPECIFIC_ERROR else st.dwWin32ExitCode)
            return st.dwCurrentState, code
        time.sleep(0.3)


def _start_and_wait(api, svc, name: str, data_dir: str, log: Callable[[str], None]) -> None:
    if not api.dll.StartServiceW(svc, 0, None):
        raise _fail("Starting the service")
    state, code = _wait_started(api, svc)
    if state != SERVICE_RUNNING:
        logs = (f"see {os.path.join(data_dir, 'all.log')} and {SERVICE_LOG} there, or the "
                "Application event log")
        if state == SERVICE_STOPPED:
            raise ServiceError(f"The service did not reach RUNNING: it stopped while starting "
                               f"(exit code {code}); {logs}.")
        raise ServiceError(f"The service did not reach RUNNING within {START_TIMEOUT_S:g} s "
                           f"(state {state}); {logs}.")
    log(f"service '{name}' is running")


def kept_data_note(name: str, data_dir: Optional[str]) -> str:
    """What --uninstall-service says it leaves: the data directory and what is in it."""
    where = f" {data_dir}" if data_dir else ""
    return (f"service '{name}' removed. Its data directory{where} is kept, with config.json "
            "(the accounts and the stored secrets), backups/, all.log, audit.log, logs/, "
            "traces/ and tls/: delete that folder to remove them too")
START_WAIT_HINT_MS = 30000  # what a START_PENDING report tells the SCM to allow for
START_PROGRESS_S = 5.0      # how often a start under way is reported again


class StartProgress:
    """While the server starts, ``report`` START_PENDING again every ``every`` seconds
    (run()'s report() counts the checkpoint up), so the SCM doesn't take a slow start
    (many serial ports to open) for a hung one. start() when the start begins, finish()
    once it is over (ready, or failed); finish() is True the first time only."""

    def __init__(self, report: Callable[[], None], every: Optional[float] = None) -> None:
        self._report = report
        self._every = START_PROGRESS_S if every is None else every
        self._done = threading.Event()
        self._finished = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._finished:
                return
            self._thread = threading.Thread(target=self._run, name="service-start-progress",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._done.wait(self._every):
            self._report()

    def finish(self) -> bool:
        with self._lock:
            first, self._finished = not self._finished, True
            thread = self._thread
        self._done.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._every + 1)
        return first


def uninstall(name: str, log: Callable[[str], None] = print) -> None:
    api = _advapi()
    name = check_name(name)
    scm = api.dll.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        raise _fail("Opening the Service Control Manager")
    try:
        svc = api.dll.OpenServiceW(scm, name, SERVICE_QUERY_CONFIG | SERVICE_QUERY_STATUS
                                   | SERVICE_STOP | DELETE)
        if not svc:
            if _win.get_last_error() == ERROR_SERVICE_DOES_NOT_EXIST:
                raise ServiceError(f"There is no service named '{name}'.")
            raise _fail("Opening the service")
        try:
            try:  # where its data is, for the last message (best effort)
                registered = parse_service_command(_query_registration(api, svc).binary_path)
            except ServiceError:
                registered = None
            data_dir = registered.get("data_dir") if registered else None
            _stop_and_wait(api, svc, name, log)  # its process gone: the exe is free below
            if not api.dll.DeleteService(svc):
                if _win.get_last_error() != ERROR_SERVICE_MARKED_FOR_DELETE:
                    raise _fail("Removing the service")
            log(kept_data_note(name, data_dir))
        finally:
            api.dll.CloseServiceHandle(svc)
    finally:
        api.dll.CloseServiceHandle(scm)
    # drop the copy this service's install put into %ProgramFiles%\ser2net (and what an
    # interrupted upgrade left next to it); best effort
    dest = installed_exe_path(name)
    for path in (dest, dest + ".old", dest + ".new"):
        if os.path.lexists(path):
            try:
                _retry(functools.partial(os.remove, path), f"Removing {path}", timeout=10)
                log(f"removed {path}")
            except ServiceError as e:
                log(f"note: {e}")
    with contextlib.suppress(OSError):
        os.rmdir(install_dir())  # only when no other service's copy is left in it


STOP_WAIT_HINT_MS = 30000  # what a STOP_PENDING report tells the SCM to allow for
STOP_PROGRESS_S = 5.0      # how often a stop under way is reported again


class StopProgress:
    """While a stop is under way, ``report`` it again every ``every`` seconds (STOP_PENDING
    with the next checkpoint, run()'s report() counts it up): one report with a 30 s
    hint made the SCM, `sc query` and Services treat a stop that took longer as a hung
    service. start() after the stop request, finish() once the server has stopped."""

    def __init__(self, report: Callable[[], None], every: float = STOP_PROGRESS_S) -> None:
        self._report = report
        self._every = every
        self._done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._done.is_set():
                return
            self._thread = threading.Thread(target=self._run, name="service-stop-progress",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._done.wait(self._every):
            self._report()

    def finish(self) -> None:
        self._done.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._every + 1)


def run(name: str, main: Callable[[], int], stop: Callable[[], None],
        log_path: Optional[str] = None,
        on_ready: Optional[Callable[[Callable[[], None]], None]] = None) -> int:
    """For --service: hand the process to the SCM dispatcher (blocks until the service
    has stopped). ``main`` runs the server in the service thread; ``stop`` is called
    (from the SCM's thread) on a stop or shutdown request. ``log_path``: where stderr goes
    (service_log). ``on_ready(callback)`` (runtime.when_ready) has the server call back
    once it is ready: the service is START_PENDING until then (StartProgress) and RUNNING
    from then on, so a start that fails is seen as one; without it, RUNNING is reported
    before main() runs."""
    api = _advapi()
    name = check_name(name)
    redirect_missing_stdio(log_path)
    result = {"code": 0}
    handle = {"h": None}
    status = api.SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, SERVICE_START_PENDING, 0, 0, 0, 0, 0)
    reporting = threading.Lock()  # the SCM's thread, the service's and StopProgress's

    def report(state: int, code: int = 0, wait_hint: int = 0) -> None:
        with reporting:
            status.dwCurrentState = state
            status.dwControlsAccepted = (0 if state == SERVICE_START_PENDING
                                         else SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)
            status.dwWin32ExitCode = ERROR_SERVICE_SPECIFIC_ERROR if code else NO_ERROR
            status.dwServiceSpecificExitCode = code
            status.dwWaitHint = wait_hint
            status.dwCheckPoint = (0 if state in (SERVICE_RUNNING, SERVICE_STOPPED)
                                   else status.dwCheckPoint + 1)
            if handle["h"]:
                api.dll.SetServiceStatus(handle["h"], ctypes.byref(status))

    progress = StopProgress(lambda: report(SERVICE_STOP_PENDING, wait_hint=STOP_WAIT_HINT_MS))

    def on_control(control, _event, _data, _ctx):
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            report(SERVICE_STOP_PENDING, wait_hint=STOP_WAIT_HINT_MS)
            progress.start()
            stop()
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    handler = api.handler_fn(on_control)

    def service_main(_argc, _argv):
        handle["h"] = api.dll.RegisterServiceCtrlHandlerExW(name, handler, None)
        report(SERVICE_START_PENDING, wait_hint=START_WAIT_HINT_MS)
        starting = StartProgress(lambda: report(SERVICE_START_PENDING,
                                                wait_hint=START_WAIT_HINT_MS))

        def running() -> None:
            if starting.finish():
                report(SERVICE_RUNNING)
        if on_ready is None:
            running()
        else:
            on_ready(running)
            starting.start()
        code = 1
        try:
            code = int(main() or 0)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        except BaseException:  # whatever happens, the SCM must still be told STOPPED
            import traceback
            traceback.print_exc()
        starting.finish()
        result["code"] = code
        progress.finish()
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


def redirect_missing_stdio(log_path: Optional[str] = None) -> None:
    """A service has no console: print() / logging to a missing stdout must not fail.
    With ``log_path`` a missing stderr goes there (service_log) instead of nowhere."""
    if log_path is not None and sys.stderr is None:
        log = service_log(log_path)
        if log is not None:
            sys.stderr = log
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))


EVENTLOG_ERROR_TYPE = 0x0001
EVENT_START_FAILED = 1  # the event id of "the service stopped: <why>"


def report_event(source: str, text: str) -> bool:
    """An error entry in the Windows Application event log, from ``source`` (the service
    name), where an administrator looks first. The source is not registered: Event
    Viewer says it has no description for event 1 and shows ``text`` below that. Best
    effort, never raises; False when it couldn't be written (or not on Windows)."""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes as w
        dll = _win.WinDLL("advapi32", use_last_error=True)
        dll.RegisterEventSourceW.restype = w.HANDLE
        dll.RegisterEventSourceW.argtypes = [w.LPCWSTR, w.LPCWSTR]
        dll.ReportEventW.restype = w.BOOL
        dll.ReportEventW.argtypes = [w.HANDLE, w.WORD, w.WORD, w.DWORD, ctypes.c_void_p,
                                     w.WORD, w.DWORD, ctypes.POINTER(w.LPCWSTR),
                                     ctypes.c_void_p]
        dll.DeregisterEventSource.argtypes = [w.HANDLE]
        handle = dll.RegisterEventSourceW(None, source)
        if not handle:
            return False
        try:
            strings = (w.LPCWSTR * 1)(text[:31000])  # (an entry holds at most 31839)
            return bool(dll.ReportEventW(handle, EVENTLOG_ERROR_TYPE, 0, EVENT_START_FAILED,
                                         None, 1, 0, strings, None))
        finally:
            dll.DeregisterEventSource(handle)
    except Exception:
        return False


SERVICE_LOG = "service.log"
SERVICE_LOG_MAX = 10 * 1024 * 1024  # what one run writes to it at most


class CappedLog:
    """A text stream that takes at most ``limit`` bytes, then one line saying so: what
    Python itself reports on stderr (tracebacks), which a fault repeated for days must not
    turn into a full disk."""

    def __init__(self, fh, limit: int = SERVICE_LOG_MAX) -> None:
        self._fh = fh
        self._left = limit
        self._full = False
        self.encoding = fh.encoding
        self.errors = fh.errors

    def write(self, text: str) -> int:
        if self._full:
            return len(text)
        data = text.encode(self.encoding, "replace")
        out = text
        if len(data) > self._left:
            self._full = True
            out = data[:self._left].decode(self.encoding, "ignore") + \
                "\n... service.log is full for this run: what follows is not kept\n"
        self._left -= len(data)
        try:
            self._fh.write(out)
            self._fh.flush()
        except (OSError, ValueError):
            self._full = True
        return len(text)

    def flush(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._fh.flush()

    def fileno(self) -> int:
        return self._fh.fileno()

    def isatty(self) -> bool:
        return False


def service_log(path: str) -> Optional[CappedLog]:
    """stderr for the Windows service: <data>/service.log, begun anew at each start (the
    previous one kept as service.log.1). Python's own reports go there: a traceback that
    no handler took, faulthandler's dump of a crash. The service's log lines are in
    all.log; this is a separate file because a second handle on all.log would keep
    Windows from rotating it. None when it can't be opened."""
    with contextlib.suppress(OSError):
        if os.path.exists(path):
            os.replace(path, path + ".1")
    try:
        fh = open(path, "w", encoding="utf-8", errors="replace")
    except OSError:
        return None
    log = CappedLog(fh)
    log.write(f"{clock.stamp()} service start (pid {os.getpid()})\n")
    with contextlib.suppress(Exception):  # a crash in native code: the threads' stacks
        import faulthandler
        faulthandler.enable(file=fh, all_threads=True)
    return log
