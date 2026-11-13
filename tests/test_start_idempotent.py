"""Start (and stop) are idempotent; restart restarts.

A start used to rebuild the runner even when the mapping was running: its TCP clients
were dropped, the serial port closed and opened again (the open string sent again, DTR/RTS
pulsed), MQTT reconnected, a Modbus gateway sent Sparkplug NDEATH. The dashboard offers
Start only on stopped rows, but a bulk Start of a mixed selection, the REST API and cluster
peers reached it. Checks, with a client connected to a running mapping:
  * REST start: `changed: false`, the client stays connected, config.json is not written;
  * a bulk start of the running mapping and a stopped one starts the stopped one only;
  * a PUT with the mapping's own settings changes nothing (`changed: false`);
  * restart restarts (`changed: true`, the client is dropped); a stop of a stopped mapping
    is `changed: false`.

Run: python3 tests/test_start_idempotent.py
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from ser2netweb.web.auth import hash_password, hash_token

PW = "idem-pass-1"
TOKEN = "s2n_idempotent-start"


def api(srv, method, path, body=None):
    req = urllib.request.Request(srv.base + "/api/v1" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def status(srv, mid):
    return api(srv, "GET", f"/mappings/{mid}")[1]["status"] or {}


def wait(cond, what, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def still_connected(sock) -> bool:
    sock.settimeout(0.5)
    try:
        data = sock.recv(256)
    except socket.timeout:
        return True
    except OSError:
        return False
    return False if not data else still_connected(sock)  # (a stop notice, then EOF)


def file_state(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def main():
    port_a, port_b = free_port(), free_port()
    net = {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1"}
    config = {"users": [{"username": "admin", "password_hash": hash_password(PW),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
              "mappings": [
                  {"id": "f10000000001", "name": "A", "serial": {"port": "/dev/s2n-idem-a"},
                   "network": dict(net, port=port_a)},
                  {"id": "f10000000002", "name": "B", "enabled": False,
                   "serial": {"port": "/dev/s2n-idem-b"}, "network": dict(net, port=port_b)}]}
    with Server(config) as srv:
        a, b = "f10000000001", "f10000000002"
        client = socket.create_connection(("127.0.0.1", port_a), timeout=5)
        wait(lambda: status(srv, a).get("client_count") == 1, "the client")
        written = file_state(srv.config_path)

        code, out = api(srv, "POST", f"/mappings/{a}/start")
        assert code == 200 and out["changed"] is False, out
        assert still_connected(client), "a start of a running mapping dropped its client"
        assert file_state(srv.config_path) == written, "config.json was written again"
        print("REST start of a running mapping: changed false, its client stays, no save  OK")

        browser = Browser(srv.base)
        browser.login("admin", PW)
        st, _, _ = browser.post("/api/mappings/bulk", {"action": "start", "ids": [a, b]}, htmx=True)
        assert st == 200, st
        wait(lambda: status(srv, b).get("state") in ("running", "reconnecting", "device-missing"),
             "B to start")
        assert still_connected(client) and status(srv, a).get("client_count") == 1
        print("bulk start of a running and a stopped mapping starts the stopped one only  OK")

        code, view = api(srv, "GET", f"/mappings/{a}")
        code, out = api(srv, "PUT", f"/mappings/{a}", view)
        assert code == 200 and out["changed"] is False, out
        assert still_connected(client)
        code, out = api(srv, "PUT", f"/mappings/{a}", dict(view, name="A2"))
        assert code == 200 and out["changed"] is True, out
        assert not still_connected(client), "an edit restarts the mapping"
        print("PUT of the same settings changes nothing; other settings restart it  OK")

        client = socket.create_connection(("127.0.0.1", port_a), timeout=5)
        wait(lambda: status(srv, a).get("client_count") == 1, "the client")
        code, out = api(srv, "POST", f"/mappings/{a}/restart")
        assert code == 200 and out["changed"] is True, out
        assert not still_connected(client), "restart must restart"
        code, out = api(srv, "POST", f"/mappings/{b}/stop")
        assert code == 200 and out["changed"] is True, out
        code, out = api(srv, "POST", f"/mappings/{b}/stop")
        assert code == 200 and out["changed"] is False, out
        print("restart restarts; a stop of a stopped mapping changes nothing  OK")
    print("\nPASS: start and stop are idempotent, restart restarts")


if __name__ == "__main__":
    main()
