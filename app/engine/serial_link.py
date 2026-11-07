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


class Backoff:
    """The pause between reconnect attempts: 0.5 s doubling up to 10 s, plus up to 0.4 s
    of jitter derived from the attempt number (not an RNG). ``attempt`` counts the attempts
    since the last success; reset() after one."""

    def __init__(self) -> None:
        self.attempt = 0
        self._delay = 0.5

    def reset(self) -> None:
        self.attempt, self._delay = 0, 0.5

    async def wait(self, stop: asyncio.Event) -> bool:
        """Sleep before the next attempt; False when ``stop`` was set meanwhile."""
        self.attempt += 1
        try:
            await asyncio.wait_for(stop.wait(),
                                   timeout=min(self._delay, 10.0) + (self.attempt % 5) * 0.1)
            return False
        except asyncio.TimeoutError:
            pass
        self._delay = min(self._delay * 2, 10.0)
        return True


class SerialLink:
    def __init__(self, settings: SerialSettings, status: RunnerStatus, stop: asyncio.Event,
                 log: Callable[[str], None]) -> None:
        self.settings = settings
        self.status = status
        self._stop = stop
        self._log = log
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
        self.reader, self.writer, self.serial, self.device = reader, writer, serial, device
        self.status.device = device
        self.status.devices = (device,) if device else ()
        self.status.last_error = ""
        self.status.state = "running"
        self.down = False
        self.ready.set()
        self._log(f"serial open: {device} @ {self.settings.compact()}")

    async def close(self) -> None:
        """Close the port if it is open; the link is down afterwards (also after an open
        that failed: run() closes after every attempt)."""
        self.down = True
        self.ready.clear()
        w = self.writer
        self.reader = self.writer = self.serial = None
        self.device = ""
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()
                await w.wait_closed()

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


def _consume_error(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()  # mark it seen: it is raised (or dropped on stop), not lost
