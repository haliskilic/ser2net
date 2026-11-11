"""Who may use a listening mapping: the per-client policy of the transparent bridge
(bridge.MappingRunner) and the Modbus gateway (modbus_gateway.ModbusGatewayRunner).

- ``allowed_client_ips``: blank admits every source, otherwise only the listed IPs/CIDRs
  (an IPv4-mapped IPv6 peer counts as its IPv4 address); entries that are no address or
  network (a hand-edited config.json) are left out, and a list with no usable entry at
  all admits nobody, never everyone;
- at ``max_connections`` a source in ``priority_client_ips`` always gets in by kicking a
  client (the oldest non-priority one, else the oldest); any other source gets in only
  with ``kick_old_user`` (kicking the same way), otherwise it is refused;
- ``read_only``: a client may watch the device but not change it. Each runner enforces it
  for its protocol: the bridge never writes a client's bytes to the port (and its RFC2217
  session refuses line changes), the Modbus gateway answers write requests with an
  exception instead of putting them on the bus and refuses MQTT/Sparkplug writes.

Also the TLS context of a listener (``network.tls``), the same for both runners.
"""
from __future__ import annotations

import contextlib
import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Collection, Optional, Protocol

from ..config import ClientNetwork as Network
from ..config import client_networks

if TYPE_CHECKING:
    import ssl

    from ..config import NetworkSettings


class Client(Protocol):
    """A connected client as the policy sees it."""
    peer: str            # "ip:port", for the log
    priority: bool       # admitted as a priority client
    connected_at: float  # time.time() when it connected

    def kick(self, reason: str = ...) -> None: ...


def ip_in_nets(ip: str, nets: Collection[Network]) -> bool:
    if not nets:
        return False
    with contextlib.suppress(ValueError):
        addr = ipaddress.ip_address(ip)
        # treat IPv4-mapped IPv6 (::ffff:1.2.3.4) as the underlying IPv4
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in net for net in nets)
    return False


def pick_victim(clients: Collection[Client]) -> Optional[Client]:
    """The client to evict when making room: the oldest non-priority one, falling back to
    the oldest client overall."""
    if not clients:
        return None
    pool = [c for c in clients if not c.priority] or list(clients)
    return min(pool, key=lambda c: c.connected_at)


@dataclass(frozen=True)
class Admission:
    """AccessPolicy.admit's verdict on a new connection."""
    admitted: bool
    priority: bool = False     # the source is a priority client
    at_capacity: bool = False  # refused because the mapping already has max_connections


class AccessPolicy:
    def __init__(self, net: NetworkSettings) -> None:
        self.net = net
        self._allowed, _ignored = client_networks(net.allowed_client_ips)
        self._priority, _ignored = client_networks(net.priority_client_ips)
        # no allow-list = everyone; one whose entries are all unusable = nobody (the runner
        # logs which entries it left out at start: MappingConfig.ignored_settings)
        self._everyone = not net.allowed_client_ips

    def allowed(self, ip: str) -> bool:
        return self._everyone or ip_in_nets(ip, self._allowed)

    def is_priority(self, ip: str) -> bool:
        return ip_in_nets(ip, self._priority)

    def admit(self, ip: str, label: str, clients: Collection[Client],
              log: Callable[[str], None]) -> Admission:
        """Let a new connection from ``ip`` (``label``: ip:port, for the log) in or not,
        given the ``clients`` connected now. Kicks one of them to make room when the policy
        says so; logs each refusal and kick. The caller closes a refused connection."""
        if not self.allowed(ip):
            log(f"rejected {label}: not in allowed list")
            return Admission(False)
        priority = self.is_priority(ip)
        if len(clients) >= self.net.max_connections:
            # A priority client always gets in (kick to make room); a normal client
            # gets in only if kick_old_user is set; otherwise it's refused.
            if not (priority or self.net.kick_old_user):
                log(f"rejected {label}: max connections reached")
                return Admission(False, priority, at_capacity=True)
            victim = pick_victim(clients)
            if victim is not None:
                why = "priority client" if priority else "new client"
                log(f"kicking {victim.peer} for {why} {label}")
                victim.kick(f"kicked for {why} {label}")
        return Admission(True, priority)


def server_ssl(net: NetworkSettings) -> Optional[ssl.SSLContext]:
    """The TLS context of a mapping's TCP listener (None without TLS)."""
    if not net.tls:
        return None
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(net.tls_cert, net.tls_key)
    return ctx
