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

What a telnet / RFC 2217 client sends is carried out in order (ProtocolSession.
from_network_parts): its data, a serial BREAK it asks for (IAC BRK) and the commands that
act on the port (RFC 2217 SET-* / PURGE-DATA), each after the data before it was written
and, for a BREAK or a setting, has left the port (at most SETTLE_MAX_S). A BREAK holds the
line in the break condition for BREAK_S without blocking the event loop (pyserial's
send_break() sleeps in the calling thread); a read-only mapping sends none.

With MQTT, the device's lines are published; with mqtt.tx_enabled each message on
<base_topic>/tx is written to the port as it is, in one piece between what TCP clients
send, from a queue of at most MQTT_TX_MAX_BYTES, and answered on <base_topic>/tx/result.
options.poll_string is sent every poll_interval_s while the port is open (by default not
while a TCP client is connected).

Opt-in, per mapping (each off by default):
  - packing (options.pack_gap_ms / pack_delimiter / pack_max_bytes, raw protocol): the
    device's output reaches the clients as frames (Packer), one datagram each over UDP;
    the console, the hex view, the trace and MQTT still get it as it is read;
  - network.writer_client_ips: only clients from these addresses may send to the device,
    the others only listen (what they send is dropped and counted);
  - network.open_on_connect (TCP server): the serial port is open only while a client (or
    the web console) is connected, and for port_linger_s after the last one left; in
    between the status says the mapping waits for a client;
  - network.remote_targets (connect-out): more targets, tried in order when the one in
    use can't be reached (failover_return_s: back to the first once it answers);
  - network.pool: a port pool's listener (engine/pool.py) hands clients to take_client().
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import io
import ipaddress
import math
import os
import queue
import re
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

from .. import i18n, storage
from ..config import (
    MQTT_TX_MAX_BYTES, MappingConfig, canonical_ip, connect_targets, failover_return, host_port,
    mapping_file, mqtt_topic_problem, pack_settings, poll_bytes, port_linger, udp_peer_address,
    udp_peer_timeout, udp_targets,
)
from . import netguard, portdiag, serial_io, sockopts, tls
from .netguard import LogEvery as _LogEvery
from .access import AccessPolicy, connection_order, server_ssl
from .protocols import make_session
from .protocols.base import BREAK, ProtocolError, ProtocolSession
from .serial_link import (
    Backoff, FailureLog, NoteLog, SerialLink, close_stream, explain, fmt_duration, sent_within,
)

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
# a serial BREAK (telnet IAC BRK, the web console): the line is held in the break condition
# this long, as pyserial's send_break() and most terminal programs do
BREAK_S = 0.25
# before a BREAK or an RFC 2217 setting change, the data the client sent ahead of it gets
# up to this long to leave the port (its line time, twice, plus 0.2 s): a port that holds
# its output (flow control, a driver that never reports an empty queue) delays the command
# no more than this, since an RFC 2217 client waits about 3 s for its answer
SETTLE_MAX_S = 2.0
# packing without a gap (a delimiter and / or a frame size only): an unfinished frame is
# sent after this long without a byte from the device, so nothing is held back for good
PACK_IDLE_FLUSH_S = 1.0
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


class Packer:
    """Cuts the device's output into frames (options.pack_*): a frame ends with the
    delimiter (which stays in it) or at ``size`` bytes, whichever comes first; what is
    left waits for more (flush(): the gap passed, or the port closed)."""

    def __init__(self, delimiter: bytes, size: int) -> None:
        self._delimiter = delimiter
        self._size = max(1, size)
        self._buf = bytearray()
        self._searched = 0  # bytes of _buf that hold no delimiter's beginning and end

    @property
    def pending(self) -> int:
        """Bytes waiting for the end of their frame."""
        return len(self._buf)

    def feed(self, data: bytes) -> list[bytes]:
        """Take what was read from the device; the frames that are complete now."""
        buf, delimiter, size = self._buf, self._delimiter, self._size
        buf += data
        frames = []
        while buf:
            end = -1
            if delimiter:
                at = buf.find(delimiter, self._searched, size)  # (all of it in the frame)
                if at >= 0:
                    end = at + len(delimiter)
            if end < 0 and len(buf) >= size:
                end = size
            if end < 0:
                break
            frames.append(bytes(buf[:end]))
            del buf[:end]
            self._searched = 0
        if delimiter:
            self._searched = max(0, len(buf) - len(delimiter) + 1)
        return frames

    def flush(self) -> bytes:
        """What is waiting, as the last frame (b"" when nothing is)."""
        frame = bytes(self._buf)
        self._buf.clear()
        self._searched = 0
        return frame


class _Demand:
    """Whether an open-on-connect mapping's serial port is wanted now: ``wanted`` is set
    while it is, ``idle`` while it is not (two events, so either can be awaited)."""

    def __init__(self) -> None:
        self.wanted = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()

    def set(self, wanted: bool) -> None:
        if wanted:
            self.idle.clear()
            self.wanted.set()
        else:
            self.wanted.clear()
            self.idle.set()


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


def trace_path(trace_dir: str, mapping: MappingConfig) -> str:
    """Where a mapping's trace goes: <trace_dir>/<mapping id>-<file name>. The id
    prefix keeps two mappings with the same file name apart (and makes a mapping's
    files easy to find); the name is already sanitised by config.trace_file_name.
    Raises ValueError if that would not be a file in trace_dir (config.mapping_file)."""
    return mapping_file(trace_dir, mapping.id, f"-{mapping.options.trace_file}")


def open_tracer(mapping: MappingConfig, trace_dir: Optional[str], log,
                status: Optional["RunnerStatus"] = None) -> Optional["Tracer"]:
    """The mapping's tracer when tracing is on (None when off or unavailable). `status`:
    the runner's, whose trace_error the tracer keeps up to date."""
    if not mapping.options.trace_file:
        return None
    if not trace_dir:
        log("trace not written: no data directory to keep it in")
        return None
    try:
        path = trace_path(trace_dir, mapping)
    except ValueError as e:
        log(f"trace not written: {e}")
        return None
    tracer = Tracer(path, mapping.options, log=log, status=status)
    if not tracer.error:  # else it said why, and keeps trying
        log(f"tracing traffic to {tracer.path}")
    return tracer


async def retrace(runner, trace_dir: Optional[str], log) -> bool:
    """Open or close a running runner's trace as its mapping's options ask for now (a
    timed capture begun or ended: MappingOptions.trace_file), leaving its clients, serial
    port and listener as they are. Returns whether anything changed. Called in the
    mapping's turn (Supervisor.retrace)."""
    mapping = runner.mapping
    want = ""
    if mapping.options.trace_file and trace_dir:
        with contextlib.suppress(ValueError):
            want = trace_path(trace_dir, mapping)
    old = runner._tracer
    if (old.path if old is not None else "") == want:
        return False
    runner._tracer = None  # nothing is traced from here on
    if old is not None:
        await old.aclose()
        log(f"tracing to {old.path} ended")
    if want:
        runner._tracer = open_tracer(mapping, trace_dir, log, runner.status)
    return True


def remove_files(paths) -> dict[str, Optional[OSError]]:
    """Delete these files: path -> None once it is gone (also when it was gone already),
    else the OSError (e.g. Windows: the file is open for a download)."""
    out: dict[str, Optional[OSError]] = {}
    for path in paths:
        try:
            os.remove(path)
            out[path] = None
        except FileNotFoundError:
            out[path] = None
        except OSError as e:
            out[path] = e
    return out


class _Job:
    """Something a Tracer's writer thread does in turn with the lines queued before it."""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.future: concurrent.futures.Future = concurrent.futures.Future()

    def run(self) -> None:
        try:
            self.future.set_result(self.fn())
        except BaseException as e:  # for the waiter
            self.future.set_exception(e)


class Tracer:
    """Append a mapping's traffic to its trace file (optional hex dump + timestamp)
    without blocking the event loop.

    ``write()`` only queues the chunk (with its arrival time); a writer thread formats,
    writes and flushes in batches and rotates the file once it passes ``max_bytes``
    (<file>.1 ... <file>.<keep>; keep=0 begins a new file; rotated files past keep, left
    when it was lowered, are deleted when the trace starts). If the disk can't keep
    up, at most QUEUE_MAX_BYTES wait in memory: newer chunks are dropped and the
    trace says how many bytes are missing.

    When the file can't be opened or written (the traces folder was removed, the disk is
    full, no file descriptor is left, another program holds the file), the trace pauses
    instead of ending: `error` says why (also the runner's status.trace_error), write()
    only counts the bytes, the writer tries again every RETRY_S seconds (making the folder
    again), and once it writes again the trace says how many bytes are missing. The log
    says so at most once per LOG_EVERY_S. So does a disk almost full: below
    storage.floor() free space (checked at each open and after every SPACE_CHECK_BYTES
    written) the trace pauses before the disk fills up, and goes on once there is 10%
    more again."""

    QUEUE_MAX_BYTES = 8 * 1024 * 1024
    BATCH = 256
    RETRY_S = 5.0
    LOG_EVERY_S = 600.0
    SPACE_CHECK_BYTES = 8 * 1024 * 1024

    def __init__(self, path: str, options, log=None,
                 status: Optional["RunnerStatus"] = None) -> None:
        self.path = path
        self._hex = options.trace_hexdump
        self._ts = options.trace_timestamp
        self._max_bytes = max(1, int(options.trace_max_mb * 1024 * 1024))
        self._keep = int(options.trace_keep)
        self._log = log or (lambda _m: None)
        self._log_failure = _LogEvery(self._log, self.LOG_EVERY_S)
        self._log_rotation = _LogEvery(self._log, self.LOG_EVERY_S)
        self._log_resumed = _LogEvery(self._log, self.LOG_EVERY_S)
        self._status = status
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._queued = 0      # bytes waiting for the writer
        self._dropped = 0     # bytes dropped since the last line written
        self._drop_reason = ""
        self._closing = False  # aclose() was called: nothing more is queued
        self._done = False     # the writer has ended (remove_files then returns None)
        self._resumed = False  # opened again after an error: logged once it writes
        self._unchecked = 0    # bytes written since the free space was checked
        self._begun: Optional[float] = None  # when the traffic in the file begins (None: empty)
        self._low_space = False  # paused for the free space (goes on with some to spare)
        self.error = ""        # why the trace is not written now ("" while it is)
        self._fh: Optional[io.BufferedWriter] = None
        try:
            self._reopen()
        except OSError as e:
            self._failed(e)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"trace:{os.path.basename(path)}")
        self._thread.start()

    @property
    def active(self) -> bool:
        """The trace is on (it may be paused, see `error`), not closed."""
        return not self._closing

    def write(self, direction: str, data: bytes) -> None:
        if not data or self._closing:
            return
        with self._lock:
            if self.error:  # paused: only counted (the trace says so once it goes on)
                self._dropped += len(data)
                return
            if self._queued + len(data) > self.QUEUE_MAX_BYTES:
                self._dropped += len(data)
                self._drop_reason = "the trace file could not keep up"
                return
            self._queued += len(data)
        self._q.put((time.time(), direction, bytes(data)))

    def annotate(self, text: str) -> None:
        """Put a note into the trace, in turn with the traffic: a "!!" line like the one
        that says how many bytes are missing (not while the trace is paused or closing)."""
        data = text.encode("utf-8", "replace")
        if self._closing:
            return
        with self._lock:
            if self.error:
                return
            self._queued += len(data)
        self._q.put((time.time(), "!!", data))

    def remove_files(self, paths) -> Optional[concurrent.futures.Future]:
        """Delete some of this trace's files in the writer thread, in turn with the lines
        queued before (so never in the middle of a rotation). The file it writes is
        closed first and begun again with the next traffic: a deleted trace is never
        written on (on POSIX an open file that was deleted keeps its disk space, on
        Windows it can't be deleted). The future's result: as remove_files(). None once
        the writer has ended: they are files like any other then."""
        job = _Job(lambda: self._remove(paths))
        with self._lock:
            if self._done:
                return None
            self._q.put(job)
        return job.future

    def _prune_rotated(self) -> None:
        """Delete this trace's rotated files past the number kept (<file>.N, N > keep):
        left when keep was lowered, no rotation would ever reach them. Logged."""
        folder, base = os.path.split(os.path.abspath(self.path))
        try:
            names = os.listdir(folder)
        except OSError:
            return
        extra = []
        for name in names:
            number = name[len(base) + 1:] if name.startswith(base + ".") else ""
            if number.isascii() and number.isdigit() and int(number) > self._keep:
                extra.append(os.path.join(folder, name))
        gone = [p for p, e in remove_files(extra).items() if e is None]
        if gone:
            self._log(f"trace: deleted {len(gone)} rotated file(s) of {self.path} past the "
                      f"{self._keep} kept")

    def renew_if_older(self, begun_before: float) -> Optional[concurrent.futures.Future]:
        """Have the writer begin a new file (rotating this one, as at its size) if the file
        it writes holds traffic from before `begun_before`, in turn with the lines queued
        before: the retention by age (trace_max_age_days) can then delete it. The future's
        result: whether it did. None once the writer has ended."""
        job = _Job(lambda: self._renew(begun_before))
        with self._lock:
            if self._done:
                return None
            self._q.put(job)
        return job.future

    def _renew(self, begun_before: float) -> bool:
        if self._fh is None or self._begun is None or self._begun >= begun_before:
            return False
        self._rotate()
        return True

    def _remove(self, paths) -> dict[str, Optional[OSError]]:
        mine = os.path.normcase(os.path.abspath(self.path))
        if any(os.path.normcase(os.path.abspath(p)) == mine for p in paths):
            self._close_file()
        return remove_files(paths)

    def _format(self, ts: float, direction: str, data: bytes) -> bytes:
        prefix = time.strftime("%Y-%m-%d %H:%M:%S ", time.localtime(ts)) if self._ts else ""
        if self._hex and direction != "!!":  # (a note of the trace's own stays text)
            return f"{prefix}{direction} [{len(data)}] {data.hex(' ')}\n".encode()
        return prefix.encode() + direction.encode() + b" " + data + b"\n"

    def _lines(self, ts: float, direction: str, data: bytes) -> list:
        """The trace lines of one chunk, each with the traffic bytes it stands for: what
        was not traced before it (if anything), then the chunk."""
        with self._lock:
            self._queued -= len(data)
            dropped, self._dropped = self._dropped, 0
            reason = self._drop_reason
        lines = []
        if dropped:
            lines.append((self._format(ts, "!!", f"{dropped} bytes not traced "
                                       f"({reason})".encode()), dropped))
        # (a note (annotate) stands for no traffic: one not written is not counted)
        lines.append((self._format(ts, direction, data), 0 if direction == "!!" else len(data)))
        return lines

    def _run(self) -> None:
        self._prune_rotated()
        stop = False
        while not stop:
            try:  # paused: wake up to try the file again
                first = self._q.get(timeout=self.RETRY_S) if self.error else self._q.get()
            except queue.Empty:
                try:
                    self._reopen()
                except OSError as e:
                    self._failed(e)
                continue
            batch = [first]
            while len(batch) < self.BATCH:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            out: list = []
            try:
                for item in batch:
                    if item is None:
                        stop = True
                    elif isinstance(item, _Job):
                        self._append(out)
                        out = []
                        item.run()
                    else:
                        out.extend(self._lines(*item))
                self._append(out)
            except Exception as e:  # never end the writer: the trace would stop unnoticed
                self._failed(e)
        with self._lock:
            self._done = True
        self._close_file()
        self._set_error("")
        while True:  # a job queued as the writer ended: the file is closed now
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _Job):
                item.run()

    def _append(self, lines: list) -> None:
        """Write a batch of lines; rotate as soon as the file reaches its size. When that
        fails, the trace pauses and the lines not written are counted as not traced."""
        if not lines:
            return
        done = 0  # lines written
        try:
            fh = self._fh if self._fh is not None else self._reopen()
            size = fh.tell()
            start = 0
            for i, (line, _n) in enumerate(lines):
                size += len(line)
                if size >= self._max_bytes:
                    self._write(lines[start:i + 1])
                    done = start = i + 1
                    self._rotate()
                    size = 0
            if start < len(lines):
                self._write(lines[start:])
            done = len(lines)
        except Exception as e:  # OSError mostly; ValueError for a file closed under it
            self._failed(e, lines[done:])
            return
        if self._resumed:
            self._resumed = False
            self._log_resumed(f"trace written again ({self.path})")

    def _write(self, lines: list) -> None:
        if self._fh is None:
            raise OSError(f"{self.path} is not open")
        data = b"".join(line for line, _n in lines)
        if self._begun is None:
            self._begun = time.time()
        self._unchecked += len(data)
        if self._unchecked >= self.SPACE_CHECK_BYTES:
            self._unchecked = 0
            storage.check_space(os.path.dirname(os.path.abspath(self.path)))
        self._fh.write(data)
        self._fh.flush()

    def _rotate(self) -> None:
        """Move the file to <file>.1 (the older ones up), or with keep=0 delete it, and
        begin a new one. Never cut in place, so a download still reading the file gets
        all of it (POSIX; on Windows a file open for a download can't be renamed or
        deleted: the trace goes on in the same file until it can). Raises OSError when
        the new file can't be opened."""
        fh, self._fh = self._fh, None
        if fh is not None:
            fh.close()
        try:
            if self._keep > 0:
                for i in range(self._keep, 0, -1):
                    src = self.path if i == 1 else f"{self.path}.{i - 1}"
                    if os.path.exists(src):
                        os.replace(src, f"{self.path}.{i}")
            else:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.path)
        except OSError as e:  # e.g. Windows: a rotated file is open for download
            self._log_rotation(f"trace rotation failed ({self.path}): {e}; "
                               "continuing in the same file")
        self._reopen()

    def _reopen(self) -> io.BufferedWriter:
        """Open the file to append, making its folder again (it may have been removed).
        Raises OSError (storage.LowSpace: the disk is almost full)."""
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, mode=0o700, exist_ok=True)
        storage.check_space(folder, resuming=self._low_space)
        self._fh = open(self.path, "ab")
        self._unchecked, self._low_space = 0, False
        self._begun = self._first_time() if self._fh.tell() else None
        if self.error:
            self._resumed = True
            self._set_error("")
        return self._fh

    def _first_time(self) -> float:
        """When the traffic in the (not empty) file begins: the time of its first line
        (trace_timestamp), else when it was last written (no earlier time is known)."""
        try:
            with open(self.path, "rb") as fh:
                head = fh.read(19).decode("ascii")
            return time.mktime(time.strptime(head, "%Y-%m-%d %H:%M:%S"))
        except (OSError, ValueError, OverflowError):
            try:
                return os.path.getmtime(self.path)
            except OSError:
                return time.time()

    def _close_file(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()

    def _set_error(self, text: str) -> None:
        with self._lock:
            self.error = text
        if self._status is not None:
            self._status.trace_error = text

    def _failed(self, e: BaseException, lost=()) -> None:
        """Opening or writing the file failed: close it, count the lines `lost` as not
        traced and pause until the file can be opened again."""
        self._close_file()
        reason = (e.strerror if isinstance(e, OSError) and e.strerror else str(e)) \
            or type(e).__name__
        self._low_space = isinstance(e, storage.LowSpace)
        with self._lock:
            self._dropped += sum(n for _line, n in lost)
            self._drop_reason = ("the disk was almost full" if self._low_space else
                                 "the trace file could not be written")
        self._set_error(reason)
        self._log_failure(f"trace not written ({self.path}): {reason}; trying again every "
                          f"{self.RETRY_S:g} s (the trace will say how much is missing)")

    async def aclose(self) -> None:
        """Flush what is queued, close the file. Waits for the writer off the loop."""
        if self._closing:
            return
        self._closing = True
        self._q.put(None)
        await asyncio.to_thread(self._thread.join, 10)


@dataclass
class RunnerStatus:
    state: str = "stopped"  # stopped|running|reconnecting|device-missing|error
    device: str = ""
    client_count: int = 0
    # connected peers, in the order they connected: each {"peer": "ip:port",
    # "connected_at": time.monotonic() then, "priority": bool, "listen_only": bool (not
    # one of network.writer_client_ips: what it sends is dropped)}
    clients: list[dict] = field(default_factory=list)
    # network.open_on_connect: the mapping runs with its serial port closed, until a client
    # connects (`state` stays "running": the listener is up)
    waiting: bool = False
    # counted at the device, once (not per client, no telnet/RFC2217 control bytes);
    # serial<->serial bridge: in = port A -> B, out = port B -> A
    bytes_in: int = 0       # read from the serial device
    bytes_out: int = 0      # handed to the serial device (from clients, datagrams, the console)
    bytes_dropped: int = 0  # sent for the device but not written: the port was not open,
    #                         or a UDP datagram found UDP_BACKLOG_MAX bytes waiting
    reconnects: int = 0     # serial port: attempts to open it again (failed opens + losses)
    open_failures: int = 0  # of which: opens that failed
    device_lost: int = 0    # times the open port went away
    hangups: int = 0        # of those: hung up and reopened at once (SerialLink)
    dropped_clients: int = 0   # clients dropped because their output queue overflowed
    queue_overflows: int = 0   # total serial->net chunks lost to queue overflow
    last_error: str = ""
    # state "error" because the listener could not take its address: the supervisor tries
    # again at this time.monotonic() (0: no retry pending); as_dict() shows it as
    # retrying / next_retry_s
    retry_at: float = 0.0
    # a connect-out mapping's connection to its remote server ("" for any other mapping,
    # and while stopped): "connecting" (no attempt has failed since the last one that
    # worked, or the link was just closed), "up", or "down" (the last attempt failed:
    # link_error says why). `state` keeps reporting the serial side.
    link: str = ""
    link_error: str = ""
    link_since: float = 0.0   # time.time() of the last change of `link`
    # the connect-out target the link is on, or the one being tried ("host:port")
    link_target: str = ""
    connect_failures: int = 0  # connect-out attempts that failed
    # why the serial port can't be opened, when that can be told (portdiag.Note: the group
    # the device needs, the mapping that has it open, ...), or, with state "error", why the
    # system refused the listener's port and what to do (engine/bindhint.py); last_error
    # keeps the error itself
    hint: Optional[portdiag.Note] = None
    # what to know about the serial port(s) open now (portdiag.Note): a setting a port did
    # not take at open (RTS / DTR, RS-485 mode); as_dict() has them (and hint) as text
    warnings: tuple = ()
    trace_error: str = ""   # why the mapping's trace is not written now (bridge.Tracer)
    # the serial devices open now, for the Ports page's "in use" (both ports of a
    # serial<->serial bridge, whose `device` reads "A <-> B"); not part of as_dict()
    devices: tuple[str, ...] = ()
    # what the device sends (watch_traffic): time.time() of the last byte seen from it,
    # and, by time.monotonic(), since when it has sent nothing (from the port's opening)
    # and since when what was sent to it has had no answer
    last_rx: float = 0.0
    _seen: tuple[int, int] = (0, 0)
    _quiet_since: Optional[float] = None
    _unanswered_since: Optional[float] = None

    def set_link(self, link: str, error: str = "") -> None:
        if link != self.link:
            self.link, self.link_since = link, time.time()
        self.link_error = error
    # Modbus gateway in edge mode: each polled unit, {"unit", "state" (unknown | online |
    # offline), "cause" (offline: "no reply" | "port down"), "since" (unix time of the last
    # change, None before one), "errors" (failed point reads)}
    units: list[dict] = field(default_factory=list)
    # ... and each polled point's last read: {"name", "unit", "value" (the last value read,
    # None before one), "ts" (when, ms since the epoch), "error" (why the last read failed;
    # "" when it gave the value)}
    points: list[dict] = field(default_factory=list)
    # the MQTT publisher's connection to its broker while the mapping runs (empty: MQTT
    # off): MqttPublisher.state — "state" (connecting | connected | down | off), "broker",
    # "since", "error", "dropped", "protocol" ("3.1.1" | "5"); MQTT 5 also "reason_code"
    # and "rejected"
    mqtt: dict = field(default_factory=dict)
    # a Modbus gateway whose points have safe values (empty: none, or not running):
    # "state" — ok (the broker, or the Sparkplug primary host, is there), waiting (it is
    # away: the points' safe_after_s are running) or applied (every safe value is
    # written); "watching": "broker" or "primary host"; "since" (unix time it went away,
    # None while ok); "points": [{"name", "value", "after_s", "written" (unix time, None:
    # not yet), "error" (why the last try to write it failed)}]
    safe: dict = field(default_factory=dict)

    def set_clients(self, clients) -> None:
        """Publish the connected clients (access.Client: peer, seq, connected_at, priority),
        first connected first."""
        self.client_count = len(clients)
        self.clients = [{"peer": c.peer, "connected_at": c.connected_at, "priority": c.priority,
                         "listen_only": getattr(c, "listen_only", False)}
                        for c in sorted(clients, key=lambda c: c.seq)]

    def watch_traffic(self, now: float) -> None:
        """Compare the counters with the last look (as_dict, every POLL_S at least: the
        notifier's): new bytes from the device end its silence; bytes to it with no reply
        since begin a wait for one. Precise to that interval, which the no-data alarm
        (at least NO_DATA_MIN_S) allows, and nothing is added to the data path."""
        seen, self._seen = self._seen, (self.bytes_in, self.bytes_out)
        if self.state != "running" or self.waiting:  # (waiting: the port is closed)
            self._quiet_since = self._unanswered_since = None
            return
        if self.bytes_in != seen[0]:
            self.last_rx = time.time()
            self._quiet_since = now
            self._unanswered_since = None
        elif self._quiet_since is None:  # the port opened (or a first look): from now
            self._quiet_since = now
        if self.bytes_out != seen[1] and self.bytes_in == seen[0] and \
                self._unanswered_since is None:
            self._unanswered_since = now

    def as_dict(self) -> dict:
        now = time.monotonic()
        self.watch_traffic(now)
        # connected_at (Unix time, whole seconds): the dashboard counts the duration on from
        # it. Worked out from the monotonic time each time, so it stays the same from one
        # refresh to the next, and a clock step moves it along with the server's clock
        wall = time.time() - now
        clients = [
            {"peer": c["peer"], "duration": fmt_duration(now - c["connected_at"]),
             "connected_s": max(0, int(now - c["connected_at"])),  # the same, as a number
             "priority": c.get("priority", False),
             "listen_only": c.get("listen_only", False),
             "connected_at": float(round(wall + c["connected_at"]))}
            for c in self.clients
        ]
        return {
            "state": self.state,
            "device": self.device,
            "waiting": self.waiting,
            "client_count": self.client_count,
            "clients": clients,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "bytes_dropped": self.bytes_dropped,
            "reconnects": self.reconnects,
            "open_failures": self.open_failures,
            "device_lost": self.device_lost,
            "hangups": self.hangups,
            "dropped_clients": self.dropped_clients,
            "queue_overflows": self.queue_overflows,
            "last_error": self.last_error,
            "retrying": bool(self.retry_at),
            "next_retry_s": (max(0, math.ceil(self.retry_at - time.monotonic()))
                             if self.retry_at else 0),
            "link": self.link,
            "link_error": self.link_error,
            "link_since": int(self.link_since),
            "link_target": self.link_target,
            "connect_failures": self.connect_failures,
            # in the web UI's language on the dashboard (English for the REST API)
            "hint": self.hint.text(i18n.gettext) if self.hint else "",
            "warnings": portdiag.texts(self.warnings, i18n.gettext),
            "trace_error": self.trace_error,
            # the device: when it last sent something (None: not yet), how long it has sent
            # nothing and how long what was sent to it has had no answer (s; None: not
            # running / nothing waits for an answer)
            "last_rx": self.last_rx or None,
            "rx_idle_s": _since(self._quiet_since, now),
            "reply_wait_s": _since(self._unanswered_since, now),
            "units": [dict(u) for u in self.units],
            "points": [dict(p) for p in self.points],
            "mqtt": dict(self.mqtt),
            "safe": ({**self.safe, "points": [dict(p) for p in self.safe.get("points", ())]}
                     if self.safe else {}),
        }


def _since(mark: Optional[float], now: float) -> Optional[float]:
    return None if mark is None else round(now - mark, 1)


def log_ignored_settings(mapping: MappingConfig, log) -> None:
    """At a runner's start: each setting of the mapping that does nothing, in English
    (MappingConfig.ignored_settings; the edit form shows the same)."""
    with i18n.english():
        for text in mapping.ignored_settings():
            log(f"warning: {text}")
_BANNER_ESCAPE = re.compile(r"\\([\\dNpIrn])")


def _render_banner(template: str, mapping: MappingConfig, device: str, peer: str) -> bytes:
    """The connect banner: \\d the device, \\N the mapping's name, \\p its port, \\I the
    client's IP, \\r \\n a line break, \\\\ a backslash (C:\\\\data stays C:\\data); any other
    backslash is kept. One pass, so what an escape puts in is not read again."""
    values = {"d": device, "N": mapping.name, "p": str(mapping.network.port), "I": peer,
              "r": "\r", "n": "\n", "\\": "\\"}
    return _BANNER_ESCAPE.sub(lambda m: values[m.group(1)], template).encode("utf-8", "replace")


class _ClientConn:
    def __init__(self, runner: "MappingRunner", reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, peer_ip: str, peer_port: int = 0,
                 priority: bool = False, listen_only: bool = False):
        self.runner = runner
        self.reader = reader
        self.writer = writer
        self.peer_ip = peer_ip
        self.peer_port = peer_port
        self.peer = netguard.client_label(peer_ip, peer_port)
        self.priority = priority
        # not one of network.writer_client_ips: it gets the device's output, and what it
        # sends for the device is dropped (counted; the log says so once)
        self.listen_only = listen_only
        self._input_refused = False
        self.seq = connection_order()
        self.connected_at = time.monotonic()
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
        self._unsettled = 0  # bytes written to the port since its last BREAK / setting change

    @property
    def closing(self) -> bool:
        """Kicked, dropped or ending: no longer one of the mapping's connections (it stays
        in the runner's set until its handler is done)."""
        return self._closing

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
        if ((m.network.protocol == "rfc2217" and ser is None)
                or (self.runner.drops_clients and not self.runner._link.is_open)):
            # an RFC 2217 session drives the port, so it starts only while the port is open,
            # as does any session with disconnect_without_device (a connect-out link can
            # come up just after the port went away)
            self._closing = True
            self.close_reason = "the serial port is not open"
            await _close_client(self.writer)
            return
        self._session = make_session(m.network.protocol, ser,
                                     poll_interval=m.options.rfc2217_poll_modem_interval_s,
                                     read_only=m.network.read_only or self.listen_only,
                                     log=self._protocol_log,
                                     signature=m.options.rfc2217_signature)

        # initial bytes: protocol negotiation, then banner
        init = bytearray(self._session.initial_net_bytes())
        if m.options.banner:
            init += _render_banner(m.options.banner, m, self.runner.status.device, self.peer_ip)

        try:
            if init:  # (a client that resets right after connecting fails this write)
                self.writer.write(bytes(init))
                await self.writer.drain()
            async with TaskGroup() as tg:
                tg.create_task(self._pump_net_to_serial())
                tg.create_task(self._pump_serial_to_net())
                if self._session.poll_interval:
                    tg.create_task(self._pump_poll())
                if m.options.idle_timeout_s > 0:
                    tg.create_task(self._idle_guard(m.options.idle_timeout_s))
                if m.options.max_connection_time_s > 0:
                    tg.create_task(self._lifetime_guard(m.options.max_connection_time_s))
        except (ConnectionError, OSError) as e:  # the client left before the pumps started
            if not self.close_reason:
                self.close_reason = describe_disconnect(e)
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
            self._release_break()

    def _release_break(self) -> None:
        """An RFC 2217 client that set BREAK on (SET-CONTROL) and left without turning it
        off: turn it off, so the line is not held in the break condition for whoever
        comes next."""
        left_on = getattr(self._session, "break_left_on", None)
        ser = self.runner.serial_instance
        if left_on is None or not left_on() or ser is None:
            return
        try:
            ser.break_condition = False
        except Exception as e:
            self.runner._log(f"serial BREAK left on by {self.peer} not turned off: "
                             f"{type(e).__name__}: {e}")
            return
        self.runner._log(f"serial BREAK turned off: {self.peer} left it on")

    def _send_net(self, ctrl: bytes) -> None:
        """Control bytes for the client (telnet / RFC 2217 replies)."""
        if ctrl:
            with contextlib.suppress(asyncio.QueueFull):
                self._out.put_nowait(ctrl)

    async def _pump_net_to_serial(self) -> None:
        m = self.runner.mapping
        runner = self.runner
        session = self._session
        assert session is not None  # noqa: S101 - the pumps start after run() created it
        while not self._closing:
            data = await self.reader.read(READ_CHUNK)
            if not data:
                break  # client closed
            self._last_activity = time.monotonic()
            parts = session.from_network_parts(data)
            self._send_net(session.take_net_out())
            written = False
            # in order: the data for the port, a BREAK, the commands that act on the port
            # (each after what came before it; see the module docstring)
            for part in parts:
                if self.listen_only and (isinstance(part, bytes) or part is BREAK):
                    self._refuse_input(len(part) if isinstance(part, bytes) else 0)
                    continue
                if isinstance(part, bytes):
                    if not m.network.read_only and await runner.serial_write(part):
                        written = True
                        self._unsettled += len(part)
                    continue
                if part is BREAK or part.settle:
                    if self._unsettled:
                        await runner.serial_settled(self._unsettled)
                        self._unsettled = 0
                if part is BREAK:
                    await runner.send_break(f"client {self.peer}")
                else:
                    part.apply()
                    self._send_net(session.take_net_out())
            if not written:
                # Nothing reached the port (read-only, only telnet/RFC 2217 commands, or
                # the port is not open), so no serial drain() paces this client: let the
                # other tasks run before the next read, which returns at once while the
                # socket buffer holds more.
                await asyncio.sleep(0)
        raise _Disconnect("closed by peer")

    def _refuse_input(self, n: int) -> None:
        """A listen-only client sent ``n`` bytes for the device (0: a BREAK): dropped and
        counted; the log says so once per connection."""
        self.runner.status.bytes_dropped += n
        if not self._input_refused:
            self._input_refused = True
            self.runner._log(f"client {self.peer} only listens (it is not one of the writer "
                             "client IPs): what it sends for the device is dropped")

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
        assert session is not None  # noqa: S101 - as above
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

    def request_graceful_close(self, reason: str = "closed by closeon match") -> None:
        """Stop feeding new serial data, but let already-queued bytes flush to the
        client before closing (used by closeon so the triggering data is delivered)."""
        if self._closing:
            return
        self._closing = True
        self.close_reason = self.close_reason or reason
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
                                on_change=self._serial_changed, owner=mapping.name)
        self._bridge_backoff: Optional[Backoff] = None  # a serial<->serial bridge's retries
        self._access = AccessPolicy(mapping.network)
        self._drop_log = _LogEvery(self._log)  # data for the device that was not written
        self._break_lock = asyncio.Lock()      # one serial BREAK at a time
        self._break_log = _LogEvery(self._log)
        self._refusal_log = netguard.RefusalLog(self._log)  # refused connections
        # MQTT writes (<base_topic>/tx) waiting for the serial port, and their bytes
        self._tx_topic = ""
        self._tx_queue: list[bytes] = []
        self._tx_queued = 0
        self._tx_wake = asyncio.Event()
        self._tx_log = _LogEvery(self._log)
        self._tx_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None  # options.poll_string
        co = mapping.options.closeon
        # nothing to close on UDP (ignored_settings says so at start)
        if mapping.kind == "net" and mapping.network.mode == "udp":
            co = ""
        self._closeon = co.encode("utf-8", "replace") if co else b""
        self._closeon_tail = b""  # rolling carry-over so matches across reads aren't missed
        # packing (options.pack_*, raw protocol): the frames the clients get
        self._packer: Optional[Packer] = None
        self._pack_gap = 0.0
        self._pack_timer: Optional[asyncio.TimerHandle] = None
        self._pack_note = self._set_up_packing()
        # open on connect: who wants the port (clients, those waiting for it, the console)
        self._demand = _Demand()
        self._pending = 0  # clients waiting for the port before they are admitted
        self._listener_log = _LogEvery(self._log)  # UDP senders that only listen
        self._listeners_said: set[str] = set()

    def _set_up_packing(self) -> str:
        """Build the packer from options.pack_*; what the log says about it at start."""
        m = self.mapping
        if m.kind != "net" or m.network.protocol != "raw":
            return ""
        try:
            pack = pack_settings(m.options)
        except ValueError as e:
            return f"warning: packing is off, the device's output is forwarded as it arrives: {e}"
        if pack is None:
            return ""
        gap, delimiter, size = pack
        self._packer = Packer(delimiter, size)
        self._pack_gap = gap or PACK_IDLE_FLUSH_S
        ends = ([f"{gap * 1000:g} ms without a byte"] if gap else []) \
            + ([f"the delimiter {delimiter!r}"] if delimiter else []) + [f"{size} bytes"]
        return ("packing the device's output: a frame ends after " + ", ".join(ends[:-1])
                + (" or " if len(ends) > 1 else "") + ends[-1]
                + ("" if gap else f" (an unfinished one is sent after {PACK_IDLE_FLUSH_S:g} s "
                                  "without a byte)")
                + (", one datagram per frame" if m.network.mode == "udp" else ""))

    @property
    def serial_instance(self):
        """The open pyserial object (None while the port is closed)."""
        return self._link.serial

    @property
    def tracer(self) -> Optional[Tracer]:
        """The trace writer while the mapping runs with tracing on."""
        return self._tracer

    async def retrace(self) -> bool:
        """Open or close the trace as the options ask for now (bridge.retrace)."""
        return await retrace(self, self.trace_dir, self._log)

    @property
    def listener(self) -> Optional[netguard.Listener]:
        """The server mode listener while the mapping runs, else None."""
        return self._server

    @property
    def needs_port(self) -> bool:
        """A client session runs only while the serial port is open: RFC 2217 (it drives
        the port), any TCP mapping with disconnect_without_device, and one that opens the
        port for its clients (open_on_connect: a client is taken once the port is open, so
        what it sends first is not lost)."""
        return (self.mapping.network.protocol == "rfc2217" or self.drops_clients
                or self.on_demand)

    @property
    def on_demand(self) -> bool:
        """open_on_connect, on a TCP server mapping (true itself, not text)."""
        net = self.mapping.network
        return (net.open_on_connect is True and net.mode == "server"
                and self.mapping.kind == "net")

    def _demand_changed(self) -> None:
        """Open on connect: the port is wanted while a client is connected or waits for it,
        or the web console is open."""
        if self.on_demand:
            self._demand.set(bool(self._clients or self._pending or self._monitors))

    @property
    def drops_clients(self) -> bool:
        """disconnect_without_device, on a TCP mapping (true itself, not text)."""
        net = self.mapping.network
        return net.disconnect_without_device is True and net.mode in ("server", "client")

    def wake_serial(self) -> None:
        """A serial device was plugged in (PortWatcher): a port waiting to be opened again
        is tried now instead of after its backoff."""
        self._link.wake()
        if self._bridge_backoff is not None:
            self._bridge_backoff.wake()

    def _log(self, msg: str) -> None:
        # The logger (a per-mapping logger from AppState) adds the "[name]" prefix
        # for the global all.log and also writes to this mapping's own log file.
        if self.log:
            self.log(msg)

    # ---------------- console monitors (browser xterm) ----------------
    def add_monitor(self, mon) -> None:
        self._monitors.add(mon)
        self._demand_changed()

    def discard_monitor(self, mon) -> None:
        self._monitors.discard(mon)
        self._demand_changed()

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

        if self.mapping.kind == "serialbridge":
            self._tracer = open_tracer(self.mapping, self.trace_dir, self._log, self.status)
            log_ignored_settings(self.mapping, self._log)
            self._main_task = asyncio.create_task(
                self._serialbridge_supervisor(), name=f"serbridge:{self.mapping.name}")
            self.status.state = "running"
            self._log("serial-to-serial bridge starting")
            return

        net = self.mapping.network
        # The listener comes first (its TLS context, then the bind): a start that fails
        # there has opened nothing yet (no serial port, whose open pulses DTR/RTS, no
        # broker connection, no trace file), so the supervisor can simply try it again
        # later when the address can't be taken (BindError). Data for the device that
        # arrives before the port is open is dropped, as while it reconnects.
        udp: Optional[_UdpBridge] = None
        if net.mode == "server":
            # admission right after accept; a TLS client's allow-list check comes
            # before its handshake (netguard.start_server)
            self._server = await netguard.start_server(
                self._on_client, netguard.listen_hosts(net), net.port, ssl=server_ssl(net),
                allowed=self._access.allowed, log=self._log)
            self._log(f"listening on {netguard.listen_where(net, self._server.sockets)} "
                      f"({net.protocol}{', TLS' if net.tls else ''}, {sockopts.describe(net)})")
        elif net.mode == "udp":
            udp = _UdpBridge(self)  # a malformed stored reply address fails the start
            bound = udp
            sock = await netguard.udp_socket(net)
            try:
                self._udp_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    lambda: bound, sock=sock)
            except OSError as e:
                sock.close()
                raise netguard.BindError.of(e) from e
            self._log(f"UDP on {netguard.listen_where(net, [sock])}, {udp.describe()}")
        if self._stop.is_set():  # stopped while the listener came up: it found none to close
            await self.stop()
            return

        # If the rest fails to come up, tear everything down (the listener too), so that
        # nothing is left holding the port or the serial device.
        try:
            self._tracer = open_tracer(self.mapping, self.trace_dir, self._log, self.status)
            log_ignored_settings(self.mapping, self._log)
            # optional MQTT publisher: serial lines -> broker (net mappings), and with
            # tx_enabled writes from <base_topic>/tx -> the serial port
            if self.mapping.mqtt.enabled:
                from .mqtt_pub import MqttPublisher
                subs = self._mqtt_subscriptions()
                self._mqtt = MqttPublisher(self.mapping.mqtt, logger=self._log,
                                           subscriptions=subs)
                self.status.mqtt = self._mqtt.state
                self._mqtt.connect()
                if subs:
                    self._tx_task = asyncio.create_task(
                        self._mqtt_tx_loop(), name=f"mqtt-tx:{self.mapping.name}")
            poll = self._poll_settings()
            if poll is not None:
                self._poll_task = asyncio.create_task(
                    self._poll_loop(*poll), name=f"poll:{self.mapping.name}")
            if self._pack_note:
                self._log(self._pack_note)
            if self._access.writers_only:
                self._log("only the writer client IPs may send to the device; other clients "
                          "only listen")
            # up from here; the serial side reports its own state once it has tried the port
            self.status.state = "running"
            self.status.last_error = ""
            # serial side is shared by server/client/udp
            if self.on_demand:
                self.status.waiting = True
                self._demand_changed()
                self._serial_task = asyncio.create_task(
                    self._serial_on_demand(), name=f"serial:{self.mapping.name}")
            else:
                self._serial_task = asyncio.create_task(
                    self._link.run(self._serial_session), name=f"serial:{self.mapping.name}")
            if net.mode == "client":
                self.status.set_link("connecting")
                targets, _bad = connect_targets(net)
                self._main_task = asyncio.create_task(
                    self._client_supervisor(), name=f"connectout:{self.mapping.name}")
                self._log(f"connect-out to {host_port(*targets[0])} "
                          + (f"(then {', '.join(host_port(*t) for t in targets[1:])}) "
                             if len(targets) > 1 else "")
                          + f"({net.protocol}, {sockopts.describe(net)})")
            elif udp is not None:
                self._main_task = asyncio.create_task(udp.run(), name=f"udp:{self.mapping.name}")
        except BaseException:
            await self.stop()
            raise

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
        if self._pack_timer is not None:
            self._pack_timer.cancel()
            self._pack_timer = None
        if self._mqtt is not None:
            mqtt, self._mqtt = self._mqtt, None
            await mqtt.aclose()
        self.status.mqtt = {}
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            self._server = None
        for task in (self._serial_task, self._main_task, self._tx_task, self._poll_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._serial_task = self._main_task = self._tx_task = self._poll_task = None
        self._tx_queue.clear()
        self._tx_queued = 0
        await self._link.close()
        if self._tracer:
            await self._tracer.aclose()
            self._tracer = None
        self.status.state = "stopped"
        self.status.waiting = False
        self.status.set_link("")
        self.status.link_target = ""
        self.status.client_count = 0
        self.status.clients = []
        self._log("stopped")

    # ---------------- MQTT writes and the poll string ----------------
    def _mqtt_subscriptions(self) -> dict:
        """With mqtt.tx_enabled: {<base_topic>/tx: handler} (nothing with a wildcard in the
        base topic, which edits refuse: such a subscription would take every topic it
        matches)."""
        mq = self.mapping.mqtt
        if mq.tx_enabled is not True:
            return {}
        if mqtt_topic_problem(mq.base_topic):
            self._log("MQTT writes not taken: the base topic has '+', '#' or a null character")
            return {}
        self._tx_topic = str(mq.base_topic).rstrip("/") + "/tx"
        note = (" (read-only: each is refused)" if self.mapping.network.read_only else "")
        self._log(f"MQTT: messages on {self._tx_topic} are written to the serial port{note}")
        missing = [what for what, have in (("TLS", mq.tls), ("a username", mq.username))
                   if not have]
        if self.mapping.accepts_mqtt_tx and missing:
            self._log("warning: MQTT writes to the serial port come over a broker connection "
                      f"without {' or '.join(missing)}: any client the broker lets publish to "
                      f"{self._tx_topic} can write to the device; restrict it with the "
                      "broker's access rules (ACLs)")
        return {self._tx_topic: self._on_mqtt_tx}

    def _on_mqtt_tx(self, topic: str, payload: bytes) -> None:
        """A message on <base_topic>/tx (on the event loop; never empty or a retained copy:
        MqttPublisher). Queued for the serial port, or refused on .../tx/result."""
        mqtt = self._mqtt
        if mqtt is None:
            return
        why = ""
        if self.mapping.network.read_only:
            why = "mapping is read-only"
        elif len(payload) > MQTT_TX_MAX_BYTES:
            why = f"message too long ({len(payload)} bytes, at most {MQTT_TX_MAX_BYTES})"
        elif self._tx_queued + len(payload) > MQTT_TX_MAX_BYTES:
            why = f"busy ({self._tx_queued} bytes waiting for the serial port)"
        if why:
            self._tx_log(f"MQTT write from {topic} refused: {why}")
            mqtt.publish_raw(topic + "/result", f"error: {why}".encode())
            return
        self._tx_queue.append(payload)
        self._tx_queued += len(payload)
        self._tx_wake.set()

    async def _mqtt_tx_loop(self) -> None:
        """Write the MQTT messages to the serial port, oldest first, each in one piece (so
        never mixed into a TCP client's data), and answer each: "ok" once the port took it,
        "error: ..." when it was not open."""
        result = self._tx_topic + "/result"
        while True:
            await self._tx_wake.wait()
            self._tx_wake.clear()
            while self._tx_queue:
                payload = self._tx_queue.pop(0)
                try:
                    err = None if await self.serial_write(payload) else \
                        "the serial port is not open"
                except Exception as e:  # the port went away mid-write: SerialLink reopens it
                    err = str(e) or type(e).__name__
                finally:
                    self._tx_queued -= len(payload)
                if self._mqtt is not None:
                    self._mqtt.publish_raw(result, b"ok" if err is None else
                                           f"error: {err}".encode())

    def _poll_settings(self) -> Optional[tuple[bytes, float]]:
        """options.poll_string as bytes and poll_interval_s, or None when there is no poll
        (off, a serial-to-serial bridge; a stored value edits refuse: logged)."""
        o = self.mapping.options
        interval = o.poll_interval_s
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) \
                or not interval or not o.poll_string:
            return None
        try:
            data = poll_bytes(o.poll_string)
        except ValueError as e:
            self._log(f"poll string not sent: {e}")
            return None
        if not (0.1 <= interval <= 86400) or not data:
            self._log(f"poll string not sent: the interval {interval!r} is not between 0.1 "
                      "and 86400 seconds")
            return None
        return data, float(interval)

    async def _poll_loop(self, data: bytes, interval: float) -> None:
        """Send the poll string every ``interval`` seconds while the port is open, and (unless
        poll_with_clients) while no TCP client is connected, which does the asking then."""
        with_clients = self.mapping.options.poll_with_clients is True
        self._log(f"polling the device every {interval:g} s with {data!r}"
                  + ("" if with_clients else " while no TCP client is connected"))
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            if not self._link.is_open:
                continue
            if not with_clients and any(isinstance(c, _ClientConn) for c in self._clients):
                continue
            with contextlib.suppress(Exception):  # the port went away: SerialLink reopens it
                await self.serial_write(data)

    # ssl context for a connect-out TLS data bridge (a listener's: access.server_ssl)
    def _client_ssl(self):
        return tls.mapping_context(self.mapping.network)

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

    async def serial_settled(self, written: int) -> None:
        """Wait until the port has sent what it holds (``written``: the bytes a client
        wrote since its last BREAK / setting change), at most their line time twice plus
        0.2 s and never more than SETTLE_MAX_S."""
        w = self._link.writer
        if w is None or w.is_closing():
            return
        baud = getattr(self._link.serial, "baudrate", 0) or self.mapping.serial.baudrate
        line_time = written * 11 / max(1, int(baud))  # (a start, 8 data, parity, stop bit)
        await sent_within(w, min(SETTLE_MAX_S, 0.2 + 2 * line_time))

    async def send_break(self, who: str) -> bool:
        """Send a serial BREAK (a telnet client's IAC BRK, the web console; ``who`` for the
        log): the line is held in the break condition for BREAK_S, on the event loop's
        timer. One at a time; False when none was sent (a read-only mapping, the port not
        open, a port that can't). Logged at most once a minute, with a count."""
        if self.mapping.network.read_only:
            self._break_log(f"serial BREAK from {who} not sent: the mapping is read-only")
            return False
        async with self._break_lock:
            ser = self._link.serial
            if ser is None or self._stop.is_set():
                self._break_log(f"serial BREAK from {who} not sent: the serial port is not "
                                "open")
                return False
            try:
                ser.break_condition = True
            except Exception as e:  # (a stand-in or a port without break control)
                self._break_log(f"serial BREAK from {who} not sent: {type(e).__name__}: {e}")
                return False
            try:
                await asyncio.sleep(BREAK_S)
            finally:
                with contextlib.suppress(Exception):
                    ser.break_condition = False
        self._break_log(f"serial BREAK sent ({who})")
        return True

    def _serial_changed(self, serial) -> None:
        """SerialLink: the port was reopened (``serial``) or closed (None). With
        disconnect_without_device a closed port disconnects every client. Otherwise an RFC
        2217 session drives the port itself, so the connected client's session follows it:
        the client stays connected through a reconnect and keeps its settings."""
        if serial is None and self.drops_clients and not self._stop.is_set():
            if self._clients:
                self._log(f"serial port closed: disconnecting {len(self._clients)} "
                          "client(s) (disconnect_without_device)")
            for c in list(self._clients):
                c.kick("the serial port is not open")
        elif self.mapping.network.protocol == "rfc2217":
            for c in list(self._clients):
                c.serial_changed(serial)

    def note_listener(self, ip: str, n: int) -> None:
        """A UDP sender that is not one of the writer client IPs sent ``n`` bytes: dropped
        and counted; the log names each such address once (the first 256, then at most
        once a minute)."""
        self.status.bytes_dropped += n
        if ip in self._listeners_said:
            return
        text = (f"datagrams from {ip} are not written to the device: it is not one of the "
                "writer client IPs (it still gets the device's output)")
        if len(self._listeners_said) < 256:
            self._listeners_said.add(ip)
            self._log(text)
        else:
            self._listener_log(text)

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
        try:
            await self._serial_read_loop(reader)
        finally:
            if self._packer is not None:  # the port is gone: what waited is the last frame
                self._pack_flush()

    async def _serial_on_demand(self) -> None:
        """open_on_connect: keep the port open (SerialLink.run, as always: reopened when it
        goes away) only while it is wanted, and close it port_linger_s after the last
        client left, so another program can use it until the next one connects."""
        linger = port_linger(self.mapping.network)
        st, demand = self.status, self._demand
        self._log("the serial port is opened while a client is connected, and closed "
                  f"{linger:g} s after the last one left")
        while not self._stop.is_set():
            await demand.wanted.wait()
            st.waiting = False
            run = asyncio.create_task(self._link.run(self._serial_session),
                                      name=f"serial-open:{self.mapping.name}")
            try:
                while True:  # until nobody has wanted the port for the linger time
                    await demand.idle.wait()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(demand.wanted.wait(), timeout=linger)
                    # (a client taken just as the linger ran out wants the port, too)
                    if not demand.wanted.is_set():
                        break
            finally:
                was_open = self._link.is_open
                # No await since the last check: a client that connects from here on finds
                # the port not open, waits for it (_admit) and so has it opened again
                # once it is closed, instead of being taken on a port about to close.
                self._link.ready.clear()
                run.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run
                await self._link.close()
                if not self._stop.is_set():
                    st.waiting, st.devices = True, ()
                    if was_open or st.state not in ("device-missing", "reconnecting"):
                        # the listener is up and the port is closed on purpose: no error
                        st.state, st.hint, st.last_error = "running", None, ""
                        self._log("serial port closed: no client is connected" if was_open
                                  else "no client is connected: the serial port is not "
                                  "tried any more")
                    else:
                        # it could not be opened: it stays so, with why, until an open works
                        # (the notifier's device_back is for a device that is back)
                        self._log("no client is connected: the serial port, which could not "
                                  "be opened, is not tried any more until the next one")

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
            if self._packer is None:
                self._to_clients(data)
            else:
                self._pack(data)

    def _to_clients(self, data: bytes) -> None:
        """The device's output for the clients: as it was read, or a frame (packing)."""
        for c in list(self._clients):
            c.feed_from_serial(data)
        if self._closeon:
            self._check_closeon(data)

    def _pack(self, data: bytes) -> None:
        """Packing: pass on the frames ``data`` completes; what is left goes out when the
        gap has passed without another byte (_pack_flush)."""
        packer = self._packer
        assert packer is not None  # noqa: S101 - called only with packing on
        for frame in packer.feed(data):
            self._to_clients(frame)
        if self._pack_timer is not None:
            self._pack_timer.cancel()
            self._pack_timer = None
        if packer.pending:
            self._pack_timer = asyncio.get_running_loop().call_later(
                self._pack_gap, self._pack_flush)

    def _pack_flush(self) -> None:
        if self._pack_timer is not None:
            self._pack_timer.cancel()
            self._pack_timer = None
        frame = self._packer.flush() if self._packer is not None else b""
        if frame:
            self._to_clients(frame)

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
        self._demand_changed()

    def free_for(self, peer_ip: str) -> bool:
        """A port pool asks: would a client from ``peer_ip`` get a connection here now,
        without anyone being kicked for it? (A TCP server mapping that runs, lets the
        address in, has a connection left and, where a session needs the serial port, has
        it open or opens it for its clients.)"""
        return self.lets_in(peer_ip) and not self.full

    def lets_in(self, peer_ip: str) -> bool:
        """A port pool asks: is this a running TCP server mapping (without TLS: the pool's
        port is plain TCP) whose allow-list has ``peer_ip``?"""
        net = self.mapping.network
        return (self._server is not None and not self._stop.is_set() and net.mode == "server"
                and not net.tls and self._access.allowed(peer_ip))

    @property
    def full(self) -> bool:
        """No client would get a connection now: every one is taken (a client waiting for
        the serial port to open counts: it is taken once it is open), or a session needs
        the serial port and it is neither open nor opened for a client."""
        if self.needs_port and not self.on_demand and not self._link.is_open:
            return True
        live = [c for c in self._clients if not c.closing]
        return len(live) + self._pending >= self.mapping.network.max_connections

    async def take_client(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> Optional[_ClientConn]:
        """A client a port pool hands over (engine/pool.py), admitted as one that connected
        to the mapping's own port, except that nobody is kicked to make room for it. The
        client, for serve_client(); or None when the mapping turns it away after all (it
        stops, its serial port could not be opened in time, another client took the last
        connection meanwhile): the connection is then left as it is, for the pool's next
        member."""
        return await self._admit(reader, writer, handed=True)

    async def serve_client(self, client: _ClientConn) -> None:
        """Serve a client take_client() admitted, until it is gone."""
        await self._serve(client)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = await self._admit(reader, writer)
        if client is not None:
            await self._serve(client)

    async def _admit(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     handed: bool = False) -> Optional[_ClientConn]:
        """A new connection: the client, listed in self._clients, or None when it is turned
        away (closed, unless a port pool ``handed`` it over: take_client)."""
        if self._stop.is_set():  # accepted while the mapping stops
            if not handed:
                writer.close()
            return None
        peer_ip, peer_port = netguard.peer_address(writer)
        peer_label = netguard.client_label(peer_ip, peer_port)

        # An RFC2217 session (and any, with disconnect_without_device) needs the open serial
        # port: wait briefly for it (an allowed client) BEFORE admission, so a client
        # waiting here holds no slot and nobody is kicked for a newcomer that is then
        # turned away. (It counts as taking a connection for a port pool: full.)
        ready = self._link.ready
        if self.needs_port and not ready.is_set() and self._access.allowed(peer_ip):
            self._pending += 1  # (open on connect: the port is opened for it)
            self._demand_changed()
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ready.wait(),
                                           timeout=self.mapping.options.rfc2217_net_timeout_s)
            finally:
                self._pending -= 1
                if not ready.is_set():
                    self._demand_changed()
            if not ready.is_set():
                # (a poller retrying all the time: each reason once a minute, with a count)
                if self.on_demand and not self.drops_clients \
                        and self.mapping.network.protocol != "rfc2217":
                    self._refusal_log(f"rejected {peer_label}: the serial port could not be "
                                      f"opened ({self.status.last_error or 'not open yet'})")
                elif self.drops_clients:
                    self._refusal_log(f"rejected {peer_label}: the serial port is not open "
                                      "(disconnect_without_device)")
                else:
                    self._refusal_log(f"rejected {peer_label}: serial not ready for RFC2217")
                if not handed:
                    writer.close()
                return None
            if self._stop.is_set():
                if not handed:
                    writer.close()
                return None

        # allow-list, then max connections (a priority client / kick_old_user makes room,
        # never for a pool's client); from here until the client is in self._clients
        # nothing awaits, so connections accepted together are admitted one after the
        # other, each seeing the one before
        admission = self._access.admit(peer_ip, peer_label, self._clients, self._refusal_log,
                                       kick=not handed)
        if not admission.admitted:
            if handed:
                return None
            if admission.at_capacity:
                with contextlib.suppress(Exception):
                    writer.write(b"Busy: maximum connections reached.\r\n")
                    await writer.drain()
            writer.close()
            return None
        is_priority = admission.priority

        self._enable_keepalive(writer, peer_label)
        client = _ClientConn(self, reader, writer, peer_ip, peer_port, priority=is_priority,
                             listen_only=not self._access.may_write(peer_ip))
        self._clients.add(client)
        self._refresh_clients()
        tag = (" [priority]" if is_priority else "") \
            + (" [listen-only]" if client.listen_only else "") + tls.client_certificate(writer)
        self._log(f"client connected: {peer_label}{tag} ({len(self._clients)} total)")
        return client

    async def _serve(self, client: _ClientConn) -> None:
        """An admitted client (_admit), until it is gone; then what its leaving does."""
        try:
            await client.run()
        finally:
            self._clients.discard(client)
            self._refresh_clients()
            held = fmt_duration(time.monotonic() - client.connected_at)
            why = f" — {client.close_reason}" if client.close_reason else ""
            self._log(f"client disconnected: {client.peer} after {held}{why} "
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
    async def _connect_out(self, host: str, port: int):
        """Resolve a target's host, then connect (+ TLS handshake) to each resolved
        address in turn, giving each one connect_timeout_s — like an OS connect
        timeout per address — so a dual-stack host whose IPv6 path is dead still
        falls back to IPv4 instead of the first address eating the whole budget.

        The DNS lookup is deliberately NOT under the timeout: getaddrinfo runs in a
        worker thread that cancellation can't stop, so timing it out would leave one
        stuck thread behind per retry during a DNS outage and starve the shared
        executor (asyncio.to_thread: password hashing, config saves, log reads).
        Connecting to the resolved IP literal needs no further lookup.

        Returns (reader, writer, what the log says about TLS); over TLS the server's
        certificate is checked first (tls.check_server: a pin that does not match is that
        address's failure; with a CA file the certificate must name ``host``)."""
        net = self.mapping.network
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ssl_ctx = self._client_ssl()
        error: BaseException = ConnectionError(f"no address for {host}")
        for family, _type, _proto, _name, sockaddr in infos:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        str(sockaddr[0]), int(sockaddr[1]), family=family, ssl=ssl_ctx,
                        server_hostname=host if ssl_ctx else None),
                    timeout=net.connect_timeout_s or None)
            except asyncio.TimeoutError as e:
                if getattr(e, "errno", None) is not None:
                    error = e  # the OS's own connect timeout, not ours
                    continue
                error = ConnectionError(f"timed out after {net.connect_timeout_s:g}s")
                continue
            except OSError as e:
                error = e
                continue
            if ssl_ctx is None:
                return reader, writer, ""
            try:  # the server's certificate, before any byte of the link is used
                return reader, writer, f" ({tls.check_server(net, writer)})"
            except ConnectionError as e:
                writer.transport.abort()
                error = e
        raise error

    async def _connect_round(self, targets: list, first: int) -> tuple:
        """Try the targets once each, in order from ``first`` (the one the link was on)
        and on to the first again. Returns (index, reader, writer, TLS note) of the one
        that answered; raises ConnectionError (why each failed) when none did."""
        st = self.status
        errors = []
        for step in range(len(targets)):
            i = (first + step) % len(targets)
            st.link_target = host_port(*targets[i])
            try:
                return (i, *await self._connect_out(*targets[i]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                st.connect_failures += 1
                errors.append(str(e) if len(targets) == 1
                              else f"{host_port(*targets[i])}: {e}")
            if self._stop.is_set():
                break
        raise ConnectionError("; ".join(errors))

    async def _return_to_first(self, client: _ClientConn, target: tuple, every: float):
        """failover_return_s: while the link is on another target, try the first one every
        ``every`` seconds; once it answers, end the link in use (what is queued for it goes
        out first) and return the new connection for the supervisor to use."""
        while True:
            await asyncio.sleep(every)
            try:
                conn = await self._connect_out(*target)
            except asyncio.CancelledError:
                raise
            except Exception:  # still away (said when the link went to another target)
                continue
            client.request_graceful_close(
                f"the first target {host_port(*target)} answers again")
            return conn

    async def _client_supervisor(self) -> None:
        net = self.mapping.network
        targets, bad = connect_targets(net)
        for entry in bad:
            self._log(f"warning: connect-out target {entry!r} is not a host and a port "
                      "(host:port): left out")
        back = failover_return(net) if len(targets) > 1 else 0.0
        backoff = Backoff()
        failures = FailureLog(self._log, "connect-out failed")
        current = 0  # the target the link is on (tried first after it closes)
        # a connection to the first target, made while the link was on another one
        ready: Optional[tuple] = None
        st = self.status
        try:
            while not self._stop.is_set():
                # an RFC 2217 session drives the serial port (disconnect_without_device: no
                # session without it): connect out only while it is open
                if self.needs_port and not await self._serial_open():
                    break
                returned: Optional[tuple] = None
                try:
                    if ready is not None:
                        index, (reader, writer, tls_note) = 0, ready
                        ready = None
                    else:
                        first, current = current, 0  # (none answers: the first comes next)
                        index, reader, writer, tls_note = await self._connect_round(
                            targets, first)
                        current = first
                    target = host_port(*targets[index])
                    st.link_target = target
                    self._enable_keepalive(writer, target)
                    client = _ClientConn(self, reader, writer, *targets[index])
                    self._clients.add(client)
                    self._refresh_clients()
                    other = (f" (target {index + 1} of {len(targets)}: "
                             f"{host_port(*targets[current])} did not answer)"
                             if index != current and index else "")
                    self._log(f"connected out to {target}{tls_note}{other}"
                              f"{failures.recovered()}")
                    current = index
                    if st.link_error and st.last_error == st.link_error:
                        st.last_error = ""  # (the link's error, not one of the serial side)
                    st.set_link("up")
                    backoff.reset()
                    returner = (asyncio.create_task(
                        self._return_to_first(client, targets[0], back),
                        name=f"connectout-return:{self.mapping.name}")
                        if index and back else None)
                    try:
                        await client.run()
                    finally:
                        if returner is not None:
                            if not returner.done():
                                returner.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                returned = await returner
                        self._clients.discard(client)
                        self._refresh_clients()
                        # closed (by either end, or a timer): "down" only if the next try
                        # fails, so a max connection time or idle timeout recycle is no outage
                        st.set_link("connecting")
                        held = fmt_duration(time.monotonic() - client.connected_at)
                        why = f" — {client.close_reason}" if client.close_reason else ""
                        self._log(f"connect-out link to {target} closed after {held}{why}")
                        self._restore_port_settings()
                except asyncio.CancelledError:
                    if returned is not None:
                        returned[1].transport.abort()
                    raise
                except Exception as e:
                    st.last_error = str(e)  # (_connect_round counted each target's failure)
                    st.set_link("down", str(e))
                    failures.failed(str(e))
                if self._stop.is_set():
                    if returned is not None:
                        returned[1].transport.abort()
                    break
                if returned is not None:  # back to the first target, at once
                    ready = returned
                    continue
                if not await backoff.wait(self._stop):
                    break
        finally:
            if ready is not None:
                ready[1].transport.abort()

    # ---------------- serial <-> serial bridge ----------------
    async def _serialbridge_supervisor(self) -> None:
        """Both ports open and close together, so this keeps its own loop (SerialLink
        supervises one port). The side that could not be opened last time is opened first:
        while one port is missing, the other one is not opened and closed again at every
        retry (each open pulses its DTR and RTS: a board that resets on DTR would reset
        every few seconds)."""
        backoff = self._bridge_backoff = Backoff()
        notes = NoteLog(self._log)
        failures = FailureLog(self._log, "serial bridge error")
        st = self.status
        sides = (self.mapping.serial, self.mapping.serial_b)
        first = 0  # the side opened first: 0 = A, 1 = B
        while not self._stop.is_set():
            opened: list = [None, None]  # (reader, writer, serial, device) of each side
            was_open = False
            try:
                for side in (first, 1 - first):
                    try:
                        opened[side] = await serial_io.open_serial(sides[side])
                    except Exception:
                        first = side
                        raise
                    portdiag.opened(opened[side][2], self.mapping.name, opened[side][3])
                was_open = True
                (ra, wa, sa, da), (rb, wb, sb, db) = opened
                self.status.device = f"{da} <-> {db}"
                self.status.devices = (da, db)
                self.status.last_error = ""
                self.status.hint = None
                self.status.warnings = tuple(
                    portdiag.Note("side", (side, note))
                    for side, ser in (("A", sa), ("B", sb)) for note in serial_io.open_notes(ser))
                self.status.state = "running"
                backoff.reset()
                self._log(f"serial bridge open: {da} <-> {db}{failures.recovered()}")
                notes(self.status.warnings)
                async with TaskGroup() as tg:
                    tg.create_task(self._pump_serial_pair(ra, wb, "A>B"))
                    tg.create_task(self._pump_serial_pair(rb, wa, "B>A"))
                if not self._stop.is_set():  # (a pump ends by raising why)
                    raise ConnectionError("serial EOF / device removed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._stop.is_set():
                    # TaskGroup raises an ExceptionGroup; flatten its messages
                    inner = getattr(e, "exceptions", None)
                    st.last_error = "; ".join(str(x) for x in inner) if inner else str(e)
                    if was_open:
                        st.device_lost += 1
                        st.state = "reconnecting"
                    else:
                        st.open_failures += 1
                        st.state = "device-missing" if portdiag.missing(e) else "reconnecting"
                    failures.failed(st.last_error)
                    hint = None if was_open else await asyncio.to_thread(
                        explain, e, sides[first])
                    st.hint = portdiag.Note("side", ("AB"[first], hint)) if hint else None
            finally:
                self.status.warnings = ()
                await asyncio.gather(*(close_stream(port[1], self._log)
                                       for port in opened if port is not None))
            if self._stop.is_set():
                break
            self.status.reconnects += 1
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
# datagram from it, when that is set). The targets (udp_targets, a _UdpTarget each in
# _clients) get it too, always; a peer that is one of them is not sent it twice.
# ---------------------------------------------------------------------------
def _udp_label(addr) -> str:
    """ip:port of a datagram's sender (an IPv4 one of a socket for both families as IPv4)."""
    return netguard.client_label(canonical_ip(str(addr[0])), addr[1])


class _UdpPeer:
    """Where a UDP mapping sends the device's output; listed as its client."""

    def __init__(self, bridge: "_UdpBridge", addr, label: str = "") -> None:
        self.bridge = bridge
        self.addr = addr
        self.peer = label or _udp_label(addr)
        self.priority = False
        self.seq = connection_order()
        self.connected_at = time.monotonic()  # since when it gets the output
        self.last_heard = time.monotonic()    # its last datagram (for the peer timeout)

    def feed_from_serial(self, data: bytes) -> None:
        self.bridge.send(self, data)

    def kick(self, reason: str = "") -> None:
        pass

    def request_graceful_close(self) -> None:
        pass


class _UdpTarget(_UdpPeer):
    """A UDP target (udp_targets): always gets the device's output; listed as a client."""

    def feed_from_serial(self, data: bytes) -> None:
        self.bridge.send_target(self, data)


class _UdpBridge(asyncio.DatagramProtocol):
    """A UDP mapping's socket side. Every datagram is checked against the allow-list; an
    allowed one makes its sender the peer (unless there is a reply address) and waits in a
    queue that run() writes to the serial port. A datagram that would take the queue past
    UDP_BACKLOG_MAX bytes is dropped and counted instead."""

    def __init__(self, runner: "MappingRunner") -> None:
        self.runner = runner
        net = runner.mapping.network
        self.reply_to = udp_peer_address(net)  # ValueError: a malformed stored one
        self.targets = udp_targets(net)        # (the same)
        # the peer timeout is for a sender that was learned, not for the reply address
        self.timeout = 0.0 if self.reply_to else udp_peer_timeout(net)
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.peer: Optional[_UdpPeer] = None
        self._target_addrs: set = set()  # where the targets are sent (as the socket needs)
        self._queue: list[bytes] = []
        self._queued = 0  # bytes waiting: queued, or being written by run()
        self._wake = asyncio.Event()
        self._peer_log = _LogEvery(runner._log)
        self._refused_log = _LogEvery(runner._log)
        self._error_log = _LogEvery(runner._log)

    def describe(self) -> str:
        """Where the device's output goes (for the start-up log line)."""
        if self.reply_to:
            out = f"device output to {_udp_label(self.reply_to)}"
        elif self.timeout:
            out = (f"device output to the last sender (forgotten after "
                   f"{fmt_duration(self.timeout)} without a datagram from it)")
        else:
            out = "device output to the last sender"
        if self.targets:
            out += " and to " + ", ".join(_udp_label(t) for t in self.targets)
        return out

    # ---- asyncio.DatagramProtocol ----
    def connection_made(self, transport) -> None:
        self.transport = transport
        sock = transport.get_extra_info("socket")
        v6 = sock is not None and sock.family == socket.AF_INET6

        def sent_to(addr):
            host, port = addr
            if v6 and ipaddress.ip_address(host).version == 4:
                host = f"::ffff:{host}"  # an IPv4 peer of a socket bound to an IPv6 address
            return host, port

        r = self.runner
        for target in self.targets:
            addr = sent_to(target)
            if addr not in self._target_addrs:
                self._target_addrs.add(addr)
                r._clients.add(_UdpTarget(self, addr, _udp_label(target)))
        r._refresh_clients()
        if self.reply_to is not None:
            self._set_peer(sent_to(self.reply_to), _udp_label(self.reply_to))

    def connection_lost(self, exc) -> None:
        self.transport = None

    def error_received(self, exc: Exception) -> None:
        # e.g. the peer's port is closed (ICMP port unreachable) or there is no route to it
        hint = ""
        if (getattr(exc, "errno", None) == errno.EACCES
                and self.runner.mapping.network.udp_broadcast is not True):
            hint = " (to send to a broadcast address, allow broadcast: udp_broadcast)"
        self._error_log(f"udp error: {exc}{hint}")

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
        if not r._access.may_write(addr[0]):
            r.note_listener(canonical_ip(str(addr[0])), len(data))
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
        """The device's output, to the peer (nothing once it has expired, or when it is one
        of the targets, which get it anyway)."""
        if (self.transport is None or peer is not self.peer or self._expired()
                or tuple(peer.addr[:2]) in self._target_addrs):
            return
        with contextlib.suppress(Exception):  # a failed send reaches error_received
            self.transport.sendto(data, peer.addr)

    def send_target(self, target: _UdpTarget, data: bytes) -> None:
        """The device's output, to a target."""
        if self.transport is None:
            return
        try:
            self.transport.sendto(data, target.addr)
        except OSError as e:  # (a synchronous refusal, e.g. EACCES: a broadcast address)
            self.error_received(e)
        except Exception:
            pass

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
