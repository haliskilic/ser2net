"""network.disconnect_without_device: TCP clients while the serial device is unavailable.

The serial port is a stand-in (each open is a connection to an echo server on 127.0.0.1,
which an unplug breaks); the mappings listen on OS-assigned ports.
  - on, TCP server (raw): a lost port disconnects the connected clients (the log says
    why), a client that connects while it is missing is turned away after the short wait
    (options.rfc2217_net_timeout_s) and never counted as connected; once the port is back
    clients are taken again;
  - off (the default, and every config written without the key): clients stay connected
    through the loss, new ones are taken, and what they send meanwhile is dropped and
    counted (bytes_dropped), as before;
  - on, RFC 2217: the session ends instead of following the reopened port;
  - on, connect-out: no connection while the port is missing, the link is closed when the
    port goes away and made again once it is back;
  - the form: a checkbox for TCP raw / telnet / RFC 2217 mappings with a presence marker
    (absent: the stored value is kept, a new mapping gets off).

Run: python3 tests/test_device_unavailable.py
"""
import asyncio
import contextlib
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import serial_io, serial_link
from app.engine.bridge import MappingRunner
from app.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields


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
    """Stands in for serial_io.open_serial: each open is a new connection to an echo
    server (the device). ``present = False`` makes an open fail as for an unplugged
    adapter; unplug() also breaks the open one, as the serial library does when a read
    on a port that went away fails."""

    def __init__(self):
        self.present = True
        self.opens = []

    async def start(self):
        self.server = await asyncio.start_server(self._echo, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    @staticmethod
    async def _echo(reader, writer):
        with contextlib.suppress(ConnectionError, OSError):
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        writer.close()

    async def open(self, settings):
        if not self.present:
            raise OSError(2, f"could not open port {settings.port}: No such file or directory")
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        self.opens.append(w)
        return r, w, FakeSerial(), settings.port

    def unplug(self):
        self.present = False
        self.opens[-1].transport.abort()

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


def _fast_pause(self):
    return 0.05


async def until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.02)


def mapping(protocol="raw", mode="server", remote_port=0, **network):
    net = {"mode": mode, "protocol": protocol, "bind_ip": "127.0.0.1", "port": 0,
           "max_connections": 4 if protocol != "rfc2217" else 1, **network}
    if mode == "client":
        net.update(remote_host="127.0.0.1", remote_port=remote_port, connect_timeout_s=2)
    return MappingConfig.from_dict({
        "name": "dev", "serial": {"port": "/dev/ttyFAKE-dev"}, "network": net,
        "options": {"rfc2217_net_timeout_s": 0.3}})


@contextlib.asynccontextmanager
async def running(m, device, logs):
    runner = MappingRunner(m, logger=logs.append)
    with mock.patch.object(serial_io, "open_serial", device.open), \
            mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
        await runner.start()
        try:
            await until(lambda: runner._link.is_open)
            yield runner
        finally:
            await asyncio.wait_for(runner.stop(), 15)


def port_of(runner):
    return runner._server.sockets[0].getsockname()[1]


async def eof_within(reader, seconds):
    """True when the connection closes within ``seconds`` (whatever comes before)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    with contextlib.suppress(ConnectionError):
        while loop.time() < end:
            try:
                if not await asyncio.wait_for(reader.read(4096), end - loop.time()):
                    return True
            except asyncio.TimeoutError:
                return False
        return False
    return True


async def _server_on():
    device, logs = FakeDevice(), []
    await device.start()
    try:
        async with running(mapping(disconnect_without_device=True), device, logs) as runner:
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1)
            w.write(b"ping")
            assert await asyncio.wait_for(r.readexactly(4), 5) == b"ping"
            device.unplug()
            assert await eof_within(r, 5), "the client was not disconnected"
            await until(lambda: runner.status.client_count == 0)
            assert any("disconnecting 1 client(s) (disconnect_without_device)" in ln
                       for ln in logs), logs
            assert any("client disconnected" in ln and "the serial port is not open" in ln
                       for ln in logs), logs
            # while the port is missing: turned away after the short wait, never counted
            t0 = asyncio.get_running_loop().time()
            r2, w2 = await asyncio.open_connection("127.0.0.1", port_of(runner))
            assert await eof_within(r2, 5)
            assert asyncio.get_running_loop().time() - t0 >= 0.25, "the short wait first"
            w2.close()
            assert any("the serial port is not open (disconnect_without_device)" in ln
                       for ln in logs), logs
            assert sum("client connected" in ln for ln in logs) == 1, logs
            # back: clients are taken again
            device.present = True
            await until(lambda: runner._link.is_open)
            r3, w3 = await asyncio.open_connection("127.0.0.1", port_of(runner))
            w3.write(b"back")
            assert await asyncio.wait_for(r3.readexactly(4), 5) == b"back"
            w3.close()
            w.close()
    finally:
        await device.close()


def test_server_on():
    asyncio.run(_server_on())
    print("on: a lost port disconnects the clients, new ones are turned away after the "
          "short wait, taken again once it is back  OK")


async def _server_off():
    device, logs = FakeDevice(), []
    await device.start()
    try:
        m = mapping()
        assert m.network.disconnect_without_device is False
        async with running(m, device, logs) as runner:
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1)
            device.unplug()
            await until(lambda: not runner._link.is_open)
            assert not await eof_within(r, 0.8), "the client stays connected"
            r2, w2 = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 2)
            w2.write(b"lost")
            await until(lambda: runner.status.bytes_dropped == 4)
            device.present = True
            await until(lambda: runner._link.is_open)
            w.write(b"ok")
            assert await asyncio.wait_for(r.readexactly(2), 5) == b"ok"
            w.close()
            w2.close()
    finally:
        await device.close()


def test_server_off():
    asyncio.run(_server_off())
    print("off (default): clients stay connected through a loss, new ones are taken, their "
          "data is dropped and counted  OK")


async def _rfc2217_on():
    device, logs = FakeDevice(), []
    await device.start()
    try:
        m = mapping(protocol="rfc2217", disconnect_without_device=True)
        async with running(m, device, logs) as runner:
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1)
            device.unplug()
            assert await eof_within(r, 5), "the RFC 2217 session ends"
            w.close()
    finally:
        await device.close()


def test_rfc2217_on():
    asyncio.run(_rfc2217_on())
    print("on, RFC 2217: the session ends with the port instead of following it  OK")


async def _connect_out_on():
    device, logs = FakeDevice(), []
    await device.start()
    links = []

    async def remote_peer(reader, writer):
        links.append(writer)
        with contextlib.suppress(ConnectionError, OSError):
            while await reader.read(4096):
                pass
        links.append("closed")

    remote = await asyncio.start_server(remote_peer, "127.0.0.1", 0)
    rport = remote.sockets[0].getsockname()[1]
    m = mapping(mode="client", remote_port=rport, disconnect_without_device=True)
    runner = MappingRunner(m, logger=logs.append)
    try:
        with mock.patch.object(serial_io, "open_serial", device.open), \
                mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
            device.present = False
            await runner.start()
            await asyncio.sleep(0.8)
            assert links == [], "no connection while the port is missing"
            device.present = True
            await until(lambda: len(links) == 1 and runner.status.link == "up")
            device.unplug()
            await until(lambda: "closed" in links)
            await asyncio.sleep(0.3)
            assert len(links) == 2, "not connected again while the port is missing"
            device.present = True
            await until(lambda: len(links) == 3 and runner.status.link == "up")
            await asyncio.wait_for(runner.stop(), 15)
    finally:
        if runner.status.state != "stopped":
            await runner.stop()
        remote.close()
        await device.close()


def test_connect_out_on():
    asyncio.run(_connect_out_on())
    print("on, connect-out: connects only while the port is open, closes the link when it "
          "goes away  OK")


def test_form():
    base = {"name": "dev", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}
    existing = MappingConfig.from_dict({"name": "dev", "serial": {"port": "COM7"},
                                        "network": {"port": 4001,
                                                    "disconnect_without_device": True}})
    assert MappingConfig.from_dict({"network": {}}).network.disconnect_without_device is False

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.network.disconnect_without_device

    marker = {"network_disconnect_without_device_shown": "1"}
    on = {"network_disconnect_without_device": "on"}
    assert parsed(dict(base, id=existing.id)) is True, "absent from the form: kept"
    assert parsed(dict(base, id=existing.id, **marker)) is False, "shown and unticked: off"
    assert parsed(dict(base, id=existing.id, **marker, **on)) is True
    assert parsed(base) is False and parsed(dict(base, **marker, **on)) is True
    print("form: disconnect_without_device kept when the form leaves it out, set when "
          "shown  OK")


def main():
    test_form()
    test_server_on()
    test_server_off()
    test_rfc2217_on()
    test_connect_out_on()
    print("\nPASS: clients while the serial device is unavailable (disconnect_without_device)")


if __name__ == "__main__":
    main()
