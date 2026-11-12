"""TLS contexts made from the files the configuration names: a mapping's listener (the
bridge and the Modbus gateway) and its connect-out link, and the admin UI's certificate.
One place, so they all read their files the same way (other TLS clients can use
tls_context too):

- a relative path names a file in the data directory (config.data_path), so a data
  directory that is moved or restored elsewhere keeps working;
- an encrypted key is decrypted with the password the configuration gives, and never makes
  OpenSSL ask for one on the terminal (without a terminal that failed with a bare
  "[Errno 22] Invalid argument"; with one, the whole process waited for someone to type
  it): without a password, loading it is an error that says so;
- a CA file holds PEM certificates (one or more) or one DER certificate (as Windows exports
  it); any certificate in it is trusted as it is: a CA's, an intermediate's, or the peer's
  own (self-signed) one.

A connect-out link checks the server's certificate when the mapping has a CA file
(``tls_ca``: the certificate must chain to it and name ``remote_host``) or a pin
(``tls_fingerprint``: its SHA-256 fingerprint, checked after the handshake by
check_server). Without either it only encrypts: anyone on the network path can pose as the
server. A listener with ``tls_client_ca`` asks every client for a certificate that chains
to it and ends the handshake of one without such a certificate.
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, Optional

from ..config import data_path

if TYPE_CHECKING:
    import ssl

    from ..config import NetworkSettings


class TlsFileError(ValueError):
    """A TLS file can't be used. ``kind``: unreadable (``path`` can't be read), password (the
    key in ``path`` is encrypted and no password is set), unusable (the certificate does not
    load with its key: a wrong password, another certificate's key, not PEM), no_cert (a
    listener without a certificate and key), ca_unreadable, ca_empty (the CA file holds no
    certificate); ``detail``: what the system said. str(): the text for the log."""

    def __init__(self, kind: str, text: str, path: str = "", detail: str = ""):
        super().__init__(text)
        self.kind, self.path, self.detail = kind, path, detail


def load_cert_chain(ctx: ssl.SSLContext, cert: Any, key: Any = "", password: Any = "") -> None:
    """ctx.load_cert_chain for a certificate and its key (blank ``key``: in the certificate's
    file), decrypting an encrypted key with ``password`` and never asking for one. Raises
    TlsFileError."""
    import ssl

    cert_file, key_file = data_path(cert), data_path(key)
    for path in (cert_file, key_file):
        if not path:
            continue
        try:
            with open(path, "rb"):
                pass
        except OSError as e:
            why = str(e.strerror or e)
            raise TlsFileError("unreadable", f"the TLS file {path!r} can't be read: {why}",
                               path, why) from None
    secret = str(password or "")
    locked = key_file or cert_file

    def give_password() -> bytes:  # called only for an encrypted key
        if not secret:
            raise TlsFileError("password", f"the TLS key {locked!r} is password-protected "
                               "and no password is set for it", locked)
        return secret.encode("utf-8")

    try:
        ctx.load_cert_chain(cert_file, key_file or None, password=give_password)
    except TlsFileError:
        raise
    except ssl.SSLError as e:  # (an OSError, so first)
        why = str(e)
        raise TlsFileError("unusable", f"the TLS certificate {cert_file!r} can't be used with "
                           f"its key{f' {key_file!r}' if key_file else ''}: {why} (a wrong key "
                           "password, or not the certificate's key?)", cert_file, why) from None
    except OSError as e:
        why = str(e.strerror or e)
        raise TlsFileError("unreadable", f"the TLS certificate or key can't be read: {why}",
                           cert_file, why) from None


def _load_ca(ctx: ssl.SSLContext, path: str) -> None:
    """Trust the certificates of the CA file at ``path`` (PEM or DER), and only those."""
    import ssl

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        why = str(e.strerror or e)
        raise TlsFileError("ca_unreadable", f"the CA file {path!r} can't be read: {why}",
                           path, why) from None
    try:  # PEM through OpenSSL's reader (several certificates, comments), else DER
        if b"-----BEGIN" in data:
            ctx.load_verify_locations(cafile=path)
        elif data.strip():  # (empty cadata would be an error of its own)
            ctx.load_verify_locations(cadata=data)
    except (ssl.SSLError, ValueError, OSError):
        pass
    if not ctx.cert_store_stats()["x509"]:
        raise TlsFileError("ca_empty", f"the CA file {path!r} holds no certificate (PEM or "
                           "DER)", path)
    # any certificate in the file is a trust anchor (an intermediate, a self-signed peer)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.verify_mode = ssl.CERT_REQUIRED


def tls_context(*, server_side: bool, cert: Any = "", key: Any = "", password: Any = "",
                ca: Any = "", verify: Optional[bool] = None,
                check_hostname: bool = True) -> ssl.SSLContext:
    """A TLS context made from files. Raises TlsFileError.

    ``server_side``: ser2net presents ``cert`` (with ``key``, decrypted with ``password``),
    which it must have; with ``ca``, a client must present a certificate that chains to a
    certificate in that file, or its handshake fails.
    Otherwise (a client): the server's certificate is checked when ``verify`` is true, or
    is None (the mappings) and a ``ca`` file is given: it must chain to a certificate in
    ``ca`` (without one, to a CA the system trusts) and, with ``check_hostname``, name the
    host given to the handshake (server_hostname). Unchecked, the caller may check a pin
    (check_server). ``cert`` is presented to a server that asks for a client certificate."""
    import ssl

    cert, key, ca = str(cert or ""), str(key or ""), str(ca or "").strip()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
    if not server_side:
        checked = bool(ca) if verify is None else verify
        ctx.check_hostname = checked and check_hostname
        ctx.verify_mode = ssl.CERT_REQUIRED if checked else ssl.CERT_NONE
        if checked and not ca:
            ctx.load_default_certs()
        elif not checked:
            ca = ""
    elif not cert:
        raise TlsFileError("no_cert", "a TLS listener needs a certificate and a key")
    if ca:
        _load_ca(ctx, data_path(ca))
    if cert:
        load_cert_chain(ctx, cert, key, password)
    return ctx


def mapping_context(net: NetworkSettings) -> Optional[ssl.SSLContext]:
    """The TLS context of a mapping (None without TLS): its listener's (server and Modbus
    gateway), or its connect-out link's. Raises TlsFileError."""
    if not net.tls:
        return None
    if net.listens:
        return tls_context(server_side=True, cert=net.tls_cert, key=net.tls_key,
                           password=net.tls_key_password, ca=net.tls_client_ca)
    return tls_context(server_side=False, cert=net.tls_cert, key=net.tls_key,
                       password=net.tls_key_password, ca=net.tls_ca)


# ---------------- certificate fingerprints and subjects ----------------
_PIN_SPLIT = re.compile(r"[\s,;]+")
_PIN = re.compile(r"[0-9a-f]{64}")


def fingerprint(der: bytes) -> str:
    """A certificate's SHA-256 fingerprint as OpenSSL prints it: AB:CD:..."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def parse_pins(text: Any) -> list[str]:
    """The SHA-256 fingerprints of a ``tls_fingerprint`` value, as 64 lower-case hex digits
    each: one or more (several for a certificate change), separated by commas or spaces,
    each with or without colons. Raises ValueError naming an entry that is not one."""
    pins = []
    for entry in _PIN_SPLIT.split(str(text or "").strip()):
        if not entry:
            continue
        digits = entry.replace(":", "").lower()
        if not _PIN.fullmatch(digits):
            raise ValueError(entry)
        pins.append(digits)
    return pins


_NAMES = {"commonName": "CN", "organizationName": "O", "organizationalUnitName": "OU",
          "countryName": "C", "stateOrProvinceName": "ST", "localityName": "L"}


def subject(cert: Any) -> str:
    """The subject of a checked certificate (ssl getpeercert()) as "CN=..., O=...", "" when
    there is none (a certificate that was not checked has no details)."""
    if not isinstance(cert, dict):
        return ""
    return ", ".join(f"{_NAMES.get(name, name)}={value}"
                     for rdn in cert.get("subject") or () for name, value in rdn)


def client_certificate(writer: Any) -> str:
    """For the log line of a client of a TLS listener: " [certificate CN=...]" for the
    certificate it presented (checked against the mapping's tls_client_ca), else ""."""
    name = subject(writer.get_extra_info("peercert"))
    return f" [certificate {name}]" if name else ""


def check_server(net: NetworkSettings, writer: Any) -> str:
    """After a connect-out TLS handshake, before the link is used: the server certificate's
    SHA-256 fingerprint and what was checked, for the log ("TLS, server certificate ...").
    Raises ConnectionError when the mapping's tls_fingerprint does not list it (or is not a
    list of fingerprints)."""
    sslobj = writer.get_extra_info("ssl_object")
    der = sslobj.getpeercert(binary_form=True) if sslobj is not None else None
    seen = fingerprint(der) if der else ""
    try:
        pins = parse_pins(net.tls_fingerprint)
    except ValueError as e:
        raise ConnectionError(f"tls_fingerprint {str(e)!r} is not a SHA-256 fingerprint "
                              "(64 hex digits)") from None
    if pins and seen.replace(":", "").lower() not in pins:
        raise ConnectionError(f"the server's TLS certificate (SHA-256 {seen or 'none'}) is "
                              "not the pinned one (tls_fingerprint)")
    checks = []
    if str(net.tls_ca or "").strip():  # (the handshake checked it)
        name = subject(writer.get_extra_info("peercert"))
        checks.append(f"{name} checked against the CA file" if name else
                      "checked against the CA file")
    if pins:
        checks.append("pinned")
    how = ", ".join(checks) or "not checked (no CA file or pin: encryption only)"
    return f"TLS, server certificate SHA-256 {seen or 'none'}, {how}"
