"""Upgrading a v2.6.x install: its config.json must keep working unchanged.

Writes a config the way 2.6.x left it (no keepalive / connect-timeout / max-time /
web-access keys; values the old REST API accepted unchecked, e.g. idle_timeout_s
-1 or "300"), then boots the real server the way Docker / headless installs do —
with SER2NETWEB_BIND_IP set, which re-saves (and so validates) the config when loading
changed it, as here. Checks: it starts, the old password still logs in, mappings /
users / API token survive, legacy values are normalised, and the new settings take
their safe defaults (keepalive on, login still required). Cross-platform.

Run: python3 tests/test_upgrade_from_2_6.py
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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import ui_cookie
from ser2netweb.web.auth import hash_password, hash_token

ROOT = os.path.join(os.path.dirname(__file__), "..")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def v26_config(ui_port):
    """What a 2.6.x node's config.json looks like (keys as 2.6.3 wrote them)."""
    return {
        "version": 1,
        "admin_ui": {"bind_ip": "127.0.0.1", "port": ui_port, "tls_cert": "", "tls_key": ""},
        "secret_key": "a" * 64,
        "api_token_hash": hash_token("s2n_legacy-token"),
        "api_token_role": "operator",
        "session_timeout_s": 28800,
        "users": [{"username": "admin", "password_hash": hash_password("oldpass1234"),
                   "role": "admin", "pwd_version": 3, "source": "local"}],
        "mappings": [
            {"id": "aaaaaaaaaaaa", "name": "PLC-raw", "enabled": True, "kind": "net",
             "serial": {"port": "/dev/does-not-exist-1", "baudrate": 9600},
             "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                         "port": free_port(), "max_connections": 1},
             "options": {"idle_timeout_s": -1, "banner": "hi"}},
            {"id": "bbbbbbbbbbbb", "name": "Modbus-GW", "enabled": True, "kind": "net",
             "serial": {"port": "/dev/does-not-exist-2", "baudrate": 19200},
             "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                         "port": free_port()},
             "options": {"idle_timeout_s": "300"}},
            {"id": "cccccccccccc", "name": "Out", "enabled": False, "kind": "net",
             "serial": {"port": "/dev/does-not-exist-3"},
             "network": {"mode": "client", "protocol": "raw", "remote_host": "10.0.0.9",
                         "remote_port": 4001}},
        ],
    }


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_upgrade_")
    cfg_path = os.path.join(tmp, "config.json")
    ui = free_port()
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(v26_config(ui), fh)
    env = dict(os.environ, SER2NETWEB_BIND_IP="127.0.0.1", SER2NETWEB_PORT=str(ui))
    # server output goes to a file, never an unread pipe (a full pipe blocks the server)
    log_path = os.path.join(tmp, "server.log")
    server_log = open(log_path, "wb")
    srv = subprocess.Popen([sys.executable, "ser2netweb.py", "--no-bootstrap", "--config", cfg_path],
                           cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                           stdout=server_log, stderr=subprocess.STDOUT)
    try:
        if not wait_port(ui):
            server_log.flush()
            with open(log_path, "rb") as fh:
                out = fh.read().decode("utf-8", "replace")
            raise AssertionError(f"2.6.x config did not boot (Docker-style re-save):\n{out[-1500:]}")
        print("2.6.x config boots with SER2NETWEB_BIND_IP (re-saved + validated on start)  OK")

        with open(cfg_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        maps = {m["name"]: m for m in saved["mappings"]}
        assert set(maps) == {"PLC-raw", "Modbus-GW", "Out"}, "mappings lost on upgrade"
        assert maps["PLC-raw"]["options"]["idle_timeout_s"] == 0, "-1 ('off') should become 0"
        assert maps["PLC-raw"]["options"]["banner"] == "hi"
        assert maps["Modbus-GW"]["options"]["idle_timeout_s"] == 300, '"300" should become 300'
        for m in maps.values():
            n = m["network"]
            assert n["tcp_keepalive"] is True and n["keepalive_idle_s"] == 60, n
            assert n["connect_timeout_s"] == 10.0 and m["options"]["max_connection_time_s"] == 0
        assert saved["admin_ui"]["require_login"] is True, "login must stay required"
        assert saved["users"][0]["pwd_version"] == 3 and saved["api_token_role"] == "operator"
        print("mappings/users/token kept; legacy idle values normalised; new keys = safe defaults  OK")

        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(f"http://127.0.0.1:{ui}/login", timeout=10)
        csrf = next(c.value for c in jar if c.name == ui_cookie("ser2net_csrf", ui))
        r = op.open(urllib.request.Request(
            f"http://127.0.0.1:{ui}/login",
            data=urllib.parse.urlencode({"username": "admin", "password": "oldpass1234",
                                         "_csrf": csrf}).encode(), method="POST"), timeout=10)
        assert r.status == 200 and r.url.rstrip("/").endswith(str(ui)), "old password must log in"
        req = urllib.request.Request(f"http://127.0.0.1:{ui}/api/v1/mappings",
                                     headers={"Authorization": "Bearer s2n_legacy-token"})
        assert urllib.request.urlopen(req, timeout=10).status == 200, "old API token must work"
        print("old admin password and REST API token still work  OK")

        print("\nPASS: upgrade from a 2.6.x config")
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
