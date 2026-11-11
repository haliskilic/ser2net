"""The serial port class ser2net opens, and the asyncio stream built on it.

pyserial-asyncio-fast's SerialTransport watches a port with the event loop's own file
descriptor watching (POSIX: add_reader, i.e. epoll / kqueue) and drives it non-blocking
(timeout=0, write_timeout=0, set when the transport is made). pyserial's POSIX Serial
does two things such a transport never needs:
  - read() asks select() first. select() takes no descriptor at or above FD_SETSIZE
    (1024) and raises ValueError, which neither pyserial nor the transport catches: a
    port opened while the process had ~1000 files open read nothing, never noticed an
    unplug, and its reader fired again on every pass of the loop;
  - open() makes two pipes (four descriptors per port) that only cancel_read() /
    cancel_write() use, to wake a blocking read or write.

``PosixSerial`` closes those pipes once the port is open and reads with os.read alone:
the transport only reads after the loop has seen the port readable. An empty read then
means the device went away (a hung-up tty reads empty and also refuses the input-queue
ioctl), unless the bytes were taken in the meantime, by a purge or another reader (the
port still answers the ioctl): the read is then just empty, as pyserial's would be.
Windows keeps pyserial's own class (overlapped I/O, polled by the transport).

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

_PIPES = ("pipe_abort_read_r", "pipe_abort_read_w", "pipe_abort_write_r", "pipe_abort_write_w")
# what pyserial's own read() says when a port reads empty although it was ready
_DISCONNECTED = ("device reports readiness to read but returned no data "
                 "(device disconnected or multiple access on port?)")


if os.name == "posix":
    from serial import serialposix

    class PosixSerial(serialposix.Serial):
        """pyserial's POSIX port without select() and without the abort pipes, for a
        non-blocking transport only: a read or write timeout other than 0 is refused
        (nothing could cancel a read or write that waits)."""

        fd: Any  # pyserial's: the port's descriptor while it is open, else None
        is_open: bool

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
            if self._write_timeout != 0:
                raise serial.SerialException("this port writes non-blocking only "
                                             "(write_timeout=0)")
            return super().write(data)

        def cancel_read(self) -> None:
            """Nothing to cancel: a read never waits."""

        def cancel_write(self) -> None:
            """Nothing to cancel: a write never waits."""


def serial_class() -> type:
    """The pyserial class a device is opened with on this OS."""
    return PosixSerial if os.name == "posix" else serial.Serial


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
    ``writer.transport.serial`` is the port. Raises serial.SerialException / OSError when
    it can't be opened."""
    import serial_asyncio_fast

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
        transport, _ = await serial_asyncio_fast.connection_for_serial(
            loop, lambda: protocol, ser)
    except BaseException:
        ser.close()
        raise
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
