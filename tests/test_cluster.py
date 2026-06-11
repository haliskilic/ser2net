"""LAN cluster: discovery beacons + peer table + key-guarded endpoints (Phase 2).

Two parts:
  1) in-process unit of ClusterDiscovery — beacon sign/verify, peer registration,
     wrong-key rejection, self-ignore, TTL expiry (no real UDP needed).
  2) the real server with the cluster enabled — /api/cluster/local is guarded by
     the shared key (403 without / 200 with), reflects this node's mappings, and
     /api/cluster/status (session-authed) renders the unified table with the host.

Run: python3 tests/test_cluster.py
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
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from app.config import AppConfig, ClusterSettings              # noqa: E402
from app.engine.cluster import ClusterDiscovery, PEER_TTL, _sign  # noqa: E402


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


def _node(instance_id, key, port=41750):
    cfg = AppConfig()
    cfg.instance_id = instance_id
    cfg.cluster = ClusterSettings(enabled=True, key=key, discovery_port=port)
    return ClusterDiscovery(cfg, lambda m: None)


def test_discovery_unit():
    a = _node("aaaa1111", "shared")
    b = _node("bbbb2222", "shared")

    # A's signed beacon registers as a peer on B (same key)
    beacon = a._make_beacon()
    b.handle_datagram(beacon, ("10.0.0.5", 41750))
    peers = b.peers()
    assert len(peers) == 1 and peers[0]["id"] == "aaaa1111", peers
    assert peers[0]["ip"] and peers[0]["port"] == 8080, peers
    print("beacon sign + verify + peer registration  OK")

    # when a node advertises a loopback IP (LAN IP undetectable), the peer's IP
    # falls back to the packet's source address
    body = json.dumps({"v": 1, "id": "dddd4444", "name": "edge", "ip": "127.0.0.1",
                       "port": 8080, "scheme": "http", "t": 0},
                      separators=(",", ":"), sort_keys=True)
    loop_beacon = json.dumps({"d": body, "s": _sign("shared", body)}).encode()
    b.handle_datagram(loop_beacon, ("10.0.0.7", 41750))
    dd = next(p for p in b.peers() if p["id"] == "dddd4444")
    assert dd["ip"] == "10.0.0.7", dd
    b._peers.pop("dddd4444")  # keep the rest of the test's peer count clean
    print("loopback advertise -> packet-source IP fallback  OK")

    # a node configured with a different key must ignore the beacon
    c = _node("cccc3333", "other-key")
    c.handle_datagram(beacon, ("10.0.0.5", 41750))
    assert c.peers() == [], "beacon signed with a different key must be ignored"
    print("beacon with wrong key rejected  OK")

    # a node ignores its own beacon
    a.handle_datagram(a._make_beacon(), ("127.0.0.1", 41750))
    assert a.peers() == [], "node must ignore its own beacon"
    print("self-beacon ignored  OK")

    # garbage / unsigned data is dropped, doesn't crash
    b.handle_datagram(b"not-json", ("10.0.0.9", 41750))
    b.handle_datagram(json.dumps({"d": "{}", "s": "deadbeef"}).encode(), ("10.0.0.9", 41750))
    assert len(b.peers()) == 1, "garbage beacons must not register peers"
    print("garbage/forged beacons dropped  OK")

    # stale peers expire after PEER_TTL
    b._peers["aaaa1111"]["last_seen"] = time.time() - PEER_TTL - 5
    assert b.peers() == [], "stale peer should expire"
    print("stale peer expiry  OK")


def raw_request(ui, path, headers=None, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{ui}{path}", method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_endpoints():
    tmp = tempfile.mkdtemp(prefix="ser2net_cluster_")
    cfg = os.path.join(tmp, "config.json")
    ui = free_port()
    key = "test-cluster-key-123"
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": ui},
                   "cluster": {"enabled": True, "key": key, "discovery_port": free_port()}}, fh)
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert wait_port(ui), "server did not start"

        # become admin first (before setup, the first-run gate redirects everything
        # to /setup, so the key guard can't be observed)
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next(c.value for c in jar if c.name == "ser2net_csrf")

        def post(path, fields, header=False):
            req = urllib.request.Request(f"http://127.0.0.1:{ui}{path}",
                                         data=urllib.parse.urlencode(fields).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            if header:
                req.add_header("X-CSRF-Token", csrf())
            return op.open(req, timeout=10)

        op.open(f"http://127.0.0.1:{ui}/setup")
        post("/setup", {"username": "admin", "password": "adminpass123",
                        "password2": "adminpass123", "_csrf": csrf()})

        # peer-facing endpoint is guarded by the shared key (no user session)
        code, _ = raw_request(ui, "/api/cluster/local")
        assert code == 403, f"no key must be 403, got {code}"
        code, _ = raw_request(ui, "/api/cluster/local", {"X-Cluster-Key": "wrong"})
        assert code == 403, f"wrong key must be 403, got {code}"
        code, body = raw_request(ui, "/api/cluster/local", {"X-Cluster-Key": key})
        assert code == 200, f"correct key must be 200, got {code}"
        data = json.loads(body)
        assert data["name"] == socket.gethostname() and data["id"], data
        assert data["mappings"] == [], "no mappings yet"
        print("cluster/local: key-guarded (403/403/200), returns node identity  OK")

        port = free_port()
        post("/api/mappings/save", {
            "_csrf": csrf(), "name": "EDGE1", "enabled": "on", "kind": "net",
            "serial_port": "/dev/ttyTEST", "serial_baudrate": "9600",
            "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(port)}, header=True)

        # the mapping now shows up in this node's cluster payload
        _, body = raw_request(ui, "/api/cluster/local", {"X-Cluster-Key": key})
        data = json.loads(body)
        names = [m["name"] for m in data["mappings"]]
        assert "EDGE1" in names, names
        assert any(m["endpoint"].endswith(str(port)) for m in data["mappings"]), data["mappings"]
        print("cluster/local: created mapping appears with its endpoint  OK")

        # the aggregated table (session-authed) renders this node + mapping
        html = op.open(f"http://127.0.0.1:{ui}/api/cluster/status", timeout=10).read().decode()
        assert socket.gethostname() in html and "this node" in html, html[:400]
        assert "EDGE1" in html, "mapping should appear in the unified table"
        print("cluster/status: unified table shows host (name+IP) + mapping  OK")

        print("\nPASS: LAN cluster discovery + key-guarded aggregated view")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_discovery_unit()
    test_endpoints()


if __name__ == "__main__":
    main()
