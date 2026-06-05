"""End-to-end raw-mode bridge test over a socat PTY pair (no hardware needed).

Starts socat to create two linked PTYs, points a MappingRunner at one end, opens
the other end as the 'device', connects a TCP client, and asserts bidirectional
byte flow. Run: python3 tests/test_bridge_raw.py
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

PORT = 45011


async def start_socat():
    proc = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    devs = []
    # socat prints "PTY is /dev/pts/N" twice on stderr
    while len(devs) < 2:
        line = await proc.stderr.readline()
        if not line:
            raise RuntimeError("socat did not report PTYs")
        m = re.search(rb"PTY is (\S+)", line)
        if m:
            devs.append(m.group(1).decode())
    return proc, devs[0], devs[1]


async def main():
    logs = []
    proc, dev_a, dev_b = await start_socat()
    print(f"socat PTYs: runner={dev_a} device={dev_b}")

    sup = Supervisor(logger=lambda m: logs.append(m))
    mapping = MappingConfig.from_dict({
        "name": "test-raw",
        "enabled": True,
        "serial": {"port": dev_a, "baudrate": 115200},
        "network": {"protocol": "raw", "bind_ip": "127.0.0.1", "port": PORT},
    })

    ok, msg = await sup.apply_mapping(mapping)
    assert ok, f"runner failed to start: {msg}"
    await asyncio.sleep(0.4)  # let serial open
    print("status:", sup.status(mapping.id))

    # open the 'device' end with plain pyserial
    import serial
    device = serial.Serial(dev_b, baudrate=115200, timeout=0)

    # connect a TCP client to the bridge
    creader, cwriter = await asyncio.open_connection("127.0.0.1", PORT)
    await asyncio.sleep(0.2)

    # 1) client -> serial
    cwriter.write(b"hello-serial")
    await cwriter.drain()
    await asyncio.sleep(0.3)
    got = await asyncio.to_thread(device.read, 64)
    print("device received:", got)
    assert got == b"hello-serial", got

    # 2) serial -> client
    await asyncio.to_thread(device.write, b"hello-network")
    await asyncio.to_thread(device.flush)
    got2 = await asyncio.wait_for(creader.read(64), timeout=2.0)
    print("client received:", got2)
    assert got2 == b"hello-network", got2

    # status counters
    st = sup.status(mapping.id)
    print("final status:", st)
    assert st["state"] == "running"
    assert st["client_count"] == 1
    assert st["bytes_out"] >= len(b"hello-serial")
    assert st["bytes_in"] >= len(b"hello-network")

    # cleanup
    cwriter.close()
    device.close()
    await sup.stop_all()
    proc.terminate()
    await proc.wait()
    print("\nPASS: bidirectional raw bridge works")


if __name__ == "__main__":
    asyncio.run(main())
