"""The web UI behind a reverse proxy (Settings -> Reverse proxy, docs/REVERSE-PROXY.md).

* Trusted proxies: X-Forwarded-For (the client's address) and X-Forwarded-Proto (whether
  the browser used HTTPS) are believed only on a connection from one of them -
  admin_ui.trusted_proxies, SER2NETWEB_TRUSTED_PROXIES for one run instead, or uvicorn's
  FORWARDED_ALLOW_IPS when neither is set. None by default: what a client sends must not
  choose the address the sign-in limits, the audit log and all.log see (up to 2.8.1 uvicorn
  believed both from any connection from 127.0.0.1 or ::1). An X-Forwarded-For entry that
  is not an IP address is never taken: the address before it stands.
* The public URL (admin_ui.public_url, SER2NETWEB_PUBLIC_URL for one run instead): the address
  users open. A page of its origin is this UI's own (forms, the console: auth.cross_site),
  a blank SSO redirect URI is made from it, notifications link to it.
* Its path, when it has one (https://gw.example.com/ser2netweb): the UI is served under it
  too, behind a proxy that passes the path on, or one that strips it and sends exactly that
  path as X-Forwarded-Prefix. A request without it is served at '/' as before (the LAN,
  cluster peers, the health check). Links, redirects and cookies follow the path a request
  came under (base()).
* Cookie names (cookie_name): browsers keep cookies per host name, not per port or scheme,
  so the UI's cookies are named after what else tells its address apart - the port (but
  the default 8080), the public URL's path, and https (the __Host- / __Secure- prefixes).
  Instances on other ports or under other paths of one host name then keep their own
  sign-ins, and an http:// address can still set its cookies after an https:// one of the
  same name left Secure ones. The plain names of 2.8 and older (legacy_cookie_name) are
  still read, so nobody is signed out by the change.
"""
from __future__ import annotations

import functools
import hashlib
import ipaddress
import os
import re
from typing import Mapping, Optional

from starlette.responses import RedirectResponse

from .. import envvars
from ..config import (
    AdminUI, normalize_public_url, proxy_entry_problem, public_url_problem, split_proxy_entries,
)
from ..engine import netguard

# (SER2NET_TRUSTED_PROXIES / SER2NET_PUBLIC_URL, their names up to 2.9.x, too: envvars)
TRUSTED_ENV = "SER2NETWEB_TRUSTED_PROXIES"
LEGACY_TRUSTED_ENV = "FORWARDED_ALLOW_IPS"   # uvicorn's; used when nothing else is set
PUBLIC_URL_ENV = "SER2NETWEB_PUBLIC_URL"

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
    """(the trusted proxy entries in effect, where they come from): SER2NETWEB_TRUSTED_PROXIES
    (or SER2NET_TRUSTED_PROXIES: the name as it is set), else admin_ui.trusted_proxies, else
    FORWARDED_ALLOW_IPS, else none ([], '')."""
    env = _env(env)
    raw = envvars.text(TRUSTED_ENV, env)
    if raw:
        return split_proxy_entries(raw), envvars.source(TRUSTED_ENV, env)
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


# --------------------------------------------------------------------------
# the public URL and its path
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=8)
def _usable_url(raw: str) -> str:
    return normalize_public_url(raw) if public_url_problem(raw) is None else ""


def public_url_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """SER2NETWEB_PUBLIC_URL (or SER2NET_PUBLIC_URL) when it is set and usable, else ''."""
    return _usable_url(envvars.text(PUBLIC_URL_ENV, env))


def public_url(admin, env: Optional[Mapping[str, str]] = None) -> str:
    """The public URL in effect ('' = none): SER2NETWEB_PUBLIC_URL when it is usable, else
    admin_ui.public_url (both kept as normalize_public_url spells them)."""
    return public_url_from_env(env) or admin.public_url


def origin_of(url: str) -> str:
    """'scheme://host[:port]' of a public URL ('' for none)."""
    scheme, sep, rest = url.partition("://")
    return f"{scheme}{sep}{rest.split('/', 1)[0]}" if sep else ""


def path_of(url: str) -> str:
    """The path of a public URL without the trailing '/' ('' at the root or for none)."""
    _scheme, sep, rest = url.partition("://")
    return "/" + rest.split("/", 1)[1] if sep and "/" in rest else ""


def published_origins(admin) -> tuple[str, ...]:
    """The origins whose pages are this UI's own besides the address a request names:
    the public URL's (auth.cross_site's `also`)."""
    url = public_url(admin)
    return (origin_of(url),) if url else ()


def start_notes(admin, env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Lines for the start-up log: what is trusted and published, and settings from the
    environment that can't be used."""
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
    raw = envvars.text(PUBLIC_URL_ENV, env)
    if raw and (why := public_url_problem(raw)) is not None:
        notes.append(f"WARNING: {envvars.source(PUBLIC_URL_ENV, env)} {raw!r} is not used: {why}")
    return notes


# --------------------------------------------------------------------------
# per request: the path prefix it came under, links, redirects, cookies
# --------------------------------------------------------------------------
def _scope(conn) -> dict:
    return conn if isinstance(conn, dict) else conn.scope


def base(conn) -> str:
    """The path prefix a request (a Request, WebSocket or ASGI scope) came under: the
    public URL's path, or '' at '/'."""
    s = _scope(conn)
    return s.get("app_root_path", s.get("root_path", "")) or ""


def route_path(conn) -> str:
    """A request's path without base(): what the UI's routes are matched against."""
    s = _scope(conn)
    path, prefix = s["path"], base(s)
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        return path[len(prefix):] or "/"
    return path


def url(conn, path: str) -> str:
    """The address of the UI's page `path` ('/login') for a request: under its base()."""
    return base(conn) + path


def redirect(conn, path: str, status_code: int = 303) -> RedirectResponse:
    """A redirect to the UI's page `path`, under the request's base()."""
    return RedirectResponse(url(conn, path), status_code=status_code)


def _header(conn, name: bytes) -> str | None:
    """The first value of a request header (`name` in lower case), or None."""
    for key, value in _scope(conn).get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


_FORWARDED_PROTO = re.compile(r'(?:^|[;\s])proto="?([a-z]+)', re.IGNORECASE)


def browser_https(conn) -> bool:
    """Did the browser use HTTPS, as the request shows it? Its scheme is https (uvicorn's
    TLS, or X-Forwarded-Proto from a trusted proxy), or the first X-Forwarded-Proto or
    Forwarded proto= says so, whoever sent it: that only makes cookies stricter (Secure,
    and their names: cookie_name), which a page can't make the browser's requests say.
    auth.is_https adds admin TLS."""
    if _scope(conn).get("scheme") in ("https", "wss"):
        return True
    if _first(_header(conn, b"x-forwarded-proto") or "") == "https":
        return True
    m = _FORWARDED_PROTO.search((_header(conn, b"forwarded") or "").split(",", 1)[0])
    return bool(m and m.group(1).lower() == "https")


# The port whose cookies keep the plain names (AdminUI's default): one instance per host
# name, the usual case, has the same cookies as before.
_PLAIN_PORT = str(AdminUI.port)


def _host_port(conn) -> str | None:
    """The port in a request's Host header as the browser wrote it ('8081'), '' when it
    names none (the scheme's default), None without a Host header."""
    host = _header(conn, b"host")
    if host is None:
        return None
    host = host.strip()
    if host.startswith("["):
        _addr, _sep, rest = host.partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        port = host.rpartition(":")[2] if host.count(":") == 1 else ""
    return str(int(port)) if port.isascii() and port.isdigit() else ""


def cookie_tag(conn) -> str:
    """What the names of the UI's cookies end with for a request: '' at '/' on port 8080
    (or without a Host header), else '_' and 8 hex digits of a hash of the port in the
    address (but 8080) and the path it came under (base())."""
    port = _host_port(conn)
    key = ("" if port is None or port == _PLAIN_PORT else f":{port}") + base(conn)
    return f"_{hashlib.sha256(key.encode()).hexdigest()[:8]}" if key else ""


def cookie_name(conn, name: str, path: str = "/") -> str:
    """The name of one of the UI's cookies for a request: `name` and its cookie_tag, with
    the __Host- prefix when the browser used HTTPS (browser_https) - __Secure- for a cookie
    whose Path isn't '/' (cookie_path) - so that a Secure cookie of an https:// address
    never has the name an http:// one of the same host name sets."""
    tagged = name + cookie_tag(conn)
    if not browser_https(conn):
        return tagged
    return ("__Host-" if cookie_path(conn, path) == "/" else "__Secure-") + tagged


def legacy_cookie_name(conn, name: str, path: str = "/") -> str | None:
    """The name 2.8 and older gave the cookie (`name` itself), when a request might carry
    one under it: at '/' only (they had no base path), and when it isn't the name the
    cookie has now (cookie_name), else None."""
    if base(conn) or cookie_name(conn, name, path) == name:
        return None
    return name


def cookie_path(conn, path: str = "/") -> str:
    """A cookie's Path for a request: `path` under its base()."""
    prefix = base(conn)
    return (prefix + ("" if path == "/" else path)) if prefix else path


# --------------------------------------------------------------------------
# the ASGI layer in front of the app
# --------------------------------------------------------------------------
class ProxyMiddleware:
    """In front of every request and WebSocket: on a connection from a trusted proxy, the
    client address and the scheme from X-Forwarded-For / X-Forwarded-Proto; under the
    public URL's path, root_path. Pure ASGI (it runs for WebSockets too, which
    BaseHTTPMiddleware doesn't); the settings are read for each request, so a change in
    Settings applies at once."""

    def __init__(self, app, state) -> None:
        self.app = app
        self.state = state
        # an X-Forwarded-For from an address that isn't trusted: noted once an hour
        self._ignored = netguard.LogEvery(lambda msg: state.log(msg), interval=3600)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            admin = self.state.config.admin_ui
            self._forwarded(scope, trusted(admin))
            prefix = path_of(public_url(admin))
            if prefix:
                _under(scope, prefix)
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


def _under(scope, prefix: str) -> None:
    """root_path for a request under the public URL's path `prefix`: its path starts with
    it (the proxy passed it on), or X-Forwarded-Prefix is exactly it (the proxy stripped
    it; it is put back, as ASGI wants root_path to be part of the path)."""
    path = scope["path"]
    if path == prefix or path.startswith(prefix + "/"):
        if path == prefix:   # its pages are below it: /ser2netweb -> /ser2netweb/
            scope["path"], scope["raw_path"] = prefix + "/", (prefix + "/").encode()
        scope["root_path"] = prefix
        return
    for name, value in scope.get("headers") or ():
        if name == b"x-forwarded-prefix":
            if value.decode("latin-1").strip().rstrip("/") == prefix:
                scope["root_path"] = prefix
                scope["path"] = prefix + path
                raw = scope.get("raw_path")
                if raw is not None:
                    scope["raw_path"] = prefix.encode() + raw
            return
