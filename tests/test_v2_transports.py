"""v2.0 transports: TCP connect-out (client), UDP, and serial<->serial bridge.

UDP also: a flood against a device that reads slowly stays bounded (the queue and the
port's write buffer), the event loop stays responsive and the device gets exactly what is
counted as written; with a reply address the device's output goes there, not to the sender.
Run: python3 tests/test_v2_transports.py
"""
import asyncio
import os
import re
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.bridge import UDP_BACKLOG_MAX
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
    st = sup.status(m.id)
    assert (st["bytes_in"], st["bytes_out"]) == (4, 4), st
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
    st = sup.status(m.id)
    assert (st["bytes_in"], st["bytes_out"]) == (len(b"udp-out"), len(b"udp-in")), st
    print("UDP mode: bidirectional datagrams  OK")
    us.close(); device.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_udp_flood():
    """~4 MB of datagrams as fast as loopback carries them, to a device that reads about
    100 KB/s: what waits for the port stays bounded, the rest is dropped and counted, the
    loop keeps running, and the device gets exactly bytes_out."""
    import serial
    p, dev_a, dev_b = await socat()
    sup = Supervisor(logger=lambda m: None)
    m = MappingConfig.from_dict({
        "name": "udp-flood", "enabled": True, "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"mode": "udp", "protocol": "raw", "bind_ip": "127.0.0.1", "port": 48021}})
    await sup.apply_mapping(m)
    await asyncio.sleep(0.4)
    runner = sup.get_runner(m.id)
    udp = runner._udp_transport.get_protocol()
    device = serial.Serial(dev_b, 115200, timeout=0)
    got = bytearray()
    done = threading.Event()

    def slow_device():
        while not done.is_set():
            got.extend(device.read(1024))
            time.sleep(0.01)

    reader = threading.Thread(target=slow_device, daemon=True)
    reader.start()
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload, count = b"\x55" * 1400, 3000

    def flood():
        for _ in range(count):
            us.sendto(payload, ("127.0.0.1", 48021))

    loop = asyncio.get_running_loop()
    lag = peak_buffer = peak_queue = 0.0
    sender = asyncio.create_task(asyncio.to_thread(flood))
    while not sender.done():
        t0 = loop.time()
        await asyncio.sleep(0.005)
        lag = max(lag, loop.time() - t0 - 0.005)
        w = runner._link.writer
        if w is not None:
            peak_buffer = max(peak_buffer, w.transport.get_write_buffer_size())
        peak_queue = max(peak_queue, udp._queued)
    await sender
    # let the device read what the port took
    last, still = -1, 0
    for _ in range(300):
        await asyncio.sleep(0.05)
        still = still + 1 if len(got) == last else 0
        last = len(got)
        if still >= 20 and len(got) == sup.status(m.id)["bytes_out"]:
            break
    done.set()
    reader.join(timeout=2)
    st = sup.status(m.id)
    print(f"UDP flood: out={st['bytes_out']} dropped={st['bytes_dropped']} "
          f"peak queue={peak_queue} peak port buffer={peak_buffer} max loop lag={lag:.3f}s")
    assert st["bytes_dropped"] > 0, st
    assert peak_queue <= UDP_BACKLOG_MAX
    assert peak_buffer <= 2 * UDP_BACKLOG_MAX, "the port's buffer grows with the flood"
    assert lag < 0.5, f"the event loop stalled for {lag:.2f}s"
    assert len(got) == st["bytes_out"], (len(got), st)
    assert st["bytes_out"] + st["bytes_dropped"] <= count * len(payload)
    print("UDP flood: bounded queue and port buffer, drops counted, device got bytes_out  OK")
    us.close(); device.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_udp_reply_address():
    import serial
    p, dev_a, dev_b = await socat()
    reply = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reply.bind(("127.0.0.1", 0)); reply.settimeout(2)
    rport = reply.getsockname()[1]
    sup = Supervisor(logger=lambda m: None)
    m = MappingConfig.from_dict({
        "name": "udp-fixed", "enabled": True, "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"mode": "udp", "protocol": "raw", "bind_ip": "127.0.0.1", "port": 48022,
                    "udp_peer_host": "127.0.0.1", "udp_peer_port": rport}})
    ok, msg = await sup.apply_mapping(m)
    assert ok, msg
    await asyncio.sleep(0.4)
    assert [c["peer"] for c in sup.status(m.id)["clients"]] == [f"127.0.0.1:{rport}"]
    device = serial.Serial(dev_b, 115200, timeout=1)
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.bind(("127.0.0.1", 0)); other.settimeout(0.5)
    other.sendto(b"cmd", ("127.0.0.1", 48022))           # any allowed sender reaches the device
    await asyncio.sleep(0.3)
    assert (await asyncio.to_thread(device.read, 16)) == b"cmd"
    await asyncio.to_thread(device.write, b"resp"); await asyncio.to_thread(device.flush)
    data, _ = await asyncio.to_thread(reply.recvfrom, 64)  # ...the output goes to the reply address
    assert data == b"resp", data
    try:
        stray = other.recvfrom(64)
    except socket.timeout:
        stray = None
    assert stray is None, f"the sender got the device's output: {stray}"
    assert [c["peer"] for c in sup.status(m.id)["clients"]] == [f"127.0.0.1:{rport}"]
    print("UDP reply address: output to the configured address, not to the sender  OK")
    reply.close(); other.close(); device.close(); await sup.stop_all()
    p.terminate(); await p.wait()


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
    await asyncio.to_thread(ext_b.write, b"B2A!"); await asyncio.to_thread(ext_b.flush)
    await asyncio.sleep(0.3)
    assert (await asyncio.to_thread(ext_a.read, 16)) == b"B2A!"
    # port A is "the device": A -> B counts as in, B -> A as out; both ports are in use
    st = sup.status(m.id)
    assert (st["bytes_in"], st["bytes_out"]) == (3, 4), st
    assert {a1, b1} <= sup.devices_in_use(), sup.devices_in_use()
    print("serial<->serial bridge: bidirectional, counted per direction  OK")
    ext_a.close(); ext_b.close(); await sup.stop_all()
    pa.terminate(); pb.terminate(); await pa.wait(); await pb.wait()


async def main():
    await test_tcp_client()
    await test_udp()
    await test_udp_flood()
    await test_udp_reply_address()
    await test_serialbridge()
    print("\nPASS: TCP-client + UDP + serial-bridge transports")


if __name__ == "__main__":
    asyncio.run(main())
