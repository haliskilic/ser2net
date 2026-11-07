"""How a node calls its cluster peers: where a call may go and how its URL is built.

The fleet view's peer calls carry the shared cluster key, so:
  * a peer URL is only built for http/https, an IP address or host name (IPv6 in
    brackets) and a port 1..65535, with an encoded query — no value can turn it into
    a URL for another host;
  * a call only goes to a discovered or configured peer (find_target), never to an
    address or scheme taken from the browser's request;
  * a beacon can only name an IP address; a loopback or wildcard one (a node that
    could not tell its LAN address) falls back to the packet's source address;
  * a manual peer entry saved from Settings must name an IP address or host name,
    while validate() stays lenient for entries an older release accepted;
  * the cluster key may hold any characters: it is NFKC-normalised and used as UTF-8
    bytes for the beacon HMAC, the header sent and the comparison on the receiving
    side, so an ASCII key keeps exactly the bytes older releases send and check;
  * a malformed beacon is dropped without an error.

Run: python3 tests/test_cluster_peer_calls.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import AppConfig, ClusterSettings, ConfigError, parse_cluster_peer, peer_host
from app.engine.cluster import (ClusterDiscovery, _sign, key_bytes, key_header,
                                key_header_matches, peer_addr, peer_url)

KEY = "shared-key"


def node(peers=(), key=KEY, log=None):
    cfg = AppConfig()
    cfg.instance_id = "self0000"
    cfg.cluster = ClusterSettings(enabled=True, key=key, discovery_port=41750, peers=list(peers))
    return ClusterDiscovery(cfg, log or (lambda m: None))


def beacon(key=KEY, **fields):
    payload = {"v": 1, "id": "peer1111", "name": "edge", "ip": "10.0.0.5", "port": 8080,
               "scheme": "http", "t": 0, **fields}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps({"d": body, "s": _sign(key, body)}).encode()


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
    print("beacons: only an IP address is used, loopback/wildcard -> packet source, bad port dropped  OK")


def test_calls_only_known_peers():
    d = node(peers=["10.0.0.9:8080", "https://user@x:1"])
    calls = []
    d._http = lambda method, url, body, timeout: calls.append(url) or (200, {"ok": True})

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


TR_KEY = "küme-anahtarı-çöüşğİı"


def test_cluster_key():
    # an ASCII key keeps the bytes every earlier release sends and checks
    for k in ("rc-key-xyz", "a long key with spaces & symbols !#$%"):
        assert key_bytes(k) == k.encode("ascii") and key_header(k) == k, k
    # any characters, typed composed, pasted decomposed (NFD) or with a no-break space
    nfd = unicodedata.normalize("NFD", TR_KEY)
    assert nfd != TR_KEY and key_bytes(nfd) == key_bytes(TR_KEY) == TR_KEY.encode("utf-8")
    assert key_bytes("iki\u00a0kelime") == key_bytes("iki kelime")
    # the header value http.client sends (latin-1) carries exactly the key's bytes
    assert key_header(TR_KEY).encode("latin-1") == TR_KEY.encode("utf-8")
    assert key_header_matches(TR_KEY, key_header(nfd)) and key_header_matches(nfd, key_header(TR_KEY))
    assert key_header_matches("rc-key-xyz", "rc-key-xyz")  # what v2.7/v2.8 peers send
    for wrong in ("", None, "rc-key-xy", TR_KEY, "çöü", "\u0100", key_header("çöü")):
        assert not key_header_matches(TR_KEY, wrong), wrong
    assert not key_header_matches("", "")
    # the same key in another normal form signs beacons the same way
    assert _sign(nfd, "body") == _sign(TR_KEY, "body")
    assert _sign("rc-key-xyz", "body") == hmac.new(b"rc-key-xyz", b"body", hashlib.sha256) \
        .hexdigest(), "ASCII beacon signatures unchanged"
    d = node(key=TR_KEY)
    d.handle_datagram(beacon(key=nfd), ("10.0.0.5", 41750))
    assert [p["id"] for p in d.peers()] == ["peer1111"]
    print("cluster key: any characters (NFKC + UTF-8 bytes), ASCII keys byte-identical  OK")


def test_malformed_beacons():
    d = node()
    signed_list = json.dumps({"d": "[]", "s": _sign(KEY, "[]")}).encode()
    for data in (b"\xff\xfe", b"[]", b'"x"', b"7", b'{"d": 1, "s": "00"}',
                 b'{"d": "{}", "s": 5}', json.dumps({"d": "{}", "s": "\u00fc"}).encode(),
                 b'{"d": "\\ud800", "s": "00"}', ("[" * 30000 + "]" * 30000).encode(),
                 signed_list, beacon(ip={"x": 1}, port=[8080])):
        d.handle_datagram(data, ("10.0.0.9", 41750))   # must not raise
    assert d.peers() == [], d.peers()
    print("malformed beacons (types, non-ASCII signature, surrogates, nesting) dropped  OK")


def main():
    test_peer_host_and_url()
    test_manual_peer_entries()
    test_find_target()
    test_beacon_address()
    test_calls_only_known_peers()
    test_cluster_key()
    test_malformed_beacons()
    print("\nPASS: cluster peer calls")


if __name__ == "__main__":
    main()
