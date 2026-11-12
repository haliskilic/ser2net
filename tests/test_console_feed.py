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
    bytes were dropped (a JSON notice), without it everything is as in 2.8.

The handlers run here against stand-ins for the WebSocket, the runner and the app state.

Run: python3 tests/test_console_feed.py
"""
import asyncio
import os
import struct
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import AppConfig, MappingConfig  # noqa: E402
from app.engine.bridge import FROM_DEVICE, TO_DEVICE, MappingRunner  # noqa: E402
from app.engine.modbus_gateway import ModbusGatewayRunner  # noqa: E402
from app.engine.supervisor import _Unbuildable  # noqa: E402
from app.web import auth  # noqa: E402
from app.web.routes import console  # noqa: E402
from app.web.routes.common import Ctx  # noqa: E402
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
        assert w.closed == (1000, "access changed") and not runner.traffic, w.closed
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
        assert con.frames[-1] == b"last" and con.closed, con.frames[-1:]
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


def main():
    test_end_of_a_full_feed()
    test_runners_end_their_viewers()
    test_feed_bounds()
    test_viewer_limits()
    test_stalled_viewer()
    test_batches()
    test_drop_notices()
    print("\nPASS: console and hex-view feeds")


if __name__ == "__main__":
    main()
