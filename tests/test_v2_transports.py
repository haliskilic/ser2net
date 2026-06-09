"""v2.0 transports: TCP connect-out (client), UDP, and serial<->serial bridge.
Run: python3 tests/test_v2_transports.py
"""
import asyncio
import os
import re
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor


async def socat():
    p = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        m = re.search(rb"PTY is (\S+)", await p.stderr.readline())
        if m:
            devs.append(m.group(1).decode())
    return p, devs[0], devs[1]


async def test_tcp_client():
    import serial
    p, dev_a, dev_b = await socat()
    # remote server the bridge will connect OUT to
    accepted = {}
    ev = asyncio.Event()

    async def on_conn(reader, writer):
        accepted["r"], accepted["w"] = reader, writer
        ev.set()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 48010)
    sup = Supervisor(logger=lambda m: None)
    m = MappingConfig.from_dict({
        "name": "cli", "enabled": True, "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"mode": "client", "protocol": "raw",
                    "remote_host": "127.0.0.1", "remote_port": 48010}})
    await sup.apply_mapping(m)
    await asyncio.wait_for(ev.wait(), timeout=3)
    await asyncio.sleep(0.3)
    device = serial.Serial(dev_b, 115200, timeout=1)
    r, w = accepted["r"], accepted["w"]
    # remote -> bridge -> serial
    w.write(b"ping"); await w.drain()
    await asyncio.sleep(0.3)
    assert (await asyncio.to_thread(device.read, 16)) == b"ping"
    # serial -> bridge -> remote
    await asyncio.to_thread(device.write, b"pong"); await asyncio.to_thread(device.flush)
    assert (await asyncio.wait_for(r.read(16), 2)) == b"pong"
    print("TCP connect-out (client mode): bidirectional  OK")
    w.close(); device.close(); server.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_udp():
    import serial
    p, dev_a, dev_b = await socat()
    sup = Supervisor(logger=lambda m: None)
    m = MappingConfig.from_dict({
        "name": "udp", "enabled": True, "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"mode": "udp", "protocol": "raw", "bind_ip": "127.0.0.1", "port": 48020}})
    await sup.apply_mapping(m)
    await asyncio.sleep(0.4)
    device = serial.Serial(dev_b, 115200, timeout=1)
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    us.bind(("127.0.0.1", 0)); us.settimeout(2)
    us.sendto(b"udp-in", ("127.0.0.1", 48020))   # client -> bridge -> serial
    await asyncio.sleep(0.3)
    assert (await asyncio.to_thread(device.read, 16)) == b"udp-in"
    await asyncio.to_thread(device.write, b"udp-out"); await asyncio.to_thread(device.flush)
    await asyncio.sleep(0.3)
    data, _ = await asyncio.to_thread(us.recvfrom, 64)        # serial -> bridge -> client
    assert data == b"udp-out", data
    print("UDP mode: bidirectional datagrams  OK")
    us.close(); device.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_serialbridge():
    import serial
    pa, a1, a2 = await socat()    # a1 = bridge serial A, a2 = external A
    pb, b1, b2 = await socat()    # b1 = bridge serial B, b2 = external B
    sup = Supervisor(logger=lambda m: None)
    m = MappingConfig.from_dict({
        "name": "sb", "enabled": True, "kind": "serialbridge",
        "serial": {"port": a1, "baudrate": 115200},
        "serial_b": {"port": b1, "baudrate": 115200}})
    ok, msg = await sup.apply_mapping(m)
    assert ok, msg
    await asyncio.sleep(0.5)
    ext_a = serial.Serial(a2, 115200, timeout=1)
    ext_b = serial.Serial(b2, 115200, timeout=1)
    await asyncio.to_thread(ext_a.write, b"A2B"); await asyncio.to_thread(ext_a.flush)
    await asyncio.sleep(0.3)
    assert (await asyncio.to_thread(ext_b.read, 16)) == b"A2B"
    await asyncio.to_thread(ext_b.write, b"B2A"); await asyncio.to_thread(ext_b.flush)
    await asyncio.sleep(0.3)
    assert (await asyncio.to_thread(ext_a.read, 16)) == b"B2A"
    print("serial<->serial bridge: bidirectional  OK")
    ext_a.close(); ext_b.close(); await sup.stop_all()
    pa.terminate(); pb.terminate(); await pa.wait(); await pb.wait()


async def main():
    await test_tcp_client()
    await test_udp()
    await test_serialbridge()
    print("\nPASS: TCP-client + UDP + serial-bridge transports")


if __name__ == "__main__":
    asyncio.run(main())
