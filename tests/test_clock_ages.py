"""How old something is, and which client came first, never go by the wall clock.

A step of the system clock (NTP correcting the clock of a board without an RTC a few
minutes after its boot, a clock set back by hand) must not change:
  * the LAN cluster's peer table and its notes: a live peer stays listed after a forward
    step, a node that left goes after PEER_TTL after a backward one (also its notes:
    another key, a copy of this node, a node using the raw key);
  * a node's uptime in the fleet view (/api/cluster/local's uptime_s);
  * the order of a mapping's clients (the oldest one is kicked to make room, listed
    first) and their connection times (status, REST connected_s);
  * how long a serial port or a connect-out link was down ("back after ..."), while the
    line about it still names the wall-clock time it started failing;
  * the OIDC discovery document's cache.
Each test moves the wall clock of the module it checks (tests/_clock.py); the system
clock is never changed.

Run: python3 tests/test_clock_ages.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _clock import WallClock

from ser2netweb.config import AppConfig, ClusterSettings, ConfigStore
from ser2netweb.engine import access, bridge, cluster, serial_link
from ser2netweb.engine.cluster import (LEGACY_SEEN_S, NOTE_TTL, PEER_TTL,
                                ClusterDiscovery, _sign, key_header)
from ser2netweb.state import AppState
from ser2netweb.web import auth, oidc_auth
from ser2netweb.web.routes import cluster as cluster_routes
from ser2netweb.web.server import build_app

KEY = "clock-ages-key"
DAY = 86400.0
_T = [time.time()]


def _node(instance_id: str, key: str = KEY) -> ClusterDiscovery:
    cfg = AppConfig()
    cfg.instance_id = instance_id
    cfg.cluster = ClusterSettings(enabled=True, key=key, discovery_port=41750, legacy_auth=True)
    return ClusterDiscovery(cfg, lambda m: None)


def _beacon(key: str = KEY, **fields) -> bytes:
    """A signed beacon, dated (by the sender's clock, which is right) after the one before."""
    _T[0] += 1
    payload = {"v": 1, "id": "peer1111", "name": "edge", "ip": "10.0.0.5", "port": 8080,
               "scheme": "http", "t": _T[0], **fields}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps({"d": body, "s": _sign(key, body)}).encode()


def _age(rows, seconds: float, field: str = "last_seen") -> None:
    """Make these rows `seconds` older on the clock they are kept by."""
    for row in rows:
        row[field] -= seconds


def test_cluster_ages():
    me = _node("self0000")
    with WallClock.patch(cluster) as clock:
        me.handle_datagram(_beacon(), ("10.0.0.5", 41750))
        me.handle_datagram(_beacon(key="another-key", id="other222"), ("10.0.0.6", 41750))
        me.handle_datagram(_beacon(id="self0000", b="ab" * 8, v=2, ip="10.0.0.7"),
                           ("10.0.0.7", 41750))
        me.check_request("GET", "/api/cluster/local", "", b"",
                         {"X-Cluster-Key": key_header(KEY)}, "10.0.0.8")   # a 2.8.0 node
        assert [p["id"] for p in me.peers()] == ["peer1111"], me.peers()
        notes = lambda: (me.other_key_nodes(), [c["addr"] for c in me.copies()],
                         "10.0.0.8" in me.legacy_nodes())
        assert notes() == (["10.0.0.6"], ["10.0.0.7:8080"], True), notes()

        clock.shift = 2 * DAY   # NTP sets a clock that was two days behind
        assert [p["id"] for p in me.peers()] == ["peer1111"], "a forward step dropped a live peer"
        assert notes() == (["10.0.0.6"], ["10.0.0.7:8080"], True), notes()
        # the peer's next beacon, dated by its own (right) clock, is still taken
        me.handle_datagram(_beacon(), ("10.0.0.5", 41750))
        assert len(me.peers()) == 1

        clock.shift = -DAY      # and a clock set back a day
        _age(me._peers.values(), PEER_TTL + 1)
        assert me.peers() == [], "a backward step kept a node that left"
        _age(me._copies.values(), NOTE_TTL + 1)
        for d, seconds in ((me._other_key, NOTE_TTL), (me._legacy_in, LEGACY_SEEN_S)):
            for k in d:
                d[k] -= seconds + 1
        assert notes() == ([], [], False), notes()
    print("cluster: peers and notes age by the monotonic clock - a step forward keeps a "
          "live peer, a step back doesn't keep one that left  OK")

    # a node that restarted with its clock set back is taken once PEER_TTL passed without
    # its beacons, however the receiver's own wall clock moved meanwhile
    me = _node("self0000")
    with WallClock.patch(cluster) as clock:
        me.handle_datagram(_beacon(v=2, b="1" * 16), ("10.0.0.5", 41750))
        restarted = _beacon(v=2, b="2" * 16, t=_T[0] - 3600)
        me.handle_datagram(restarted, ("10.0.0.5", 41750))
        assert me.peers()[0]["v"] == 2 and len(me.peers()) == 1
        assert next(iter(me._ids.values()))["cur"] == "1" * 16, "taken within PEER_TTL"
        clock.shift = -2 * DAY
        _age(me._ids.values(), PEER_TTL + 1, "seen")
        me.handle_datagram(restarted, ("10.0.0.5", 41750))
        assert next(iter(me._ids.values()))["cur"] == "2" * 16, "not taken after PEER_TTL"
    print("cluster: a restarted node's earlier clock is forgiven after PEER_TTL by the "
          "monotonic clock  OK")


def test_clients():
    """A clock step between two connections can't make the newest client the oldest one,
    and doesn't change how long a client has been connected."""
    def client(peer):
        return types.SimpleNamespace(peer=peer, priority=False, seq=access.connection_order(),
                                     connected_at=time.monotonic(), kick=lambda reason="": None)

    with WallClock.patch(bridge) as clock:
        first = client("10.0.0.1:1")
        clock.shift = -3600       # the clock is set back an hour
        second = client("10.0.0.2:2")
        assert access.pick_victim([second, first]) is first, "the newest client was kicked"
        st = bridge.RunnerStatus()
        first.connected_at -= 125
        st.set_clients([second, first])
        assert [c["peer"] for c in st.clients] == ["10.0.0.1:1", "10.0.0.2:2"], st.clients
        clock.shift = 2 * DAY     # and then two days forward
        rows = st.as_dict()["clients"]
        assert 124 <= rows[0]["connected_s"] <= 127 and rows[1]["connected_s"] <= 2, rows
        assert rows[0]["duration"].startswith("2m "), rows
    print("clients: the first connected is the oldest and listed first; connection times "
          "by the monotonic clock  OK")


def test_down_time():
    lines: list[str] = []
    with WallClock.patch(serial_link) as clock:
        log = serial_link.FailureLog(lines.append, "connect-out failed", every=0)
        log.failed("refused")
        started = time.time()
        clock.shift = 2 * DAY     # NTP moves the clock two days forward meanwhile
        log.failed("refused")
        assert "since " + time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) in lines[-1], \
            lines
        back = log.recovered()
    assert back in (" (back after 0s)", " (back after 1s)"), back
    print("down time: 'back after' by the monotonic clock; 'since' is the wall-clock time  OK")


async def _get(app, path: str) -> tuple[int, str]:
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8080")], "client": ("127.0.0.1", 50000),
             "server": ("127.0.0.1", 8080)}
    out = {"status": 0, "body": b""}
    done = asyncio.Event()

    async def receive():
        if not done.is_set():
            done.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(app(scope, receive, send), 30)
    return out["status"], out["body"].decode("utf-8", "replace")


def test_uptime():
    """The fleet view's uptime counts from the process start by the monotonic clock: a
    forward step after the start (NTP fixing a clock that was days behind) is not uptime."""
    d = tempfile.mkdtemp(prefix="s2n-clock-")
    try:
        cfg = AppConfig.from_dict({
            "users": [{"username": "admin", "password_hash": auth.hash_password("clock-ages-1"),
                       "role": "admin", "pwd_version": 1}],
            "admin_ui": {"require_login": False, "anonymous_role": "viewer"},
            "cluster": {"enabled": True, "key": KEY, "discovery": False}})
        state = AppState(ConfigStore(os.path.join(d, "config.json")), cfg)
        state.started_mono -= 7200    # started two hours ago ...
        with WallClock.patch(cluster_routes) as clock:
            clock.shift = 3 * DAY     # ... and the clock was set three days forward since
            status, page = asyncio.run(_get(build_app(state), "/api/cluster/status"))
        assert status == 200 and "up 2h 0m" in page, (status, page[:600])
    finally:
        for name in list(logging.root.manager.loggerDict):
            if name == "ser2net" or name.startswith("ser2net."):
                lg = logging.getLogger(name)
                for h in list(lg.handlers):
                    h.close()
                    lg.removeHandler(h)
        shutil.rmtree(d, ignore_errors=True)
    print("fleet view: this node's uptime by the monotonic clock (a clock step after the "
          "start isn't uptime)  OK")


def test_discovery_cache():
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"issuer": "https://idp.example.com"}

    oidc_auth._DISCOVERY_CACHE.clear()
    try:
        with WallClock.patch(oidc_auth) as clock:
            oidc_auth.discover("https://idp.example.com", fetch=fetch)
            clock.shift = 2 * DAY
            oidc_auth.discover("https://idp.example.com", fetch=fetch)
            assert len(fetched) == 1, "a clock step ended the cached discovery document"
            clock.shift = -DAY
            doc, when = oidc_auth._DISCOVERY_CACHE["https://idp.example.com"]
            oidc_auth._DISCOVERY_CACHE["https://idp.example.com"] = (
                doc, when - oidc_auth._DISCOVERY_TTL - 1)
            oidc_auth.discover("https://idp.example.com", fetch=fetch)
            assert len(fetched) == 2, "a clock set back kept the document past its time"
    finally:
        oidc_auth._DISCOVERY_CACHE.clear()
    print("OIDC: the discovery document is cached for an hour by the monotonic clock  OK")


def main():
    test_cluster_ages()
    test_clients()
    test_down_time()
    test_uptime()
    test_discovery_cache()
    print("\nPASS: ages and the order of clients don't follow wall clock steps")


if __name__ == "__main__":
    main()
