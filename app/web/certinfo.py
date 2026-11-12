"""What a TLS certificate file says about itself - its subject, names and validity - for
Settings → Admin TLS and the start-up log, for a self-signed certificate and a CA-issued
one alike. Only reads the file.

Read with the optional `cryptography` package when it is there (the standalone executables
and the Docker image have it), otherwise with the decoder in Python's ssl module.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import ipaddress
import ssl
import time

SOON_DAYS = 30            # a certificate that ends sooner than this is flagged
_MAX_BYTES = 1024 * 1024  # a certificate file larger than this is not read


@dataclasses.dataclass
class CertInfo:
    subject: str              # its common name (CN), else the whole subject
    names: list[str]          # its subject alternative names (DNS names, IP addresses)
    not_before: float         # valid from (epoch seconds)
    not_after: float          # valid until
    self_signed: bool         # issued by itself

    def state(self, now: float | None = None) -> str:
        """"not_yet_valid", "expired", "expiring" (within SOON_DAYS) or "valid"."""
        now = time.time() if now is None else now
        if now < self.not_before:
            return "not_yet_valid"
        if now > self.not_after:
            return "expired"
        return "expiring" if self.not_after - now < SOON_DAYS * 86400 else "valid"

    def days_left(self, now: float | None = None) -> int:
        """Whole days until it ends (0 on its last day; negative once it has ended)."""
        return int((self.not_after - (time.time() if now is None else now)) // 86400)

    def describe(self, now: float | None = None) -> str:
        """One line for the log: subject, validity in UTC and what that means now."""
        def day(t: float) -> str:
            return time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))
        state = self.state(now)
        what = {"not_yet_valid": "NOT YET VALID (this host's clock is behind its start?)",
                "expired": "EXPIRED", "expiring": f"ends in {self.days_left(now)} day(s)",
                "valid": f"{self.days_left(now)} day(s) left"}[state]
        kind = ", self-signed" if self.self_signed else ""
        return (f"{self.subject}{kind}, valid {day(self.not_before)} to "
                f"{day(self.not_after)} UTC: {what}")


class CertReadError(Exception):
    """The file can't be read as a certificate. `code`: "unreadable" (str(self) says
    why), "not_pem" (no PEM certificate in it) or "no_decoder" (neither cryptography nor
    the ssl module's decoder is available)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def read(path: str) -> CertInfo:
    """The first certificate in the PEM file at *path*. Raises CertReadError. Blocking
    file I/O: call via asyncio.to_thread from the event loop."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_BYTES + 1)
    except OSError as e:
        raise CertReadError("unreadable", e.strerror or str(e)) from None
    if len(data) > _MAX_BYTES or b"-----BEGIN CERTIFICATE-----" not in data:
        raise CertReadError("not_pem")
    try:
        return _with_cryptography(data)
    except ImportError:
        pass
    return _with_ssl(path)


def _utc(value: datetime.datetime) -> float:
    if value.tzinfo is None:          # cryptography before 42: naive UTC
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.timestamp()


def _with_cryptography(data: bytes) -> CertInfo:
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    try:
        cert = x509.load_pem_x509_certificate(data)
    except ValueError:
        raise CertReadError("not_pem") from None
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    names: list[str] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [*san.get_values_for_type(x509.DNSName),
                 *(str(ip) for ip in san.get_values_for_type(x509.IPAddress))]
    except x509.ExtensionNotFound:
        pass
    return CertInfo(
        subject=str(cn[0].value) if cn else cert.subject.rfc4514_string(), names=names,
        not_before=_utc(getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before),
        not_after=_utc(getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after),
        self_signed=cert.issuer == cert.subject)


def _with_ssl(path: str) -> CertInfo:
    """ssl's own certificate decoder (the one its tests use; CPython has it on every
    platform): a source install without cryptography still sees its certificate."""
    decode = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decode is None:
        raise CertReadError("no_decoder")
    try:
        d = decode(path)
        subject = [kv for rdn in d.get("subject", ()) for kv in rdn]
        issuer = [kv for rdn in d.get("issuer", ()) for kv in rdn]
        cn = [v for k, v in subject if k == "commonName"]
        names = [_ip(v) if k == "IP Address" else str(v) for k, v in d.get("subjectAltName", ())
                 if k in ("DNS", "IP Address")]
        return CertInfo(subject=cn[0] if cn else ", ".join(f"{k}={v}" for k, v in subject),
                        names=names, not_before=float(ssl.cert_time_to_seconds(d["notBefore"])),
                        not_after=float(ssl.cert_time_to_seconds(d["notAfter"])),
                        self_signed=subject == issuer)
    except (ssl.SSLError, ValueError, KeyError, TypeError):
        raise CertReadError("not_pem") from None


def _ip(value) -> str:
    """An IP address as cryptography writes it ('::1', not '0:0:0:0:0:0:0:1')."""
    text = str(value).strip()
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(text))
    return text
