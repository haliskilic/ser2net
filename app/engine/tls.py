"""TLS contexts made from the files the configuration names: a mapping's listener (the
bridge and the Modbus gateway) and its connect-out link, and the admin UI's certificate.
One place, so they all read their files the same way: an encrypted key is decrypted with
the password the configuration gives, and never makes OpenSSL ask for one on the terminal
(without a terminal that failed with a bare "[Errno 22] Invalid argument"; with one, the
whole process waited for someone to type it): without a password, loading it is an error
that says so.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import ssl

    from ..config import NetworkSettings


class TlsFileError(ValueError):
    """A TLS file can't be used. ``kind``: unreadable (``path`` can't be read), password (the
    key in ``path`` is encrypted and no password is set), unusable (the certificate does not
    load with its key: a wrong password, another certificate's key, not PEM), no_cert (a
    listener without a certificate and key); ``detail``: what the system said. str(): the
    text for the log."""

    def __init__(self, kind: str, text: str, path: str = "", detail: str = ""):
        super().__init__(text)
        self.kind, self.path, self.detail = kind, path, detail


def load_cert_chain(ctx: ssl.SSLContext, cert: Any, key: Any = "", password: Any = "") -> None:
    """ctx.load_cert_chain for a certificate and its key (blank ``key``: in the certificate's
    file), decrypting an encrypted key with ``password`` and never asking for one. Raises
    TlsFileError."""
    import ssl

    cert_file, key_file = str(cert or ""), str(key or "")
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


def tls_context(*, server_side: bool, cert: Any = "", key: Any = "",
                password: Any = "") -> ssl.SSLContext:
    """A TLS context made from files. Raises TlsFileError.

    ``server_side``: ser2net presents ``cert`` (with ``key``, decrypted with ``password``),
    which it must have. Otherwise (a client): the server's certificate is not checked
    (opportunistic encryption)."""
    import ssl

    cert, key = str(cert or ""), str(key or "")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
    if not server_side:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if not cert:
        raise TlsFileError("no_cert", "a TLS listener needs a certificate and a key")
    load_cert_chain(ctx, cert, key, password)
    return ctx


def mapping_context(net: NetworkSettings) -> Optional[ssl.SSLContext]:
    """The TLS context of a mapping (None without TLS): its listener's (server and Modbus
    gateway), or its connect-out link's. Raises TlsFileError."""
    if not net.tls:
        return None
    if net.listens:
        return tls_context(server_side=True, cert=net.tls_cert, key=net.tls_key,
                           password=net.tls_key_password)
    return tls_context(server_side=False)
