"""Client admission shared by the transparent bridge and the Modbus gateway (access.py):
allow-list, max connections, kick_old_user and priority clients (the oldest client is the
one that connected first, by a counter, not by a clock; with writer_client_ips a client
that would only listen never evicts a writer), plus the reconnect backoff of
serial_link.py. Pure: no sockets, no serial port, so it runs on any OS
(tests/test_access_priority.py drives the same rules end to end over socat PTYs).

The runners' accept handlers are driven with stand-in streams: connections that arrive
together (in one pass of the event loop) and RFC2217 clients that wait for the serial
port must not get past max_connections.

Run: python3 tests/test_access_policy.py
"""
import asyncio
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import MappingConfig, NetworkSettings, client_networks
from ser2netweb.engine import access, bridge, netguard
from ser2netweb.engine.bridge import MappingRunner
from ser2netweb.engine.modbus_gateway import ModbusGatewayRunner
from ser2netweb.engine.serial_link import Backoff


class FakeClient:
    def __init__(self, peer, seq, priority=False, connected_at=None, listen_only=False):
        self.peer, self.seq, self.priority = peer, seq, priority
        self.listen_only = listen_only
        self.connected_at = time.monotonic() if connected_at is None else connected_at
        self.kicked = ""
        self.closing = False

    def kick(self, reason="kicked"):
        self.kicked = reason
        self.closing = True


def policy(**network):
    return access.AccessPolicy(NetworkSettings.from_dict(dict({"max_connections": 1}, **network)))


def test_allow_list():
    p = policy()
    assert p.allowed("203.0.113.9") and p.allowed("::1"), "a blank allow-list admits everyone"
    p = policy(allowed_client_ips=["10.0.0.0/24", "192.168.1.5", "not-an-ip"])
    assert p.allowed("10.0.0.7") and p.allowed("192.168.1.5")
    assert p.allowed("::ffff:10.0.0.7"), "an IPv4-mapped IPv6 peer counts as its IPv4 address"
    assert not p.allowed("10.0.1.7") and not p.allowed("192.168.1.6") and not p.allowed("bogus")
    assert policy(priority_client_ips=["10.9.9.9"]).is_priority("10.9.9.9")
    assert not policy().is_priority("10.9.9.9")
    print("allow-list: blank = any, CIDR/host match, IPv4-mapped IPv6, junk ignored  OK")


def test_allow_list_families():
    v4, v6, mapped, link = "203.0.113.9", "2001:db8::7", "::ffff:198.51.100.4", "fe80::1%eth0"
    # a bare wildcard is "any client" (README), whichever family it is written in
    for bare in ("0.0.0.0", "::", " :: ", "0:0:0:0:0:0:0:0"):
        p = policy(allowed_client_ips=[bare], priority_client_ips=[bare])
        assert all(p.allowed(ip) and p.is_priority(ip) for ip in (v4, v6, mapped, link)), bare
    # a network is the family it is written in
    p = policy(allowed_client_ips=["0.0.0.0/0"])
    assert p.allowed(v4) and p.allowed(mapped) and not p.allowed(v6)
    p = policy(allowed_client_ips=["::/0"])
    assert p.allowed(v6) and p.allowed(link) and not p.allowed(v4) and not p.allowed(mapped)
    # an IPv4-mapped entry is the IPv4 addresses it maps (a client is compared as IPv4)
    p = policy(allowed_client_ips=["::ffff:10.0.0.0/120", "::FFFF:192.168.1.5"])
    assert p.allowed("10.0.0.9") and p.allowed("::ffff:10.0.0.9") and p.allowed("192.168.1.5")
    assert not p.allowed("10.0.1.9") and not p.allowed("192.168.1.6") and not p.allowed(v6)
    assert policy(allowed_client_ips=["::ffff:0:0/96"]).allowed(v4)
    p = policy(allowed_client_ips=["2001:db8::/64", "fe80::/10"])
    assert p.allowed(v6) and p.allowed(link) and not p.allowed("2001:db8:0:1::7")
    print("allow-list: bare 0.0.0.0 / :: = every client of both families, ::ffff: entries match, "
          "networks keep their family  OK")
def test_unusable_allow_list_admits_nobody():
    """A list is set but no entry of it is an address or network (a hand-edited config.json:
    edits refuse such entries): nobody gets in — before, the empty result admitted all."""
    for entries in (["plc.local"], ["10.0.0.0/8, 192.168.1.0/24"], ["10.0.0.1-10.0.0.9"],
                    ["10.0.0.0/33"], [""], "plc.local"):
        p = policy(allowed_client_ips=entries)
        for ip in ("10.0.0.7", "192.168.1.9", "127.0.0.1", "::1"):
            assert not p.allowed(ip), (entries, ip)
    # a bare text that is one network still works as that network (a one-entry list)
    assert policy(allowed_client_ips="10.0.0.0/8").allowed("10.1.2.3")
    nets, bad = client_networks(["10.0.0.0/8", "plc.local", "0.0.0.0", 7])
    assert [str(n) for n in nets] == ["10.0.0.0/8", "0.0.0.0/0"] and bad == ["plc.local", "7"]
    logs = []
    a = policy(allowed_client_ips=["plc.local"]).admit("10.0.0.7", "10.0.0.7:5000", set(),
                                                         logs.append)
    assert not a.admitted and logs == ["rejected 10.0.0.7:5000: not in allowed list"], logs
    print("allow-list with no usable entry: nobody admitted (was: everyone)  OK")


def test_admit():
    logs = []

    # room left: admitted, nobody kicked
    p = policy(max_connections=2)
    old = FakeClient("10.0.0.1:5000", 1.0)
    a = p.admit("10.0.0.2", "10.0.0.2:5001", {old}, logs.append)
    assert a.admitted and not a.priority and not old.kicked and not logs

    # full, no kick: refused at capacity, the client keeps its slot
    p = policy()
    a = p.admit("10.0.0.2", "10.0.0.2:5001", {old}, logs.append)
    assert (a.admitted, a.at_capacity) == (False, True) and not old.kicked
    assert logs[-1] == "rejected 10.0.0.2:5001: max connections reached", logs

    # not in the allow-list: refused before capacity is even looked at
    p = policy(allowed_client_ips=["10.0.0.0/24"])
    a = p.admit("203.0.113.7", "203.0.113.7:9", set(), logs.append)
    assert (a.admitted, a.at_capacity) == (False, False)
    assert logs[-1] == "rejected 203.0.113.7:9: not in allowed list", logs

    # kick_old_user: the oldest client makes room
    p = policy(max_connections=2, kick_old_user=True)
    oldest, newer = FakeClient("10.0.0.1:1", 1.0), FakeClient("10.0.0.3:3", 5.0)
    a = p.admit("10.0.0.2", "10.0.0.2:2", {newer, oldest}, logs.append)
    assert a.admitted and oldest.kicked == "kicked for new client 10.0.0.2:2" and not newer.kicked
    assert logs[-1] == "kicking 10.0.0.1:1 for new client 10.0.0.2:2", logs

    # a priority source gets in without kick_old_user, evicting the oldest NON-priority client
    p = policy(max_connections=2, priority_client_ips=["10.0.0.9", "10.0.0.1"])
    vip_old = FakeClient("10.0.0.1:1", 1.0, priority=True)
    normal = FakeClient("10.0.0.3:3", 5.0)
    a = p.admit("10.0.0.9", "10.0.0.9:9", {vip_old, normal}, logs.append)
    assert a.admitted and a.priority
    assert normal.kicked == "kicked for priority client 10.0.0.9:9" and not vip_old.kicked

    # ...and when every client is a priority one, the oldest of them
    vip_new = FakeClient("10.0.0.9:7", 6.0, priority=True)
    a = p.admit("10.0.0.9", "10.0.0.9:8", {vip_new, vip_old}, logs.append)
    assert a.admitted and vip_old.kicked and not vip_new.kicked

    # a port pool's client (kick=False) never makes room, priority or kick_old_user
    p = policy(kick_old_user=True, priority_client_ips=["10.0.0.9"])
    held = FakeClient("10.0.0.1:1", 1.0)
    for ip in ("10.0.0.2", "10.0.0.9"):
        a = p.admit(ip, f"{ip}:6", {held}, logs.append, kick=False)
        assert (a.admitted, a.at_capacity) == (False, True) and not held.kicked, ip
        assert logs[-1] == f"rejected {ip}:6: max connections reached", logs
    assert p.admit("10.0.0.2", "10.0.0.2:6", set(), logs.append, kick=False).admitted

    assert access.pick_victim(set()) is None
    print("admit: room / full / not allowed / kick_old_user / priority victim choice, a "
          "pool's client kicks nobody  OK")


def test_only_live_clients_count():
    logs = []
    # a kicked client still listed (its handler has not finished) neither counts nor is
    # kicked again: two newcomers at once with max 1 leave one client, not two
    p = policy(kick_old_user=True)
    old = FakeClient("10.0.0.1:1", 1.0)
    clients = {old}
    first = p.admit("10.0.0.2", "10.0.0.2:2", clients, logs.append)
    assert first.admitted and old.closing
    new = FakeClient("10.0.0.2:2", 2.0)
    clients.add(new)
    second = p.admit("10.0.0.3", "10.0.0.3:3", clients, logs.append)
    assert second.admitted and new.kicked == "kicked for new client 10.0.0.3:3", logs
    assert old.kicked == "kicked for new client 10.0.0.2:2", "kicked once, not twice"
    assert [line for line in logs if line.startswith("kicking 10.0.0.1:1")] == \
        ["kicking 10.0.0.1:1 for new client 10.0.0.2:2"], logs
    # ...and a slot freed by a kick is free: no new kick
    p = policy(max_connections=2)
    gone = FakeClient("10.0.0.1:1", 1.0)
    gone.closing = True
    stays = FakeClient("10.0.0.4:4", 3.0)
    a = p.admit("10.0.0.5", "10.0.0.5:5", {gone, stays}, logs.append)
    assert a.admitted and not stays.kicked and not gone.kicked
    print("admit: kicked clients still being torn down neither count nor get kicked again  OK")


def test_normal_newcomer_never_evicts_priority():
    logs = []
    p = policy(max_connections=2, kick_old_user=True, priority_client_ips=["10.0.0.9"])
    vips = {FakeClient("10.0.0.9:1", 1.0, priority=True), FakeClient("10.0.0.9:2", 2.0, priority=True)}
    a = p.admit("10.0.0.3", "10.0.0.3:3", vips, logs.append)
    assert (a.admitted, a.at_capacity) == (False, True), a
    assert not any(c.kicked for c in vips)
    assert logs[-1] == "rejected 10.0.0.3:3: max connections reached (all priority clients)", logs
    # a non-priority client among them is still the one that makes room
    normal = FakeClient("10.0.0.4:4", 0.5)
    p = policy(max_connections=3, kick_old_user=True, priority_client_ips=["10.0.0.9"])
    a = p.admit("10.0.0.3", "10.0.0.3:3", vips | {normal}, logs.append)
    assert a.admitted and normal.kicked and not any(c.kicked for c in vips)
    assert access.pick_victim(vips, priority=False) is None
    assert access.pick_victim(vips, priority=True).peer == "10.0.0.9:1"
    print("admit: a normal newcomer (kick_old_user) never evicts a priority client  OK")


def test_listener_never_evicts_a_writer():
    # writer_client_ips: a newcomer that would only listen never takes a writer's place,
    # with kick_old_user or as a priority client; it may take another listener's
    logs = []
    p = policy(kick_old_user=True, writer_client_ips=["10.0.0.5"],
               priority_client_ips=["10.0.0.9"])
    writer = FakeClient("10.0.0.5:1", 1.0)
    for ip in ("10.0.0.99", "10.0.0.9"):  # a listener, a priority listener
        a = p.admit(ip, f"{ip}:2", {writer}, logs.append)
        assert (a.admitted, a.at_capacity) == (False, True) and not writer.kicked, (ip, a)
        assert logs[-1] == (f"rejected {ip}:2: max connections reached (the clients connected "
                            "write, and one that only listens takes no writer's place)"), logs
    p = policy(max_connections=2, kick_old_user=True, writer_client_ips=["10.0.0.5"])
    listener = FakeClient("10.0.0.98:3", 2.0, listen_only=True)
    a = p.admit("10.0.0.99", "10.0.0.99:4", {writer, listener}, logs.append)
    assert a.admitted and listener.kicked and not writer.kicked, "the listener makes room"
    # a writer newcomer: the oldest client makes room, as without the list
    listener = FakeClient("10.0.0.98:3", 2.0, listen_only=True)
    a = p.admit("10.0.0.5", "10.0.0.5:5", {writer, listener}, logs.append)
    assert a.admitted and writer.kicked and not listener.kicked
    print("admit: a client that only listens never evicts a writer (kick_old_user, "
          "priority); it may evict another listener  OK")


# ---- the runners' accept handlers, with stand-in streams ----
class FakeTransport:
    def __init__(self):
        self.cut = asyncio.Event()

    def abort(self):
        self.cut.set()

    def close(self):
        self.cut.set()

    def is_closing(self):
        return self.cut.is_set()

    def get_extra_info(self, name, default=None):
        return default


class FakeWriter:
    def __init__(self, port):
        self.transport = FakeTransport()
        self.peer = ("10.0.0.7", port)
        self.sent = bytearray()

    def get_extra_info(self, name, default=None):
        return self.peer if name == "peername" else default

    def write(self, data):
        self.sent += data

    async def drain(self):
        pass

    def close(self):
        self.transport.close()

    def is_closing(self):
        return self.transport.is_closing()

    async def wait_closed(self):
        pass


async def _held(conn):
    """_ClientConn.run's stand-in: the client stays connected until it is cut off."""
    await conn.writer.transport.cut.wait()


def _mapping(protocol="raw", **network):
    return MappingConfig.from_dict({
        "name": "adm", "serial": {"port": "/dev/null-test"},
        "network": dict({"mode": "server", "protocol": protocol, "bind_ip": "127.0.0.1",
                         "port": 4001, "max_connections": 1, "tcp_keepalive": False},
                        **network)})


async def _arrive(runner, ports):
    """Connections accepted in one pass of the event loop: their handlers start together."""
    writers = [FakeWriter(p) for p in ports]
    tasks = [asyncio.ensure_future(runner._on_client(asyncio.StreamReader(), w)) for w in writers]
    await asyncio.sleep(0.05)
    return writers, tasks


def _live(runner):
    return sorted(c.peer for c in runner._clients if not c.closing)


async def _bridge_bursts():
    real_run = bridge._ClientConn.run
    bridge._ClientConn.run = _held
    tasks = []
    try:
        runner = MappingRunner(_mapping(kick_old_user=True), logger=lambda m: None)
        writers, t = await _arrive(runner, [1])
        tasks += t
        assert _live(runner) == ["10.0.0.7:1"]
        for burst in ([2, 3], [4, 5, 6], [7, 8, 9]):
            writers, t = await _arrive(runner, burst)
            tasks += t
            # the last of the burst holds the one slot; the rest were kicked in turn
            assert _live(runner) == [f"10.0.0.7:{burst[-1]}"], (burst, _live(runner))
            assert len(runner._clients) == 1, [c.peer for c in runner._clients]
        # priority arrivals at once, kick_old_user off: still one
        runner = MappingRunner(_mapping(priority_client_ips=["10.0.0.7"]), logger=lambda m: None)
        writers, t = await _arrive(runner, [11, 12])
        tasks += t
        assert _live(runner) == ["10.0.0.7:12"], _live(runner)
        print("bridge: same-tick arrivals (max 1, kick_old_user / priority) leave one client, "
              "bursts no longer grow 1 -> 3 -> 5  OK")
    finally:
        bridge._ClientConn.run = real_run
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _rfc2217_waiters():
    real_run = bridge._ClientConn.run
    bridge._ClientConn.run = _held
    tasks = []
    try:
        for kick in (False, True):
            logs = []
            runner = MappingRunner(_mapping("rfc2217", kick_old_user=kick), logger=logs.append)
            writers, t = await _arrive(runner, [21, 22, 23])  # the serial port is not open
            tasks += t
            assert not runner._clients and not any(w.transport.cut.is_set() for w in writers)
            runner._link.ready.set()  # it opens: the waiting clients are decided one by one
            await asyncio.sleep(0.05)
            assert len(_live(runner)) == 1, (kick, _live(runner), logs)
            if kick:
                assert _live(runner) == ["10.0.0.7:23"], _live(runner)
            else:
                assert _live(runner) == ["10.0.0.7:21"], _live(runner)
                refused = [w for w in writers[1:] if b"Busy" in w.sent]
                assert len(refused) == 2, logs
        # a waiter that gives up kicks nobody
        logs = []
        runner = MappingRunner(_mapping("rfc2217", kick_old_user=True, port=4002),
                               logger=logs.append)
        runner.mapping.options.rfc2217_net_timeout_s = 0.1
        runner._link.ready.set()
        writers, t = await _arrive(runner, [31])
        tasks += t
        runner._link.ready.clear()  # the port went away
        writers2, t = await _arrive(runner, [32])
        tasks += t
        await asyncio.sleep(0.2)
        assert _live(runner) == ["10.0.0.7:31"], (_live(runner), logs)
        assert "rejected 10.0.0.7:32: serial not ready for RFC2217" in logs, logs
        assert not any(line.startswith("kicking") for line in logs), logs
        print("bridge RFC2217: clients waiting for the serial port are admitted within "
              "max_connections; one turned away kicks nobody  OK")
    finally:
        bridge._ClientConn.run = real_run
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _gateway_bursts():
    runner = ModbusGatewayRunner(_mapping("modbus", kick_old_user=True), logger=lambda m: None)
    tasks = []

    async def arrive(ports):
        out = []
        for p in ports:
            reader = asyncio.StreamReader()
            w = FakeWriter(p)
            w.transport.abort = lambda reader=reader, w=w: (w.transport.cut.set(),
                                                            reader.feed_eof())
            out.append(asyncio.ensure_future(runner._on_client(reader, w)))
        await asyncio.sleep(0.05)
        return out

    try:
        tasks += await arrive([1])
        for burst in ([2, 3], [4, 5, 6]):
            tasks += await arrive(burst)
            live = sorted(c.peer for c in runner._clients if not c.closing)
            assert live == [f"10.0.0.7:{burst[-1]}"], (burst, live)
            assert len(runner._clients) == 1
        print("Modbus gateway: same-tick masters (max 1, kick_old_user) leave one  OK")
    finally:
        runner._stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_runners_admit_within_the_limit():
    asyncio.run(_bridge_bursts())
    asyncio.run(_rfc2217_waiters())
    asyncio.run(_gateway_bursts())


def test_connection_order():
    order = [access.connection_order() for _ in range(1000)]
    assert order == sorted(set(order)), "every client gets a later number than the one before"
    # the order decides, whatever a clock says: two clients within one tick of a coarse
    # clock (the same connected_at), or a client whose time reads earlier than the one
    # before it (a wall clock set back between them, as 2.8 recorded connection times)
    first, second = (FakeClient("10.0.0.1:1", access.connection_order(), connected_at=5.0),
                     FakeClient("10.0.0.2:2", access.connection_order(), connected_at=5.0))
    assert access.pick_victim({second, first}) is first
    later = FakeClient("10.0.0.3:3", access.connection_order(), connected_at=1.0)
    assert access.pick_victim({later, second}) is second, "the newest client was picked"
    print("the oldest client is the first connected (a counter, no clock): ties and clock "
          "steps can't change it  OK")


def test_backoff():
    async def run():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        stop.set()
        b = Backoff()
        assert await b.wait(stop) is False and b.attempt == 1, "stop ends the backoff at once"
        b.reset()
        stop.clear()
        t0 = loop.time()
        assert await b.wait(stop) is True
        waited = loop.time() - t0
        assert 0.55 <= waited < 2.0, waited  # first pause: 0.5 s + 0.1 s jitter
        assert b.attempt == 1
        b.reset()
        assert b.attempt == 0
    asyncio.run(run())
    print("backoff: 0.5 s + jitter first, stop ends it, reset() starts over  OK")


def test_refusal_lines_limited():
    """A client refused again and again (a retry loop, a scan) writes one line per reason
    a minute, the next one saying how many were held back; a kick is logged as it is."""
    logs, clock = [], [1000.0]
    with mock.patch.object(netguard.time, "monotonic", lambda: clock[0]):
        log = netguard.RefusalLog(logs.append)
        p = policy(allowed_client_ips="10.0.0.0/24")
        for i in range(50):
            assert not p.admit(f"192.0.2.{i}", f"192.0.2.{i}:4000", [], log).admitted
        full = policy(max_connections=1)
        assert not full.admit("10.0.0.9", "10.0.0.9:1", [FakeClient("10.0.0.1:1", 1.0)],
                              log).admitted
        log("kicking 10.0.0.1:1 for priority client 10.0.0.2:2")
        assert logs == ["rejected 192.0.2.0:4000: not in allowed list",
                        "rejected 10.0.0.9:1: max connections reached",
                        "kicking 10.0.0.1:1 for priority client 10.0.0.2:2"], logs
        clock[0] += 61
        p.admit("192.0.2.200", "192.0.2.200:4000", [], log)
    assert logs[-1] == ("rejected 192.0.2.200:4000: not in allowed list (49 more since the "
                        "last such line)"), logs[-1]
    print("refused connections: one line per reason a minute, then the count  OK")


def main():
    test_allow_list()
    test_allow_list_families()
    test_unusable_allow_list_admits_nobody()
    test_admit()
    test_only_live_clients_count()
    test_normal_newcomer_never_evicts_priority()
    test_listener_never_evicts_a_writer()
    test_runners_admit_within_the_limit()
    test_connection_order()
    test_backoff()
    test_refusal_lines_limited()
    print("\nPASS: client admission policy + reconnect backoff")


if __name__ == "__main__":
    main()
