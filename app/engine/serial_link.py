"""A mapping's serial port, kept open for as long as the mapping runs.

Shared by the transparent bridge (bridge.MappingRunner) and the Modbus gateway
(modbus_gateway.ModbusGatewayRunner), so both open, lose and reopen a port the same way.
``SerialLink.run(session)`` opens the port (serial_io.open_serial re-resolves a stable USB
identity on every open), runs ``session(reader, writer)`` while it is open and, when the
port can't be opened or goes away, records why in the runner's status and reopens it after
a capped backoff, until the mapping stops (the runner cancels the task).

The link's state, for the runner and anything built on it:
  - ``is_open`` / ``ready``: the port is open right now (``ready`` is the Event to wait on);
  - ``down``: it could not be opened or went away, and is not open again yet; False until
    the first attempt is over (at start-up the port may simply not be open *yet*);
  - ``reader`` / ``writer`` / ``serial`` / ``device``: the open port (None / "" otherwise);
    ``on_change`` (optional) is told each time ``serial`` changes: the pyserial object of
    a port just opened, None when it is closed (what drives the port itself, an RFC 2217
    session, follows it);
  - ``status.state``: ``running`` while it is open, ``device-missing`` / ``reconnecting``
    while it is not (what the dashboard, /metrics and the notifications report).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from . import serial_io

if TYPE_CHECKING:
    from ..config import SerialSettings
    from .bridge import RunnerStatus

# a session runs while the port is open and returns or raises once it is gone
Session = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

# closing a port: what is still waiting to be written to the device gets this long to go
# out (then it is dropped), and so does the close itself after that
SERIAL_CLOSE_TIMEOUT_S = 2.0
_SENT_POLL_S = 0.05  # how often a close looks whether the port has sent everything


class Backoff:
    """The pause between reconnect attempts: 0.5 s doubling up to ``cap`` (10 s), plus up
    to 0.4 s of jitter derived from the attempt number (not an RNG). ``attempt`` counts the
    attempts since the last success; reset() after one."""

    def __init__(self, cap: float = 10.0) -> None:
        self.cap = cap
        self.attempt = 0
        self._delay = 0.5

    def reset(self) -> None:
        self.attempt, self._delay = 0, 0.5

    def pause(self) -> float:
        """The pause before the next attempt (what wait() sleeps), counting that attempt."""
        self.attempt += 1
        pause = min(self._delay, self.cap) + (self.attempt % 5) * 0.1
        self._delay = min(self._delay * 2, self.cap)
        return pause

    async def wait(self, stop: asyncio.Event) -> bool:
        """Sleep before the next attempt; False when ``stop`` was set meanwhile."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.pause())
            return False
        except asyncio.TimeoutError:
            return True


class SerialLink:
    def __init__(self, settings: SerialSettings, status: RunnerStatus, stop: asyncio.Event,
                 log: Callable[[str], None],
                 on_change: Optional[Callable[[Any], None]] = None) -> None:
        self.settings = settings
        self.status = status
        self._stop = stop
        self._log = log
        self._on_change = on_change
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.serial: Any = None  # the pyserial object (an RFC2217 session drives it)
        self.device = ""
        self.ready = asyncio.Event()
        self.down = False

    @property
    def is_open(self) -> bool:
        return self.ready.is_set()

    async def run(self, session: Session) -> None:
        """Keep the port open until ``stop`` is set: open it, run ``session`` while it is
        open, close it once the session ends, and try again after a backoff."""
        backoff = Backoff()
        self.down = False  # the first attempt is under way
        while not self._stop.is_set():
            try:
                reader, writer, ser, device = await serial_io.open_serial(self.settings)
                self.attach(reader, writer, ser, device)
                backoff.reset()
                await session(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # SerialException, OSError, the port went away
                self.status.last_error = str(e)
                self.status.state = "device-missing" if backoff.attempt == 0 else "reconnecting"
                self._log(f"serial error: {e}")
            finally:
                await self.close()
            if self._stop.is_set():
                break
            self.status.reconnects += 1
            self.status.state = "reconnecting"
            if not await backoff.wait(self._stop):
                break

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
               serial: Any = None, device: str = "") -> None:
        """Mark an open port as the link's: what run() does after each open (a test can
        use it to wire up a stand-in stream pair)."""
        changed = serial is not self.serial
        self.reader, self.writer, self.serial, self.device = reader, writer, serial, device
        self.status.device = device
        self.status.devices = (device,) if device else ()
        self.status.last_error = ""
        self.status.state = "running"
        self.down = False
        self.ready.set()
        self._log(f"serial open: {device} @ {self.settings.compact()}")
        if changed:
            self._changed(serial)

    async def close(self) -> None:
        """Close the port if it is open; the link is down afterwards (also after an open
        that failed: run() closes after every attempt)."""
        self.down = True
        self.ready.clear()
        w, had_serial = self.writer, self.serial is not None
        self.reader = self.writer = self.serial = None
        self.device = ""
        if had_serial:
            self._changed(None)
        if w is not None:
            await close_stream(w, self._log)

    def _changed(self, serial: Any) -> None:
        if self._on_change is not None:
            try:
                self._on_change(serial)
            except Exception as e:  # what follows the port must not break the link
                self._log(f"serial port change not taken up: {type(e).__name__}: {e}")

    async def hold_open(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A session for a runner that reads the port only when it needs to (the Modbus
        gateway reads one reply per request): hold the port until it goes away.

        The stream library reads the port from the moment it opens and closes the
        connection itself on an I/O error (an unplugged USB adapter, a failed write), so a
        loss is noticed here even while no request is in flight, and raised so that run()
        reopens the port. A request that only times out (a silent slave) is not a loss."""
        closed = asyncio.ensure_future(writer.wait_closed())
        # Never cancel that task (stop() cancels this one instead): it would cancel the
        # stream's own close future, which close() waits on as well.
        closed.add_done_callback(_consume_error)
        await asyncio.wait((closed,))
        raise closed.exception() or ConnectionError("serial EOF / device removed")


async def close_stream(writer: asyncio.StreamWriter,
                       log: Optional[Callable[[str], None]] = None) -> None:
    """Close a serial port's stream, in at most about 2 x SERIAL_CLOSE_TIMEOUT_S.

    What the port still has to send (the transport's buffer, the OS's output queue) gets
    SERIAL_CLOSE_TIMEOUT_S to go out. What the device has not taken by then (CTS low, XOFF,
    a PTY or virtual port nobody reads) is dropped, and ``log`` told how much. The port is
    then closed at once: the stream's own graceful close waits for the OS to drain the port
    (tcdrain, in an executor thread, without a time limit), which kept whoever closed it (a
    mapping's stop() on a save, restart or shutdown) waiting while the device took nothing;
    on some USB adapters that wait even outlived a purge of the output, and kept the
    process from exiting."""
    transport: Any = writer.transport
    closed = asyncio.ensure_future(writer.wait_closed())
    closed.add_done_callback(_consume_error)
    if not transport.is_closing():  # (it is when an I/O error closed it already)
        ser: Any = getattr(transport, "serial", None)  # (a test's stand-in stream has none)
        left = await _sent_within(transport, ser, SERIAL_CLOSE_TIMEOUT_S)
        if left and not transport.is_closing():
            with contextlib.suppress(Exception):
                ser.reset_output_buffer()  # what the OS holds: the close won't wait for it
            if log is not None:
                log(f"serial close: dropped {left} byte(s) the device did not take within "
                    f"{SERIAL_CLOSE_TIMEOUT_S:g} s")
        if not transport.is_closing():
            with contextlib.suppress(Exception):
                transport.abort()  # nothing left to send: close without the OS drain
    await _done_within(closed, SERIAL_CLOSE_TIMEOUT_S)


async def _sent_within(transport: Any, ser: Any, seconds: float) -> int:
    """Wait until the port has sent what it holds, at most ``seconds`` (and only while the
    transport is open); the bytes still waiting then, 0 when all is gone."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        left = _unsent(transport, ser)
        if not left or transport.is_closing() or loop.time() >= deadline:
            return left
        await asyncio.sleep(_SENT_POLL_S)


def _unsent(transport: Any, ser: Any) -> int:
    """Bytes the transport holds plus those in the OS's output queue (an unplugged port
    answers neither: 0)."""
    n = 0
    with contextlib.suppress(Exception):
        n += int(transport.get_write_buffer_size())
    with contextlib.suppress(Exception):
        n += int(ser.out_waiting or 0)
    return n


async def _done_within(fut: asyncio.Future, seconds: float) -> bool:
    # asyncio.wait, not wait_for: a timeout must not cancel the stream's own close future
    done, _pending = await asyncio.wait((fut,), timeout=seconds)
    return bool(done)


def _consume_error(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()  # mark it seen: it is raised (or dropped on stop), not lost
