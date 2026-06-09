"""JSON REST API (/api/v1) with bearer-token auth (Phase 2).

Spawns the real server, completes setup, generates an API token via the settings
endpoint, then exercises the API: unauthenticated -> 401, token -> CRUD +
start/stop, health/openapi public. Cross-platform (no socat).

Run: python3 tests/test_rest_api.py
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
UI = 8095
BASE = f"http://127.0.0.1:{UI}"


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def api(method, path, token=None, body=None, expect=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None:
        assert code == expect, f"{method} {path} -> {code} (wanted {expect}): {payload[:200]}"
    try:
        return code, json.loads(payload) if payload else {}
    except ValueError:
        return code, {}


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_api_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert wait_port(UI), "server did not start"

        # ---- public endpoints need no token ----
        api("GET", "/api/v1/health", expect=200)
        code, spec = api("GET", "/api/v1/openapi.json", expect=200)
        assert spec.get("openapi", "").startswith("3."), spec
        print("public: /health + /openapi.json reachable without a token  OK")

        # ---- before setup the API is unavailable (no password yet) ----
        api("GET", "/api/v1/mappings", expect=503)
        print("pre-setup: /api/v1/mappings -> 503 setup_incomplete  OK")

        # ---- session flow: set password, generate an API token ----
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next((c.value for c in jar if c.name == "ser2net_csrf"), "")

        def post_form(path, data):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", csrf())
            return op.open(req, timeout=10)

        op.open(BASE + "/setup")
        post_form("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})

        # password set but no token yet -> API rejects with 401
        api("GET", "/api/v1/mappings", expect=401)
        print("post-setup, no token configured: /api/v1/mappings -> 401  OK")

        body = post_form("/settings/api-token", {"_csrf": csrf()}).read().decode()
        m = re.search(r"s2n_[A-Za-z0-9_\-]+", body)
        assert m, "generated token not found in settings page"
        token = m.group(0)
        print("token generated via settings and shown once  OK")

        # ---- bad token rejected, good token works ----
        api("GET", "/api/v1/mappings", token="s2n_wrong", expect=401)
        code, listing = api("GET", "/api/v1/mappings", token=token, expect=200)
        assert listing == {"mappings": []}, listing
        print("auth: bad token -> 401, valid token -> 200 (empty list)  OK")

        # ---- create ----
        port = free_port()
        new = {"name": "api-made", "kind": "net",
               "serial": {"port": "/dev/ttyTEST", "baudrate": 9600},
               "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port}}
        code, created = api("POST", "/api/v1/mappings", token=token, body=new, expect=201)
        mid = created["id"]
        assert created["name"] == "api-made" and "status" in created
        print("POST create -> 201 with server-assigned id + status  OK")

        # validation error -> 400
        api("POST", "/api/v1/mappings", token=token, body={"name": ""}, expect=400)
        print("POST invalid -> 400  OK")

        # ---- get / update / action ----
        api("GET", f"/api/v1/mappings/{mid}", token=token, expect=200)
        upd = dict(new, name="api-renamed")
        code, updated = api("PUT", f"/api/v1/mappings/{mid}", token=token, body=upd, expect=200)
        assert updated["name"] == "api-renamed"
        code, acted = api("POST", f"/api/v1/mappings/{mid}/stop", token=token, expect=200)
        assert acted["action"] == "stop"
        api("POST", f"/api/v1/mappings/{mid}/bogus", token=token, expect=400)
        print("GET/PUT/POST action work; unknown action -> 400  OK")

        # ---- status / ports ----
        code, st = api("GET", "/api/v1/status", token=token, expect=200)
        assert mid in st["mappings"], st
        api("GET", "/api/v1/ports", token=token, expect=200)
        print("status + ports endpoints  OK")

        # ---- delete ----
        api("DELETE", f"/api/v1/mappings/{mid}", token=token, expect=200)
        api("GET", f"/api/v1/mappings/{mid}", token=token, expect=404)
        print("DELETE removes the mapping (then 404)  OK")

        print("\nPASS: REST API /api/v1 (Phase 2)")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
