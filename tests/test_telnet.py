"""Telnet-mode test: BINARY negotiation + IAC escaping in both directions.

Run: python3 tests/test_telnet.py
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

PORT = 45051
IAC, DO, WONT, WILL, BINARY, SGA = 255, 253, 252, 251, 0, 3


async def start_socat():
    proc = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        line = await proc.stderr.readline()
        m = re.search(rb"PTY is (\S+)", line)
        if m:
            devs.append(m.group(1).decode())
    return proc, devs[0], devs[1]


async def main():
    proc, dev_a, dev_b = await start_socat()
    sup = Supervisor(logger=lambda m: None)
    mapping = MappingConfig.from_dict({
        "name": "test-telnet", "enabled": True,
        "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"protocol": "telnet", "bind_ip": "127.0.0.1", "port": PORT}})
    ok, msg = await sup.apply_mapping(mapping)
    assert ok, msg
    await asyncio.sleep(0.4)

    import serial
    device = serial.Serial(dev_b, baudrate=115200, timeout=0)

    r, w = await asyncio.open_connection("127.0.0.1", PORT)
    neg = await asyncio.wait_for(r.read(64), timeout=1.0)
    print("negotiation from server:", neg.hex())
    # expect WILL/DO BINARY and WILL/DO SGA
    assert bytes([IAC, WILL, BINARY]) in neg and bytes([IAC, DO, BINARY]) in neg, neg.hex()

    # client -> serial with an escaped IAC (ff ff -> single ff) and an unsupported DO
    w.write(b"ab" + bytes([IAC, IAC]) + b"cd" + bytes([IAC, DO, 24]))
    await w.drain()
    await asyncio.sleep(0.3)
    got = await asyncio.to_thread(device.read, 64)
    print("device received:", got)
    assert got == b"ab\xffcd", got  # IAC IAC collapsed to one 0xff, negotiation stripped

    # server should refuse unsupported option 24 with WONT 24
    reply = await asyncio.wait_for(r.read(64), timeout=1.0)
    print("server reply to DO 24:", reply.hex())
    assert bytes([IAC, WONT, 24]) in reply, reply.hex()

    # serial -> client: a raw 0xff must be doubled
    await asyncio.to_thread(device.write, b"x\xffy")
    await asyncio.to_thread(device.flush)
    out = await asyncio.wait_for(r.read(64), timeout=1.0)
    print("client received:", out.hex())
    assert out == b"x\xff\xffy", out.hex()

    w.close()
    device.close()
    await sup.stop_all()
    proc.terminate()
    await proc.wait()
    print("\nPASS: telnet negotiation + IAC escaping work")


if __name__ == "__main__":
    asyncio.run(main())
