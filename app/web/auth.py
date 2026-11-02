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
everything, and cluster peer actions always need a real login).
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
from collections import defaultdict, deque

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


def issue_session(secret: str, ttl_seconds: int, username: str, pwd_version: int = 0) -> str:
    payload = _b64e(json.dumps(
        {"exp": int(time.time()) + ttl_seconds, "u": username, "v": int(pwd_version)}
    ).encode("utf-8"))
    return f"{payload}.{_sign(secret, payload)}"


def decode_session(secret: str, token: str | None) -> dict | None:
    """Verify a session cookie's signature + expiry and return its payload
    ({"u": username, "v": pwd_version}), or None if invalid/expired. The caller
    must still confirm the user exists and that pwd_version matches (so a password
    change revokes only that user's sessions)."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) <= int(time.time()):
        return None
    return {"u": str(data.get("u", "")), "v": int(data.get("v", 0))}


def sign_payload(secret: str, data: dict, ttl_seconds: int = 600) -> str:
    """A short-lived HMAC-signed token carrying a small dict (e.g. the OIDC state +
    nonce stashed in a cookie across the redirect)."""
    payload = _b64e(json.dumps({**data, "exp": int(time.time()) + ttl_seconds}).encode("utf-8"))
    return f"{payload}.{_sign(secret, payload)}"


def read_payload(secret: str, token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) <= int(time.time()):
        return None
    return data


def session_user(cfg, token: str | None):
    """Resolve a session cookie to the live User it authenticates, or None.
    Confirms the signed username still exists and its pwd_version is current."""
    data = decode_session(cfg.secret_key, token)
    if data is None:
        return None
    user = cfg.get_user(data["u"])
    if user is None or user.pwd_version != data["v"]:
        return None
    return user


# --------------------------------------------------------------------------
# optional anonymous access (login requirement switched off by an admin)
# --------------------------------------------------------------------------
# Not a valid account name (see routes._username_problem), so it can never collide
# with a real user.
ANONYMOUS_USERNAME = "(anonymous)"


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


def anonymous_user(cfg, headers):
    """The pseudo-user for a visitor without a session, or None when login is
    required (the default), setup isn't done yet, or a claimed host isn't this
    machine. Note: a reverse proxy that rewrites Host to 127.0.0.1 without passing
    the original as X-Forwarded-Host/Forwarded hides the real name from this check."""
    ui = cfg.admin_ui
    if ui.require_login or not cfg.password_set:
        return None
    if not all(is_own_host(h) for h in _claimed_hosts(headers)):
        return None
    from ..config import User

    return User(username=ANONYMOUS_USERNAME, role=ui.anonymous_role, source="anonymous")


def request_user(cfg, token: str | None, headers):
    """The user a request acts as: its session's user, else the anonymous pseudo-user
    when the login requirement is off, else None (must sign in). ``headers`` is the
    request's (or WebSocket's) header mapping."""
    return session_user(cfg, token) or anonymous_user(cfg, headers)


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
    return hmac.compare_digest(str(token), cookie)


# --------------------------------------------------------------------------
# login rate limiter (per IP, sliding window)
# --------------------------------------------------------------------------
class LoginRateLimiter:
    def __init__(self, max_attempts: int = 8, window_s: int = 300):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._fails: dict[str, deque] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        q = self._fails[ip]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return len(q) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        self._fails[ip].append(time.monotonic())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
