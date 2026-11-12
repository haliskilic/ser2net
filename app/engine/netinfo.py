"""Host IP enumeration for the bind-IP pickers (admin UI + per-mapping).

Builds the selectable address list: the synthetic entries (every interface, IPv4 only
(0.0.0.0), IPv6 only (::) or both; localhost of each family) plus every up, non-loopback
NIC address: IPv4, and IPv6 other than link-local (and, on Linux, other than temporary or
deprecated privacy addresses, which go away). The IPv6 entries are left out where this
machine has no IPv6. Uses psutil when available, with a stdlib-only fallback so the app
still runs if psutil is missing.
"""
from __future__ import annotations

import functools
import ipaddress
import socket
from typing import Any

from ..config import bind_ip_problem, canonical_ip
from ..i18n import _

try:
    import psutil
except Exception:  # pragma: no cover - optional
    psutil = None

# the picker value for "every interface, IPv4 and IPv6": a mapping stores it as bind_ip
# 0.0.0.0 with network.dual_stack, the admin UI as bind_ip "" (asyncio's every interface)
BOTH_FAMILIES = "*"

# Linux lists its IPv6 addresses with their flags here; those of addresses a listener
# should not be bound to: temporary (privacy extensions: replaced every day or so),
# duplicate address found, deprecated
_IF_INET6 = "/proc/net/if_inet6"
_IFA_SKIP = 0x01 | 0x08 | 0x20


def _interface_stats() -> dict[str, Any]:
    """psutil.net_if_stats(), or {} where it can't be read: on Linux it reads /proc/net/dev,
    which a sandbox may hide (the systemd unit's ProcSubset=pid). Every interface then
    counts as up; the addresses come from psutil.net_if_addrs() all the same."""
    try:
        return psutil.net_if_stats() if psutil is not None else {}
    except Exception:
        return {}


def _nic_addresses() -> list[tuple[str, str]]:
    """Return [(interface_name, ipv4_address)] for up, non-loopback interfaces."""
    results: list[tuple[str, str]] = []
    if psutil is not None:
        try:
            stats = _interface_stats()
            for ifname, addrs in psutil.net_if_addrs().items():
                st = stats.get(ifname)
                if st is not None and not st.isup:
                    continue
                for a in addrs:
                    if a.family == socket.AF_INET and a.address:
                        ip = a.address
                        if ip.startswith("127."):
                            continue
                        results.append((ifname, ip))
            return results
        except Exception:
            pass
    # stdlib fallback: best-effort, may miss some interfaces
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if not ip.startswith("127."):
                results.append(("", ip))
    except Exception:
        pass
    return results


def _skipped_ipv6() -> set[str]:
    """Linux: the IPv6 addresses to leave out of the picker (_IFA_SKIP); empty elsewhere."""
    skip: set[str] = set()
    try:
        with open(_IF_INET6, encoding="ascii") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) >= 5 and int(fields[4], 16) & _IFA_SKIP:
                    skip.add(str(ipaddress.IPv6Address(int(fields[0], 16))))
    except (OSError, ValueError):
        pass
    return skip


def _usable_ipv6(ip: str) -> bool:
    try:
        addr = ipaddress.IPv6Address(ip.partition("%")[0])
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.ipv4_mapped is not None or bind_ip_problem(str(addr)))


def _nic_addresses6() -> list[tuple[str, str]]:
    """Return [(interface_name, ipv6_address)] a listener can be bound to: up interfaces,
    not loopback or link-local (those need the interface named), nor a temporary one."""
    results: list[tuple[str, str]] = []
    skip = _skipped_ipv6()
    if psutil is not None:
        try:
            stats = psutil.net_if_stats()
            for ifname, addrs in psutil.net_if_addrs().items():
                st = stats.get(ifname)
                if st is not None and not st.isup:
                    continue
                for a in addrs:
                    ip = canonical_ip(a.address or "")
                    if a.family == socket.AF_INET6 and _usable_ipv6(ip) and ip not in skip:
                        results.append((ifname, ip))
            return results
        except Exception:
            pass
    try:  # stdlib fallback: the addresses the host name has (best effort)
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            ip = canonical_ip(str(info[4][0]))
            if _usable_ipv6(ip) and ip not in skip:
                results.append(("", ip))
    except Exception:
        pass
    return results


@functools.lru_cache(maxsize=1)
def ipv6_available() -> bool:
    """Whether this machine can make IPv6 sockets (else the pickers offer IPv4 only)."""
    if not socket.has_ipv6:
        return False
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
    except OSError:
        return False
    return True


def own_ipv4() -> list[str]:
    """This host's non-loopback IPv4 addresses, sorted and unique (best effort)."""
    return sorted({ip for _ifname, ip in _nic_addresses()})


def primary_lan_ip(family: int = socket.AF_INET) -> str | None:
    """Best-effort primary outbound address of ``family`` (no traffic actually sent)."""
    probe = ("8.8.8.8", 80) if family == socket.AF_INET else ("2001:4860:4860::8888", 80)
    try:
        s = socket.socket(family, socket.SOCK_DGRAM)
        try:
            s.connect(probe)
            ip = canonical_ip(str(s.getsockname()[0]))
            return None if ipaddress.ip_address(ip).is_loopback else ip
        finally:
            s.close()
    except Exception:
        return None


def list_ip_candidates() -> list[dict[str, Any]]:
    """Ordered list of bind-IP options for the UI / console.

    Each item: {value, label, kind}. kind in {all, loopback, lan}. The value
    BOTH_FAMILIES stands for every interface, IPv4 and IPv6 (each caller stores it its
    way). A "Custom" entry is handled by the UI, not listed here.
    """
    v6 = ipv6_available()
    items: list[dict[str, Any]] = [
        {"value": "0.0.0.0", "label": _("All IPv4 interfaces (0.0.0.0)"), "kind": "all"}]
    if v6:
        items += [
            {"value": BOTH_FAMILIES, "label": _("All interfaces (IPv4 + IPv6)"), "kind": "all"},
            {"value": "::", "label": _("All IPv6 interfaces (::)"), "kind": "all"}]
    items.append({"value": "127.0.0.1", "label": _("Localhost (127.0.0.1)"), "kind": "loopback"})
    if v6:
        items.append({"value": "::1", "label": _("Localhost, IPv6 (::1)"), "kind": "loopback"})
    seen = {item["value"] for item in items}
    lan = sorted(set(_nic_addresses()))
    if v6:
        lan += sorted(set(_nic_addresses6()))
    primary = {primary_lan_ip(), primary_lan_ip(socket.AF_INET6) if v6 else None} - {None}
    for ifname, ip in lan:
        if ip in seen:
            continue
        seen.add(ip)
        label = f"{ifname} — {ip}" if ifname else ip
        if ip in primary:
            label += "  " + _("(primary)")
        items.append({"value": ip, "label": label, "kind": "lan"})
    return items


def validate_ip(value: str) -> str:
    """Check a user-entered bind IP by the mappings' rule (config.bind_ip_problem: the
    wildcards and loopback addresses of both families are fine); returns it normalized or
    raises ValueError."""
    if problem := bind_ip_problem(value):
        raise ValueError(problem)
    return canonical_ip(value)
