"""An allow-list with no usable entry admits nobody, and says so (a hand-edited config.json).

Every edit (form, REST API, import, restore, cluster) refuses an allowed / priority client
entry that is no IP address or network, but config.json is not checked when it loads. A
list of only such entries — a host name, a comma-joined text, a range, a /33 — used to
parse to nothing, which the TCP bridge, UDP and the Modbus gateway all took as "no list":
everyone was admitted, Modbus writes included, without a word. Now, on a real server:
  * such a mapping refuses every client (TCP, the Modbus gateway, UDP datagrams); a list
    with some usable entries still admits those (as before);
  * start-up notes the unusable entries, and the runner logs them when it starts;
  * the mapping can still be stopped and started (start-up accepted the config as it is);
  * its edit form shows which entries are left out.

Run: python3 tests/test_allow_list_unusable.py
"""
import json
import socket
import time
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app.web.auth import hash_password, hash_token

TOKEN = "s2n_allow-list-test"


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(srv, method, path):
    req = urllib.request.Request(srv.base + "/api/v1" + path, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def closed_at_once(port: int) -> bool:
    """Connect; True when the server closes the connection right away."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.settimeout(3)
        try:
            return s.recv(1) == b""
        except (socket.timeout, TimeoutError):
            return False
        except ConnectionResetError:
            return True


def wait_for(srv, text: str, seconds: float = 10) -> str:
    end = time.time() + seconds
    while time.time() < end:
        log = srv.output()
        if text in log:
            return log
        time.sleep(0.2)
    raise AssertionError(f"{text!r} not logged:\n{srv.output()[-3000:]}")


def main():
    ports = {"tcp": free_port(), "gw": free_port(), "udp": free_udp_port(), "ok": free_port()}

    def mapping(mid, name, net):
        return {"id": mid, "name": name, "serial": {"port": f"/dev/s2n-missing-{mid}"},
                "network": {"mode": "server", "bind_ip": "127.0.0.1", **net}}

    config = {
        "users": [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
                   "role": "admin", "pwd_version": 1}],
        "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
        "mappings": [
            mapping("c00000000001", "tcp-host",
                    {"port": ports["tcp"], "allowed_client_ips": ["plc.local"]}),
            mapping("c00000000002", "gw-joined",
                    {"protocol": "modbus", "port": ports["gw"],
                     "allowed_client_ips": ["127.0.0.0/8, 10.0.0.0/8"]}),
            mapping("c00000000003", "udp-name",
                    {"mode": "udp", "port": ports["udp"], "allowed_client_ips": "localhost"}),
            mapping("c00000000004", "tcp-partly",
                    {"port": ports["ok"], "allowed_client_ips": ["127.0.0.1", "plc.local"]}),
        ]}
    with Server(config) as srv:
        log = wait_for(srv, "[tcp-partly] warning: Allowed client IPs")
        for line in (
                "mapping 'tcp-host': network.allowed_client_ips entries that are not IP "
                "addresses or networks are left out: 'plc.local'; none is usable, so every "
                "client is refused until the list is fixed",
                "mapping 'gw-joined': network.allowed_client_ips entries that are not IP "
                "addresses or networks are left out: '127.0.0.0/8, 10.0.0.0/8'; none is usable",
                "mapping 'udp-name': network.allowed_client_ips entries that are not IP "
                "addresses or networks are left out: 'localhost'; none is usable",
                "[tcp-host] warning: Allowed client IPs: these entries are not IP addresses or "
                "networks, so they are left out: plc.local. No entry is usable, so every client "
                "is refused.",
                "[gw-joined] warning: Allowed client IPs: these entries are not IP addresses or "
                "networks, so they are left out: 127.0.0.0/8, 10.0.0.0/8. No entry is usable",
                "[tcp-partly] warning: Allowed client IPs: these entries are not IP addresses "
                "or networks, so they are left out: plc.local.",
        ):
            assert line in log, f"{line!r} not logged:\n{log[-3000:]}"
        # (the line ends right after the entry; on Windows the server's output has CRLF)
        assert "tcp-partly': network.allowed_client_ips entries that are not IP addresses or " \
               "networks are left out: 'plc.local'\n" in log.replace("\r\n", "\n"), \
            "a partly usable list is not 'none'"
        print("start-up: the unusable entries noted; each runner says what it leaves out  OK")

        assert closed_at_once(ports["tcp"]), "the TCP bridge admitted a client"
        wait_for(srv, "[tcp-host] rejected 127.0.0.1:")
        assert closed_at_once(ports["gw"]), "the Modbus gateway admitted a master"
        wait_for(srv, "[gw-joined] rejected 127.0.0.1:")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.sendto(b"hello", ("127.0.0.1", ports["udp"]))
        wait_for(srv, "[udp-name] rejected datagram from 127.0.0.1:")
        assert not closed_at_once(ports["ok"]), "the usable entry no longer admits"
        wait_for(srv, "[tcp-partly] client connected: 127.0.0.1:")
        print("no usable entry: TCP, Modbus and UDP refuse everyone; a usable entry "
              "still admits  OK")

        for action in ("stop", "start"):
            st, body = api(srv, "POST", f"/mappings/c00000000001/{action}")
            assert st == 200, (action, st, body)
        assert srv.saved_config()["mappings"][0]["network"]["allowed_client_ips"] == ["plc.local"]
        print("the mapping can be stopped and started (the stored list kept as it is)  OK")

        b = Browser(srv.base)
        b.login("admin", "admin-pass-1")
        st, _h, form = b.get("/api/mappings/c00000000001/form")
        assert st == 200 and ("Allowed client IPs: these entries are not IP addresses or "
                              "networks, so they are left out: plc.local. No entry is usable, "
                              "so every client is refused.") in form, form[:1500]
        print("edit form: shows which entries are left out  OK")
    print("\nPASS: an allow-list with no usable entry admits nobody")


if __name__ == "__main__":
    main()
