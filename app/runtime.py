"""Runtime orchestration: own the event loop, run uvicorn + the bridge engine.

Critical cross-platform detail (from the design research): we OWN the event loop
rather than letting uvicorn pick it. On Windows we force a SelectorEventLoop (via
asyncio.Runner's loop_factory) — durable and future-proof vs. the deprecated
event-loop policies — and run uvicorn.Server.serve() INSIDE that loop with
loop="asyncio". On Linux the default selector loop is already correct.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import threading

# asyncio.Runner is native on Python 3.11+. On 3.10 use the pure-Python backport
# (bundled: taskgroup), which accepts the same loop_factory argument.
if sys.version_info >= (3, 11):
    from asyncio import Runner
else:  # pragma: no cover - exercised only on Python 3.10
    from taskgroup import Runner

from . import console, standalone
from .config import ConfigStore
from .state import AppState
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
    admin UI address) and apply SER2NET_BIND_IP / SER2NET_PORT. Returns (store, config)."""
    store = ConfigStore(config_path)
    first_run = not store.exists()
    config = store.load()

    env_set = _env_bind_override(config.admin_ui)
    if (first_run or reconfigure) and not env_set:
        bind_ip, port = console.choose_admin_bind(config.admin_ui)
        config.admin_ui.bind_ip = bind_ip
        config.admin_ui.port = port
    if first_run or reconfigure or env_set:
        store.save(config)
    return store, config


def main(config_path: str, reconfigure: bool = False) -> int:
    standalone.prepare()  # standalone Linux binary: find the system CA certificates
    store, config = prepare_config(config_path, reconfigure)

    # Own the loop. Force SelectorEventLoop on Windows for pyserial-asyncio-fast.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with Runner(loop_factory=loop_factory) as runner:
            return runner.run(_serve(store, config))
    except KeyboardInterrupt:
        return 0


async def _serve(store: ConfigStore, config) -> int:
    import uvicorn

    state = AppState(store, config)
    admin = config.admin_ui
    scheme = "https" if admin.tls_enabled else "http"
    state.log(f"ser2net starting — admin UI on {scheme}://{admin.bind_ip}:{admin.port}")
    for note in standalone.notes + config.load_notes:
        state.log(note)
    if not config.password_set:
        state.log("no admin password set yet — open the UI to complete first-run setup")
    if admin.bind_ip not in ("127.0.0.1", "::1", "localhost") and not admin.tls_enabled:
        state.log("WARNING: admin UI is bound to a network address over plain HTTP — "
                  "the password and session cookies travel unencrypted. Configure TLS "
                  "(admin_ui.tls_cert/tls_key) or bind to 127.0.0.1.")
    if config.password_set and not admin.require_login:
        state.log(f"WARNING: web UI login is switched off — visitors without a session get "
                  f"the '{admin.anonymous_role}' role (Settings → Web UI access).")

    await state.start_engine()
    app = build_app(state)

    uconfig = uvicorn.Config(
        app,
        host=admin.bind_ip,
        port=admin.port,
        loop="asyncio",
        log_level="warning",
        access_log=False,
        ssl_certfile=admin.tls_cert or None,
        ssl_keyfile=admin.tls_key or None,
    )
    server = uvicorn.Server(uconfig)
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
        state.log("shutting down — stopping all mappings")
        await state.stop_engine()
    return 0
