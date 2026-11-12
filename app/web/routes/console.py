"""Live traffic in the browser over WebSockets: the interactive console (xterm.js;
what the device sends, and typing to it) and the read-only traffic feed of the hex
view (timestamped; what is sent to the device only for operators and admins)."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import struct
import time
from urllib.parse import urlparse

from starlette.routing import WebSocketRoute

from ...engine.bridge import TO_DEVICE
from .. import auth
from .common import Ctx, client_ip, console_interactive, sees_to_device

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
        websocket.state.user = ws_user  # like request.state.user (ctx.audit)
        return ws_user, token

    def _record_open(websocket, action: str, mapping, detail: str) -> None:
        """The audit record of a console or hex view opened, bounded (AppState.view_records):
        per account — an open-access visitor: per address —, mapping and kind."""
        ip, user = client_ip(websocket), ctx.actor(websocket)
        who = (user, ip if ctx.is_anonymous(websocket) else "")
        state.view_records.open(ip, user, action, f"{mapping.name} ({detail})",
                                key=(*who, action, mapping.id))

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
        # the mapping's runner as it is now, with nothing awaited until the monitor is in:
        # a restart while the handshake went on replaced the one looked up above
        runner = state.supervisor.get_runner(mid)
        if runner is None:  # deleted meanwhile
            await websocket.close(code=1011)
            return
        mon = _Monitor()
        runner.add_monitor(mon)
        # viewers get an observe-only console; writing to the device needs operator+
        interactive = console_interactive(ws_user, mapping)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] console opened by {ws_user.username} ({peer})")
        _record_open(websocket, "console_open", mapping,
                     "interactive" if interactive else "read-only")

        async def sender():
            while True:
                chunks, ended = await mon.take()
                for data in chunks:
                    await websocket.send_bytes(data)
                if ended:  # mapping stopped
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    return

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
        runner = state.supervisor.get_runner(mid)  # as it is now (see the console's)
        if runner is None:
            await websocket.close(code=1011)
            return
        to_device = sees_to_device(ws_user, mapping)
        mon = _TrafficMonitor(to_device=to_device)
        runner.add_traffic_monitor(mon)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] hex view opened by {ws_user.username} ({peer})")
        _record_open(websocket, "hex_view_open", mapping,
                     "both directions" if to_device else "device output only")

        async def sender():
            while True:
                records, ended = await mon.take()
                for direction, ts, data in records:
                    await websocket.send_bytes(struct.pack(">Bd", direction, ts) + data)
                if ended:  # mapping stopped
                    return

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


class _Feed:
    """What one browser viewer has not been sent yet. The bridges put() into it
    synchronously from their read loops, best-effort: when it is full the oldest item is
    dropped, so a slow viewer never stalls a bridge. Its end is a flag, not an item in the
    queue, so a full feed can't lose it: the sender sends what is left, then stops."""

    MAX_ITEMS = 4096

    def __init__(self) -> None:
        self._items: collections.deque = collections.deque()
        self._ready = asyncio.Event()  # something to take, or closed
        self.closed = False

    def put(self, item) -> None:
        if self.closed:
            return
        self._items.append(item)
        if len(self._items) > self.MAX_ITEMS:
            self._items.popleft()
        self._ready.set()

    def close(self) -> None:
        """The runner stopped (or let the viewer go): no more items come."""
        self.closed = True
        self._ready.set()

    async def take(self) -> tuple[list, bool]:
        """What to send next, once there is something: (items, ended). `ended`: the feed
        is closed and nothing is left after these."""
        while not self._items and not self.closed:
            self._ready.clear()
            await self._ready.wait()
        items = list(self._items)
        self._items.clear()
        return items, self.closed


class _Monitor(_Feed):
    """A browser console observer: what the device sends (runner.add_monitor)."""

    MAX_ITEMS = 2048

    def feed(self, data: bytes) -> None:
        self.put(data)


class _TrafficMonitor(_Feed):
    """A hex-view observer: (direction, time, bytes) items (runner.add_traffic_monitor).
    Without `to_device`, what is sent to the device is left out."""

    def __init__(self, *, to_device: bool) -> None:
        super().__init__()
        self.to_device = to_device

    def feed_traffic(self, direction: int, data: bytes) -> None:
        if direction == TO_DEVICE and not self.to_device:
            return
        self.put((direction, time.time(), bytes(data)))
