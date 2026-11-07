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
import logging
import os
import signal
import sys
import threading
from typing import Callable, Mapping, Optional

# asyncio.Runner is native on Python 3.11+. On 3.10 use the pure-Python backport
# (bundled: taskgroup), which accepts the same loop_factory argument.
if sys.version_info >= (3, 11):
    from asyncio import Runner
else:  # pragma: no cover - exercised only on Python 3.10
    from taskgroup import Runner

from . import accounts, console, standalone
from .config import ConfigError, ConfigStore
from .engine import netguard
from .state import AppState, write_audit_record
from .web import httpguard
from .web.server import build_app


def _env_bind_override(admin) -> bool:
    """Apply SER2NET_BIND_IP / SER2NET_PORT env vars to the admin UI bind address.

    Lets a headless/containerized deployment set the bind address without the
    interactive console picker (which, being non-interactive in a container,
    would default to loopback and leave the UI unreachable behind a published
    port). Returns True if a bind IP was provided. Env wins on every start, so a
    container's compose/run config stays the source of truth across restarts."""
    ip = (os.environ.get("SER2NET_BIND_IP") or "").strip()
    if not ip:
        return False
    admin.bind_ip = ip
    port = (os.environ.get("SER2NET_PORT") or "").strip()
    if port:
        with contextlib.suppress(ValueError):
            admin.port = int(port)
    return True


# Stopping from another thread (the Windows service's stop request): request_shutdown()
# sets the event and calls the hook a running _serve() registered.
_shutdown_requested = threading.Event()
_shutdown_hooks: list = []


def request_shutdown() -> None:
    """Ask the running server to stop, from any thread (thread-safe)."""
    _shutdown_requested.set()
    for hook in list(_shutdown_hooks):
        hook()


def prepare_config(config_path: str, reconfigure: bool = False):
    """Load config.json (creating it on the first run, when the console asks for the
    admin UI address), apply SER2NET_BIND_IP / SER2NET_PORT and create the first admin
    from SER2NET_ADMIN_PASSWORD_FILE. Returns (store, config)."""
    store = ConfigStore(config_path)
    first_run = not store.exists()
    config = store.load()

    env_set = _env_bind_override(config.admin_ui)
    if (first_run or reconfigure) and not env_set:
        bind_ip, port = console.choose_admin_bind(config.admin_ui)
        config.admin_ui.bind_ip = bind_ip
        config.admin_ui.port = port
    admin, note = accounts.provision_from_env(config, os.environ)
    if note:
        config.load_notes.append(note)
    if first_run or reconfigure or env_set:
        store.save(config)
    elif config.ids_created or admin:
        # the first admin from the environment, or config.json had no secret_key /
        # instance_id (made by hand, or a downloaded backup put in its place): keep them,
        # or every restart would sign everyone out and give this node a new cluster id
        try:
            store.save(config)
        except (ConfigError, OSError) as e:
            config.load_notes.append(f"WARNING: could not save {store.path}: {e}")
    if admin:
        write_audit_record(os.path.join(os.path.dirname(store.path), "audit.log"), "local",
                           "first_run_setup", "SER2NET_ADMIN_PASSWORD_FILE", admin)
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


def main(config_path: str, reconfigure: bool = False) -> int:
    standalone.prepare()  # standalone Linux binary: find the system CA certificates
    store, config = prepare_config(config_path, reconfigure)

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
    state.prepare_first_run()
    if admin.bind_ip not in ("127.0.0.1", "::1", "localhost") and not admin.tls_enabled:
        state.log("WARNING: admin UI is bound to a network address over plain HTTP — "
                  "the password and session cookies travel unencrypted. Configure TLS "
                  "(admin_ui.tls_cert/tls_key) or bind to 127.0.0.1.")
    if config.password_set and not admin.require_login:
        state.log(f"WARNING: web UI login is switched off — visitors without a session get "
                  f"the '{admin.anonymous_role}' role (Settings → Web UI access).")

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
        http=httpguard.protocol_class(guard),
    )
    if admin.tls_enabled:
        try:
            guard.ssl = httpguard.ssl_context(admin.tls_cert, admin.tls_key, uconfig)
        except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
            state.log(f"admin UI: the TLS certificate could not be loaded: {e}")
            await state.stop_engine()
            raise
    server = guard.server = uvicorn.Server(uconfig)
    # uvicorn (>= 0.29) also traps SIGINT/SIGTERM while it serves, from the main
    # thread only; both its handler and ours below just ask the server to exit.

    loop = asyncio.get_running_loop()
    serve_task = asyncio.create_task(server.serve())

    def _request_shutdown() -> None:
        server.should_exit = True

    # (not on Windows, and not outside the main thread — e.g. the Windows service
    # thread; request_shutdown() covers those)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError, AttributeError):
            loop.add_signal_handler(sig, _request_shutdown)

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
        _shutdown_hooks.remove(_from_other_thread)
        guard.close()
        state.log("shutting down — stopping all mappings")
        await state.stop_engine()
    return 0
