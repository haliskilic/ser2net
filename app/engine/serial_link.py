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
    while it is not (what the dashboard, /metrics and the notifications report):
    ``device-missing`` when the last open found no such device (portdiag.missing),
    ``reconnecting`` otherwise (the port went away, or can't be opened for another reason);
    ``status.open_failures`` / ``status.device_lost`` count failed opens and losses of the
    open port (``reconnects`` both, as before); ``status.hangups`` the losses after which
    the first try opened the same device again (POSIX: a hang-up, by a login prompt on
    the port for one, not an unplug);
    ``status.hint``: what the error of a failed open means (portdiag.explain);
    ``status.warnings``: a setting the open port did not take (serial_io.open_notes),
    logged when it differs from the last open's.
While the port can't be opened, the log says so once, again only when the error changes or
every FAILING_SUMMARY_S (FailureLog), and how long it was down once it opens again.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Optional

from . import portdiag, portlist, serial_io

if TYPE_CHECKING:
    from ..config import SerialSettings
    from .bridge import RunnerStatus

# a session runs while the port is open and returns or raises once it is gone
Session = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

# closing a port: what is still waiting to be written to the device gets this long to go
# out (then it is dropped), and so does the close itself after that
SERIAL_CLOSE_TIMEOUT_S = 2.0
_SENT_POLL_S = 0.05  # how often a close looks whether the port has sent everything
# while something keeps failing in the same way (a port that can't be opened, a connect-out)
# the log says so again this often, instead of at every attempt (FailureLog)
FAILING_SUMMARY_S = 900.0


def fmt_duration(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class FailureLog:
    """The log lines of something tried again and again until it works (a serial port that
    can't be opened, a connect-out): the first failure and each change of the error, then
    the same error only every ``every`` seconds, with how many attempts failed since when;
    recovered() ends it, with how long it lasted for the line that says it works again.
    (A missing USB adapter used to write the same line at every retry, about 8,500 a day,
    which pushed what happened first out of the log viewer within hours.)"""

    def __init__(self, log: Callable[[str], None], prefix: str,
                 every: float = FAILING_SUMMARY_S) -> None:
        self._log = log
        self._prefix = prefix
        self._every = every
        self.error = ""        # the last failure's error ("" while it works)
        self.failures = 0      # failures since it last worked
        self.since = 0.0       # time.time() of the first of them
        self._next = 0.0       # time.monotonic() of the next summary

    def failed(self, error: str) -> bool:
        """Count a failure and log it as said above; True when it is the first one, or
        the error changed."""
        now = time.monotonic()
        if not self.failures:
            self.since = time.time()
        self.failures += 1
        new = error != self.error
        self.error = error
        if new:
            self._log(f"{self._prefix}: {error}")
        elif now >= self._next:
            since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.since))
            self._log(f"{self._prefix}: {error} (still failing: {self.failures} attempts "
                      f"since {since})")
        else:
            return False
        self._next = now + self._every
        return new

    def recovered(self) -> str:
        """It works again: what to add to the line that says so (" (back after 5m 12s)"),
        "" when nothing had failed."""
        if not self.failures:
            return ""
        took = fmt_duration(time.time() - self.since)
        self.error, self.failures, self.since = "", 0, 0.0
        return f" (back after {took})"


class Backoff:
    """The pause between reconnect attempts: 0.5 s doubling up to ``cap`` (10 s), plus up
    to 0.4 s of jitter derived from the attempt number (not an RNG). ``attempt`` counts the
    attempts since the last success; reset() after one. wake() ends a pause under way (a
    serial device just appeared: no need to wait up to 10 s for it)."""

    def __init__(self, cap: float = 10.0) -> None:
        self.cap = cap
        self.attempt = 0
        self._delay = 0.5
        self._woken: Optional[asyncio.Event] = None  # set by wake() while wait() sleeps

    def reset(self) -> None:
        self.attempt, self._delay = 0, 0.5

    def pause(self) -> float:
        """The pause before the next attempt (what wait() sleeps), counting that attempt."""
        self.attempt += 1
        pause = min(self._delay, self.cap) + (self.attempt % 5) * 0.1
        self._delay = min(self._delay * 2, self.cap)
        return pause

    async def wait(self, stop: asyncio.Event) -> bool:
        """Sleep before the next attempt (or until wake()); False when ``stop`` was set
        meanwhile."""
        pause = self.pause()
        woken = self._woken = asyncio.Event()
        waits = (asyncio.ensure_future(stop.wait()), asyncio.ensure_future(woken.wait()))
        try:
            await asyncio.wait(waits, timeout=pause, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
            self._woken = None
        return not stop.is_set()

    def wake(self) -> None:
        """End the pause wait() is in, if any: try again now."""
        if self._woken is not None:
            self._woken.set()


class SerialLink:
    def __init__(self, settings: SerialSettings, status: RunnerStatus, stop: asyncio.Event,
                 log: Callable[[str], None],
                 on_change: Optional[Callable[[Any], None]] = None, owner: str = "") -> None:
        self.settings = settings
        self.owner = owner  # the mapping's name ("mapping X has this port open")
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
        self._notes = NoteLog(log)
        self._failures = FailureLog(log, "serial error")
        self._backoff: Optional[Backoff] = None  # run()'s, while it runs
        # POSIX: the open port's device number, and that of a port that went away while
        # no open has failed since (a hang-up, if the first try opens the same device)
        self._rdev: Optional[int] = None
        self._lost_rdev: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.ready.is_set()

    async def run(self, session: Session) -> None:
        """Keep the port open until ``stop`` is set: open it, run ``session`` while it is
        open, close it once the session ends, and try again after a backoff."""
        backoff = self._backoff = Backoff()
        self.down = False  # the first attempt is under way
        while not self._stop.is_set():
            was_open = False
            try:
                reader, writer, ser, device = await serial_io.open_serial(self.settings)
                was_open = True
                self.attach(reader, writer, ser, device)
                backoff.reset()
                await session(reader, writer)
                if not self._stop.is_set():  # (a session ends by raising why)
                    raise ConnectionError("serial EOF / device removed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # SerialException, OSError, the port went away
                if not self._stop.is_set():
                    await self._failed(e, was_open)
            finally:
                await self.close()
            if self._stop.is_set():
                break
            self.status.reconnects += 1
            if not await backoff.wait(self._stop):
                break

    def wake(self) -> None:
        """Try to open the port now if run() is waiting to try again (a serial device was
        plugged in, PortWatcher saw it)."""
        if self._backoff is not None and not self.is_open:
            self._backoff.wake()

    async def _failed(self, e: Exception, was_open: bool) -> None:
        """The port could not be opened, or went away (``was_open``): say why in the status
        and the log (FailureLog), and what that means."""
        st = self.status
        st.last_error = str(e)
        if was_open:
            st.device_lost += 1
            st.state = "reconnecting"
            self._lost_rdev = self._rdev  # (no stat now: an unplug may still be under way)
        else:
            st.open_failures += 1
            st.state = "device-missing" if portdiag.missing(e) else "reconnecting"
            self._lost_rdev = None
        self._failures.failed(str(e))
        # (worked out at every failed open: the same error can come to mean something else,
        # a user just added to the device's group needs a restart)
        st.hint = None if was_open else await asyncio.to_thread(explain, e, self.settings)

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
               serial: Any = None, device: str = "") -> None:
        """Mark an open port as the link's: what run() does after each open (a test can
        use it to wire up a stand-in stream pair)."""
        changed = serial is not self.serial
        self.reader, self.writer, self.serial, self.device = reader, writer, serial, device
        self._rdev = _rdev(serial)
        if self._lost_rdev is not None and self._lost_rdev == self._rdev:
            # the port went away and the very next try opened the same device: hung up
            # (a login prompt's vhangup, another program), not unplugged
            self.status.hangups += 1
            self._log("serial port was hung up while open and could be reopened at once: "
                      "another program may be using it (a login prompt on it?)")
        self._lost_rdev = None
        notes = serial_io.open_notes(serial)
        hangups = self.status.hangups
        hung_up = (portdiag.Note("hangups", (hangups,)),) if hangups else ()
        self.status.device = device
        self.status.devices = (device,) if device else ()
        self.status.last_error = ""
        self.status.hint = None
        self.status.warnings = tuple(notes) + hung_up
        self.status.state = "running"
        self.down = False
        self.ready.set()
        portdiag.opened(serial, self.owner, device)
        self._log(f"serial open: {device} @ {self.settings.compact()}"
                  f"{self._failures.recovered()}")
        self._notes(notes)
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
        self.status.warnings = ()
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


def _rdev(ser: Any) -> Optional[int]:
    """The device number of an open POSIX port (None elsewhere, or for a stand-in)."""
    try:
        return os.fstat(ser.fileno()).st_rdev or None
    except (AttributeError, OSError, ValueError, TypeError):
        return None


def explain(e: BaseException, settings: SerialSettings) -> Optional[portdiag.Note]:
    """portdiag.explain for the device ``settings`` opens (blocking: run it in a thread)."""
    return portdiag.explain(e, serial_io.resolve_device(settings, portlist.recent()))


class NoteLog:
    """Logs what to know about a port just opened (portdiag.Note) when it differs from what
    was logged for the last open: a port that keeps refusing RS-485 says so once, not at
    every reopen."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._last: tuple = ()

    def __call__(self, notes: Iterable[Any]) -> None:
        notes = tuple(notes)
        for note in notes:
            if note not in self._last:
                self._log(f"serial warning: {note.text()}")
        self._last = notes


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
