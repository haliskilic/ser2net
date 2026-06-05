"""Web auth lifecycle: CSRF enforcement, login rate-limiting, and session
revocation on password change (pwd_version). Run: python3 tests/test_web_auth.py
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
UI = 8087
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
    tmp = tempfile.mkdtemp(prefix="ser2net_auth_")
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

        def session():
            return next((c.value for c in jar if c.name == "ser2net_session"), "")

        def post(path, data, header=True):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            if header:
                req.add_header("X-CSRF-Token", csrf())
            try:
                return op.open(req, timeout=10)
            except urllib.error.HTTPError as e:
                return e

        op.open(BASE + "/setup")
        post("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})
        old_session = session()
        assert old_session, "no session after setup"
        print("setup + session issued  OK")

        # 1) CSRF: an /api POST without the X-CSRF-Token header is rejected
        r = post("/api/mappings/save", {"name": "x"}, header=False)
        assert getattr(r, "code", getattr(r, "status", None)) == 403, r
        print("CSRF: /api POST without token -> 403  OK")

        # 2) session revocation: change password, then the OLD session must be invalid
        r = post("/settings/password",
                 {"current": "supersecret1", "password": "newsecret123",
                  "password2": "newsecret123", "_csrf": csrf()})
        assert getattr(r, "status", 200) == 200
        new_session = session()
        assert new_session and new_session != old_session, "session should be refreshed"
        # request with ONLY the old cookie -> redirected to /login
        req = urllib.request.Request(BASE + "/")
        req.add_header("Cookie", f"ser2net_session={old_session}")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.geturl().endswith("/login"), f"old session still valid: {resp.geturl()}"
        print("session revocation: old session invalid after password change  OK")

        # 3) login rate-limit: repeated wrong passwords -> 429
        jar2 = http.cookiejar.CookieJar()
        op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar2))
        op2.open(BASE + "/login")  # get a csrf cookie
        tok = next((c.value for c in jar2 if c.name == "ser2net_csrf"), "")
        codes = []
        for _ in range(11):
            req = urllib.request.Request(BASE + "/login",
                                         data=urllib.parse.urlencode(
                                             {"password": "wrong", "_csrf": tok}).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                codes.append(op2.open(req, timeout=10).status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        assert 429 in codes, f"expected a 429 after repeated failures, got {codes}"
        assert codes.count(401) >= 5, codes
        print(f"rate-limit: wrong logins -> {codes.count(401)}x401 then 429  OK")

        print("\nPASS: CSRF + session revocation + login rate-limit")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
