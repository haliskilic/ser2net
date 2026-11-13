"""Runtime adjustments for the standalone (PyInstaller) executables.

The Linux binary carries its own OpenSSL, which looks for CA certificates where
the distribution it was built on keeps them. Other distributions keep them
elsewhere (RHEL/Fedora under /etc/pki, openSUSE in /etc/ssl/ca-bundle.pem…), so
certificate checks — MQTT over TLS, OIDC discovery, LDAPS / StartTLS — would fail there
for want of trust anchors. prepare() points OpenSSL at the system bundle through the standard
SSL_CERT_FILE variable, unless the admin already set SSL_CERT_FILE / SSL_CERT_DIR.
Windows needs nothing: Python reads the Windows certificate store.

Both executables are single files that unpack themselves at every start into a new
_MEIxxxxxx directory under $TMPDIR (/tmp) or %TEMP% and run from it for as long as
they run: the page templates, static files, translations, ldap3 and native libraries
are read from there long after the start. Temp cleaners delete files nobody touched
for some days (systemd-tmpfiles: 10 days in /tmp; tmpreaper, Windows Disk Cleanup: a
week), which would break the web UI of an instance that runs longer. keep_unpacked()
holds a shared lock on the directory — systemd-tmpfiles (242+) skips a locked
directory and everything in it — and refreshes the access times inside it for the
cleaners that only compare timestamps (systemd 239 on RHEL 8, tmpreaper, Windows). A
directory left behind by a killed process is neither locked nor refreshed, so it
still ages out.
"""
from __future__ import annotations

import os
import sys
import threading
import time

# Probe order as in Go's crypto/x509 (root_linux.go).
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",                 # Debian, Ubuntu, Arch, Gentoo
    "/etc/pki/tls/certs/ca-bundle.crt",                   # Fedora, RHEL, CentOS, Alma, Rocky
    "/etc/ssl/ca-bundle.pem",                             # openSUSE, SLES
    "/etc/pki/tls/cacert.pem",                            # OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL 7+ extracted bundle
    "/etc/ssl/cert.pem",                                  # Alpine
)

# How often a one-file build refreshes the access times of its unpacked files: far
# inside the cleaners' ages (10 days, a week), even where an admin cut them to a day.
REFRESH_EVERY_S = 6 * 3600

notes: list[str] = []  # startup log lines, written once logging is up (runtime._serve)
_kept: dict[str, str] = {}   # unpacked directory -> its startup note
_locks: dict[str, int] = {}  # unpacked directory -> the fd holding its lock until exit


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


def is_unpacked(path: str) -> bool:
    """True for the temporary directory a one-file PyInstaller build unpacked itself
    into (the bootloader names it _MEIxxxxxx; a one-folder build has none)."""
    return os.path.basename(os.path.normpath(path)).startswith("_MEI") and os.path.isdir(path)


def touch_tree(root: str, now_ns: int | None = None) -> int:
    """Set the access time of root and of everything below it to now. Each modification
    time stays, so nothing looks changed (Jinja's template cache, the static files'
    ETags and Python's bytecode cache stay valid); the inode change time moves along.
    Symbolic links are refreshed themselves. Returns how many entries were refreshed."""
    now = time.time_ns() if now_ns is None else now_ns
    follow = os.utime not in os.supports_follow_symlinks  # Windows: no links in the bundle

    def touch(path: str) -> int:
        try:
            os.utime(path, ns=(now, os.lstat(path).st_mtime_ns), follow_symlinks=follow)
        except OSError:  # gone already, or not ours to change
            return 0
        return 1

    count = touch(root)
    for dirpath, dirnames, filenames in os.walk(root):
        count += sum(touch(os.path.join(dirpath, name)) for name in dirnames + filenames)
    return count


def _lock_shared(path: str) -> int:
    """An fd on path that holds flock(LOCK_SH). Waits up to 5 s for an exclusive holder
    (systemd-tmpfiles locks a directory only while it scans it) rather than hang."""
    if sys.platform == "win32":  # keep_unpacked locks on POSIX only
        raise OSError("flock() is POSIX only")
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    for _ in range(50):
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            time.sleep(0.1)
        except OSError:
            os.close(fd)
            raise
    os.close(fd)
    raise BlockingIOError(f"{path} stays locked by another process")


def _refresh_forever(path: str, every: float) -> None:
    while True:
        time.sleep(every)
        touch_tree(path)


def keep_unpacked(path: str, every: float = REFRESH_EVERY_S) -> str:
    """Keep temp cleaners away from a one-file build's unpacked directory for as long
    as this process runs: a shared lock on it (POSIX), and its access times refreshed
    every `every` seconds. Returns the line for the startup log."""
    if path in _kept:
        return _kept[path]
    how = f"access times refreshed every {every / 3600:g} h"
    if os.name == "posix":
        try:
            _locks[path] = _lock_shared(path)
            how = "locked, " + how
        except OSError as e:
            how += f" (could not lock it: {e})"
    threading.Thread(target=_refresh_forever, args=(path, every), daemon=True,
                     name="ser2netweb-keep-unpacked").start()
    _kept[path] = f"standalone: unpacked in {path} — {how}, so temp cleaners leave it alone"
    return _kept[path]


def prepare(frozen: bool | None = None, platform: str = sys.platform, environ=None,
            isfile=os.path.isfile, bundle: str | None = None) -> None:
    """Call once at startup, before anything creates an SSL context."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    environ = os.environ if environ is None else environ
    if not frozen:
        return
    bundle = getattr(sys, "_MEIPASS", "") if bundle is None else bundle
    if is_unpacked(bundle):
        notes.append(keep_unpacked(bundle))
    if not platform.startswith("linux"):
        return
    if environ.get("SSL_CERT_FILE") or environ.get("SSL_CERT_DIR"):
        notes.append("TLS: CA certificates from SSL_CERT_FILE / SSL_CERT_DIR")
    elif path := use_system_ca_bundle(environ, isfile):
        notes.append(f"TLS: using the system CA bundle {path}")
    else:
        notes.append("WARNING: no system CA certificates found — install the ca-certificates "
                     "package or set SSL_CERT_FILE, otherwise certificate checks for MQTT over "
                     "TLS, OIDC sign-in and LDAPS / StartTLS (unless ldap.ca_file is set) fail")
