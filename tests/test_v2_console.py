"""v2.1a browser console: serial traffic over WebSocket (read + interactive write).
Run: python3 tests/test_v2_console.py
"""
import asyncio
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
                got = await asyncio.wait_for(ws.recv(), timeout=3)
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

        asyncio.run(ws_test())
        assert asyncio.run(ws_noauth()), "unauthenticated WS should be rejected"
        print("console: unauthenticated WS rejected  OK")

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
