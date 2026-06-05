"""End-to-end web test: boot the real server, drive setup/login/mapping CRUD
over HTTP, and verify the resulting bridge actually forwards bytes.

Run: python3 tests/test_web_e2e.py
"""
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
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))

UI_PORT = 8099
MAP_PORT = 45111


def wait_port(host, port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def csrf(self):
        for c in self.jar:
            if c.name == "ser2net_csrf":
                return c.value
        return ""

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        return self.opener.open(req, timeout=10)

    def post(self, path, data, follow=True):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        # Mirror the real browser: htmx (/api/*) sends the header; plain HTML form
        # posts (login/setup/settings/logout) send only the _csrf body field.
        if path.startswith("/api/"):
            req.add_header("X-CSRF-Token", self.csrf())
        if not follow:
            req.add_header("X-No-Redirect", "1")
        try:
            return self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            return e


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_e2e_")
    cfg_path = os.path.join(tmp, "config.json")
    # pre-seed admin_ui so the console picker is skipped (config already exists)
    with open(cfg_path, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI_PORT}}, fh)

    server = subprocess.Popen(
        [sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg_path],
        cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    socat = None
    try:
        assert wait_port("127.0.0.1", UI_PORT), "server did not start"
        c = Client(f"http://127.0.0.1:{UI_PORT}")

        # 1) unauthenticated root -> setup
        r = c.get("/")
        assert r.geturl().endswith("/setup"), r.geturl()
        c.get("/setup")  # ensure csrf cookie issued
        assert c.csrf(), "no csrf cookie"
        print("setup redirect + csrf cookie OK")

        # 2) complete setup (sets password + auto-login)
        r = c.post("/setup", {"password": "supersecret1", "password2": "supersecret1",
                              "_csrf": c.csrf()})
        assert r.status == 200 and r.geturl().endswith("/"), (r.status, r.geturl())
        print("setup POST -> dashboard OK")

        # 3) dashboard + ports API now authorized
        assert b"mappings" in c.get("/").read()
        ports = json.load(c.get("/api/ports.json"))
        print(f"ports API OK ({len(ports)} ports)")

        # 4) create a mapping pointing at a socat PTY
        socat = subprocess.Popen(["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"],
                                 stderr=subprocess.PIPE)
        devs = []
        while len(devs) < 2:
            line = socat.stderr.readline()
            m = re.search(rb"PTY is (\S+)", line)
            if m:
                devs.append(m.group(1).decode())
        dev_a, dev_b = devs
        print(f"socat PTYs: {dev_a} / {dev_b}")

        r = c.post("/api/mappings/save", {
            "_csrf": c.csrf(), "name": "e2e-map", "enabled": "on",
            "serial_port": dev_a, "serial_baudrate": "115200", "serial_bytesize": "8",
            "serial_parity": "N", "serial_stopbits": "1", "serial_flowcontrol": "none",
            "serial_rts_on_open": "keep", "serial_dtr_on_open": "keep", "serial_exclusive": "on",
            "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
            "network_port": str(MAP_PORT), "network_max_connections": "1",
            "opt_idle_timeout_s": "0",
        })
        assert r.status == 200, (r.status, r.read()[:300])
        assert r.getheader("HX-Trigger") == "refreshMappings"
        print("mapping create OK (HX-Trigger present)")

        # 5) status fragment lists it
        body = c.get("/api/status").read().decode()
        assert "e2e-map" in body and "running" in body, body[:400]
        print("status fragment shows running mapping")

        # 6) verify the bridge actually forwards bytes
        time.sleep(0.5)
        import serial
        device = serial.Serial(dev_b, baudrate=115200, timeout=1)
        cs = socket.create_connection(("127.0.0.1", MAP_PORT), timeout=3)
        cs.sendall(b"web-e2e-hello")
        time.sleep(0.3)
        got = device.read(64)
        assert got == b"web-e2e-hello", got
        device.write(b"reply-back"); device.flush()
        time.sleep(0.3)
        back = cs.recv(64)
        assert back == b"reply-back", back

        # the connected client's ip:port must be visible in the UI status
        peer = f"127.0.0.1:{cs.getsockname()[1]}"
        sbody = c.get("/api/status").read().decode()
        assert peer in sbody, f"expected client {peer} in status; got: {sbody[:600]}"
        print(f"connected client {peer} shown in UI OK")

        cs.close(); device.close()
        print("bridge forwards bytes bidirectionally OK")

        # 7) stop the mapping via API
        cfg = json.load(open(cfg_path))
        mid = cfg["mappings"][0]["id"]
        r = c.post(f"/api/mappings/{mid}/stop", {"_csrf": c.csrf()})
        assert r.status == 200
        time.sleep(0.3)
        body = c.get("/api/status").read().decode()
        assert "disabled" in body, body[:400]
        print("mapping stop OK")

        print("\nPASS: web end-to-end (setup -> auth -> mapping CRUD -> live bridge)")
    finally:
        if socat:
            socat.terminate()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
