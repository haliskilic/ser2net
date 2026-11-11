"""Per-mapping bridge runner: TCP listener + shared serial endpoint + clients.

One ``MappingRunner`` per configured mapping owns:
  - an asyncio TCP server bound to the mapping's IP:port,
  - a supervised serial endpoint (serial_link.SerialLink: opened and kept open,
    auto-reconnecting),
  - a set of connected ``_ClientConn`` objects.

Data model: a single serial port is shared by up to ``max_connections`` TCP
clients. Bytes read from serial are broadcast to every client (each through its
own protocol session); bytes from any client are written to the shared serial
port. Each client has its OWN bounded outbound queue + writer task, so a slow
client cannot stall serial reads for the others (per-client backpressure
isolation). Network->serial backpressure is handled by awaiting the serial
writer's drain() before reading more from that client.

Every TCP data socket (accepted or connect-out) gets TCP keepalive (sockopts.py), so
a peer that vanished without a FIN/RST is detected and its slot freed instead of
staying ESTABLISHED forever; each disconnect is logged with its reason.

A UDP mapping can't slow a sender down: its datagrams wait in a bounded queue
(UDP_BACKLOG_MAX) that is written to the port the same way, and a datagram that finds it
full is dropped and counted. The device's output goes to the last allowed sender, or
always to a configured reply address.

Traffic counters (RunnerStatus) count at the device, once: bytes_in what was read from
it, bytes_out what was handed to it, bytes_dropped what the network sent for it that
was not written (the port was not open, or the UDP queue was full).
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import os
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

# asyncio.TaskGroup and exception groups are native on Python 3.11+. On 3.10 we
# use the pure-Python backports (bundled in vendor/wheels: taskgroup +
# exceptiongroup), which preserve TaskGroup semantics exactly. BaseExceptionGroup
# is a builtin on 3.11+, so it is only imported on the backport path.
if sys.version_info >= (3, 11):
    from asyncio import TaskGroup
else:  # pragma: no cover - exercised only on Python 3.10
    from taskgroup import TaskGroup
    from exceptiongroup import BaseExceptionGroup

from ..config import MappingConfig, mapping_file, udp_peer_address, udp_peer_timeout
from . import netguard, serial_io, sockopts
from .netguard import LogEvery as _LogEvery
from .access import AccessPolicy, server_ssl
from .protocols import make_session
from .protocols.base import ProtocolError, ProtocolSession
from .serial_link import Backoff, SerialLink, close_stream

if TYPE_CHECKING:
    from .mqtt_pub import MqttPublisher

READ_CHUNK = 4096
# directions for traffic monitors (the live hex view): bytes read from the serial
# device / bytes written to it (serial<->serial bridge: port A -> B / B -> A)
FROM_DEVICE = 0
TO_DEVICE = 1
CLIENT_QUEUE_MAX = 2048  # outbound chunks buffered per client before it's dropped
# UDP can't make a sender wait, so a UDP mapping's datagrams wait for its serial port in a
# queue of at most this many bytes, written like a TCP client's data (drain() keeps the
# port's own buffer at its high-water mark). A datagram that would go beyond it is dropped
# and counted (bytes_dropped): a sender faster than the line costs neither memory nor
# event-loop time. 64 KiB is that high-water mark too, about 68 s of line time at 9600
# baud and 6 s at 115200; the largest datagram fits.
UDP_BACKLOG_MAX = 64 * 1024
# a client connection's graceful close (and the flush before a closeon close) gets this
# long; a peer that keeps the connection up but reads nothing is then cut off, instead of
# holding one of the mapping's connection slots for as long as TCP takes to give up
TCP_CLOSE_TIMEOUT_S = 5.0
# errno/winerror values for "the peer stopped answering" (keepalive or retransmission
# timeout): ETIMEDOUT, plus on Windows WSAETIMEDOUT, WSAENETRESET and the overlapped-I/O
# ERROR_SEM_TIMEOUT.
_PEER_TIMEOUT_ERRNOS = {errno.ETIMEDOUT, 10060, 10052, 121}


class _Disconnect(ConnectionError):
    """Ends a client session for a known reason (the message is logged)."""


async def _close_client(writer: asyncio.StreamWriter) -> None:
    """Close a client's connection gracefully (what is queued for it goes out first), and
    abort it if that takes longer than TCP_CLOSE_TIMEOUT_S."""
    with contextlib.suppress(Exception):
        writer.close()
    closed = asyncio.ensure_future(writer.wait_closed())
    closed.add_done_callback(lambda f: f.cancelled() or f.exception())
    # asyncio.wait, not wait_for: the timeout must not cancel the stream's close future
    done, _pending = await asyncio.wait((closed,), timeout=TCP_CLOSE_TIMEOUT_S)
    if not done:
        with contextlib.suppress(Exception):
            writer.transport.abort()


def describe_disconnect(exc: BaseException) -> str:
    """Short, operator-facing reason a TCP client connection ended."""
    if isinstance(exc, (_Disconnect, ProtocolError)):
        return str(exc)
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if code in _PEER_TIMEOUT_ERRNOS:
        return "peer stopped responding (TCP timeout: unreachable, or not reading its data)"
    if isinstance(exc, ConnectionResetError):
        # Windows reports a failed keepalive as WSAECONNRESET too, so don't blame the peer
        if sys.platform == "win32":
            return "connection reset (by the peer, or TCP keepalive found it unreachable)"
        return "connection reset by peer"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "closed by peer"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


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


def trace_path(trace_dir: str, mapping: MappingConfig) -> str:
    """Where a mapping's trace goes: <trace_dir>/<mapping id>-<file name>. The id
    prefix keeps two mappings with the same file name apart (and makes a mapping's
    files easy to find); the name is already sanitised by config.trace_file_name.
    Raises ValueError if that would not be a file in trace_dir (config.mapping_file)."""
    return mapping_file(trace_dir, mapping.id, f"-{mapping.options.trace_both}")


def open_tracer(mapping: MappingConfig, trace_dir: Optional[str], log) -> Optional["Tracer"]:
    """The mapping's tracer when tracing is on (None when off or unavailable)."""
    if not mapping.options.trace_both:
        return None
    if not trace_dir:
        log("trace not written: no data directory to keep it in")
        return None
    try:
        path = trace_path(trace_dir, mapping)
    except ValueError as e:
        log(f"trace not written: {e}")
        return None
    tracer = Tracer(path, mapping.options, log=log)
    if not tracer.active:
        return None
    log(f"tracing traffic to {tracer.path}")
    return tracer


class Tracer:
    """Append a mapping's traffic to its trace file (optional hex dump + timestamp)
    without blocking the event loop.

    ``write()`` only queues the chunk (with its arrival time); a writer thread formats,
    writes and flushes in batches and rotates the file once it passes ``max_bytes``
    (<file>.1 ... <file>.<keep>; keep=0 starts the file over). If the disk can't keep
    up, at most QUEUE_MAX_BYTES wait in memory: newer chunks are dropped and the
    trace says how many bytes are missing."""

    QUEUE_MAX_BYTES = 8 * 1024 * 1024
    BATCH = 256

    def __init__(self, path: str, options, log=None) -> None:
        self.path = path
        self._hex = options.trace_hexdump
        self._ts = options.trace_timestamp
        self._max_bytes = max(1, int(options.trace_max_mb * 1024 * 1024))
        self._keep = int(options.trace_keep)
        self._log = log or (lambda _m: None)
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._queued = 0      # bytes waiting for the writer
        self._dropped = 0     # bytes dropped since the last line written
        self._fh = None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "ab")
        except OSError as e:
            self._log(f"trace disabled: cannot open {path}: {e}")
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"trace:{os.path.basename(path)}")
        self._thread.start()

    @property
    def active(self) -> bool:
        return self._fh is not None

    def write(self, direction: str, data: bytes) -> None:
        if self._fh is None or not data:
            return
        with self._lock:
            if self._queued + len(data) > self.QUEUE_MAX_BYTES:
                self._dropped += len(data)
                return
            self._queued += len(data)
        self._q.put((time.time(), direction, bytes(data)))

    def _format(self, ts: float, direction: str, data: bytes) -> bytes:
        prefix = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime(ts)) if self._ts else ""
        if self._hex:
            return f"{prefix}{direction} [{len(data)}] {data.hex(' ')}\n".encode()
        return prefix.encode() + direction.encode() + b" " + data + b"\n"

    def _run(self) -> None:
        stop = False
        while not stop:
            batch = [self._q.get()]
            while len(batch) < self.BATCH:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            out = []
            for item in batch:
                if item is None:
                    stop = True
                    continue
                ts, direction, data = item
                with self._lock:
                    self._queued -= len(data)
                    dropped, self._dropped = self._dropped, 0
                if dropped:
                    out.append(self._format(ts, "!!", f"{dropped} bytes not traced "
                                            "(the trace file could not keep up)".encode()))
                out.append(self._format(ts, direction, data))
            if out:
                self._append(out)
        with contextlib.suppress(Exception):
            if self._fh is not None:
                self._fh.close()
        self._fh = None

    def _append(self, lines: list) -> None:
        """Write a batch of lines; rotate as soon as the file reaches its size."""
        try:
            pending: list = []
            size = self._fh.tell() if self._fh is not None else 0
            for line in lines:
                pending.append(line)
                size += len(line)
                if size >= self._max_bytes:
                    self._write(pending)
                    pending = []
                    self._rotate()
                    size = 0
            if pending:
                self._write(pending)
        except OSError as e:
            self._log(f"trace write failed ({self.path}): {e}")

    def _write(self, lines: list) -> None:
        if self._fh is not None:
            self._fh.write(b"".join(lines))
            self._fh.flush()

    def _rotate(self) -> None:
        if self._fh is not None:
            self._fh.close()
        try:
            if self._keep > 0:
                for i in range(self._keep, 0, -1):
                    src = self.path if i == 1 else f"{self.path}.{i - 1}"
                    if os.path.exists(src):
                        os.replace(src, f"{self.path}.{i}")
            self._fh = open(self.path, "wb" if self._keep == 0 else "ab")
        except OSError as e:  # e.g. Windows: a rotated file is open for download
            self._log(f"trace rotation failed ({self.path}): {e}; continuing in the same file")
            self._fh = open(self.path, "ab")

    async def aclose(self) -> None:
        """Flush what is queued, close the file. Waits for the writer off the loop."""
        if self._fh is None:
            return
        self._q.put(None)
        await asyncio.to_thread(self._thread.join, 10)


@dataclass
class RunnerStatus:
    state: str = "stopped"  # stopped|running|reconnecting|device-missing|error
    device: str = ""
    client_count: int = 0
    # connected peers: each {"peer": "ip:port", "connected_at": unix_ts}
    clients: list[dict] = field(default_factory=list)
    # counted at the device, once (not per client, no telnet/RFC2217 control bytes);
    # serial<->serial bridge: in = port A -> B, out = port B -> A
    bytes_in: int = 0       # read from the serial device
    bytes_out: int = 0      # handed to the serial device (from clients, datagrams, the console)
    bytes_dropped: int = 0  # sent for the device but not written: the port was not open,
    #                         or a UDP datagram found UDP_BACKLOG_MAX bytes waiting
    reconnects: int = 0
    dropped_clients: int = 0   # clients dropped because their output queue overflowed
    queue_overflows: int = 0   # total serial->net chunks lost to queue overflow
    last_error: str = ""
    # the serial devices open now, for the Ports page's "in use" (both ports of a
    # serial<->serial bridge, whose `device` reads "A <-> B"); not part of as_dict()
    devices: tuple[str, ...] = ()

    def set_clients(self, clients) -> None:
        """Publish the connected clients (each has peer, connected_at and priority)."""
        self.client_count = len(clients)
        self.clients = sorted(
            ({"peer": c.peer, "connected_at": c.connected_at, "priority": c.priority}
             for c in clients),
            key=lambda d: d["connected_at"],
        )

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
            "bytes_dropped": self.bytes_dropped,
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
        # None = graceful-close sentinel (see request_graceful_close)
        self._out: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=self._queue_max)
        self._session: Optional[ProtocolSession] = None
        # what the client sends that its protocol session refuses or ignores (malformed
        # RFC 2217 requests, ...): logged at most once a minute per client
        self._protocol_log = _LogEvery(lambda msg: runner._log(f"client {self.peer}: {msg}"))
        self._last_activity = time.monotonic()
        self._closing = False
        self.close_reason = ""  # why the session ended (first cause wins; for the log)

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
            self.close_reason = self.close_reason or "dropped: output queue full (slow client)"
            self.runner.status.queue_overflows += 1
            self.runner.status.dropped_clients += 1
            self.runner._log(f"dropping slow client {self.peer}: output queue full "
                             f"({self._queue_max} chunks) — data lost for this client")
            with contextlib.suppress(Exception):
                self.writer.transport.abort()

    def serial_changed(self, serial) -> None:
        """The runner's serial port was reopened (the new pyserial object) or closed
        (None): an RFC 2217 session follows it. A reopened port that refuses the client's
        settings ends the session (ProtocolError says which)."""
        session = self._session
        if session is None or self._closing:
            return
        try:
            session.serial_changed(serial)
        except ProtocolError as e:
            self.kick(str(e))
            return
        ctrl = session.take_net_out()  # e.g. the modem lines as they are now
        if ctrl:
            with contextlib.suppress(asyncio.QueueFull):
                self._out.put_nowait(ctrl)

    async def run(self) -> None:
        m = self.runner.mapping
        # Create the protocol session. RFC2217 needs the live serial instance.
        ser = self.runner.serial_instance
        if m.network.protocol == "rfc2217" and ser is None:
            # it drives the port, so it starts only while the port is open (a connect-out
            # link can come up just after the port went away)
            self._closing = True
            self.close_reason = "the serial port is not open"
            await _close_client(self.writer)
            return
        self._session = make_session(m.network.protocol, ser,
                                     poll_interval=m.options.rfc2217_poll_modem_interval_s,
                                     read_only=m.network.read_only, log=self._protocol_log)

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
                if m.options.max_connection_time_s > 0:
                    tg.create_task(self._lifetime_guard(m.options.max_connection_time_s))
        except BaseExceptionGroup as eg:
            # Mirror the original `except*` split: connection-reset paths are a
            # normal disconnect (swallow them); log every other Exception; let
            # anything that is not an Exception (e.g. CancelledError) propagate.
            _normal = (ConnectionError, asyncio.IncompleteReadError, OSError)
            normal, rest = eg.split(_normal)
            if normal is not None and not self.close_reason:
                self.close_reason = describe_disconnect(next(_iter_leaf_exceptions(normal)))
            if rest is not None:
                unexpected, base_only = rest.split(Exception)
                if unexpected is not None:
                    for exc in _iter_leaf_exceptions(unexpected):
                        self._log_unexpected(exc)
                if base_only is not None:
                    raise base_only from None
        finally:
            self._closing = True
            await _close_client(self.writer)

    async def _pump_net_to_serial(self) -> None:
        m = self.runner.mapping
        session = self._session
        assert session is not None  # the pumps start after run() created it
        while not self._closing:
            data = await self.reader.read(READ_CHUNK)
            if not data:
                break  # client closed
            self._last_activity = time.monotonic()
            serial_bound = session.from_network(data)
            ctrl = session.take_net_out()
            if ctrl:
                with contextlib.suppress(asyncio.QueueFull):
                    self._out.put_nowait(ctrl)
            written = False
            if serial_bound and not m.network.read_only:
                written = await self.runner.serial_write(serial_bound)
            if not written:
                # Nothing reached the port (read-only, only telnet/RFC 2217 commands, or
                # the port is not open), so no serial drain() paces this client: let the
                # other tasks run before the next read, which returns at once while the
                # socket buffer holds more.
                await asyncio.sleep(0)
        raise _Disconnect("closed by peer")

    async def _pump_serial_to_net(self) -> None:
        while True:
            data = await self._out.get()
            if data is None:
                # graceful-close sentinel (e.g. closeon): flush queued bytes, then close
                while not self._out.empty():
                    d = self._out.get_nowait()
                    if d:
                        self.writer.write(d)
                try:
                    await asyncio.wait_for(self.writer.drain(), timeout=TCP_CLOSE_TIMEOUT_S)
                except asyncio.TimeoutError:  # a peer that reads nothing: cut it off
                    with contextlib.suppress(Exception):
                        self.writer.transport.abort()
                raise _Disconnect("closed by closeon match")
            self.writer.write(data)
            await self.writer.drain()
            self._last_activity = time.monotonic()

    async def _pump_poll(self) -> None:
        session = self._session
        assert session is not None
        interval = session.poll_interval or 1.0
        while not self._closing:
            await asyncio.sleep(interval)
            extra = session.poll()
            if extra:
                with contextlib.suppress(asyncio.QueueFull):
                    self._out.put_nowait(extra)

    async def _idle_guard(self, timeout: float) -> None:
        while not self._closing:
            await asyncio.sleep(1.0)
            if time.monotonic() - self._last_activity > timeout:
                raise _Disconnect(f"idle timeout ({fmt_duration(timeout)} without traffic)")

    async def _lifetime_guard(self, limit: float) -> None:
        await asyncio.sleep(limit)
        raise _Disconnect(f"max connection time reached ({fmt_duration(limit)})")

    def kick(self, reason: str = "kicked") -> None:
        self._closing = True
        self.close_reason = self.close_reason or reason
        with contextlib.suppress(Exception):
            self.writer.transport.abort()

    def request_graceful_close(self) -> None:
        """Stop feeding new serial data, but let already-queued bytes flush to the
        client before closing (used by closeon so the triggering data is delivered)."""
        if self._closing:
            return
        self._closing = True
        self.close_reason = self.close_reason or "closed by closeon match"
        try:
            self._out.put_nowait(None)  # sentinel handled by _pump_serial_to_net
        except asyncio.QueueFull:
            with contextlib.suppress(Exception):
                self.writer.transport.abort()


class MappingRunner:
    def __init__(self, mapping: MappingConfig, logger=None, trace_dir: Optional[str] = None):
        self.mapping = mapping
        self.trace_dir = trace_dir  # where traces go (data/traces); None = tracing unavailable
        self.status = RunnerStatus(state="stopped")
        self.log = logger
        self._server: Optional[netguard.Listener] = None
        self._serial_task: Optional[asyncio.Task] = None
        self._main_task: Optional[asyncio.Task] = None       # client/udp/serialbridge driver
        self._udp_transport: Optional[asyncio.DatagramTransport] = None
        self._tracer: Optional[Tracer] = None
        self._clients: set = set()
        self._monitors: set = set()  # browser console observers (read traffic)
        self._traffic: set = set()   # live hex view observers: both directions
        self._mqtt: Optional[MqttPublisher] = None  # optional MQTT publisher (serial -> broker)
        self._stop = asyncio.Event()
        # the serial port shared by server/client/udp (a serial<->serial bridge opens its own)
        self._link = SerialLink(mapping.serial, self.status, self._stop, self._log,
                                on_change=self._serial_changed)
        self._access = AccessPolicy(mapping.network)
        self._drop_log = _LogEvery(self._log)  # data for the device that was not written
        co = mapping.options.closeon
        self._closeon = co.encode("utf-8", "replace") if co else b""
        self._closeon_tail = b""  # rolling carry-over so matches across reads aren't missed

    @property
    def serial_instance(self):
        """The open pyserial object (None while the port is closed)."""
        return self._link.serial

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

    def add_traffic_monitor(self, mon) -> None:
        self._traffic.add(mon)

    def discard_traffic_monitor(self, mon) -> None:
        self._traffic.discard(mon)

    def _feed_traffic(self, direction: int, data: bytes) -> None:
        for mon in list(self._traffic):
            mon.feed_traffic(direction, data)

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._stop.clear()
        self._tracer = open_tracer(self.mapping, self.trace_dir, self._log)

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
            self._link.run(self._serial_session), name=f"serial:{self.mapping.name}")

        # If the network side fails to come up (address-in-use, bad TLS cert, ...),
        # tear everything down — otherwise the serial supervisor task is orphaned and
        # keeps the serial device open forever, so no other mapping can use that port.
        try:
            if net.mode == "server":
                # admission right after accept; a TLS client's allow-list check comes
                # before its handshake (netguard.start_server)
                self._server = await netguard.start_server(
                    self._on_client, net.bind_ip, net.port, ssl=server_ssl(net),
                    allowed=self._access.allowed, log=self._log)
                self._log(f"listening on {net.bind_ip}:{net.port} ({net.protocol}"
                          f"{', TLS' if net.tls else ''}, {sockopts.describe(net)})")
            elif net.mode == "client":
                self._main_task = asyncio.create_task(
                    self._client_supervisor(), name=f"connectout:{self.mapping.name}")
                self._log(f"connect-out to {net.remote_host}:{net.remote_port} ({net.protocol}, "
                          f"{sockopts.describe(net)})")
            elif net.mode == "udp":
                udp = _UdpBridge(self)  # a malformed stored reply address fails the start
                loop = asyncio.get_running_loop()
                self._udp_transport, _ = await loop.create_datagram_endpoint(
                    lambda: udp, local_addr=(net.bind_ip, net.port))
                self._main_task = asyncio.create_task(udp.run(), name=f"udp:{self.mapping.name}")
                self._log(f"UDP on {net.bind_ip}:{net.port}, {udp.describe()}")
        except BaseException:
            await self.stop()
            raise
        # the serial port may already have failed to open while the listener came up
        if self.status.state == "stopped":  # the serial side has not reported yet
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
            c.kick("mapping stopped")
        for mon in list(self._monitors) + list(self._traffic):
            mon.close()
        self._monitors.clear()
        self._traffic.clear()
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
        await self._link.close()
        if self._tracer:
            await self._tracer.aclose()
            self._tracer = None
        self.status.state = "stopped"
        self.status.client_count = 0
        self.status.clients = []
        self._log("stopped")

    # ssl context for a connect-out TLS data bridge (a listener's: access.server_ssl)
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
    async def serial_write(self, data: bytes) -> bool:
        """Write data from the network (a client, a datagram, the browser console) to the
        device and count it: bytes_out once the open port took it, else bytes_dropped.
        Returns False when it was dropped. Awaits drain(): the caller reads no more from
        its client until the port has room (network->serial backpressure)."""
        w = self._link.writer
        if w is None or w.is_closing():
            # serial not currently open (status shows reconnecting): drop
            self._drop(len(data), "the serial port is not open")
            return False
        if self._tracer:
            self._tracer.write("net>ser", data)
        self._feed_traffic(TO_DEVICE, data)
        w.write(data)
        self.status.bytes_out += len(data)
        await w.drain()
        return True

    def _serial_changed(self, serial) -> None:
        """SerialLink: the port was reopened (``serial``) or closed (None). An RFC 2217
        session drives the port itself, so the connected client's session follows it: the
        client stays connected through a reconnect and keeps its settings."""
        if self.mapping.network.protocol == "rfc2217":
            for c in list(self._clients):
                c.serial_changed(serial)

    def _restore_port_settings(self) -> None:
        """rfc2217_restore_on_disconnect: once the RFC 2217 client has left, put the port
        back to this mapping's settings, so what comes next (the next client, the console,
        MQTT, a trace) runs at what the mapping says. Only what the mapping sets and a
        client can change over RFC 2217 (baud rate, data bits, parity, stop bits, flow
        control): RTS/DTR stay as they are, since toggling them can reset a device."""
        m = self.mapping
        ser = self._link.serial
        if (m.network.protocol != "rfc2217" or not m.options.rfc2217_restore_on_disconnect
                or ser is None or self._clients or self._stop.is_set()):
            return
        restored = False
        for name, value in serial_io.port_settings(m.serial).items():
            try:
                if getattr(ser, name) != value:
                    setattr(ser, name, value)
                    restored = True
            except Exception as e:
                self._log(f"serial setting not restored ({name} {value!r}): "
                          f"{type(e).__name__}: {e}")
        if restored:
            self._log(f"serial settings restored to the mapping's ({m.serial.compact()}, "
                      f"flow control {m.serial.flowcontrol}) after the RFC 2217 client left")

    async def _serial_open(self) -> bool:
        """Wait until the serial port is open; False when the mapping stops first."""
        link = self._link
        if not link.is_open:
            ready = asyncio.ensure_future(link.ready.wait())
            stop = asyncio.ensure_future(self._stop.wait())
            try:
                await asyncio.wait((ready, stop), return_when=asyncio.FIRST_COMPLETED)
            finally:
                ready.cancel()
                stop.cancel()
        return link.is_open and not self._stop.is_set()

    def _drop(self, n: int, why: str) -> None:
        """Count data for the device that is not written, and log it (rate-limited)."""
        if n <= 0:
            return
        self.status.bytes_dropped += n
        self._drop_log(f"data for the device dropped: {why} "
                       f"({self.status.bytes_dropped} bytes so far)")

    async def _serial_session(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        """While the port is open (SerialLink.run): send the open string, then relay what
        the device sends until the port goes away."""
        if self.mapping.options.openstr:
            writer.write(self.mapping.options.openstr.encode("utf-8", "replace"))
            await writer.drain()
        await self._serial_read_loop(reader)

    async def _serial_read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stop.is_set():
            data = await reader.read(READ_CHUNK)
            if not data:
                raise ConnectionError("serial EOF / device removed")
            self.status.bytes_in += len(data)  # once, whoever receives it (or nobody)
            if self._tracer:
                self._tracer.write("ser>net", data)
            self._feed_monitors(data)
            self._feed_traffic(FROM_DEVICE, data)
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
    def _refresh_clients(self) -> None:
        self.status.set_clients(self._clients)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("?", 0)
        if isinstance(peername, tuple):
            peer_ip, peer_port = str(peername[0]), int(peername[1]) if len(peername) > 1 else 0
        else:
            peer_ip, peer_port = str(peername), 0
        peer_label = f"{peer_ip}:{peer_port}" if peer_port else peer_ip
        net = self.mapping.network

        # allow-list, then max connections (a priority client / kick_old_user makes room)
        admission = self._access.admit(peer_ip, peer_label, self._clients, self._log)
        if not admission.admitted:
            if admission.at_capacity:
                with contextlib.suppress(Exception):
                    writer.write(b"Busy: maximum connections reached.\r\n")
                    await writer.drain()
            writer.close()
            return
        is_priority = admission.priority

        # For RFC2217 we need the live serial instance; wait briefly for it.
        ready = self._link.ready
        if net.protocol == "rfc2217" and not ready.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ready.wait(),
                                       timeout=self.mapping.options.rfc2217_net_timeout_s)
            if not ready.is_set():
                self._log(f"rejected {peer_label}: serial not ready for RFC2217")
                writer.close()
                return

        self._enable_keepalive(writer, peer_label)
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
            why = f" — {client.close_reason}" if client.close_reason else ""
            self._log(f"client disconnected: {peer_label} after {held}{why} "
                      f"({len(self._clients)} total)")
            self._restore_port_settings()
            # closestr to device when the last client leaves
            sw = self._link.writer
            if not self._clients and self.mapping.options.closestr and sw:
                with contextlib.suppress(Exception):
                    sw.write(self.mapping.options.closestr.encode("utf-8", "replace"))
                    await sw.drain()

    def _enable_keepalive(self, writer: asyncio.StreamWriter, peer: str) -> None:
        net = self.mapping.network
        if err := sockopts.apply_keepalive(writer, net):
            self._log(f"TCP keepalive not enabled for {peer}: {err}")

    # ---------------- connect-out (TCP client) ----------------
    async def _connect_out(self):
        """Resolve the remote host, then connect (+ TLS handshake) to each resolved
        address in turn, giving each one connect_timeout_s — like an OS connect
        timeout per address — so a dual-stack host whose IPv6 path is dead still
        falls back to IPv4 instead of the first address eating the whole budget.

        The DNS lookup is deliberately NOT under the timeout: getaddrinfo runs in a
        worker thread that cancellation can't stop, so timing it out would leave one
        stuck thread behind per retry during a DNS outage and starve the shared
        executor (asyncio.to_thread: password hashing, config saves, log reads).
        Connecting to the resolved IP literal needs no further lookup."""
        net = self.mapping.network
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(net.remote_host, net.remote_port,
                                       type=socket.SOCK_STREAM)
        ssl_ctx = self._client_ssl()
        error: BaseException = ConnectionError(f"no address for {net.remote_host}")
        for family, _type, _proto, _name, sockaddr in infos:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(
                        str(sockaddr[0]), int(sockaddr[1]), family=family, ssl=ssl_ctx,
                        server_hostname=net.remote_host if ssl_ctx else None),
                    timeout=net.connect_timeout_s or None)
            except asyncio.TimeoutError as e:
                if getattr(e, "errno", None) is not None:
                    error = e  # the OS's own connect timeout, not ours
                    continue
                error = ConnectionError(f"timed out after {net.connect_timeout_s:g}s")
            except OSError as e:
                error = e
        raise error

    async def _client_supervisor(self) -> None:
        net = self.mapping.network
        target = f"{net.remote_host}:{net.remote_port}"
        backoff = Backoff()
        while not self._stop.is_set():
            # an RFC 2217 session drives the serial port: connect out only while it is open
            if net.protocol == "rfc2217" and not await self._serial_open():
                break
            try:
                reader, writer = await self._connect_out()
                self._enable_keepalive(writer, target)
                client = _ClientConn(self, reader, writer, net.remote_host, net.remote_port)
                self._clients.add(client)
                self._refresh_clients()
                self._log(f"connected out to {target}")
                backoff.reset()
                try:
                    await client.run()
                finally:
                    self._clients.discard(client)
                    self._refresh_clients()
                    held = fmt_duration(time.time() - client.connected_at)
                    why = f" — {client.close_reason}" if client.close_reason else ""
                    self._log(f"connect-out link to {target} closed after {held}{why}")
                    self._restore_port_settings()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.last_error = str(e)
                self._log(f"connect-out failed: {e}")
            if self._stop.is_set():
                break
            if not await backoff.wait(self._stop):
                break

    # ---------------- serial <-> serial bridge ----------------
    async def _serialbridge_supervisor(self) -> None:
        # both ports open and close together, so this keeps its own loop (SerialLink
        # supervises one port)
        backoff = Backoff()
        while not self._stop.is_set():
            wa = wb = None
            try:
                ra, wa, _sa, da = await serial_io.open_serial(self.mapping.serial)
                rb, wb, _sb, db = await serial_io.open_serial(self.mapping.serial_b)
                self.status.device = f"{da} <-> {db}"
                self.status.devices = (da, db)
                self.status.last_error = ""
                self.status.state = "running"
                backoff.reset()
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
                self.status.state = "reconnecting" if backoff.attempt else "device-missing"
                self._log(f"serial bridge error: {self.status.last_error}")
            finally:
                await asyncio.gather(*(close_stream(w, self._log)
                                       for w in (wa, wb) if w is not None))
            if self._stop.is_set():
                break
            self.status.reconnects += 1
            self.status.state = "reconnecting"
            if not await backoff.wait(self._stop):
                break

    async def _pump_serial_pair(self, reader, writer, tag: str) -> None:
        # port A is "the device" of the counters and the live view: A -> B counts as in,
        # B -> A as out
        from_a = tag.startswith("A")
        while not self._stop.is_set():
            data = await reader.read(READ_CHUNK)
            if not data:
                raise ConnectionError(f"serial EOF ({tag})")
            if self._tracer:
                self._tracer.write(tag, data)
            self._feed_monitors(data)
            self._feed_traffic(FROM_DEVICE if from_a else TO_DEVICE, data)
            writer.write(data)
            if from_a:
                self.status.bytes_in += len(data)
            else:
                self.status.bytes_out += len(data)
            await writer.drain()


# ---------------------------------------------------------------------------
# UDP transport (datagram bridge). The serial read loop broadcasts to _clients, so the
# _UdpPeer in _clients carries serial->net; datagram_received carries net->serial, through
# a bounded queue that _UdpBridge.run() writes to the port. One peer gets the device's
# output: the reply address when one is set (udp_peer_host / udp_peer_port), otherwise
# the sender of the last allowed datagram (forgotten after udp_peer_timeout_s without a
# datagram from it, when that is set).
# ---------------------------------------------------------------------------
def _udp_label(addr) -> str:
    return f"{addr[0]}:{addr[1]}"


class _UdpPeer:
    """Where a UDP mapping sends the device's output; listed as its client."""

    def __init__(self, bridge: "_UdpBridge", addr, label: str = "") -> None:
        self.bridge = bridge
        self.addr = addr
        self.peer = label or _udp_label(addr)
        self.priority = False
        self.connected_at = time.time()     # since when it gets the output
        self.last_heard = time.monotonic()  # its last datagram (for the peer timeout)

    def feed_from_serial(self, data: bytes) -> None:
        self.bridge.send(self, data)

    def kick(self, reason: str = "") -> None:
        pass

    def request_graceful_close(self) -> None:
        pass


class _UdpBridge(asyncio.DatagramProtocol):
    """A UDP mapping's socket side. Every datagram is checked against the allow-list; an
    allowed one makes its sender the peer (unless there is a reply address) and waits in a
    queue that run() writes to the serial port. A datagram that would take the queue past
    UDP_BACKLOG_MAX bytes is dropped and counted instead."""

    def __init__(self, runner: "MappingRunner") -> None:
        self.runner = runner
        net = runner.mapping.network
        self.reply_to = udp_peer_address(net)  # ValueError: a malformed stored one
        # the peer timeout is for a sender that was learned, not for the reply address
        self.timeout = 0.0 if self.reply_to else udp_peer_timeout(net)
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.peer: Optional[_UdpPeer] = None
        self._queue: list[bytes] = []
        self._queued = 0  # bytes waiting: queued, or being written by run()
        self._wake = asyncio.Event()
        self._peer_log = _LogEvery(runner._log)
        self._refused_log = _LogEvery(runner._log)
        self._error_log = _LogEvery(runner._log)

    def describe(self) -> str:
        """Where the device's output goes (for the start-up log line)."""
        if self.reply_to:
            return f"device output to {_udp_label(self.reply_to)}"
        if self.timeout:
            return (f"device output to the last sender (forgotten after "
                    f"{fmt_duration(self.timeout)} without a datagram from it)")
        return "device output to the last sender"

    # ---- asyncio.DatagramProtocol ----
    def connection_made(self, transport) -> None:
        self.transport = transport
        if self.reply_to is not None:
            host, port = self.reply_to
            sock = transport.get_extra_info("socket")
            if (sock is not None and sock.family == socket.AF_INET6
                    and ipaddress.ip_address(host).version == 4):
                host = f"::ffff:{host}"  # an IPv4 peer of a socket bound to an IPv6 address
            self._set_peer((host, port), _udp_label(self.reply_to))

    def connection_lost(self, exc) -> None:
        self.transport = None

    def error_received(self, exc: Exception) -> None:
        # e.g. the peer's port is closed (ICMP port unreachable) or there is no route to it
        self._error_log(f"udp error: {exc}")

    def datagram_received(self, data: bytes, addr) -> None:
        r = self.runner
        # Access control: a TCP listener checks the allow-list in _on_client, but UDP has
        # no accept hook, so it is checked HERE, for every datagram. An unlisted source is
        # dropped entirely: it neither reaches the device nor becomes the peer. The check
        # looks at the datagram's source address, which a sender can forge (README,
        # Security); with a reply address the device's output can't be redirected.
        if not r._access.allowed(addr[0]):
            self._refused_log(f"rejected datagram from {_udp_label(addr)}: not in allowed list")
            return
        if self.reply_to is None:
            self._heard_from(addr)
        if r.mapping.network.read_only or not data:
            return
        if r._link.writer is None:
            r._drop(len(data), "the serial port is not open")
        elif self._queued + len(data) > UDP_BACKLOG_MAX:
            r._drop(len(data), f"the serial port cannot keep up with the datagrams "
                               f"({self._queued} bytes waiting for it)")
        else:
            self._queue.append(data)
            self._queued += len(data)
            self._wake.set()

    # ---- the peer ----
    def _set_peer(self, addr, label: str = "") -> _UdpPeer:
        r = self.runner
        if self.peer is not None:
            r._clients.discard(self.peer)
        peer = self.peer = _UdpPeer(self, addr, label)
        r._clients.add(peer)
        r._refresh_clients()
        return peer

    def _heard_from(self, addr) -> None:
        """No reply address: the sender of an allowed datagram gets the device's output
        from now on. A new sender is logged (a change at most once a minute) and listed
        with its own connection time."""
        old = self.peer
        if old is not None and old.addr == addr:
            old.last_heard = time.monotonic()
            return
        peer = self._set_peer(addr)
        if old is None:
            self.runner._log(f"udp peer {peer.peer}")
        else:
            self._peer_log(f"udp peer is now {peer.peer} (was {old.peer})")

    def _expired(self) -> bool:
        """Forget the sender once it has sent nothing for the peer timeout."""
        peer = self.peer
        if (peer is None or not self.timeout
                or time.monotonic() - peer.last_heard < self.timeout):
            return False
        r = self.runner
        r._clients.discard(peer)
        self.peer = None
        r._refresh_clients()
        r._log(f"udp peer {peer.peer} forgotten: no datagram for {fmt_duration(self.timeout)}")
        return True

    def send(self, peer: _UdpPeer, data: bytes) -> None:
        """The device's output, to the peer (nothing once it has expired)."""
        if self.transport is None or peer is not self.peer or self._expired():
            return
        with contextlib.suppress(Exception):  # a failed send reaches error_received
            self.transport.sendto(data, peer.addr)

    # ---- network -> serial ----
    async def run(self) -> None:
        """Write the queued datagrams to the serial port, oldest first: what has queued up
        goes in one write that awaits drain(), like a TCP client's data, so the port's own
        buffer stays at its high-water mark and the queue holds at most UDP_BACKLOG_MAX.
        With a peer timeout, also forgets a sender that went quiet."""
        r = self.runner
        while True:
            if self.timeout:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                self._expired()
            else:
                await self._wake.wait()
            self._wake.clear()
            if not self._queue:
                continue
            batch = self._queue[0] if len(self._queue) == 1 else b"".join(self._queue)
            self._queue.clear()
            try:
                await r.serial_write(batch)
            except Exception:  # the port went away mid-write: SerialLink reopens it
                pass
            finally:
                self._queued -= len(batch)
