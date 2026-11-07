"""Test certificates (needs cryptography): a CA, and server certificates it issues.

    from _pki import TestCA
    ca = TestCA(folder)                                   # folder/<name>.pem: the CA
    cert, key = ca.issue(folder, "dc", ["dc.corp.local", "127.0.0.1"])
    ca.der_path                                           # the CA as DER (Windows' .cer)

Names that parse as IP addresses become IP subject alternative names, the rest DNS
names. Every certificate has the extensions strict verification wants (basic
constraints, key usage, subject / authority key identifiers).
"""
from __future__ import annotations

import datetime
import ipaddress
import os


class TestCA:
    __test__ = False   # not a test class

    def __init__(self, folder: str, name: str = "ser2net test CA"):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        self.key = ec.generate_private_key(ec.SECP256R1())
        self.name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        self.cert = self._build(self.name, self.key.public_key(), ca=True)
        stem = os.path.join(folder, name.replace(" ", "-"))
        self.path, self.der_path = stem + ".pem", stem + ".der"
        with open(self.path, "wb") as fh:
            fh.write(self.cert.public_bytes(serialization.Encoding.PEM))
        with open(self.der_path, "wb") as fh:
            fh.write(self.cert.public_bytes(serialization.Encoding.DER))

    def _build(self, subject, public_key, ca: bool, names=(), self_signed_key=None):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        now = datetime.datetime.now(datetime.timezone.utc)
        signer = self_signed_key or self.key
        b = (x509.CertificateBuilder().subject_name(subject)
             .issuer_name(subject if self_signed_key or ca else self.name)
             .public_key(public_key).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(hours=1))
             .not_valid_after(now + datetime.timedelta(days=2))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             .add_extension(x509.KeyUsage(digital_signature=not ca, key_encipherment=False,
                                          content_commitment=False, data_encipherment=False,
                                          key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                          encipher_only=False, decipher_only=False),
                            critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key),
                            critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                 signer.public_key()), critical=False))
        if names:
            sans = []
            for n in names:
                try:
                    sans.append(x509.IPAddress(ipaddress.ip_address(n)))
                except ValueError:
                    sans.append(x509.DNSName(n))
            b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        return b.sign(signer, hashes.SHA256())

    def issue(self, folder: str, stem: str, names, self_signed: bool = False):
        """A server certificate for `names` (signed by this CA, or by its own key):
        (certificate path, key path), PEM."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, list(names)[0])])
        cert = self._build(subject, key.public_key(), ca=False, names=names,
                           self_signed_key=key if self_signed else None)
        cert_path, key_path = (os.path.join(folder, stem + ext) for ext in (".pem", ".key"))
        with open(cert_path, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        return cert_path, key_path
