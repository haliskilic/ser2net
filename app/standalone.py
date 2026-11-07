"""Runtime adjustments for the standalone (PyInstaller) executables.

The Linux binary carries its own OpenSSL, which looks for CA certificates where
the distribution it was built on keeps them. Other distributions keep them
elsewhere (RHEL/Fedora under /etc/pki, openSUSE in /etc/ssl/ca-bundle.pem…), so
certificate checks — MQTT over TLS, OIDC discovery, LDAPS / StartTLS — would fail there
for want of trust anchors. prepare() points OpenSSL at the system bundle through the standard
SSL_CERT_FILE variable, unless the admin already set SSL_CERT_FILE / SSL_CERT_DIR.
Windows needs nothing: Python reads the Windows certificate store.
"""
from __future__ import annotations

import os
import sys

# Probe order as in Go's crypto/x509 (root_linux.go).
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",                 # Debian, Ubuntu, Arch, Gentoo
    "/etc/pki/tls/certs/ca-bundle.crt",                   # Fedora, RHEL, CentOS, Alma, Rocky
    "/etc/ssl/ca-bundle.pem",                             # openSUSE, SLES
    "/etc/pki/tls/cacert.pem",                            # OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL 7+ extracted bundle
    "/etc/ssl/cert.pem",                                  # Alpine
)

notes: list[str] = []  # startup log lines, written once logging is up (runtime._serve)


def use_system_ca_bundle(environ=None, isfile=os.path.isfile) -> str | None:
    """Set SSL_CERT_FILE to the first CA bundle that exists; returns its path, or
    None if the admin configured SSL_CERT_FILE/SSL_CERT_DIR or none was found."""
    environ = os.environ if environ is None else environ
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        return None
    for path in CA_BUNDLES:
        if isfile(path):
            environ["SSL_CERT_FILE"] = path
            return path
    return None


def prepare(frozen: bool | None = None, platform: str = sys.platform, environ=None,
            isfile=os.path.isfile) -> None:
    """Call once at startup, before anything creates an SSL context."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    environ = os.environ if environ is None else environ
    if not (frozen and platform.startswith("linux")):
        return
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        notes.append("TLS: CA certificates from SSL_CERT_FILE / SSL_CERT_DIR")
    elif path := use_system_ca_bundle(environ, isfile):
        notes.append(f"TLS: using the system CA bundle {path}")
    else:
        notes.append("WARNING: no system CA certificates found — install the ca-certificates "
                     "package or set SSL_CERT_FILE, otherwise certificate checks for MQTT over "
                     "TLS, OIDC sign-in and LDAPS / StartTLS (unless ldap.ca_file is set) fail")
