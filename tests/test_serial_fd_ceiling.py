"""Serial ports read without select(): a port at file descriptor 1024 or above works (POSIX).

pyserial's POSIX read() asks select() first, and select() takes no descriptor at or above
FD_SETSIZE (1024): it raises ValueError, which neither pyserial nor pyserial-asyncio-fast
catches. A port opened while the process had ~1000 files open (many mappings, clients,
or a raised open-file limit) read nothing, never noticed an unplug, and its reader fired
again on every pass of the event loop. serial_compat.PosixSerial reads with os.read only
and keeps one descriptor per port (pyserial's two abort pipes are closed once it is open).

Checked here on PTYs (os.openpty: no socat, no serial hardware): one descriptor per port;
an empty read is "no data" while the port is there and a disconnect once it is hung up
(with pyserial's message); blocking use is refused; a write to a port that takes nothing
returns at once (pyserial's own spins); data both ways and the hang-up at a
descriptor above 1024, through serial_io.open_serial and through a raw bridge mapping
(binds 127.0.0.1); the pyserial / pyserial-asyncio-fast internals this builds on.
Skipped on Windows (its serial ports are not file descriptors).

Run: python3 tests/test_serial_fd_ceiling.py
"""
import asyncio
import contextlib
import inspect
import os
import socket
import sys
import time

import serial

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _fds import Skip, high_fds, open_fds

from ser2netweb.config import MappingConfig, SerialSettings
from ser2netweb.engine import serial_io

DISCONNECTED = "device reports readiness to read but returned no data"


class Pty:
    """A PTY pair: the slave's path is the "device", the master end is the far side."""

    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)  # the code under test opens it by path

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def recv(self, n: int, timeout: float = 3.0) -> bytes:
        """Up to ``n`` bytes the device side got, waiting at most ``timeout`` seconds."""
        import select
        p = select.poll()
        p.register(self.master, select.POLLIN)
        got = b""
        end = time.monotonic() + timeout
        while len(got) < n and p.poll(max(0.0, end - time.monotonic()) * 1000):
            got += os.read(self.master, n - len(got))
        return got

    async def arecv(self, n: int, timeout: float = 3.0) -> bytes:
        """recv() without holding up the event loop the code under test runs on."""
        return await asyncio.to_thread(self.recv, n, timeout)

    def unplug(self) -> None:
        """Close the master: Linux hangs the slave up, as for a removed USB adapter."""
        if self.master is not None:
            os.close(self.master)
            self.master = None




def test_one_descriptor_per_port():
    from ser2netweb.engine.serial_compat import PosixSerial, serial_class

    assert serial_class() is PosixSerial
    pty = Pty()
    try:
        before = open_fds()
        ser = PosixSerial(pty.path, 9600, timeout=0, write_timeout=0)
        assert open_fds() == before + 1, "a port must hold one descriptor (no abort pipes)"
        assert all(getattr(ser, n) is None for n in ("pipe_abort_read_r", "pipe_abort_read_w",
                                                     "pipe_abort_write_r", "pipe_abort_write_w"))
        assert ser.read(100) == b"", "no data yet: an empty read"
        pty.send(b"abc")
        got = b""
        for _ in range(50):
            got += ser.read(100)
            if len(got) >= 3:
                break
            time.sleep(0.02)
        assert got == b"abc", got
        assert ser.write(b"xyz") == 3
        assert pty.recv(3) == b"xyz"
        ser.close()
        ser.close()  # twice is fine, as with pyserial
        assert open_fds() == before, "close() must release the port's descriptor"
    finally:
        pty.unplug()
    print("PosixSerial: one descriptor per port, empty read = no data, reads and writes  OK")


def test_hangup_and_blocking_use():
    from ser2netweb.engine.serial_compat import PosixSerial

    pty = Pty()
    ser = PosixSerial(pty.path, 9600, timeout=0, write_timeout=0)
    try:
        ser.timeout = 1
        with contextlib.suppress(serial.SerialException):
            ser.read(1)
            raise AssertionError("a blocking read must be refused (nothing could cancel it)")
        ser.timeout = 0
        ser.write_timeout = 1
        with contextlib.suppress(serial.SerialException):
            ser.write(b"x")
            raise AssertionError("a blocking write must be refused")
        ser.write_timeout = 0
        assert ser.read(10) == b"", "the port is there: still no data, no error"
        pty.unplug()
        try:
            data = ser.read(10)
        except serial.SerialException as e:
            assert DISCONNECTED in str(e) or "read failed" in str(e), e
        else:
            raise AssertionError(f"a hung-up port must raise, read {data!r}")
    finally:
        ser.close()
        pty.unplug()
    print("PosixSerial: a hung-up port raises pyserial's disconnect error; blocking use "
          "refused  OK")


def test_write_to_a_port_that_takes_nothing():
    """A device that takes no data (here a PTY nobody reads): a write the OS refuses is
    raised as BlockingIOError at once (the transport keeps the data), it does not spin."""
    import signal

    from ser2netweb.engine.serial_compat import PosixSerial

    def stuck(*_args):
        raise AssertionError("a write to a port that takes nothing did not return")

    pty = Pty()
    ser = PosixSerial(pty.path, 9600, timeout=0, write_timeout=0)
    signal.signal(signal.SIGALRM, stuck)
    signal.alarm(10)
    try:
        took = 0
        for _ in range(1000):  # far more than a PTY holds
            try:
                took += ser.write(b"z" * 1000)
            except BlockingIOError:
                break
        else:
            raise AssertionError("the PTY never filled up")
        assert took > 0
        assert pty.recv(took) == b"z" * took, "what a write reported taken must arrive"
        assert ser.write(b"more") > 0, "once there is room again, writes go through"
    finally:
        signal.alarm(0)
        ser.close()
        pty.unplug()
    print("PosixSerial: a write to a port that takes nothing raises BlockingIOError at once "
          "(no spinning), and goes through once there is room  OK")


async def _open_above_fd_setsize():
    pty = Pty()
    errors = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    try:
        with high_fds():
            reader, writer, ser, _dev = await serial_io.open_serial(
                SerialSettings(port=pty.path, baudrate=115200))
        assert ser.fileno() >= 1024, ser.fileno()
        pty.send(b"from the device")
        got = b""
        while len(got) < 15:
            got += await asyncio.wait_for(reader.read(100), 2)
        assert got == b"from the device", got
        writer.write(b"to the device")
        await writer.drain()
        got = await pty.arecv(13)
        assert got == b"to the device", got
        pty.unplug()
        try:
            data = await asyncio.wait_for(reader.read(100), 2)
        except serial.SerialException as e:
            assert DISCONNECTED in str(e) or "read failed" in str(e), e
        else:
            assert data == b"", data  # EOF
        writer.close()
        await asyncio.sleep(0.2)
        assert not errors, f"the event loop reported errors: {errors}"
    finally:
        pty.unplug()


def test_stream_above_fd_setsize():
    asyncio.run(_open_above_fd_setsize())
    print("open_serial at fd >= 1024: data both ways, the unplug is noticed, no loop errors  OK")


async def _bridge_above_fd_setsize():
    from ser2netweb.engine.bridge import MappingRunner

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    pty = Pty()
    lines = []
    m = MappingConfig.from_dict({
        "name": "high-fd", "serial": {"port": pty.path, "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": port}})
    runner = MappingRunner(m, logger=lines.append)
    try:
        with high_fds():
            await runner.start()
            for _ in range(100):
                if runner.serial_instance is not None:
                    break
                await asyncio.sleep(0.02)
        ser = runner.serial_instance
        assert ser is not None and ser.fileno() >= 1024, (ser and ser.fileno(), lines)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.2)
        pty.send(b"meter reading\n")
        assert await asyncio.wait_for(r.readexactly(14), 3) == b"meter reading\n"
        w.write(b"command\n")
        await w.drain()
        got = await pty.arecv(8)
        assert got == b"command\n", (got, lines)
        pty.unplug()
        for _ in range(100):
            if runner.status.state in ("reconnecting", "device-missing"):
                break
            await asyncio.sleep(0.05)
        assert runner.status.state in ("reconnecting", "device-missing"), runner.status.state
        assert any("serial error" in ln for ln in lines), lines
        w.close()
    finally:
        await runner.stop()
        pty.unplug()


def test_bridge_above_fd_setsize():
    asyncio.run(_bridge_above_fd_setsize())
    print("raw bridge with its port at fd >= 1024: device <-> TCP client, unplug noticed  OK")


def test_upstream_internals():
    """What serial_compat builds on, so a pyserial / pyserial-asyncio-fast bump that changes
    it fails here instead of in the field (without pyserial-asyncio-fast: the copy of its
    transport serial_compat builds on then)."""
    from serial import serialposix

    from ser2netweb.engine.serial_compat import _saf as saf

    opened = inspect.getsource(serialposix.Serial.open)
    for name in ("pipe_abort_read_r", "pipe_abort_read_w", "pipe_abort_write_r",
                 "pipe_abort_write_w"):
        assert name in opened, f"pyserial's open() no longer makes {name}"
    assert "self.fd = os.open(" in opened, "pyserial's open() no longer keeps the port in .fd"
    for attr in ("_timeout", "_write_timeout", "is_open"):
        assert hasattr(serial.Serial(), attr), attr
    write_data = inspect.getsource(saf.SerialTransport._write_data)
    assert "n = self._serial.write(data)" in write_data, write_data
    assert "except (BlockingIOError, InterruptedError)" in write_data, write_data
    read_ready = inspect.getsource(saf.SerialTransport._read_ready)
    assert "self._serial.read(self._max_read_size)" in read_ready, read_ready
    assert "except serial.SerialException" in read_ready, read_ready
    init = inspect.getsource(saf.SerialTransport.__init__)
    assert "self._serial.timeout = 0" in init and "self._serial.write_timeout = 0" in init
    init_code = saf.SerialTransport.__init__.__code__  # (its annotations don't evaluate on 3.14)
    assert init_code.co_varnames[:init_code.co_argcount] == (
        "self", "loop", "protocol", "serial_instance")
    if "select.select" not in inspect.getsource(serialposix.Serial.read):
        print("note: pyserial's read() no longer uses select(); serial_compat may be simpler now")
    print("pyserial / pyserial-asyncio-fast internals serial_compat relies on are there  OK")


if __name__ == "__main__":
    if os.name != "posix":
        print("SKIP: POSIX only (Windows serial ports are not file descriptors)")
        raise SystemExit(0)
    try:
        test_one_descriptor_per_port()
        test_hangup_and_blocking_use()
        test_write_to_a_port_that_takes_nothing()
        test_stream_above_fd_setsize()
        test_bridge_above_fd_setsize()
    except Skip as e:
        print(f"SKIP (part): {e}")
    test_upstream_internals()
    print("\nPASS: serial ports read without select(), one descriptor each")
