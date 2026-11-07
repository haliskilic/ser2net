"""Client admission shared by the transparent bridge and the Modbus gateway (access.py):
allow-list, max connections, kick_old_user and priority clients, plus the reconnect
backoff of serial_link.py. Pure: no sockets, no serial port, so it runs on any OS
(tests/test_access_priority.py drives the same rules end to end over socat PTYs).

Run: python3 tests/test_access_policy.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import NetworkSettings
from app.engine import access
from app.engine.serial_link import Backoff


class FakeClient:
    def __init__(self, peer, connected_at, priority=False):
        self.peer, self.connected_at, self.priority = peer, connected_at, priority
        self.kicked = ""

    def kick(self, reason="kicked"):
        self.kicked = reason


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

    assert access.pick_victim(set()) is None
    print("admit: room / full / not allowed / kick_old_user / priority victim choice  OK")


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


def main():
    test_allow_list()
    test_admit()
    test_backoff()
    print("\nPASS: client admission policy + reconnect backoff")


if __name__ == "__main__":
    main()
