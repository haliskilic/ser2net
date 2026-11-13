"""The serial port classes ser2netweb opens, and the asyncio transport and stream built on them.

ser2netweb opens every port itself, with ``serial_class()``, and builds pyserial-asyncio-fast's
transport itself (``Transport``), instead of letting serial_for_url pick the class and
open_serial_connection make the transport: the fixes below therefore apply to ser2netweb's own
ports only, not to pyserial process-wide.

pyserial-asyncio-fast's SerialTransport watches a port with the event loop's own file
descriptor watching (POSIX: add_reader, i.e. epoll / kqueue) and drives it non-blocking
(timeout=0, write_timeout=0, set when the transport is made). pyserial's POSIX Serial
does things such a transport never needs, or gets wrong:
  - read() asks select() first. select() takes no descriptor at or above FD_SETSIZE
    (1024) and raises ValueError, which neither pyserial nor the transport catches: a
    port opened while the process had ~1000 files open read nothing, never noticed an
    unplug, and its reader fired again on every pass of the loop;
  - open() makes two pipes (four descriptors per port) that only cancel_read() /
    cancel_write() use, to wake a blocking read or write;
  - write() tries again while the OS refuses a write for now (EAGAIN), and with
    write_timeout 0 never stops trying: while a device took no data (CTS low, XOFF, a
    PTY nobody reads), the event loop spun in it and nothing else ran (pyserial #280,
    fix PR #883 not merged);
  - for a baud rate with no termios constant (250000, 74880, 31250, ...) it hands
    termios.tcsetattr() the BOTHER placeholder, which glibc 2.42 and later refuse
    (EINVAL) through the cfsetispeed() / cfsetospeed() versions a program built for an
    older glibc uses, as the standalone Linux executable is: every port at such a rate
    failed to open on Ubuntu 26.04, Fedora 43 and later.

``PosixSerial`` closes those pipes once the port is open, writes with one os.write (an
EAGAIN is raised as BlockingIOError: the transport keeps the data and waits for the port
to be writable), reads with os.read alone (the transport only reads after the loop has
seen the port readable; an empty read then means the device went away, as a hung-up tty
reads empty and also refuses the input-queue ioctl, unless the bytes were taken in the
meantime by a purge or another reader: the read is then just empty, as pyserial's would
be), and sets a rate without a termios constant the way pyserial's master branch does
(70d18864): 38400 through tcsetattr, then the real rate with the TCSETS2 ioctl.

Windows: pyserial issues every non-blocking write on the port's one OVERLAPPED structure
while earlier ones are still pending, reports it written at once and keeps nothing alive
for the driver (pyserial #671). Nothing on Windows then waited for the device: a fast
sender filled the driver's queue without limit, drain() never waited, and a stop waited
for all of it. ``WindowsSerial`` gives each write its own OVERLAPPED and event and keeps
its data until it has completed, with at most WRITE_WINDOW bytes in flight per port: past
that a write raises BlockingIOError, the transport keeps the data, and drain() waits for
the device as on POSIX. SER2NETWEB_WINDOWS_SERIAL=pyserial keeps pyserial's own class.

``Transport`` is pyserial-asyncio-fast's SerialTransport with:
  - _write_ready() that stops watching for writability once its buffer is empty
    (upstream never does: the next writable event hit an assert on the empty buffer,
    every time the loop ran; pyserial-asyncio-fast PR #36, not merged);
  - reads of up to READ_SIZE bytes instead of 1024 (upstream has a TODO about it);
  - on Windows, where the transport polls the port on a timer (pyserial-asyncio PR #8):
    a write poll that keeps writing what the transport holds while it closes (upstream
    stopped, so a graceful close with data left never completed), and a read poll every
    POLL_IDLE_WAIT_S instead of every 0.5 ms once the port has been quiet for
    POLL_IDLE_AFTER_S, back to 0.5 ms as soon as data arrives or is written (a reply
    usually follows a write).

How ser2netweb carries fixes for upstream code (CONTRIBUTING.md, "Third-party code"): each
override names the upstream issue or commit it mirrors and is removed once a release has
the fix; tests/test_serial_compat.py pins the upstream code each one replaces (a release
that changes it fails there) and fails once upstream fixes _write_ready itself. At run
time an override is used whenever the upstream members it builds on are there;
unverified_note() says so at start when pyserial / pyserial-asyncio-fast are not the
versions these were written for. Where pyserial-asyncio-fast isn't installed (a Linux
distribution that doesn't package it, docs/PACKAGING.md), Transport builds on
serial_transport.py instead: its 0.16 SerialTransport, copied (``BUILT_IN``).

``open_stream`` opens a device with the class for this OS and builds the StreamReader /
StreamWriter pair the way serial_asyncio_fast.open_serial_connection does, without its
URL handling (URL ports are never opened: serial_io.refuse_url).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import ctypes
import errno
import functools
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import serial

from .. import envvars, optional

try:
    import serial_asyncio_fast as _saf
except ImportError as _e:
    _problem = optional.problem(_e, "pyserial-asyncio-fast", "serial_asyncio_fast")
    if not _problem.missing:
        # installed, but broken: said as it is (as --about does), not passed over with the
        # copy as if it weren't installed
        raise ImportError(str(_problem)) from _e
    # not packaged by every distribution: its transport, copied (0.16)
    from . import serial_transport as _saf  # type: ignore[no-redef]

# pyserial-asyncio-fast isn't installed: Transport builds on ser2netweb's copy of its
# SerialTransport (serial_transport.py), the same code as the pinned release's
BUILT_IN = _saf.__name__ != "serial_asyncio_fast"

_PIPES = ("pipe_abort_read_r", "pipe_abort_read_w", "pipe_abort_write_r", "pipe_abort_write_w")
# what pyserial's own read() says when a port reads empty although it was ready
_DISCONNECTED = ("device reports readiness to read but returned no data "
                 "(device disconnected or multiple access on port?)")

# the versions the overrides here were written for and checked against (unverified_note)
VERIFIED = (("pyserial", serial, "3.5"), ("pyserial-asyncio-fast", _saf, "0.16"))

READ_SIZE = 65536        # bytes the transport reads per readiness event (upstream: 1024)
WRITE_WINDOW = 8192      # Windows: bytes of a port's writes in flight at most
CANCEL_WAIT_S = 1.0      # Windows: how long a close waits for cancelled writes to end
POLL_IDLE_AFTER_S = 1.0  # Windows: a port quiet this long is polled every ...
POLL_IDLE_WAIT_S = 0.01  # ... this instead of every 0.5 ms
WINDOWS_SERIAL_ENV = "SER2NETWEB_WINDOWS_SERIAL"  # (or SER2NET_WINDOWS_SERIAL, its 2.9.x name)


# (sys.platform too, which mypy --platform reads; os.name alone decides at run time, as
# tests/_windows_transport.py relies on: it imports this module with sys.platform "win32")
if os.name == "posix" and sys.platform != "win32":
    import fcntl
    import termios

    from serial import serialposix

    if TYPE_CHECKING:
        class _PosixBase(serialposix.Serial):
            # pyserial's private parts PosixSerial uses; its type stubs leave them out
            _bytesize: Any
            _parity: Any
            _stopbits: Any

            def _reconfigure_port(self, force_update: bool = False) -> None: ...
            def _set_special_baudrate(self, baudrate: int) -> None: ...
    else:
        _PosixBase = serialposix.Serial

    class PosixSerial(_PosixBase):
        """pyserial's POSIX port without select() and without the abort pipes, for a
        non-blocking transport only: a read or write timeout other than 0 is refused
        (nothing could cancel a read or write that waits).

        Keeping other programs off the port, beyond pyserial's flock (``exclusive``, which
        only programs that flock the port too respect), as classic ser2net does:
          - ``tiocexcl``: once open, the port is marked exclusive (TIOCEXCL): opens by other
            programs fail with EBUSY (not root's), until it is closed (TIOCNXCL);
          - ``respect_uucp_locks``: a port another program holds a UUCP lock file on
            (minicom, cu) is not opened (portdiag.uucp_lock: read only, none is made)."""

        fd: Any  # pyserial's: the port's descriptor while it is open, else None
        is_open: bool
        _baudrate: Any  # pyserial's: the rate asked for
        tiocexcl = False
        respect_uucp_locks = False
        _excl = False  # TIOCEXCL is set on the open port

        def open(self) -> None:
            if self.respect_uucp_locks and self.portstr:
                from . import portdiag
                held = portdiag.uucp_lock(self.portstr)
                if held is not None:
                    raise serial.SerialException(
                        errno.EBUSY, f"could not open port {self.portstr}: {held.describe()}")
            super().open()
            if self.tiocexcl:
                try:
                    fcntl.ioctl(self.fd, termios.TIOCEXCL)
                except (OSError, AttributeError) as e:  # (AttributeError: no TIOCEXCL here)
                    self.close()
                    raise serial.SerialException(
                        getattr(e, "errno", None),
                        f"could not keep other programs out of {self.portstr} (TIOCEXCL): "
                        f"{e}") from e
                self._excl = True
            self._close_pipes()

        def close(self) -> None:
            if self.is_open:
                if self.fd is not None:
                    if self._excl:
                        with contextlib.suppress(OSError):
                            fcntl.ioctl(self.fd, termios.TIOCNXCL)
                        self._excl = False
                    os.close(self.fd)
                    self.fd = None
                self._close_pipes()
                self.is_open = False

        def _close_pipes(self) -> None:
            for name in _PIPES:
                fd = getattr(self, name, None)
                setattr(self, name, None)
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)

        def _reconfigure_port(self, force_update: bool = False) -> None:
            """pyserial's, with a baud rate that has no termios constant set as pyserial's
            master branch does (70d18864): 3.5 hands tcsetattr() the BOTHER placeholder,
            which glibc 2.42 and later refuse through the cfsetispeed() / cfsetospeed() of
            an executable built for an older glibc (the standalone Linux binary). The port
            is set to 38400 first, then to the real rate with the TCSETS2 ioctl, which
            glibc has no part in. A termios error (the driver refused a setting) is raised
            as SerialException, with what was asked for."""
            rate = self._baudrate
            try:
                if _needs_placeholder(self, rate):
                    self._baudrate = 38400
                    try:
                        super()._reconfigure_port(force_update)
                    finally:
                        self._baudrate = rate
                    self._set_special_baudrate(rate)
                else:
                    super()._reconfigure_port(force_update)
            except termios.error as e:
                reason = e.args[1] if len(e.args) > 1 else e
                raise serial.SerialException(
                    f"could not configure {self.portstr} for {self._baudrate} baud, "
                    f"{self._bytesize}{self._parity}{self._stopbits:g}: {reason}") from e

        def read(self, size: int = 1) -> bytes:
            if not self.is_open:
                raise serial.PortNotOpenError()
            if self.timeout != 0:
                raise serial.SerialException("this port reads non-blocking only (timeout=0)")
            try:
                data = os.read(self.fd, size)
            except BlockingIOError:
                return b""  # nothing to read now
            except OSError as e:  # e.g. EIO: the other side of a PTY closed
                raise serial.SerialException(f"read failed: {e}") from e
            if data:
                return data
            try:
                self.in_waiting  # noqa: B018 - the ioctl a hung-up tty refuses
            except OSError:
                raise serial.SerialException(_DISCONNECTED) from None
            return b""  # taken in the meantime: the port is still there

        def write(self, data: Any) -> int:
            """One non-blocking write: how much of ``data`` the OS took. pyserial's own
            write() tries again while the OS refuses a write for now (EAGAIN), and with
            write_timeout 0 it never stops trying: a device that took no data (CTS low,
            XOFF, a PTY nobody reads) kept the event loop spinning in it (pyserial #280).
            That raises BlockingIOError here instead, and the transport keeps the data
            until the port is writable again."""
            if not self.is_open:
                raise serial.PortNotOpenError()
            if self.write_timeout != 0:
                raise serial.SerialException("this port writes non-blocking only "
                                             "(write_timeout=0)")
            try:
                return os.write(self.fd, bytes(data))
            except BlockingIOError:
                raise
            except OSError as e:
                raise serial.SerialException(f"write failed: {e}") from e

        def cancel_read(self) -> None:
            """Nothing to cancel: a read never waits."""

        def cancel_write(self) -> None:
            """Nothing to cancel: a write never waits."""

    def _needs_placeholder(port: Any, rate: Any) -> bool:
        """Whether pyserial would hand tcsetattr() the BOTHER placeholder for ``rate``: a
        rate with neither a termios constant nor an entry in the platform's table, on a
        platform that has BOTHER (Linux; elsewhere pyserial uses 38400 itself)."""
        if not hasattr(serialposix, "BOTHER"):
            return False
        try:
            rate = int(rate)
        except (TypeError, ValueError):
            return False  # pyserial refuses it with its own message
        return (not hasattr(termios, f"B{rate}")
                and rate not in getattr(port, "BAUDRATE_CONSTANTS", {}))


def _win_error(code: int) -> OSError:
    make = getattr(ctypes, "WinError", None)  # Windows only
    return make(code) if make is not None else OSError(code, f"Windows error {code}")


# Windows writes that a close could not see end (a driver that ignored the cancel): the
# driver may still use their OVERLAPPED and data, so they are never freed
_ABANDONED: list = []


class OverlappedWrites:
    """A Windows port's writes in flight, each with its own OVERLAPPED structure and event,
    and its data kept until the write has completed; at most ``window`` bytes at a time.
    ``win32`` is pyserial's serial.win32 (a test passes a stand-in), ``handle`` the port's,
    opened for overlapped I/O. The writes complete in the order they were issued; one the
    port's purge aborted (an RFC 2217 purge, a reset_output_buffer()) counts as done. A
    write that failed after it was started is raised as SerialException by the next one."""

    def __init__(self, win32: Any, handle: Any, window: int = WRITE_WINDOW) -> None:
        self._w32 = win32
        self._handle = handle
        self.window = window
        self._pending: collections.deque = collections.deque()  # (OVERLAPPED, data)
        self._bytes = 0
        self._error: Exception | None = None
        self._lock = threading.Lock()  # the close and a flush run in an executor thread

    def pending(self) -> int:
        """Bytes written to the port whose write has not completed yet."""
        with self._lock:
            self._reap()
            return self._bytes

    def write(self, data: bytes) -> int:
        """Start writing as much of ``data`` as the window has room for; how much that
        was. BlockingIOError: no room (or the driver can't take a write right now)."""
        w = self._w32
        with self._lock:
            self._reap()
            if self._error is not None:
                e, self._error = self._error, None
                raise e
            room = self.window - self._bytes
            if room <= 0:
                raise BlockingIOError(errno.EAGAIN, "the port's writes in flight are at "
                                                    "their limit")
            chunk = data if len(data) <= room else data[:room]
            ov = w.OVERLAPPED()
            ov.hEvent = w.CreateEvent(None, 1, 0, None)  # manual reset: stays set when done
            if not ov.hEvent:
                error = _win_error(w.GetLastError())
                raise serial.SerialException(f"CreateEvent failed ({error!r})")
            done = w.DWORD()
            ok = w.WriteFile(self._handle, chunk, len(chunk), ctypes.byref(done),
                             ctypes.byref(ov))
            code = w.ERROR_SUCCESS if ok else w.GetLastError()
            if code in (w.ERROR_SUCCESS, w.ERROR_IO_PENDING):
                self._pending.append((ov, chunk))  # both alive until it has completed
                self._bytes += len(chunk)
                return len(chunk)
            w.CloseHandle(ov.hEvent)
            if code in (w.ERROR_INVALID_USER_BUFFER, w.ERROR_NOT_ENOUGH_MEMORY,
                        w.ERROR_OPERATION_ABORTED):
                # what pyserial reads as "nothing written now" (no room in the driver, a
                # purge under way): the transport tries again once the port has room
                raise BlockingIOError(errno.EAGAIN, f"the port took no data now ({code})")
            raise serial.SerialException(f"WriteFile failed ({_win_error(code)!r})")

    def _reap(self) -> None:
        """Forget the writes at the front that have completed (lock held)."""
        w = self._w32
        while self._pending:
            ov, chunk = self._pending[0]
            done = w.DWORD()
            if not w.GetOverlappedResult(self._handle, ctypes.byref(ov), ctypes.byref(done),
                                         False):
                code = w.GetLastError()
                if code in (w.ERROR_IO_INCOMPLETE, w.ERROR_IO_PENDING):
                    return
                if code != w.ERROR_OPERATION_ABORTED and self._error is None:
                    self._error = serial.SerialException(f"write failed ({_win_error(code)!r})")
            self._pending.popleft()
            self._bytes -= len(chunk)
            w.CloseHandle(ov.hEvent)

    def cancel(self, wait_s: float = CANCEL_WAIT_S) -> None:
        """Cancel every write in flight and wait (up to ``wait_s``) until the driver is done
        with them before they are freed: what a close does before the port's handle goes."""
        w = self._w32
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
            self._bytes = 0
        for ov, _chunk in pending:
            w.CancelIoEx(self._handle, ctypes.byref(ov))
        deadline = time.monotonic() + wait_s
        for ov, chunk in pending:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            if w.WaitForSingleObject(ov.hEvent, left) == 0:  # WAIT_OBJECT_0: it has ended
                w.CloseHandle(ov.hEvent)
            else:
                _ABANDONED.append((ov, chunk))


if os.name == "nt" and sys.platform == "win32":
    from serial import serialwin32
    from serial import win32 as _win32

    if TYPE_CHECKING:
        class _WindowsBase(serialwin32.Serial):
            # pyserial's private parts WindowsSerial uses; its type stubs leave them out
            _port_handle: Any

            def _close(self) -> None: ...
    else:
        _WindowsBase = serialwin32.Serial

    class WindowsSerial(_WindowsBase):
        """pyserial's Windows port, whose non-blocking writes are tracked (OverlappedWrites)
        instead of reported done at once on one shared OVERLAPPED; for a non-blocking
        transport only (a write timeout other than 0 is refused)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._writes: OverlappedWrites | None = None
            super().__init__(*args, **kwargs)

        def open(self) -> None:
            super().open()
            self._writes = OverlappedWrites(_win32, self._port_handle)

        def write(self, data: Any) -> int:
            if not self.is_open or self._writes is None:
                raise serial.PortNotOpenError()
            if self.write_timeout != 0:
                raise serial.SerialException("this port writes non-blocking only "
                                             "(write_timeout=0)")
            data = bytes(data)
            return self._writes.write(data) if data else 0

        @property
        def out_waiting(self) -> int:
            """What is still to go out: the driver's queue, or the writes in flight (some
            drivers report no queue)."""
            queued = super().out_waiting
            writes = self._writes
            return max(queued, writes.pending()) if writes is not None else queued

        def flush(self) -> None:
            """Wait until everything written has gone out (pyserial waits for the driver's
            queue only)."""
            while self.is_open and self.out_waiting:
                time.sleep(0.05)

        def cancel_write(self) -> None:
            if self._writes is not None:
                self._writes.cancel()
            super().cancel_write()

        def _close(self) -> None:
            writes, self._writes = self._writes, None
            if writes is not None and self._port_handle is not None:
                writes.cancel()  # before the handle (and with it the driver's I/O) goes
            super()._close()


def serial_class() -> type:
    """The pyserial class a device is opened with on this OS."""
    if os.name == "posix" and sys.platform != "win32":
        return PosixSerial
    if os.name == "nt" and sys.platform == "win32":
        if envvars.text(WINDOWS_SERIAL_ENV).lower() != "pyserial":
            return WindowsSerial
    return serial.Serial


class Transport(_saf.SerialTransport):
    """pyserial-asyncio-fast's transport with ser2netweb's fixes (see the module docstring)."""

    # upstream's: False, or (Windows) the TimerHandle of the next poll; typed bool there
    _has_reader: Any
    _has_writer: Any

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol,
                 serial_instance: Any) -> None:
        super().__init__(loop, protocol, serial_instance)
        self._max_read_size = READ_SIZE
        self._last_io = loop.time()  # Windows: when data last came or went (read polling)
        self._idle_poll = False      # Windows: the read poll is at POLL_IDLE_WAIT_S

    def _write_ready(self) -> None:
        """Upstream's, but it stops watching for writability once its buffer is empty
        (pyserial-asyncio-fast PR #36): upstream never did, and the next writable event
        hit `assert data` on the empty buffer, every time the loop ran."""
        if not self._write_buffer:
            self._remove_writer()
            return
        if len(self._write_buffer) == 1:
            data = self._write_buffer.pop()
        else:
            data = b"".join(self._write_buffer)
            self._write_buffer.clear()
        self._write_data(data)
        self._maybe_resume_protocol()  # may append to the buffer
        if not self._write_buffer and not self._closing:
            self._remove_writer()

    if sys.platform == "win32":
        def write(self, data: Any) -> None:
            self._last_io = self._loop.time()
            if self._idle_poll and self._has_reader and not self._closing:
                # a reply usually follows: poll at the fast rate again from now
                self._has_reader.cancel()
                self._has_reader = self._loop.call_later(self._poll_wait_time, self._poll_read)
                self._idle_poll = False
            super().write(data)

        def _poll_read(self) -> None:
            """Upstream's every 0.5 ms; every POLL_IDLE_WAIT_S once the port has been quiet
            for POLL_IDLE_AFTER_S (back to 0.5 ms when data arrives, or is written). A
            port that fails (unplugged) is closed instead of raising on every poll."""
            if not self._has_reader or self._closing:
                return
            self._idle_poll = self._loop.time() - self._last_io >= POLL_IDLE_AFTER_S
            wait = POLL_IDLE_WAIT_S if self._idle_poll else self._poll_wait_time
            self._has_reader = self._loop.call_later(wait, self._poll_read)
            try:
                waiting = self._serial.in_waiting
            except serial.SerialException as exc:
                self._fatal_error(exc, "Fatal read error on serial transport")
                return
            if waiting:
                self._last_io = self._loop.time()
                self._read_ready()

        def _poll_write(self) -> None:
            """Upstream's, but it keeps writing what the transport holds while it closes:
            upstream stopped once closing, so a graceful close with data left never
            completed and the port stayed open. A port that fails is closed instead of
            raising on every poll."""
            if not self._has_writer:
                return
            self._has_writer = self._loop.call_later(self._poll_wait_time, self._poll_write)
            try:
                room = self._serial.out_waiting < self._max_out_waiting
            except serial.SerialException as exc:
                self._fatal_error(exc, "Fatal write error on serial transport")
                return
            if room:
                self._last_io = self._loop.time()
                self._write_ready()


# what Transport builds on in the upstream class (feature detection, not a version gate:
# stepping aside on a newer release that still has the bugs would bring them back)
_UPSTREAM_MEMBERS = ("_write_data", "_maybe_resume_protocol", "_maybe_pause_protocol",
                     "_remove_writer", "_ensure_writer", "_read_ready", "_fatal_error") + (
    ("_poll_read", "_poll_write") if sys.platform == "win32" else ())


def transport_class() -> type:
    """``Transport``, or upstream's own SerialTransport when it lacks what Transport builds
    on (unverified_note says so)."""
    if all(hasattr(_saf.SerialTransport, m) for m in _UPSTREAM_MEMBERS):
        return Transport
    return _saf.SerialTransport


def unverified_note() -> str:
    """A start-up warning when pyserial / pyserial-asyncio-fast are not the versions the
    overrides here were written for ("" when they are), or a note that the built-in copy
    of pyserial-asyncio-fast's transport is used."""
    other = [f"{name} {getattr(mod, '__version__', '?')} (checked: {want})"
             for name, mod, want in VERIFIED if getattr(mod, "__version__", None) != want]
    built_in = (f"pyserial-asyncio-fast is not installed — its transport "
                f"({_saf.__version__}), built into ser2netweb, is used" if BUILT_IN else "")
    if not other:
        return f"serial ports: {built_in}" if built_in else ""
    note = (f"WARNING: serial ports: {', '.join(other)} — ser2netweb's fixes for these "
            "libraries were not checked against this version")
    if transport_class() is not Transport:
        note += ("; pyserial-asyncio-fast lacks what they build on, so its own transport is "
                 "used (without them)")
    if built_in:
        note += f"; {built_in}"
    return note


def _open_port(device: str, rts: bool | None = None, dtr: bool | None = None,
               keep_out: dict[str, bool] | None = None, **kwargs: Any) -> Any:
    """Open ``device`` the way serial.serial_for_url opens a device path (blocking: runs in
    an executor). ``rts`` / ``dtr``: the line's state from the moment the port opens
    (None: pyserial's default, on). pyserial switches both on inside open() unless told
    otherwise before it: on Windows a line set to off then never pulses; on Linux the tty
    layer itself raises both at open(2), so the pulse is only shorter there. ``keep_out``:
    PosixSerial's tiocexcl / respect_uucp_locks (POSIX only)."""
    port = serial_class()(None, **kwargs)
    port.port = device
    if rts is not None:
        port.rts = rts
    if dtr is not None:
        port.dtr = dtr
    for name, value in (keep_out or {}).items():
        if value and os.name == "posix":
            setattr(port, name, True)
    port.open()
    return port


def _close_opened(fut: asyncio.Future) -> None:
    if not fut.cancelled() and fut.exception() is None:
        with contextlib.suppress(Exception):
            fut.result().close()


async def open_stream(device: str, limit: int, *, rts: bool | None = None,
                      dtr: bool | None = None, keep_out: dict[str, bool] | None = None,
                      **kwargs: Any) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open ``device`` with ``kwargs`` (pyserial settings) and return its (reader, writer);
    ``writer.transport`` is a ``Transport`` and ``writer.transport.serial`` the port.
    ``rts`` / ``dtr`` / ``keep_out``: see _open_port. Raises serial.SerialException /
    OSError when it can't be opened."""
    loop = asyncio.get_running_loop()
    opening = loop.run_in_executor(None, functools.partial(_open_port, device, rts, dtr,
                                                           keep_out, **kwargs))
    try:
        ser = await asyncio.shield(opening)
    except asyncio.CancelledError:
        opening.add_done_callback(_close_opened)  # cancelled mid-open: close it once open
        raise
    try:
        reader = asyncio.StreamReader(limit=limit, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        transport = transport_class()(loop, protocol, ser)
    except BaseException:
        ser.close()
        raise
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
