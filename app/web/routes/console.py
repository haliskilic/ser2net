"""Live traffic in the browser over WebSockets: the interactive console (xterm.js;
what the device sends, and typing to it) and the read-only traffic feed of the hex
view (timestamped; what is sent to the device only for operators and admins). And the
console's serial BREAK, an audited POST (not a WebSocket frame: those are typed data)."""
from __future__ import annotations

import asyncio
import contextlib
import struct
import time

from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute

from ...engine import netguard
from ...engine.bridge import TO_DEVICE
from ...i18n import _
from .. import auth, proxy
from .common import Ctx, client_ip, console_interactive, current_user, sees_to_device

# the protocols whose runner sends a serial BREAK (not a Modbus gateway: an RTU bus)
_BREAK_PROTOCOLS = ("raw", "telnet", "rfc2217")

CONSOLE_ACCESS_RECHECK_S = 2.0  # how often an open console re-checks its access


def routes(ctx: Ctx) -> list:
    state = ctx.state

    # refused WebSockets of another site's page: logged at most once a minute (a client
    # other than a browser can send any Origin)
    cross_site_note = netguard.LogEvery(lambda msg: state.log(msg))

    async def _authorize(websocket):
        """The user a WebSocket acts as, or None (after closing it). BaseHTTPMiddleware
        does NOT run for the WebSocket scope, so authenticate here; a WebSocket that a
        page of another site opens is rejected (CSWSH guard), by the same rule as the
        UI's forms (auth.cross_site): a reverse proxy that passes the browser's address
        as Host, X-Forwarded-Host or Forwarded host= works for both, and so does a page of
        the public URL (Settings -> Reverse proxy)."""
        token = auth.cookie(websocket, auth.SESSION_COOKIE)
        if why := auth.cross_site(websocket.headers,
                                  also=proxy.published_origins(state.config.admin_ui)):
            cross_site_note(f"refused WebSocket {websocket.url.path} from "
                            f"{client_ip(websocket)}: opened by a page of another site "
                            f"({why}); {auth.PROXY_HINT}")
            await websocket.close(code=1008)
            return None, token
        ws_user = auth.request_user(state.config, token, websocket.headers,
                                    state.revoked_sessions)
        if ws_user is None:
            await websocket.close(code=1008)
            # (one of another site's pages was refused above, with its own line)
            ip = client_ip(websocket)
            state.log_refused((ip, "websocket"),
                              f"WebSocket {websocket.url.path} from {ip} refused: "
                              "no valid session")
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
        mon = _Monitor()
        runner.add_monitor(mon)
        # viewers get an observe-only console; writing to the device needs operator+
        interactive = console_interactive(ws_user, mapping)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] console opened by {ws_user.username} ({peer})")
        _record_open(websocket, "console_open", mapping,
                     "interactive" if interactive else "read-only")
        written = [0]  # how many bytes were typed to the device (never what)

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
                    written[0] += len(data)

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
            if written[0]:  # who wrote to the device, and how much (bounded: console_writes)
                state.log(f"[{mapping.name}] console closed ({peer}; {written[0]} bytes written)")
                state.console_writes.add(client_ip(websocket), ctx.actor(websocket),
                                         "console_close",
                                         f"{mapping.name} ({written[0]} bytes written)")
            else:
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
        _record_open(websocket, "hex_view_open", mapping,
                     "both directions" if to_device else "device output only")

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

    # ---------------- serial BREAK (the console's "Send BREAK") ----------------
    async def send_break(request):
        """POST /api/mappings/{mid}/break: a serial BREAK on the mapping's port (as a telnet
        client's IAC BRK: the line held in the break condition for bridge.BREAK_S). For
        whoever may type in the console (operators and admins, a network mapping that is
        not read-only); audited as mapping_break. 204 when sent; 400 read-only or not a
        serial-to-network mapping, 409 the port is not open (or can't send one)."""
        if deny := ctx.require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        mapping = state.config.get_mapping(mid)
        runner = state.supervisor.get_runner(mid)
        if mapping is None or runner is None:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        if (mapping.kind != "net" or mapping.network.protocol not in _BREAK_PROTOCOLS
                or not hasattr(runner, "send_break")):
            return PlainTextResponse(_("A serial BREAK can't be sent on this mapping."),
                                     status_code=400)
        if not console_interactive(current_user(request), mapping):
            return PlainTextResponse(_("The mapping is read-only: no serial BREAK is sent."),
                                     status_code=400)
        if not await runner.send_break(f"web console, {ctx.actor(request)}"):
            return PlainTextResponse(_("No serial BREAK was sent: the serial port is not open, "
                                       "or it can't send one (see the mapping's log)."),
                                     status_code=409)
        ctx.audit(request, "mapping_break", mapping.name)
        return Response(status_code=204)

    return [
        WebSocketRoute("/api/mappings/{mid}/console", console_ws),
        WebSocketRoute("/api/mappings/{mid}/traffic", traffic_ws),
        Route("/api/mappings/{mid}/break", send_break, methods=["POST"]),
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
