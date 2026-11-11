"""The process's open files: their limit, how many are in use, and what the mappings need.

Every serial port, listener, client connection, log and trace file and MQTT connection
holds a file descriptor, and a process may have at most its soft RLIMIT_NOFILE open at
once: 1024 under systemd unless the unit raises it. Past it, whatever opens the next
file fails, wherever that is (a serial reopen, a client, a config save). So:
  - ``raise_fd_limit`` lifts the soft limit toward the hard one at start-up (at most
    FD_LIMIT_TARGET), with prlimit() on Linux: the shipped unit's system call filter
    denies setrlimit() but allows prlimit64. Nothing in the process waits with select()
    any more (serial_compat, mqtt_pub.PollSelect), so descriptors above 1024 work;
  - ``fd_budget_warning`` says when the configured mappings need about as many as the
    limit allows;
  - ``open_fds`` / ``fd_limit`` are what /metrics reports.
Windows has no such limit for sockets and serial ports (they are handles): each of these
answers None or "" there.

Incoming connections pass the process's budget (``BUDGET``) right after accept, whatever
listener they reach, before they cost anything more:
  - ``Budget.headroom``: a connection is refused (closed, with a log line at most once a
    minute) while the process is within ``fd_reserve`` of its open-file limit, so clients
    can't take the files that serial ports, logs and the configuration need;
  - TLS handshakes in progress hold a socket and a 256 KiB buffer each: at most
    TLS_HANDSHAKES_MAX at a time, TLS_HANDSHAKES_PER_IP from one address, each given
    TLS_HANDSHAKE_TIMEOUT_S (asyncio's default is 60 s).
``start_server`` is asyncio.start_server for a mapping's listener with that admission. For
a TLS listener it also applies the mapping's allow-list before the handshake (a refused
host never gets one) and does the handshake itself (``start_tls``): the mapping sees the
connection only once it is done, and decides about capacity, kicking and "Busy" then.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Callable, Optional

# the soft open-file limit raise_fd_limit aims for: far above what a few hundred
# mappings need, without handing child processes a huge limit
FD_LIMIT_TARGET = 65536
# what a process holds open before any mapping: stdio, all.log, the event loop, the admin
# listener and its connections, the executor's threads' files
FD_BASE = 40

# TLS on the listeners: the handshake must be over in this time; closing a TLS connection
# waits this long for the peer's close_notify (Python 3.11+; 3.10 has no such setting)
TLS_HANDSHAKE_TIMEOUT_S = 15.0
TLS_SHUTDOWN_TIMEOUT_S = 5.0
# TLS handshakes in progress at once: in the process, and from one source address
TLS_HANDSHAKES_MAX = 128
TLS_HANDSHAKES_PER_IP = 32


class LogEvery:
    """A log line that can repeat many times a second (dropped data, a UDP peer that keeps
    changing, refused connections): written at most once per ``interval`` seconds, saying
    how many were held back since the last one."""

    def __init__(self, log: Callable[[str], None], interval: float = 60.0) -> None:
        self._log = log
        self._interval = interval
        self._next = 0.0
        self._held = 0

    def __call__(self, msg: str) -> None:
        now = time.monotonic()
        if now < self._next:
            self._held += 1
            return
        if self._held:
            msg += f" ({self._held} more since the last such line)"
        self._held = 0
        self._next = now + self._interval
        self._log(msg)


def _resource() -> Any:
    try:
        import resource
    except ImportError:  # Windows
        return None
    return resource


def fd_limit() -> Optional[int]:
    """The soft open-file limit (None: unlimited, or Windows)."""
    resource = _resource()
    if resource is None:
        return None
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return None if soft == resource.RLIM_INFINITY else int(soft)


def open_fds() -> Optional[int]:
    """How many files this process has open (None where that can't be counted cheaply)."""
    if os.name != "posix":
        return None
    for folder in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(folder)) - 1  # less the one listdir() itself opened
        except OSError:
            continue
    return None


def _shown(value: int, resource: Any) -> str:
    return "unlimited" if value == resource.RLIM_INFINITY else str(value)


def raise_fd_limit(target: int = FD_LIMIT_TARGET) -> str:
    """Raise the soft open-file limit toward the hard limit, at most to ``target`` (never
    lower it). Returns the line for the start-up log ("" on Windows)."""
    resource = _resource()
    if resource is None:
        return ""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(hard, target)
    if soft == resource.RLIM_INFINITY or soft >= want:
        return f"open files: limit {_shown(soft, resource)}"
    try:
        if hasattr(resource, "prlimit"):  # Linux: prlimit64, never the setrlimit() call
            resource.prlimit(0, resource.RLIMIT_NOFILE, (want, hard))
        else:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (OSError, ValueError) as e:
        return f"open files: limit {soft} (raising it to {want} failed: {e})"
    return f"open files: limit raised from {soft} to {want} (hard limit {_shown(hard, resource)})"


def fd_estimate(config: Any) -> int:
    """About how many files the configured mappings hold open: per mapping its log file
    (disabled ones too), and per enabled one its serial port(s), its socket (a listener, a
    UDP socket or the connection out), each client it admits, its trace file and an MQTT
    connection (its socket and paho's wake-up pair)."""
    need = FD_BASE
    for m in config.mappings:
        need += 1
        if not m.enabled:
            continue
        need += len(m.serial_sides())
        need += 1 if m.options.trace_both else 0
        need += 3 if m.mqtt.enabled else 0
        if m.kind == "net":
            need += 1 + (m.network.max_connections if m.network.mode == "server" else 0)
    return need


def fd_budget_warning(config: Any, limit: Optional[int] = None) -> str:
    """A start-up warning when the mappings need over 80% of the open-file limit ("" when
    they fit, or there is no such limit)."""
    limit = fd_limit() if limit is None else limit
    if limit is None:
        return ""
    need = fd_estimate(config)
    if need <= limit * 0.8:
        return ""
    return (f"WARNING: the {len(config.mappings)} mappings need about {need} open files, "
            f"close to this process's limit of {limit}: beyond it serial ports, clients and "
            "files fail to open. Raise the limit (LimitNOFILE= in the systemd unit, "
            "--ulimit nofile= for Docker) or split the mappings over more instances.")


def fd_reserve(limit: int) -> int:
    """Open files kept free for what is not a client (serial ports, logs, the configuration
    and its backups, traces, MQTT): incoming connections are refused within this many of
    the limit."""
    return min(256, max(32, limit // 8))


# ---------------------------------------------------------------------------
# incoming connections
# ---------------------------------------------------------------------------
def peer_ip(transport: Any) -> str:
    """A connection's source address; an IPv4-mapped IPv6 one as its IPv4 address."""
    peer = transport.get_extra_info("peername")
    ip = str(peer[0]) if isinstance(peer, tuple) and peer else str(peer or "?")
    if ip.startswith("::ffff:") and "." in ip:
        ip = ip[len("::ffff:"):]
    return ip


def peer_label(transport: Any) -> str:
    """ip:port of a connection, for the log."""
    peer = transport.get_extra_info("peername")
    if isinstance(peer, tuple) and len(peer) > 1:
        return f"{peer[0]}:{peer[1]}"
    return peer_ip(transport)


class Pool:
    """Connections (their transports) counted in total and per source address, each up to a
    cap (0: none). A connection whose transport is closing no longer counts."""

    def __init__(self, total: int = 0, per_ip: int = 0) -> None:
        self.total = total
        self.per_ip = per_ip
        self._members: dict[Any, str] = {}

    def _sweep(self) -> None:
        for t in [t for t in self._members if t.is_closing()]:
            del self._members[t]

    def __len__(self) -> int:
        self._sweep()
        return len(self._members)

    def count(self, ip: str) -> int:
        self._sweep()
        return sum(1 for member_ip in self._members.values() if member_ip == ip)

    def full(self, ip: str) -> str:
        """"" when one more from ``ip`` fits, else why not."""
        if self.total and len(self) >= self.total:
            return f"{self.total} at once is the limit"
        if self.per_ip and self.count(ip) >= self.per_ip:
            return f"{self.per_ip} at once from one address is the limit"
        return ""

    def add(self, transport: Any, ip: str) -> None:
        self._members[transport] = ip

    def discard(self, transport: Any) -> None:
        self._members.pop(transport, None)

    def members(self) -> list:
        self._sweep()
        return list(self._members)


class Budget:
    """Whether the process takes one more incoming connection, whichever listener it reaches
    (one instance: ``BUDGET``), and the TLS handshakes in progress."""

    def __init__(self) -> None:
        self.handshakes = Pool(TLS_HANDSHAKES_MAX, TLS_HANDSHAKES_PER_IP)
        self.refused: dict[str, int] = {}  # why -> connections refused for it (/metrics)
        self._counted = (-1.0, 0)  # (when, how many files were open)
        self._admitted = 0  # connections let in since

    def _files_in_use(self) -> Optional[int]:
        now = time.monotonic()
        if now - self._counted[0] > 0.25:  # at most four counts a second, under a flood too
            n = open_fds()
            if n is None:
                return None
            self._counted, self._admitted = (now, n), 0
        return self._counted[1] + self._admitted

    def headroom(self) -> str:
        """"" when one more connection fits, else why not (for the log)."""
        limit = fd_limit()
        if limit is not None:
            used = self._files_in_use()
            if used is not None and used >= limit - fd_reserve(limit):
                self.note_refused("open_files")
                return (f"{used} of this process's {limit} open files are in use (the rest "
                        "is kept for serial ports, logs and the configuration)")
        self._admitted += 1
        return ""

    def handshake_room(self, ip: str) -> str:
        why = self.handshakes.full(ip)
        if why:
            self.note_refused("tls_handshakes")
            return f"too many TLS handshakes in progress ({why})"
        return ""

    def note_refused(self, why: str) -> None:
        self.refused[why] = self.refused.get(why, 0) + 1


BUDGET = Budget()
# why a connection can be refused right after accept (Budget.refused keys, for /metrics)
REFUSAL_REASONS = ("open_files", "tls_handshakes", "admin_ui")


class _Early(asyncio.Protocol):
    """What arrives on a TLS connection between the end of its handshake and the hand-over
    to the protocol that serves it (both in one pass of the event loop)."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.eof = False
        self.lost = False

    def data_received(self, data: bytes) -> None:
        self.data += data

    def eof_received(self) -> bool:
        self.eof = True
        return False

    def connection_lost(self, exc: Optional[BaseException]) -> None:
        self.lost = True


async def start_tls(transport: Any, ctx: Any, protocol: asyncio.BaseProtocol) -> bool:
    """The server side of a TLS handshake on an accepted ``transport`` whose reading the
    caller paused, within TLS_HANDSHAKE_TIMEOUT_S; then ``protocol`` serves the connection
    (its connection_made gets the TLS transport). False when the connection went away
    first; raises what the handshake raised (the transport is closed then)."""
    loop = asyncio.get_running_loop()
    early = _Early()
    if sys.version_info >= (3, 11):
        tls = await loop.start_tls(transport, early, ctx, server_side=True,
                                   ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT_S,
                                   ssl_shutdown_timeout=TLS_SHUTDOWN_TIMEOUT_S)
    else:  # pragma: no cover - exercised only on Python 3.10 (no ssl_shutdown_timeout)
        tls = await loop.start_tls(transport, early, ctx, server_side=True,
                                   ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT_S)
    if tls is None or early.lost or tls.is_closing():
        transport.close()
        return False
    tls.set_protocol(protocol)
    protocol.connection_made(tls)
    if early.data and isinstance(protocol, asyncio.Protocol):
        protocol.data_received(bytes(early.data))
    if early.eof and isinstance(protocol, asyncio.Protocol):
        protocol.eof_received()
    return True


def describe_tls_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if isinstance(exc, ConnectionError) else f"{type(exc).__name__}: {text}"


def begin_tls(transport: Any, ctx: Any, protocol: asyncio.BaseProtocol, label: str,
              failed: Callable[[str], None]) -> asyncio.Task:
    """Start the TLS handshake of an admitted connection: reading paused, counted among the
    handshakes in progress until it is over, run in a task (start_tls). A failed handshake
    is reported to ``failed`` (rate-limit it: anyone can make one fail)."""
    transport.pause_reading()  # the ClientHello waits for the TLS protocol
    BUDGET.handshakes.add(transport, peer_ip(transport))

    async def handshake() -> None:
        try:
            await start_tls(transport, ctx, protocol)
        except Exception as e:  # the timeout, a bad ClientHello, the client went away
            failed(f"TLS handshake with {label} failed: {describe_tls_error(e)}")
        finally:
            BUDGET.handshakes.discard(transport)

    return asyncio.get_running_loop().create_task(handshake())


class _Gate(asyncio.Protocol):
    """A new connection's first protocol: its listener admits it (giving it the protocol
    that serves it) or closes it."""

    def __init__(self, listener: Listener) -> None:
        self._listener = listener

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._listener.admit(transport)


class Listener:
    """A mapping's TCP listener (``start_server``); close() / wait_closed() / sockets as on
    the asyncio Server it wraps."""

    def __init__(self, client_connected_cb: Callable, ssl: Any, allowed: Optional[Callable],
                 log: Callable[[str], None], limit: int) -> None:
        self._loop = asyncio.get_running_loop()
        self._cb = client_connected_cb
        self._ssl = ssl
        self._allowed = allowed
        self._log = log
        self._limit = limit
        self._refused = LogEvery(log)
        self._failed = LogEvery(log)
        self._handshakes: dict[Any, asyncio.Task] = {}  # plain transport -> its handshake
        self.server: Any = None

    @property
    def sockets(self):
        return self.server.sockets

    def close(self) -> None:
        self.server.close()
        for transport in list(self._handshakes):
            transport.abort()

    async def wait_closed(self) -> None:
        await self.server.wait_closed()

    def _stream_protocol(self) -> asyncio.StreamReaderProtocol:
        reader = asyncio.StreamReader(limit=self._limit, loop=self._loop)
        return asyncio.StreamReaderProtocol(reader, self._cb, loop=self._loop)

    def admit(self, transport: Any) -> None:
        label = peer_label(transport)
        why = BUDGET.headroom()
        if not why and self._ssl is not None:
            ip = peer_ip(transport)
            if self._allowed is not None and not self._allowed(ip):
                self._log(f"rejected {label}: not in allowed list")
                transport.close()
                return
            why = BUDGET.handshake_room(ip)
        if why:
            self._refused(f"refused {label}: {why}")
            transport.close()
            return
        protocol = self._stream_protocol()
        if self._ssl is None:
            transport.set_protocol(protocol)
            protocol.connection_made(transport)
            return
        task = begin_tls(transport, self._ssl, protocol, label, self._failed)
        self._handshakes[transport] = task
        task.add_done_callback(lambda _t: self._handshakes.pop(transport, None))


async def start_server(client_connected_cb: Callable, host: str, port: int, *, ssl: Any = None,
                       allowed: Optional[Callable[[str], bool]] = None,
                       log: Optional[Callable[[str], None]] = None,
                       limit: int = 2 ** 16) -> Listener:
    """asyncio.start_server(client_connected_cb, host, port, ssl=ssl), admitting each
    connection right after accept: the process's budget, then (TLS) ``allowed`` (the
    mapping's allow-list) and the handshakes in progress, before the handshake.
    ``client_connected_cb(reader, writer)`` gets TLS connections once their handshake is
    done. ``log``: where refusals and failed handshakes are reported (the mapping's log)."""
    listener = Listener(client_connected_cb, ssl, allowed, log or (lambda _m: None), limit)
    listener.server = await asyncio.get_running_loop().create_server(
        lambda: _Gate(listener), host, port)
    return listener
