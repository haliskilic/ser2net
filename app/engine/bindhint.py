"""Why a listener can't take its port when the system refuses it, and what to do.

On Linux a process that isn't root may listen on a port below
net.ipv4.ip_unprivileged_port_start (1024 unless changed) only with the
CAP_NET_BIND_SERVICE capability. The systemd unit runs ser2net as such a user, and so
does the Docker image (uid 10001) on the host network, so Modbus/TCP's 502 or a web UI on
80 fails with EACCES: "permission denied", with neither the cause nor the fix. On Windows
the same error (WSAEACCES, 10013) means another program holds the port exclusively, or
Windows keeps it in an excluded range.

hint() names the case for a mapping's status (the dashboard words it in the page's
language); explain() is the English line for the logs; admin_port_note() is the start-up
warning for an admin UI port that this process can't listen on.
"""
from __future__ import annotations

import errno
import os
import sys
from typing import Any

PRIVILEGED_PORT = "privileged_port"   # Linux: a port below 1024 without the capability
EXCLUDED_PORT = "excluded_port"       # Windows: a port another program or Windows keeps
CAP_NET_BIND_SERVICE = 10             # its bit in the capability sets (linux/capability.h)
_WSAEACCES = 10013

_EXPLAIN = {
    PRIVILEGED_PORT: (
        "ports below {start} need the CAP_NET_BIND_SERVICE capability on Linux (systemd: "
        "AmbientCapabilities=CAP_NET_BIND_SERVICE; Docker on the host network: "
        "SER2NET_LOW_PORTS=1), or use port {start} or above (README: Ports below 1024)"),
    EXCLUDED_PORT: (
        "Windows doesn't let ser2net use this port: another program holds it, or Windows "
        "reserves it (netsh interface ipv4 show excludedportrange protocol=tcp); pick "
        "another port"),
}


def unprivileged_port_start() -> int:
    """The first port a process without the capability may listen on (Linux): 1024 when
    the setting can't be read (the systemd unit's ProcSubset=pid hides /proc/sys)."""
    try:
        with open("/proc/sys/net/ipv4/ip_unprivileged_port_start", encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return 1024


def _euid() -> int:
    if sys.platform == "win32":
        return -1
    return os.geteuid()


def may_bind_low_ports() -> bool:
    """Linux: whether this process may listen below unprivileged_port_start(): root, or
    CAP_NET_BIND_SERVICE among its effective capabilities."""
    if _euid() == 0:
        return True
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) >> CAP_NET_BIND_SERVICE & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def hint(error: BaseException, port: int, platform: str = sys.platform) -> str:
    """PRIVILEGED_PORT / EXCLUDED_PORT when the bind of ``port`` failed for that reason,
    else "". (asyncio keeps the errno when it rewords the error.)"""
    code = getattr(error, "errno", None)
    if platform.startswith("linux"):
        if code == errno.EACCES and 0 < port < unprivileged_port_start():
            return PRIVILEGED_PORT
    elif platform == "win32":
        if code in (errno.EACCES, _WSAEACCES) or getattr(error, "winerror", None) == _WSAEACCES:
            return EXCLUDED_PORT
    return ""


def for_mapping(error: BaseException, mapping: Any) -> str:
    """hint() for a mapping that failed to start: only a listener's port counts."""
    net = getattr(mapping, "network", None)
    if getattr(mapping, "kind", "") != "net" or not getattr(net, "listens", False):
        return ""
    return hint(error, int(getattr(net, "port", 0) or 0))


def explain(name: str) -> str:
    """The English line for a hint (the logs)."""
    text = _EXPLAIN.get(name, "")
    return text.format(start=unprivileged_port_start()) if text else ""


def admin_port_note(port: int, platform: str = sys.platform) -> str:
    """The start-up warning when the admin UI's port is one this process can't listen on
    (Linux): the web UI would not start (and a service manager restart it again and
    again). "" otherwise."""
    if not platform.startswith("linux") or not 0 < port < unprivileged_port_start():
        return ""
    if may_bind_low_ports():
        return ""
    return (f"WARNING: the admin UI port {port} is below {unprivileged_port_start()}, which "
            f"this account (uid {_euid()}) can't listen on, so the web UI can't start: "
            + explain(PRIVILEGED_PORT))
