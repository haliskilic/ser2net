"""Authentication, sessions, CSRF and login rate-limiting — all stdlib.

The admin UI is network-exposed by design, so it is password-protected by default:
  - scrypt password hashing (no extra dependency),
  - a stateless HMAC-signed session cookie (secret from AppConfig.secret_key),
  - double-submit-cookie CSRF protection on every state-changing request,
  - a simple in-memory per-IP login rate limiter to blunt brute force.

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
from collections import OrderedDict, deque

SESSION_COOKIE = "ser2net_session"
CSRF_COOKIE = "ser2net_csrf"

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${salt.hex()}${dk.hex()}"


# What a sign-in under a name that has no local account is checked against while there is
# no directory to ask: a hash of a password nobody knows, so that such a sign-in takes as
# long as a wrong password and the answer's timing doesn't tell which names have one.
DUMMY_HASH = f"scrypt${secrets.token_hex(16)}${secrets.token_hex(_DKLEN)}"


def is_password_hash(stored: str) -> bool:
    """Is `stored` a hash_password() result ("scrypt$<salt hex>$<hash hex>")?"""
    parts = str(stored).split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        return bool(bytes.fromhex(parts[1])) and bool(bytes.fromhex(parts[2]))
    except ValueError:
        return False


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, AttributeError):
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
API_TOKEN_PREFIX = "s2n_"


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


def bearer_token(request) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


# --------------------------------------------------------------------------
# CSRF (double-submit cookie)
# --------------------------------------------------------------------------
def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_from_request(request) -> str:
    return request.cookies.get(CSRF_COOKIE, "")


def csrf_token_matches(request, token) -> bool:
    """Compare a supplied token (header or form field) against the CSRF cookie.

    Deliberately does NOT read the request body — reading it inside a
    BaseHTTPMiddleware would drain the stream before the route handler can parse
    the form. API/HTMX requests pass the token via the X-CSRF-Token header
    (checked in middleware); plain HTML form posts pass _csrf in the body and are
    validated by their handlers, which read the form exactly once.
    """
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not token:
        return False
    token = str(token)
    # tokens are ASCII; compare_digest raises on other text, which is just a mismatch
    return token.isascii() and cookie.isascii() and hmac.compare_digest(token, cookie)


# --------------------------------------------------------------------------
# login rate limiter (per IP, sliding window)
# --------------------------------------------------------------------------
class LoginRateLimiter:
    """Blocks an address after `max_attempts` failed sign-ins in `window_s` seconds.
    Keeps the times of at most that many failures for at most MAX_ADDRESSES addresses
    (when full, the one whose last failure is the oldest is forgotten), so addresses
    that only ever fail don't add up in memory."""

    MAX_ADDRESSES = 4096

    def __init__(self, max_attempts: int = 8, window_s: int = 300):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._fails: OrderedDict[str, deque] = OrderedDict()

    def blocked(self, ip: str) -> bool:
        q = self._fails.get(ip)
        if q is None:
            return False
        now = time.monotonic()
        while q and now - q[0] > self.window_s:
            q.popleft()
        if not q:
            del self._fails[ip]
        return len(q) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        q = self._fails.get(ip)
        if q is None:
            q = self._fails[ip] = deque(maxlen=self.max_attempts)
            while len(self._fails) > self.MAX_ADDRESSES:
                self._fails.popitem(last=False)
        else:
            self._fails.move_to_end(ip)
        q.append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
