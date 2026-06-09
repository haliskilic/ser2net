"""Multi-user RBAC: roles, role enforcement, user management (Phase 2).

Spawns the real server, creates an operator and a viewer as admin, then logs in
as each role and checks the permission boundaries (viewer read-only, operator can
manage mappings but not users/TLS, admin can do everything). Cross-platform.

Run: python3 tests/test_rbac.py
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
UI = 8096
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


class Session:
    """A cookie-jar-backed client for one user."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def csrf(self):
        return next((c.value for c in self.jar if c.name == "ser2net_csrf"), "")

    def get(self, path):
        try:
            return self.op.open(BASE + path, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    def post(self, path, data, api=False):
        req = urllib.request.Request(BASE + path,
                                     data=urllib.parse.urlencode(data).encode(), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("X-CSRF-Token", self.csrf())  # for /api/ middleware CSRF
        try:
            return self.op.open(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    def prime_csrf(self, path="/login"):
        try:
            self.op.open(BASE + path, timeout=10)
        except urllib.error.HTTPError:
            pass

    def login(self, username, password):
        self.prime_csrf("/login")
        return self.post("/login", {"username": username, "password": password, "_csrf": self.csrf()})


def mapping_fields(name, port, serial="/dev/ttyTEST"):
    # distinct serial device per mapping (two enabled mappings can't share a port)
    return {"name": name, "kind": "net", "enabled": "on",
            "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(port),
            "serial_port": serial, "serial_baudrate": "9600"}


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_rbac_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert wait_port(UI), "server did not start"

        # ---- first-run: create the admin ----
        admin = Session()
        admin.prime_csrf("/setup")
        admin.post("/setup", {"username": "admin", "password": "adminpass123",
                              "password2": "adminpass123", "_csrf": admin.csrf()})
        assert admin.get("/") == 200, "admin not signed in after setup"
        print("setup created admin and signed in  OK")

        # ---- admin creates operator + viewer ----
        for uname, role in (("op1", "operator"), ("view1", "viewer")):
            code = admin.post("/settings/users", {"username": uname, "role": role,
                                                  "password": "userpass123", "password2": "userpass123",
                                                  "_csrf": admin.csrf()})
            assert code == 200, f"admin create {uname} -> {code}"
        print("admin created an operator and a viewer  OK")

        # admin creates a mapping (operator-level action, admin is above it)
        port = free_port()
        assert admin.post("/api/mappings/save", mapping_fields("m-admin", port, "/dev/ttyA")) == 200
        print("admin created a mapping via the UI endpoint  OK")

        # ---- viewer: read-only ----
        viewer = Session()
        assert viewer.login("view1", "userpass123") == 200
        assert viewer.get("/") == 200, "viewer cannot view dashboard"
        assert viewer.post("/api/mappings/save", mapping_fields("nope", free_port())) == 403, \
            "viewer must not create mappings"
        assert viewer.post("/settings/users", {"username": "x", "role": "viewer",
                                               "password": "userpass123", "password2": "userpass123",
                                               "_csrf": viewer.csrf()}) == 403, \
            "viewer must not manage users"
        print("viewer: can read, blocked from mapping writes and user management (403)  OK")

        # ---- operator: mappings yes, admin functions no ----
        operator = Session()
        assert operator.login("op1", "userpass123") == 200
        oport = free_port()
        assert operator.post("/api/mappings/save", mapping_fields("m-op", oport, "/dev/ttyB")) == 200, \
            "operator must be able to create mappings"
        assert operator.post("/settings/users", {"username": "y", "role": "viewer",
                                                 "password": "userpass123", "password2": "userpass123",
                                                 "_csrf": operator.csrf()}) == 403, \
            "operator must not manage users"
        assert operator.post("/settings/tls", {"tls_cert": "/x", "tls_key": "/y",
                                               "_csrf": operator.csrf()}) == 403, \
            "operator must not change admin TLS"
        print("operator: manages mappings, blocked from users + admin TLS (403)  OK")

        # ---- admin: manage users (change role, delete) ----
        assert admin.post("/settings/users/view1/role", {"role": "operator", "_csrf": admin.csrf()}) == 200
        assert admin.post("/settings/users/op1/delete", {"_csrf": admin.csrf()}) == 200
        # last-admin protection: admin cannot delete itself as the only admin
        assert admin.post("/settings/users/admin/delete", {"_csrf": admin.csrf()}) == 200  # returns settings page w/ error
        assert admin.get("/") == 200, "admin should still be signed in (self-delete blocked)"
        print("admin: role change + delete work; last-admin self-delete is blocked  OK")

        print("\nPASS: multi-user RBAC (Phase 2)")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
