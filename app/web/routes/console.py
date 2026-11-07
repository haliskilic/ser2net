"""Live traffic in the browser over WebSockets: the interactive console (xterm.js;
what the device sends, and typing to it) and the read-only traffic feed of the hex
view (timestamped; what is sent to the device only for operators and admins)."""
from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from urllib.parse import urlparse

from starlette.routing import WebSocketRoute

from ...engine.bridge import TO_DEVICE
from .. import auth
from .common import Ctx, console_interactive, sees_to_device

CONSOLE_ACCESS_RECHECK_S = 2.0  # how often an open console re-checks its access


def routes(ctx: Ctx) -> list:
    state = ctx.state

    async def _authorize(websocket):
        """The user a WebSocket acts as, or None (after closing it). BaseHTTPMiddleware
        does NOT run for the WebSocket scope, so authenticate here; a cross-origin
        WebSocket is rejected (CSWSH guard)."""
        token = websocket.cookies.get(auth.SESSION_COOKIE)
        ws_user = auth.request_user(state.config, token, websocket.headers,
                                    state.revoked_sessions)
        origin = websocket.headers.get("origin")
        if ws_user is None or (origin and urlparse(origin).netloc != websocket.headers.get("host")):
            await websocket.close(code=1008)
            return None, token
        return ws_user, token

    # ---------------- console (xterm over WebSocket) ----------------
    async def console_ws(websocket):
        cfg = state.config
        ws_user, token = await _authorize(websocket)
        if ws_user is None:
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
        interactive = console_interactive(ws_user, mapping)
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
                now = auth.request_user(state.config, token, websocket.headers,
                                        state.revoked_sessions)
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

    # ---------------- traffic feed for the hex view (read-only) ----------------
    async def traffic_ws(websocket):
        """A mapping's traffic, as binary frames
        [direction: 1 byte (0 = from the device, 1 = to it)][time: float64 BE][bytes].
        Read-only for everyone who may open the console; what is sent to the device
        only for those who may see it (sees_to_device), the others never get it queued."""
        ws_user, token = await _authorize(websocket)
        if ws_user is None:
            return
        mid = websocket.path_params["mid"]
        runner = state.supervisor.get_runner(mid)
        mapping = state.config.get_mapping(mid)
        if runner is None or mapping is None:
            await websocket.close(code=1011)
            return
        await websocket.accept()
        to_device = sees_to_device(ws_user, mapping)
        mon = _TrafficMonitor(to_device=to_device)
        runner.add_traffic_monitor(mon)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] hex view opened by {ws_user.username} ({peer})")

        async def sender():
            while True:
                item = await mon.queue.get()
                if item is None:  # mapping stopped
                    return
                direction, ts, data = item
                await websocket.send_bytes(struct.pack(">Bd", direction, ts) + data)

        async def receiver():  # nothing is accepted from the browser; notice a close
            while (await websocket.receive()).get("type") != "websocket.disconnect":
                pass

        async def access_watch():
            while True:
                await asyncio.sleep(CONSOLE_ACCESS_RECHECK_S)
                now = auth.request_user(state.config, token, websocket.headers,
                                        state.revoked_sessions)
                if now is None or now.role != ws_user.role:
                    return

        tasks = [asyncio.create_task(c()) for c in (sender, receiver, access_watch)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(BaseException):
                    await t
            runner.discard_traffic_monitor(mon)
            with contextlib.suppress(Exception):
                await websocket.close()
            state.log(f"[{mapping.name}] hex view closed ({peer})")

    return [
        WebSocketRoute("/api/mappings/{mid}/console", console_ws),
        WebSocketRoute("/api/mappings/{mid}/traffic", traffic_ws),
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



class _TrafficMonitor:
    """A hex-view observer: (direction, time, bytes) items, best-effort like _Monitor
    (the oldest item is dropped on overflow so a slow browser never stalls a bridge).
    Without `to_device`, what is sent to the device is left out."""

    def __init__(self, *, to_device: bool) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.to_device = to_device

    def feed_traffic(self, direction: int, data: bytes) -> None:
        if direction == TO_DEVICE and not self.to_device:
            return
        item = (direction, time.time(), bytes(data))
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(Exception):
                self.queue.get_nowait()
                self.queue.put_nowait(item)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.queue.put_nowait(None)
