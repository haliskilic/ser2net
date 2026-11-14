"""A stand-in serial device for runner tests: serial_io.open_serial is patched to
FakeDevice.open, and each open is a connection to a TCP server on 127.0.0.1 that plays the
device. A test makes it speak (say), reads what was written to it (heard) and sees how
often and whether the port is open (opens, is_open); ``present = False`` makes an open
fail as for an unplugged adapter, and unplug() also breaks the open port.
"""
import asyncio
import contextlib
from unittest import mock

from ser2netweb.engine import serial_io, serial_link
from ser2netweb.engine.bridge import MappingRunner


class FakeSerial:
    def __init__(self):
        self.__dict__.update(baudrate=9600, bytesize=8, parity="N", stopbits=1,
                             xonxoff=False, rtscts=False, dsrdtr=False, rts=True, dtr=True,
                             break_condition=False, cts=False, dsr=False, ri=False, cd=False)

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass


class FakeDevice:
    def __init__(self, echo=False):
        self.present = True
        self.echo = echo
        self.opens = 0            # how often the port was opened
        self.heard = bytearray()  # what was written to the device
        self._ends = []           # the device's end of each open port
        self._port_ends = []      # the port's end (the runner's stream)

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _serve(self, reader, writer):
        self._ends.append(writer)
        with contextlib.suppress(ConnectionError, OSError):
            while data := await reader.read(4096):
                self.heard += data
                if self.echo:
                    writer.write(data)
                    await writer.drain()
        with contextlib.suppress(ValueError):
            self._ends.remove(writer)
        writer.close()

    async def open(self, settings):
        if not self.present:
            raise OSError(2, f"could not open port {settings.port}: No such file or directory")
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        self.opens += 1
        self._port_ends.append(w)
        return r, w, FakeSerial(), settings.port

    @property
    def is_open(self):
        return bool(self._ends)

    def say(self, data):
        """The device sends ``data``."""
        self._ends[-1].write(data)

    def unplug(self):
        self.present = False
        self._port_ends[-1].transport.abort()

    async def close(self):
        self.server.close()
        for w in list(self._ends):
            w.close()


def _fast_pause(self):
    return 0.05


async def until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def running(mapping, device, logs, wait_open=True):
    """A started MappingRunner on ``device`` (stopped at the end)."""
    runner = MappingRunner(mapping, logger=logs.append)
    with mock.patch.object(serial_io, "open_serial", device.open), \
            mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
        await runner.start()
        try:
            if wait_open:
                await until(lambda: runner._link.is_open and device.is_open)
            yield runner
        finally:
            await asyncio.wait_for(runner.stop(), 15)


def port_of(runner):
    return runner._server.sockets[0].getsockname()[1]


async def read_for(reader, seconds):
    """What arrives within ``seconds``, as the list of reads (b"" last: closed)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    out = []
    with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
        while loop.time() < end:
            data = await asyncio.wait_for(reader.read(65536), end - loop.time())
            out.append(data)
            if not data:
                break
    return out


async def eof_within(reader, seconds):
    """True when the connection closes within ``seconds`` (whatever comes before)."""
    got = await read_for(reader, seconds)
    return bool(got) and got[-1] == b"" or False
