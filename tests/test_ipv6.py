"""IPv6 listeners: which bind IPs a mapping (and the console's admin UI picker) takes, how
two listeners' addresses are compared, and how bind IPs are written.

Run: python3 tests/test_ipv6.py
"""
import builtins
import sys

from _server import ROOT  # noqa: F401  (puts the app on sys.path)

from app import console
from app.config import (
    AppConfig, ConfigError, MappingConfig, NetworkSettings, _ip_overlaps, bind_ip_problem,
    canonical_ip,
)
from app.engine import netinfo


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


def main():
    test_bind_ip_rule()
    test_console_custom_ip()
    test_written_canonically()
    test_overlap_any_spelling()
    print("\nPASS: IPv6 bind addresses")


if __name__ == "__main__":
    main()
