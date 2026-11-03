"""Self-signed certificate for the admin UI (Settings → Generate self-signed certificate).

Made in-process with the `cryptography` package — bundled in the standalone
executables — so no external `openssl` program is needed (Windows has none, and
minimal Linux installs often lack it). A source install without cryptography
falls back to the openssl command if one is on PATH.
"""
from __future__ import annotations

import contextlib
import datetime
import ipaddress
import os
import socket
import subprocess

DAYS = 825  # the longest lifetime browsers accept for a TLS server certificate


class CertError(RuntimeError):
    """Neither cryptography nor a working openssl command is available."""


def generate(cert_path: str, key_path: str, common_name: str = "ser2net", days: int = DAYS) -> str:
    """Write a self-signed RSA-2048 certificate and its unencrypted private key (PEM).

    Returns how it was made: "cryptography" or "openssl". Raises CertError when
    neither is available or openssl fails; OSError if the files can't be written."""
    try:
        return _with_cryptography(cert_path, key_path, common_name, days)
    except ImportError:
        pass
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key_path, "-out", cert_path, "-days", str(days),
                        "-subj", f"/CN={common_name}"],
                       check=True, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise CertError("this needs the 'cryptography' Python package (pip install "
                        "cryptography) or the openssl program") from None
    except (subprocess.SubprocessError, OSError) as e:
        raise CertError(f"openssl failed: {e}") from e
    return "openssl"


def _with_cryptography(cert_path: str, key_path: str, common_name: str, days: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    alt_names: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    host = socket.gethostname()
    if host and host != "localhost" and host.isascii():
        alt_names.insert(0, x509.DNSName(host))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True,
                                     content_commitment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    # both files are written aside first, then swapped in: a failure never leaves a
    # new key next to the old certificate (TLS would refuse to start with that pair)
    _write_aside(key_path, key_pem, mode=0o600)
    _write_aside(cert_path, cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    os.replace(key_path + ".tmp", key_path)
    os.replace(cert_path + ".tmp", cert_path)
    return "cryptography"


def _write_aside(path: str, data: bytes, mode: int) -> None:
    tmp = path + ".tmp"
    with contextlib.suppress(FileNotFoundError):
        os.remove(tmp)  # O_CREAT keeps an existing file's mode; start fresh
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
