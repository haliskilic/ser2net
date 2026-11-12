"""IPv6 listeners: which bind IPs a mapping (and the console's admin UI picker) takes, how
two listeners' addresses are compared, and how bind IPs are written; listening for IPv4
and IPv6 clients on one mapping (network.dual_stack), and :: taking IPv6 clients only.

The listener parts bind 0.0.0.0 / :: with OS-assigned ports (run them in a throwaway
network namespace locally) and skip where this machine has no IPv6 loopback.

Run: python3 tests/test_ipv6.py
"""
import asyncio
import builtins
import ipaddress
import json
import os
import socket
import sys
import tempfile
import urllib.request
from types import SimpleNamespace
from unittest import mock

from _server import Browser, Server

from ser2netweb import config as config_mod
from ser2netweb import console, healthcheck, runtime
from ser2netweb.config import (
    AdminUI, AppConfig, ConfigError, MappingConfig, NetworkSettings, _ip_overlaps, bind_ip_problem,
    canonical_ip, host_port,
)
from ser2netweb.engine import netguard, netinfo, serial_io
from ser2netweb.engine.bridge import MappingRunner
from ser2netweb.engine.modbus_gateway import ModbusGatewayRunner


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
    # fixed port: the default for configs that are only checked; the runners started below
    # pass port=0
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
    chosen, said = _console("c", "ff02::1", "c", "::1", "8090")
    assert chosen == ("::1", 8090), chosen
    assert any("multicast/reserved" in line for line in said), said
    assert any("http://[::1]:8090" in line for line in said), said
    print("console: a custom admin UI address may be :: or ::1, multicast is refused  OK")


def _console(*answers, current=None, tty=True):
    feed = iter(answers)
    said = []
    real = builtins.input, sys.stdin, builtins.print
    builtins.input = lambda _prompt="": next(feed)
    sys.stdin = _Terminal() if tty else SimpleNamespace(isatty=lambda: False)
    builtins.print = lambda *a, **k: said.append(" ".join(map(str, a)))
    try:
        return console.choose_admin_bind(current), said
    finally:
        builtins.input, sys.stdin, builtins.print = real


def _addr(family, address):
    return SimpleNamespace(family=family, address=address)


class _Psutil:
    """psutil's view of eth0 (up) and eth1 (down)."""

    @staticmethod
    def net_if_stats():
        return {"eth0": SimpleNamespace(isup=True), "eth1": SimpleNamespace(isup=False),
                "lo": SimpleNamespace(isup=True)}

    @staticmethod
    def net_if_addrs():
        v4, v6 = socket.AF_INET, socket.AF_INET6
        return {"lo": [_addr(v4, "127.0.0.1"), _addr(v6, "::1")],
                "eth0": [_addr(v4, "10.0.0.5"), _addr(v6, "2001:db8::5"),
                         _addr(v6, "2001:db8::abcd:1234"), _addr(v6, "fe80::1%eth0"),
                         _addr(v6, "fd00::5"), _addr(v6, "::ffff:10.0.0.5")],
                "eth1": [_addr(v4, "10.0.1.5"), _addr(v6, "2001:db8:1::5")]}


def test_pickers():
    d = tempfile.mkdtemp(prefix="s2n_v6_")
    flags = os.path.join(d, "if_inet6")
    with open(flags, "w") as fh:  # 2001:db8::abcd:1234 is a temporary (privacy) address
        for ip, flag in (("2001:db8::5", "80"), ("2001:db8::abcd:1234", "01")):
            fh.write(f"{int(ipaddress.IPv6Address(ip)):032x} 02 40 00 {flag}     eth0\n")
    with mock.patch.object(netinfo, "psutil", _Psutil), \
            mock.patch.object(netinfo, "_IF_INET6", flags), \
            mock.patch.object(netinfo, "primary_lan_ip",
                              lambda family=socket.AF_INET: {socket.AF_INET: "10.0.0.5"}.get(family)):
        with mock.patch.object(netinfo, "ipv6_available", lambda: True):
            items = netinfo.list_ip_candidates()
        with mock.patch.object(netinfo, "ipv6_available", lambda: False):
            v4only = [i["value"] for i in netinfo.list_ip_candidates()]
        assert v4only == ["0.0.0.0", "127.0.0.1", "10.0.0.5"], v4only
        values = [i["value"] for i in items]
        assert values == ["0.0.0.0", "*", "::", "127.0.0.1", "::1", "10.0.0.5", "2001:db8::5",
                          "fd00::5"], values
        labels = {i["value"]: i["label"] for i in items}
        assert labels["*"] == "All interfaces (IPv4 + IPv6)" and "(primary)" in labels["10.0.0.5"]
        assert labels["0.0.0.0"] == "All IPv4 interfaces (0.0.0.0)"
        assert labels["2001:db8::5"] == "eth0 — 2001:db8::5"

        # the console: the "*" entry is the admin UI's "" (every interface, both families)
        with mock.patch.object(netinfo, "ipv6_available", lambda: True):
            (ip, port), _said = _console(str(values.index("*") + 1), "8091")
        assert (ip, port) == ("", 8091)
    # without a terminal a configured "" is kept, as any other address
    (ip, _port), said = _console(current=AdminUI(bind_ip=""), tty=False)
    assert ip == "" and "*:8080 (as configured)" in said[0], (ip, said)
    (ip, _port), said = _console(current=AdminUI(), tty=False)
    assert ip == "127.0.0.1" and "127.0.0.1:8080 (loopback)" in said[0], (ip, said)
    print("pickers: every interface for IPv4, IPv6 or both, both loopbacks, LAN IPv6 addresses "
          "(not link-local or temporary); IPv4 only without IPv6  OK")


class _Req:
    class state:
        csrf_token = "t"
        user = None


def test_mapping_form():
    from ser2netweb.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields
    from ser2netweb.web.server import make_templates

    base = {"name": "f", "serial_port": "/dev/ttyS0", "network_mode": "server",
            "network_port": "4001"}

    def parsed(existing=None, **form):
        form = dict(base, **form)
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing, form)
        return m.network

    shown = {"network_dual_stack_shown": "1"}
    net = parsed(network_bind_ip="*", **shown)
    assert (net.bind_ip, net.dual_stack, net.both_families) == ("0.0.0.0", True, True)
    assert parsed(network_bind_ip="::", **shown).dual_stack is False
    assert parsed(network_bind_ip="custom", network_bind_ip_custom="::1", **shown).bind_ip == "::1"
    # an older node's form has no such field: the stored choice stays with a wildcard
    stored = mapping(name="f", bind_ip="0.0.0.0", dual_stack=True)
    assert parsed(stored, network_bind_ip="0.0.0.0").both_families
    assert parsed(stored, network_bind_ip="10.0.0.5").dual_stack is False
    assert parsed(mapping(name="f"), network_bind_ip="*").both_families, "this node's * entry"
    assert parsed(stored, network_bind_ip="0.0.0.0", **shown).dual_stack is False, "unpicked"

    ips = [{"value": "0.0.0.0", "label": "v4"}, {"value": "*", "label": "both"},
           {"value": "::", "label": "v6"}]
    tpl = make_templates().env.get_template("_mapping_form.html")

    def selected(m):
        html = tpl.render(request=_Req, mapping=m, is_new=False, modbus_points_json="",
                          ports=[], ips=ips, error=None, links={})
        assert 'name="network_dual_stack_shown"' in html
        assert "0.0.0.0 takes IPv4 clients only, :: IPv6 clients only" in html
        return [v for v, _ in __import__("re").findall(
            r'<option value="([^"]*)" (selected)', html)]

    assert "*" in selected(stored) and "0.0.0.0" not in selected(stored)
    assert "::" in selected(mapping(name="f", bind_ip="::"))
    print("mapping form: “All interfaces (IPv4 + IPv6)” is 0.0.0.0 + dual_stack; an older "
          "node's form keeps the stored choice  OK")


def test_env_and_healthcheck():
    keep = {k: os.environ.pop(k, None) for k in ("SER2NETWEB_BIND_IP", "SER2NETWEB_PORT",
                                                 "SER2NET_BIND_IP", "SER2NET_PORT")}
    try:
        os.environ["SER2NETWEB_BIND_IP"] = "*"
        admin = AdminUI()
        assert runtime._env_bind_override(admin) is True and admin.bind_ip == ""
        d = tempfile.mkdtemp(prefix="s2n_v6hc_")
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"admin_ui": {"bind_ip": "", "port": 8124}}, fh)
        assert healthcheck.target(cfg) == "http://127.0.0.1:8124/healthz"
        del os.environ["SER2NETWEB_BIND_IP"]
        assert healthcheck.target(cfg) == "http://127.0.0.1:8124/healthz", "'' too"
        os.environ["SER2NETWEB_BIND_IP"] = "**"
        try:
            runtime._env_bind_override(AdminUI())
        except ConfigError as e:
            assert "or * (every interface, IPv4 and IPv6)" in str(e), e
        else:
            raise AssertionError("** accepted")
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    print("SER2NETWEB_BIND_IP=*: every interface, IPv4 and IPv6; the healthcheck probes 127.0.0.1  OK")


def test_admin_ui_every_interface():
    if not ipv6_loopback():
        print("SKIP (part): no IPv6 loopback here: the admin UI on both families not tried")
        return
    srv = Server()
    srv.env["SER2NETWEB_BIND_IP"] = "*"
    with srv:
        for host in ("127.0.0.1", "[::1]"):
            with urllib.request.urlopen(f"http://{host}:{srv.port}/healthz", timeout=10) as r:
                assert r.read().strip() == b"ok", host
        assert f"admin UI on http://*:{srv.port}" in srv.output(), srv.output()[-2000:]
        assert srv.saved_config()["admin_ui"]["bind_ip"] == ""
        b = Browser(srv.base)
        _st, _h, page = b.get("/setup")
        assert f"*:{srv.port}" in page, page[-3000:]
    print("admin UI on every interface (SER2NETWEB_BIND_IP=*): answers on 127.0.0.1 and ::1  OK")


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


def test_udp_wildcard_kept():
    """A UDP mapping on :: took IPv4 datagrams too where the OS makes IPv6 sockets dual-stack
    (Linux): IPV6_V6ONLY is now set, so a config from before keeps that behaviour as
    dual_stack, said once in a load note; one that has the key (or another OS) is left."""
    legacy = {"mappings": [
        {"name": "u6", "network": {"mode": "udp", "bind_ip": "::", "port": 1}},
        {"name": "u6-long", "network": {"mode": "udp", "bind_ip": "0:0:0:0:0:0:0:0", "port": 2}},
        {"name": "u6-new", "network": {"mode": "udp", "bind_ip": "::", "dual_stack": False,
                                       "port": 3}},
        {"name": "t6", "network": {"mode": "server", "bind_ip": "::", "port": 4}},
        {"name": "u4", "network": {"mode": "udp", "bind_ip": "0.0.0.0", "port": 5}}]}
    for dual_default, expect in ((True, [True, True, False, False, False]),
                                 (False, [False] * 5)):
        with mock.patch.object(config_mod, "_ipv6_takes_ipv4", lambda d=dual_default: d):
            cfg = AppConfig.from_dict(legacy)
        assert [m.network.dual_stack for m in cfg.mappings] == expect, (dual_default, cfg.mappings)
        notes = [n for n in cfg.load_notes if "takes IPv6 datagrams only" in n]
        assert len(notes) == sum(expect) and all("'u6" in n for n in notes), notes
        assert not [p for p in cfg.problems() if "overlapping" in p]
    saved = AppConfig.from_dict(legacy).to_dict()["mappings"][0]["network"]
    assert "dual_stack" in saved, "saved with the key: decided once"
    # the same for a mapping written for an older release that comes in another way (REST
    # API, an older cluster node, an import of mappings): the key decides when it is there
    with mock.patch.object(config_mod, "_ipv6_takes_ipv4", lambda: True):
        assert mapping(mode="udp", bind_ip="::").network.dual_stack is True
        assert mapping(mode="udp", bind_ip="::", dual_stack=False).network.dual_stack is False
        assert mapping(mode="server", bind_ip="::").network.dual_stack is False
    print("UDP on :: from an older config: dual_stack where the OS made it take IPv4 too  OK")


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
    # UDP alike: :: takes IPv6 only on every OS now (IPV6_V6ONLY set, not the OS default)
    assert not problems({"bind_ip": "0.0.0.0"}, {"bind_ip": "::", "dual_stack": False}, kind="udp")
    assert problems({"bind_ip": "0.0.0.0"}, {"bind_ip": "::", "dual_stack": True}, kind="udp")

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
    assert not conflict(cluster=keyed, mode="udp", bind_ip="::", dual_stack=False), \
        ":: gets no IPv4 beacons"
    print("overlaps: a TCP wildcard clashes with its own family only, a dual-stack one with "
          "both; the admin UI and discovery checks use the same rule  OK")


class _Peer:
    def __init__(self, peername):
        self.peername = peername

    def get_extra_info(self, name):
        return self.peername if name == "peername" else None


def test_labels():
    assert host_port("::1", 4001) == "[::1]:4001" and host_port("10.0.0.5", 1) == "10.0.0.5:1"
    assert host_port("", 8080) == "*:8080" and host_port("plc.local", 502) == "plc.local:502"
    assert host_port("[::1]", 1) == "[::1]:1"
    for peername, ip, label in (
            (("2001:db8::7", 50123, 0, 0), "2001:db8::7", "[2001:db8::7]:50123"),
            (("::ffff:10.0.0.9", 50124, 0, 0), "10.0.0.9", "10.0.0.9:50124"),
            (("10.0.0.9", 50125), "10.0.0.9", "10.0.0.9:50125"),
            (("fe80::1%eth0", 7, 0, 2), "fe80::1%eth0", "[fe80::1%eth0]:7"),
            (None, "?", "?")):
        peer = _Peer(peername)
        assert netguard.peer_ip(peer) == ip and netguard.peer_label(peer) == label, peername
    assert [NetworkSettings(**n).bind_shown for n in (
        {"bind_ip": "::1"}, {"bind_ip": "10.0.0.5"}, {"bind_ip": "0.0.0.0", "dual_stack": True},
        {"bind_ip": "::"})] == ["[::1]", "10.0.0.5", "*", "[::]"]

    from ser2netweb.web.server import make_templates
    env = make_templates().env
    maps = [mapping(name="v6", bind_ip="::1", port=4001),
            mapping(name="both", bind_ip="0.0.0.0", dual_stack=True, port=4002),
            mapping(name="out", mode="client", remote_host="2001:db8::9", remote_port=4003)]
    out = env.get_template("_mappings_body.html").render(mappings=maps, statuses={},
                                                         can_edit=False)
    assert ">[:<wbr>:<wbr>1]:<wbr>4001<" in out, out
    assert 'title="All interfaces (IPv4 + IPv6)">*</span>:<wbr>4002' in out, out
    assert "→ [2001:<wbr>db8:<wbr>:<wbr>9]:<wbr>4003" in out, out
    print("labels: IPv6 addresses in brackets (logs, clients, dashboard), IPv4-mapped clients "
          "as IPv4, a dual-stack listener as *  OK")


def test_address_keys():
    key = netguard.address_key
    assert key("2001:db8:1:2:aaaa::1") == key("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert key("2001:db8:1:3::1") != key("2001:db8:1:2::1")
    assert key("fe80::1%eth0") == "fe80::/64" and key("::1") == "::/64"
    assert key("::ffff:10.0.0.9") == key("10.0.0.9") == "10.0.0.9" and key("?") == "?"

    class T:
        def is_closing(self):
            return False

    pool = netguard.Pool(total=10, per_ip=2)
    pool.add(T(), "2001:db8::1")
    pool.add(T(), "2001:db8::2")  # another address of the same /64: the same source
    assert pool.full("2001:db8::3") == "2 at once from one /64 network (2001:db8::/64) is the limit"
    assert pool.full("2001:db8:0:1::1") == "", "another /64"
    pool.add(T(), "::ffff:10.0.0.9")
    pool.add(T(), "10.0.0.9")  # one IPv4 client, however its address is written
    assert pool.full("10.0.0.9") == "2 at once from one address is the limit"
    assert pool.count("::ffff:10.0.0.9") == 2
    print("per-address limits: an IPv6 source by its /64, an IPv4-mapped one as IPv4  OK")


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
                    (client,) = r._clients
                    local = writer.get_extra_info("sockname")
                    assert client.peer == host_port(host, local[1]), (client.peer, local)
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
        # UDP on :: alone: IPv6 senders only, whatever the OS default
        r = MappingRunner(mapping(port=0, mode="udp", bind_ip="::", dual_stack=False), logger=log)
        await r.start()
        try:
            sock = r._udp_transport.get_extra_info("socket")
            assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
            assert any("UDP on [::]:0, IPv6 clients only" in x for x in lines), lines
            port = sock.getsockname()[1]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as c:
                c.sendto(b"x", ("127.0.0.1", port))  # nobody listens there for IPv4
            with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as c:
                c.sendto(b"x", ("::1", port))
                await _wait(lambda: r.status.clients)
            await asyncio.sleep(0.1)
            assert [c["peer"].startswith("[::1]:") for c in r.status.clients] == [True], \
                r.status.clients
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
            for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
                with socket.socket(family, socket.SOCK_DGRAM) as c:
                    c.bind((host, 0))
                    want = host_port(host, c.getsockname()[1])  # an IPv4 sender as IPv4
                    c.sendto(b"x", (host, port))
                    await _wait(lambda: [p["peer"] for p in r.status.clients] == [want])
        finally:
            await r.stop()


def test_no_ipv6_here():
    """Where Python or the kernel has no IPv6, a mapping for both families listens on IPv4
    (and says so) instead of failing to start."""
    net = mapping(port=0, mode="udp", dual_stack=True).network
    with mock.patch.object(netguard.socket, "has_ipv6", False):
        assert netguard.listen_hosts(net) == ["0.0.0.0"]
    assert netguard.listen_hosts(net) == (["0.0.0.0", "::"] if socket.has_ipv6 else ["0.0.0.0"])
    real = socket.socket

    def no_ipv6(family=socket.AF_INET, *a, **k):
        if family == socket.AF_INET6:
            raise OSError(97, "Address family not supported by protocol")
        return real(family, *a, **k)

    with mock.patch.object(netguard.socket, "socket", no_ipv6):
        sock = asyncio.run(netguard.udp_socket(net))
    with sock:
        assert sock.family == socket.AF_INET and sock.getsockname()[0] == "0.0.0.0"
        assert netguard.listen_where(net, [sock]) == \
            "0.0.0.0:0, IPv4 only (IPv6 is not available here)"
    print("no IPv6 here: a dual-stack mapping listens on IPv4 and says so  OK")


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
    test_udp_wildcard_kept()
    test_dual_stack_setting()
    test_family_aware_overlaps()
    test_labels()
    test_pickers()
    test_mapping_form()
    test_env_and_healthcheck()
    test_admin_ui_every_interface()
    test_address_keys()
    test_no_ipv6_here()
    test_listeners()
    print("\nPASS: IPv6 bind addresses")


if __name__ == "__main__":
    main()
