"""LAN cluster: discovery beacons + peer table + key-guarded endpoints (Phase 2).

Parts:
  1) in-process unit of ClusterDiscovery — beacon sign/verify, peer registration,
     wrong-key rejection, self-ignore, TTL expiry (no real UDP needed).
  2) the real server with the cluster enabled — /api/cluster/local is guarded by
     the shared key: a signed request is accepted once (a replay, one for another
     node or one dated too far from the node's clock is refused, the last two with a
     signed answer that tells the right id / time), the raw key of a node running
     2.8.0 or older only while legacy authentication is on; refusals are logged and audited once;
     it reflects this node's mappings, and /api/cluster/status (session-authed)
     renders the unified table with the host; a remote action's audit record names
     the user a signed request says acted on the sending node (cluster-peer:NODE/USER),
     else cluster-peer.
  3) real nodes linked by manual peers: remote control and edit (signed, the key
     never on the wire - checked through a recording relay, whose captured request
     can't be replayed; both nodes' audit logs name the user), an old node that only
     knows the raw key, legacy authentication switched off, and a node with another
     key ("key rejected").
Also the fleet view's edit of a mapping on a stand-in v2.7/v2.8 peer whose id this
version would not take (kept as the peer's).

Run: python3 tests/test_cluster.py
"""
import contextlib
import html as htmllib
import http.client
import http.cookiejar
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from app.config import AppConfig, ClusterSettings              # noqa: E402
from app.engine import netinfo                                  # noqa: E402
from app.engine.cluster import (AUTH_HEADER, PEER_TTL, ClusterDiscovery, _sign,  # noqa: E402
                                canonical_target, format_auth, key_header, parse_auth,
                                reply_mac, request_mac)
from app.web.auth import hash_password                         # noqa: E402


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

    # A's signed beacon registers as a peer on B (same key); A advertises 127.0.0.1 (no
    # default route here), so B takes the packet's source - one of A's own addresses
    beacon = a._make_beacon()
    src = (netinfo.own_ipv4() or ["10.0.0.5"])[0]
    b.handle_datagram(beacon, (src, 41750))
    peers = b.peers()
    assert len(peers) == 1 and peers[0]["id"] == "aaaa1111", peers
    assert peers[0]["ip"] and peers[0]["port"] == 8080 and peers[0]["v"] == 2, peers
    print("beacon sign + verify + peer registration  OK")

    # when a node running 2.8.0 or older advertises a loopback IP (LAN IP undetectable), the peer's
    # IP falls back to the packet's source address
    body = json.dumps({"v": 1, "id": "dddd4444", "name": "edge", "ip": "127.0.0.1",
                       "port": 8080, "scheme": "http", "t": 0},
                      separators=(",", ":"), sort_keys=True)
    loop_beacon = json.dumps({"d": body, "s": _sign("shared", body)}).encode()
    b.handle_datagram(loop_beacon, ("10.0.0.7", 41750))
    dd = next(p for p in b.peers() if p["id"] == "dddd4444")
    assert dd["ip"] == "10.0.0.7", dd
    b.handle_datagram(loop_beacon, ("10.0.0.66", 41750))   # the same beacon again
    assert next(p for p in b.peers() if p["id"] == "dddd4444")["ip"] == "10.0.0.7", \
        "a replayed beacon must not move the peer"
    b._peers.pop(("dddd4444", ""))  # keep the rest of the test's peer count clean
    print("loopback advertise -> packet-source IP fallback, replay ignored  OK")

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
    for row in b._peers.values():
        row["last_seen"] = time.time() - PEER_TTL - 5
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


def signed(key, method, url, body=b"", to="", sender="tester01", ts=None):
    """X-Cluster-Auth for one request (as a signing node makes it) and its nonce."""
    parts = urllib.parse.urlsplit(url)
    target = canonical_target(urllib.parse.unquote(parts.path), parts.query)
    ts = f"{time.time() if ts is None else ts:.3f}"
    nonce = os.urandom(16).hex()
    sig = request_mac(key, method, target, body, sender, to, ts, nonce)
    return {AUTH_HEADER: format_auth({"from": sender, "to": to, "ts": ts, "nonce": nonce,
                                      "sig": sig})}, nonce


def signed_call(ui, key, path, method="GET", body=b"", **kw):
    """(status, JSON body, reply auth params, nonce, whether the reply is signed for it)."""
    url = f"http://127.0.0.1:{ui}{path}"
    headers, nonce = signed(key, method, url, body, **kw)
    req = urllib.request.Request(url, data=body or None, method=method, headers=headers)
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        r = e
    raw = r.read()
    status = r.status if hasattr(r, "status") else r.code
    reply = parse_auth(r.headers.get(AUTH_HEADER)) or {}
    ok = bool(reply.get("sig")) and reply["sig"] == reply_mac(
        key, nonce, status, raw, reply.get("id", ""), reply.get("now", ""))
    return status, json.loads(raw or b"{}"), reply, headers, ok


def header_request(ui, path, name, value: bytes):
    """GET with one header sent as raw bytes (e.g. non-ASCII); returns the status."""
    conn = http.client.HTTPConnection("127.0.0.1", ui, timeout=10)
    try:
        conn.putrequest("GET", path)
        conn.putheader(name, value)
        conn.endheaders()
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


def test_endpoints():
    tmp = tempfile.mkdtemp(prefix="ser2net_cluster_")
    cfg = os.path.join(tmp, "config.json")
    ui = free_port()
    key = "test-cluster-key-123"
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": ui},
                   "cluster": {"enabled": True, "key": key, "discovery_port": free_port()}}, fh)
    log = open(os.path.join(tmp, "server.out"), "wb")
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    def srv_output():
        log.flush()
        return open(os.path.join(tmp, "server.out"), encoding="utf-8", errors="replace").read()
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

        op.open(f"http://127.0.0.1:{ui}/setup", timeout=10)
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
        # a key that isn't ASCII (the UTF-8 or latin-1 bytes of a letter) is a wrong key
        for raw in ("ü".encode(), "ü".encode("latin-1"), key.encode() + "ş".encode()):
            code = header_request(ui, "/api/cluster/local", "X-Cluster-Key", raw)
            assert code == 403, f"non-ASCII key bytes must be 403, got {code}"
        print("cluster/local: key-guarded (403/403/200, non-ASCII -> 403), returns node identity  OK")

        # signed requests: a first one without the node's id is refused with a signed
        # answer naming it; then accepted, with a signed reply - once
        node_id = data["id"]
        st, body, reply, _h, ok = signed_call(ui, key, "/api/cluster/local")
        assert st == 403 and body["reason"] == "recipient" and ok and reply["id"] == node_id, \
            (st, body, reply)
        st, body, reply, headers, ok = signed_call(ui, key, "/api/cluster/local", to=node_id)
        assert st == 200 and body["id"] == node_id and ok, (st, body, reply)
        replay = urllib.request.Request(f"http://127.0.0.1:{ui}/api/cluster/local", headers=headers)
        try:
            urllib.request.urlopen(replay, timeout=10)
            raise AssertionError("a replayed signed request must be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 403 and json.loads(e.read())["reason"] == "replay"
        # dated far from the node's clock (an unsynchronised one): refused, with the node's
        # time in a signed answer, which a signing node uses to sign again
        for ts in (time.time() - 3600, time.time() + 86400):
            st, body, reply, _h, ok = signed_call(ui, key, "/api/cluster/local", to=node_id, ts=ts)
            assert st == 403 and body["reason"] == "stale" and ok, (st, body)
            assert abs(float(reply["now"]) - time.time()) < 30, reply
        # another key: refused (not signed), logged once and audited once
        for _ in range(3):
            st, body, *_rest = signed_call(ui, "not-the-cluster-key", "/api/cluster/local",
                                           to=node_id)
            assert st == 403 and body["reason"] == "signature", (st, body)
        print("cluster/local: signed requests accepted once; replay, other node, clock skew and "
              "another key refused  OK")

        # a peer request's body is bounded: a large one is refused (and only a signed
        # request's body is read at all)
        big = b"mapping_id=x&action=stop&pad=" + b"0" * (2 * 1024 * 1024)
        url = f"http://127.0.0.1:{ui}/api/cluster/control"
        for headers in (signed(key, "POST", url, big, to=node_id)[0], {}):
            req = urllib.request.Request(url, data=big, method="POST", headers=headers)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                urllib.request.urlopen(req, timeout=10)
                raise AssertionError("an oversized peer request must be refused")
            except urllib.error.HTTPError as e:
                assert e.code in (403, 413), e.code
            except (urllib.error.URLError, ConnectionError):
                pass   # refused (413) and closed while the body was still being sent
        assert raw_request(ui, "/api/cluster/local", {"X-Cluster-Key": key})[0] == 200
        print("peer requests: an oversized body is refused  OK")

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

        # who acted on the sending node: a signed request's name is recorded as that node's
        # claim; one sent with the raw key (a node running 2.8.0 or older) is not used
        edge = next(m["id"] for m in data["mappings"] if m["name"] == "EDGE1")
        for fields in ({"_actor": "Ayşe İlker", "_actor_node": "plc gw/2"},
                       {"_actor": "op/1"}, {}):
            body = urllib.parse.urlencode({"mapping_id": edge, "action": "stop", **fields}).encode()
            st, answer, *_rest = signed_call(ui, key, "/api/cluster/control", "POST", body,
                                             to=node_id)
            assert st == 200 and answer["ok"], (st, answer)
        legacy = urllib.request.Request(
            f"http://127.0.0.1:{ui}/api/cluster/control", method="POST",
            data=b"mapping_id=" + edge.encode() + b"&action=stop&_actor=admin&_actor_node=x",
            headers={"X-Cluster-Key": key, "Content-Type": "application/x-www-form-urlencoded"})
        assert urllib.request.urlopen(legacy, timeout=10).status == 200
        users = [ln.split("\t")[4] for ln in open(os.path.join(tmp, "audit.log"),
                                                   encoding="utf-8").read().splitlines()
                 if "\tcluster_remote_stop\tEDGE1\t" in ln]
        assert users == ["cluster-peer:plcgw2/Ayşe İlker", "cluster-peer:tester01/op/1",
                         "cluster-peer", "cluster-peer"], users
        print("remote action: the sending node's user is recorded as its claim "
              "(cluster-peer:NODE/USER); without one, or with the raw key: cluster-peer  OK")

        # the aggregated table (session-authed) renders this node + mapping
        html = op.open(f"http://127.0.0.1:{ui}/api/cluster/status", timeout=10).read().decode()
        assert socket.gethostname() in html and "this node" in html, html[:400]
        assert "EDGE1" in html, "mapping should appear in the unified table"
        print("cluster/status: unified table shows host (name+IP) + mapping  OK")

        # legacy authentication off: the raw key is refused, signed requests still work
        st = post("/settings/cluster", {"_csrf": csrf(), "cluster_form": "2", "enabled": "on",
                                        "key": key, "discovery": "on",
                                        "discovery_port": str(free_port())}).status
        assert st == 200
        code, body = raw_request(ui, "/api/cluster/local", {"X-Cluster-Key": key})
        assert code == 403 and json.loads(body)["reason"] == "legacy", (code, body)
        assert signed_call(ui, key, "/api/cluster/local", to=node_id)[0] == 200
        out = srv_output()
        assert out.count("sent the cluster key unsigned") == 1, "raw-key use logged once"
        assert out.count("refused GET /api/cluster/local from 127.0.0.1: a signature") == 1, out[-3000:]
        assert "refused GET /api/cluster/local from 127.0.0.1: the cluster key unsigned" in out
        audit = open(os.path.join(tmp, "audit.log"), encoding="utf-8").read()
        assert audit.count("cluster_auth_failed") == audit.count("\tcluster_auth_failed\t") >= 3, audit
        assert "GET /api/cluster/local: signature" in audit and "GET /api/cluster/local: legacy" in audit
        print("legacy authentication off: raw key refused; refusals and raw-key use logged and "
              "audited once per change  OK")

        print("\nPASS: LAN cluster discovery + key-guarded aggregated view")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


def rendered_actions(html, mid):
    """What the fleet view's buttons for mapping *mid* send: {action: hx-vals} for the
    control buttons, plus "edit": the Edit button's URL."""
    out = {}
    for vals in re.findall(r"hx-vals='([^']*)'", html):
        v = json.loads(htmllib.unescape(vals))
        if v.get("mid") == mid:
            out[v["action"]] = v
    for url in re.findall(r'hx-get="(/api/cluster/peer-form\?[^"]*)"', html):
        url = htmllib.unescape(url)
        if urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("mid") == [mid]:
            out["edit"] = url
    return out


def trap_listener():
    """A listening socket nothing should ever connect to (the kernel queues a connection
    attempt even if it is never accepted, so trapped() sees it)."""
    trap = socket.socket()
    trap.bind(("127.0.0.1", 0))
    trap.listen(8)
    trap.setblocking(False)
    return trap


def trapped(trap) -> bool:
    try:
        trap.accept()[0].close()
        return True
    except BlockingIOError:
        return False


def start_node(cfg_obj, env=None):
    tmp = tempfile.mkdtemp(prefix="ser2net_cl_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump(cfg_obj, fh)
    child = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
    proc = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                            cwd=ROOT, stdin=subprocess.DEVNULL, env=dict(child, **(env or {})),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc, tmp


def test_manual_peer_and_remote_control():
    """Two real nodes linked by a MANUAL peer (no UDP dependency): node A aggregates
    node B's mappings + health and can remotely start/stop/edit them with the shared
    key. B reports an address of its own (as behind NAT or Docker) that A can't use:
    the buttons A renders must still reach B, so the test sends exactly those. The key
    has Turkish letters, and B's copy is the same text in decomposed form (NFD)."""
    key = "küme-anahtarı-çöüşğİı"
    hdr = {"X-Cluster-Key": key_header(key)}
    ui_a, ui_b = free_port(), free_port()
    # different discovery ports so the nodes DON'T auto-discover each other — the
    # manual peer is the only link, making the test deterministic.
    dp_a, dp_b = free_port(), free_port()
    map_port, map_port2 = free_port(), free_port()
    node_b = {"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_b},
              "users": [{"username": "admin", "password_hash": hash_password("clusteradmin1"),
                         "role": "admin", "pwd_version": 1}],
              "cluster": {"enabled": True, "key": unicodedata.normalize("NFD", key),
                          "discovery_port": dp_b,
                          "advertise_ip": "192.0.2.10"},   # not where A reaches it
              "mappings": [{"name": "REMOTE1", "enabled": False, "kind": "net",
                            "serial": {"port": "/dev/ttyTEST", "baudrate": 9600},
                            "network": {"mode": "server", "protocol": "raw",
                                        "bind_ip": "127.0.0.1", "port": map_port}},
                           {"name": "REMOTE2", "enabled": False, "kind": "net",
                            "serial": {"port": "/dev/ttyTEST2", "baudrate": 9600},
                            "network": {"mode": "server", "protocol": "raw",
                                        "bind_ip": "127.0.0.1", "port": map_port2}}]}
    node_a = {"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_a},
              "cluster": {"enabled": True, "key": key, "discovery_port": dp_a,
                          "peers": [f"127.0.0.1:{ui_b}"]}}
    # A runs with proxy variables (as for webhooks / SSO): its peer calls must not use them
    proxy = trap_listener()
    via = f"http://127.0.0.1:{proxy.getsockname()[1]}"
    pa, ta = start_node(node_a, {"HTTP_PROXY": via, "HTTPS_PROXY": via, "ALL_PROXY": via})
    pb, tb = start_node(node_b)
    try:
        assert wait_port(ui_a) and wait_port(ui_b), "nodes did not start"

        # admin session on node A
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next(c.value for c in jar if c.name == "ser2net_csrf")

        def post_a(path, fields):
            req = urllib.request.Request(f"http://127.0.0.1:{ui_a}{path}",
                                         data=urllib.parse.urlencode(fields).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", csrf())
            try:
                return op.open(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code

        def get_a(path, header=True):
            req = urllib.request.Request(f"http://127.0.0.1:{ui_a}{path}")
            if header:  # what htmx sends with every request
                req.add_header("X-CSRF-Token", csrf())
            try:
                r = op.open(req, timeout=10)
            except urllib.error.HTTPError as e:
                r = e
            return r.status if hasattr(r, "status") else r.code, r.read().decode()

        op.open(f"http://127.0.0.1:{ui_a}/setup")
        post_a("/setup", {"username": "admin", "password": "adminpass123",
                          "password2": "adminpass123", "_csrf": csrf()})

        # node B's mapping id (and confirm it starts disabled)
        _, body = raw_request(ui_b, "/api/cluster/local", hdr)
        bmaps = json.loads(body)["mappings"]
        mid = bmaps[0]["id"]
        assert bmaps[0]["enabled"] is False, bmaps

        # A's aggregated view fetches B through the manual peer: shows it + health
        html = op.open(f"http://127.0.0.1:{ui_a}/api/cluster/status", timeout=10).read().decode()
        assert "REMOTE1" in html and "manual" in html, html[:500]
        assert ("v2.6" in html or "up " in html), "per-node health (version/uptime) missing"
        assert "unsigned key" not in html, "two current nodes must sign their requests"
        assert not trapped(proxy), "peer calls must connect directly, never via a proxy"
        print("manual peer aggregated with health (version/uptime) + 'manual' tag, signed  OK")

        # B is shown, opened and controlled at the address A reaches it at; the address
        # B reports for itself is only shown as information
        assert f'href="http://127.0.0.1:{ui_b}/"' in html, "Open must point where A reaches B"
        assert f"its own address: 192.0.2.10:{ui_b}" in html, "B's own address shown as info"
        acts = rendered_actions(html, mid)
        assert set(acts) == {"start", "edit"}, acts
        assert acts["start"] == {"host": "127.0.0.1", "port": ui_b, "mid": mid, "action": "start"}
        print("fleet view uses the address B was found at, B's own one only shown  OK")

        # peer-facing control is key-guarded
        code, _ = raw_request(ui_b, "/api/cluster/control", method="POST")
        assert code == 403, f"control without key must be 403, got {code}"

        # remote START via A's proxy (the values A's Start button sends) -> B's mapping
        # becomes enabled
        assert post_a("/api/cluster/control-peer", acts["start"]) == 200, \
            "remote start should be accepted"
        for _ in range(20):
            _, body = raw_request(ui_b, "/api/cluster/local", hdr)
            if json.loads(body)["mappings"][0]["enabled"]:
                break
            time.sleep(0.1)
        assert json.loads(body)["mappings"][0]["enabled"] is True, "remote start did not take effect"
        host = re.sub(r"[\s/]+", "", socket.gethostname())[:64]
        claim = f"cluster-peer:{host}/admin"   # A's host name and user, as A states them
        audit_b = open(os.path.join(tb, "audit.log"), encoding="utf-8").read()
        assert f"\tcluster_remote_start\tREMOTE1\t{claim}\n" in audit_b, audit_b
        audit_a = open(os.path.join(ta, "audit.log"), encoding="utf-8").read()
        assert f"\tcluster_control_start\t127.0.0.1:{ui_b}/REMOTE1\tadmin\n" in audit_a, audit_a
        print("remote start of a peer's mapping via the unified view; both nodes' audit logs "
              "name the user  OK")

        # A's view shows the start at once: the answer it shares between dashboards for a
        # few seconds was dropped by the remote action
        html = op.open(f"http://127.0.0.1:{ui_a}/api/cluster/status", timeout=10).read().decode()
        acts = rendered_actions(html, mid)
        assert set(acts) == {"stop", "restart", "edit"}, f"stale fleet view after start: {acts}"

        # remote STOP via A's proxy -> B's mapping becomes disabled. A scheme in the
        # request is not used: the call goes to the peer's own address, never elsewhere.
        trap = trap_listener()
        crafted = f"http://127.0.0.1:{trap.getsockname()[1]}/?x="
        assert post_a("/api/cluster/control-peer", {**acts["stop"], "scheme": crafted}) == 200
        for _ in range(20):
            _, body = raw_request(ui_b, "/api/cluster/local", hdr)
            if not json.loads(body)["mappings"][0]["enabled"]:
                break
            time.sleep(0.1)
        assert json.loads(body)["mappings"][0]["enabled"] is False, "remote stop did not take effect"
        assert not trapped(trap), "a scheme from the request must not choose where the call goes"
        print("remote stop of a peer's mapping via the unified view (request scheme unused)  OK")

        # the proxy refuses an address that isn't a known cluster peer (anti-SSRF)
        bad = {"scheme": "http", "host": "9.9.9.9", "port": "1", "mid": mid, "action": "start"}
        assert post_a("/api/cluster/control-peer", bad) == 403, "unknown peer must be rejected"
        print("control proxy rejects an unknown (non-peer) address  OK")

        # ---- remote EDIT: load the peer's mapping into the form, change it, save ----
        form_url = acts["edit"] + "&" + urllib.parse.urlencode({"scheme": crafted})
        code, html = get_a(form_url)
        assert code == 200, (code, html[:300])
        assert "/api/cluster/peer-save" in html and 'name="host"' in html, html[:400]
        assert 'name="scheme"' not in html, "the form must not carry a scheme back"
        assert "Edit mapping" in html and "/dev/ttyTEST" in html, "form should show peer's values"
        assert f"on 127.0.0.1:{ui_b}" in html, "the form names the peer where A reaches it"
        assert not trapped(trap), "the form must be loaded from the peer itself"
        # a GET without the token header (a link from another site) makes no peer call
        assert get_a(form_url, header=False)[0] == 403, "peer-form needs the CSRF header"
        print("remote edit: peer's mapping loads into the form (routed to peer-save)  OK")

        # peer-facing save is key-guarded
        code, _ = raw_request(ui_b, "/api/cluster/mapping-save", method="POST")
        assert code == 403, f"mapping-save without key must be 403, got {code}"

        edit = {"_csrf": csrf(), "host": "127.0.0.1", "port": str(ui_b), "scheme": crafted,
                "id": mid, "name": "REMOTE1", "enabled": "on", "kind": "net",
                "serial_port": "/dev/ttyEDITED", "serial_baudrate": "19200",
                "serial_bytesize": "8", "serial_parity": "N", "serial_stopbits": "1",
                "serial_flowcontrol": "none", "network_mode": "server",
                "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
                "network_port": str(map_port)}
        assert post_a("/api/cluster/peer-save", edit) == 200, "remote edit should be accepted"
        for _ in range(20):
            _, body = raw_request(ui_b, "/api/cluster/local", hdr)
            if json.loads(body)["mappings"][0]["serial"] == "/dev/ttyEDITED":
                break
            time.sleep(0.1)
        assert json.loads(body)["mappings"][0]["serial"] == "/dev/ttyEDITED", \
            "remote edit did not change the peer's mapping"
        html = op.open(f"http://127.0.0.1:{ui_a}/api/cluster/status", timeout=10).read().decode()
        assert "ttyEDITED" in html, "A's fleet view must show the edit at once"
        assert not trapped(trap), "the edit must be saved on the peer itself"
        assert f"\tcluster_remote_save\tREMOTE1\t{claim}\n" in open(
            os.path.join(tb, "audit.log"), encoding="utf-8").read()
        trap.close()
        print("remote edit of a peer's mapping took effect on the peer  OK")

        # the proxy forwards only known, validated fields: a URL serial port is refused
        bad_port = {**edit, "serial_port": "socket://10.0.0.9:1"}
        assert post_a("/api/cluster/peer-save", bad_port) == 400, "a URL serial port must be refused"
        _, body = raw_request(ui_b, "/api/cluster/local", hdr)
        assert json.loads(body)["mappings"][0]["serial"] == "/dev/ttyEDITED", "the URL edit leaked"

        # a trace path typed into the form reaches the peer only as a bare file name
        trace_edit = {**edit, "serial_port": "/dev/ttyEDITED", "opt_trace_both": "/data/config.json"}
        assert post_a("/api/cluster/peer-save", trace_edit) == 200
        bcfg = {}
        for _ in range(20):
            bcfg = json.load(open(os.path.join(tb, "config.json")))
            if bcfg["mappings"][0]["options"]["trace_both"]:
                break
            time.sleep(0.1)
        assert bcfg["mappings"][0]["options"]["trace_both"] == "config.json", \
            bcfg["mappings"][0]["options"]["trace_both"]

        # editing a mapping id the peer doesn't have is refused (no create via the proxy)
        ghost = {**edit, "id": "nosuchid9999"}
        assert post_a("/api/cluster/peer-save", ghost) == 400, "editing a missing mapping must be refused"
        print("remote edit proxy: URL port refused, trace path reduced to a name, missing id refused  OK")

        # a change only the peer can refuse (its other mapping has that port) shows the
        # peer's own reason in the form
        req = urllib.request.Request(
            f"http://127.0.0.1:{ui_a}/api/cluster/peer-save", method="POST",
            data=urllib.parse.urlencode({**edit, "network_port": str(map_port2)}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf()})
        try:
            op.open(req, timeout=10)
            raise AssertionError("a port clash on the peer must be refused")
        except urllib.error.HTTPError as e:
            code, page = e.code, e.read().decode()
        assert code == 400 and "REMOTE2" in page, (code, page[:600])
        print("remote edit: the peer's own validation message is shown  OK")
        assert not trapped(proxy), "peer calls must connect directly, never via a proxy"
        proxy.close()
        print("peer calls ignored HTTP(S)_PROXY / ALL_PROXY  OK")

        print("\nPASS: manual peers + per-node health + remote control + remote edit")
    finally:
        for proc, tmp in ((pa, ta), (pb, tb)):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)


def _old_peer(key, peer_id, seen):
    """A stand-in for a v2.7/v2.8 cluster node that keeps a mapping under an id this
    version would not take (it rewrites such ids on load). Serves the key-guarded peer
    endpoints the fleet view's edit uses, and records what it was asked (``seen``)."""
    import http.server
    import threading

    mapping = {"id": peer_id, "name": "OLD1", "enabled": False, "kind": "net",
               "serial": {"port": "/dev/ttyOLD", "baudrate": 9600},
               "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                           "port": 4555}, "options": {"trace_both": ""}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("X-Cluster-Key") != key:
                return self._reply(403, {"error": "forbidden"})
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/cluster/local":
                port = self.server.server_address[1]
                return self._reply(200, {
                    "id": "oldnode", "name": "old-node", "ip": "127.0.0.1", "port": port,
                    "scheme": "http", "version": "2.8.0", "uptime_s": 5, "mapping_count": 1,
                    "running_count": 0, "mappings": [{
                        "id": peer_id, "name": "OLD1", "enabled": False, "kind": "server/raw",
                        "endpoint": "127.0.0.1:4555", "serial": "/dev/ttyOLD",
                        "state": "stopped", "client_count": 0, "bytes_in": 0, "bytes_out": 0}]})
            if url.path == "/api/cluster/mapping-data":
                mid = urllib.parse.parse_qs(url.query).get("mid", [""])[0]
                seen.append(("mapping-data", mid))
                if mid != peer_id:
                    return self._reply(404, {"error": "not_found"})
                return self._reply(200, {"mapping": mapping, "ports": [], "ips": []})
            return self._reply(404, {"error": "not_found"})

        def do_POST(self):
            if self.headers.get("X-Cluster-Key") != key:
                return self._reply(403, {"error": "forbidden"})
            fields = urllib.parse.parse_qs(
                self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            seen.append(("mapping-save", fields.get("id", [""])[0]))
            if fields.get("id", [""])[0] != peer_id:
                return self._reply(400, {"ok": False, "error": "no such mapping"})
            return self._reply(200, {"ok": True, "name": "OLD1"})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_edit_on_a_peer_with_its_own_id():
    """The fleet view edits a mapping on a v2.7/v2.8 peer whose id this version would not
    take: the form keeps the peer's own id (this node's copy of the mapping gets a derived
    one), and the save reaches the peer's mapping under it."""
    key = "old-peer-key-1"
    peer_id = "my-port&x"
    seen = []
    stub = _old_peer(key, peer_id, seen)
    stub_port = stub.server_address[1]
    ui_a = free_port()
    pa, ta = start_node({"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_a},
                         "cluster": {"enabled": True, "key": key, "discovery_port": free_port(),
                                     "peers": [f"127.0.0.1:{stub_port}"]}})
    try:
        assert wait_port(ui_a), "node did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next(c.value for c in jar if c.name == "ser2net_csrf")

        def post_a(path, fields):
            req = urllib.request.Request(f"http://127.0.0.1:{ui_a}{path}",
                                         data=urllib.parse.urlencode(fields).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", csrf())
            try:
                return op.open(req, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code

        op.open(f"http://127.0.0.1:{ui_a}/setup")
        post_a("/setup", {"username": "admin", "password": "adminpass123",
                          "password2": "adminpass123", "_csrf": csrf()})

        html = op.open(f"http://127.0.0.1:{ui_a}/api/cluster/status", timeout=10).read().decode()
        assert "OLD1" in html and "mid=my-port%26x" in html, html[:800]

        # the peer by host + port (this node resolves it); the GET carries the CSRF header,
        # as the Edit button's htmx request does
        q = urllib.parse.urlencode({"host": "127.0.0.1", "port": stub_port, "mid": peer_id})
        form_req = urllib.request.Request(f"http://127.0.0.1:{ui_a}/api/cluster/peer-form?{q}")
        form_req.add_header("X-CSRF-Token", csrf())
        html = op.open(form_req, timeout=10).read().decode()
        assert seen[-1] == ("mapping-data", peer_id), seen
        assert '<input type="hidden" name="id" value="my-port&amp;x">' in html, html[:1500]
        assert "/dev/ttyOLD" in html

        edit = {"_csrf": csrf(), "host": "127.0.0.1", "port": str(stub_port),
                "id": peer_id, "name": "OLD1", "enabled": "on", "kind": "net",
                "serial_port": "/dev/ttyEDITED", "serial_baudrate": "19200",
                "serial_bytesize": "8", "serial_parity": "N", "serial_stopbits": "1",
                "serial_flowcontrol": "none", "network_mode": "server",
                "network_protocol": "raw", "network_bind_ip": "127.0.0.1", "network_port": "4555"}
        assert post_a("/api/cluster/peer-save", edit) == 200, seen
        assert ("mapping-data", peer_id) in seen and seen[-1] == ("mapping-save", peer_id), seen
        # a form that comes back with an error keeps the peer's id too
        req = urllib.request.Request(f"http://127.0.0.1:{ui_a}/api/cluster/peer-save",
                                     data=urllib.parse.urlencode(
                                         {**edit, "serial_port": "socket://10.0.0.9:1"}).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("X-CSRF-Token", csrf())
        try:
            op.open(req, timeout=10)
            raise AssertionError("a URL serial port must be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            again = e.read().decode()
        assert '<input type="hidden" name="id" value="my-port&amp;x">' in again, again[:1500]
        print("remote edit of a v2.7/v2.8 peer's mapping under the peer's own id (one this "
              "version would not take): form, save and a re-shown form keep it  OK")
    finally:
        stub.shutdown()
        pa.terminate()
        try:
            pa.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pa.kill()
        shutil.rmtree(ta, ignore_errors=True)


class Relay:
    """A TCP relay to 127.0.0.1:<target> that keeps what it forwards: sent[i] / got[i] are
    the bytes of connection i towards the target and back."""

    def __init__(self, target):
        self.target, self.sent, self.got = target, [], []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._pump, args=(conn,), daemon=True).start()

    def _pump(self, conn):
        up = socket.create_connection(("127.0.0.1", self.target), timeout=10)
        out, back = bytearray(), bytearray()

        def copy(src, dst, buf):
            try:
                while data := src.recv(65536):
                    buf.extend(data)
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    dst.shutdown(socket.SHUT_WR)
        t = threading.Thread(target=copy, args=(up, conn, back), daemon=True)
        t.start()
        copy(conn, up, out)
        t.join(10)
        conn.close()
        up.close()
        self.sent.append(bytes(out))
        self.got.append(bytes(back))

    def close(self):
        self.sock.close()


class OldNode:
    """A cluster node as releases up to 2.8.0 are: it only checks X-Cluster-Key and refuses
    anything else with a plain 403; it records the requests it gets."""

    def __init__(self, key):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({k.lower(): v for k, v in self.headers.items()})
                if self.headers.get("X-Cluster-Key") == key:
                    code, body = 200, json.dumps({
                        "id": "old00001", "name": "old-node", "ip": "127.0.0.1",
                        "port": outer.port, "scheme": "http", "version": "2.8.0",
                        "uptime_s": 60, "mapping_count": 0, "running_count": 0,
                        "mappings": []}).encode()
                else:
                    code, body = 403, b'{"error": "forbidden"}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_signed_legacy_and_mismatch():
    """Node A has three manual peers: node B (current, the same key) behind a relay that keeps
    every byte, an old node that only knows the raw key, and node D (current, another key)."""
    key = "fleet-key-0123456789-abcdef"
    ui_a, ui_b, ui_d = free_port(), free_port(), free_port()
    users = [{"username": "admin", "password_hash": hash_password("clusteradmin1"),
              "role": "admin", "pwd_version": 1}]
    old = OldNode(key)
    pb, tb = start_node({"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_b}, "users": users,
                         "cluster": {"enabled": True, "key": key, "discovery_port": free_port()}})
    relay = Relay(ui_b)
    pd, td = start_node({"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_d}, "users": users,
                         "cluster": {"enabled": True, "key": "another-fleet-key-98765",
                                     "discovery_port": free_port()}})
    pa, ta = start_node({
        "admin_ui": {"bind_ip": "127.0.0.1", "port": ui_a}, "users": users,
        "cluster": {"enabled": True, "key": key, "discovery_port": free_port(),
                    "peers": [f"127.0.0.1:{relay.port}", f"127.0.0.1:{old.port}",
                              f"127.0.0.1:{ui_d}"]}})
    try:
        assert wait_port(ui_a) and wait_port(ui_b) and wait_port(ui_d), "nodes did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(f"http://127.0.0.1:{ui_a}/login", timeout=10)
        csrf = next(c.value for c in jar if c.name == "ser2net_csrf")
        op.open(urllib.request.Request(
            f"http://127.0.0.1:{ui_a}/login", method="POST",
            data=urllib.parse.urlencode({"username": "admin", "password": "clusteradmin1",
                                         "_csrf": csrf}).encode()), timeout=10)

        def status_html():
            return op.open(f"http://127.0.0.1:{ui_a}/api/cluster/status", timeout=15).read().decode()

        def rows(html):
            """{peer address: the text of its row}"""
            out = {}
            for m in re.finditer(r'<tr class="node-row[^"]*">(.*?)</tr>', html, re.S):
                addr = re.search(r'<span class="peer mono">([^<]*)</span>', m.group(1))
                out[addr.group(1) if addr else "?"] = m.group(1)
            return out

        html = status_html()
        r = rows(html)
        b_row, old_row, d_row = (r[f"127.0.0.1:{p}"] for p in (relay.port, old.port, ui_d))
        # B: signed, online; the key itself never went over the wire, in either direction
        assert "up " in b_row and "unsigned key" not in b_row and "state-error" not in b_row, b_row
        sent = b"".join(relay.sent)
        assert key.encode() not in sent and key.encode() not in b"".join(relay.got)
        assert b"\r\nx-cluster-auth: v2 " in sent.lower() and b"x-cluster-key" not in sent.lower()
        # what was captured can't be used again: a replay of A's request is refused
        ok_req = next(req for req, back in zip(relay.sent, relay.got, strict=False)
                      if b"GET /api/cluster/local" in req and back.startswith(b"HTTP/1.1 200"))
        with socket.create_connection(("127.0.0.1", ui_b), timeout=10) as c:
            c.sendall(ok_req)
            answer = b""
            while chunk := c.recv(65536):
                answer += chunk
        assert answer.startswith(b"HTTP/1.1 403") and b'"replay"' in answer, answer[:300]
        print("current nodes: requests signed, the key never on the wire, a captured request "
              "can't be replayed  OK")

        # the old node: shown with the unsigned key, after a refused signed first attempt
        assert "old-node" in old_row and "unsigned key" in old_row, old_row
        assert "x-cluster-key" not in old.requests[0] and "x-cluster-auth" in old.requests[0]
        assert old.requests[1].get("x-cluster-key") == key
        assert "Nodes running 2.8.0 or older get the cluster key unsigned" in html and \
            f"127.0.0.1:{old.port}" in html.split("Nodes running 2.8.0 or older")[1][:300]
        # D has another key: shown as such, not as "offline"
        assert "cluster key rejected — check the key on that node" in d_row, d_row
        print("an old node gets the raw key (tagged, with a banner); a node with another key "
              "shows \"key rejected\"  OK")

        # legacy authentication off on A: the old node gets no key any more
        st = op.open(urllib.request.Request(
            f"http://127.0.0.1:{ui_a}/settings/cluster", method="POST",
            data=urllib.parse.urlencode({
                "_csrf": csrf, "cluster_form": "2", "enabled": "on", "key": key, "discovery": "on",
                "discovery_port": str(free_port()),
                "peers": "\n".join(f"127.0.0.1:{p}" for p in (relay.port, old.port, ui_d))
            }).encode()), timeout=10).status
        assert st == 200
        n = len(old.requests)
        old_row = rows(status_html())[f"127.0.0.1:{old.port}"]
        assert "2.8.0 or older — legacy authentication is off on this node" in old_row, old_row
        assert old.requests[n:] and all("x-cluster-key" not in h for h in old.requests[n:]), \
            old.requests[n:]
        d_log = open(os.path.join(td, "all.log"), encoding="utf-8").read()
        assert d_log.count("refused GET /api/cluster/local from 127.0.0.1: a signature") == 1, \
            d_log[-2000:]
        print("legacy authentication off: the old node is no longer sent the key; the node with "
              "another key logged the refusal once  OK")
    finally:
        relay.close()
        old.close()
        for proc, tmp in ((pa, ta), (pb, tb), (pd, td)):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_discovery_unit()
    test_endpoints()
    test_manual_peer_and_remote_control()
    test_edit_on_a_peer_with_its_own_id()
    test_signed_legacy_and_mismatch()


if __name__ == "__main__":
    main()
