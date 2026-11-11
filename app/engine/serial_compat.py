"""The serial port classes ser2net opens, and the asyncio transport and stream built on them.

ser2net opens every port itself, with ``serial_class()``, and builds pyserial-asyncio-fast's
transport itself (``Transport``), instead of letting serial_for_url pick the class and
open_serial_connection make the transport: the fixes below therefore apply to ser2net's own
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
Windows keeps pyserial's own class (overlapped I/O, polled by the transport).

``Transport`` is pyserial-asyncio-fast's SerialTransport with:
  - _write_ready() that stops watching for writability once its buffer is empty
    (upstream never does: the next writable event hit an assert on the empty buffer,
    every time the loop ran; pyserial-asyncio-fast PR #36, not merged);
  - reads of up to READ_SIZE bytes instead of 1024 (upstream has a TODO about it).

How ser2net carries fixes for upstream code: each override names the upstream issue or
commit it mirrors and is removed once a release has the fix; tests/test_serial_compat.py
pins the upstream code each one replaces (a release that changes it fails there) and fails
once upstream fixes _write_ready itself. At run time an override is used whenever the
upstream members it builds on are there; unverified_note() says so at start when pyserial
/ pyserial-asyncio-fast are not the versions these were written for.

``open_stream`` opens a device with the class for this OS and builds the StreamReader /
StreamWriter pair the way serial_asyncio_fast.open_serial_connection does, without its
URL handling (URL ports are never opened: serial_io.refuse_url).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import os
from typing import Any

import serial
import serial_asyncio_fast as _saf

_PIPES = ("pipe_abort_read_r", "pipe_abort_read_w", "pipe_abort_write_r", "pipe_abort_write_w")
# what pyserial's own read() says when a port reads empty although it was ready
_DISCONNECTED = ("device reports readiness to read but returned no data "
                 "(device disconnected or multiple access on port?)")

# the versions the overrides here were written for and checked against (unverified_note)
VERIFIED = (("pyserial", serial, "3.5"), ("pyserial-asyncio-fast", _saf, "0.16"))

READ_SIZE = 65536  # bytes the transport reads per readiness event (upstream: 1024)


if os.name == "posix":
    import termios

    from serial import serialposix, serialutil

    class PosixSerial(serialposix.Serial):
        """pyserial's POSIX port without select() and without the abort pipes, for a
        non-blocking transport only: a read or write timeout other than 0 is refused
        (nothing could cancel a read or write that waits)."""

        fd: Any  # pyserial's: the port's descriptor while it is open, else None
        is_open: bool
        _baudrate: Any  # pyserial's: the rate asked for

        def open(self) -> None:
            super().open()
            self._close_pipes()

        def close(self) -> None:
            if self.is_open:
                if self.fd is not None:
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
            if self._timeout != 0:
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
            if self._write_timeout != 0:
                raise serial.SerialException("this port writes non-blocking only "
                                             "(write_timeout=0)")
            try:
                return os.write(self.fd, serialutil.to_bytes(data))
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


def serial_class() -> type:
    """The pyserial class a device is opened with on this OS."""
    return PosixSerial if os.name == "posix" else serial.Serial


class Transport(_saf.SerialTransport):
    """pyserial-asyncio-fast's transport with ser2net's fixes (see the module docstring)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol,
                 serial_instance: Any) -> None:
        super().__init__(loop, protocol, serial_instance)
        self._max_read_size = READ_SIZE

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


# what Transport builds on in the upstream class (feature detection, not a version gate:
# stepping aside on a newer release that still has the bugs would bring them back)
_UPSTREAM_MEMBERS = ("_write_data", "_maybe_resume_protocol", "_maybe_pause_protocol",
                     "_remove_writer", "_ensure_writer", "_read_ready", "_fatal_error")


def transport_class() -> type:
    """``Transport``, or upstream's own SerialTransport when it lacks what Transport builds
    on (unverified_note says so)."""
    if all(hasattr(_saf.SerialTransport, m) for m in _UPSTREAM_MEMBERS):
        return Transport
    return _saf.SerialTransport


def unverified_note() -> str:
    """A start-up warning when pyserial / pyserial-asyncio-fast are not the versions the
    overrides here were written for ("" when they are)."""
    other = [f"{name} {getattr(mod, '__version__', '?')} (checked: {want})"
             for name, mod, want in VERIFIED if getattr(mod, "__version__", None) != want]
    if not other:
        return ""
    note = (f"WARNING: serial ports: {', '.join(other)} — ser2net's fixes for these "
            "libraries were not checked against this version")
    if transport_class() is not Transport:
        note += ("; pyserial-asyncio-fast lacks what they build on, so its own transport is "
                 "used (without them)")
    return note


def _open_port(device: str, **kwargs: Any) -> Any:
    """Open ``device`` the way serial.serial_for_url opens a device path (blocking: runs in
    an executor)."""
    port = serial_class()(None, **kwargs)
    port.port = device
    port.open()
    return port


def _close_opened(fut: asyncio.Future) -> None:
    if not fut.cancelled() and fut.exception() is None:
        with contextlib.suppress(Exception):
            fut.result().close()


async def open_stream(device: str, limit: int,
                      **kwargs: Any) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open ``device`` with ``kwargs`` (pyserial settings) and return its (reader, writer);
    ``writer.transport`` is a ``Transport`` and ``writer.transport.serial`` the port.
    Raises serial.SerialException / OSError when it can't be opened."""
    loop = asyncio.get_running_loop()
    opening = loop.run_in_executor(None, functools.partial(_open_port, device, **kwargs))
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
