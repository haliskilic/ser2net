"""How a node calls its cluster peers and checks their calls: where a call may go, how
it is authenticated, and what a beacon may change.

  * a peer URL is only built for http/https, an IP address or host name (IPv6 in
    brackets) and a port 1..65535, with an encoded query — no value can turn it into
    a URL for another host;
  * a call only goes to a discovered or configured peer (find_target), never to an
    address or scheme taken from the browser's request;
  * a beacon can only name an IP address; a loopback or wildcard one (a node that
    could not tell its LAN address) falls back to the packet's source address - for a
    current sender only one of the addresses its signed beacon lists as its own;
  * a manual peer entry saved from Settings must name an IP address or host name,
    while validate() stays lenient for entries an older release accepted;
  * the cluster key may hold any characters: it is NFKC-normalised and used as UTF-8
    bytes, so an ASCII key keeps exactly the bytes older releases send and check;
  * requests are signed, not keyed: the signature covers the method, path and query,
    body, both node ids, the time and a nonce; a replayed, altered, misdirected or
    stale request is refused, refusals after a valid signature are signed (the
    caller learns the node's id and clock and sends again), replies are signed for
    their request, and the nonce store is bounded and fails closed;
  * nodes up to 2.8.0 only know the raw key (X-Cluster-Key): it is accepted and sent
    only while legacy authentication is on, and sent only to a peer whose beacon says
    it is older (at the address that beacon advertises, never at the one it came
    from) or a manual one that refused a signed request as older nodes do - never to
    one that answered a signed request;
  * during a planned key change the previous key is accepted (until its end) and
    tried with a peer that refuses the key;
  * beacons: an old one (a replay, an earlier run of a node) is dropped, also after
    discovery restarts (a settings save) - unless nothing was heard from the node for
    longer than a peer lives (a restart that set its clock back); a restart replaces
    the row, copies of one installation (two runs of one id) show up as such, a beacon
    with this node's own id from another run is reported, one signed with another key
    is noted but never contacted;
  * a malformed beacon is dropped without an error;
  * peer calls connect directly - HTTP(S)_PROXY / ALL_PROXY never apply - and don't
    follow redirects; why a peer's requests fail is logged once per change;
  * the fleet view's per-peer answer is shared by every open dashboard for a few
    seconds, with one request per peer in flight, dropped after a remote change; peer
    requests run on their own small thread pool and a reply has a size limit.

The network checks use small HTTP servers on 127.0.0.1 standing in for peers: a current
node (its own ClusterDiscovery checks each request and signs the reply), a node
up to 2.8.0 (it only checks X-Cluster-Key), a proxy and a host nothing should reach.

Run: python3 tests/test_cluster_peer_calls.py
"""
import asyncio
import hashlib
import hmac
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import AppConfig, ClusterSettings, ConfigError, parse_cluster_peer, peer_host
from app.engine import cluster as cluster_mod
from app.engine.cluster import (AUTH_HEADER, BOOT_SKEW_S, CLOCK_WINDOW_S, FETCH_CACHE_S,
                                KEY_HEADER, NOTE_TTL, PEER_TTL, ClusterDiscovery, _Answer,
                                _Link, _Nonces, _sign,
                                canonical_target, format_auth, key_bytes, key_header,
                                key_header_matches, parse_auth, peer_addr, peer_url, reply_mac,
                                request_mac)

KEY = "shared-key-for-the-tests"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


class FakePeer:
    """An HTTP server on 127.0.0.1 that records requests and answers with
    respond(request) -> (status, headers, body) - by default reply(path); stands in
    for a peer, a proxy or a host nothing should reach."""

    def __init__(self, reply=None, port=0):
        self.requests = []
        self.reply = reply or (lambda path: (200, {}, b'{"id": "p1", "mappings": []}'))
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_one(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = {"method": self.command, "path": self.path, "headers": self.headers,
                       "body": self.rfile.read(n) if n else b""}
                outer.requests.append(req)
                status, headers, body = outer.respond(req)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_CONNECT = handle_one

            def log_message(self, *args):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def respond(self, req):
        return self.reply(req["path"])

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class SignedPeer(FakePeer):
    """A peer that authenticates as a current node does: its own ClusterDiscovery checks
    every request (and tallies it) and signs the reply; reply(path) answers the
    accepted ones."""

    def __init__(self, reply=None, key=KEY, instance_id="peer2222", port=0, **cluster):
        cfg = AppConfig()
        cfg.instance_id = instance_id
        cfg.cluster = ClusterSettings(enabled=True, key=key, **cluster)
        self.node = ClusterDiscovery(cfg, lambda m: None)
        self.auths = []
        super().__init__(reply, port)

    def respond(self, req):
        path, _sep, query = req["path"].partition("?")
        a = self.node.check_request(req["method"], urllib.parse.unquote(path), query,
                                    req["body"], req["headers"], "127.0.0.1")
        self.auths.append(a)
        if a.ok:
            status, headers, body = self.reply(req["path"])
        else:
            status, headers = 403, {}
            body = json.dumps({"error": "forbidden", "reason": a.reason}).encode()
        return status, {**headers, AUTH_HEADER: self.node.reply_auth(a, status, body)}, body


class LegacyPeer(FakePeer):
    """A peer as releases up to 2.8.0 are: it only checks X-Cluster-Key and refuses with a
    plain 403 (no X-Cluster-Auth)."""

    def __init__(self, reply=None, key=KEY, port=0):
        self.key = key
        super().__init__(reply, port)

    def respond(self, req):
        if key_header_matches(self.key, req["headers"].get(KEY_HEADER)):
            return self.reply(req["path"])
        return 403, {}, b'{"error": "forbidden"}'


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def node(peers=(), key=KEY, log=None, instance_id="self0000", **cluster):
    cfg = AppConfig()
    cfg.instance_id = instance_id
    cfg.cluster = ClusterSettings(enabled=True, key=key, discovery_port=41750, peers=list(peers),
                                  **cluster)
    return ClusterDiscovery(cfg, log or (lambda m: None))


_T = [time.time() - 3600]


def beacon(key=KEY, **fields):
    """A signed v1 beacon (as releases up to 2.8.0 send), dated after the previous one
    unless "t" is given."""
    _T[0] += 1
    payload = {"v": 1, "id": "peer1111", "name": "edge", "ip": "10.0.0.5", "port": 8080,
               "scheme": "http", "t": _T[0], **fields}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps({"d": body, "s": _sign(key, body)}).encode()


def beacon2(key=KEY, **fields):
    """A signed beacon as a current node sends it (run id "b", own addresses "ips")."""
    return beacon(key, **{"v": 2, "b": "run0000000000001", "ips": ["10.0.0.5"], **fields})


def signed_headers(key, method, url, body=b"", to="peer2222", sender="self0000", ts=None,
                   nonce=None, **override):
    """X-Cluster-Auth for a request, as ClusterDiscovery._http builds it."""
    parts = urllib.parse.urlsplit(url)
    target = canonical_target(urllib.parse.unquote(parts.path), parts.query)
    ts = f"{time.time() if ts is None else ts:.3f}"
    nonce = nonce or secrets.token_hex(16)
    params = {"from": sender, "to": to, "ts": ts, "nonce": nonce,
              "sig": request_mac(key, method, target, body, sender, to, ts, nonce)}
    params.update(override)
    return {AUTH_HEADER: format_auth(params)}


def check(n, method, url, body=b"", headers=None, client="10.0.0.4"):
    parts = urllib.parse.urlsplit(url)
    return n.check_request(method, urllib.parse.unquote(parts.path), parts.query, body,
                           headers or {}, client)


def test_peer_host_and_url():
    for given, canon in (("10.0.5.12", "10.0.5.12"), ("[fd00::5]", "fd00::5"),
                         ("FD00:0::5", "fd00::5"), ("fe80::1%eth0", "fe80::1%eth0"),
                         ("plc-gw.example", "plc-gw.example"), ("node_2.local", "node_2.local")):
        assert peer_host(given) == canon, (given, peer_host(given))
    for bad in ("", "a b", "evil.example/x?", "user@host", "h#f", "h:1", "-bad", "a..b",
                "http://attacker.example/?x=", "fe80::1%x/y", "10.0.0.1%eth0", "x" * 64):
        assert peer_host(bad) is None, bad

    assert peer_url("http", "10.0.0.5", 8080, "/api/cluster/local") == \
        "http://10.0.0.5:8080/api/cluster/local"
    assert peer_url("https", "fd00::5", "8443") == "https://[fd00::5]:8443/"
    assert peer_url("http", "h", 1, "/x", {"mid": 'a"b&c#d e'}) == "http://h:1/x?mid=a%22b%26c%23d+e"
    for scheme, host, port in (("http://attacker.example/?x=", "10.0.0.5", 8080),
                               ("javascript", "10.0.0.5", 8080), ("ftp", "10.0.0.5", 21),
                               ("http", "evil.example/x?", 80), ("http", "10.0.0.5", 0),
                               ("http", "10.0.0.5", 70000), ("http", "10.0.0.5", "8080x")):
        try:
            peer_url(scheme, host, port)
        except ValueError:
            continue
        raise AssertionError(f"peer_url accepted {scheme!r} {host!r} {port!r}")
    assert peer_addr("fd00::5", 8080) == "[fd00::5]:8080" and peer_addr("h", 1) == "h:1"
    print("peer URLs: http(s) + IP/host name + port only, IPv6 bracketed, query encoded  OK")


def test_manual_peer_entries():
    assert parse_cluster_peer("[fd00::5]:8080", strict=True) == ("http", "[fd00::5]", 8080)
    assert parse_cluster_peer("https://plc-gw.example:8443", strict=True)[1] == "plc-gw.example"
    for bad in ("https://user@plc:8443", "evil.example/x?:80", "a b:80"):
        try:
            parse_cluster_peer(bad, strict=True)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"Settings must refuse the peer entry {bad!r}")
        parse_cluster_peer(bad)  # lenient, as before: a config that has it still loads
    ClusterSettings(enabled=True, key=KEY, peers=["https://user@plc:8443"]).validate()
    print("manual peers: Settings needs an IP/host name; old entries still load  OK")


def test_settings_fields():
    c = ClusterSettings.from_dict({"enabled": True, "key": KEY})
    assert (c.previous_key, c.previous_until, c.legacy_auth, c.discovery) == ("", 0.0, True, True)
    odd = ClusterSettings.from_dict({"enabled": True, "key": KEY, "previous_key": 5,
                                     "previous_until": "soon", "legacy_auth": "no",
                                     "discovery": None, "unknown_later_field": 1})
    assert (odd.previous_key, odd.previous_until, odd.legacy_auth, odd.discovery) == \
        ("", 0.0, True, True), odd
    odd.validate()
    assert ClusterSettings.from_dict({"previous_until": float("nan")}).previous_until == 0.0
    print("cluster settings: the new fields are optional and an odd stored value loads  OK")


def test_find_target():
    d = node(peers=["[FD00::5]:8080", "plc-gw.example:8443", "10.0.0.9:8080"])
    d.handle_datagram(beacon(), ("10.0.0.5", 41750))
    assert d.find_target("10.0.0.5", 8080)["source"] == "auto"
    assert d.find_target("fd00::5", "8080")["ip"] == "fd00::5"
    assert d.find_target("[fd00::5]", 8080) == d.find_target("fd00::5", 8080) is not None
    assert d.find_target("plc-gw.example", 8443)["scheme"] == "http"
    for host, port in (("10.0.0.5", 8081), ("9.9.9.9", 1), ("plc-gw.example", "x"),
                       ("http://10.0.0.5", 8080), ("", 8080)):
        assert d.find_target(host, port) is None, (host, port)
    print("find_target: only discovered/configured peers, IPv6 with or without brackets  OK")


def test_beacon_address():
    d = node()
    # a usable advertised IP is kept; the packet's source is only a fallback
    d.handle_datagram(beacon(ip="10.0.0.5"), ("10.0.0.99", 41750))
    assert d.peers()[0]["ip"] == "10.0.0.5"
    for unusable in ("127.0.0.1", "127.0.1.1", "::1", "0.0.0.0", "::", "", "localhost",
                     "evil.example", "http://evil.example/?x="):
        d._peers.clear()
        d.handle_datagram(beacon(ip=unusable), ("10.0.0.7", 41750))
        assert [p["ip"] for p in d.peers()] == ["10.0.0.7"], (unusable, d.peers())
    for bad_port in (0, 70000, "x", None, True):
        d._peers.clear()
        d.handle_datagram(beacon(port=bad_port), ("10.0.0.7", 41750))
        assert d.peers() == [], (bad_port, d.peers())
    d.handle_datagram(beacon(port="8081"), ("10.0.0.7", 41750))
    assert d.peers()[0]["port"] == 8081
    # a current sender lists its own addresses: the fallback is only taken from one of them
    for ips, src, want in ((["10.0.0.7", "192.168.5.2"], "192.168.5.2", ["192.168.5.2"]),
                           (["10.0.0.7"], "10.6.6.6", []), ([], "10.0.0.8", ["10.0.0.8"])):
        d._peers.clear()
        d.handle_datagram(beacon2(ip="127.0.0.1", ips=ips), (src, 41750))
        assert [p["ip"] for p in d.peers()] == want, (ips, src, d.peers())
    print("beacons: only an IP address, loopback/wildcard -> packet source (current: one of its own"
          " addresses), bad port dropped  OK")


def test_beacon_replay_and_runs():
    d = node()
    old = beacon2(t=1000.0)
    d.handle_datagram(old, ("10.0.0.5", 41750))
    assert len(d.peers()) == 1
    # the same beacon again, or an older one of that run: dropped (a replay)
    d._peers.clear()
    for again in (old, beacon2(t=999.0)):
        d.handle_datagram(again, ("10.6.6.6", 41750))
        assert d.peers() == [], d.peers()
    d.handle_datagram(beacon2(t=1005.0), ("10.0.0.5", 41750))
    assert len(d.peers()) == 1
    # an earlier run of the node (another run id, dated well before the latest): dropped
    d.handle_datagram(beacon2(t=1005.0 - BOOT_SKEW_S - 1, b="earlier000000000", ip="10.6.6.6"),
                      ("10.6.6.6", 41750))
    assert [p["ip"] for p in d.peers()] == ["10.0.0.5"], d.peers()
    # a restart (a new run, newer): replaces the row, even at another address
    d.handle_datagram(beacon2(t=1010.0, b="run0000000000002", ip="10.0.0.6", ips=["10.0.0.6"]),
                      ("10.0.0.6", 41750))
    assert [p["ip"] for p in d.peers()] == ["10.0.0.6"] and d.shared_ids() == [], d.peers()
    # nodes up to 2.8.0 send no run id: one stream per node id, newer beacons only
    v1 = node()
    v1.handle_datagram(beacon(id="old1", t=50.0), ("10.0.1.1", 41750))
    v1._peers.clear()
    v1.handle_datagram(beacon(id="old1", t=50.0, ip="10.6.6.6"), ("10.6.6.6", 41750))
    assert v1.peers() == [], "a replayed beacon of a node up to 2.8.0 must be dropped"
    print("beacons: a replay or an earlier run is dropped, a restart replaces the row  OK")


def test_copies_of_one_installation():
    # a third node: two runs of one id that keep beaconing are copies; each gets a row
    d = node()
    a = {"b": "copyA00000000000", "ip": "10.0.0.5", "ips": ["10.0.0.5"]}
    b = {"b": "copyB00000000000", "ip": "10.0.0.6", "ips": ["10.0.0.6"], "name": "edge-copy"}
    d.handle_datagram(beacon2(id="dup1", **a), ("10.0.0.5", 41750))
    d.handle_datagram(beacon2(id="dup1", **b), ("10.0.0.6", 41750))
    d.handle_datagram(beacon2(id="dup1", **a), ("10.0.0.5", 41750))
    d.handle_datagram(beacon2(id="dup1", **b), ("10.0.0.6", 41750))
    assert sorted(p["ip"] for p in d.peers()) == ["10.0.0.5", "10.0.0.6"], d.peers()
    shared = d.shared_ids()
    assert len(shared) == 1 and shared[0]["addrs"] == ["10.0.0.5:8080", "10.0.0.6:8080"], shared
    assert d.find_target("10.0.0.5", 8080) and d.find_target("10.0.0.6", 8080), \
        "both copies stay reachable from the fleet view"

    # this node itself: a beacon with its own id from another run is a copy of it
    logs = []
    me = node(log=logs.append)
    me.handle_datagram(beacon2(id="self0000", b=me.boot, t=time.time()), ("10.0.0.2", 41750))
    assert me.copies() == [] and not logs, "this node's own beacon is no copy"
    me.handle_datagram(beacon2(id="self0000", t=me.started - 60, ip="10.0.0.9"), ("10.0.0.9", 41750))
    assert me.copies() == [] and not logs, "a beacon from before this node started is a replay"
    for _ in range(2):
        me.handle_datagram(beacon2(id="self0000", t=time.time(), ip="10.0.0.9", ips=["10.0.0.9"],
                                   name="golden-image"), ("10.0.0.9", 41750))
    assert [c["addr"] for c in me.copies()] == ["10.0.0.9:8080"], me.copies()
    assert len(logs) == 1 and "another node uses this node's identity" in logs[0] \
        and "golden-image" in logs[0], logs
    me.handle_datagram(beacon(id="self0000", t=time.time(), ip="10.0.0.8"), ("10.0.0.8", 41750))
    assert len(me.copies()) == 2, "a copy that runs 2.8.0 or older is seen too"
    assert me.peers() == [], "a copy is not listed as a peer of its own id"
    me._copies["10.0.0.9:8080"]["last_seen"] -= NOTE_TTL + 1
    assert [c["addr"] for c in me.copies()] == ["10.0.0.8:8080"]
    print("beacons: copies of one installation get a row each and are named; a copy of this"
          " node is reported, its own and replayed beacons are not  OK")


def test_other_key_beacons():
    d = node()
    for i in range(20):
        d.handle_datagram(beacon(key="another-key", id=f"n{i}"), (f"10.0.9.{i}", 41750))
    assert len(d.other_key_nodes()) == 16, "bounded"
    assert d.peers() == [], "a node with another key is never a peer"
    d.handle_datagram(beacon(key="another-key", id="x"), ("10.0.0.5", 41750))
    d.handle_datagram(beacon(), ("10.0.0.5", 41750))   # a valid beacon from it (a key change)
    assert "10.0.0.5" not in d.other_key_nodes()
    d.handle_datagram(b"garbage", ("10.0.8.8", 41750))
    assert "10.0.8.8" not in d.other_key_nodes(), "only what looks like a beacon counts"
    for ip in list(d._other_key):
        d._other_key[ip] -= NOTE_TTL + 1
    assert d.other_key_nodes() == []
    print("beacons signed with another key: their senders are noted (bounded, expiring)  OK")


def test_signed_requests():
    a, b = node(instance_id="self0000"), node(instance_id="peer2222")
    url = "http://10.0.0.5:8080/api/cluster/mapping-data?mid=ab%20c&x="
    h = signed_headers(KEY, "GET", url)
    ok = check(b, "GET", url, headers=h)
    assert ok.ok and not ok.legacy and ok.sender == "self0000", ok
    assert check(b, "GET", url, headers=h).reason == "replay", "a request is accepted once"
    # every signed part matters: another method, path, query, body or key
    for method, u, body, hdr in (
            ("POST", url, b"", signed_headers(KEY, "GET", url)),
            ("GET", url.replace("ab%20c", "abd"), b"", signed_headers(KEY, "GET", url)),
            ("GET", "http://h:1/api/cluster/local", b"", signed_headers(KEY, "GET", url)),
            ("POST", url, b"action=stop", signed_headers(KEY, "POST", url, b"action=start")),
            ("GET", url, b"", signed_headers("another-key", "GET", url))):
        r = check(b, method, u, body, hdr)
        assert not r.ok and r.reason == "signature", (method, u, body, r)
    # ... or another sender, recipient, time or nonce than the signed ones
    p = parse_auth(signed_headers(KEY, "GET", url)[AUTH_HEADER])
    for field, value in (("from", "evil"), ("to", "other999"), ("ts", f"{float(p['ts']) + 1:.3f}"),
                         ("nonce", secrets.token_hex(16))):
        forged = format_auth(dict(p, **{field: value}))
        assert check(b, "GET", url, headers={AUTH_HEADER: forged}).reason == "signature", field
    # meant for another node (or sent before this node's id was known): refused, signed
    for to in ("other999", ""):
        r = check(b, "GET", url, headers=signed_headers(KEY, "GET", url, to=to))
        assert r.reason == "recipient" and r.key == KEY and r.nonce, r
        reply = parse_auth(b.reply_auth(r, 403, b"{}"))
        assert reply["id"] == "peer2222" and hmac.compare_digest(
            reply["sig"], reply_mac(KEY, r.nonce, 403, b"{}", "peer2222", reply["now"]))
    # dated too far from this node's clock, or before this process started: refused, signed
    for ts in (time.time() - CLOCK_WINDOW_S - 5, time.time() + CLOCK_WINDOW_S + 5,
               b.started - 30):
        r = check(b, "GET", url, headers=signed_headers(KEY, "GET", url, ts=ts))
        assert r.reason == "stale" and parse_auth(b.reply_auth(r, 403, b"")).get("now"), (ts, r)
    # a sender that corrects for this node's clock is accepted whatever its own clock says
    assert check(b, "GET", url, headers=signed_headers(KEY, "GET", url, ts=time.time() + 30)).ok
    # malformed / missing / cluster off
    for value in ("v2", "v3 to=x", "v2 to=x,ts=1,nonce=zz,sig=00", "v2 ts=1;nonce=1",
                  "v2 to=peer2222,ts=inf,nonce=" + "0" * 32 + ",sig=00", "v2 a=1,a=2"):
        assert check(b, "GET", url, headers={AUTH_HEADER: value}).reason == "malformed", value
    assert check(b, "GET", url).reason == "missing"
    off = node(instance_id="peer2222")
    off.cluster.enabled = False
    assert check(off, "GET", url, headers=signed_headers(KEY, "GET", url)).reason == "off"
    # replies: signed for one request; a bare "v2" otherwise
    assert b.reply_auth(None, 200, b"") == "v2" and b.reply_auth(check(b, "GET", url), 403, b"") == "v2"
    assert a.reply_auth(ok, 200, b'{"x": 1}') != a.reply_auth(ok, 200, b'{"x": 2}')
    print("signed requests: every part signed, replay / other node / stale / malformed refused,"
          " refusals after a valid signature signed  OK")


def test_legacy_key_and_previous_key():
    url = "http://10.0.0.5:8080/api/cluster/local"
    b = node(instance_id="peer2222")
    first = check(b, "GET", url, headers={KEY_HEADER: key_header(KEY)}, client="10.0.0.7")
    again = check(b, "GET", url, headers={KEY_HEADER: key_header(KEY)}, client="10.0.0.7")
    assert first.ok and first.legacy and first.first and again.ok and not again.first
    assert b.legacy_nodes() == ["10.0.0.7"], "a node that sent the raw key is listed"
    wrong = check(b, "GET", url, headers={KEY_HEADER: "wrong"}, client="10.0.0.8")
    assert wrong.reason == "key" and wrong.first
    assert not check(b, "GET", url, headers={KEY_HEADER: "wrong"}, client="10.0.0.8").first, \
        "a refusal is reported once per change"
    assert check(b, "GET", url, headers=signed_headers(KEY, "GET", url), client="10.0.0.8").again
    b.cluster.legacy_auth = False
    assert check(b, "GET", url, headers={KEY_HEADER: key_header(KEY)}).reason == "legacy"
    assert check(b, "GET", url, headers=signed_headers(KEY, "GET", url)).ok

    # a planned key change: the previous key is still accepted, until its end
    new = node(instance_id="peer2222", key="the-new-key-0123456789", previous_key=KEY,
               previous_until=time.time() + 60)
    assert new.keys() == ["the-new-key-0123456789", KEY]
    assert check(new, "GET", url, headers=signed_headers(KEY, "GET", url)).key == KEY
    assert check(new, "GET", url, headers={KEY_HEADER: key_header(KEY)}).ok
    assert check(new, "GET", url, headers=signed_headers("the-new-key-0123456789", "GET", url)).ok
    new.cluster.previous_until = time.time() - 1
    assert new.keys() == ["the-new-key-0123456789"]
    assert check(new, "GET", url, headers=signed_headers(KEY, "GET", url)).reason == "signature"
    # beacons: verified with either key, and sent with both while the previous one lasts
    new.cluster.previous_until = 0.0
    new.handle_datagram(beacon(key=KEY, id="notyet"), ("10.0.0.5", 41750))
    assert new.peers()[0]["key"] == 1 and new.peer_state(new.all_targets()[0])["previous_key"]
    print("raw key: accepted only with legacy authentication on, reported once per address; "
          "previous key accepted until its end  OK")


def test_nonce_store():
    store, now = _Nonces(limit=4), time.time()
    assert all(store.add(f"n{i}", now - 4 + i, now) for i in range(4))
    assert not store.add("n0", now, now), "seen"
    assert store.add("n4", now + 0.5, now) and store.floor == now - 4, store.floor
    assert not store.add("late", now - 4, now), "dated at or before a forgotten nonce: refused"
    assert store.add("n5", now + 1, now)
    old = _Nonces(limit=2)
    old.add("a", now - CLOCK_WINDOW_S - 10, now)
    old.add("b", now - CLOCK_WINDOW_S - 5, now)
    assert old.add("c", now, now) and old.floor == 0.0, "expired nonces just go"
    print("nonce store: bounded, fails closed when full  OK")


def _attempts(peer):
    return [("auth" if AUTH_HEADER in r["headers"] else "") + ("key" if KEY_HEADER in r["headers"] else "")
            for r in peer.requests]


def test_calls_only_known_peers():
    d = node(peers=["10.0.0.9:8080", "https://user@x:1"])
    calls = []

    def fake_http(method, url, body, timeout, how):
        calls.append(url)
        return _Answer(status=200, data={"ok": True}, signed=True, v2=True, peer_id="p9")
    d._http = fake_http

    async def run():
        known = d.find_target("10.0.0.9", 8080)
        assert await d.get_peer(known, "/api/cluster/local") == {"ok": True}
        assert calls == ["http://10.0.0.9:8080/api/cluster/local"], calls
        calls.clear()
        for target in ({"scheme": "http", "ip": "9.9.9.9", "port": 80},             # unknown
                       {**known, "scheme": "http://attacker.example/?x="},          # other scheme
                       {"scheme": "https", "ip": "user@x", "port": 1}):             # no address
            assert await d.get_peer(target, "/api/cluster/local") is None, target
            assert await d.post_peer(target, "/api/cluster/control", {"a": "b"}) is None
        assert calls == [], calls
    asyncio.run(run())
    print("peer calls go only to discovered/configured peers at a valid address  OK")


def test_signed_peer_calls():
    hits = []

    def reply(path):
        hits.append(path)
        return 200, {}, json.dumps({"id": "peer2222", "path": path}).encode()
    peer = SignedPeer(reply)
    logs = []
    d = node(peers=[f"127.0.0.1:{peer.port}"], log=logs.append)
    t = d.all_targets()[0]

    async def run():
        # first contact: the peer's id is learned from its signed refusal, then the call
        assert (await d.get_peer(t, "/api/cluster/local"))["id"] == "peer2222"
        assert [a.reason for a in peer.auths] == ["recipient", ""] and len(hits) == 1
        assert _attempts(peer) == ["auth", "auth"], "the key itself is never sent"
        assert d.peer_state(t) == {"auth": "v2", "problem": "", "previous_key": False}
        # later calls: one signed request each, POST bodies too
        assert (await d.post_peer(t, "/api/cluster/control", {"action": "stop"}))["path"]
        assert len(peer.requests) == 3 and peer.auths[-1].ok
        assert b"action=stop" == peer.requests[-1]["body"]
        # a reply that is not signed for the request (altered, forged) is not used
        saved = peer.respond

        def altered(req):    # signed by the peer, then changed on the way
            status, headers, body = saved(req)
            return status, headers, body.replace(b"peer2222", b"evil2222")
        peer.respond = lambda req: (200, {AUTH_HEADER: "v2"}, b'{"id": "forged"}')
        assert await d.get_peer(t, "/api/cluster/local") is None
        assert d.peer_state(t)["problem"] == "unsigned" and "not signed" in logs[-1], logs
        peer.respond = altered
        assert await d.get_peer(t, "/api/cluster/local") is None, "an altered reply is not used"
        peer.respond = saved
        # a wrong idea of the peer's clock: its signed "stale" refusal corrects it
        d._links[cluster_mod._target_key(t)].offset = 3600.0
        assert (await d.get_peer(t, "/api/cluster/local"))["id"] == "peer2222"
        assert peer.auths[-2].reason == "stale" and peer.auths[-1].ok
        assert abs(d._links[cluster_mod._target_key(t)].offset) < 5
        # an auto-discovered peer whose clock is an hour ahead: the beacon tells, and
        # the requests are dated for that clock (refused here, as this test peer's clock
        # is ours - then corrected by the signed refusal)
        auto = node(log=logs.append)
        auto.handle_datagram(beacon2(id="peer2222", ip="127.0.0.1", ips=["127.0.0.1"],
                                     port=peer.port, t=time.time() + 3600), ("127.0.0.1", 41750))
        ta = auto.all_targets()[0]
        assert ta["offset"] > 3500, ta
        assert (await auto.get_peer(ta, "/api/cluster/local"))["id"] == "peer2222"
        assert [a.reason for a in peer.auths[-2:]] == ["stale", ""]
        await auto.stop()
        await d.stop()
    try:
        asyncio.run(run())
    finally:
        peer.close()
    print("signed calls: id learned from a signed refusal, no key on the wire, unsigned or "
          "altered replies dropped, the peer's clock learned  OK")


def test_key_mismatch_and_rotation():
    other = SignedPeer(key="a-completely-different-key")
    off = SignedPeer()
    off.node.cluster.enabled = False
    rotated = SignedPeer(key="the-new-key-0123456789")
    logs = []
    d = node(peers=[f"127.0.0.1:{p.port}" for p in (other, off, rotated)], log=logs.append)
    t_other, t_off, t_rot = d.all_targets()

    async def run():
        assert await d.get_peer(t_other, "/api/cluster/local") is None
        assert d.peer_state(t_other)["problem"] == "key", d.peer_state(t_other)
        assert _attempts(other) == ["auth"], "a node that refuses the key is not sent it unsigned"
        assert await d.get_peer(t_off, "/api/cluster/local") is None
        assert d.peer_state(t_off)["problem"] == "cluster-off"
        # this node changed its key, the peer has it already: the previous one is not needed;
        # the other way round (the peer not changed yet), the previous key is tried and kept
        d.cluster.key, d.cluster.previous_key = "the-new-key-0123456789", KEY
        d.cluster.previous_until = time.time() + 60
        assert (await d.get_peer(t_rot, "/api/cluster/local")) is not None
        assert not d.peer_state(t_rot)["previous_key"]
        late = SignedPeer()          # still has the old key (KEY)
        d.cluster.peers.append(f"127.0.0.1:{late.port}")
        t_late = d.all_targets()[-1]
        try:
            assert (await d.get_peer(t_late, "/api/cluster/local")) is not None
            assert d.peer_state(t_late)["previous_key"], "shown as not changed yet"
            n = len(late.requests)
            assert (await d.get_peer(t_late, "/api/cluster/local")) is not None
            assert len(late.requests) == n + 1, "the key that worked is remembered"
        finally:
            late.close()
        await d.stop()
    try:
        asyncio.run(run())
    finally:
        for p in (other, off, rotated):
            p.close()
    assert any("refused this node's cluster key" in m for m in logs), logs
    print("key mismatch shown per peer (also a peer whose cluster is off); a key change tries "
          "the previous key and remembers what works  OK")


def test_legacy_peer_calls():
    legacy = LegacyPeer(lambda path: (200, {}, b'{"id": "old1", "mappings": []}'))
    logs = []
    d = node(peers=[f"127.0.0.1:{legacy.port}"], log=logs.append)
    t = d.all_targets()[0]

    async def run():
        # a manual peer is signed to first; it refuses as releases up to 2.8.0 do -> the raw key
        assert (await d.get_peer(t, "/api/cluster/local"))["id"] == "old1"
        assert _attempts(legacy) == ["auth", "key"], _attempts(legacy)
        assert d.peer_state(t)["auth"] == "legacy" and d.legacy_nodes() == [f"127.0.0.1:{legacy.port}"]
        assert (await d.get_peer(t, "/api/cluster/local"))["id"] == "old1"
        assert _attempts(legacy)[-1] == "key", "it is remembered as an older node"
        # legacy authentication off: the raw key is never sent
        d.cluster.legacy_auth = False
        legacy.requests.clear()
        assert await d.get_peer(t, "/api/cluster/local") is None
        assert legacy.requests == [] and d.peer_state(t)["problem"] == "legacy-off"
        assert d.legacy_nodes() == [], "nothing gets the unsigned key any more"
        d.cluster.legacy_auth = True
        await d.stop()

        # a manual peer that has answered a signed request is never sent the raw key, even
        # if something at its address later refuses as an older node does
        signed = SignedPeer()
        e = node(peers=[f"127.0.0.1:{signed.port}"])
        te = e.all_targets()[0]
        try:
            assert await e.get_peer(te, "/api/cluster/local") is not None
        finally:
            signed.close()
        imposter = LegacyPeer(port=signed.port)
        try:
            assert await e.get_peer(te, "/api/cluster/local") is None
            assert "key" not in "".join(_attempts(imposter)), "no downgrade to the raw key"
            assert e.peer_state(te)["problem"] == "key"
        finally:
            imposter.close()
        await e.stop()

        # an auto-discovered node up to 2.8.0 (its beacon says v1) gets the raw key at once,
        # at the address its beacon advertises - not at the one the beacon came from
        f, sent = node(), []

        def fake_http(method, url, body, timeout, how):
            sent.append((url, how["mode"]))
            return _Answer(status=200, data={"id": "old2"})
        f._http = fake_http
        f.handle_datagram(beacon(id="old2", ip="10.0.0.44", port=8080), ("10.6.6.6", 41750))
        tf = f.all_targets()[0]
        assert (await f.get_peer(tf, "/api/cluster/local"))["id"] == "old2"
        assert sent == [("http://10.0.0.44:8080/api/cluster/local", "legacy")], sent
        f.cluster.legacy_auth = False
        sent.clear()
        assert await f.get_peer(tf, "/api/cluster/local") is None and sent == []
        await f.stop()
    try:
        asyncio.run(run())
    finally:
        legacy.close()
    print("older peers: the raw key only for them (manual: after a refusal as they refuse; "
          "auto: by their beacon, at the address it advertises), never after a signed "
          "answer, never with legacy authentication off  OK")


class _Clock:
    """The cluster module's clock, *offset* seconds ahead (when a beacon is received)."""

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return time.time() + self.offset

    def monotonic(self):
        return time.monotonic() + self.offset


def test_replayed_old_beacons_get_no_key():
    """A node up to 2.8.0 that advertises 127.0.0.1 (no default route): its beacons can be
    replayed from anywhere, so captured ones (validly signed, increasing times) sent from
    another address, received several seconds apart like real ones, with nothing
    remembered about the node yet (as right after a restart), make this node send
    nothing there - let alone the raw key."""
    trap = LegacyPeer(lambda path: (200, {}, b'{"id": "old3"}'))   # the replaying host
    logs = []
    d, clock = node(log=logs.append), _Clock()

    def replayed(t):
        d.handle_datagram(beacon(id="old3", ip="127.0.0.1", port=trap.port, t=t),
                          ("127.0.0.1", 41750))

    async def run():
        for i, t in enumerate((1000.0, 1006.0, 1012.0)):
            clock.offset = 7.0 * i     # received 7 s apart
            replayed(t)
        target = d.all_targets()[0]
        assert target["from_packet"] and not target["configured"], target
        assert d._how(target, _Link()) == "manual-only"
        assert await d.get_peer(target, "/api/cluster/local") is None
        assert trap.requests == [], "nothing may go to the address a beacon came from"
        assert d.peer_state(target)["problem"] == "manual-only" and "manual peer" in logs[-1]
        # a settings save (discovery restarts) doesn't make the old beacons new again
        await d.stop()
        replayed(1012.0)
        assert d.peers() == [], d.peers()
        # added as a manual peer at that address, where the admin says it is: the raw key
        # goes there
        d.cluster.peers.append(f"127.0.0.1:{trap.port}")
        replayed(1018.0)
        target = d.all_targets()[0]
        assert target["source"] == "auto" and target["configured"], target
        assert (await d.get_peer(target, "/api/cluster/local"))["id"] == "old3"
        assert _attempts(trap) == ["key"], _attempts(trap)
        await d.stop()
    saved, cluster_mod.time = cluster_mod.time, clock
    try:
        asyncio.run(run())
    finally:
        cluster_mod.time = saved
        trap.close()
    print("an older node that doesn't advertise its address: replayed beacons from another "
          "address get no request, the raw key only goes to it as a manual peer  OK")


def test_reboot_with_clock_set_back():
    """A node that restarts with its clock set back (no RTC, NTP not synced yet) looks like
    a replay only while its row is live: once nothing was heard from it for longer than a
    peer lives, it is accepted again - a node up to 2.8.0 and a current one alike."""
    d = node()
    before = (beacon(id="rtc1", ip="10.0.0.21", t=50000.0),
              beacon2(id="rtc2", ip="10.0.0.22", ips=["10.0.0.22"], t=50000.0))
    after = (beacon(id="rtc1", ip="10.0.0.21", t=46400.0),     # an hour back
             beacon2(id="rtc2", ip="10.0.0.22", ips=["10.0.0.22"], b="afterreboot00001",
                     t=46400.0))
    for data in before:
        d.handle_datagram(data, ("10.0.0.2", 41750))
    assert sorted(p["id"] for p in d.peers()) == ["rtc1", "rtc2"]
    d._peers.clear()            # (they stopped beaconing while they restarted)
    for data in after:
        d.handle_datagram(data, ("10.0.0.2", 41750))
    assert d.peers() == [], "right after their last beacon, an older time is a replay"
    for rec in d._ids.values():  # nothing heard from them for longer than a peer lives
        rec["seen"] -= PEER_TTL + 1
    for data in after:
        d.handle_datagram(data, ("10.0.0.2", 41750))
    assert sorted(p["id"] for p in d.peers()) == ["rtc1", "rtc2"], d.peers()
    d.handle_datagram(beacon(id="rtc1", ip="10.0.0.21", t=46405.0), ("10.0.0.2", 41750))
    d.handle_datagram(after[0], ("10.6.6.6", 41750))
    assert next(p for p in d.peers() if p["id"] == "rtc1")["offset"] < -3000, \
        "its newer beacons count, a repeat of an older one does not"
    print("a node that restarts with its clock set back shows up again once it was silent "
          "longer than a peer lives  OK")


TR_KEY = "küme-anahtarı-çöüşğİı"


def test_cluster_key():
    # an ASCII key keeps the bytes every earlier release sends and checks
    for k in ("rc-key-xyz", "a long key with spaces & symbols !#$%"):
        assert key_bytes(k) == k.encode("ascii") and key_header(k) == k, k
    # any characters, typed composed, pasted decomposed (NFD) or with a no-break space
    nfd = unicodedata.normalize("NFD", TR_KEY)
    assert nfd != TR_KEY and key_bytes(nfd) == key_bytes(TR_KEY) == TR_KEY.encode("utf-8")
    assert key_bytes("iki kelime") == key_bytes("iki kelime")
    # the header value http.client sends (latin-1) carries exactly the key's bytes
    assert key_header(TR_KEY).encode("latin-1") == TR_KEY.encode("utf-8")
    assert key_header_matches(TR_KEY, key_header(nfd)) and key_header_matches(nfd, key_header(TR_KEY))
    assert key_header_matches("rc-key-xyz", "rc-key-xyz")  # what v2.7/v2.8 peers send
    for wrong in ("", None, "rc-key-xy", TR_KEY, "çöü", "Ā", key_header("çöü")):
        assert not key_header_matches(TR_KEY, wrong), wrong
    assert not key_header_matches("", "")
    # the same key in another normal form signs beacons and requests the same way
    assert _sign(nfd, "body") == _sign(TR_KEY, "body")
    assert request_mac(nfd, "GET", "/x", b"", "a", "b", "1", "n") == \
        request_mac(TR_KEY, "GET", "/x", b"", "a", "b", "1", "n")
    assert _sign("rc-key-xyz", "body") == hmac.new(b"rc-key-xyz", b"body", hashlib.sha256) \
        .hexdigest(), "ASCII beacon signatures unchanged"
    # the request key is derived: no request signature is a beacon signature
    assert request_mac("rc-key-xyz", "GET", "/x", b"", "a", "b", "1", "n") != \
        hmac.new(b"rc-key-xyz", b"GET\n/x", hashlib.sha256).hexdigest()
    d = node(key=TR_KEY)
    d.handle_datagram(beacon(key=nfd), ("10.0.0.5", 41750))
    assert [p["id"] for p in d.peers()] == ["peer1111"]
    print("cluster key: any characters (NFKC + UTF-8 bytes), ASCII keys byte-identical  OK")


def test_malformed_beacons():
    d = node()
    signed_list = json.dumps({"d": "[]", "s": _sign(KEY, "[]")}).encode()
    for data in (b"\xff\xfe", b"[]", b'"x"', b"7", b'{"d": 1, "s": "00"}',
                 b'{"d": "{}", "s": 5}', json.dumps({"d": "{}", "s": "ü"}).encode(),
                 b'{"d": "\\ud800", "s": "00"}', ("[" * 30000 + "]" * 30000).encode(),
                 signed_list, beacon(ip={"x": 1}, port=[8080]), beacon(t="soon"),
                 beacon(t=float("inf")), beacon(t=True), beacon2(b={"x": 1}, ips="10.0.0.5")):
        d.handle_datagram(data, ("10.0.0.9", 41750))   # must not raise
    assert len(d.peers()) <= 1 and all(p["ip"] == "10.0.0.5" for p in d.peers()), d.peers()
    print("malformed beacons (types, non-ASCII signature, surrogates, nesting, times) dropped  OK")


def test_direct_no_redirect():
    proxy = FakePeer()
    peer = LegacyPeer(key=TR_KEY)   # (its raw key shows the bytes that go out)
    saved = {k: os.environ.get(k) for k in PROXY_VARS}
    for k in PROXY_VARS[:3]:   # as set so that webhooks / SSO reach the Internet
        os.environ[k] = f"http://127.0.0.1:{proxy.port}"
    os.environ.pop("NO_PROXY", None)
    try:
        logs = []
        d = node(peers=[f"127.0.0.1:{peer.port}"], key=TR_KEY, log=logs.append)
        t = d.all_targets()[0]
        assert asyncio.run(d.get_peer(t, "/api/cluster/local"))["id"] == "p1"
        assert len(peer.requests) == 2 and not proxy.requests, "must connect directly"
        sent = peer.requests[1]["headers"][KEY_HEADER].encode("latin-1")
        assert sent == TR_KEY.encode("utf-8"), sent
        # a redirect is not followed (the request would go along)
        peer.respond = lambda req: (302, {"Location": f"http://127.0.0.1:{proxy.port}/x"}, b"")
        assert asyncio.run(d.get_peer(t, "/api/cluster/local")) is None
        assert not proxy.requests, "a redirect must not be followed"
        assert any("redirects are not followed" in m for m in logs), logs
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        proxy.close()
        peer.close()
    print("peer calls: direct despite HTTP(S)_PROXY/ALL_PROXY, redirects not followed  OK")


def test_failures_logged_once():
    logs = []
    port = free_port()   # nothing listens here yet
    d = node(peers=[f"127.0.0.1:{port}"], log=logs.append)
    t = d.all_targets()[0]

    async def calls(n):
        # (a long timeout: on Windows a refused connection to loopback takes ~2 s)
        return [await d.get_peer(t, "/api/cluster/local", timeout=8) for _ in range(n)]
    assert asyncio.run(calls(3)) == [None] * 3
    failed = [m for m in logs if "failed" in m]
    assert len(failed) == 1 and f"127.0.0.1:{port}" in failed[0], logs
    peer = FakePeer(lambda path: (403, {}, b'{"error": "forbidden"}'), port=port)
    try:
        assert asyncio.run(calls(2)) == [None] * 2
        assert len(logs) == 2 and "refused this node's cluster key" in logs[1], logs
        peer.reply = lambda path: (200, {}, b'{"id": "p1"}')
        assert asyncio.run(calls(2))[0] == {"id": "p1"}
        assert len(logs) == 3 and logs[2].endswith("answers again"), logs
        # a refusal with the peer's own reason (400) is an answer, not a failure
        peer.reply = lambda path: (400, {}, b'{"ok": false, "error": "port in use"}')
        assert asyncio.run(d.post_peer(t, "/api/cluster/mapping-save", {"id": "x"})) == \
            {"ok": False, "error": "port in use"}
        assert len(logs) == 3, logs
    finally:
        peer.close()
    print("peer request failures: logged once per change, recovery logged  OK")


def test_shared_answers():
    gate, hits = threading.Event(), []

    def reply(path):
        hits.append(path)
        n = len(hits)
        gate.wait(5)
        return 200, {}, json.dumps({"n": n}).encode()
    peer = SignedPeer(reply)
    down = FakePeer(lambda path: (503, {}, b""))
    logs = []
    d = node(peers=[f"127.0.0.1:{peer.port}", f"127.0.0.1:{down.port}"], log=logs.append)
    t, t_down = d.all_targets()

    async def run():
        tabs = [asyncio.create_task(d.fetch_peer(t)) for _ in range(10)]  # ten dashboards
        await asyncio.sleep(0.3)
        gate.set()
        assert await asyncio.gather(*tabs) == [{"n": 1}] * 10 and len(hits) == 1, hits
        assert await d.fetch_peer(t) == {"n": 1} and len(hits) == 1, "answer is reused"
        assert any(th.name.startswith("cluster-http") for th in threading.enumerate())
        d.forget(t)   # after a remote start/stop/edit
        assert await d.fetch_peer(t) == {"n": 2}, "asked again after forget()"
        k = next(iter(d._fetched))
        d._fetched[k] = (d._fetched[k][0] - FETCH_CACHE_S - 1, d._fetched[k][1])
        assert await d.fetch_peer(t) == {"n": 3}, "an old answer is not reused"
        # an answer to a request that started before forget() is not kept
        gate.clear()
        d.forget(t)
        first = asyncio.create_task(d.fetch_peer(t))
        await asyncio.sleep(0.2)
        d.forget(t)
        second = asyncio.create_task(d.fetch_peer(t))
        await asyncio.sleep(0.2)
        gate.set()
        assert await first == {"n": 4} and await second == {"n": 5}
        assert await d.fetch_peer(t) == {"n": 5} and len(hits) == 5, hits
        # "no answer" is shared too, so a dead peer is asked once, not once per tab
        assert await asyncio.gather(*[d.fetch_peer(t_down) for _ in range(5)]) == [None] * 5
        assert await d.fetch_peer(t_down) is None and len(down.requests) == 1, down.requests
        # a reply over the size limit is refused
        saved = cluster_mod.MAX_REPLY_BYTES
        cluster_mod.MAX_REPLY_BYTES = 64
        try:
            peer.reply = lambda path: (200, {}, json.dumps({"x": "y" * 100}).encode())
            assert await d.get_peer(t, "/api/cluster/local") is None
            assert any("reply larger than" in m for m in logs), logs
        finally:
            cluster_mod.MAX_REPLY_BYTES = saved
        await d.stop()
        assert d._fetched == {} and d._executor is None
    try:
        asyncio.run(run())
    finally:
        gate.set()
        peer.close()
        down.close()
    print("fleet view: one shared request per peer, reused briefly, dropped on change; "
          "own thread pool; reply size limit  OK")


def main():
    test_peer_host_and_url()
    test_manual_peer_entries()
    test_settings_fields()
    test_find_target()
    test_beacon_address()
    test_beacon_replay_and_runs()
    test_copies_of_one_installation()
    test_other_key_beacons()
    test_signed_requests()
    test_legacy_key_and_previous_key()
    test_nonce_store()
    test_calls_only_known_peers()
    test_signed_peer_calls()
    test_key_mismatch_and_rotation()
    test_legacy_peer_calls()
    test_replayed_old_beacons_get_no_key()
    test_reboot_with_clock_set_back()
    test_cluster_key()
    test_malformed_beacons()
    test_direct_no_redirect()
    test_failures_logged_once()
    test_shared_answers()
    print("\nPASS: cluster peer calls")


if __name__ == "__main__":
    main()
