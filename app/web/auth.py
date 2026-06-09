"""Authentication, sessions, CSRF and login rate-limiting — all stdlib.

The admin UI is network-exposed by design, so it is always password-protected:
  - scrypt password hashing (no extra dependency),
  - a stateless HMAC-signed session cookie (secret from AppConfig.secret_key),
  - double-submit-cookie CSRF protection on every state-changing request,
  - a simple in-memory per-IP login rate limiter to blunt brute force.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
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
