"""Interactive console: live serial traffic in the browser (xterm.js over a WebSocket)."""
from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlparse

from starlette.routing import WebSocketRoute

from ...config import ROLE_RANK
from .. import auth
from .common import Ctx

CONSOLE_ACCESS_RECHECK_S = 2.0  # how often an open console re-checks its access


def routes(ctx: Ctx) -> list:
    state = ctx.state

    # ---------------- console (xterm over WebSocket) ----------------
    async def console_ws(websocket):
        cfg = state.config
        # BaseHTTPMiddleware does NOT run for WebSocket scope, so authenticate here.
        token = websocket.cookies.get(auth.SESSION_COOKIE)
        ws_user = auth.request_user(cfg, token, websocket.headers)
        if ws_user is None:
            await websocket.close(code=1008)
            return
        # CSWSH guard: cross-origin WebSocket is rejected.
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=1008)
            return
        mid = websocket.path_params["mid"]
        runner = state.supervisor.get_runner(mid)
        mapping = cfg.get_mapping(mid)
        if runner is None or mapping is None:
            await websocket.close(code=1011)
            return

        await websocket.accept()
        mon = _Monitor()
        runner.add_monitor(mon)
        # viewers get an observe-only console; writing to the device needs operator+
        can_write = ROLE_RANK.get(ws_user.role, 0) >= ROLE_RANK["operator"]
        interactive = (mapping.kind == "net" and not mapping.network.read_only and can_write)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] console opened by {ws_user.username} ({peer})")

        async def sender():
            while True:
                data = await mon.queue.get()
                if data is None:  # mapping stopped
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    return
                await websocket.send_bytes(data)

        async def receiver():
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                if data is None and msg.get("text") is not None:
                    data = msg["text"].encode("utf-8", "replace")
                if data and interactive:
                    await runner.serial_write(data)

        async def access_watch():
            # A console outlives the check at connect: end it once its access is gone
            # (login switched back on, session revoked by a password change, role
            # changed) instead of letting it keep watching — or writing to — the device.
            while True:
                await asyncio.sleep(CONSOLE_ACCESS_RECHECK_S)
                now = auth.request_user(state.config, token, websocket.headers)
                if now is None or now.role != ws_user.role:
                    state.log(f"[{mapping.name}] console closed: access changed ({peer})")
                    return

        st = asyncio.create_task(sender())
        rt = asyncio.create_task(receiver())
        wt = asyncio.create_task(access_watch())
        try:
            await asyncio.wait({st, rt, wt}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (st, rt, wt):
                t.cancel()
            for t in (st, rt, wt):
                with contextlib.suppress(BaseException):
                    await t
            runner.discard_monitor(mon)
            with contextlib.suppress(Exception):
                await websocket.close()
            state.log(f"[{mapping.name}] console closed ({peer})")

    return [
        WebSocketRoute("/api/mappings/{mid}/console", console_ws),
    ]


class _Monitor:
    """A browser console observer: serial traffic is pushed here (best-effort —
    drops oldest on overflow so a slow viewer never stalls the bridge)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    def feed(self, data: bytes) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            with contextlib.suppress(Exception):
                self.queue.get_nowait()
                self.queue.put_nowait(data)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.queue.put_nowait(None)  # sentinel: tells the sender to close

