"""The bind-IP pickers and the cluster beacons find this machine's interfaces, also where
/proc/net is hidden (app/engine/netinfo.py, app/engine/cluster.py).

The systemd unit runs ser2net with ProcSubset=pid, which hides /proc/net: psutil's
net_if_stats() (it reads /proc/net/dev) then fails, while net_if_addrs() (getifaddrs, over
netlink, which the unit allows) still works. Without its own handling the whole psutil
branch fell back to the host name's addresses (usually only 127.0.1.1, so the pickers
listed no LAN address). Checked with a stand-in psutil: the interfaces and addresses are
listed when net_if_stats() fails (they count as up), IPv4 and IPv6, a down interface is
still left out when it works, and the beacons go to each interface's directed broadcast
too.

Run: python3 tests/test_netinfo.py
"""
import socket
import sys
from types import SimpleNamespace

import _server  # noqa: F401  (puts the checkout and ./lib on sys.path)

from app.engine import cluster, netinfo


class FakePsutil:
    """net_if_addrs() as psutil gives it; net_if_stats() raises like a hidden /proc/net."""

    def __init__(self, stats_error: bool = True, down: tuple = ()):
        self.stats_error = stats_error
        self.down = set(down)

    def net_if_addrs(self):
        def a(family, address, broadcast=None):
            return SimpleNamespace(family=family, address=address, netmask=None,
                                   broadcast=broadcast, ptp=None)
        return {
            "lo": [a(socket.AF_INET, "127.0.0.1")],
            "eth0": [a(socket.AF_INET, "192.0.2.10", "192.0.2.255"),
                     a(socket.AF_INET6, "2001:db8::10")],
            "wlan0": [a(socket.AF_INET, "198.51.100.7", "198.51.100.255")],
        }

    def net_if_stats(self):
        if self.stats_error:
            raise FileNotFoundError(2, "No such file or directory", "/proc/net/dev")
        return {name: SimpleNamespace(isup=name not in self.down)
                for name in ("lo", "eth0", "wlan0")}


def _with_psutil(fake, fn):
    saved_netinfo, saved_module = netinfo.psutil, sys.modules.get("psutil")
    netinfo.psutil = fake
    sys.modules["psutil"] = fake  # cluster imports it where it uses it
    try:
        return fn()
    finally:
        netinfo.psutil = saved_netinfo
        if saved_module is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = saved_module


def test_interfaces_without_proc_net():
    got = _with_psutil(FakePsutil(stats_error=True), netinfo._nic_addresses)
    assert sorted(got) == [("eth0", "192.0.2.10"), ("wlan0", "198.51.100.7")], got
    values = [c["value"] for c in _with_psutil(FakePsutil(), netinfo.list_ip_candidates)]
    assert "192.0.2.10" in values and "198.51.100.7" in values, values
    print("net_if_stats() failing (hidden /proc/net): the interfaces are listed by name  OK")


def test_ipv6_interfaces_without_proc_net():
    got = _with_psutil(FakePsutil(stats_error=True), netinfo._nic_addresses6)
    assert ("eth0", "2001:db8::10") in got, got
    got = _with_psutil(FakePsutil(stats_error=False, down=("eth0",)), netinfo._nic_addresses6)
    assert ("eth0", "2001:db8::10") not in got, got
    print("IPv6: net_if_stats() failing lists the interfaces' addresses too; a down one not  OK")


def test_down_interface_left_out():
    got = _with_psutil(FakePsutil(stats_error=False, down=("wlan0",)), netinfo._nic_addresses)
    assert got == [("eth0", "192.0.2.10")], got
    print("with interface stats readable, a down interface is still left out  OK")


def test_beacons_use_directed_broadcasts():
    got = _with_psutil(FakePsutil(stats_error=True), cluster.ClusterDiscovery._broadcast_addrs)
    assert got == ["255.255.255.255", "192.0.2.255", "198.51.100.255"], got
    print("beacons: the global and each interface's directed broadcast  OK")


def test_real_host():
    # whatever this machine has: no exception, loopback never listed
    addrs = netinfo._nic_addresses()
    assert all(not ip.startswith("127.") for _name, ip in addrs), addrs
    assert cluster.ClusterDiscovery._broadcast_addrs()[0] == "255.255.255.255"
    print(f"this host: {len(addrs)} interface address(es)  OK")


if __name__ == "__main__":
    test_interfaces_without_proc_net()
    test_ipv6_interfaces_without_proc_net()
    test_down_interface_left_out()
    test_beacons_use_directed_broadcasts()
    test_real_host()
    print("\nPASS: interface lists")
