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


def install(name: str, data_dir: str, config: Optional[str] = None, start: bool = True,
            log: Callable[[str], None] = print) -> None:
    api = _advapi()
    name = check_name(name)
    cmd = service_command(name, data_dir, config)
    scm = api.dll.OpenSCManagerW(None, None, SC_MANAGER_CONNECT | SC_MANAGER_CREATE_SERVICE)
    if not scm:
        raise _fail("Opening the Service Control Manager")
    try:
        svc = api.dll.CreateServiceW(
            scm, name, display_name(name),
            SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP | SERVICE_CHANGE_CONFIG | DELETE,
            SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
            cmd, None, None, None, None, None)  # LocalSystem
        if not svc:
            if _win.get_last_error() == ERROR_SERVICE_EXISTS:
                raise ServiceError(f"A service named '{name}' already exists. Remove it first "
                                   f"with --uninstall-service, or pick another --service-name.")
            raise _fail("Creating the service")
        try:
            desc = api.SERVICE_DESCRIPTIONW(
                f"Serial-to-network bridge with a web UI. Data directory: {data_dir}")
            api.dll.ChangeServiceConfig2W(svc, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(desc))
            actions = (api.SC_ACTION * 3)(*[api.SC_ACTION(SC_ACTION_RESTART, RESTART_DELAY_MS)] * 3)
            fa = api.SERVICE_FAILURE_ACTIONSW(86400, None, None, 3, actions)
            if not api.dll.ChangeServiceConfig2W(svc, SERVICE_CONFIG_FAILURE_ACTIONS, ctypes.byref(fa)):
                raise _fail("Setting the restart-on-failure actions")
            flag = api.SERVICE_FAILURE_ACTIONS_FLAG(True)  # also after an exit with an error
            api.dll.ChangeServiceConfig2W(svc, SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, ctypes.byref(flag))
            log(f"service '{name}' installed: starts at boot, restarts {RESTART_DELAY_MS // 1000} s "
                f"after a failure\n  command: {cmd}")
            if start:
                if not api.dll.StartServiceW(svc, 0, None):
                    raise _fail("Starting the service")
                state = _wait_state(api, svc, SERVICE_RUNNING)
                if state != SERVICE_RUNNING:
                    raise ServiceError(f"The service did not reach RUNNING (state {state}); "
                                       f"see {os.path.join(data_dir, 'all.log')}.")
                log(f"service '{name}' is running")
        finally:
            api.dll.CloseServiceHandle(svc)
    finally:
        api.dll.CloseServiceHandle(scm)


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
