"""End-to-end RFC2217 tests: a real pyserial rfc2217:// client (and a raw TCP client
speaking RFC 2217 bytes) against our server, over socat PTYs (Linux, needs socat).

  - bidirectional data, and a client-requested baud rate is applied to the server-side
    serial.Serial via PortManager;
  - a client whose subnegotiation never ends is disconnected once it passes 1024 bytes,
    with the reason in the log, and the mapping goes on serving the next client;
  - a baud rate the port refuses (2^32-1: the OS can't take it) and reserved or malformed
    values are answered with the current setting: the pyserial client sees "remote
    rejected value", the port keeps its setting, the session and its data go on;
  - the device goes away and comes back under a connected pyserial client: the client
    stays connected (what it changes meanwhile is acknowledged and waits for the port),
    the reopened port is a new serial object that gets the client's settings (at the OS
    level too), data flows both ways and later changes reach the new port, not the old;
  - a connect-out RFC 2217 mapping connects only once its serial port is open;
  - what a client set stays on the port after it leaves (the default), or, with
    rfc2217_restore_on_disconnect, the port goes back to the mapping's settings.

socat links the PTYs under fixed names (pty,link=...), so a test can stop it (the device
vanishes, as an unplugged USB adapter does) and start it again under the same path.

Run: python3 tests/test_rfc2217.py
"""
import asyncio
import os
import shutil
import socket
import sys
import tempfile
import termios

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import serial

from ser2netweb.config import MappingConfig
from ser2netweb.engine.supervisor import Supervisor

IAC, SB, SE = 255, 250, 240


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Device:
    """socat with two linked PTYs: <dir>/port for the mapping, <dir>/device for the test."""

    def __init__(self, folder):
        self.port = os.path.join(folder, "port")
        self.device = os.path.join(folder, "device")
        self.proc = None

    async def plug(self):
        self.proc = await asyncio.create_subprocess_exec(
            "socat", "-d", "-d", f"pty,raw,echo=0,link={self.port}",
            f"pty,raw,echo=0,link={self.device}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        seen = 0
        while seen < 2:  # socat reports each PTY on stderr
            line = await self.proc.stderr.readline()
            if not line:
                raise RuntimeError("socat did not report its PTYs")
            seen += b"PTY is" in line
        for _ in range(100):
            if os.path.exists(self.port) and os.path.exists(self.device):
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("socat did not create the PTY links")

    async def unplug(self):
        self.proc.terminate()
        await self.proc.wait()
        self.proc = None


async def wait_for(cond, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def rfc2217_mapping(dev, port, **options):
    return MappingConfig.from_dict({
        "name": "test-rfc2217", "enabled": True,
        "serial": {"port": dev.port, "baudrate": 9600},
        "network": {"protocol": "rfc2217", "bind_ip": "127.0.0.1", "port": port},
        "options": options})


async def start(dev, **options):
    """A running rfc2217 mapping on the device's port: (supervisor, mapping, logs)."""
    logs = []
    sup = Supervisor(logger=logs.append)
    mapping = rfc2217_mapping(dev, free_port(), **options)
    ok, msg = await sup.apply_mapping(mapping)
    assert ok, f"runner failed: {msg}"
    assert await wait_for(lambda: sup.status(mapping.id)["state"] == "running"), \
        sup.status(mapping.id)
    return sup, mapping, logs


def open_client(port, baudrate=19200):
    return serial.serial_for_url(f"rfc2217://127.0.0.1:{port}", baudrate=baudrate, timeout=1)


async def test_data_and_baud_change(folder):
    dev = Device(folder)
    await dev.plug()
    sup, mapping, _logs = await start(dev)
    device = client = None
    try:
        device = serial.Serial(dev.device, baudrate=9600, timeout=0)
        # rfc2217 client requesting a DIFFERENT baud (19200) -> server must apply it
        client = await asyncio.to_thread(open_client, mapping.network.port)
        await asyncio.sleep(0.5)

        # the runner's underlying serial should now be at 19200 (set via RFC2217)
        runner = sup._runners[mapping.id]
        server_baud = runner.serial_instance.baudrate if runner.serial_instance else None
        print("server-side serial baudrate after client open:", server_baud)
        assert server_baud == 19200, f"expected 19200, got {server_baud}"

        # client -> device
        await asyncio.to_thread(client.write, b"ping")
        await asyncio.sleep(0.3)
        got = await asyncio.to_thread(device.read, 64)
        print("device received:", got)
        assert got == b"ping", got

        # device -> client
        await asyncio.to_thread(device.write, b"pong")
        await asyncio.to_thread(device.flush)
        await asyncio.sleep(0.3)
        got2 = await asyncio.to_thread(client.read, 4)
        print("client received:", got2)
        assert got2 == b"pong", got2
        print("RFC2217 data + live baud change work  OK")

        # by default what the client set stays when it leaves
        await asyncio.to_thread(client.close)
        client = None
        assert await wait_for(lambda: sup.status(mapping.id)["client_count"] == 0)
        await asyncio.sleep(0.2)
        assert runner.serial_instance.baudrate == 19200
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)
        if device is not None:
            device.close()
        await sup.stop_all()
        await dev.unplug()


async def test_endless_subnegotiation(folder):
    dev = Device(folder)
    await dev.plug()
    sup, mapping, logs = await start(dev)
    client = None
    try:
        r, w = await asyncio.open_connection("127.0.0.1", mapping.network.port)
        assert await asyncio.wait_for(r.read(256), timeout=2)  # the server's negotiation
        w.write(bytes([IAC, SB]))
        closed = False
        try:
            for _ in range(64):  # up to 256 KiB that never ends the subnegotiation
                w.write(b"A" * 4096)
                await w.drain()
                await asyncio.sleep(0.01)
            while await asyncio.wait_for(r.read(4096), timeout=2):
                pass
            closed = True
        except (ConnectionError, OSError):
            closed = True
        assert closed, "the client was not disconnected"
        assert await wait_for(lambda: any("client disconnected" in ln for ln in logs)), logs
        line = next(ln for ln in logs if "client disconnected" in ln)
        assert "protocol error: telnet subnegotiation longer than 1024 bytes" in line, line
        assert not any("pump error" in ln for ln in logs), logs
        w.close()
        # the mapping goes on serving: the next client gets its settings applied
        client = await asyncio.to_thread(open_client, mapping.network.port)
        assert sup._runners[mapping.id].serial_instance.baudrate == 19200
        print("an endless subnegotiation ends that session with the reason logged; the "
              "next client is served  OK")
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)
        await sup.stop_all()
        await dev.unplug()


async def read_answers(reader, want: bytes, timeout=3.0):
    """Read from the server until ``want`` has come (modem notifications may be mixed in)."""
    got = b""
    end = asyncio.get_running_loop().time() + timeout
    while want not in got:
        left = end - asyncio.get_running_loop().time()
        assert left > 0, (want.hex(), got.hex())
        got += await asyncio.wait_for(reader.read(256), timeout=left)
    return got


async def test_refused_values(folder):
    dev = Device(folder)
    await dev.plug()
    sup, mapping, logs = await start(dev)
    device = client = None
    try:
        device = serial.Serial(dev.device, baudrate=9600, timeout=0)
        runner = sup._runners[mapping.id]
        client = await asyncio.to_thread(open_client, mapping.network.port)
        assert runner.serial_instance.baudrate == 19200
        try:
            await asyncio.to_thread(setattr, client, "baudrate", 2 ** 32 - 1)
        except ValueError as e:
            assert "remote rejected value" in str(e), e
        else:
            raise AssertionError("a baud rate the port refused was acknowledged")
        assert runner.serial_instance.baudrate == 19200, runner.serial_instance.baudrate
        assert any("the serial port refused baudrate 4294967295" in ln for ln in logs), logs
        await asyncio.to_thread(setattr, client, "baudrate", 38400)
        assert runner.serial_instance.baudrate == 38400
        await asyncio.to_thread(client.write, b"ping")
        await asyncio.sleep(0.3)
        assert await asyncio.to_thread(device.read, 64) == b"ping"
        await asyncio.to_thread(client.close)
        client = None
        print("a baud rate the port refuses: 'remote rejected value', the port keeps its "
              "setting, the session goes on  OK")

        # reserved / malformed values from a client that speaks RFC 2217 itself
        assert await wait_for(lambda: sup.status(mapping.id)["client_count"] == 0)
        r, w = await asyncio.open_connection("127.0.0.1", mapping.network.port)
        await asyncio.wait_for(r.read(256), timeout=2)  # the server's negotiation
        w.write(bytes([IAC, SB, 44, 3, 9, IAC, SE]))  # parity 9 (reserved)
        await read_answers(r, bytes([IAC, SB, 44, 103, 1, IAC, SE]))  # parity: none
        w.write(bytes([IAC, SB, 44, 1, 0, 0, IAC, SE]))  # a two-byte baud rate
        await read_answers(r, bytes([IAC, SB, 44, 101, 0, 0, 0x96, 0, IAC, SE]))  # 38400
        w.write(b"raw")
        await w.drain()
        await asyncio.sleep(0.3)
        assert await asyncio.to_thread(device.read, 64) == b"raw"
        assert runner.serial_instance.baudrate == 38400
        assert sup.status(mapping.id)["client_count"] == 1
        assert not any("pump error" in ln for ln in logs), logs
        w.close()
        print("reserved / malformed values: answered with the current setting, the client "
              "stays connected  OK")
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)
        if device is not None:
            device.close()
        await sup.stop_all()
        await dev.unplug()


LOST = ("reconnecting", "device-missing")


async def test_restore_on_disconnect(folder):
    dev = Device(folder)
    await dev.plug()
    sup, mapping, logs = await start(dev, rfc2217_restore_on_disconnect=True)
    client = None
    try:
        runner = sup._runners[mapping.id]
        client = await asyncio.to_thread(open_client, mapping.network.port)  # 19200
        await asyncio.to_thread(setattr, client, "stopbits", serial.STOPBITS_TWO)
        ser = runner.serial_instance
        assert (ser.baudrate, ser.stopbits) == (19200, 2)
        await asyncio.to_thread(client.close)
        client = None
        assert await wait_for(lambda: any("serial settings restored" in ln for ln in logs)), logs
        assert runner.serial_instance is ser and (ser.baudrate, ser.stopbits) == (9600, 1)
        attrs = termios.tcgetattr(ser.fd)
        assert attrs[4] == termios.B9600 and not attrs[2] & termios.CSTOPB, attrs
        print("rfc2217_restore_on_disconnect: the port is back at the mapping's settings once "
              "the client has left  OK")
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)
        await sup.stop_all()
        await dev.unplug()


async def test_reopen_under_connected_client(folder):
    dev = Device(folder)
    await dev.plug()
    sup, mapping, logs = await start(dev)
    client = device = None
    try:
        runner = sup._runners[mapping.id]
        client = await asyncio.to_thread(open_client, mapping.network.port)  # 19200
        # (2 stop bits: some kernels' PTYs refuse parity / 7 data bits)
        await asyncio.to_thread(setattr, client, "stopbits", serial.STOPBITS_TWO)
        first = runner.serial_instance
        assert (first.baudrate, first.stopbits) == (19200, 2)

        await dev.unplug()
        assert await wait_for(lambda: sup.status(mapping.id)["state"] in LOST), \
            sup.status(mapping.id)
        # the client stays connected; what it changes meanwhile waits for the port
        await asyncio.to_thread(setattr, client, "baudrate", 38400)
        await asyncio.to_thread(client.reset_input_buffer)
        assert sup.status(mapping.id)["client_count"] == 1
        assert first.baudrate == 19200, "the old serial object was changed"
        print("device gone: the RFC 2217 client stays connected, its changes are "
              "acknowledged  OK")

        await dev.plug()
        assert await wait_for(lambda: sup.status(mapping.id)["state"] == "running",
                              timeout=25), sup.status(mapping.id)
        second = runner.serial_instance
        assert second is not first, "still the old serial object"
        assert (second.baudrate, second.stopbits) == (38400, 2), (second.baudrate,
                                                                 second.stopbits)
        attrs = termios.tcgetattr(second.fd)  # what the OS port runs at
        assert attrs[4] == termios.B38400 and attrs[2] & termios.CSTOPB, attrs
        device = serial.Serial(dev.device, baudrate=9600, timeout=0)
        await asyncio.to_thread(client.write, b"after")
        await asyncio.sleep(0.3)
        assert await asyncio.to_thread(device.read, 64) == b"after"
        await asyncio.to_thread(device.write, b"back")
        await asyncio.to_thread(device.flush)
        await asyncio.sleep(0.3)
        assert await asyncio.to_thread(client.read, 4) == b"back"
        # later changes reach the reopened port; a purge works on it
        await asyncio.to_thread(setattr, client, "baudrate", 57600)
        await asyncio.to_thread(client.reset_input_buffer)
        assert second.baudrate == 57600 and first.baudrate == 19200
        assert sup.status(mapping.id)["client_count"] == 1
        assert not any("client disconnected" in ln or "pump error" in ln for ln in logs), logs
        print("device back: the session drives the reopened port, which got the client's "
              "settings; data flows, later changes reach it  OK")
    finally:
        if client is not None:
            await asyncio.to_thread(client.close)
        if device is not None:
            device.close()
        await sup.stop_all()
        if dev.proc is not None:
            await dev.unplug()


async def test_connect_out_waits_for_the_port(folder):
    dev = Device(folder)  # not plugged in yet
    accepted: asyncio.Queue = asyncio.Queue()

    async def on_conn(reader, writer):
        await accepted.put((reader, writer))

    srv = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    logs = []
    sup = Supervisor(logger=logs.append)
    m = MappingConfig.from_dict({
        "name": "rfc2217-out", "enabled": True,
        "serial": {"port": dev.port, "baudrate": 9600},
        "network": {"mode": "client", "protocol": "rfc2217", "remote_host": "127.0.0.1",
                    "remote_port": srv.sockets[0].getsockname()[1]}})
    try:
        ok, msg = await sup.apply_mapping(m)
        assert ok, msg
        await asyncio.sleep(1.5)
        assert accepted.empty(), "connected out while the serial port is missing"
        assert not any("pump error" in ln for ln in logs), logs
        await dev.plug()
        r, w = await asyncio.wait_for(accepted.get(), timeout=25)
        negotiation = await asyncio.wait_for(r.read(256), timeout=2)
        assert bytes([IAC, 251, 44]) in negotiation, negotiation.hex()  # WILL COM-PORT
        w.write(bytes([IAC, SB, 44, 1, 0, 0, 0, 0, IAC, SE]))  # what is the baud rate?
        await read_answers(r, bytes([IAC, SB, 44, 101, 0, 0, 0x25, 0x80, IAC, SE]))  # 9600
        w.close()
        print("connect-out RFC 2217: connects once the serial port is open, the session "
              "answers  OK")
    finally:
        await sup.stop_all()
        srv.close()
        if dev.proc is not None:
            await dev.unplug()


async def main():
    if not shutil.which("socat"):
        print("SKIP: socat not found")
        return
    folder = tempfile.mkdtemp(prefix="s2n_rfc2217_")
    try:
        await test_data_and_baud_change(folder)
        await test_endless_subnegotiation(folder)
        await test_refused_values(folder)
        await test_reopen_under_connected_client(folder)
        await test_connect_out_waits_for_the_port(folder)
        await test_restore_on_disconnect(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nPASS: RFC2217 data + live baud change, bounded subnegotiation, refused values, "
          "reopened port, connect-out, restore on disconnect")


if __name__ == "__main__":
    asyncio.run(main())
