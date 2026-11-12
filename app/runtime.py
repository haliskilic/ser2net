"""Runtime orchestration: own the event loop, run uvicorn + the bridge engine.

Critical cross-platform detail (from the design research): we OWN the event loop
rather than letting uvicorn pick it (asyncio.Runner's loop_factory: durable and
future-proof vs. the deprecated event-loop policies) and run uvicorn.Server.serve()
INSIDE that loop with loop="asyncio". On Linux the default selector loop (epoll) is
correct. On Windows it is a SelectorEventLoop whose select() takes at most 512 sockets:
its selector refuses a socket past that instead of stopping the loop, and incoming
connections are refused before it fills (engine/netguard.py). SER2NET_WINDOWS_LOOP=proactor
runs the ProactorEventLoop instead, which has no such limit (event_loop_choice).
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import signal
import stat
import sys
import threading
import time
import traceback
from typing import Callable, Mapping, Optional

# asyncio.Runner is native on Python 3.11+. On 3.10 use the pure-Python backport
# (bundled: taskgroup), which accepts the same loop_factory argument.
if sys.version_info >= (3, 11):
    from asyncio import Runner
else:  # pragma: no cover - exercised only on Python 3.10
    from taskgroup import Runner

from . import (
    accounts, console, datalock, i18n, logsetup, reload, sdnotify, standalone, tlsreload,
    watchdog,
)
from .config import ConfigError, ConfigStore
from .engine import netguard
from .state import AppState, write_audit_record
from .web import httpguard
from .web.server import build_app


def _env_bind_override(admin, notes: list[str] | None = None) -> bool:
    """Apply SER2NET_BIND_IP / SER2NET_PORT env vars to the admin UI bind address.

    Lets a headless/containerized deployment set the bind address without the
    interactive console picker (which, being non-interactive in a container,
    would default to loopback and leave the UI unreachable behind a published
    port). Returns True if a bind IP was provided. Env wins on every start, so a
    container's compose/run config stays the source of truth across restarts.

    Raises ConfigError for a SER2NET_BIND_IP that is not an IP address or a
    SER2NET_PORT outside 1-65535 (the UI could not start on either). A SER2NET_PORT
    that is not a number is ignored, as it always was, with a warning in `notes`."""
    ip = (os.environ.get("SER2NET_BIND_IP") or "").strip()
    if not ip:
        return False
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ConfigError(f"SER2NET_BIND_IP must be an IP address (e.g. 0.0.0.0), "
                          f"not {ip!r}") from None
    port = (os.environ.get("SER2NET_PORT") or "").strip()
    number = None
    if port:
        try:
            number = int(port)
        except ValueError:
            if notes is not None:
                notes.append(f"WARNING: SER2NET_PORT {port!r} is not a number and is ignored "
                             f"— the admin UI uses port {admin.port}")
        else:
            if not 1 <= number <= 65535:
                raise ConfigError(f"SER2NET_PORT must be a port number (1-65535), not {port!r}")
    admin.bind_ip = ip
    if number is not None:
        admin.port = number
    return True


def _os_error(e: OSError, data_dir: str | None = None) -> str:
    """'<file>: <reason>' for an OSError on a data file (one line, no traceback). A
    permission error on POSIX adds who owns the file, who ser2net runs as and the fix."""
    where = e.filename if e.filename is not None else ""
    if e.filename2 is not None:
        where = f"{where} -> {e.filename2}"
    reason = e.strerror or str(e)
    text = f"{where}: {reason}" if where else reason
    if isinstance(e, PermissionError) and os.name == "posix" and e.filename is not None:
        text += _access_hint(os.fsdecode(e.filename), data_dir)
    return text


def _access_hint(path: str, data_dir: str | None) -> str:
    """The usual cause of a permission error on ser2net's files: another account left
    them behind (a run with sudo, or a Docker image before v2.8, which ran as root)."""
    me = f"{os.getuid()}:{os.getgid()}"
    owner = "it can't be inspected"
    for target, label in ((path, "it"), (os.path.dirname(path), "its folder")):
        try:
            st = os.stat(target)
        except OSError:
            continue
        owner = f"{label} belongs to {st.st_uid}:{st.st_gid} (mode {stat.S_IMODE(st.st_mode):04o})"
        break
    if data_dir is None:
        return f" — {owner} and ser2net runs as {me}: make it readable for that account"
    fix = f"sudo chown -R {me} {data_dir}"
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        fix += " (in a container: on the host folder mounted there)"
    return (f" — {owner} and ser2net runs as {me}: give the data directory to that "
            f"account: {fix}")


# Stopping from another thread (the Windows service's stop request): request_shutdown()
# sets the event and calls the hook a running _serve() registered.
_shutdown_requested = threading.Event()
_shutdown_hooks: list = []
# Called once the server is ready (when_ready): the Windows service reports RUNNING then.
_ready_hooks: list[Callable[[], None]] = []


# SIGHUP (POSIX) has ser2net read config.json again (app/reload.py). One that comes before
# the event loop handles it (while config.json is loaded at start-up) is noted here instead
# of ending the process, its default, and handled once the server runs.
_hangups: list[float] = []


def _defer_hangups() -> None:
    if hasattr(signal, "SIGHUP"):
        with contextlib.suppress(ValueError, OSError):  # (not the main thread)
            signal.signal(signal.SIGHUP, lambda _sig, _frame: _hangups.append(time.time()))


def _stdin_is_terminal() -> bool:
    try:
        return sys.stdin is not None and os.isatty(sys.stdin.fileno())
    except (AttributeError, ValueError, OSError):
        return False


def when_ready(hook: Callable[[], None]) -> None:
    """Call ``hook()`` once the server has started: its mappings were started and its admin
    UI listens (from the event loop's thread; not at all when the start fails)."""
    _ready_hooks.append(hook)


def request_shutdown() -> None:
    """Ask the running server to stop, from any thread (thread-safe)."""
    _shutdown_requested.set()
    for hook in list(_shutdown_hooks):
        hook()


def prepare_config(config_path: str, reconfigure: bool = False):
    """Load config.json (creating it on the first run, when the console asks for the
    admin UI address), apply SER2NET_BIND_IP / SER2NET_PORT and create the first admin
    from SER2NET_ADMIN_PASSWORD_FILE. Returns (store, config).

    A config.json that loads is never refused: what validate() rejects in it is logged
    as a warning (config.load_notes) and accepted (AppConfig.tolerate_problems), so the
    server starts with it, as a start without the env vars always has (a mapping that
    can't start fails on its own), and later saves only refuse what they change or add.
    The file is written back only when this start changes it (first run, --reconfigure,
    a new env address, an upgrade's normalisation); a failing write is a warning too.
    Fatal, with one line instead of a traceback: an unusable SER2NET_BIND_IP /
    SER2NET_PORT, a config.json that can't be read or parsed, a first run that can't
    create it."""
    data_dir = os.path.dirname(os.path.abspath(config_path))
    try:
        store = ConfigStore(config_path)
        first_run = not store.exists()
        config = store.load()
    except OSError as e:
        raise SystemExit(
            f"ser2net: cannot read the configuration: {_os_error(e, data_dir)}") from None
    except (ValueError, TypeError, AttributeError) as e:  # not JSON, or not shaped like it
        newest = store.backup_names()[:1]
        restore = (f", or put back its newest backup ({os.path.join(store.backup_dir, newest[0])})"
                   if newest else "")
        raise SystemExit(f"ser2net: {store.path} is not a valid configuration ({e}) — fix "
                         f"it{restore}") from None
    notes = config.load_notes

    try:
        env_set = _env_bind_override(config.admin_ui, notes)
    except ConfigError as e:
        raise SystemExit(f"ser2net: {e}") from None
    if (first_run or reconfigure) and not env_set:
        bind_ip, port = console.choose_admin_bind(config.admin_ui)
        config.admin_ui.bind_ip = bind_ip
        config.admin_ui.port = port
    admin, note = accounts.provision_from_env(config, os.environ)
    if note:
        notes.append(note)

    problems = config.tolerate_problems()
    notes.extend(f"WARNING: config.json: {p}" for p in problems)
    # also the first admin from the environment, or a config.json without secret_key /
    # instance_id (made by hand, or a downloaded backup put in its place): keep them, or
    # every restart would sign everyone out and give this node a new cluster id
    if ((first_run or reconfigure or env_set) and not store.matches(config)
            or config.ids_created or admin):
        try:
            # not validated: what this config breaks was accepted just above, so a check
            # could only fail on a file it looks for that went away since, and that must
            # not stop the start either (under a restart policy: over and over)
            store.save(config, validate=False)
        except OSError as e:  # read-only/full data dir, config.json mounted on its own
            if first_run:
                raise SystemExit(f"ser2net: cannot create the configuration: "
                                 f"{_os_error(e, data_dir)}") from None
            notes.append(f"WARNING: could not update config.json at start-up "
                         f"({_os_error(e, data_dir)}) — ser2net runs with these settings, "
                         "but can't keep any change until config.json can be written (keep "
                         "the data directory writable, and don't mount config.json on its "
                         "own)")
    if admin:
        write_audit_record(os.path.join(os.path.dirname(store.path), "audit.log"), "local",
                           "first_run_setup", "SER2NET_ADMIN_PASSWORD_FILE", admin,
                           max_age_s=config.log_max_age_days * 86400)
    if problems:
        notes.append("WARNING: ser2net starts with the setting(s) above anyway — what they "
                     "affect may not work. Fix them in the web UI (or in config.json); "
                     "changes that leave them as they are can be saved meanwhile")
    return store, config


WINDOWS_LOOP_ENV = "SER2NET_WINDOWS_LOOP"


def event_loop_choice(platform: str = sys.platform,
                      env: Optional[Mapping[str, str]] = None) -> tuple[str, str]:
    """The event loop ser2net runs on ("default", "selector" or "proactor") and the line the
    start-up log gets about it ("" when there is nothing to say). Windows: the
    SelectorEventLoop, which serial ports and sockets have run on so far, unless
    SER2NET_WINDOWS_LOOP=proactor; elsewhere the platform's default."""
    if platform != "win32":
        return "default", ""
    env = os.environ if env is None else env
    want = (env.get(WINDOWS_LOOP_ENV) or "").strip().lower()
    if want == "proactor":
        return "proactor", (f"event loop: proactor ({WINDOWS_LOOP_ENV}=proactor), no "
                            f"{netguard.SELECT_LIMIT}-socket limit")
    note = "" if want in ("", "selector") else (
        f"{WINDOWS_LOOP_ENV}={want!r} is neither 'selector' nor 'proactor'; ")
    return "selector", (note + f"event loop: selector (select() watches at most "
                        f"{netguard.SELECT_LIMIT} sockets, so new connections are refused "
                        f"near that; {WINDOWS_LOOP_ENV}=proactor has no such limit)")


def loop_factory(choice: str) -> Optional[Callable[[], asyncio.AbstractEventLoop]]:
    """What asyncio.Runner makes the loop with, for an event_loop_choice() answer (None: the
    platform's default)."""
    if choice == "selector":
        def selector_loop() -> asyncio.AbstractEventLoop:
            selector = netguard.CappedSelectSelector()
            netguard.BUDGET.selector = selector
            return asyncio.SelectorEventLoop(selector)
        return selector_loop
    if choice == "proactor" and sys.platform == "win32":
        return asyncio.ProactorEventLoop
    return None


def require_admin_tls(admin) -> None:
    """The admin UI's TLS certificate and key must be readable, or the start stops here
    with one line (uvicorn would fail later, with a traceback, after the bridges started).
    No fallback to plain HTTP: it would send passwords in clear where TLS was set up."""
    if not admin.tls_enabled:
        return
    for field, what, path in (("tls_cert", "certificate", admin.tls_cert),
                              ("tls_key", "key", admin.tls_key)):
        if not isinstance(path, str):  # a hand edit: open(True) would open (and close) stdout
            raise SystemExit(f"ser2net: admin_ui.{field} in config.json must be a file name, "
                             f"not {path!r}")
        try:
            with open(path, "rb"):
                pass
        except OSError as e:
            fix = ("" if isinstance(e, PermissionError)
                   else " — fix admin_ui.tls_cert / tls_key in config.json")
            raise SystemExit(f"ser2net: cannot read the admin UI TLS {what}: "
                             f"{_os_error(e)}{fix}") from None


def admin_tls_builder(uconfig) -> Callable:
    """How the admin UI's TLS context is made from its settings (AdminUI) while ser2net
    runs (Settings -> Admin TLS, a reload): as at start-up, except that a key with a
    passphrase waits for the next start, where the console asks for it."""
    def build(admin):
        if not admin.tls_enabled:
            return None
        tlsreload.refuse_passphrase(admin.tls_key)
        return httpguard.ssl_context(admin.tls_cert, admin.tls_key, uconfig)
    return build


def hold_data_dir(config_path: str) -> str | None:
    """Take the data directory's single-instance lock (app.datalock) before anything is
    written there: a second ser2net on the same directory stops here with one line; a
    --set-password / --sign-out-all that is changing config.json is waited for.
    Returns a warning for the start-up log when the lock can't be taken (the start goes
    on without it)."""
    data_dir = os.path.dirname(os.path.abspath(config_path))
    with contextlib.suppress(OSError):  # if it can't be created, loading config.json says why
        os.makedirs(data_dir, exist_ok=True)
    try:
        return datalock.acquire(data_dir, wait=datalock.BRIEF_WAIT_S)
    except datalock.Held as e:
        if e.brief:
            raise SystemExit(f"ser2net: {e} — start again once it is done") from None
        raise SystemExit(f"ser2net: {e} — stop it first (a Windows service: sc stop ser2net; "
                         "systemd: systemctl stop ser2net) or give this one its own "
                         "--data-dir") from None


def main(config_path: str, reconfigure: bool = False) -> int:
    _defer_hangups()
    standalone.prepare()  # standalone Linux binary: find the system CA certificates
    lock_note = hold_data_dir(config_path)
    try:
        store, config = prepare_config(config_path, reconfigure)
        if lock_note:
            config.load_notes.append(lock_note)
        require_admin_tls(config.admin_ui)

        choice, loop_note = event_loop_choice()
        try:
            with Runner(loop_factory=loop_factory(choice)) as runner:
                return runner.run(_serve(store, config, loop_note))
        except KeyboardInterrupt:
            return 0
        except Exception:
            # all.log too, not only stderr (a service has none to read); then exit with it
            logging.getLogger("ser2net").exception("fatal error, ser2net stops")
            raise
    finally:
        datalock.release(os.path.dirname(os.path.abspath(config_path)))


def _log_loop_errors(loop: asyncio.AbstractEventLoop, log) -> None:
    """What asyncio reports to its exception handler (an error in a callback, a listener
    out of descriptors: "socket.accept() out of system resource") goes to all.log as well
    as stderr, at most once a minute."""
    say = netguard.LogEvery(log)

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        detail = f" ({type(exc).__name__}: {exc})" if exc is not None else ""
        say(f"event loop: {context.get('message') or 'error'}{detail}")
        loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def _log_thread_errors(log) -> None:
    """A thread that ends with an exception (a trace writer, the port list or Windows
    hot-plug watcher) is named in all.log too, at most once a minute, not only on stderr,
    which the Windows service doesn't have: such a thread does nothing from then on.
    Python's own report still follows on stderr."""
    say = netguard.LogEvery(log)
    previous = threading.excepthook

    def hook(args) -> None:
        if args.exc_type is not SystemExit:  # which ends a thread quietly
            frames = traceback.extract_tb(args.exc_traceback) if args.exc_traceback else []
            where = (f" (at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno})"
                     if frames else "")
            name = args.thread.name if args.thread is not None else "?"
            say(f"thread {name!r} stopped by an error: {args.exc_type.__name__}: "
                f"{args.exc_value}{where}")
        previous(args)

    threading.excepthook = hook


_as_service = False  # set by service_main so _serve can log the service-only warnings
_service_name = ""   # (and the service's name, for the Settings page's hints)


def service_main(config_path: str, name: str = "ser2net") -> int:
    """main() for the Windows service ``name``, which has no console: a start-up error
    that stops it (the one-line SystemExit) also goes to all.log next to config.json,
    where the admin looks for it; that and any other error that ends it is also written
    to the Application event log (service_stopped_event)."""
    global _as_service, _service_name
    _as_service, _service_name = True, name
    try:
        return main(config_path=config_path)
    except SystemExit as e:
        if isinstance(e.code, str):
            log = os.path.join(os.path.dirname(os.path.abspath(config_path)), "all.log")
            with contextlib.suppress(OSError), open(log, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e.code}\n")
        if e.code not in (None, 0):
            service_stopped_event(name, config_path, e.code if isinstance(e.code, str)
                                  else f"exit code {e.code}")
        raise
    except Exception as e:
        service_stopped_event(name, config_path, f"{type(e).__name__}: {e}")
        raise


def service_stopped_event(name: str, config_path: str, why: str) -> bool:
    """The Application event log entry of a service that stopped on an error."""
    from . import winservice
    data_dir = os.path.dirname(os.path.abspath(config_path))
    return winservice.report_event(name, (
        f"The ser2net service '{name}' stopped: {why}\n\nDetails: "
        f"{os.path.join(data_dir, 'all.log')} and {os.path.join(data_dir, winservice.SERVICE_LOG)}"))


async def _serve(store: ConfigStore, config, loop_note: str = "") -> int:
    import uvicorn

    state = AppState(store, config)
    admin = config.admin_ui
    scheme = "https" if admin.tls_enabled else "http"
    state.log(f"ser2net starting — admin UI on {scheme}://{admin.bind_ip}:{admin.port}")
    for note in standalone.notes + config.load_notes + ([loop_note] if loop_note else []):
        state.log(note)
    for line in (netguard.raise_fd_limit(), netguard.fd_budget_warning(config)):
        if line:
            state.log(line)
    _log_loop_errors(asyncio.get_running_loop(), state.log)
    _log_thread_errors(state.log)
    # a stuck event loop ends the process (to be started again): app/watchdog.py
    if hang := watchdog.previous_hang(state.data_dir):
        state.log(hang)
        state.previous_hang = hang
    dog = state.loop_watchdog = watchdog.start(state.data_dir, state.log)
    # uvicorn's warnings and errors (a port it can't use, a TLS error, the traceback of a
    # request that failed) and other libraries' go to all.log (logsetup.LibraryLogHandler)
    logsetup.route_library_logs(logging.getLogger("ser2net"))
    for lang in i18n.missing_catalogs():
        state.log(f"WARNING: the '{lang}' UI translation is missing from {i18n.LOCALE_DIR} "
                  "— the web UI shows English to visitors who pick it")
    if _as_service and sys.platform == "win32":
        from . import winservice
        if warning := winservice.location_warning():
            state.log(warning)
    state.prepare_first_run()
    if admin.bind_ip not in ("127.0.0.1", "::1", "localhost") and not admin.tls_enabled:
        state.log("WARNING: admin UI is bound to a network address over plain HTTP — "
                  "the password and session cookies travel unencrypted. Configure TLS "
                  "(admin_ui.tls_cert/tls_key) or bind to 127.0.0.1.")
    if config.password_set and not admin.require_login:
        state.log(f"WARNING: web UI login is switched off — visitors without a session get "
                  f"the '{admin.anonymous_role}' role (Settings → Web UI access).")
    ldap = config.ldap
    if ldap.enabled and ldap.uses_tls and not ldap.tls_verify:
        state.log("WARNING: LDAP: the directory server's certificate is not checked "
                  "(ldap.tls_verify is false) — anyone on the network between ser2net and the "
                  "directory could read the passwords and sign in as anyone. Set the CA "
                  "certificate file instead (Settings → LDAP / Active Directory).")
    elif ldap.enabled and ldap.sends_plaintext:
        state.log("WARNING: LDAP sends the passwords unencrypted (ldap:// without StartTLS) — "
                  "use ldaps:// or StartTLS (Settings → LDAP / Active Directory).")

    await state.start_engine()
    app = build_app(state)

    # the admin listener's connections: admitted (caps, budget) before TLS, which the
    # protocol does itself, and each request head on a deadline (web/httpguard.py)
    guard = state.admin_guard = httpguard.AdminGuard(admin, state.log)
    uconfig = uvicorn.Config(
        app,
        host=admin.bind_ip,
        port=admin.port,
        loop="asyncio",
        log_level="warning",
        access_log=False,
        log_config=None,  # (its own would write to stderr only: logsetup takes them)
        http=httpguard.protocol_class(guard),
    )
    if admin.tls_enabled:
        try:
            guard.ssl = httpguard.ssl_context(admin.tls_cert, admin.tls_key, uconfig)
        except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
            state.log(f"admin UI: the TLS certificate could not be loaded: {e}")
            await state.stop_engine()
            raise
    state.admin_tls = admin_tls_builder(uconfig)
    state.admin_served = (admin.bind_ip, admin.port)
    if _as_service:
        state.service_name = _service_name
    server = guard.server = uvicorn.Server(uconfig)
    # uvicorn (>= 0.29) also traps SIGINT/SIGTERM while it serves, from the main
    # thread only; both its handler and ours below just ask the server to exit.

    loop = asyncio.get_running_loop()
    serve_task = asyncio.create_task(server.serve())
    ready_task = asyncio.create_task(_when_started(server, serve_task, state), name="ready")

    def _request_shutdown() -> None:
        server.should_exit = True

    # (not on Windows, and not outside the main thread — e.g. the Windows service
    # thread; request_shutdown() covers those)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError, AttributeError):
            loop.add_signal_handler(sig, _request_shutdown)

    # SIGHUP: read config.json again. A terminal that closes sends it too (to a ser2net
    # started in it): its stdin is a terminal no more then, and ser2net stops cleanly,
    # as the end of that session asks; `kill -HUP` while the terminal is open reloads.
    in_terminal = _stdin_is_terminal()

    def _hangup() -> None:
        if in_terminal and not _stdin_is_terminal():
            state.log("SIGHUP: the terminal ser2net was started in was closed — shutting down")
            _request_shutdown()
            return
        task = asyncio.create_task(reload.reload_config(state, "SIGHUP"), name="reload")
        state.reloads.add(task)
        task.add_done_callback(state.reloads.discard)

    if hasattr(signal, "SIGHUP"):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            loop.add_signal_handler(signal.SIGHUP, _hangup)
            if _hangups:  # (one that came while ser2net was starting)
                _hangups.clear()
                _hangup()

    def _from_other_thread() -> None:
        loop.call_soon_threadsafe(_request_shutdown)

    _shutdown_hooks.append(_from_other_thread)
    if _shutdown_requested.is_set():  # asked to stop before we were ready
        _request_shutdown()
    try:
        await serve_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task
    finally:
        sdnotify.stopping()
        ready_task.cancel()
        for task in list(state.reloads):  # (a reload under way ends with the process)
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        _shutdown_hooks.remove(_from_other_thread)
        guard.close()
        state.log("shutting down — stopping all mappings")
        await state.stop_engine()
        await dog.stop()
    return 0


async def _when_started(server, serve_task: asyncio.Task, state: AppState) -> None:
    """Once uvicorn listens (server.started): _announce_ready. Not when it stops first."""
    while not server.started:
        if serve_task.done():
            return
        await asyncio.sleep(0.05)
    _announce_ready(state)


def _announce_ready(state: AppState) -> None:
    """The server has started (its mappings were started, its admin UI listens): tell
    systemd (READY=1, under the optional Type=notify unit) and the when_ready hooks (the
    Windows service reports RUNNING only now, so --install-service sees a start that
    fails)."""
    statuses = state.supervisor.all_status()
    running = sum(1 for st in statuses.values() if st.get("state") == "running")
    admin = state.config.admin_ui
    sdnotify.ready(f"{running} of {len(state.config.mappings)} mapping(s) running, admin UI "
                   f"on {admin.bind_ip}:{admin.port}")
    hooks, _ready_hooks[:] = list(_ready_hooks), []
    for hook in hooks:
        try:
            hook()
        except Exception as e:  # (the server runs on; only the report is lost)
            state.log(f"WARNING: could not report that ser2net is ready: {e}")
