"""End-to-end RFC2217 tests: a real pyserial rfc2217:// client (and a raw TCP client
speaking RFC 2217 bytes) against our server, over socat PTYs (Linux, needs socat).

  - bidirectional data, and a client-requested baud rate is applied to the server-side
    serial.Serial via PortManager;
  - a client whose subnegotiation never ends is disconnected once it passes 1024 bytes,
    with the reason in the log, and the mapping goes on serving the next client.

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import serial

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

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


async def main():
    if not shutil.which("socat"):
        print("SKIP: socat not found")
        return
    folder = tempfile.mkdtemp(prefix="s2n_rfc2217_")
    try:
        await test_data_and_baud_change(folder)
        await test_endless_subnegotiation(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nPASS: RFC2217 data + live baud change, bounded subnegotiation")


if __name__ == "__main__":
    asyncio.run(main())
