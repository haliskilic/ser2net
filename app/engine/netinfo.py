"""Host IP enumeration for the bind-IP pickers (admin UI + per-mapping).

Builds the selectable address list: the two synthetic entries (all-interfaces
0.0.0.0 and localhost) plus every up, non-loopback NIC IPv4 address. Uses psutil
when available, with a stdlib-only fallback so the app still runs if psutil is
missing.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional
    psutil = None


def _nic_addresses() -> list[tuple[str, str]]:
    """Return [(interface_name, ipv4_address)] for up, non-loopback interfaces."""
    results: list[tuple[str, str]] = []
    if psutil is not None:
        try:
            stats = psutil.net_if_stats()
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


def primary_lan_ip() -> str | None:
    """Best-effort primary outbound IPv4 (no traffic actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return ip if not ip.startswith("127.") else None
        finally:
            s.close()
    except Exception:
        return None


def list_ip_candidates() -> list[dict[str, Any]]:
    """Ordered list of bind-IP options for the UI / console.

    Each item: {value, label, kind}. kind in {all, loopback, lan}.
    A "Custom" entry is handled by the UI, not listed here.
    """
    primary = primary_lan_ip()
    items: list[dict[str, Any]] = [
        {"value": "0.0.0.0", "label": "All interfaces (0.0.0.0)", "kind": "all"},
        {"value": "127.0.0.1", "label": "Localhost (127.0.0.1)", "kind": "loopback"},
    ]
    seen = {"0.0.0.0", "127.0.0.1"}
    for ifname, ip in sorted(set(_nic_addresses())):
        if ip in seen:
            continue
        seen.add(ip)
        label = f"{ifname} — {ip}" if ifname else ip
        if ip == primary:
            label += "  (primary)"
        items.append({"value": ip, "label": label, "kind": "lan"})
    return items


def validate_ip(value: str) -> str:
    """Validate a user-entered IP; returns it normalized or raises ValueError."""
    ip = ipaddress.ip_address(value)  # raises ValueError on bad input
    if ip.is_multicast or ip.is_reserved:
        raise ValueError("Address cannot be multicast/reserved.")
    return str(ip)
