"""A serial device that disappears and comes back, over socat PTYs (Linux, needs socat).

socat links two PTYs under fixed names (pty,link=...): stopping socat makes the device
vanish (reads on the port fail, as for an unplugged USB adapter) and starting it again
brings it back under the same path. Both runners keep their port with the same
supervision (serial_link.SerialLink), so both must report the outage and reopen:
  - Modbus gateway: a fake RTU slave answers on the device end. While the device is gone
    the mapping is reconnecting (not running, not "in use"), a master gets 0x0A, and once
    it is back the same Modbus/TCP master connection is served again;
  - raw bridge: a connected TCP client gets the device's data again after it returns,
    and its bytes reach the device.

Run: python3 tests/test_serial_reconnect.py
"""
import asyncio
import os
import shutil
import socket
import sys
import tempfile
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import notify
from app.config import MappingConfig, SerialSettings
from app.engine import modbus, serial_io
from app.engine.supervisor import Supervisor

REPLY_PDU = bytes([0x03, 0x02, 0x12, 0x34])  # read holding registers: one word
READ_PDU = bytes([0x03, 0x00, 0x00, 0x00, 0x01])
UNAVAILABLE_PDU = bytes([0x83, modbus.GATEWAY_PATH_UNAVAILABLE])
LOST = ("reconnecting", "device-missing")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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

    async def open_end(self):
        """The device side of the pair, as an asyncio stream pair."""
        reader, writer, _ser, _dev = await serial_io.open_serial(
            SerialSettings(port=self.device, baudrate=9600))
        return reader, writer


async def rtu_slave(device: Device):
    """Answer every RTU request on the device end with REPLY_PDU, until it goes away."""
    reader, writer = await device.open_end()
    buf = bytearray()
    try:
        while True:
            data = await reader.read(256)
            if not data:
                return
            buf += data
            try:
                unit, _pdu = modbus.rtu_unwrap(bytes(buf))
            except ValueError:  # not a whole frame yet
                continue
            buf.clear()
            writer.write(modbus.rtu_wrap(unit, REPLY_PDU))
            await writer.drain()
    except OSError:  # serial.SerialException: the device went away
        return
    finally:
        writer.close()


async def request(reader, writer, txn, pdu=READ_PDU, unit=1):
    writer.write(modbus.build_tcp_adu(txn, unit, pdu))
    await writer.drain()
    head = await asyncio.wait_for(reader.readexactly(modbus.MBAP_LEN), timeout=5)
    rest = await asyncio.wait_for(reader.readexactly(((head[4] << 8) | head[5]) - 1), timeout=5)
    assert ((head[0] << 8) | head[1], head[6]) == (txn, unit), head
    return rest


async def wait_for(cond, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def test_gateway_device_returns(folder):
    dev = Device(folder)
    await dev.plug()
    slave = asyncio.create_task(rtu_slave(dev))
    sup = Supervisor(logger=lambda _m: None)
    gw_port = free_port()
    m = MappingConfig.from_dict({
        "name": "gw", "enabled": True, "serial": {"port": dev.port, "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                    "port": gw_port},
        "options": {"modbus_response_timeout_s": 0.5}})
    names = {m.id: m.name}
    try:
        ok, msg = await sup.apply_mapping(m)
        assert ok, msg
        assert await wait_for(lambda: sup.status(m.id)["state"] == "running"), sup.status(m.id)
        mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(mr, mw, 1) == REPLY_PDU
        before = notify.snapshot(sup.all_status())
        print("gateway: master -> RTU slave over the PTY  OK")

        await dev.unplug()
        await asyncio.wait_for(slave, timeout=5)
        assert await wait_for(lambda: sup.status(m.id)["state"] in LOST
                              and sup.status(m.id)["reconnects"] >= 1), sup.status(m.id)
        st = sup.status(m.id)
        assert st["last_error"] and dev.port not in sup.devices_in_use(), st
        lost = sup.all_status()
        assert [e[0] for e in notify.status_events(before, lost, names)] == ["device_lost"]
        assert await request(mr, mw, 2) == UNAVAILABLE_PDU
        print(f"gateway: device gone -> {st['state']} ({st['last_error']}), 0x0A, "
              "device_lost  OK")

        await dev.plug()
        slave = asyncio.create_task(rtu_slave(dev))
        assert await wait_for(lambda: sup.status(m.id)["state"] == "running", timeout=25), \
            sup.status(m.id)
        assert await request(mr, mw, 3) == REPLY_PDU
        assert dev.port in sup.devices_in_use()
        events = notify.status_events(notify.snapshot(lost), sup.all_status(), names)
        assert [e[0] for e in events] == ["device_back"], events
        print("gateway: device back -> reopened, the same master is served again, "
              "device_back  OK")
        mw.close()
    finally:
        await sup.stop_all()
        slave.cancel()
        with suppress(BaseException):
            await slave
        if dev.proc is not None:
            await dev.unplug()


async def test_bridge_device_returns(folder):
    dev = Device(folder)
    await dev.plug()
    sup = Supervisor(logger=lambda _m: None)
    port = free_port()
    m = MappingConfig.from_dict({
        "name": "raw", "enabled": True, "serial": {"port": dev.port, "baudrate": 9600},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": port}})
    end_w = None
    try:
        ok, msg = await sup.apply_mapping(m)
        assert ok, msg
        assert await wait_for(lambda: sup.status(m.id)["state"] == "running"), sup.status(m.id)
        cr, cw = await asyncio.open_connection("127.0.0.1", port)
        end_r, end_w = await dev.open_end()
        await asyncio.sleep(0.2)
        end_w.write(b"before")
        await end_w.drain()
        assert await asyncio.wait_for(cr.readexactly(6), timeout=5) == b"before"

        await dev.unplug()
        assert await wait_for(lambda: sup.status(m.id)["state"] in LOST), sup.status(m.id)
        end_w.close()
        print(f"bridge: device gone -> {sup.status(m.id)['state']}  OK")

        await dev.plug()
        assert await wait_for(lambda: sup.status(m.id)["state"] == "running", timeout=25), \
            sup.status(m.id)
        end_r, end_w = await dev.open_end()
        await asyncio.sleep(0.2)
        end_w.write(b"after")
        await end_w.drain()
        assert await asyncio.wait_for(cr.readexactly(5), timeout=5) == b"after"
        cw.write(b"to-dev")
        await cw.drain()
        assert await asyncio.wait_for(end_r.readexactly(6), timeout=5) == b"to-dev"
        print("bridge: device back -> reopened, the connected client's data flows both ways  OK")
        cw.close()
    finally:
        await sup.stop_all()
        if end_w is not None:
            end_w.close()
        if dev.proc is not None:
            await dev.unplug()


async def main():
    if not shutil.which("socat"):
        print("SKIP: socat not found")
        return
    folder = tempfile.mkdtemp(prefix="s2n_replug_")
    try:
        await test_gateway_device_returns(folder)
        await test_bridge_device_returns(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nPASS: serial device lost and back (Modbus gateway + raw bridge)")


if __name__ == "__main__":
    asyncio.run(main())
