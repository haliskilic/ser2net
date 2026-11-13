"""Port pools (classic ser2net's rotators): one listening port for several mappings.

  - config: pools are optional (a config.json without the key loads and saves as it was),
    an entry that is no pool is left out with a load note, never refused; the Settings
    text ("name port", "name IP:port", "... off") reads back as written; edits refuse a
    doubled name or address, a port a TCP server mapping or the admin UI listens on, and
    a mapping's port that is a pool's; a mapping is a member by its network.pool when it
    is a TCP server mapping of the transparent bridge without TLS;
  - engine (a Supervisor with stand-in serial devices): each client of the pool's port is
    handed to the first member with a free connection, in the order of the mappings, and
    talks to that mapping's device; a member that does not let the client's address in is
    passed over; when every member is in use the client reads "Busy: ..." and is
    disconnected, counted and logged; a freed member is used again; a pool whose port is
    taken keeps trying; a changed pool moves while the clients handed over stay connected,
    one whose rotate changed keeps listening; a pool stopped while it binds its port leaves
    no listener behind;
  - REST: GET / PUT /api/v1/pools, pools in /status, the errors; the Settings form.

Run: python3 tests/test_port_pools.py
"""
import asyncio
import contextlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _device import FakeDevice, _fast_pause, read_for, until
from _server import Browser, Server, free_port

from ser2netweb.config import (
    AppConfig, ConfigError, MappingConfig, PortPool, pool_member, pools_from_json,
    pools_from_text, pools_text,
)
from ser2netweb.engine import netguard, serial_io, serial_link
from ser2netweb.engine.supervisor import Supervisor
from ser2netweb.web.auth import hash_password, hash_token

TOKEN = "s2n_pools-operator-token"


def mapping(name, port, pool="lab", **network):
    return {"id": f"pool{name}00001", "name": name,
            "serial": {"port": f"/dev/ttyFAKE-{name}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": port, "max_connections": 1, "pool": pool, **network}}


def test_config():
    assert AppConfig.from_dict({}).pools == [] and "pools" not in AppConfig().to_dict()
    cfg = AppConfig.from_dict({"pools": [
        {"name": "lab", "port": 3000}, {"name": "lab", "port": 3001}, {"name": "no port"},
        {"name": "x", "port": 70000}, "junk", {"name": "v6", "bind_ip": "::1", "port": 3002,
                                               "enabled": False, "later": 1}]})
    assert [(p.name, p.bind_ip, p.port, p.enabled) for p in cfg.pools] == [
        ("lab", "0.0.0.0", 3000, True), ("v6", "::1", 3002, False)], cfg.pools
    assert len([n for n in cfg.load_notes if n.startswith("port pools:")]) == 4, cfg.load_notes
    cfg.validate()  # (no rule of validate() is about pools)
    assert cfg.to_dict()["pools"] == [
        {"name": "lab", "bind_ip": "0.0.0.0", "port": 3000, "enabled": True, "rotate": False},
        {"name": "v6", "bind_ip": "::1", "port": 3002, "enabled": False, "rotate": False}]
    assert AppConfig.from_dict({"pools": "lab 3000"}).pools == []

    text = "lab 3000\n\n  bench 192.168.1.5:3001  Rotate\nv6 [fd00::5]:3002 off rotate\n"
    pools = pools_from_text(text)
    assert [(p.name, p.bind_ip, p.port, p.enabled, p.rotate) for p in pools] == [
        ("lab", "0.0.0.0", 3000, True, False), ("bench", "192.168.1.5", 3001, True, True),
        ("v6", "fd00::5", 3002, False, True)]
    assert pools_text(pools) == \
        "lab 3000\nbench 192.168.1.5:3001 rotate\nv6 [fd00::5]:3002 rotate off"
    assert pools_from_text(pools_text(pools)) == pools and pools_from_text("") == []
    for bad in ("lab", "lab 3000 on", "lab port", "lab :3000", "a b c d", "lab 1 off off"):
        try:
            pools_from_text(bad)
        except ConfigError as e:
            assert "line 1" in str(e), e
        else:
            raise AssertionError(f"{bad!r} was read as a pool")
    assert pools_from_json([{"name": "lab", "port": 3000}]) == [PortPool("lab", "0.0.0.0", 3000)]
    for bad in ("lab", [{"name": "lab", "port": 3000, "members": []}], [["lab", 3000]]):
        try:
            pools_from_json(bad)
        except ConfigError:
            continue
        raise AssertionError(f"{bad!r} was read as pools")

    cfg = AppConfig.from_dict({"admin_ui": {"bind_ip": "0.0.0.0", "port": 8080}, "mappings": [
        mapping("a", 4001), mapping("b", 4002, tls=True, tls_cert="c", tls_key="k"),
        mapping("c", 4003, mode="udp"), mapping("d", 4004, protocol="modbus"),
        mapping("e", 4005, pool=""), mapping("f", 4006, protocol="rfc2217")]})
    assert [m.name for m in cfg.pool_members("lab")] == ["a", "f"]
    assert cfg.pool_members("") == [] and cfg.pool_members("other") == []
    notes = {m.name: m.ignored_settings() for m in cfg.mappings}
    assert not notes["a"] and not notes["e"] and not notes["f"]
    for name in "bcd":
        assert any("it is not a member" in t for t in notes[name]), (name, notes[name])
    assert pool_member(MappingConfig.from_dict({"kind": "serialbridge",
                                                "network": {"pool": "lab"}})) == ""

    def problem(*pools):
        return cfg.pools_problem([PortPool(*p) for p in pools])

    assert problem(("lab", "0.0.0.0", 3000), ("bench", "0.0.0.0", 3001)) == ""
    assert problem(("lab", "127.0.0.1", 3000), ("bench", "127.0.0.2", 3000)) == ""
    assert "Two port pools are named" in problem(("lab", "0.0.0.0", 3000), ("lab", "0.0.0.0", 3001))
    assert "both listen on TCP port 3000" in problem(("lab", "0.0.0.0", 3000),
                                                     ("bench", "127.0.0.1", 3000))
    assert "mapping 'a'" in problem(("lab", "0.0.0.0", 4001))
    assert problem(("lab", "0.0.0.0", 4003)) == "", "a UDP mapping's port is another transport"
    assert "admin UI" in problem(("lab", "127.0.0.1", 8080))
    for bad in (("", "0.0.0.0", 3000), ("a b", "0.0.0.0", 3000), ("x" * 65, "0.0.0.0", 3000),
                ("lab", "nowhere", 3000), ("lab", "0.0.0.0", 0), ("lab", "0.0.0.0", "3000")):
        assert problem(bad), bad
    # a mapping can't take a pool's port either (where the admin UI's port is checked)
    cfg.pools = [PortPool("lab", "0.0.0.0", 3000)]
    clash = MappingConfig.from_dict(mapping("g", 3000))
    assert "port pool" in cfg.own_port_conflict(clash)
    assert cfg.own_port_conflict(MappingConfig.from_dict(mapping("g", 3001))) == ""
    assert cfg.own_port_conflict(MappingConfig.from_dict(mapping("g", 3000, mode="udp"))) == ""
    # the pool's name on a mapping: checked at edit time only
    odd = MappingConfig.from_dict(mapping("h", 4010, pool="no such name!"))
    odd.validate()
    try:
        odd.validate_input()
    except ConfigError as e:
        assert "pool name" in str(e)
    else:
        raise AssertionError("an edit took a pool name with a space")
    print("config: optional key, entries left out with a note, the Settings text, the "
          "edit-time checks, who is a member  OK")


class Devices:
    """A FakeDevice per serial port name (serial_io.open_serial's stand-in)."""

    def __init__(self):
        self.by_port = {}

    async def open(self, settings):
        if settings.port not in self.by_port:
            self.by_port[settings.port] = await FakeDevice().start()
        return await self.by_port[settings.port].open(settings)

    def heard(self, name):
        dev = self.by_port.get(f"/dev/ttyFAKE-{name}")
        return bytes(dev.heard) if dev else b""

    async def close(self):
        for dev in self.by_port.values():
            await dev.close()


async def pool_client(port):
    return await asyncio.open_connection("127.0.0.1", port)


async def _engine():
    logs, devices = [], Devices()
    pool_port, other_port = free_port(), free_port()
    cfg = AppConfig.from_dict({
        "pools": [{"name": "lab", "bind_ip": "127.0.0.1", "port": pool_port},
                  {"name": "off", "bind_ip": "127.0.0.1", "port": other_port,
                   "enabled": False}],
        "mappings": [
            # (first in the pool, but only for another address: passed over)
            mapping("z", free_port(), allowed_client_ips=["10.9.9.9"]),
            mapping("a", free_port()), mapping("b", free_port(), open_on_connect=True),
            mapping("c", free_port(), pool="")]})
    sup = Supervisor(logger=logs.append)
    ids = {m.name: m.id for m in cfg.mappings}

    def clients(name):
        return sup.status(ids[name])["client_count"]

    with mock.patch.object(serial_io, "open_serial", devices.open), \
            mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
        await sup.start_all(cfg)
        try:
            await until(lambda: sup.status(ids["a"])["state"] == "running" and sup.get_runner(
                ids["a"])._link.is_open)
            st = sup.pools.status()
            assert set(st) == {"lab"}, "a pool that is off does not listen"
            assert st["lab"]["state"] == "listening" and st["lab"]["port"] == pool_port
            assert [m["name"] for m in st["lab"]["members"]] == ["z", "a", "b"]
            # the first free member, in the order of the mappings
            r1, w1 = await pool_client(pool_port)
            await until(lambda: clients("a") == 1)
            w1.write(b"to-a")
            await until(lambda: devices.heard("a") == b"to-a")
            devices.by_port["/dev/ttyFAKE-a"].say(b"from-a")
            assert await asyncio.wait_for(r1.readexactly(6), 5) == b"from-a"
            # the next one: a member that opens its port for its clients
            r2, w2 = await pool_client(pool_port)
            await until(lambda: clients("b") == 1)
            w2.write(b"to-b")
            await until(lambda: devices.heard("b") == b"to-b")
            assert sup.pools.status()["lab"]["busy"] == 2 and clients("z") == 0
            # all in use: told so, disconnected, counted, logged
            r3, w3 = await pool_client(pool_port)
            got = b"".join(await read_for(r3, 5))
            assert got == b"Busy: every port of the pool is in use.\r\n", got
            w3.close()
            st = sup.pools.status()["lab"]
            assert st["handed"] == 2 and st["refused"] == 1 and st["last_refused"] > 0
            assert any("[pool lab] rejected" in ln and "every port of the pool is in use" in ln
                       for ln in logs), logs
            assert any("[pool lab]" in ln and "-> mapping 'a'" in ln for ln in logs), logs
            assert clients("c") == 0, "a mapping outside the pool gets none of its clients"
            # a member's own port still works, and counts as in use for the pool
            w1.close()
            await until(lambda: clients("a") == 0)
            own = sup.get_runner(ids["a"])._server.sockets[0].getsockname()[1]
            r4, w4 = await asyncio.open_connection("127.0.0.1", own)
            await until(lambda: clients("a") == 1)
            r5, w5 = await pool_client(pool_port)
            assert b"Busy" in b"".join(await read_for(r5, 5))
            w5.close()
            w4.close()
            await until(lambda: clients("a") == 0)
            # freed: used again
            r6, w6 = await pool_client(pool_port)
            await until(lambda: clients("a") == 1)
            # the pool moves to another port: the clients handed over stay connected
            cfg.pools[0].port = other_port
            await sup.apply_pools(cfg.pools)
            assert sup.pools.status()["lab"]["port"] == other_port
            w6.write(b"still-here")
            await until(lambda: devices.heard("a").endswith(b"still-here"))
            with contextlib.suppress(OSError):
                _r, w = await pool_client(pool_port)
                w.close()
                raise AssertionError("the old port still takes connections")
            r7, w7 = await pool_client(other_port)
            assert b"Busy" in b"".join(await read_for(r7, 5))
            w7.close()
            # stopped mappings: the pool says it has none to hand a client to
            for w in (w2, w6):
                w.close()
            await sup.stop_all()
            assert sup.pools.status() == {}, "stop_all stops the pools"
        finally:
            await sup.stop_all()
            await devices.close()


async def _bind_retry():
    logs = []
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    sup = Supervisor(logger=logs.append)
    with mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
        try:
            await sup.apply_pools([PortPool("lab", "127.0.0.1", port)])
            st = sup.pools.status()["lab"]
            assert st["state"] == "error" and st["last_error"], st
            assert any("[pool lab] can't listen on" in ln and "trying again" in ln for ln in logs)
            holder.close()
            await until(lambda: sup.pools.status()["lab"]["state"] == "listening")
            # nobody in the pool: a client is turned away, without the Busy line
            r, w = await pool_client(port)
            assert await read_for(r, 5) == [b""]
            w.close()
            assert any("the pool has no running mapping" in ln for ln in logs), logs
        finally:
            holder.close()
            await sup.stop_all()


async def _rotate():
    """ser2net.yaml(5), ROTATOR: "Connections to the accepter will go through the set of
    connections and find the first unused one and use that. The next connection will start
    after the last connection used." (rotate; without it the search starts at the first
    mapping each time)."""
    for rotate, want in ((True, ["a", "b", "c", "a"]), (False, ["a", "a", "a", "a"])):
        logs, devices = [], Devices()
        pool_port = free_port()
        cfg = AppConfig.from_dict({
            "pools": [{"name": "lab", "bind_ip": "127.0.0.1", "port": pool_port,
                       "rotate": rotate}],
            "mappings": [mapping(n, free_port()) for n in "abc"]})
        sup = Supervisor(logger=logs.append)
        with mock.patch.object(serial_io, "open_serial", devices.open), \
                mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
            await sup.start_all(cfg)
            try:
                got = []
                for i in range(4):  # one client at a time: every member is free each time
                    _r, w = await pool_client(pool_port)
                    await until(lambda i=i: sup.pools.status()["lab"]["handed"] == i + 1)
                    await until(lambda: any(m["clients"] for m in
                                            sup.pools.status()["lab"]["members"]))
                    got += [m["name"] for m in sup.pools.status()["lab"]["members"]
                            if m["clients"]]
                    w.close()
                    await until(lambda: not any(m["clients"] for m in
                                                sup.pools.status()["lab"]["members"]))
                assert got == want, (rotate, got)
                # rotate changed: the pool goes on listening, and the next client is handed
                # by it at once
                listener = sup.pools.get("lab")
                cfg.pools[0].rotate = not rotate
                await sup.apply_pools(cfg.pools)
                assert sup.pools.get("lab") is listener, "restarted for a changed rotate"
                st = sup.pools.status()["lab"]
                assert st["rotate"] is not rotate and st["handed"] == 4, st
                if not rotate:  # (the last client went to a: now b comes first)
                    _r, w = await pool_client(pool_port)
                    await until(lambda: sup.pools.status()["lab"]["members"][1]["clients"])
                    w.close()
            finally:
                await sup.stop_all()
                await devices.close()


async def _stop_while_binding():
    """A pool stopped while its retry is binding its port: the listener that attempt opens
    is closed as well (it is not cancelled half-way), so nothing listens afterwards."""
    logs, opened = [], []
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    real = netguard.start_server
    binding = asyncio.Event()

    async def slow_start_server(*args, **kwargs):
        listener = await real(*args, **kwargs)
        opened.append(listener)
        binding.set()
        await asyncio.sleep(0.3)  # (as asyncio's start_serving() awaits, only longer)
        return listener

    sup = Supervisor(logger=logs.append)
    with mock.patch.object(serial_link.Backoff, "pause", _fast_pause), \
            mock.patch.object(netguard, "start_server", slow_start_server):
        try:
            await sup.apply_pools([PortPool("lab", "127.0.0.1", port)])
            assert sup.pools.status()["lab"]["state"] == "error"
            holder.close()
            await asyncio.wait_for(binding.wait(), 10)
            await sup.apply_pools([])
            assert sup.pools.status() == {}
            try:
                _r, w = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                pass
            else:
                w.close()
                raise AssertionError("the attempt's listener was left listening")
        finally:
            holder.close()
            for listener in opened:
                listener.close()
            await sup.stop_all()


def test_engine():
    asyncio.run(_rotate())
    print("engine: rotate begins after the mapping that got the last client; without it "
          "the first free one each time  OK")
    asyncio.run(_engine())
    print("engine: first free member in order, an address a member refuses is passed over, "
          "Busy when all are in use, a freed one is used again, a moved pool keeps its "
          "clients  OK")
    asyncio.run(_bind_retry())
    print("engine: a pool whose port is taken says why and listens once it is free  OK")
    asyncio.run(_stop_while_binding())
    print("engine: a pool stopped while it binds leaves no listener behind  OK")


def call(base, method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        r = e
    return (r.status if hasattr(r, "status") else r.code), json.loads(r.read() or b"null")


def test_web():
    map_port, pool_port, pool2 = free_port(), free_port(), free_port()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
                      "role": "admin"}],
           "api_token_hash": hash_token(TOKEN), "api_token_role": "operator",
           "pools": [{"name": "lab", "bind_ip": "127.0.0.1", "port": pool_port}],
           "mappings": [dict(mapping("a", map_port), enabled=False)]}
    with Server(cfg) as srv:
        base = srv.base + "/api/v1"
        st, body = call(base, "GET", "/pools")
        assert st == 200 and len(body["pools"]) == 1, body
        lab = body["pools"][0]
        assert (lab["name"], lab["port"], lab["state"], lab["handed"]) == \
            ("lab", pool_port, "listening", 0), lab
        assert [m["name"] for m in lab["members"]] == ["a"]
        st, body = call(base, "GET", "/status")
        assert st == 200 and body["pools"]["lab"]["state"] == "listening"
        st, body = call(base, "GET", "/info")
        assert "pools" in body["features"]
        # PUT replaces them; they listen at once
        st, body = call(base, "PUT", "/pools", {"pools": [
            {"name": "lab", "bind_ip": "127.0.0.1", "port": pool_port},
            {"name": "bench", "bind_ip": "127.0.0.1", "port": pool2, "enabled": False}]})
        assert st == 200 and [(p["name"], p["state"]) for p in body["pools"]] == \
            [("lab", "listening"), ("bench", "stopped")], body
        assert [p["name"] for p in srv.saved_config()["pools"]] == ["lab", "bench"]
        for bad, code in (({"pools": [{"name": "lab", "port": map_port,
                                       "bind_ip": "127.0.0.1"}]}, "validation"),
                          ({"pools": [{"name": "a b", "port": pool2}]}, "validation"),
                          ({"pools": "lab"}, "validation"), ({}, "validation"),
                          ([1], "invalid_json")):
            st, body = call(base, "PUT", "/pools", bad)
            assert st == 400 and body["error"] == code, (bad, st, body)
        assert len(srv.saved_config()["pools"]) == 2, "a refused PUT changes nothing"
        st, body = call(base, "PUT", "/mappings/poola00001",
                        dict(mapping("a", pool_port), enabled=False))
        assert st == 400 and "port pool" in body["detail"], body
        with socket.create_connection(("127.0.0.1", pool_port), timeout=5) as sock:
            assert sock.recv(100) == b"", "no running mapping in the pool: turned away"

        # the Settings form
        b = Browser(srv.base)
        b.login("admin", "admin-pass-1")
        st, _, page = b.get("/settings")
        assert st == 200 and 'id="pools"' in page and f"lab 127.0.0.1:{pool_port}" in page
        st, _, page = b.post("/settings/pools", {"pools": f"lab 127.0.0.1:{pool2}\n"})
        assert st == 200 and b.redirected == "/settings#pools", (st, b.redirected)
        assert srv.saved_config()["pools"] == [
            {"name": "lab", "bind_ip": "127.0.0.1", "port": pool2, "enabled": True,
             "rotate": False}]
        st, body = call(base, "GET", "/pools")
        assert body["pools"][0]["port"] == pool2 and body["pools"][0]["state"] == "listening"
        st, _, page = b.post("/settings/pools", {"pools": "lab"})
        assert st == 400 and "line 1" in page and ">lab</textarea>" in page, st
        st, _, page = b.post("/settings/pools", {"pools": f"lab 127.0.0.1:{map_port}"})
        assert st == 400 and "listening port of mapping" in page
        st, _, page = b.post("/settings/pools", {"pools": ""})
        assert st == 200 and "pools" not in srv.saved_config()
        assert call(base, "GET", "/pools")[1] == {"pools": []}
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = fh.read()
        assert f"\tapi_pools\tlab 127.0.0.1:{pool_port}, bench 127.0.0.1:{pool2} (off)" \
            "\tapi-token" in audit, audit[-600:]
        assert f"\tpools\tlab 127.0.0.1:{pool2}\tadmin" in audit and \
            "\tpools\tnone\tadmin" in audit, audit[-600:]
    print("REST: GET / PUT /api/v1/pools, pools in /status, refusals change nothing; the "
          "Settings form saves, refuses and clears them  OK")


def main():
    test_config()
    test_engine()
    test_web()
    print("\nPASS: port pools")


if __name__ == "__main__":
    main()
