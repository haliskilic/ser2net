"""v2.1a browser console: serial traffic over WebSocket (read + interactive write).

Also: the hex view's traffic feed (both directions, timestamped) for an admin; a viewer
gets a read-only console and a hex view of what the device sends only — never what a
client sends to the device.

Run: python3 tests/test_v2_console.py
"""
import asyncio
import contextlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
UI = 8085


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def start_socat():
    p = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"],
                         stderr=subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        m = re.search(rb"PTY is (\S+)", p.stderr.readline())
        if m:
            devs.append(m.group(1).decode())
    return p, devs[0], devs[1]


def main():
    import serial
    import websockets  # client (installed in lib)

    tmp = tempfile.mkdtemp(prefix="ser2net_con_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    socat, dev_a, dev_b = start_socat()
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert wait_port(UI), "server did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next(c.value for c in jar if c.name == "ser2net_csrf")

        def post(path, data, header=False):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (UI, path),
                                         data=urllib.parse.urlencode(data).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            if header:
                req.add_header("X-CSRF-Token", csrf())
            return op.open(req, timeout=10)

        op.open("http://127.0.0.1:%d/setup" % UI)
        post("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})
        post("/api/mappings/save", {
            "_csrf": csrf(), "name": "CON", "enabled": "on", "kind": "net",
            "serial_port": dev_a, "serial_baudrate": "115200", "serial_bytesize": "8",
            "serial_parity": "N", "serial_stopbits": "1", "serial_flowcontrol": "none",
            "network_mode": "server", "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
            "network_port": "47350"}, header=True)
        mid = json.load(open(cfg))["mappings"][0]["id"]
        session = next(c.value for c in jar if c.name == "ser2net_session")
        time.sleep(0.5)
        device = serial.Serial(dev_b, 115200, timeout=1)

        # WS auth via the session cookie (BaseHTTPMiddleware doesn't cover WS)
        async def ws_test():
            uri = f"ws://127.0.0.1:{UI}/api/mappings/{mid}/console"
            async with websockets.connect(uri, additional_headers={"Cookie": f"ser2net_session={session}"}) as ws:
                # device -> monitor (browser receives serial traffic)
                await asyncio.to_thread(device.write, b"hello-mon"); await asyncio.to_thread(device.flush)
                # generous timeout: under CI load the serial->monitor->WS hop can be
                # slow to warm up; we return as soon as data arrives, so this only
                # raises the ceiling (reduces flakiness), it doesn't slow the happy path.
                got = await asyncio.wait_for(ws.recv(), timeout=10)
                got = got if isinstance(got, (bytes, bytearray)) else got.encode()
                assert b"hello-mon" in got, got
                # monitor -> device (interactive write)
                await ws.send(b"to-device")
                await asyncio.sleep(0.3)
                back = await asyncio.to_thread(device.read, 32)
                assert back == b"to-device", back
            print("console: serial->browser + browser->serial over WS  OK")

        # WS without auth cookie must be rejected
        async def ws_noauth():
            uri = f"ws://127.0.0.1:{UI}/api/mappings/{mid}/console"
            try:
                async with websockets.connect(uri) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=2)
                return False  # should not succeed
            except Exception:
                return True

        # hex view: both directions, timestamped frames [dir u8][time f64][bytes]
        async def traffic_test():
            import struct
            uri = f"ws://127.0.0.1:{UI}/api/mappings/{mid}/traffic"
            async with websockets.connect(uri, additional_headers={"Cookie": f"ser2net_session={session}"}) as ws:
                _r, w = await asyncio.open_connection("127.0.0.1", 47350)
                await asyncio.sleep(0.3)
                w.write(b"from-net"); await w.drain()
                await asyncio.to_thread(device.write, b"from-dev"); await asyncio.to_thread(device.flush)
                seen = {}
                t0 = time.time()
                while len(seen) < 2 and time.time() - t0 < 10:
                    frame = await asyncio.wait_for(ws.recv(), timeout=10)
                    d, ts = struct.unpack(">Bd", frame[:9])
                    assert abs(ts - time.time()) < 30, ts
                    seen.setdefault(d, b"")
                    seen[d] += frame[9:]
                assert b"from-net" in seen.get(1, b""), seen   # to the device
                assert b"from-dev" in seen.get(0, b""), seen   # from the device
                w.close()
            print("hex view: both directions over the traffic WS, timestamped  OK")
            try:
                async with websockets.connect(uri) as ws:
                    await asyncio.wait_for(ws.recv(), timeout=2)
                raise AssertionError("unauthenticated traffic WS accepted")
            except AssertionError:
                raise
            except Exception:
                pass

        asyncio.run(ws_test())
        assert asyncio.run(ws_noauth()), "unauthenticated WS should be rejected"
        print("console: unauthenticated WS rejected  OK")
        asyncio.run(traffic_test())

        # ---- a viewer: read-only console, hex view without what is sent to the device ----
        post("/settings/users", {"_csrf": csrf(), "username": "view1", "role": "viewer",
                                 "password": "viewerpass1", "password2": "viewerpass1"})
        vjar = http.cookiejar.CookieJar()
        vop = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(vjar))
        vop.open("http://127.0.0.1:%d/login" % UI)
        vop.open(urllib.request.Request(
            "http://127.0.0.1:%d/login" % UI, method="POST", data=urllib.parse.urlencode({
                "username": "view1", "password": "viewerpass1",
                "_csrf": next(c.value for c in vjar if c.name == "ser2net_csrf")}).encode()))
        vsession = next(c.value for c in vjar if c.name == "ser2net_session")

        def console_view(opener):
            return opener.open("http://127.0.0.1:%d/api/mappings/%s/console-view" % (UI, mid),
                               timeout=10).read().decode()

        page = console_view(vop)
        assert 'data-interactive="0"' in page and "what the device sends, with timestamps" in page
        assert "type to send to the device" not in page and "from / to the device" not in page
        page = console_view(op)
        assert 'data-interactive="1"' in page and "from / to the device" in page

        async def viewer_test():
            import struct
            hdr = {"Cookie": f"ser2net_session={vsession}"}
            base = f"ws://127.0.0.1:{UI}/api/mappings/{mid}"
            async with websockets.connect(base + "/console", additional_headers=hdr) as ws:
                await ws.send(b"viewer-typed")  # a viewer's console is read-only: dropped
                await asyncio.sleep(0.5)
            async with websockets.connect(base + "/traffic", additional_headers=hdr) as ws:
                _r, w = await asyncio.open_connection("127.0.0.1", 47350)
                await asyncio.sleep(0.3)
                w.write(b"typed-secret"); await w.drain()
                await asyncio.sleep(0.5)  # reaches the device well before its answer
                await asyncio.to_thread(device.write, b"dev-reply"); await asyncio.to_thread(device.flush)
                got, t0 = b"", time.time()
                while b"dev-reply" not in got and time.time() - t0 < 10:
                    frame = await asyncio.wait_for(ws.recv(), timeout=10)
                    d, _ts = struct.unpack(">Bd", frame[:9])
                    assert d == 0, f"a viewer's hex view got bytes sent to the device: {frame!r}"
                    got += frame[9:]
                assert b"dev-reply" in got and b"typed-secret" not in got, got
                with contextlib.suppress(asyncio.TimeoutError):
                    while True:  # nothing else may follow either
                        frame = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        assert frame[0] == 0, f"a viewer's hex view got bytes sent to the device: {frame!r}"
                w.close()
            back = await asyncio.to_thread(device.read, 256)
            assert b"typed-secret" in back and b"viewer-typed" not in back, back

        asyncio.run(viewer_test())
        print("viewer: read-only console, hex view shows only what the device sends  OK")

        device.close()
        print("\nPASS: WebSocket console (auth + bidirectional)")
    finally:
        socat.terminate()
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
