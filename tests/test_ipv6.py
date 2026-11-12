"""IPv6 listeners: which bind IPs a mapping (and the console's admin UI picker) takes, how
two listeners' addresses are compared, and how bind IPs are written; listening for IPv4
and IPv6 clients on one mapping (network.dual_stack), and :: taking IPv6 clients only.

The listener parts bind 0.0.0.0 / :: with OS-assigned ports (run them in a throwaway
network namespace locally) and skip where this machine has no IPv6 loopback.

Run: python3 tests/test_ipv6.py
"""
import asyncio
import builtins
import socket
import sys
from unittest import mock

from _server import ROOT  # noqa: F401  (puts the app on sys.path)

from app import console
from app.config import (
    AppConfig, ConfigError, MappingConfig, NetworkSettings, _ip_overlaps, bind_ip_problem,
    canonical_ip,
)
from app.engine import netinfo, serial_io
from app.engine.bridge import MappingRunner
from app.engine.modbus_gateway import ModbusGatewayRunner


def ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


def mapping(**network):
    return MappingConfig.from_dict({
        "name": network.pop("name", "m"), "serial": {"port": network.pop("port_dev", "/dev/ttyS0")},
        "network": dict({"mode": "server", "bind_ip": "0.0.0.0", "port": 4001}, **network)})


def test_bind_ip_rule():
    for ip in ("0.0.0.0", "::", "::1", "0:0:0:0:0:0:0:1", "0:0:0:0:0:0:0:0", "127.0.0.1",
               "10.0.0.5", "2001:db8::5", "fe80::1%eth0", "::ffff:10.0.0.5"):
        assert bind_ip_problem(ip) == "", ip
        mapping(bind_ip=ip).validate()  # what the UI, the REST API and an import check
    for ip in ("ff02::1", "224.0.0.1", "255.255.255.255", "240.0.0.1", "100::1",
               "::ffff:224.0.0.1", "::ffff:255.255.255.255"):
        assert "multicast/reserved" in bind_ip_problem(ip), ip
    for ip in ("not-an-ip", "", "10.0.0.300"):
        assert "Invalid bind IP" in bind_ip_problem(ip), ip
    try:
        mapping(bind_ip="ff02::1").validate()
    except ConfigError as e:
        assert "multicast/reserved" in str(e)
    else:
        raise AssertionError("a multicast bind IP was accepted")
    # the admin UI's address stays parse-only: a stored one never starts blocking saves
    AppConfig.from_dict({"admin_ui": {"bind_ip": "ff02::1"}}).validate()
    print("bind IP: wildcards and loopback of both families taken, multicast/reserved refused; "
          "IPv4-mapped judged by its IPv4 address  OK")


class _Terminal:
    def isatty(self):
        return True


def test_console_custom_ip():
    assert netinfo.validate_ip("::") == "::" and netinfo.validate_ip("::1") == "::1"
    assert netinfo.validate_ip("0:0:0:0:0:0:0:1") == "::1"
    assert netinfo.validate_ip("::ffff:10.0.0.5") == "10.0.0.5"
    for bad in ("ff02::1", "bogus"):
        try:
            netinfo.validate_ip(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    answers = iter(["c", "ff02::1", "c", "::1", "8090"])
    said = []
    real = builtins.input, sys.stdin, builtins.print
    builtins.input = lambda _prompt="": next(answers)
    sys.stdin = _Terminal()
    builtins.print = lambda *a, **k: said.append(" ".join(map(str, a)))
    try:
        assert console.choose_admin_bind() == ("::1", 8090)
    finally:
        builtins.input, sys.stdin, builtins.print = real
    assert any("multicast/reserved" in line for line in said), said
    print("console: a custom admin UI address may be :: or ::1, multicast is refused  OK")


def test_written_canonically():
    assert canonical_ip("0:0:0:0:0:0:0:0") == "::" and canonical_ip("::FFFF:10.0.0.5") == "10.0.0.5"
    assert canonical_ip("not-an-ip") == "not-an-ip" and canonical_ip(5) == 5
    cfg = AppConfig.from_dict({"mappings": [
        {"name": "long", "network": {"mode": "server", "bind_ip": "0:0:0:0:0:0:0:1", "port": 1}},
        {"name": "mapped", "network": {"mode": "udp", "bind_ip": "::ffff:10.0.0.5", "port": 2}},
        {"name": "plain", "network": {"mode": "server", "bind_ip": "::1", "port": 3}}]})
    assert [m.network.bind_ip for m in cfg.mappings] == ["::1", "10.0.0.5", "::1"]
    notes = " | ".join(cfg.load_notes)
    assert "'long': its bind IP '0:0:0:0:0:0:0:1' is now written '::1'" in notes, notes
    assert "'mapped'" in notes and "IPv4-mapped" in notes and "'plain'" not in notes, notes
    # every way into the config (form, REST API, import, cluster) goes through from_dict
    assert MappingConfig.from_dict({"network": {"bind_ip": "::FFFF:127.0.0.1"}}).network.bind_ip \
        == "127.0.0.1"
    print("bind IPs are written canonically (IPv4-mapped as IPv4), with a load note  OK")


def test_overlap_any_spelling():
    assert _ip_overlaps("0:0:0:0:0:0:0:0", "2001:db8::1"), "a long wildcard is a wildcard too"
    assert _ip_overlaps("0:0:0:0:0:0:0:1", "::1") and _ip_overlaps("::ffff:10.0.0.5", "10.0.0.5")
    assert not _ip_overlaps("10.0.0.5", "10.0.0.6")
    # validate()'s clash check sees through a spelling that skipped from_dict
    cfg = AppConfig(mappings=[mapping(name="a", bind_ip="2001:db8::1", port_dev="/dev/ttyS1"),
                              mapping(name="b", port_dev="/dev/ttyS2")])
    cfg.mappings[1].network = NetworkSettings(bind_ip="0:0:0:0:0:0:0:0", port=4001)
    assert any("overlapping" in p for p in cfg.problems()), cfg.problems()
    print("overlap check: any spelling of an address or wildcard  OK")


def test_dual_stack_setting():
    both = NetworkSettings(bind_ip="0.0.0.0", dual_stack=True)
    assert both.both_families and NetworkSettings(bind_ip="::", dual_stack=True).both_families
    assert not NetworkSettings(bind_ip="0.0.0.0").both_families
    assert not NetworkSettings(bind_ip="0.0.0.0", dual_stack="false").both_families, "text"
    assert not NetworkSettings(bind_ip="10.0.0.5", dual_stack=True).both_families
    # an edit can't set it with an address of its own; a stored one still loads and saves
    m = mapping(bind_ip="10.0.0.5", dual_stack=True)
    m.validate()
    try:
        m.validate_input()
    except ConfigError as e:
        assert "needs the bind IP 0.0.0.0 or ::" in str(e), e
    else:
        raise AssertionError("dual_stack with 10.0.0.5 was accepted")
    mapping(bind_ip="::", dual_stack=True).validate_input()
    mapping(mode="client", remote_host="h", remote_port=1, dual_stack=True).validate_input()
    # a UDP reply address of the other family is fine when the socket takes both
    mapping(mode="udp", dual_stack=True, udp_peer_host="fd00::1", udp_peer_port=9).validate_input()
    print("dual_stack: only with 0.0.0.0 / :: (edits refuse another address), a JSON true  OK")


def test_family_aware_overlaps():
    assert not _ip_overlaps("0.0.0.0", "::"), "TCP :: is IPv6 only: no clash with 0.0.0.0"
    assert not _ip_overlaps("2001:db8::5", "0.0.0.0") and not _ip_overlaps("10.0.0.5", "::")
    assert _ip_overlaps("::", "::1") and _ip_overlaps("0.0.0.0", "10.0.0.5")
    assert _ip_overlaps("0.0.0.0", "::1", a_both=True) and _ip_overlaps("10.0.0.5", "::", b_both=True)
    assert _ip_overlaps("::", "0.0.0.0", a_both=True, b_both=True)
    assert _ip_overlaps("fe80::1%eth0", "::") and not _ip_overlaps("fe80::1%eth0", "fe80::1%eth1")

    def problems(*nets, kind="server"):
        cfg = AppConfig(mappings=[mapping(name=f"m{i}", port_dev=f"/dev/ttyS{i}", mode=kind, **n)
                                  for i, n in enumerate(nets)])
        return [p for p in cfg.problems() if "overlapping" in p]

    assert not problems({"bind_ip": "0.0.0.0"}, {"bind_ip": "::"}), "one per family is fine"
    assert not problems({"bind_ip": "0.0.0.0"}, {"bind_ip": "2001:db8::5"})
    assert problems({"bind_ip": "0.0.0.0", "dual_stack": True}, {"bind_ip": "::1"})
    assert problems({"bind_ip": "::", "dual_stack": True}, {"bind_ip": "127.0.0.1"})
    assert problems({"bind_ip": "::"}, {"bind_ip": "::1"})
    assert not problems({"bind_ip": "0.0.0.0", "dual_stack": True}, {"bind_ip": "0.0.0.0", "port": 4002})

    def conflict(admin=("127.0.0.1", 4001), cluster=None, **net):
        cfg = AppConfig.from_dict({"admin_ui": {"bind_ip": admin[0], "port": admin[1]},
                                   "cluster": cluster or {}})
        return cfg.own_port_conflict(mapping(**net))

    assert not conflict(admin=("0.0.0.0", 4001), bind_ip="::"), "the UI on IPv4 only"
    assert conflict(admin=("", 4001), bind_ip="::"), "'' is every interface of both families"
    assert conflict(admin=("::1", 4001), bind_ip="0.0.0.0", dual_stack=True)
    assert conflict(bind_ip="0.0.0.0", dual_stack=True) and not conflict(bind_ip="::")
    keyed = {"enabled": True, "key": "k" * 20, "discovery_port": 4001}
    assert conflict(cluster=keyed, mode="udp", bind_ip="0.0.0.0")
    assert not conflict(cluster=keyed, mode="udp", bind_ip="2001:db8::5"), "no IPv4 beacons there"
    assert conflict(cluster=keyed, mode="udp", bind_ip="::1", dual_stack=True) == "", "not a wildcard"
    assert conflict(cluster=keyed, mode="udp", bind_ip="::", dual_stack=True)
    print("overlaps: a TCP wildcard clashes with its own family only, a dual-stack one with "
          "both; the admin UI and discovery checks use the same rule  OK")


async def _never_opens(_settings):
    await asyncio.Event().wait()


async def _wait(check, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def _refused(host, port):
    try:
        _r, w = await asyncio.open_connection(host, port)
    except OSError:
        return True
    w.close()
    return False


async def _listeners():
    lines = []
    log = lines.append
    with mock.patch.object(serial_io, "open_serial", _never_opens):
        # both families: two sockets, 0.0.0.0 and :: (port 0: each gets its own port here)
        for cls, extra in ((MappingRunner, {}), (ModbusGatewayRunner, {"protocol": "modbus"})):
            r = cls(mapping(port=0, dual_stack=True, **extra), logger=log)
            await r.start()
            try:
                ports = {s.family: s.getsockname()[1] for s in r._server.sockets}
                assert set(ports) == {socket.AF_INET, socket.AF_INET6}, ports
                assert any("listening on 0.0.0.0:0 and [::]:0, IPv4 and IPv6" in x
                           for x in lines), lines
                for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
                    _reader, writer = await asyncio.open_connection(host, ports[family])
                    await _wait(lambda: len(r._clients) == 1)
                    writer.close()
                    await _wait(lambda: not r._clients)
            finally:
                await r.stop()
        # :: alone takes IPv6 clients only (IPV6_V6ONLY), on every OS
        r = MappingRunner(mapping(port=0, bind_ip="::"), logger=log)
        await r.start()
        try:
            (sock,) = r._server.sockets
            port = sock.getsockname()[1]
            assert any("listening on [::]:0, IPv6 clients only" in x for x in lines), lines
            assert await _refused("127.0.0.1", port)
            assert not await _refused("::1", port)
        finally:
            await r.stop()
        # UDP for both: one socket on :: that takes IPv4 senders too
        r = MappingRunner(mapping(port=0, mode="udp", dual_stack=True), logger=log)
        await r.start()
        try:
            sock = r._udp_transport.get_extra_info("socket")
            assert sock.family == socket.AF_INET6
            assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
            assert any("UDP on 0.0.0.0:0 and [::]:0, IPv4 and IPv6" in x for x in lines), lines
            port = sock.getsockname()[1]
            seen = set()
            for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
                with socket.socket(family, socket.SOCK_DGRAM) as c:
                    c.sendto(b"x", (host, port))
                    await _wait(lambda: r.status.clients and r.status.clients[0]["peer"] not in seen)
                    seen.add(r.status.clients[0]["peer"])
        finally:
            await r.stop()


def test_listeners():
    if not ipv6_loopback():
        print("SKIP (part): no IPv6 loopback here: dual-stack listeners not tried")
        return
    asyncio.run(_listeners())
    print("listeners: dual_stack takes IPv4 and IPv6 clients (TCP, Modbus gateway, UDP), :: "
          "alone IPv6 only  OK")


def main():
    test_bind_ip_rule()
    test_console_custom_ip()
    test_written_canonically()
    test_overlap_any_spelling()
    test_dual_stack_setting()
    test_family_aware_overlaps()
    test_listeners()
    print("\nPASS: IPv6 bind addresses")


if __name__ == "__main__":
    main()
