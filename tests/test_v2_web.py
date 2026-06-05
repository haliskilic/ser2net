"""v2.0 web features: metrics endpoint, mapping duplicate, config export/import.
Run: python3 tests/test_v2_web.py
"""
import http.cookiejar
import json
import os
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
UI = 8086
BASE = f"http://127.0.0.1:{UI}"


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_v2web_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert wait_port(UI), "server did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next((c.value for c in jar if c.name == "ser2net_csrf"), "")

        def post(path, data, header=True):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            if header:
                req.add_header("X-CSRF-Token", csrf())
            try:
                return op.open(req, timeout=10)
            except urllib.error.HTTPError as e:
                return e

        op.open(BASE + "/setup")
        post("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})

        # create a mapping
        post("/api/mappings/save", {
            "_csrf": csrf(), "name": "MX", "enabled": "on", "kind": "net",
            "serial_port": "/dev/ttyX", "serial_baudrate": "9600", "serial_bytesize": "8",
            "serial_parity": "N", "serial_stopbits": "1", "serial_flowcontrol": "none",
            "network_mode": "server", "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
            "network_port": "47301", "network_max_connections": "1"})
        cfgdoc = json.load(open(cfg))
        mid = cfgdoc["mappings"][0]["id"]

        # 1) metrics endpoint (auth-protected)
        body = op.open(BASE + "/metrics").read().decode()
        assert "ser2net_up 1" in body and 'mapping="MX"' in body, body[:300]
        print("metrics: /metrics exposes mapping gauges  OK")

        # 2) export mappings
        exp = op.open(BASE + "/settings/config/export").read().decode()
        doc = json.loads(exp)
        assert len(doc["mappings"]) == 1 and doc["mappings"][0]["name"] == "MX"
        print("export: mappings JSON downloaded (no secrets)  OK")
        assert "password_hash" not in exp and "secret_key" not in exp

        # 3) duplicate
        r = post(f"/api/mappings/{mid}/duplicate", {"_csrf": csrf()})
        assert r.status == 200
        names = [m["name"] for m in json.load(open(cfg))["mappings"]]
        assert "MX (copy)" in names, names
        # the copy must have bumped its port (no clash)
        ports = [m["network"]["port"] for m in json.load(open(cfg))["mappings"]]
        assert len(set(ports)) == len(ports), ports
        print("duplicate: copy created with bumped port  OK")

        # 4) import (multipart) — replace with a single different mapping
        new_doc = {"mappings": [{"name": "IMPORTED", "serial": {"port": "/dev/ttyZ"},
                                 "network": {"mode": "server", "port": 48888}}]}
        boundary = "----ser2nettest"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="_csrf"\r\n\r\n{csrf()}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="m.json"\r\n'
            f'Content-Type: application/json\r\n\r\n{json.dumps(new_doc)}\r\n',
            f'--{boundary}--\r\n',
        ]
        bodyb = "".join(parts).encode()
        req = urllib.request.Request(BASE + "/settings/config/import", data=bodyb, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        op.open(req, timeout=10)
        names = [m["name"] for m in json.load(open(cfg))["mappings"]]
        assert names == ["IMPORTED"], names
        print("import: mappings replaced from uploaded JSON  OK")

        print("\nPASS: metrics + duplicate + export + import")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
