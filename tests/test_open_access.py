"""Optional web-UI login (issue #24, item 5) + the mapping form's new connection fields.

Spawns the real server and checks, over HTTP / WebSocket:
  - login is required by default (no session -> /login),
  - an admin can switch it off in Settings; visitors without a session then act with
    the chosen role and RBAC still applies (viewer: read-only; operator: may edit),
  - the DNS-rebinding guard: open access only for an IP / localhost Host header —
    any other host name still has to sign in,
  - signing in still works on an open UI; the REST API still needs its token,
  - the edit form / mappings export (they carry the MQTT password) need operator+,
  - the console WebSocket follows the same rule, and switching login back on closes
    the door again — including consoles that are already open,
  - reverse-proxy host headers (X-Forwarded-Host / Forwarded) are checked too,
  - with a LAN cluster, anonymous visitors see only this node and can't act on peers,
  - a failed save re-renders the form without resetting unposted stored values,
  - the mapping form saves keepalive / connect timeout / max connection time and
    re-renders them on edit.

Cross-platform. Run: python3 tests/test_open_access.py
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface 303s to the test instead of following them


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                              _NoRedirect)

    def csrf(self):
        return next((c.value for c in self.jar if c.name == ui_cookie("ser2net_csrf", UI)), "")

    def request(self, path, data=None, host=None, headers=None):
        """(status, body, location) — redirects are not followed."""
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(BASE + path, data=body, method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", self.csrf())
        if host:
            req.add_header("Host", host)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            r = self.op.open(req, timeout=10)
            return r.status, r.read().decode("utf-8", "replace"), r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location", "")

    def get(self, path, host=None, headers=None):
        return self.request(path, host=host, headers=headers)

    def post(self, path, data):
        return self.request(path, data=data)

    def prime(self, path="/login"):
        self.get(path)


def console_ws_accepted(mid) -> bool:
    from websockets.sync.client import connect

    try:
        with connect(f"ws://127.0.0.1:{UI}/api/mappings/{mid}/console", open_timeout=5):
            return True
    except Exception:
        return False


def mapping_form(name, port, serial, **extra):
    return {"name": name, "kind": "net", "enabled": "on", "network_mode": "server",
            "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
            "network_port": str(port), "serial_port": serial, "serial_baudrate": "9600",
            **extra}


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_open_")
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    # The server logs to stdout: send it to a file, never an unread pipe — on Windows the
    # pipe buffer is ~4 KB and a full pipe blocks the server's event loop mid-test.
    server_log = open(os.path.join(tmp, "server.log"), "wb")
    srv = subprocess.Popen([sys.executable, "ser2netweb.py", "--no-bootstrap", "--config", cfg_path],
                           cwd=ROOT, stdin=subprocess.DEVNULL, stdout=server_log,
                           stderr=subprocess.STDOUT)

    def config():
        with open(cfg_path, encoding="utf-8") as fh:
            return json.load(fh)

    try:
        assert wait_port(UI), "server did not start"
        admin = Session()
        admin.prime("/setup")
        admin.post("/setup", {"username": "admin", "password": "adminpass123",
                              "password2": "adminpass123", "_csrf": admin.csrf()})
        assert admin.get("/")[0] == 200, "admin not signed in after setup"

        # ---- the mapping form persists + re-renders the new connection fields ----
        mport = free_port()
        st, _, _ = admin.post("/api/mappings/save", mapping_form(
            "plc", mport, "/dev/ttyOPEN1", network_tcp_keepalive="on",
            network_keepalive_idle_s="45", network_keepalive_interval_s="7",
            network_keepalive_count="5", opt_idle_timeout_s="300",
            opt_max_connection_time_s="86400"))
        assert st == 200, f"mapping save -> {st}"
        saved = config()["mappings"][0]
        mid = saved["id"]
        n, o = saved["network"], saved["options"]
        assert (n["tcp_keepalive"], n["keepalive_idle_s"], n["keepalive_interval_s"],
                n["keepalive_count"]) == (True, 45, 7, 5), n
        assert (o["idle_timeout_s"], o["max_connection_time_s"]) == (300, 86400), o
        st, body, _ = admin.get(f"/api/mappings/{mid}/form")
        assert st == 200
        for needle in ('name="network_keepalive_idle_s" min="1" max="32767" value="45"',
                       'name="network_keepalive_count" min="1" max="127" value="5"',
                       'name="opt_max_connection_time_s" min="0" value="86400"',
                       'name="network_connect_timeout_s"'):
            assert needle in body, f"edit form lacks {needle!r}"
        assert 'name="network_tcp_keepalive" checked' in body
        print("mapping form: keepalive / idle / max connection time saved and re-rendered  OK")

        # an INVALID save from a view that doesn't post the keepalive block (UDP mode):
        # the re-rendered form must still carry the stored values, not defaults
        st, body, _ = admin.post("/api/mappings/save", dict(
            mapping_form("", mport, "/dev/ttyOPEN1"), id=mid, network_mode="udp"))
        assert st == 400 and "Mapping name is required" in body, st
        assert 'name="network_keepalive_idle_s" min="1" max="32767" value="45"' in body, \
            "error re-render reset the stored keepalive idle to the default"
        assert config()["mappings"][0]["network"]["keepalive_idle_s"] == 45
        print("failed save: the re-rendered form keeps stored values of unposted fields  OK")

        # ---- default: login required ----
        anon = Session()
        st, _, loc = anon.get("/")
        assert st == 303 and loc.endswith("/login"), f"expected redirect to /login, got {st} {loc}"
        assert not console_ws_accepted(mid), "console WebSocket must need a login by default"
        print("default: no session -> redirected to /login; console WebSocket refused  OK")

        # ---- admin switches login off, visitors get 'viewer' ----
        st, body, loc = admin.post("/settings/access", {"anonymous_role": "viewer", "_csrf": admin.csrf()})
        assert st == 303 and loc == "/settings#access", (st, loc)   # to the page, which says it
        st, body, _ = admin.get("/settings")
        assert st == 200 and "Open access enabled" in body, st
        assert config()["admin_ui"]["require_login"] is False
        st, body, _ = anon.get("/")
        assert st == 200, f"open access: dashboard -> {st}"
        assert "no login · viewer" in body and "Sign in" in body, "topbar should show open access"
        st, _, _ = anon.get("/", host=f"localhost:{UI}")
        assert st == 200, "localhost must count as this machine"
        anon.prime("/")
        assert anon.post("/api/mappings/save", mapping_form("nope", free_port(), "/dev/ttyX"))[0] == 403
        assert anon.post("/settings/access", {"require_login": "on", "_csrf": anon.csrf()})[0] == 403
        st, body, _ = anon.get("/settings")
        assert st == 200 and "Web UI access" not in body and "Change your password" not in body
        # the edit form and the export carry stored secrets (MQTT password): operator+ only
        for path in (f"/api/mappings/{mid}/form", "/api/mappings/form", "/settings/config/export"):
            assert anon.get(path)[0] == 403, f"anonymous viewer must not read {path}"
        assert console_ws_accepted(mid), "open access (viewer) should allow the read-only console"
        print("open access (viewer): dashboard + console without login; writes/admin -> 403  OK")

        # a reverse proxy's forwarded host names count too (they can only narrow access)
        st, _, loc = Session().get("/", headers={"X-Forwarded-Host": "attacker.example"})
        assert st == 303 and loc.endswith("/login"), f"foreign X-Forwarded-Host must sign in: {st}"
        st, _, loc = Session().get("/", headers={"Forwarded": "for=1.2.3.4;host=attacker.example"})
        assert st == 303 and loc.endswith("/login"), f"foreign Forwarded host must sign in: {st}"
        assert Session().get("/", headers={"X-Forwarded-Host": f"127.0.0.1:{UI}"})[0] == 200
        print("open access also checks X-Forwarded-Host / Forwarded host names  OK")

        # ---- DNS-rebinding guard: another host name must still sign in ----
        rebind = Session()
        st, _, loc = rebind.get("/", host=f"attacker.example:{UI}")
        assert st == 303 and loc.endswith("/login"), f"foreign Host must not get open access: {st}"
        print("open access is refused for a foreign Host name (DNS rebinding)  OK")

        # ---- REST API is unaffected ----
        st, _, _ = anon.get("/api/v1/mappings")
        assert st == 401, f"REST API must still require its token, got {st}"

        # ---- operator role for visitors: mapping edits allowed ----
        assert admin.post("/settings/access", {"anonymous_role": "operator",
                                               "_csrf": admin.csrf()})[0] == 303
        st, _, _ = anon.post("/api/mappings/save", mapping_form("by-anon", free_port(), "/dev/ttyOPEN2"))
        assert st == 200, f"anonymous operator should save mappings, got {st}"
        assert anon.get(f"/api/mappings/{mid}/form")[0] == 200, "operator may open the edit form"
        assert anon.post("/settings/users", {"username": "x", "role": "viewer", "password": "userpass123",
                                             "password2": "userpass123", "_csrf": anon.csrf()})[0] == 403
        print("open access (operator): mapping edits allowed, user management still 403  OK")

        # LAN cluster: open access on THIS node must not open the other nodes
        st, _, _ = admin.post("/settings/cluster", {"enabled": "on", "key": "shared-cluster-key",
                                                    "discovery_port": str(free_port()),
                                                    "_csrf": admin.csrf()})
        assert st == 303 and config()["cluster"]["enabled"] is True
        st, body, _ = anon.get("/api/cluster/status")
        assert st == 200 and "Sign in</a> to see and manage the other cluster nodes" in body
        st, body, _ = admin.get("/api/cluster/status")
        assert st == 200 and "to see and manage the other cluster nodes" not in body
        peer = {"scheme": "http", "host": "127.0.0.1", "port": str(UI), "mid": mid}
        assert anon.post("/api/cluster/control-peer", dict(peer, action="stop"))[0] == 403
        assert anon.get("/api/cluster/peer-form?" + urllib.parse.urlencode(peer))[0] == 403
        assert anon.post("/api/cluster/peer-save", dict(peer, name="x"))[0] == 403
        print("cluster: anonymous visitors see only this node; peer control/edit need a login  OK")

        # ---- signing in still works on an open UI ----
        alice = Session()
        alice.prime("/login")
        st, _, _ = alice.post("/login", {"username": "admin", "password": "adminpass123",
                                         "_csrf": alice.csrf()})
        assert st == 303, f"login -> {st}"
        st, body, _ = alice.get("/settings")
        assert st == 200 and "Web UI access" in body and "admin · admin" in body
        print("signing in on an open UI still gives the account's own role  OK")

        # ---- invalid role is rejected, login back on closes the door ----
        st, body, _ = admin.post("/settings/access", {"anonymous_role": "root", "_csrf": admin.csrf()})
        assert "Invalid role" in body and config()["admin_ui"]["anonymous_role"] == "operator"
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect
        ws = connect(f"ws://127.0.0.1:{UI}/api/mappings/{mid}/console", open_timeout=5)
        assert admin.post("/settings/access", {"require_login": "on", "anonymous_role": "viewer",
                                               "_csrf": admin.csrf()})[0] == 303
        try:
            ws.recv(timeout=6)  # no serial data flows here: only a close can end this
            raise AssertionError("an open anonymous console survived login being switched on")
        except ConnectionClosed as e:
            assert e.rcvd is not None and e.rcvd.code == 4002, e.rcvd  # access changed
        except TimeoutError:
            raise AssertionError("an open anonymous console survived login being switched on") from None
        finally:
            ws.close()
        assert config()["admin_ui"]["require_login"] is True
        st, _, loc = anon.get("/")
        assert st == 303 and loc.endswith("/login"), "login must be required again"
        assert not console_ws_accepted(mid), "console WebSocket must need a login again"
        assert admin.get("/")[0] == 200, "the admin session survives the toggle"
        print("invalid role rejected; re-enabling login locks anonymous visitors out again  OK")

        print("\nPASS: optional web login (open access) + connection fields in the form")
    except BaseException:
        server_log.flush()
        with open(os.path.join(tmp, "server.log"), "rb") as fh:
            print("--- server log (tail) ---\n" + fh.read()[-3000:].decode("utf-8", "replace"))
        raise
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        server_log.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
