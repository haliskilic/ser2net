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
import signal
import sys

from . import console
from .config import ConfigStore
from .state import AppState
from .web.server import build_app


def main(config_path: str, reconfigure: bool = False) -> int:
    store = ConfigStore(config_path)
    first_run = not store.exists()
    config = store.load()

    if first_run or reconfigure:
        bind_ip, port = console.choose_admin_bind(config.admin_ui)
        config.admin_ui.bind_ip = bind_ip
        config.admin_ui.port = port
        store.save(config)

    # Own the loop. Force SelectorEventLoop on Windows for pyserial-asyncio-fast.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            return runner.run(_serve(store, config))
    except KeyboardInterrupt:
        return 0


async def _serve(store: ConfigStore, config) -> int:
    import uvicorn

    state = AppState(store, config)
    admin = config.admin_ui
    scheme = "https" if admin.tls_enabled else "http"
    state.log(f"pyser2net starting — admin UI on {scheme}://{admin.bind_ip}:{admin.port}")
    if not config.password_set:
        state.log("no admin password set yet — open the UI to complete first-run setup")
    if admin.bind_ip not in ("127.0.0.1", "::1", "localhost") and not admin.tls_enabled:
        state.log("WARNING: admin UI is bound to a network address over plain HTTP — "
                  "the password and session cookies travel unencrypted. Configure TLS "
                  "(admin_ui.tls_cert/tls_key) or bind to 127.0.0.1.")

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
    # we manage signals ourselves (don't let uvicorn install its own handlers)
    server.install_signal_handlers = lambda: None

    loop = asyncio.get_running_loop()
    serve_task = asyncio.create_task(server.serve())

    def _request_shutdown() -> None:
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError, AttributeError):
            loop.add_signal_handler(sig, _request_shutdown)

    try:
        await serve_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task
    finally:
        state.log("shutting down — stopping all mappings")
        await state.stop_engine()
    return 0
