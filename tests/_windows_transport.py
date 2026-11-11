"""The Windows branch of ser2net's serial transport, driven by stand-in ports (a helper of
tests/test_serial_compat.py, which runs it in a child process: python _windows_transport.py).

pyserial-asyncio-fast polls a Windows port on event-loop timers (no file descriptors), and
serial_compat.Transport overrides those polls. That code is plain Python, so off Windows it
is loaded here as Windows would load it: pyserial-asyncio-fast with os.name "nt" and
serial_compat with sys.platform "win32", everything else (asyncio, pyserial) imported first
with the real values, which are put back at once. On Windows it is simply imported.

Checked (each prints one OK line):
  - backpressure: through a port whose writes are tracked (serial_compat.OverlappedWrites
    over a stand-in serial.win32), drain() waits for the device, so what is written stays
    within the transport's high-water mark plus the write window of what the device took;
  - a graceful close with data still in the transport's buffer completes once the device
    takes it (upstream's transport stops writing once closing, and never completes);
  - a port that fails while the transport writes (unplugged) is closed, not polled again.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import serial  # with the real os.name: it picks its backend by it

import app.engine  # noqa: F401
from _fake_win32 import FakeWin32

SIMULATED = os.name != "nt"
if SIMULATED:
    real_name, real_platform = os.name, sys.platform
    os.name = "nt"
    try:
        import serial_asyncio_fast as saf
    finally:
        os.name = real_name
    sys.platform = "win32"
    try:
        from app.engine import serial_compat
    finally:
        sys.platform = real_platform
else:
    import serial_asyncio_fast as saf

    from app.engine import serial_compat

assert hasattr(serial_compat.Transport, "_poll_write"), "not the Windows branch"
HIGH_WATER = 64 * 1024  # the transport's default


class WinPort:
    """What the Windows transport uses of a port, writing through OverlappedWrites on a
    stand-in win32 (as serial_compat.WindowsSerial does)."""

    def __init__(self, win32, window=serial_compat.WRITE_WINDOW):
        self.timeout = self.write_timeout = None
        self.win32 = win32
        self.writes = serial_compat.OverlappedWrites(win32, 7, window)
        self.in_waiting = 0
        self.fail = None  # raised by out_waiting once set (an unplugged port)
        self.closed = False

    @property
    def out_waiting(self):
        if self.fail is not None:
            raise self.fail
        return self.writes.pending()

    def write(self, data):
        return self.writes.write(bytes(data))

    def read(self, size=1):
        return b""

    def flush(self):
        pass

    def reset_output_buffer(self):
        self.win32.purge()

    def close(self):
        self.closed = True


class Lost(asyncio.Protocol):
    def __init__(self):
        self.lost = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc):
        if not self.lost.done():
            self.lost.set_result(exc)


async def backpressure():
    loop = asyncio.get_running_loop()
    win32 = FakeWin32()
    port = WinPort(win32)
    reader = asyncio.StreamReader(loop=loop)
    proto = asyncio.StreamReaderProtocol(reader, loop=loop)
    t = serial_compat.Transport(loop, proto, port)
    writer = asyncio.StreamWriter(t, proto, reader, loop)
    await asyncio.sleep(0.01)
    taken = 0
    done = False

    async def device():  # takes 2 KiB every 10 ms
        nonlocal taken
        while not done:
            await asyncio.sleep(0.01)
            taken += win32.complete(2048)

    dev = asyncio.ensure_future(device())
    written = ahead = 0
    t0 = time.monotonic()
    for _ in range(256):  # 256 KiB
        writer.write(b"d" * 1024)
        written += 1024
        await writer.drain()
        ahead = max(ahead, written - taken)
    took = time.monotonic() - t0
    done = True
    await dev
    t.abort()
    limit = HIGH_WATER + serial_compat.WRITE_WINDOW + 2048
    assert ahead <= limit, f"the writer got {ahead} bytes ahead of the device (limit {limit})"
    assert took > 0.5, f"drain() did not wait for the device ({took:.2f} s for 256 KiB)"
    print(f"Windows transport: drain() waits for the device: at most {ahead} bytes ahead of "
          f"it (high-water + write window), 256 KiB in {took:.1f} s  OK")


async def close_with_data(cls):
    """Close a ``cls`` transport whose port refuses writes for now, with 10 KiB in its
    buffer; then the device takes data again. Whether the close completed."""
    loop = asyncio.get_running_loop()
    win32 = FakeWin32()
    port = WinPort(win32, window=1024)
    proto = Lost()
    t = cls(loop, proto, port)
    await asyncio.sleep(0.01)
    for _ in range(10):
        t.write(b"c" * 1024)
    assert t.get_write_buffer_size() > 0
    t.close()
    for _ in range(100):
        await asyncio.sleep(0.01)
        win32.complete(1024)
        if proto.lost.done():
            break
    ok = proto.lost.done() and port.closed and not t.get_write_buffer_size()
    if not ok:
        t.abort()
        await asyncio.sleep(0.05)
    return ok


async def unplugged():
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    win32 = FakeWin32()
    port = WinPort(win32, window=1024)
    proto = Lost()
    t = serial_compat.Transport(loop, proto, port)
    await asyncio.sleep(0.01)
    t.write(b"u" * 4096)  # 1 KiB in flight, the rest buffered: the write poll runs
    port.fail = serial.SerialException("ClearCommError failed (device removed)")
    exc = await asyncio.wait_for(proto.lost, 2)
    await asyncio.sleep(0.05)
    assert isinstance(exc, serial.SerialException), exc
    assert port.closed
    assert len(errors) == 1, f"the failing port was polled again: {len(errors)} errors"
    print("Windows transport: a port that fails while writing is closed once, not polled "
          "again  OK")


async def main():
    await backpressure()
    assert await close_with_data(serial_compat.Transport), \
        "a graceful close with data left did not complete"
    upstream = await close_with_data(saf.SerialTransport)
    print("Windows transport: a graceful close with data in the buffer completes once the "
          f"device takes it (upstream's: {'completes' if upstream else 'never completes'})  OK")
    await unplugged()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"PASS: Windows transport ({'simulated off Windows' if SIMULATED else 'native'})")
