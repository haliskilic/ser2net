"""End-to-end RFC2217 test: a real pyserial rfc2217:// client against our server.

Verifies (a) bidirectional data and (b) that a client-requested baud rate is
applied to the server-side serial.Serial via PortManager. Run:
    python3 tests/test_rfc2217.py
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

PORT = 45021


async def start_socat():
    proc = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    devs = []
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
        "name": "test-rfc2217",
        "enabled": True,
        "serial": {"port": dev_a, "baudrate": 9600},
        "network": {"protocol": "rfc2217", "bind_ip": "127.0.0.1", "port": PORT},
    })
    ok, msg = await sup.apply_mapping(mapping)
    assert ok, f"runner failed: {msg}"
    await asyncio.sleep(0.4)

    import serial
    from app.engine.bridge import MappingRunner  # noqa

    # device end
    device = serial.Serial(dev_b, baudrate=9600, timeout=0)

    # rfc2217 client requesting a DIFFERENT baud (19200) -> server must apply it
    def open_client():
        return serial.serial_for_url(f"rfc2217://127.0.0.1:{PORT}", baudrate=19200, timeout=1)

    client = await asyncio.to_thread(open_client)
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

    await asyncio.to_thread(client.close)
    device.close()
    await sup.stop_all()
    proc.terminate()
    await proc.wait()
    print("\nPASS: RFC2217 data + live baud change work")


if __name__ == "__main__":
    asyncio.run(main())
