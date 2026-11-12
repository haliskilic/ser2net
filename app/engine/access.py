"""Who may use a listening mapping: the per-client policy of the transparent bridge
(bridge.MappingRunner) and the Modbus gateway (modbus_gateway.ModbusGatewayRunner).

- ``allowed_client_ips``: blank admits every source, otherwise only the listed IPs/CIDRs
  (an IPv4-mapped IPv6 peer counts as its IPv4 address, an IPv4-mapped entry as the IPv4
  addresses it maps, and a bare 0.0.0.0 or :: as every source, IPv4 and IPv6);
- at ``max_connections`` a source in ``priority_client_ips`` always gets in by kicking a
  client (the oldest non-priority one, else the oldest); any other source gets in only
  with ``kick_old_user``, by kicking the oldest non-priority client: it never takes a
  priority client's place, and is refused while only priority clients are connected.
  Only clients still connected count: one kicked (or otherwise on its way out) is neither
  counted nor kicked again, so connections that arrive together can't all take the same
  slot;
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
from typing import TYPE_CHECKING, Callable, Collection, Iterable, Optional, Protocol, Union

from ..config import canonical_ip, normalize_cidr
from . import tls

if TYPE_CHECKING:
    import ssl

    from ..config import NetworkSettings

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class Client(Protocol):
    """A connected client as the policy sees it."""
    peer: str            # "ip:port", for the log
    priority: bool       # admitted as a priority client
    connected_at: float  # time.time() when it connected

    @property
    def closing(self) -> bool: ...  # kicked, or ending: still listed until it is gone

    def kick(self, reason: str = ...) -> None: ...


# a bare wildcard in an IP/CIDR list: every client, of either family
_ANY: tuple[Network, ...] = (ipaddress.IPv4Network("0.0.0.0/0"), ipaddress.IPv6Network("::/0"))
_MAPPED = ipaddress.IPv6Network("::ffff:0:0/96")


def _unmapped(net: Network) -> Network:
    """An IPv4-mapped IPv6 network (::ffff:10.0.0.0/120) as the IPv4 network it maps
    (10.0.0.0/24): a client's IPv4-mapped address is compared as its IPv4 address."""
    if isinstance(net, ipaddress.IPv6Network) and net.prefixlen >= 96 and net.subnet_of(_MAPPED):
        return ipaddress.IPv4Network((int(net.network_address) & 0xFFFFFFFF, net.prefixlen - 96))
    return net


def parse_nets(cidrs: Iterable[str]) -> list[Network]:
    """The networks of an IP/CIDR list. A bare wildcard address (0.0.0.0, ::) stands for
    every client, IPv4 and IPv6 alike (0.0.0.0/0 and ::/0 are each one family); an
    IPv4-mapped entry for the IPv4 addresses it maps. An unparsable entry is skipped
    (validated elsewhere)."""
    nets: list[Network] = []
    for c in cidrs:
        with contextlib.suppress(ValueError, TypeError):
            if "/" not in c and ipaddress.ip_address(canonical_ip(c.strip())).is_unspecified:
                nets += _ANY
                continue
        with contextlib.suppress(ValueError):
            nets.append(_unmapped(ipaddress.ip_network(normalize_cidr(c), strict=False)))
    return nets


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


def pick_victim(clients: Collection[Client], priority: bool = True) -> Optional[Client]:
    """The client to evict to make room for a newcomer: the oldest non-priority one. Only
    a priority newcomer (``priority``) falls back to the oldest client overall; for any
    other there is none to evict while only priority clients are connected (None)."""
    pool = [c for c in clients if not c.priority]
    if not pool and priority:
        pool = list(clients)
    return min(pool, key=lambda c: c.connected_at) if pool else None


@dataclass(frozen=True)
class Admission:
    """AccessPolicy.admit's verdict on a new connection."""
    admitted: bool
    priority: bool = False     # the source is a priority client
    at_capacity: bool = False  # refused because the mapping already has max_connections


class AccessPolicy:
    def __init__(self, net: NetworkSettings) -> None:
        self.net = net
        self._allowed = parse_nets(net.allowed_client_ips)
        self._priority = parse_nets(net.priority_client_ips)

    def allowed(self, ip: str) -> bool:
        # empty allow-list = allow everyone
        return True if not self._allowed else ip_in_nets(ip, self._allowed)

    def is_priority(self, ip: str) -> bool:
        return ip_in_nets(ip, self._priority)

    def admit(self, ip: str, label: str, clients: Collection[Client],
              log: Callable[[str], None]) -> Admission:
        """Let a new connection from ``ip`` (``label``: ip:port, for the log) in or not,
        given the ``clients`` listed now. Kicks one of them to make room when the policy
        says so; logs each refusal and kick. The caller closes a refused connection, and
        adds an admitted one to ``clients`` before it awaits anything (so the next
        connection is decided with this one counted)."""
        if not self.allowed(ip):
            log(f"rejected {label}: not in allowed list")
            return Admission(False)
        priority = self.is_priority(ip)
        live = [c for c in clients if not c.closing]  # a kicked one is still listed a while
        if len(live) >= self.net.max_connections:
            # A priority client always gets in (kick to make room); a normal client
            # gets in only if kick_old_user is set, and never in a priority client's place.
            if not (priority or self.net.kick_old_user):
                log(f"rejected {label}: max connections reached")
                return Admission(False, priority, at_capacity=True)
            victim = pick_victim(live, priority)
            if victim is None:
                log(f"rejected {label}: max connections reached (all priority clients)")
                return Admission(False, priority, at_capacity=True)
            why = "priority client" if priority else "new client"
            log(f"kicking {victim.peer} for {why} {label}")
            victim.kick(f"kicked for {why} {label}")
        return Admission(True, priority)


def server_ssl(net: NetworkSettings) -> Optional[ssl.SSLContext]:
    """The TLS context of a mapping's TCP listener (None without TLS; engine/tls.py).
    Raises tls.TlsFileError when its files can't be used."""
    return tls.mapping_context(net)
