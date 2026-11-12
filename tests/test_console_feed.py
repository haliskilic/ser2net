"""The browser viewers of a mapping's traffic — the console and the hex view — from the
feed the bridges fill to the WebSocket's close (no socat, no serial device).

  - a feed's end is a flag outside its queue: a viewer that fell behind until its feed is
    full still learns that the mapping stopped (its sender sends what is left, then ends),
    on every kind of runner, the stand-in for a mapping that can't be built included;
  - a feed holds at most FEED_MAX_BYTES of traffic in FEED_MAX_ITEMS chunks (the oldest
    go first, counted); a viewer whose sending is stuck for STALL_S while its traffic is
    dropped is closed (4003), its monitor removed first and a close it takes nothing of
    given up, while a slow viewer that keeps taking stays;
  - consoles and hex views are counted together: VIEWERS_PER_USER per account and
    mapping, VIEWERS_PER_MAPPING, VIEWERS_TOTAL; one more is closed with 1013 and why;
  - what arrives within FLUSH_S goes out in one message; with protocol 2 (?v=2) the hex
    view packs its records ([dir][time][length][bytes] ...) and a viewer is told how many
    bytes were dropped (a JSON notice), without it everything is as in 2.8;
  - after the handshake a close says why: the mapping stopped or restarted (4001), access
    changed (4002), stalled (4003), no such mapping (4004), too many (1013); a page that
    asks for protocol 2 is told when the mapping is not running.

A mapping's console text encoding (options.console_encoding): UTF-8 by default, or a
single-byte code page whose characters for 0x80-0xFF the console panel carries (C1 codes
as '.'); an older peer's form keeps it, an edit refuses an unknown one, a stored unknown
one reads as UTF-8.

End to end (a real server, no serial device): a Modbus gateway's console is observe-only
and opens in the hex view; the close codes and the notice over real WebSockets; the
encoding saved from the form and shown by the console panel.
The other parts run the handlers against stand-ins for the WebSocket, the runner and
the app state.

Run: python3 tests/test_console_feed.py
"""
import asyncio
import html
import json
import os
import re
import struct
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import Browser, Server, free_port  # noqa: E402

from app.config import (  # noqa: E402
    CONSOLE_ENCODINGS, AppConfig, ConfigError, MappingConfig, console_charset, console_encoding,
)
from app.engine.bridge import FROM_DEVICE, TO_DEVICE, MappingRunner  # noqa: E402
from app.engine.modbus_gateway import ModbusGatewayRunner  # noqa: E402
from app.engine.supervisor import _Unbuildable  # noqa: E402
from app.web import auth  # noqa: E402
from app.web.routes import console  # noqa: E402
from app.web.routes.common import Ctx  # noqa: E402
from app.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields  # noqa: E402
from app.web.routes.console import _Monitor, _TrafficMonitor  # noqa: E402


def test_end_of_a_full_feed():
    async def run():
        con, hexv = _Monitor(), _TrafficMonitor(to_device=True)
        for mon, feed in ((con, con.feed), (hexv, lambda b: hexv.feed_traffic(FROM_DEVICE, b))):
            for i in range(console.FEED_MAX_ITEMS + 10):  # a viewer that fell behind: full
                feed(b"%d" % i)
            mon.close()
            feed(b"late")
            items, _dropped, ended = await asyncio.wait_for(mon.take(), 2)
            assert ended, "the end of a full feed was lost"
            assert len(items) == console.FEED_MAX_ITEMS, len(items)   # the oldest went first
            last = items[-1] if isinstance(mon, _Monitor) else items[-1][2]
            assert last == b"%d" % (console.FEED_MAX_ITEMS + 9), last  # nothing after the end
    asyncio.run(run())
    print("a full console / hex-view feed still delivers its end, after what is left  OK")


def test_runners_end_their_viewers():
    async def run():
        net = MappingConfig.from_dict({"name": "n", "kind": "net", "serial": {"port": "/dev/none"}})
        modbus = MappingConfig.from_dict({"name": "m", "kind": "net", "serial": {"port": "/dev/none"},
                                          "network": {"protocol": "modbus"}})
        for runner in (MappingRunner(net, logger=None), ModbusGatewayRunner(modbus, logger=None),
                       _Unbuildable(ValueError("bad value"))):
            con, hexv = _Monitor(), _TrafficMonitor(to_device=True)
            runner.add_monitor(con)
            runner.add_traffic_monitor(hexv)
            for _ in range(console.FEED_MAX_ITEMS):
                con.feed(b"x")
            await runner.stop()
            for mon in (con, hexv):
                _items, _dropped, ended = await asyncio.wait_for(mon.take(), 2)
                assert ended, f"{type(runner).__name__}.stop() left a viewer open"
    asyncio.run(run())
    print("stopping a bridge, a Modbus gateway or an unbuildable mapping's stand-in ends "
          "its consoles and hex views  OK")


class FakeRunner:
    def __init__(self):
        self.monitors, self.traffic, self.written = set(), set(), []
        self.status = types.SimpleNamespace(state="running")

    def add_monitor(self, mon):
        self.monitors.add(mon)

    def discard_monitor(self, mon):
        self.monitors.discard(mon)

    def add_traffic_monitor(self, mon):
        self.traffic.add(mon)

    def discard_traffic_monitor(self, mon):
        self.traffic.discard(mon)

    async def serial_write(self, data):
        self.written.append(data)
        return True

    def stop(self):
        for mon in list(self.monitors) + list(self.traffic):
            mon.close()
        self.monitors.clear()
        self.traffic.clear()


class FakeSocket:
    """A browser's WebSocket as the handlers see it (Starlette's interface). `stuck`: it
    takes nothing — every send and the close wait for good; `delay`: each send takes this
    long (a slow link)."""

    def __init__(self, mid, cookie, stuck=False, delay=0.0):
        self.path_params, self.query_params = {"mid": mid}, {}
        self.headers = {"host": "127.0.0.1:8000", "cookie": f"{auth.SESSION_COOKIE}={cookie}"}
        self.cookies = {auth.SESSION_COOKIE: cookie}
        self.client = types.SimpleNamespace(host="127.0.0.1", port=40000)
        self.url = types.SimpleNamespace(path=f"/api/mappings/{mid}/console")
        self.scope = {"type": "websocket", "path": self.url.path, "root_path": "",   # (proxy.py)
                      "headers": [(k.encode(), v.encode()) for k, v in self.headers.items()],
                      "client": ("127.0.0.1", 40000)}
        self.state = types.SimpleNamespace()
        self.stuck, self.delay = stuck, delay
        self.accepted, self.closed = False, None
        self.frames: list = []
        self._gone = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        if self.closed is None:
            self.closed = (code, reason or "")
        if self.stuck:
            await asyncio.Event().wait()

    async def _send(self, frame):
        if self.stuck:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.frames.append(frame)

    async def send_bytes(self, data):
        await self._send(bytes(data))

    async def send_json(self, data):
        await self._send(data)

    async def receive(self):
        await self._gone.wait()
        return {"type": "websocket.disconnect", "code": 1001}

    def leave(self):  # the browser closes it
        self._gone.set()


class Harness:
    """routes/console.py's handlers on an app state with these users and mappings, each
    mapping with a FakeRunner."""

    def __init__(self, users=("admin",), mappings=("m1",)):
        self.cfg = AppConfig.from_dict({
            "users": [{"username": u, "role": "admin", "password_hash": "x", "pwd_version": 1}
                      for u in users],
            "mappings": [{"id": m, "name": m.upper(), "kind": "net"} for m in mappings]})
        self.runners = {m: FakeRunner() for m in mappings}
        self.logs: list = []
        self.state = types.SimpleNamespace(
            config=self.cfg, revoked_sessions=None, log=self.logs.append,
            supervisor=types.SimpleNamespace(get_runner=self.runners.get),
            view_records=types.SimpleNamespace(open=lambda *a, **k: True))
        handlers = {r.path.rsplit("/", 1)[1]: r.endpoint
                    for r in console.routes(Ctx(None, self.state))}
        self.console, self.traffic = handlers["console"], handlers["traffic"]

    def socket(self, mid="m1", user="admin", **kw):
        u = self.cfg.get_user(user)
        return FakeSocket(mid, auth.issue_session(self.cfg.secret_key, 3600, u.username,
                                                  u.pwd_version), **kw)


async def settle():
    await asyncio.sleep(0.05)


def test_feed_bounds():
    async def run():
        mon = _Monitor()
        for _ in range(2 * console.FEED_MAX_BYTES // 4096):
            mon.feed(b"x" * 4096)
        assert mon._bytes == console.FEED_MAX_BYTES and mon.dropped == console.FEED_MAX_BYTES
        mon = _TrafficMonitor(to_device=True)
        for i in range(console.FEED_MAX_ITEMS + 100):  # many small chunks: bounded too
            mon.feed_traffic(FROM_DEVICE, bytes([i % 256]))
        assert len(mon._items) == console.FEED_MAX_ITEMS and mon.dropped == 100
        mon.feed_traffic(FROM_DEVICE, b"")  # nothing to show: not queued
        assert len(mon._items) == console.FEED_MAX_ITEMS
        mon.close()
        items, dropped, ended = await mon.take()
        assert ended and len(items) == console.FEED_MAX_ITEMS  # all fit in FRAME_MAX_BYTES
        assert dropped == 100 and (await mon.take())[1:] == (0, True), "said once"
        mon = _Monitor()
        for _ in range(40):
            mon.feed(b"y" * 4096)
        items, _, _ = await mon.take()
        assert sum(map(len, items)) == console.FRAME_MAX_BYTES, "one take: one frame's worth"
    asyncio.run(run())
    print(f"a feed holds at most {console.FEED_MAX_BYTES // 1024} KiB in "
          f"{console.FEED_MAX_ITEMS} chunks, the oldest dropped first and counted  OK")


def test_viewer_limits():
    v = console._Viewers()
    alice, bob = ("alice", ""), ("bob", "")
    assert all(v.admit(alice, "m1") == "" for _ in range(console.VIEWERS_PER_USER))
    assert "for one user" in v.admit(alice, "m1")
    assert v.admit(alice, "m2") == "" and v.admit(bob, "m1") == ""
    v.leave(alice, "m1")
    assert v.admit(alice, "m1") == ""
    visitors = [("(no login)", f"10.0.0.{i}") for i in range(console.VIEWERS_PER_MAPPING)]
    w = console._Viewers()
    assert all(w.admit(who, "m1") == "" for who in visitors)
    assert "of this mapping open (16)" in w.admit(("(no login)", "10.0.1.1"), "m1")
    w = console._Viewers()
    assert all(w.admit((f"u{i}", ""), f"m{i % 20}") == "" for i in range(console.VIEWERS_TOTAL))
    assert "in all" in w.admit(("u0", ""), "m99")
    for i in range(console.VIEWERS_TOTAL):
        w.leave((f"u{i}", ""), f"m{i % 20}")
    assert not w._open, w._open

    async def run():
        h = Harness(mappings=("m1", "m2"))
        socks = [h.socket() for _ in range(console.VIEWERS_PER_USER)]
        tasks = [asyncio.create_task(h.console(s)) for s in socks[:-1]]
        tasks.append(asyncio.create_task(h.traffic(socks[-1])))  # a hex view counts too
        await settle()
        assert all(s.accepted and s.closed is None for s in socks)
        extra = h.socket()
        await asyncio.wait_for(h.console(extra), 2)
        assert extra.closed[0] == 1013 and "for one user" in extra.closed[1], extra.closed
        assert any("console of admin (127.0.0.1) refused: too many" in m for m in h.logs)
        other = h.socket("m2")
        tasks.append(asyncio.create_task(h.console(other)))
        await settle()
        assert other.closed is None, "another mapping has room"
        socks[0].leave()
        await asyncio.wait_for(tasks[0], 2)
        again = h.socket()
        tasks.append(asyncio.create_task(h.traffic(again)))
        await settle()
        assert again.closed is None, "a viewer that left made room"
        for s in socks[1:] + [other, again]:
            s.leave()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert not any(r.monitors or r.traffic for r in h.runners.values())
    asyncio.run(run())
    print(f"viewers: {console.VIEWERS_PER_USER} per account and mapping (console and hex view "
          f"together), {console.VIEWERS_PER_MAPPING} per mapping, {console.VIEWERS_TOTAL} in "
          "all; one more is closed with 1013 and the reason  OK")


def test_stalled_viewer():
    async def run():
        h = Harness()
        runner = h.runners["m1"]
        # a viewer whose link is slow but keeps taking: dropped data, not closed
        slow = h.socket(delay=0.05)
        task = asyncio.create_task(h.console(slow))
        await settle()
        mon = next(iter(runner.monitors))
        for _ in range(12):
            for _ in range(80):
                mon.feed(b"s" * 4096)
            await asyncio.sleep(0.05)
        assert mon.dropped and not mon.stalled.is_set() and slow.closed is None
        slow.leave()
        await asyncio.wait_for(task, 2)

        # a viewer that takes nothing: closed once it has been stuck for STALL_S while
        # its traffic was dropped, though the close never gets through either
        stuck = h.socket(stuck=True)
        task = asyncio.create_task(h.console(stuck))
        await settle()
        mon = next(iter(runner.monitors))
        mon.feed(b"first")  # taken: its send never ends
        await settle()
        for _ in range(300):
            mon.feed(b"x" * 4096)
        assert mon.dropped and not mon.stalled.is_set(), "stuck only briefly yet"
        await asyncio.sleep(console.STALL_S + 0.05)
        mon.feed(b"x" * 4096)
        assert mon.stalled.is_set()
        await asyncio.wait_for(task, console.CLOSE_WAIT_S + 2)
        assert not runner.monitors, "the monitor of a closed viewer is still fed"
        assert stuck.closed == (4003, f"took nothing for {console.STALL_S:g} s"), stuck.closed
        assert any("console closed (127.0.0.1): took nothing" in m for m in h.logs), h.logs

        # its access ends (the session is revoked by a password change): closed, saying so
        w = h.socket()
        task = asyncio.create_task(h.traffic(w))
        await settle()
        h.cfg.get_user("admin").pwd_version += 1
        await asyncio.wait_for(task, console.CONSOLE_ACCESS_RECHECK_S + 2)
        assert w.closed == (4002, "access changed") and not runner.traffic, w.closed
    saved = console.STALL_S, console.CLOSE_WAIT_S, console.CONSOLE_ACCESS_RECHECK_S
    console.STALL_S, console.CLOSE_WAIT_S, console.CONSOLE_ACCESS_RECHECK_S = 0.3, 0.3, 0.1
    try:
        asyncio.run(run())
    finally:
        console.STALL_S, console.CLOSE_WAIT_S, console.CONSOLE_ACCESS_RECHECK_S = saved
    print("a viewer stuck while its traffic is dropped is closed (4003), monitor first, its "
          "close given up after CLOSE_WAIT_S; a slow one that keeps taking stays; an access "
          "change closes it  OK")


def records(frame):
    """The records of a protocol 2 hex-view message."""
    out, off = [], 0
    while off < len(frame):
        d, ts, n = struct.unpack_from(">BdI", frame, off)
        out.append((d, ts, frame[off + 13:off + 13 + n]))
        off += 13 + n
    return out


def test_batches():
    async def run():
        h = Harness()
        runner = h.runners["m1"]
        con, hex1, hex2 = h.socket(), h.socket(), h.socket()
        hex2.query_params = {"v": "2"}
        tasks = [asyncio.create_task(h.console(con)), asyncio.create_task(h.traffic(hex1)),
                 asyncio.create_task(h.traffic(hex2))]
        await settle()
        t0 = time.time()
        for i in range(200):  # one burst of small chunks, as a USB adapter delivers them
            data = b"%03d" % i
            for mon in runner.monitors:
                mon.feed(data)
            for mon in runner.traffic:
                mon.feed_traffic(FROM_DEVICE if i % 2 else TO_DEVICE, data)
        await asyncio.sleep(console.FLUSH_S + 0.1)
        burst = b"".join(b"%03d" % i for i in range(200))
        assert con.frames == [burst], f"console: {len(con.frames)} messages"
        assert len(hex1.frames) == 200, "protocol 1: a message per record, as in 2.8"
        assert [struct.unpack(">Bd", f[:9])[0] for f in hex1.frames][:2] == [TO_DEVICE, FROM_DEVICE]
        assert len(hex2.frames) == 1, f"protocol 2: {len(hex2.frames)} messages"
        recs = records(hex2.frames[0])
        assert [r[2] for r in recs] == [b"%03d" % i for i in range(200)]
        assert [r[0] for r in recs[:2]] == [TO_DEVICE, FROM_DEVICE]
        assert all(t0 - 1 < r[1] < time.time() + 1 for r in recs)
        # what comes within FLUSH_S of the first chunk goes with it; later, a new message
        for mon in runner.monitors:
            mon.feed(b"a")
        await asyncio.sleep(console.FLUSH_S / 4)
        for mon in runner.monitors:
            mon.feed(b"b")
        await asyncio.sleep(console.FLUSH_S + 0.1)
        for mon in runner.monitors:
            mon.feed(b"c")
        await asyncio.sleep(console.FLUSH_S + 0.1)
        assert con.frames[1:] == [b"ab", b"c"], con.frames[1:]
        # the end: what was collected goes first
        for mon in runner.monitors:
            mon.feed(b"last")
        runner.stop()
        await asyncio.wait_for(asyncio.gather(*tasks[:1]), 2)
        assert con.frames[-1] == b"last" and con.closed[0] == 4001, (con.frames[-1:], con.closed)
        for s in (hex1, hex2):
            s.leave()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    saved = console.FLUSH_S
    console.FLUSH_S = 0.2  # a burst is fed at once here; the window only needs to be clear
    try:
        asyncio.run(run())
    finally:
        console.FLUSH_S = saved
    print("a burst of chunks goes out as one message (protocol 2: the hex view's records "
          "packed; protocol 1: as in 2.8), what comes within FLUSH_S goes with it  OK")


def test_drop_notices():
    async def run():
        h = Harness()
        runner = h.runners["m1"]
        new, old = h.socket(), h.socket()
        new.query_params = {"v": "2"}
        tasks = [asyncio.create_task(h.console(s)) for s in (new, old)]
        await settle()
        over = 100
        for _ in range(console.FEED_MAX_BYTES // 4096 + over):  # more than a feed holds
            for mon in runner.monitors:
                mon.feed(b"d" * 4096)
        await asyncio.sleep(0.5)
        assert new.frames[0] == {"dropped": over * 4096}, new.frames[0]
        got = b"".join(f for f in new.frames[1:])
        assert len(got) == console.FEED_MAX_BYTES and isinstance(new.frames[1], bytes)
        assert all(isinstance(f, bytes) for f in old.frames), "protocol 1: no notices"
        for s in (new, old):
            s.leave()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    asyncio.run(run())
    print("protocol 2: a viewer is told how many bytes were dropped, before what follows  OK")


def test_close_reasons():
    async def run():
        h = Harness()
        gone = h.socket("nosuchmapping")
        await asyncio.wait_for(h.console(gone), 2)
        assert gone.accepted and gone.closed == (4004, "no such mapping"), gone.closed
        runner = h.runners["m1"]
        runner.status.state = "stopped"
        new, old = h.socket(), h.socket()
        new.query_params = {"v": "2"}
        tasks = [asyncio.create_task(h.traffic(s)) for s in (new, old)]
        await settle()
        assert new.frames == [{"state": "stopped"}] and old.frames == [], (new.frames, old.frames)
        runner.stop()  # started: the placeholder runner lets its viewers go
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert new.closed == old.closed == (4001, "mapping stopped or restarted"), new.closed
    asyncio.run(run())
    print("closes say why (4001 stopped/restarted, 4004 no such mapping); protocol 2: told "
          "when the mapping is not running  OK")


def test_console_encoding():
    m = MappingConfig.from_dict({"name": "n"})
    assert m.options.console_encoding == "utf-8" and console_charset("utf-8") == ""
    for enc in CONSOLE_ENCODINGS[1:]:
        table = console_charset(enc)
        assert len(table) == 128, enc
        for b in range(0x80, 0x100):
            ch = bytes([b]).decode(enc, "replace")
            assert table[b - 0x80] == ("." if 0x80 <= ord(ch) <= 0x9F else ch), (enc, hex(b))
    assert console_charset("iso-8859-9")[0xDE - 0x80] == "Ş" == console_charset("cp857")[0x9E - 0x80]
    assert console_charset("cp1254")[0xFD - 0x80] == "ı"
    assert "Ş" not in console_charset("iso-8859-1"), "Latin-1 has no Ş"
    # a stored value that isn't offered (hand-edited, a newer version's) reads as UTF-8;
    # an edit refuses it
    odd = MappingConfig.from_dict({"name": "n", "serial": {"port": "COM7"},
                                   "options": {"console_encoding": "koi8-r"}})
    assert odd.options.console_encoding == "koi8-r" and console_encoding(odd.options) == "utf-8"
    odd.validate()  # a stored config keeps loading and saving
    try:
        odd.validate_input()
        raise AssertionError("an edit with an unknown console encoding was accepted")
    except ConfigError as e:
        assert "Unknown console text encoding 'koi8-r'" in str(e), e
    MappingConfig.from_dict({"name": "n", "options": {"console_encoding": "cp857"}}).validate_input()

    form = {"name": "n", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "network_mode": "server", "network_protocol": "raw", "network_port": "4001"}
    posted = MappingConfig.from_dict(_build_mapping_dict(dict(form, opt_console_encoding="cp1254"),
                                                         strict=True))
    assert posted.options.console_encoding == "cp1254"
    fresh = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
    assert fresh.options.console_encoding == "utf-8", "a form without it: the default"
    stored = MappingConfig.from_dict({"name": "n", "options": {"console_encoding": "iso-8859-9"}})
    old_peer = dict(form, id=stored.id)  # an older cluster peer's form doesn't have it
    edited = MappingConfig.from_dict(_build_mapping_dict(old_peer, strict=True))
    _preserve_unmanaged_fields(edited, stored, old_peer)
    assert edited.options.console_encoding == "iso-8859-9", "an older peer's edit reset it"
    print("console text encoding: UTF-8 by default, a code page's table for 0x80-0xFF (C1 as "
          "'.'), kept by an older peer's form, an unknown one refused by edits and read as "
          "UTF-8  OK")


PW = "console-feed-pass-1"


def test_live():
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    from app.web.auth import hash_password

    def mapping(mid, name, enabled=True, **network):
        return {"id": mid, "name": name, "enabled": enabled, "kind": "net",
                "serial": {"port": f"/dev/ser2net-console-feed-{mid}"},
                "network": dict({"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                                 "port": free_port()}, **network)}

    config = {"users": [{"username": "admin", "password_hash": hash_password(PW),
                         "role": "admin", "pwd_version": 1}],
              "mappings": [mapping("cf0000000001", "Raw"),
                           mapping("cf0000000002", "Gateway", protocol="modbus"),
                           mapping("cf0000000003", "Off", enabled=False)]}
    with Server(config) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        cookie = {"Cookie": f"ser2net_session={b.cookie('ser2net_session').value}"}
        ws = "ws" + srv.base[len("http"):] + "/api/mappings/"

        st, _, page = b.get("/api/mappings/cf0000000002/console-view")
        assert st == 200 and 'data-mode="hex"' in page and 'data-interactive="0"' in page, page
        assert "type to send to the device" not in page and "read-only" in page
        st, _, page = b.get("/api/mappings/cf0000000001/console-view")
        assert 'data-mode="text"' in page and 'data-interactive="1"' in page
        print("a Modbus gateway's console: read-only for an admin too, opens in the hex view  OK")

        def closed(conn, seconds=5):
            try:
                while True:
                    conn.recv(timeout=seconds)
            except ConnectionClosed as e:
                return e.rcvd.code, e.rcvd.reason
            except TimeoutError:
                return None

        with connect(ws + "nosuchmapping/console", additional_headers=cookie) as conn:
            assert closed(conn) == (4004, "no such mapping")
        with connect(ws + "cf0000000003/traffic?v=2", additional_headers=cookie) as conn:
            assert json.loads(conn.recv(timeout=5)) == {"state": "stopped"}
        with connect(ws + "cf0000000003/traffic", additional_headers=cookie) as conn:
            assert closed(conn, 1) is None, "protocol 1: no notice"
        socks = [connect(ws + "cf0000000001/console", additional_headers=cookie)
                 for _ in range(4)]
        with connect(ws + "cf0000000001/traffic", additional_headers=cookie) as conn:
            code, reason = closed(conn)
            assert code == 1013 and "for one user" in reason, (code, reason)
        st, _, _ = b.post("/api/mappings/cf0000000001/stop", htmx=True)
        assert st == 200
        for conn in socks:
            assert closed(conn) == (4001, "mapping stopped or restarted")
            conn.close()
        assert "[Raw] console closed (127.0.0.1): mapping stopped or restarted" in srv.output()
        print("live: 4004 for no such mapping, the not-running notice, 1013 past the limit, "
              "4001 when the mapping is stopped  OK")

        # the console text encoding, from the form to the console panel
        st, _, form = b.get("/api/mappings/cf0000000001/form")
        assert st == 200 and 'name="opt_console_encoding"' in form
        assert '<option value="utf-8" selected>' in form, "the default is selected"
        fields = {"id": "cf0000000001", "name": "Raw", "enabled": "on", "kind": "net",
                  "serial_port": "/dev/ser2net-console-feed-cf0000000001",
                  "network_mode": "server", "network_protocol": "raw",
                  "network_bind_ip": "127.0.0.1", "network_port": str(free_port())}
        st, _, body = b.post("/api/mappings/save", dict(fields, opt_console_encoding="koi8-r"),
                             htmx=True)
        assert st == 400 and "Unknown console text encoding" in body, (st, body[:300])
        st, _, _ = b.post("/api/mappings/save", dict(fields, opt_console_encoding="iso-8859-9"),
                          htmx=True)
        assert st == 200
        saved = next(m for m in srv.saved_config()["mappings"] if m["id"] == "cf0000000001")
        assert saved["options"]["console_encoding"] == "iso-8859-9", saved["options"]
        st, _, page = b.get("/api/mappings/cf0000000001/console-view")
        table = re.search(r'data-charset="([^"]*)"', page).group(1)
        assert html.unescape(table) == console_charset("iso-8859-9"), table
        assert "Text encoding: ISO-8859-9." in page
        st, _, form = b.get("/api/mappings/cf0000000001/form")
        assert '<option value="iso-8859-9" selected>' in form
        st, _, page = b.get("/api/mappings/cf0000000003/console-view")
        assert 'data-charset=""' in page and "Text encoding" not in page, "UTF-8: xterm decodes"
    print("console text encoding: saved from the form (an unknown one refused), the console "
          "panel carries its table  OK")


def main():
    test_end_of_a_full_feed()
    test_runners_end_their_viewers()
    test_feed_bounds()
    test_viewer_limits()
    test_stalled_viewer()
    test_batches()
    test_drop_notices()
    test_close_reasons()
    test_console_encoding()
    test_live()
    print("\nPASS: console and hex-view feeds")


if __name__ == "__main__":
    main()
