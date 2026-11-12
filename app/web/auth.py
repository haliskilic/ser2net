"""Authentication, sessions, CSRF and login rate-limiting — all stdlib.

The admin UI is network-exposed by design, so it is password-protected by default:
  - scrypt password hashing (no extra dependency),
  - a stateless HMAC-signed session cookie (secret from AppConfig.secret_key),
  - double-submit-cookie CSRF protection on every state-changing request, which the
    browser must not say a page of another site made (cross_site),
  - a simple in-memory per-IP login rate limiter to blunt brute force, and a cap on
    how many password checks (and directory sign-ins) run at once (SlowWork).

An admin may switch the login requirement off for trusted networks: a visitor
without a session then gets a pseudo-user with ``admin_ui.anonymous_role``. That
only applies when every host name the request claims (Host, and X-Forwarded-Host /
Forwarded from a reverse proxy) is an IP literal, ``localhost`` or this machine's
own name — a page on an attacker's domain that DNS-rebinds to this UI carries the
attacker's host name, so it never gets anonymous access (CSRF still applies to
everything, and cluster peer actions always need a real login). The first-run setup
page has the same guard (claims_own_host); through another name it needs the one-time
setup code (AppState.prepare_first_run).
"""
from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time
import unicodedata
from collections import OrderedDict, deque

from ..engine.netguard import address_key  # an IPv6 address by its /64, as the connection caps
from . import proxy

SESSION_COOKIE = "ser2net_session"
CSRF_COOKIE = "ser2net_csrf"

# The scrypt cost of new password hashes: N=2^14, r=8, p=5, OWASP's minimum for scrypt.
# It needs 16 MiB like the p=1 used before 2.9, and about 5 times the CPU time. A hash
# names the cost it was made with ("scrypt:N:r:p$<salt hex>$<hash hex>"), so the cost can
# change without breaking the hashes stored before; one in the format of older versions
# ("scrypt$<salt hex>$<hash hex>") was made with N=2^14, r=8, p=1.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 5
_DKLEN = 32
_COST = (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
_LEGACY_COST = (2 ** 14, 8, 1)
# what a stored hash may ask for (more is refused, not computed): at most 256 MiB
_MAX_N, _MAX_R, _MAX_P, _MAX_MEM, _MAX_DKLEN = 2 ** 20, 32, 64, 256 * 1024 * 1024, 64


# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------
def _cost(scheme: str) -> tuple[int, int, int] | None:
    """(N, r, p) a stored hash's scheme names ("scrypt" or "scrypt:N:r:p"), or None."""
    if scheme == "scrypt":
        return _LEGACY_COST
    name, _sep, rest = scheme.partition(":")
    values = rest.split(":")
    if name != "scrypt" or len(values) != 3 or not all(v.isascii() and v.isdigit()
                                                       for v in values):
        return None
    n, r, p = (int(v) for v in values)
    if not (2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P
            and 128 * r * n <= _MAX_MEM):
        return None
    return n, r, p


def _parse(stored) -> tuple[tuple[int, int, int], bytes, bytes] | None:
    """(cost, salt, hash) of a hash_password() result (of any version), or None."""
    parts = str(stored).split("$")
    if len(parts) != 3:
        return None
    cost = _cost(parts[0])
    try:
        salt, expected = bytes.fromhex(parts[1]), bytes.fromhex(parts[2])
    except ValueError:
        return None
    if cost is None or not salt or not 1 <= len(expected) <= _MAX_DKLEN:
        return None
    return cost, salt, expected


def _prep(password: str) -> str:
    """A password as it is hashed: Unicode NFKC, like the cluster key (engine/cluster.py
    key_bytes), so that the same password typed on another device or pasted from
    elsewhere is the same text - a letter with its accent as one character or two (NFD,
    from macOS file names or some PDFs), a no-break space (macOS Option+Space, text from
    Word or the web) for a space, full-width forms. Letters typed on a keyboard, Turkish
    ones included, are NFKC already. Only for local accounts: a directory gets the
    password as it was typed."""
    return unicodedata.normalize("NFKC", password)


def _scrypt(password: str, salt: bytes, cost: tuple[int, int, int], dklen: int) -> bytes:
    n, r, p = cost
    # the memory these parameters need (OpenSSL refuses more than 32 MiB unless told)
    maxmem = 128 * r * (n + p + 2) + 1024 * 1024
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                          maxmem=maxmem)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = _scrypt(_prep(password), salt, _COST, _DKLEN)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}${salt.hex()}${dk.hex()}"


# What a sign-in under a name that has no local account is checked against while there is
# no directory to ask: a hash of a password nobody knows, so that such a sign-in takes as
# long as a wrong password and the answer's timing doesn't tell which names have one.
DUMMY_HASH = (f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}"
              f"${secrets.token_hex(16)}${secrets.token_hex(_DKLEN)}")


def is_password_hash(stored: str) -> bool:
    """Is `stored` a hash_password() result, of this version or an older one?"""
    return _parse(stored) is not None


def needs_rehash(stored: str) -> bool:
    """Was `stored` made with another cost than new hashes get (an older version's)?"""
    parsed = _parse(stored)
    return parsed is None or parsed[0] != _COST or len(parsed[2]) != _DKLEN


def verify_password(password: str, stored: str) -> bool:
    """Does `password` match `stored`? Checked as NFKC (_prep), the form every hash of
    this version is made of; a hash in the format of older versions, which hashed the
    password as typed, is also checked against that when it differs (one scrypt more,
    only for a password that NFKC changes)."""
    parsed = _parse(stored)
    if parsed is None:
        return False
    cost, salt, expected = parsed
    try:
        tries = [_prep(password)]
        if str(stored).startswith("scrypt$") and password != tries[0]:
            tries.append(password)
        return any(hmac.compare_digest(_scrypt(pw, salt, cost, len(expected)), expected)
                   for pw in tries)
    except (ValueError, TypeError, MemoryError):  # not (encodable) text; out of memory
        return False


# --------------------------------------------------------------------------
# signed session token
# --------------------------------------------------------------------------
def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(secret: str, payload: str) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())


def _payload_secret(secret: str) -> str:
    """What sign_payload's tokens are signed with: derived from the session key, so that
    such a token (the OIDC state cookie) is never a valid session cookie, nor a session
    cookie a valid state."""
    return hmac.new(secret.encode("utf-8"), b"ser2net signed payload", hashlib.sha256).hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def issue_session(secret: str, ttl_seconds: int, username: str, pwd_version: int = 0) -> str:
    # "n": two sign-ins in the same second still get different tokens, so a logout
    # (RevokedSessions) ends exactly one of them
    payload = _b64e(json.dumps(
        {"exp": int(time.time()) + ttl_seconds, "u": username, "v": int(pwd_version),
         "n": secrets.token_urlsafe(6)}
    ).encode("utf-8"))
    return f"{payload}.{_sign(secret, payload)}"


def decode_session(secret: str, token: str | None) -> dict | None:
    """Verify a session cookie's signature + expiry and return its payload
    ({"u": username, "v": pwd_version, "exp": expiry}), or None if invalid/expired.
    The caller must still confirm the user exists and that pwd_version matches (so a
    password change revokes only that user's sessions)."""
    if not token or "." not in token or not token.isascii():  # ours are ASCII
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    user, version, exp = data.get("u", ""), data.get("v", 0), data.get("exp", 0)
    if not (isinstance(user, str) and _is_int(version) and _is_int(exp)):
        return None  # signed, but not a session token
    if exp <= int(time.time()):
        return None
    return {"u": user, "v": version, "exp": exp}


class RevokedSessions:
    """Session cookies ended by a logout, remembered until they would expire anyway.

    A cookie is a stateless signed token: the browser deleting it at logout would leave
    a copy of it valid for the rest of its lifetime. Held in memory (a restart forgets
    the list) and never written to config.json, where a save on every logout would
    push real changes out of the 50 backups. Keyed by the token's signature.

    Bounded: at most PER_USER cookies of one account and MAX_ENTRIES in all. Past that
    nothing remembered is forgotten (that would make someone's copied cookie valid
    again): add() names the account instead, and the caller ends all of its sessions
    (pwd_version + 1), after which its remembered cookies are moot (forget())."""

    MAX_ENTRIES = 10000
    PER_USER = 100

    def __init__(self) -> None:
        self._until: dict[str, int] = {}  # signature -> the token's expiry (epoch seconds)
        self._owner: dict[str, str] = {}  # signature -> the token's account name
        self._count: dict[str, int] = {}  # account name -> its signatures in the list

    def add(self, secret: str, token: str | None) -> str | None:
        """Refuse this token from now on (if it is a valid session at all). Returns its
        account's name when it can't be remembered (that account's share or the list is
        full): the caller ends all of that account's sessions instead. Else None."""
        data = decode_session(secret, token)
        if data is None or token is None:
            return None
        now = int(time.time())
        for sig in [s for s, exp in self._until.items() if exp <= now]:
            self._drop(sig)
        sig, user = token.rsplit(".", 1)[1], data["u"]
        if sig in self._until:
            return None
        if self._count.get(user, 0) >= self.PER_USER or len(self._until) >= self.MAX_ENTRIES:
            return user
        self._until[sig], self._owner[sig] = data["exp"], user
        self._count[user] = self._count.get(user, 0) + 1
        return None

    def forget(self, username: str) -> None:
        """Drop an account's cookies: all of its sessions were ended another way."""
        for sig in [s for s, u in self._owner.items() if u == username]:
            self._drop(sig)

    def _drop(self, sig: str) -> None:
        del self._until[sig]
        user = self._owner.pop(sig)
        if self._count[user] > 1:
            self._count[user] -= 1
        else:
            del self._count[user]

    def __contains__(self, token: object) -> bool:
        if not isinstance(token, str) or "." not in token:
            return False
        exp = self._until.get(token.rsplit(".", 1)[1])
        return exp is not None and exp > time.time()

    def clear(self) -> None:
        self._until.clear()
        self._owner.clear()
        self._count.clear()


def sign_payload(secret: str, data: dict, ttl_seconds: int = 600) -> str:
    """A short-lived HMAC-signed token carrying a small dict (e.g. the OIDC state +
    nonce stashed in a cookie across the redirect), under a key of its own
    (_payload_secret)."""
    payload = _b64e(json.dumps({**data, "exp": int(time.time()) + ttl_seconds}).encode("utf-8"))
    return f"{payload}.{_sign(_payload_secret(secret), payload)}"


def read_payload(secret: str, token: str | None) -> dict | None:
    if not token or "." not in token or not token.isascii():  # ours are ASCII
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(_payload_secret(secret), payload)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not _is_int(data.get("exp")) or data["exp"] <= int(time.time()):
        return None
    return data


def session_user(cfg, token: str | None, revoked: RevokedSessions | None = None):
    """Resolve a session cookie to the live User it authenticates, or None.
    Confirms the signed username still exists, its pwd_version is current and its
    sign-in method (LDAP/OIDC) is still on, and that the session was not ended by a
    logout (``revoked``)."""
    data = decode_session(cfg.secret_key, token)
    if data is None or (revoked is not None and token in revoked):
        return None
    user = cfg.get_user(data["u"])
    if user is None or user.pwd_version != data["v"]:
        return None
    if not cfg.source_enabled(user):  # LDAP/OIDC switched off: its accounts are signed out
        return None
    return user


# --------------------------------------------------------------------------
# optional anonymous access (login requirement switched off by an admin)
# --------------------------------------------------------------------------
# Not a valid account name: local names can't hold parentheses (forms._username_problem)
# and LDAP/OIDC names in parentheses are refused (config.external_name_problem). Identity
# checks compare the source too (an older install may have such an account).
ANONYMOUS_USERNAME = "(anonymous)"
# What logs and the audit log name for a failed sign-in whose user name is no account's:
# people type their password into that field. Not a valid account name either.
UNKNOWN_USER = "(unknown user)"
# The longest user name a sign-in is tried with (local names have at most 32 characters);
# a longer one fails at once, without asking the directory.
LOGIN_NAME_MAX = 256


@functools.lru_cache(maxsize=1)
def _own_host_names() -> frozenset:
    # gethostname() only (no DNS lookup on the event loop): the name as configured,
    # its short form and the mDNS "<short>.local" variant.
    full = socket.gethostname().strip().lower()
    short = full.split(".", 1)[0]
    return frozenset(n for n in (full, short, f"{short}.local" if short else "") if n)


def _host_only(host_header: str | None) -> str:
    """'Host' header value without the port (and IPv6 brackets), lower-cased."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def is_own_host(host_header: str | None) -> bool:
    """True when the browser addressed this UI by IP, localhost or this machine's own
    name — i.e. not through some other DNS name (the DNS-rebinding guard)."""
    name = _host_only(host_header)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or name in _own_host_names()


_FORWARDED_HOST = re.compile(r'(?:^|[;,\s])host="?([^";,\s]+)', re.IGNORECASE)


def _claimed_hosts(headers) -> list:
    """Every host name the request claims: Host, plus X-Forwarded-Host / Forwarded
    host= when a reverse proxy sets them. All must be this machine for open access
    (requiring more names to match can only narrow it, so a client-supplied header
    can't widen anything)."""
    hosts = [headers.get("host") or ""]
    xfh = headers.get("x-forwarded-host")
    if xfh:
        hosts += [h.strip() for h in xfh.split(",")]
    fwd = headers.get("forwarded")
    if fwd:
        hosts += _FORWARDED_HOST.findall(fwd)
    return hosts


def claims_own_host(headers) -> bool:
    """Every host name the request claims is this machine (is_own_host): the
    DNS-rebinding guard of open access and first-run setup."""
    return all(is_own_host(h) for h in _claimed_hosts(headers))


def foreign_host(headers) -> str:
    """The first claimed host name that is not this machine ('' if there is none)."""
    return next((h for h in _claimed_hosts(headers) if not is_own_host(h)), "")


# --------------------------------------------------------------------------
# requests that a page of another site makes the browser send
# --------------------------------------------------------------------------
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _host_port(netloc: str, scheme: str) -> tuple[str, str] | None:
    """('host', 'port') of a Host-style 'host[:port]' (IPv6 in brackets), lower-cased, the
    port of `scheme` when none is given; None when it is not such a value."""
    n = netloc.strip().lower()
    if n.startswith("["):
        end = n.find("]")
        host, rest = (n[:end + 1], n[end + 1:]) if end > 0 else ("", "")
    elif n.count(":") == 1:
        host, rest = n.split(":")[0], n[n.find(":"):]
    else:
        host, rest = ("", "") if ":" in n else (n, "")
    if not host or (rest and not (rest[0] == ":" and rest[1:].isascii() and rest[1:].isdigit())):
        return None
    return host, str(int(rest[1:])) if rest else _DEFAULT_PORTS.get(scheme, "")


def _origin(value: str) -> tuple[str, str, str] | None:
    """(scheme, host, port) of an http(s) origin 'scheme://host[:port]' (no path), the
    scheme's port when none is given; None when it is not such a value."""
    scheme, sep, netloc = value.strip().partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _DEFAULT_PORTS:
        return None
    hp = _host_port(netloc, scheme)
    return (scheme, *hp) if hp else None


# What a refusal of a page's own request behind a reverse proxy usually needs (all.log).
PROXY_HINT = ("behind a reverse proxy, pass the browser's address on as Host (nginx: "
              "proxy_set_header Host $http_host; Apache: ProxyPreserveHost On) or as "
              "X-Forwarded-Host")


def cross_site(headers, also=()) -> str:
    """Why the browser says a request was made by a page of another site, or '' when it
    doesn't: the forms and changes of the web UI, and its WebSockets (the console, the
    hex view). Sec-Fetch-Site (sent to HTTPS and localhost) must be same-origin or none;
    without it, an Origin must be one of the hosts the request claims (Host, and
    X-Forwarded-Host / Forwarded host= from a reverse proxy, which a page can't set),
    port included, or one of the origins in `also` ('scheme://host[:port]', an address
    the UI is published under): another port of the same host or a sibling subdomain is
    another site, and its pages can put a CSRF cookie of their own into the browser. A
    request with neither header (a script, a very old browser) is left to the CSRF token
    and the session. The model of Go's net/http CrossOriginProtection."""
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        return "" if site in ("same-origin", "none") else f"Sec-Fetch-Site {site!r}"
    origin = headers.get("origin")
    if origin is None:
        return ""
    ours = _origin(origin)
    claimed = _claimed_hosts(headers)
    if ours is not None and (any(_host_port(h, ours[0]) == ours[1:] for h in claimed)
                             or any(_origin(o) == ours for o in also)):
        return ""
    return (f"Origin {origin[:200]!r}; this UI was addressed as "
            + ", ".join(repr(h[:100]) for h in claimed))


def anonymous_user(cfg, headers):
    """The pseudo-user for a visitor without a session, or None when login is
    required (the default), setup isn't done yet, or a claimed host isn't this
    machine. Note: a reverse proxy that rewrites Host to 127.0.0.1 without passing
    the original as X-Forwarded-Host/Forwarded hides the real name from this check."""
    ui = cfg.admin_ui
    if ui.require_login or not cfg.password_set:
        return None
    if not claims_own_host(headers):
        return None
    from ..config import User

    return User(username=ANONYMOUS_USERNAME, role=ui.anonymous_role, source="anonymous")


def request_user(cfg, token: str | None, headers, revoked: RevokedSessions | None = None):
    """The user a request acts as: its session's user, else the anonymous pseudo-user
    when the login requirement is off, else None (must sign in). ``headers`` is the
    request's (or WebSocket's) header mapping; ``revoked`` the logged-out sessions."""
    return session_user(cfg, token, revoked) or anonymous_user(cfg, headers)


# --------------------------------------------------------------------------
# API bearer token (for the JSON REST API / automation)
# --------------------------------------------------------------------------
API_TOKEN_PREFIX = "s2n_"  # noqa: S105 - the public prefix of every token


def new_api_token() -> str:
    """A high-entropy bearer token shown to the operator once on generation."""
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex of a token. The token is already 256 bits of entropy, so a fast
    hash (not scrypt) is appropriate — we only store the hash, never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def find_api_token(cfg, token: str):
    """The API token (config.ApiToken) a request's bearer token is: the one of
    cfg.api_token_hash — as an ApiToken without an id, named 'api-token' in the audit log
    like before there were others — or one of cfg.api_tokens. None when it is none of them.
    Whether it has expired is the caller's to check. Every stored hash is compared, in
    constant time, so how long this takes doesn't tell which one matched."""
    from ..config import ApiToken

    if not token:
        return None
    digest = hash_token(token).encode("ascii")
    found = None
    stored = [ApiToken(sha256=cfg.api_token_hash, role=cfg.api_token_role)] if cfg.api_token_hash else []
    for candidate in stored + list(cfg.api_tokens):
        # bytes: a hand-edited non-ASCII hash is a mismatch, not an error
        if hmac.compare_digest(digest, str(candidate.sha256).encode("utf-8")) and found is None:
            found = candidate
    return found


def bearer_token(request) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


# --------------------------------------------------------------------------
# cookies
# --------------------------------------------------------------------------
def is_https(request, cfg) -> bool:
    """Did the browser use HTTPS for this request? With admin TLS as the UI's listener
    serves it (AppState.serving_tls: a TLS change applies to new connections at once;
    without the app's state, as config.json sets it); when the request's scheme is https
    (uvicorn's TLS, or X-Forwarded-Proto from a trusted proxy: app/web/proxy.py); or when
    the first X-Forwarded-Proto or Forwarded proto= says so, whoever sent it: it only makes
    cookies stricter (Secure) and adds HSTS, which browsers take only over HTTPS, and a
    page can't add those headers to the browser's requests - so a TLS proxy needs no
    trusted-proxy setting for the cookies to be Secure."""
    app = (getattr(request, "scope", None) or {}).get("app")
    state = getattr(getattr(app, "state", None), "appstate", None)
    served = state.serving_tls if state is not None else cfg.admin_ui.tls_enabled
    return served or proxy.browser_https(request)


def set_cookie(response, request, cfg, name: str, value: str, max_age: int,
               path: str = "/") -> None:
    """One of the UI's cookies: HttpOnly, SameSite=Lax, Secure whenever the browser used
    HTTPS (is_https), under the request's path prefix and with its name for the request
    (proxy.cookie_name / cookie_path: by port, path and scheme)."""
    response.set_cookie(proxy.cookie_name(request, name, path), value, max_age=max_age,
                        path=proxy.cookie_path(request, path), httponly=True, samesite="lax",
                        secure=is_https(request, cfg))


def delete_cookie(response, request, cfg, name: str, path: str = "/") -> None:
    response.delete_cookie(proxy.cookie_name(request, name, path),
                           path=proxy.cookie_path(request, path),
                           httponly=True, samesite="lax", secure=is_https(request, cfg))


def cookie(request, name: str, path: str = "/", legacy: bool = True) -> str | None:
    """The value of one of the UI's cookies (its name for this request: set_cookie); when
    the request has none under that name, the one under the name 2.8 and older gave it
    (proxy.legacy_cookie_name), unless `legacy` is False. Such a session cookie is moved to
    its new name by GuardMiddleware (legacy_session / move_session)."""
    value = request.cookies.get(proxy.cookie_name(request, name, path))
    if value is None and legacy and (old := proxy.legacy_cookie_name(request, name, path)):
        value = request.cookies.get(old)
    return value


def legacy_session(request, cfg, revoked: RevokedSessions | None = None) -> str | None:
    """A session cookie of this UI under the name of 2.8 and older (a sign-in from before
    the upgrade), when the request has none under its name now: the token, only when it
    is a valid session here. Another instance on this host name may still use that name
    (one of 2.8, or one on port 8080, whose name it is): its cookie doesn't verify with
    this UI's key and is left alone."""
    old = proxy.legacy_cookie_name(request, SESSION_COOKIE)
    if old is None or proxy.cookie_name(request, SESSION_COOKIE) in request.cookies:
        return None
    token = request.cookies.get(old)
    return token if session_user(cfg, token, revoked) is not None else None


def move_session(response, request, cfg, token: str) -> None:
    """Move a legacy_session to the session cookie's name now (the same token, until it
    expires anyway) - unless the response sets or deletes that cookie itself (a sign-in, a
    sign-out) - and delete it under the old name."""
    name = proxy.cookie_name(request, SESSION_COOKIE)
    if not any(c.startswith(name + "=") for c in response.headers.getlist("set-cookie")):
        data = decode_session(cfg.secret_key, token)
        if data is not None:
            set_cookie(response, request, cfg, SESSION_COOKIE, token,
                       max_age=max(1, data["exp"] - int(time.time())))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=is_https(request, cfg))


# --------------------------------------------------------------------------
# CSRF (double-submit cookie)
# --------------------------------------------------------------------------
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_matches(request, token) -> bool:
    """Compare a supplied token (header or form field) against the CSRF cookie.

    Deliberately does NOT read the request body — reading it inside a
    BaseHTTPMiddleware would drain the stream before the route handler can parse
    the form. API/HTMX requests pass the token via the X-CSRF-Token header
    (checked in middleware); plain HTML form posts pass _csrf in the body and are
    validated by their handlers, which read the form exactly once.
    """
    stored = cookie(request, CSRF_COOKIE)  # a 2.8 one too, until GuardMiddleware moves it
    if not stored or not token:
        return False
    token = str(token)
    # tokens are ASCII; compare_digest raises on other text, which is just a mismatch
    return token.isascii() and stored.isascii() and hmac.compare_digest(token, stored)


# --------------------------------------------------------------------------
# login rate limiter (per IP or per user name, sliding window)
# --------------------------------------------------------------------------
class LoginRateLimiter:
    """Blocks an address (an IPv6 one by its /64: address_key), or what `key` makes of the
    value given (a user name), after `max_attempts` failed sign-ins in `window_s` seconds.
    Keeps the times of at most that many failures for at most MAX_ADDRESSES keys (when
    full, the one whose last failure is the oldest is forgotten), so addresses that only
    ever fail don't add up in memory."""

    MAX_ADDRESSES = 4096

    def __init__(self, max_attempts: int = 8, window_s: int = 300, key=None):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._key = key or address_key
        self._fails: OrderedDict[str, deque] = OrderedDict()

    def blocked(self, ip: str) -> bool:
        k = self._key(ip)
        q = self._fails.get(k)
        if q is None:
            return False
        now = time.monotonic()
        while q and now - q[0] > self.window_s:
            q.popleft()
        if not q:
            del self._fails[k]
        return len(q) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        k = self._key(ip)
        q = self._fails.get(k)
        if q is None:
            q = self._fails[k] = deque(maxlen=self.max_attempts)
            while len(self._fails) > self.MAX_ADDRESSES:
                self._fails.popitem(last=False)
        else:
            self._fails.move_to_end(k)
        q.append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._fails.pop(self._key(ip), None)


# --------------------------------------------------------------------------
# password checks and directory sign-ins: a few at a time
# --------------------------------------------------------------------------
class SlowWork:
    """Runs slow sign-in work (scrypt: CPU time and 16 MiB each; a directory bind:
    seconds when the server is slow) in worker threads, at most `limit` at a time; the
    rest wait their turn. A burst of sign-ins thus can't take every thread of the shared
    pool, where serial ports are opened and closed and config.json is saved too, nor
    the memory of one scrypt job per request. busy(): `queue` jobs wait already — a
    sign-in is answered 429 then instead of queueing as well."""

    def __init__(self, limit: int, queue: int) -> None:
        self.limit, self.queue = limit, queue
        self._slots = asyncio.Semaphore(limit)
        self._pending = 0  # running or waiting

    def busy(self) -> bool:
        return self._pending >= self.limit + self.queue

    async def run(self, fn, /, *args, **kwargs):
        self._pending += 1
        try:
            async with self._slots:
                return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            self._pending -= 1
