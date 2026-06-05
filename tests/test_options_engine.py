"""Engine option tests: connect banner, idle timeout, closeon, and reconnect.
Run: python3 tests/test_options_engine.py
"""
import asyncio
import os
import re
import sys
from contextlib import suppress

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


async def start(dev, port, network=None, options=None, name="m"):
    sup = Supervisor(logger=lambda s: None)
    m = MappingConfig.from_dict({
        "name": name, "enabled": True,
        "serial": {"port": dev, "baudrate": 115200},
        "network": dict(network or {}, protocol="raw", bind_ip="127.0.0.1", port=port),
        "options": options or {},
    })
    ok, msg = await sup.apply_mapping(m)
    assert ok, msg
    await asyncio.sleep(0.4)
    return sup, m


async def test_banner():
    p, a, b = await socat()
    sup, m = await start(a, 47301, options={"banner": "Hi \\N \\p\\r\\n"}, name="bantest")
    r, w = await asyncio.open_connection("127.0.0.1", 47301)
    data = await asyncio.wait_for(r.read(64), timeout=2)
    assert data == b"Hi bantest 47301\r\n", data
    print("banner: substitutions + sent on connect  OK")
    w.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_idle_timeout():
    p, a, b = await socat()
    sup, m = await start(a, 47302, options={"idle_timeout_s": 1})
    r, w = await asyncio.open_connection("127.0.0.1", 47302)
    # no traffic -> should be disconnected within a few seconds
    data = await asyncio.wait_for(r.read(64), timeout=5)
    assert data == b"", f"expected EOF (idle disconnect), got {data!r}"
    print("idle timeout: idle client disconnected  OK")
    w.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_closeon():
    import serial
    p, a, b = await socat()
    sup, m = await start(a, 47303, options={"closeon": "BYE"})
    dev = serial.Serial(b, 115200, timeout=0)
    r, w = await asyncio.open_connection("127.0.0.1", 47303)
    await asyncio.sleep(0.2)
    await asyncio.to_thread(dev.write, b"hello BYE bye")
    await asyncio.to_thread(dev.flush)
    # client should receive the data then get EOF (closed by closeon)
    got = bytearray()
    try:
        while True:
            chunk = await asyncio.wait_for(r.read(64), timeout=3)
            if not chunk:
                break
            got += chunk
    except asyncio.TimeoutError:
        pass
    assert b"BYE" in got, got
    assert (sup.status(m.id) or {})["client_count"] == 0, "client should be kicked by closeon"
    print("closeon: clients closed when device emits the string  OK")
    w.close(); dev.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_reconnect():
    p, a, b = await socat()
    sup, m = await start(a, 47304)
    assert (sup.status(m.id) or {})["state"] == "running"
    # device disappears -> serial read hits EOF -> supervised reconnect kicks in
    p.terminate(); await p.wait()
    await asyncio.sleep(2.0)
    st = sup.status(m.id)
    assert st["state"] in ("reconnecting", "device-missing"), st
    assert st["reconnects"] >= 1, st
    print(f"reconnect: device loss -> state={st['state']} reconnects={st['reconnects']}  OK")
    await sup.stop_all()


async def main():
    await test_banner()
    await test_idle_timeout()
    await test_closeon()
    await test_reconnect()
    print("\nPASS: banner + idle-timeout + closeon + reconnect")


if __name__ == "__main__":
    asyncio.run(main())
