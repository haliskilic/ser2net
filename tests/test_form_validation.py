"""Invalid mapping saves must return a renderable error fragment, not a dead end (H6).

htmx does not swap non-2xx responses by default, so a 400 from the save endpoint
was invisible in the UI (Save appeared to do nothing). The client-side fix lets
400/422 swap; this test guards the server contract the fix relies on: an invalid
save returns status 400 AND a swappable form fragment containing the error banner
and the form itself (so the panel re-renders with the message visible).

Also exercises the setup + CSRF flow. Spawns the real server (no httpx/TestClient
dependency), like the other web tests. Run: python3 tests/test_form_validation.py
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

from _server import free_port, server_log, show_log_on_failure

ROOT = os.path.join(os.path.dirname(__file__), "..")
UI = free_port()
BASE = f"http://127.0.0.1:{UI}"

from _server import ui_cookie


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_form_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    out = server_log(tmp)  # the server's output: a file, never an unread pipe
    srv = subprocess.Popen([sys.executable, "ser2netweb.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=out, stderr=subprocess.STDOUT)
    out.close()  # (the server has its own copy)
    try:
        assert wait_port(UI), "server did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next((c.value for c in jar if c.name == ui_cookie("ser2net_csrf", UI)), "")

        def post(path, data):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", csrf())
            try:
                return op.open(req, timeout=10)
            except urllib.error.HTTPError as e:
                return e

        op.open(BASE + "/setup")
        post("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})

        # invalid mapping: a name but no serial device -> ConfigError at validate()
        r = post("/api/mappings/save", {
            "name": "Bad", "kind": "net", "enabled": "on",
            "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(free_port()),
            "serial_port": "",  # <-- missing required device
        })
        status = getattr(r, "code", getattr(r, "status", None))
        body = r.read().decode("utf-8", "replace")

        assert status == 400, f"expected 400 for invalid mapping, got {status}"
        assert "Serial port is required" in body, "validation message missing from fragment"
        assert "banner error" in body, "error banner markup missing (nothing for the user to see)"
        assert "<form" in body and 'name="name"' in body, "response is not the swappable form fragment"
        print("invalid save -> 400 with a swappable form fragment + visible error  OK")

        # the Modbus points field is raw JSON: a value of the wrong type is named, not a 500
        gateway = {"name": "GW", "kind": "net", "network_mode": "server",
                   "network_protocol": "modbus", "network_bind_ip": "127.0.0.1",
                   "network_port": str(free_port()), "serial_port": "/dev/ttyUSB-gw",
                   "serial_baudrate": "9600"}
        for points, needle in (
                ('[{"name":"t","unit":"one","fn":3,"address":0,"dtype":"uint16"}]',
                 "modbus_poll.points[0].unit: &#34;one&#34; is not a whole number."),
                ('[1]', "Modbus poll point #1 is not an object."),  # (the form's JSON check)
                ('[{"name":null,"unit":1,"fn":3,"address":0,"dtype":"uint16"}]',
                 "Each Modbus poll point needs a name.")):
            r = post("/api/mappings/save", dict(gateway, modbus_points=points))
            status = getattr(r, "code", getattr(r, "status", None))
            body = r.read().decode("utf-8", "replace")
            assert status == 400 and needle in body, (status, points, body[:600])
        print("Modbus points of the wrong type -> 400 naming the point's setting  OK")

        # sanity: a valid mapping saves (200 + refresh trigger), proving the path works
        r2 = post("/api/mappings/save", {
            "name": "Good", "kind": "net", "enabled": "",
            "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(free_port()),
            "serial_port": "/dev/ttyUSB-test", "serial_baudrate": "9600",
        })
        s2 = getattr(r2, "code", getattr(r2, "status", 200))
        assert s2 == 200, f"valid mapping should save (200), got {s2}"
        assert r2.headers.get("HX-Trigger") == "refreshMappings"
        print("valid save -> 200 + HX-Trigger refresh  OK")

        print("\nPASS: form validation error visibility (H6)")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        show_log_on_failure(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
