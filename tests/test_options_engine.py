"""Engine option tests: connect banner, idle timeout, closeon, and reconnect.
Run: python3 tests/test_options_engine.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import MappingConfig
from ser2netweb.engine.supervisor import Supervisor
from _server import free_port
from _socat import pty_pair as socat


async def start(dev, port, network=None, options=None, name="m", trace_dir=None):
    sup = Supervisor(logger=lambda s: None, trace_dir=trace_dir)
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
    sup, m = await start(a, port := free_port(), options={"banner": "Hi \\N \\p\\r\\n"}, name="bantest")
    r, w = await asyncio.open_connection("127.0.0.1", port)
    data = await asyncio.wait_for(r.read(64), timeout=2)
    assert data == f"Hi bantest {port}\r\n".encode(), data
    print("banner: substitutions + sent on connect  OK")
    w.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_idle_timeout():
    p, a, b = await socat()
    sup, m = await start(a, port := free_port(), options={"idle_timeout_s": 1})
    r, w = await asyncio.open_connection("127.0.0.1", port)
    # no traffic -> should be disconnected within a few seconds
    data = await asyncio.wait_for(r.read(64), timeout=5)
    assert data == b"", f"expected EOF (idle disconnect), got {data!r}"
    print("idle timeout: idle client disconnected  OK")
    w.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_closeon():
    import serial
    p, a, b = await socat()
    sup, m = await start(a, port := free_port(), options={"closeon": "BYE"})
    dev = serial.Serial(b, 115200, timeout=0)
    r, w = await asyncio.open_connection("127.0.0.1", port)
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
    # the client sees the close as the runner closes it; the runner forgets it right after
    for _ in range(100):
        if (sup.status(m.id) or {})["client_count"] == 0:
            break
        await asyncio.sleep(0.05)
    assert (sup.status(m.id) or {})["client_count"] == 0, "client should be kicked by closeon"
    print("closeon: clients closed when device emits the string  OK")
    w.close(); dev.close(); await sup.stop_all(); p.terminate(); await p.wait()


async def test_trace():
    import serial, tempfile
    p, a, b = await socat()
    tdir = tempfile.mkdtemp(prefix="trace_")
    sup, m = await start(a, port := free_port(), options={"trace_both": "trace.log", "trace_hexdump": True},
                         trace_dir=tdir)
    dev = serial.Serial(b, 115200, timeout=0)
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0.2)
    w.write(b"hello"); await w.drain()
    await asyncio.sleep(0.2)
    await asyncio.to_thread(dev.read, 16)
    await asyncio.to_thread(dev.write, b"world"); await asyncio.to_thread(dev.flush)
    await asyncio.sleep(0.3)
    await asyncio.wait_for(r.read(16), 2)
    w.close(); dev.close()
    await sup.stop_all()
    path = os.path.join(tdir, f"{m.id}-trace.log")
    content = open(path).read()
    os.remove(path)
    os.rmdir(tdir)
    assert "net>ser" in content and "ser>net" in content, content
    assert "68 65 6c 6c 6f" in content, content  # 'hello' hex
    print("trace: hex dump captured both directions  OK")
    p.terminate(); await p.wait()


async def test_reconnect():
    p, a, b = await socat()
    sup, m = await start(a, port := free_port())
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
    await test_trace()
    await test_reconnect()
    print("\nPASS: banner + idle-timeout + closeon + trace + reconnect")


if __name__ == "__main__":
    asyncio.run(main())
