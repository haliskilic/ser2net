"""Time-based one-time passwords (RFC 6238, TOTP) for two-factor sign-in — stdlib only.

The codes any authenticator app makes (Google Authenticator, Microsoft Authenticator,
Aegis, FreeOTP, KeePassXC, ...): HMAC-SHA1 of the number of 30-second steps since the
epoch, under a shared secret, as 6 digits (RFC 4226 dynamic truncation). The secret is
kept base32 without padding (how apps take it) in the account's User.totp_secret.

verify() accepts the code of the current step and of one step either side, for a clock
that is a little off (an OT box without NTP), and only a step later than the last one
accepted for that account, so a code seen once can't be used again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
STEP_S = 30
WINDOW = 1          # steps accepted either side of the current one
SECRET_BYTES = 20   # 160 bits, as RFC 4226 recommends for HMAC-SHA1
_BASE32 = re.compile(r"[A-Z2-7]{16,128}")
_CODE = re.compile(r"[0-9]{%d}" % DIGITS)


def new_secret() -> str:
    """A new random secret, base32 without padding."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def is_secret(value: object) -> bool:
    """Is `value` a secret verify() can use (base32, at least 80 bits)?"""
    return isinstance(value, str) and _BASE32.fullmatch(value) is not None


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226: the one-time password for `counter` under `key`."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def step_at(when: float) -> int:
    return int(when // STEP_S)


def code_at(secret: str, when: float) -> str:
    """The code an authenticator shows at `when` (epoch seconds)."""
    return hotp(_key(secret), step_at(when))


def normalize_code(code: object) -> str:
    """What was typed, without the spaces or dashes some apps show in the middle."""
    return re.sub(r"[\s-]", "", str(code or ""))


def verify(secret: str, code: object, last_step: int = -1,
           now: float | None = None) -> int | None:
    """The time step `code` belongs to — the current one or WINDOW steps either side, and
    later than `last_step` (the replay guard) — or None when it is not a valid code now.
    An unusable secret matches nothing."""
    code = normalize_code(code)
    if not is_secret(secret) or not _CODE.fullmatch(code):
        return None
    key = _key(secret)
    current = step_at(time.time() if now is None else now)
    found = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        # every step is computed and compared, in constant time, whichever matches
        if hmac.compare_digest(hotp(key, step), code) and step > last_step and found is None:
            found = step
    return found


def grouped(secret: str) -> str:
    """The secret in groups of four, easier to type into an app."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def otpauth_uri(secret: str, account: str, issuer: str = "ser2netweb") -> str:
    """The otpauth:// link authenticator apps take (pasted, or as a QR code made of it)."""
    label = quote(f"{issuer}:{account}", safe="")
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                       "digits": DIGITS, "period": STEP_S})
    return f"otpauth://totp/{label}?{query}"
