"""Per-mapping bridge runner: TCP listener + shared serial endpoint + clients.

One ``MappingRunner`` per configured mapping owns:
  - an asyncio TCP server bound to the mapping's IP:port,
  - a supervised serial endpoint (opened and kept open, auto-reconnecting),
  - a set of connected ``_ClientConn`` objects.

Data model: a single serial port is shared by up to ``max_connections`` TCP
clients. Bytes read from serial are broadcast to every client (each through its
own protocol session); bytes from any client are written to the shared serial
port. Each client has its OWN bounded outbound queue + writer task, so a slow
client cannot stall serial reads for the others (per-client backpressure
isolation). Network->serial backpressure is handled by awaiting the serial
writer's drain() before reading more from that client.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# asyncio.TaskGroup and exception groups are native on Python 3.11+. On 3.10 we
# use the pure-Python backports (bundled in vendor/wheels: taskgroup +
# exceptiongroup), which preserve TaskGroup semantics exactly. BaseExceptionGroup
# is a builtin on 3.11+, so it is only imported on the backport path.
if sys.version_info >= (3, 11):
    from asyncio import TaskGroup
else:  # pragma: no cover - exercised only on Python 3.10
    from taskgroup import TaskGroup
    from exceptiongroup import BaseExceptionGroup

from ..config import MappingConfig
from . import serial_io
from .protocols import make_session

READ_CHUNK = 4096
CLIENT_QUEUE_MAX = 2048  # outbound chunks buffered per client before it's dropped


def _iter_leaf_exceptions(eg: BaseExceptionGroup):
    """Yield the individual (non-group) exceptions from a possibly-nested group."""
    for exc in eg.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            yield from _iter_leaf_exceptions(exc)
        else:
            yield exc


def fmt_duration(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class Tracer:
    """Append serial traffic to a per-mapping trace file (optional hex + timestamp)."""

    def __init__(self, options) -> None:
        self.path = options.trace_both
        self._hex = options.trace_hexdump
        self._ts = options.trace_timestamp
        self._fh = None
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            self._fh = open(self.path, "ab")
        except OSError:
            self._fh = None

    def write(self, direction: str, data: bytes) -> None:
        if not self._fh:
            return
        prefix = (time.strftime("%Y-%m-%d %H:%M:%S ") if self._ts else "")
        try:
            if self._hex:
                self._fh.write(f"{prefix}{direction} [{len(data)}] {data.hex(' ')}\n".encode())
            else:
                self._fh.write(prefix.encode() + direction.encode() + b" " + data + b"\n")
            self._fh.flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._fh:
            with contextlib.suppress(Exception):
                self._fh.close()
            self._fh = None


@dataclass
class RunnerStatus:
    state: str = "stopped"  # stopped|running|reconnecting|device-missing|error
    device: str = ""
    client_count: int = 0
    # connected peers: each {"peer": "ip:port", "connected_at": unix_ts}
    clients: list[dict] = field(default_factory=list)
    bytes_in: int = 0   # from serial -> network
    bytes_out: int = 0  # from network -> serial
    reconnects: int = 0
    dropped_clients: int = 0   # clients dropped because their output queue overflowed
    queue_overflows: int = 0   # total serial->net chunks lost to queue overflow
    last_error: str = ""

    def as_dict(self) -> dict:
        now = time.time()
        clients = [
            {"peer": c["peer"], "duration": fmt_duration(now - c["connected_at"]),
             "priority": c.get("priority", False)}
            for c in self.clients
        ]
        return {
            "state": self.state,
            "device": self.device,
            "client_count": self.client_count,
            "clients": clients,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "reconnects": self.reconnects,
            "dropped_clients": self.dropped_clients,
            "queue_overflows": self.queue_overflows,
            "last_error": self.last_error,
        }


def _render_banner(template: str, mapping: MappingConfig, device: str, peer: str) -> bytes:
    s = (
        template.replace("\\d", device)
        .replace("\\N", mapping.name)
        .replace("\\p", str(mapping.network.port))
        .replace("\\I", peer)
        .replace("\\r", "\r")
        .replace("\\n", "\n")
    )
    return s.encode("utf-8", "replace")


class _ClientConn:
    def __init__(self, runner: "MappingRunner", reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, peer_ip: str, peer_port: int = 0,
                 priority: bool = False):
        self.runner = runner
        self.reader = reader
        self.writer = writer
        self.peer_ip = peer_ip
        self.peer_port = peer_port
        self.peer = f"{peer_ip}:{peer_port}" if peer_port else peer_ip
        self.priority = priority
        self.connected_at = time.time()
        self._queue_max = runner.mapping.network.client_queue_max or CLIENT_QUEUE_MAX
        self._out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._queue_max)
        self._session = None
        self._last_activity = time.monotonic()
        self._closing = False

    def _log_unexpected(self, exc: BaseException) -> None:
        self.runner._log(f"client {self.peer} pump error: {type(exc).__name__}: {exc}")

    # called synchronously from the runner's serial read loop (broadcast)
    def feed_from_serial(self, data: bytes) -> None:
        if self._closing or self._session is None:
            return
        try:
            net = self._session.from_serial(data)
        except Exception as e:
            self.runner._log(f"protocol error (from_serial) for {self.peer}: "
                             f"{type(e).__name__}: {e}")
            return
        if not net:
            return
        try:
            self._out.put_nowait(net)
        except asyncio.QueueFull:
            # slow client: protect the others by dropping this one. This loses the
            # current chunk for THIS client only — surface it instead of hiding it.
            self._closing = True
            self.runner.status.queue_overflows += 1
            self.runner.status.dropped_clients += 1
            self.runner._log(f"dropping slow client {self.peer}: output queue full "
                             f"({self._queue_max} chunks) — data lost for this client")
            with contextlib.suppress(Exception):
                self.writer.transport.abort()

    async def run(self) -> None:
        m = self.runner.mapping
        # Create the protocol session. RFC2217 needs the live serial instance.
        ser = self.runner.serial_instance
        self._session = make_session(m.network.protocol, ser,
                                     poll_interval=m.options.rfc2217_poll_modem_interval_s,
                                     read_only=m.network.read_only)

        # initial bytes: protocol negotiation, then banner
        init = bytearray(self._session.initial_net_bytes())
        if m.options.banner:
            init += _render_banner(m.options.banner, m, self.runner.status.device, self.peer_ip)
        if init:
            self.writer.write(bytes(init))
            await self.writer.drain()

        try:
            async with TaskGroup() as tg:
                tg.create_task(self._pump_net_to_serial())
                tg.create_task(self._pump_serial_to_net())
                if self._session.poll_interval:
                    tg.create_task(self._pump_poll())
                if m.options.idle_timeout_s > 0:
                    tg.create_task(self._idle_guard(m.options.idle_timeout_s))
        except BaseExceptionGroup as eg:
            # Mirror the original `except*` split: connection-reset paths are a
            # normal disconnect (swallow them); log every other Exception; let
            # anything that is not an Exception (e.g. CancelledError) propagate.
            _normal = (ConnectionError, asyncio.IncompleteReadError, OSError)
            _, rest = eg.split(_normal)
            if rest is not None:
                unexpected, base_only = rest.split(Exception)
                if unexpected is not None:
                    for exc in _iter_leaf_exceptions(unexpected):
                        self._log_unexpected(exc)
                if base_only is not None:
                    raise base_only from None
        finally:
            self._closing = True
            with contextlib.suppress(Exception):
                self.writer.close()
                await self.writer.wait_closed()

    async def _pump_net_to_serial(self) -> None:
        m = self.runner.mapping
        while not self._closing:
            data = await self.reader.read(READ_CHUNK)
            if not data:
                break  # client closed
            self._last_activity = time.monotonic()
            serial_bound = self._session.from_network(data)
            ctrl = self._session.take_net_out()
            if ctrl:
                with contextlib.suppress(asyncio.QueueFull):
                    self._out.put_nowait(ctrl)
            if serial_bound and not m.network.read_only:
                await self.runner.serial_write(serial_bound)
                self.runner.status.bytes_out += len(serial_bound)
        raise ConnectionError("client closed")

    async def _pump_serial_to_net(self) -> None:
        while True:
            data = await self._out.get()
            if data is None:
                # graceful-close sentinel (e.g. closeon): flush queued bytes, then close
                while not self._out.empty():
                    d = self._out.get_nowait()
                    if d:
                        self.writer.write(d)
                await self.writer.drain()
                raise ConnectionError("closed by closeon")
            self.writer.write(data)
            await self.writer.drain()
            self._last_activity = time.monotonic()
            self.runner.status.bytes_in += len(data)

    async def _pump_poll(self) -> None:
        interval = self._session.poll_interval or 1.0
        while not self._closing:
            await asyncio.sleep(interval)
            extra = self._session.poll()
            if extra:
                with contextlib.suppress(asyncio.QueueFull):
                    self._out.put_nowait(extra)

    async def _idle_guard(self, timeout: float) -> None:
        while not self._closing:
            await asyncio.sleep(1.0)
            if time.monotonic() - self._last_activity > timeout:
                raise ConnectionError("idle timeout")

    def kick(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):
            self.writer.transport.abort()

    def request_graceful_close(self) -> None:
        """Stop feeding new serial data, but let already-queued bytes flush to the
        client before closing (used by closeon so the triggering data is delivered)."""
        if self._closing:
            return
        self._closing = True
        try:
            self._out.put_nowait(None)  # sentinel handled by _pump_serial_to_net
        except asyncio.QueueFull:
            with contextlib.suppress(Exception):
                self.writer.transport.abort()


class MappingRunner:
    def __init__(self, mapping: MappingConfig, logger=None):
        self.mapping = mapping
        self.status = RunnerStatus(state="stopped")
        self.log = logger
        self.serial_instance = None
        self._swriter: Optional[asyncio.StreamWriter] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._serial_task: Optional[asyncio.Task] = None
        self._main_task: Optional[asyncio.Task] = None       # client/udp/serialbridge driver
        self._udp_transport = None
        self._tracer: Optional[Tracer] = None
        self._clients: set = set()
        self._monitors: set = set()  # browser console observers (read traffic)
        self._mqtt = None            # optional MQTT publisher (serial -> broker)
        self._serial_ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._allowed_nets = self._parse_allowed(mapping.network.allowed_client_ips)
        self._priority_nets = self._parse_allowed(mapping.network.priority_client_ips)
        co = mapping.options.closeon
        self._closeon = co.encode("utf-8", "replace") if co else b""
        self._closeon_tail = b""  # rolling carry-over so matches across reads aren't missed

    @staticmethod
    def _parse_allowed(cidrs: list[str]):
        from ..config import normalize_cidr

        nets = []
        for c in cidrs:
            with contextlib.suppress(ValueError):
                nets.append(ipaddress.ip_network(normalize_cidr(c), strict=False))
        return nets

    def _log(self, msg: str) -> None:
        # The logger (a per-mapping logger from AppState) adds the "[name]" prefix
        # for the global all.log and also writes to this mapping's own log file.
        if self.log:
            self.log(msg)

    # ---------------- console monitors (browser xterm) ----------------
    def add_monitor(self, mon) -> None:
        self._monitors.add(mon)

    def discard_monitor(self, mon) -> None:
        self._monitors.discard(mon)

    def _feed_monitors(self, data: bytes) -> None:
        for mon in list(self._monitors):
            mon.feed(data)

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._stop.clear()
        if self.mapping.options.trace_both:
            self._tracer = Tracer(self.mapping.options)

        if self.mapping.kind == "serialbridge":
            self._main_task = asyncio.create_task(
                self._serialbridge_supervisor(), name=f"serbridge:{self.mapping.name}")
            self.status.state = "running"
            self._log("serial-to-serial bridge starting")
            return

        # optional MQTT publisher: serial lines -> broker (net mappings)
        if self.mapping.mqtt.enabled:
            from .mqtt_pub import MqttPublisher
            self._mqtt = MqttPublisher(self.mapping.mqtt, logger=self._log)
            self._mqtt.connect()

        net = self.mapping.network
        # serial side is shared by server/client/udp
        self._serial_task = asyncio.create_task(
            self._serial_supervisor(), name=f"serial:{self.mapping.name}")

        # If the network side fails to come up (address-in-use, bad TLS cert, ...),
        # tear everything down — otherwise the serial supervisor task is orphaned and
        # keeps the serial device open forever, so no other mapping can use that port.
        try:
            if net.mode == "server":
                self._server = await asyncio.start_server(
                    self._on_client, host=net.bind_ip, port=net.port, ssl=self._server_ssl())
                self._log(f"listening on {net.bind_ip}:{net.port} ({net.protocol}"
                          f"{', TLS' if net.tls else ''})")
            elif net.mode == "client":
                self._main_task = asyncio.create_task(
                    self._client_supervisor(), name=f"connectout:{self.mapping.name}")
                self._log(f"connect-out to {net.remote_host}:{net.remote_port} ({net.protocol})")
            elif net.mode == "udp":
                loop = asyncio.get_running_loop()
                self._udp_transport, _ = await loop.create_datagram_endpoint(
                    lambda: _UdpBridge(self), local_addr=(net.bind_ip, net.port))
                self._log(f"UDP on {net.bind_ip}:{net.port}")
        except BaseException:
            await self.stop()
            raise
        self.status.state = "running"

    async def stop(self) -> None:
        self._stop.set()
        # Stop accepting new connections first (but do NOT await wait_closed yet).
        if self._server is not None:
            self._server.close()
        if self._udp_transport is not None:
            with contextlib.suppress(Exception):
                self._udp_transport.close()
            self._udp_transport = None
        # Abort existing clients/monitors BEFORE awaiting the listener teardown:
        # on Python 3.12+ asyncio.Server.wait_closed() blocks until every active
        # connection has finished, so awaiting it before kicking clients would
        # deadlock a stop() while clients are still connected.
        for c in list(self._clients):
            c.kick()
        for mon in list(self._monitors):
            mon.close()
        self._monitors.clear()
        if self._mqtt is not None:
            self._mqtt.close()
            self._mqtt = None
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            self._server = None
        for task in (self._serial_task, self._main_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._serial_task = self._main_task = None
        await self._close_serial()
        if self._tracer:
            self._tracer.close()
            self._tracer = None
        self.status.state = "stopped"
        self.status.client_count = 0
        self.status.clients = []
        self._log("stopped")

    # ssl contexts for per-mapping TLS data bridges
    def _server_ssl(self):
        net = self.mapping.network
        if not net.tls:
            return None
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(net.tls_cert, net.tls_key)
        return ctx

    def _client_ssl(self):
        net = self.mapping.network
        if not net.tls:
            return None
        import ssl
        ctx = ssl.create_default_context()
        # data-bridge TLS: encrypt the link; peer cert is typically self-signed, so
        # don't hard-fail verification (this is opportunistic transport encryption).
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ---------------- serial side ----------------
    async def serial_write(self, data: bytes) -> None:
        if self._swriter is None:
            return  # serial not currently open; drop (status shows reconnecting)
        if self._tracer:
            self._tracer.write("net>ser", data)
        self._swriter.write(data)
        await self._swriter.drain()

    async def _close_serial(self) -> None:
        self._serial_ready.clear()
        w = self._swriter
        self._swriter = None
        self.serial_instance = None
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()
                await w.wait_closed()

    async def _serial_supervisor(self) -> None:
        backoff = 0.5
        attempt = 0
        while not self._stop.is_set():
            try:
                reader, writer, ser, device = await serial_io.open_serial(self.mapping.serial)
                self._swriter = writer
                self.serial_instance = ser
                self.status.device = device
                self.status.last_error = ""
                self.status.state = "running"
                self._serial_ready.set()
                backoff = 0.5
                attempt = 0
                self._log(f"serial open: {device} @ {self.mapping.serial.compact()}")
                # optional open string to device
                if self.mapping.options.openstr:
                    writer.write(self.mapping.options.openstr.encode("utf-8", "replace"))
                    await writer.drain()
                await self._serial_read_loop(reader)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # SerialException, OSError, etc.
                self.status.last_error = str(e)
                self.status.state = "device-missing" if attempt == 0 else "reconnecting"
                self._log(f"serial error: {e}")
            finally:
                await self._close_serial()
            if self._stop.is_set():
                break
            attempt += 1
            self.status.reconnects += 1
            self.status.state = "reconnecting"
            # backoff with light jitter (derived from attempt, not RNG)
            delay = min(backoff, 10.0) + (attempt % 5) * 0.1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break  # stop requested during backoff
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)

    async def _serial_read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stop.is_set():
            data = await reader.read(READ_CHUNK)
            if not data:
                raise ConnectionError("serial EOF / device removed")
            if self._tracer:
                self._tracer.write("ser>net", data)
            self._feed_monitors(data)
            if self._mqtt is not None:
                self._mqtt.feed(data)
            for c in list(self._clients):
                c.feed_from_serial(data)
            if self._closeon:
                self._check_closeon(data)

    def _check_closeon(self, data: bytes) -> None:
        window = self._closeon_tail + data
        if self._closeon in window:
            self._log(f"closeon matched {self._closeon!r} — closing {len(self._clients)} client(s)")
            for c in list(self._clients):
                c.request_graceful_close()
            self._closeon_tail = b""
        elif len(self._closeon) > 1:
            self._closeon_tail = window[-(len(self._closeon) - 1):]

    # ---------------- network side ----------------
    @staticmethod
    def _ip_in_nets(ip: str, nets) -> bool:
        if not nets:
            return False
        with contextlib.suppress(ValueError):
            addr = ipaddress.ip_address(ip)
            # treat IPv4-mapped IPv6 (::ffff:1.2.3.4) as the underlying IPv4
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            return any(addr in net for net in nets)
        return False

    def _client_allowed(self, ip: str) -> bool:
        # empty allow-list = allow everyone
        return True if not self._allowed_nets else self._ip_in_nets(ip, self._allowed_nets)

    def _pick_victim(self):
        """Choose a client to evict when making room: oldest non-priority first,
        falling back to the oldest client overall."""
        if not self._clients:
            return None
        non_priority = [c for c in self._clients if not c.priority]
        pool = non_priority or list(self._clients)
        return min(pool, key=lambda c: c.connected_at)

    def _refresh_clients(self) -> None:
        self.status.client_count = len(self._clients)
        self.status.clients = sorted(
            ({"peer": c.peer, "connected_at": c.connected_at, "priority": c.priority}
             for c in self._clients),
            key=lambda d: d["connected_at"],
        )

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("?", 0)
        if isinstance(peername, tuple):
            peer_ip, peer_port = str(peername[0]), int(peername[1]) if len(peername) > 1 else 0
        else:
            peer_ip, peer_port = str(peername), 0
        peer_label = f"{peer_ip}:{peer_port}" if peer_port else peer_ip
        net = self.mapping.network

        if not self._client_allowed(peer_ip):
            self._log(f"rejected {peer_label}: not in allowed list")
            writer.close()
            return

        is_priority = self._ip_in_nets(peer_ip, self._priority_nets)

        if len(self._clients) >= net.max_connections:
            # A priority client always gets in (kick to make room); a normal client
            # gets in only if kick_old_user is set; otherwise it's refused.
            if is_priority or net.kick_old_user:
                victim = self._pick_victim()
                if victim is not None:
                    why = "priority client" if is_priority else "new client"
                    self._log(f"kicking {victim.peer} for {why} {peer_label}")
                    victim.kick()
            else:
                self._log(f"rejected {peer_label}: max connections reached")
                with contextlib.suppress(Exception):
                    writer.write(b"Busy: maximum connections reached.\r\n")
                    await writer.drain()
                writer.close()
                return

        # For RFC2217 we need the live serial instance; wait briefly for it.
        if net.protocol == "rfc2217" and not self._serial_ready.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._serial_ready.wait(),
                                       timeout=self.mapping.options.rfc2217_net_timeout_s)
            if not self._serial_ready.is_set():
                self._log(f"rejected {peer_label}: serial not ready for RFC2217")
                writer.close()
                return

        client = _ClientConn(self, reader, writer, peer_ip, peer_port, priority=is_priority)
        self._clients.add(client)
        self._refresh_clients()
        tag = " [priority]" if is_priority else ""
        self._log(f"client connected: {peer_label}{tag} ({len(self._clients)} total)")
        try:
            await client.run()
        finally:
            self._clients.discard(client)
            self._refresh_clients()
            held = fmt_duration(time.time() - client.connected_at)
            self._log(f"client disconnected: {peer_label} after {held} ({len(self._clients)} total)")
            # closestr to device when the last client leaves
            if not self._clients and self.mapping.options.closestr and self._swriter:
                with contextlib.suppress(Exception):
                    self._swriter.write(self.mapping.options.closestr.encode("utf-8", "replace"))
                    await self._swriter.drain()

    # ---------------- connect-out (TCP client) ----------------
    async def _client_supervisor(self) -> None:
        net = self.mapping.network
        backoff, attempt = 0.5, 0
        while not self._stop.is_set():
            try:
                reader, writer = await asyncio.open_connection(
                    net.remote_host, net.remote_port, ssl=self._client_ssl())
                client = _ClientConn(self, reader, writer, net.remote_host, net.remote_port)
                self._clients.add(client)
                self._refresh_clients()
                self._log(f"connected out to {net.remote_host}:{net.remote_port}")
                backoff, attempt = 0.5, 0
                try:
                    await client.run()
                finally:
                    self._clients.discard(client)
                    self._refresh_clients()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.last_error = str(e)
                self._log(f"connect-out failed: {e}")
            if self._stop.is_set():
                break
            attempt += 1
            delay = min(backoff, 10.0) + (attempt % 5) * 0.1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)

    # ---------------- serial <-> serial bridge ----------------
    async def _serialbridge_supervisor(self) -> None:
        backoff, attempt = 0.5, 0
        while not self._stop.is_set():
            wa = wb = None
            try:
                ra, wa, _sa, da = await serial_io.open_serial(self.mapping.serial)
                rb, wb, _sb, db = await serial_io.open_serial(self.mapping.serial_b)
                self.status.device = f"{da} <-> {db}"
                self.status.last_error = ""
                self.status.state = "running"
                backoff, attempt = 0.5, 0
                self._log(f"serial bridge open: {da} <-> {db}")
                async with TaskGroup() as tg:
                    tg.create_task(self._pump_serial_pair(ra, wb, "A>B"))
                    tg.create_task(self._pump_serial_pair(rb, wa, "B>A"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # TaskGroup raises an ExceptionGroup; flatten its messages
                inner = getattr(e, "exceptions", None)
                self.status.last_error = ("; ".join(str(x) for x in inner)
                                          if inner else str(e))
                self.status.state = "reconnecting" if attempt else "device-missing"
                self._log(f"serial bridge error: {self.status.last_error}")
            finally:
                for w in (wa, wb):
                    if w is not None:
                        with contextlib.suppress(Exception):
                            w.close()
                            await w.wait_closed()
            if self._stop.is_set():
                break
            attempt += 1
            self.status.reconnects += 1
            self.status.state = "reconnecting"
            delay = min(backoff, 10.0) + (attempt % 5) * 0.1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)

    async def _pump_serial_pair(self, reader, writer, tag: str) -> None:
        while not self._stop.is_set():
            data = await reader.read(READ_CHUNK)
            if not data:
                raise ConnectionError(f"serial EOF ({tag})")
            if self._tracer:
                self._tracer.write(tag, data)
            self._feed_monitors(data)
            writer.write(data)
            await writer.drain()
            self.status.bytes_in += len(data)


# ---------------------------------------------------------------------------
# UDP transport (datagram bridge). The serial read loop broadcasts to _clients,
# so a _UdpPeer sink in _clients carries serial->net; datagram_received carries
# net->serial. One peer is tracked (the last sender).
# ---------------------------------------------------------------------------
class _UdpPeer:
    def __init__(self, runner: "MappingRunner", addr) -> None:
        self.runner = runner
        self.addr = addr
        self.peer = f"{addr[0]}:{addr[1]}"
        self.priority = False
        self.connected_at = time.time()

    def feed_from_serial(self, data: bytes) -> None:
        t = self.runner._udp_transport
        if t is not None and self.addr is not None:
            with contextlib.suppress(Exception):
                t.sendto(data, self.addr)
            self.runner.status.bytes_in += len(data)

    def kick(self) -> None:
        pass

    def request_graceful_close(self) -> None:
        pass


class _UdpBridge(asyncio.DatagramProtocol):
    def __init__(self, runner: "MappingRunner") -> None:
        self.runner = runner

    def datagram_received(self, data: bytes, addr) -> None:
        r = self.runner
        # Access control: a TCP listener checks _client_allowed() in _on_client, but
        # UDP has no accept hook — enforce the allow-list HERE. An unlisted source is
        # dropped entirely, so a stray or spoofed datagram cannot become "the peer"
        # and hijack the serial->net stream (this also protects read-only mappings).
        if not r._client_allowed(addr[0]):
            return
        peer = next((c for c in r._clients if isinstance(c, _UdpPeer)), None)
        if peer is None:
            peer = _UdpPeer(r, addr)
            r._clients.add(peer)
            r._refresh_clients()
            r._log(f"udp peer {addr[0]}:{addr[1]}")
        elif peer.addr != addr:
            peer.addr = addr
            peer.peer = f"{addr[0]}:{addr[1]}"
            r._refresh_clients()
        if r.mapping.network.read_only:
            return
        if r._tracer:
            r._tracer.write("net>ser", data)
        if r._swriter is not None:
            with contextlib.suppress(Exception):
                r._swriter.write(data)  # datagram_received is sync; transport buffers
            r.status.bytes_out += len(data)
