"""REST API token roles: a 'viewer' token is read-only (Phase 2.3).

The API bearer token now carries a role; viewer tokens may only GET, while
operator/admin tokens may also create/modify. Spawns the real server. Run:
    python3 tests/test_api_token_role.py
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
UI = 8097
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


def api(method, path, token, body=None, expect=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        code = urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        code = e.code
    if expect is not None:
        assert code == expect, f"{method} {path} -> {code} (wanted {expect})"
    return code


def mapping(port, serial):
    return {"name": f"m{port}", "kind": "net", "serial": {"port": serial, "baudrate": 9600},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port}}


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_tok_")
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

        def post_form(path, data):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", csrf())
            return op.open(req, timeout=10)

        op.open(BASE + "/setup")
        post_form("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})

        def gen_token(role):
            body = post_form("/settings/api-token", {"api_token_role": role, "_csrf": csrf()}).read().decode()
            return re.search(r"s2n_[A-Za-z0-9_\-]+", body).group(0)

        # viewer token: reads OK, writes forbidden
        vtok = gen_token("viewer")
        api("GET", "/api/v1/mappings", vtok, expect=200)
        api("GET", "/api/v1/status", vtok, expect=200)
        api("POST", "/api/v1/mappings", vtok, body=mapping(free_port(), "/dev/ttyV"), expect=403)
        print("viewer token: GET allowed, POST -> 403 (read-only)  OK")

        # operator token: writes allowed
        otok = gen_token("operator")
        api("GET", "/api/v1/mappings", otok, expect=200)
        api("POST", "/api/v1/mappings", otok, body=mapping(free_port(), "/dev/ttyO"), expect=201)
        print("operator token: GET + POST allowed (read-write)  OK")

        print("\nPASS: REST API token roles")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
