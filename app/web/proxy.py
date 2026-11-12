"""The web UI behind a reverse proxy (Settings -> Reverse proxy).

Trusted proxies: X-Forwarded-For (the client's address) and X-Forwarded-Proto (whether the
browser used HTTPS) are believed only on a connection from one of them -
admin_ui.trusted_proxies, SER2NET_TRUSTED_PROXIES for one run instead, or uvicorn's
FORWARDED_ALLOW_IPS when neither is set. None by default: what a client sends must not
choose the address the sign-in limits, the audit log and all.log see (up to 2.8.1 uvicorn
believed both from any connection from 127.0.0.1 or ::1). An X-Forwarded-For entry that is
not an IP address is never taken: the address before it stands.
"""
from __future__ import annotations

import functools
import ipaddress
import os
from typing import Mapping, Optional

from ..config import proxy_entry_problem, split_proxy_entries
from ..engine import netguard

TRUSTED_ENV = "SER2NET_TRUSTED_PROXIES"
LEGACY_TRUSTED_ENV = "FORWARDED_ALLOW_IPS"   # uvicorn's; used when nothing else is set

_SCHEMES = {"http": ("http", "ws"), "https": ("https", "wss")}


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


# --------------------------------------------------------------------------
# trusted proxies
# --------------------------------------------------------------------------
class Trusted:
    """A set of trusted proxy addresses and networks; '*' (FORWARDED_ALLOW_IPS only) trusts
    every address."""

    def __init__(self, entries) -> None:
        self.everyone = "*" in entries
        self.networks = [ipaddress.ip_network(e, strict=False) for e in entries
                         if e != "*" and proxy_entry_problem(e) is None]

    def __bool__(self) -> bool:
        return self.everyone or bool(self.networks)

    def __contains__(self, host) -> bool:
        if self.everyone:
            return True
        ip = _address(host)
        if ip is None:
            return False
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in self.networks)


@functools.lru_cache(maxsize=16)
def _trusted(entries: tuple[str, ...]) -> Trusted:
    return Trusted(entries)


def trusted_entries(admin, env: Optional[Mapping[str, str]] = None) -> tuple[list[str], str]:
    """(the trusted proxy entries in effect, where they come from): SER2NET_TRUSTED_PROXIES,
    else admin_ui.trusted_proxies, else FORWARDED_ALLOW_IPS, else none ([], '')."""
    env = _env(env)
    raw = (env.get(TRUSTED_ENV) or "").strip()
    if raw:
        return split_proxy_entries(raw), TRUSTED_ENV
    if admin.trusted_proxies:
        return list(admin.trusted_proxies), "admin_ui.trusted_proxies"
    raw = (env.get(LEGACY_TRUSTED_ENV) or "").strip()
    if raw:
        return split_proxy_entries(raw), LEGACY_TRUSTED_ENV
    return [], ""


def trusted(admin) -> Trusted:
    return _trusted(tuple(trusted_entries(admin)[0]))


def _address(text) -> str | None:
    """The IP address in an X-Forwarded-For entry or a peer address ('1.2.3.4',
    '1.2.3.4:5678', '[2001:db8::1]:443', '::ffff:1.2.3.4' -> '1.2.3.4'), or None."""
    t = str(text or "").strip()
    if t.startswith("["):
        end = t.find("]")
        rest = t[end + 1:] if end > 0 else "x"
        if rest and not (rest[0] == ":" and rest[1:].isdigit()):
            return None
        t = t[1:end]
    elif t.count(":") == 1:
        t, _, port = t.partition(":")
        if not port.isdigit():
            return None
    try:
        ip = ipaddress.ip_address(t) if t.isascii() else None
    except ValueError:
        return None
    if ip is None:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.compressed


def forwarded_client(values: list[str], peer: str, proxies: Trusted) -> str:
    """The client's address behind trusted proxies: the last X-Forwarded-For entry that
    isn't one of them (each proxy adds the address it was reached from at the end), the
    first one when all are; walking back stops at an entry that isn't an IP address, and
    the address after it stands (in the end `peer`, the connection's own)."""
    hops = [h for v in values for h in v.split(",")]
    client = peer
    for hop in reversed(hops):
        ip = _address(hop)
        if ip is None:
            break
        client = ip
        if ip not in proxies:
            break
    return client


def _first(value: str) -> str:
    return value.split(",", 1)[0].strip().lower()


def start_notes(admin, env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Lines for the start-up log: what is trusted, and entries that can't be used."""
    env = _env(env)
    notes = []
    entries, source = trusted_entries(admin, env)
    bad = [e for e in entries if e != "*" and proxy_entry_problem(e) is not None]
    if bad:
        notes.append(f"WARNING: {source}: {', '.join(repr(e) for e in bad)} left out (not IP "
                     "addresses or networks)")
    if "*" in entries:
        notes.append(f"WARNING: {source}='*': X-Forwarded-For and X-Forwarded-Proto are "
                     "believed from every client, so anyone who reaches the UI's port can "
                     "choose the address the sign-in limits and the logs see. List the "
                     "proxies' addresses instead (Settings → Reverse proxy)")
    usable = [e for e in entries if e not in bad]
    if usable:
        notes.append(f"trusted proxies ({source}): {', '.join(usable)} — X-Forwarded-For / "
                     "X-Forwarded-Proto are believed from these only")
        if source == LEGACY_TRUSTED_ENV:
            notes.append(f"{LEGACY_TRUSTED_ENV} is read as the list of trusted proxies because "
                         f"neither {TRUSTED_ENV} nor Settings → Reverse proxy sets one")
    return notes



# --------------------------------------------------------------------------
# the ASGI layer in front of the app
# --------------------------------------------------------------------------
class ProxyMiddleware:
    """In front of every request and WebSocket: on a connection from a trusted proxy, the
    client address and the scheme from X-Forwarded-For / X-Forwarded-Proto. Pure ASGI (it
    runs for WebSockets too, which BaseHTTPMiddleware doesn't); the settings are read for
    each request, so a change in Settings applies at once."""

    def __init__(self, app, state) -> None:
        self.app = app
        self.state = state
        # an X-Forwarded-For from an address that isn't trusted: noted once an hour
        self._ignored = netguard.LogEvery(lambda msg: state.log(msg), interval=3600)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            self._forwarded(scope, trusted(self.state.config.admin_ui))
        await self.app(scope, receive, send)

    def _forwarded(self, scope, proxies: Trusted) -> None:
        client = scope.get("client")
        peer = _address(client[0]) if client else None
        if peer is None:
            return
        xff, proto = [], None
        for name, value in scope.get("headers") or ():
            if name == b"x-forwarded-for":
                xff.append(value.decode("latin-1"))
            elif name == b"x-forwarded-proto":
                proto = value.decode("latin-1")
        if not proxies or peer not in proxies:
            if xff:
                self._ignored(f"X-Forwarded-For from {peer} is ignored: {peer} is not a "
                              "trusted proxy, so the sign-in limits, the audit log and "
                              f"all.log see {peer} for every client behind it (a reverse "
                              "proxy: Settings → Reverse proxy → Trusted proxies)")
            return
        if xff:
            scope["client"] = (forwarded_client(xff, peer, proxies), 0)
        if proto is not None and _first(proto) in ("http", "https", "ws", "wss"):
            plain = _first(proto).replace("ws", "http")
            scope["scheme"] = _SCHEMES[plain][scope["type"] == "websocket"]
