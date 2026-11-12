"""Live traffic in the browser over WebSockets: the interactive console (xterm.js;
what the device sends, and typing to it) and the read-only traffic feed of the hex
view (timestamped; what is sent to the device only for operators and admins).

What a viewer has not been sent yet waits in its _Feed, and goes out in batches: what
arrives within FLUSH_S of a chunk goes in one WebSocket message with it.

The page asks for protocol 2 with ?v=2 (a socket without it gets what 2.8 sent):
  - text messages from the server are notices, JSON: {"dropped": n} — n bytes of traffic
    were dropped before what comes next (the viewer did not keep up);
  - the hex view packs its records, [direction u8][time f64 BE][length u32 BE][bytes]
    one after the other (version 1: one record per message, without the length).
"""
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
from ...engine.netguard import LogEvery

CONSOLE_ACCESS_RECHECK_S = 2.0  # how often an open console re-checks its access
# Consoles and hex views open at once (counted together): per account — an open-access
# visitor: per address — and mapping, per mapping, and in all. One more is accepted, then
# closed with CLOSE_BUSY and the limit it is over.
VIEWERS_PER_USER = 4
VIEWERS_PER_MAPPING = 16
VIEWERS_TOTAL = 64
# What a viewer has not been sent yet (_Feed): at most FEED_MAX_BYTES of traffic in at
# most FEED_MAX_ITEMS chunks, the oldest dropped first; taken FRAME_MAX_BYTES at a time. A
# viewer whose sending has been stuck for STALL_S while its traffic is being dropped
# takes nothing: it is closed (CLOSE_SLOW), so the bridges stop feeding it.
FEED_MAX_BYTES = 1024 * 1024
FEED_MAX_ITEMS = 4096
FRAME_MAX_BYTES = 64 * 1024
FLUSH_S = 0.016  # about one screen refresh: what comes this soon after a chunk goes with it
STALL_S = 30.0
CLOSE_WAIT_S = 5.0  # a close the client takes nothing of is given up after this long
# close codes after the handshake
CLOSE_BUSY = 1013  # past a VIEWERS_* limit ("try again later")
CLOSE_SLOW = 4003  # took nothing for STALL_S while its traffic was being dropped


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

    # ---------------- the viewers: consoles and hex views ----------------
    viewers = _Viewers()
    refused = LogEvery(lambda msg: state.log(msg))  # past a VIEWERS_* limit: once a minute

    def _who(websocket) -> tuple:
        """Whose viewer it is, for the limits and the audit records: the account — an
        open-access visitor: per address."""
        return ctx.actor(websocket), client_ip(websocket) if ctx.is_anonymous(websocket) else ""

    def _record_open(websocket, action: str, mapping, detail: str) -> None:
        """The audit record of a console or hex view opened, bounded (AppState.view_records):
        per account — an open-access visitor: per address —, mapping and kind."""
        state.view_records.open(client_ip(websocket), ctx.actor(websocket), action,
                                f"{mapping.name} ({detail})",
                                key=(*_who(websocket), action, mapping.id))

    async def _admit(websocket, kind: str) -> _Accepted | None:
        """The handshake of a console or hex view (`kind`, for the log): accepted and
        counted in (_end counts it out), or None once it has been refused and closed."""
        ws_user, token = await _authorize(websocket)
        if ws_user is None:
            return None
        mid = websocket.path_params["mid"]
        mapping = state.config.get_mapping(mid)
        if state.supervisor.get_runner(mid) is None or mapping is None:
            await websocket.close(code=1011)
            return None
        await websocket.accept()
        # the mapping's runner as it is now, with nothing awaited until the monitor is in:
        # a restart while the handshake went on replaced the one looked up above
        runner = state.supervisor.get_runner(mid)
        if runner is None:  # deleted meanwhile
            await _close(websocket, 1011)
            return None
        peer = websocket.client.host if websocket.client else "?"
        who = _who(websocket)
        if why := viewers.admit(who, mid):
            refused(f"[{mapping.name}] {kind} of {ws_user.username} ({peer}) refused: {why}")
            await _close(websocket, CLOSE_BUSY, why)
            return None
        return _Accepted(ws_user, token, mapping, runner, peer, who)

    async def _serve(websocket, v: _Accepted, mon, sender, receiver) -> tuple[int, str]:
        """Serve an accepted viewer until it ends: `sender` returns (its feed ended: the
        mapping stopped), `receiver` returns (the browser closed it), its access changed,
        or it stalled (_Feed.stalled). The close code and reason for that."""

        async def access_watch():
            # A viewer outlives the check at connect: end it once its access is gone (login
            # switched back on, session revoked by a password change, role changed) instead
            # of letting it keep watching — or writing to — the device.
            while True:
                await asyncio.sleep(CONSOLE_ACCESS_RECHECK_S)
                now = auth.request_user(state.config, v.token, websocket.headers,
                                        state.revoked_sessions)
                if now is None or now.role != v.user.role:
                    return

        ends = {asyncio.create_task(sender()): (1000, ""),
                asyncio.create_task(receiver()): None,  # nothing more to say to it
                asyncio.create_task(access_watch()): (1000, "access changed"),
                asyncio.create_task(mon.stalled.wait()):
                    (CLOSE_SLOW, f"took nothing for {STALL_S:g} s")}
        try:
            done, _ = await asyncio.wait(ends, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in ends:
                t.cancel()
            for t in ends:
                with contextlib.suppress(BaseException):
                    await t
        for t in done:
            end = ends[t]
            if end and not t.cancelled() and t.exception() is None:
                return end
        return 1000, ""

    async def _end(websocket, kind: str, v: _Accepted, code: int, reason: str) -> None:
        """After its monitor is gone: count the viewer out and close it, saying why."""
        viewers.leave(v.who, v.mapping.id)
        await _close(websocket, code, reason)
        state.log(f"[{v.mapping.name}] {kind} closed ({v.peer})" + (f": {reason}" if reason else ""))

    # ---------------- console (xterm over WebSocket) ----------------
    async def console_ws(websocket):
        v = await _admit(websocket, "console")
        if v is None:
            return
        # viewers get an observe-only console; writing to the device needs operator+
        interactive = console_interactive(v.user, v.mapping)
        mon, (code, reason) = _Monitor(), (1000, "")
        v2 = websocket.query_params.get("v") == "2"

        async def sender():
            while True:
                chunks, dropped, ended = await mon.take()
                if dropped and v2:
                    await websocket.send_json({"dropped": dropped})
                if chunks:
                    await websocket.send_bytes(b"".join(chunks))
                    mon.sent()
                if ended:  # mapping stopped
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
                    await v.runner.serial_write(data)

        try:
            v.runner.add_monitor(mon)
            state.log(f"[{v.mapping.name}] console opened by {v.user.username} ({v.peer})")
            _record_open(websocket, "console_open", v.mapping,
                         "interactive" if interactive else "read-only")
            code, reason = await _serve(websocket, v, mon, sender, receiver)
        finally:
            v.runner.discard_monitor(mon)
            await _end(websocket, "console", v, code, reason)

    # ---------------- traffic feed for the hex view (read-only) ----------------
    async def traffic_ws(websocket):
        """A mapping's traffic, as records [direction: 1 byte (0 = from the device, 1 = to
        it)][time: float64 BE][length: uint32 BE][bytes], several to a binary message
        (protocol 1: one per message, without the length; see the module docstring).
        Read-only for everyone who may open the console; what is sent to the device
        only for those who may see it (sees_to_device), the others never get it queued."""
        v = await _admit(websocket, "hex view")
        if v is None:
            return
        to_device = sees_to_device(v.user, v.mapping)
        mon, (code, reason) = _TrafficMonitor(to_device=to_device), (1000, "")
        v2 = websocket.query_params.get("v") == "2"

        async def sender():
            while True:
                records, dropped, ended = await mon.take()
                if dropped and v2:
                    await websocket.send_json({"dropped": dropped})
                if v2 and records:
                    await websocket.send_bytes(b"".join(
                        struct.pack(">BdI", direction, ts, len(data)) + data
                        for direction, ts, data in records))
                    mon.sent()
                elif records:
                    for direction, ts, data in records:
                        await websocket.send_bytes(struct.pack(">Bd", direction, ts) + data)
                        mon.sent()
                if ended:  # mapping stopped
                    return

        async def receiver():  # nothing is accepted from the browser; notice a close
            while (await websocket.receive()).get("type") != "websocket.disconnect":
                pass

        try:
            v.runner.add_traffic_monitor(mon)
            state.log(f"[{v.mapping.name}] hex view opened by {v.user.username} ({v.peer})")
            _record_open(websocket, "hex_view_open", v.mapping,
                         "both directions" if to_device else "device output only")
            code, reason = await _serve(websocket, v, mon, sender, receiver)
        finally:
            v.runner.discard_traffic_monitor(mon)
            await _end(websocket, "hex view", v, code, reason)

    return [
        WebSocketRoute("/api/mappings/{mid}/console", console_ws),
        WebSocketRoute("/api/mappings/{mid}/traffic", traffic_ws),
    ]


class _Accepted:
    """A console or hex view past its handshake (routes._admit): who opened it (`user`,
    the session `token`; `peer`: its address, `who`: the key it is counted by) and the
    mapping and runner it watches."""

    __slots__ = ("user", "token", "mapping", "runner", "peer", "who")

    def __init__(self, user, token, mapping, runner, peer: str, who: tuple) -> None:
        self.user, self.token, self.mapping, self.runner = user, token, mapping, runner
        self.peer, self.who = peer, who


async def _close(websocket, code: int = 1000, reason: str = "") -> None:
    """Close a WebSocket, giving up after CLOSE_WAIT_S: a client that takes nothing would
    hold the close, and the handler, for good."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(websocket.close(code=code, reason=reason), CLOSE_WAIT_S)


class _Viewers:
    """The consoles and hex views open, counted per account (an open-access visitor: per
    address) and mapping, per mapping and in all: the VIEWERS_* limits."""

    def __init__(self) -> None:
        self._open: collections.Counter = collections.Counter()

    @staticmethod
    def _keys(who: tuple, mid: str) -> tuple:
        return ("all",), ("mapping", mid), ("user", who, mid)

    def admit(self, who: tuple, mid: str) -> str:
        """Count one more viewer in: '' — or, past a limit, why not (it isn't counted)."""
        total, mapping, user = (self._open[k] for k in self._keys(who, mid))
        if total >= VIEWERS_TOTAL:
            return f"too many consoles and hex views open ({VIEWERS_TOTAL} in all)"
        if mapping >= VIEWERS_PER_MAPPING:
            return f"too many consoles and hex views of this mapping open ({VIEWERS_PER_MAPPING})"
        if user >= VIEWERS_PER_USER:
            return (f"too many consoles and hex views of this mapping open for one user "
                    f"({VIEWERS_PER_USER})")
        self._open.update(self._keys(who, mid))
        return ""

    def leave(self, who: tuple, mid: str) -> None:
        for k in self._keys(who, mid):
            self._open[k] -= 1
            if self._open[k] <= 0:
                del self._open[k]


class _Feed:
    """What one browser viewer has not been sent yet. The bridges put() into it
    synchronously from their read loops, best-effort: past FEED_MAX_BYTES of traffic or
    FEED_MAX_ITEMS chunks the oldest are dropped, so a slow viewer never stalls a bridge.
    Its end is a flag, not an item in the queue, so a full feed can't lose it: the sender
    sends what is left, then stops. A viewer whose sending has been stuck for STALL_S
    while its traffic is being dropped is `stalled`: it takes nothing."""

    def __init__(self) -> None:
        self._items: collections.deque = collections.deque()  # (item, bytes of traffic)
        self._bytes = 0
        self._ready = asyncio.Event()  # something to take, or closed
        self._busy_since: float | None = None  # sending what it took, since (monotonic)
        self._unsaid = 0  # bytes dropped since the last take()
        self.dropped = 0  # bytes of traffic dropped in all
        self.closed = False
        self.stalled = asyncio.Event()

    def put(self, item, size: int) -> None:
        if self.closed:
            return
        items = self._items
        items.append((item, size))
        self._bytes += size
        while self._bytes > FEED_MAX_BYTES or len(items) > FEED_MAX_ITEMS:
            _old, n = items.popleft()
            self._bytes -= n
            self.dropped += n
            self._unsaid += n
            busy = self._busy_since
            if busy is not None and time.monotonic() - busy > STALL_S:
                self.stalled.set()
        self._ready.set()

    def close(self) -> None:
        """The runner stopped (or let the viewer go): no more items come."""
        self.closed = True
        self._ready.set()

    async def take(self, max_bytes: int = FRAME_MAX_BYTES) -> tuple[list, int, bool]:
        """What to send next: (items, bytes dropped before them, ended). Waits for an item,
        then FLUSH_S for those right behind it (not when `max_bytes` are there already),
        and takes at most `max_bytes` of traffic, at least one item. `ended`: the feed is
        closed and nothing is left."""
        self._busy_since = None
        while not self._items and not self.closed:
            self._ready.clear()
            await self._ready.wait()
        if not self.closed and self._bytes < max_bytes:
            await asyncio.sleep(FLUSH_S)
        items, n = self._items, 0
        out: list = []
        while items and (not out or n + items[0][1] <= max_bytes):
            item, size = items.popleft()
            out.append(item)
            n += size
        self._bytes -= n
        dropped, self._unsaid = self._unsaid, 0
        self._busy_since = time.monotonic()
        return out, dropped, self.closed and not items

    def sent(self) -> None:
        """The sender got a message out: it is not stuck."""
        self._busy_since = time.monotonic()


class _Monitor(_Feed):
    """A browser console observer: what the device sends (runner.add_monitor)."""

    def feed(self, data: bytes) -> None:
        if data:
            self.put(data, len(data))


class _TrafficMonitor(_Feed):
    """A hex-view observer: (direction, time, bytes) items (runner.add_traffic_monitor).
    Without `to_device`, what is sent to the device is left out."""

    def __init__(self, *, to_device: bool) -> None:
        super().__init__()
        self.to_device = to_device

    def feed_traffic(self, direction: int, data: bytes) -> None:
        if not data or (direction == TO_DEVICE and not self.to_device):
            return
        self.put((direction, time.time(), bytes(data)), len(data))
