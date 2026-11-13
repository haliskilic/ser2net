"""The standalone executables must run with nothing else installed (#26).

Portable part (always runs): ser2netweb.standalone points a frozen Linux build's
bundled OpenSSL at the distribution's CA bundle (Debian/Ubuntu, RHEL/Fedora,
openSUSE, Alpine layouts), leaves an admin's SSL_CERT_FILE / SSL_CERT_DIR alone,
warns when there is none, and does nothing for source runs or on Windows.
ser2netweb.spec bundles every data file the app reads at run time (each non-Python
file under ser2netweb/: templates, static assets, translations), read without PyInstaller.
A one-file build keeps its unpacked directory from temp cleaners: access times are
refreshed (modification times kept) and, on Linux, the directory is locked, so the
real systemd-tmpfiles (242+, when installed) skips it even at age 0 while it still
removes a directory a killed run left behind.

Binary part — opt-in: SER2NETWEB_STANDALONE_BIN=<built ser2netweb or ser2netweb.exe> (the
CI 'standalone' jobs build the executables and set it):
  1. bundle: every ELF/PE inside needs only libraries that are bundled or part of
     the OS base (glibc; Windows system DLLs) — Linux: no glibc symbol newer than
     SER2NETWEB_MAX_GLIBC (default 2.28); Windows: VC++ runtime and Python inside;
  2. run with an EMPTY PATH (no python, no openssl, nothing to borrow) and no
     PYTHON* variables: --version; it boots and serves the UI; a raw TCP mapping
     accepts a client; MQTT, LDAP and OIDC libraries are inside and import (no "not
     installed", no "failed to import");
     Settings → Generate self-signed certificate works (no openssl program); OIDC
     sign-in over certificate-checked HTTPS works (bundled OpenSSL + CA certificates):
     discovery, then the callback with the token exchange and the RS256 id_token
     check (bundled authlib), which signs the user in; TR / EN → Türkçe really shows
     the Turkish UI. Linux: the unpacked copy is locked while it runs, and the checks
     after start-up follow a systemd-tmpfiles sweep of $TMPDIR at age 0 that must
     remove none of its files; a mapping on a PTY at 250000 baud (no termios constant:
     glibc 2.42+, e.g. Fedora 43+ / Ubuntu 26.04, refused how pyserial 3.5 set such a
     rate from a binary built for glibc 2.28) opens and relays the device's data;
  3. every shared library the running processes mapped comes from the bundle or
     the OS base (Linux /proc/<pid>/maps, Windows module list);
  4. Windows: hard links of the exe where winget, Scoop and Chocolatey put it name their
     package manager in --about, and --help the default data folder: the user's for
     winget and Chocolatey (a data\\config.json next to the exe, 3.0.0's, stays in use),
     data\\ next to the exe for Scoop (the folder its manifest persists);
  5. SER2NETWEB_STANDALONE_IMAGE=<docker image> runs part 2 inside a clean container
     of that distribution (no Python, no openssl, empty PATH) instead (the host's
     /dev/pts is mounted at /dev/hostpts for the PTY mapping). The test CA
     is mounted over the CA bundle the image has (or, for base images that ship
     without ca-certificates, at SER2NETWEB_STANDALONE_CA=<the distribution's path>),
     so the OIDC check also proves the binary found it on its own.
Test-side needs: cryptography; pyelftools (Linux) or pefile (Windows); docker for 4.

Run: python3 tests/test_standalone.py
"""
import base64
import datetime
import glob
import http.cookiejar
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from ser2netweb import __version__, envvars, standalone
from ser2netweb.web.auth import hash_password
from _server import ui_cookie

_BIN = envvars.get("SER2NETWEB_STANDALONE_BIN")
BIN = os.path.abspath(_BIN) if _BIN else ""
IMAGE = envvars.get("SER2NETWEB_STANDALONE_IMAGE", "")
IMAGE_CA = envvars.get("SER2NETWEB_STANDALONE_CA", "")
MAX_GLIBC = tuple(int(x) for x in envvars.get("SER2NETWEB_MAX_GLIBC", "2.28").split("."))
WINDOWS = os.name == "nt"
PASSWORD = "standalone-pass-1"

# Linux libraries every glibc distribution has, so they may come from the system:
# glibc itself, libgcc_s (glibc needs it; manylinux wheels expect the system's) and
# zlib (PyInstaller's own bootloader links it; Debian/Ubuntu "Priority: required").
LINUX_BASE = re.compile(r"^(ld-linux-x86-64\.so\.2|ld-linux-aarch64\.so\.1|libc\.so\.6|libm\.so\.6|"
                        r"libdl\.so\.2|libpthread\.so\.0|librt\.so\.1|libutil\.so\.1|"
                        r"libresolv\.so\.2|libnss_\w+\.so\.2|libgcc_s\.so\.1|libz\.so\.1)$")


def linux_base(name):
    """True for a glibc-base library, also under its versioned file name (libz.so.1.3.1)."""
    while True:
        if LINUX_BASE.match(name):
            return True
        head, dot, tail = name.rpartition(".")
        if not dot or not tail.isdigit() or ".so" not in head:
            return False
        name = head


# Windows runtimes that are redistributables, not part of Windows: must be bundled.
WIN_REDIST = re.compile(r"^(vcruntime|msvcp|msvcr|concrt|vcomp|vccorlib|mfc)\d", re.I)


# ---------------------------------------------------------------- portable part
def test_ca_bundle_choice():
    layouts = {
        "/etc/ssl/certs/ca-certificates.crt": {"/etc/ssl/certs/ca-certificates.crt"},       # Debian/Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt": {"/etc/pki/tls/certs/ca-bundle.crt",             # RHEL/Fedora
                                             "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"},
        "/etc/ssl/ca-bundle.pem": {"/etc/ssl/ca-bundle.pem"},                                  # openSUSE
        "/etc/ssl/cert.pem": {"/etc/ssl/cert.pem"},                                            # Alpine
    }
    for expected, files in layouts.items():
        env = {}
        assert standalone.use_system_ca_bundle(env, isfile=files.__contains__) == expected
        assert env == {"SSL_CERT_FILE": expected}, env
    for var in ("SSL_CERT_FILE", "SSL_CERT_DIR"):  # the admin's choice wins
        env = {var: "/custom"}
        assert standalone.use_system_ca_bundle(env, isfile=lambda p: True) is None
        assert env == {var: "/custom"}
    env = {}
    assert standalone.use_system_ca_bundle(env, isfile=lambda p: False) is None and env == {}

    def notes_for(frozen, platform, env, isfile):
        standalone.notes.clear()
        standalone.prepare(frozen=frozen, platform=platform, environ=env, isfile=isfile)
        return list(standalone.notes)

    rhel = {"/etc/pki/tls/certs/ca-bundle.crt"}.__contains__
    assert notes_for(False, "linux", {}, rhel) == [], "source runs keep Python's own OpenSSL setup"
    assert notes_for(True, "win32", {}, rhel) == [], "Windows reads its certificate store"
    env = {}
    assert notes_for(True, "linux", env, rhel) == [
        "TLS: using the system CA bundle /etc/pki/tls/certs/ca-bundle.crt"]
    assert env["SSL_CERT_FILE"] == "/etc/pki/tls/certs/ca-bundle.crt"
    assert "from SSL_CERT_FILE" in notes_for(True, "linux", {"SSL_CERT_DIR": "/x"}, rhel)[0]
    assert notes_for(True, "linux", {}, lambda p: False)[0].startswith("WARNING: no system CA")
    standalone.notes.clear()
    print("frozen Linux build finds the CA bundle on Debian/RHEL/openSUSE/Alpine layouts  OK")


def posix_path(path):
    return os.path.normpath(path).replace(os.sep, "/")


def spec_datas(exe=None):
    """The data entries ser2netweb.spec hands PyInstaller, without PyInstaller: the spec
    runs with stand-ins for collect_all() and the build steps (``exe``: a dict that gets
    the executable's EXE() keywords)."""
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda package: ([], [], [])
    fakes = {"PyInstaller": types.ModuleType("PyInstaller"),
             "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
             "PyInstaller.utils.hooks": hooks}
    seen = {}

    def analysis(scripts, **kw):
        seen.update(kw)
        return types.SimpleNamespace(scripts=scripts, pure=[], binaries=list(kw["binaries"]),
                                     datas=list(kw["datas"]))

    saved = {name: sys.modules.get(name) for name in fakes}
    sys.modules.update(fakes)
    try:
        with open(os.path.join(ROOT, "ser2netweb.spec"), encoding="utf-8") as fh:
            exec(compile(fh.read(), "ser2netweb.spec", "exec"),
                 {"Analysis": analysis, "PYZ": lambda *a, **k: None,
                  "EXE": lambda *a, **k: (exe if exe is not None else {}).update(k)})
    finally:
        for name, module in saved.items():
            if module is None:
                del sys.modules[name]
            else:
                sys.modules[name] = module
    return seen["datas"]


def bundled_files(datas):
    """Where every file of the (source, destination) entries lands in the bundle. As in
    PyInstaller, a relative source is relative to the spec and a directory brings
    everything below it."""
    out = set()
    for src, dest in datas:
        for match in glob.glob(os.path.join(ROOT, src)):
            if not os.path.isdir(match):
                out.add(posix_path(os.path.join(dest, os.path.basename(match))))
                continue
            for d, _, files in os.walk(match):
                for f in files:
                    out.add(posix_path(os.path.join(dest, os.path.relpath(os.path.join(d, f), match))))
    return out


def app_data_files():
    """Every file under ser2netweb/ that isn't Python source or bytecode — what the code reads
    next to itself at run time (in a frozen build: inside the bundle)."""
    out = set()
    for d, dirs, files in os.walk(os.path.join(ROOT, "ser2netweb")):
        dirs[:] = [x for x in dirs if x != "__pycache__" and not x.startswith(".")]
        out.update(posix_path(os.path.relpath(os.path.join(d, f), ROOT)) for f in files
                   if not f.startswith(".") and not f.endswith((".py", ".pyc", ".pyo")))
    return out


def test_spec_bundles_app_data():
    from ser2netweb import i18n
    from ser2netweb.web import server

    datas = spec_datas()
    for src, _ in datas:
        assert glob.glob(os.path.join(ROOT, src)), f"ser2netweb.spec bundles {src}, which doesn't exist"
    needed = app_data_files()
    for d in (server.TEMPLATES_DIR, server.STATIC_DIR, i18n.LOCALE_DIR):
        rel = posix_path(os.path.relpath(d, ROOT))
        assert any(p.startswith(rel + "/") for p in needed), f"no files found in {rel}"
    missing = sorted(needed - bundled_files(datas))
    assert not missing, ("the app reads these at run time, but ser2netweb.spec leaves them out of "
                         "the executables:\n  " + "\n  ".join(missing))
    print(f"ser2netweb.spec bundles all {len(needed)} data files under ser2netweb/ "
          "(templates, static assets, translations)  OK")


def test_spec_names_the_executable():
    """The build makes dist/ser2netweb(.exe), the name the build scripts, standalone.yml
    and release.yml take it by (ser2net up to 2.9.x), with the ser2netweb icon."""
    exe = {}
    spec_datas(exe)
    assert exe["name"] == "ser2netweb", exe
    assert os.path.isfile(os.path.join(ROOT, "tools", "ser2netweb.ico"))
    for script in ("build_standalone_linux.sh", "build_standalone_windows.ps1"):
        text = read(os.path.join(ROOT, "tools", script))
        assert "dist/ser2netweb --version" in text.replace("\\", "/").replace(".exe", ""), script
        assert "sbom build/ser2netweb.cdx.json" in text.replace("\\", "/"), script
    workflow = read(os.path.join(ROOT, ".github", "workflows", "standalone.yml"))
    for name in ("dist/ser2netweb\n", "dist/ser2netweb.exe\n", "build/ser2netweb.cdx.json\n",
                 "name: ser2netweb-linux-${{ matrix.arch }}\n", "name: ser2netweb-windows-x64\n"):
        assert name in workflow, name
    assert not re.search(r"ser2net(?!web)[-.](?:exe|cdx|linux|windows)|dist/ser2net\b(?!web)",
                         workflow), "an old asset name in standalone.yml"
    print("ser2netweb.spec builds dist/ser2netweb(.exe) with its icon; the build scripts and "
          "standalone.yml take it by that name  OK")


def assert_locked(path):
    """What systemd-tmpfiles tries before it cleans a directory must fail (Linux)."""
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    finally:
        os.close(fd)
    raise AssertionError(f"{path} is not locked, so systemd-tmpfiles would clean it")


def tmpfiles_version():
    exe = shutil.which("systemd-tmpfiles")
    if not exe:
        return 0
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout
    m = re.match(r"systemd (\d+)", out)
    return int(m.group(1)) if m else 0


def sweep_with_tmpfiles(tmp):
    """Clean tmp with the real systemd-tmpfiles at age 0 — everything counts as old, as
    in /tmp for the daily systemd-tmpfiles-clean after 10 days. False when there is no
    systemd-tmpfiles that honours directory locks (242+). A directory left behind by a
    killed run goes in first and must be gone after, so the sweep surely ran."""
    if tmpfiles_version() < 242:
        return False
    left = os.path.join(tmp, "_MEI_left_behind")
    os.makedirs(os.path.join(left, "ser2netweb"))
    with open(os.path.join(left, "ser2netweb", "base.html"), "w") as fh:
        fh.write("x")
    conf = tmp + ".tmpfiles.conf"
    with open(conf, "w") as fh:
        fh.write("e %s - - - 0\n" % tmp.replace("\\", "\\\\").replace(" ", "\\x20"))
    out = subprocess.run([shutil.which("systemd-tmpfiles"), "--clean", conf],
                         capture_output=True, text=True, timeout=120)
    os.remove(conf)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not os.path.exists(left), f"systemd-tmpfiles kept {left}: did it clean {tmp}?"
    return True


def test_unpacked_dir_kept():
    """A one-file build's unpacked directory is kept from temp cleaners while it runs."""
    work = tempfile.mkdtemp(prefix="ser2net_unpacked_")
    try:
        tmp = os.path.join(work, "tmp")
        bundle = os.path.join(tmp, "_MEI0000beefcafe00")
        catalog = os.path.join(bundle, "ser2netweb", "locale", "tr.json")
        os.makedirs(os.path.dirname(catalog))
        with open(catalog, "w") as fh:
            fh.write("{}")
        entries = [bundle, os.path.join(bundle, "ser2netweb"), os.path.dirname(catalog), catalog]
        if not WINDOWS:  # PyInstaller links shared libraries on Linux
            os.symlink(os.path.join("ser2netweb", "locale", "tr.json"), os.path.join(bundle, "libssl.so.3"))
            entries.append(os.path.join(bundle, "libssl.so.3"))
        month_ago = (int(time.time()) - 30 * 86400) * 10**9  # whole seconds: exact on NTFS too
        follow = os.utime not in os.supports_follow_symlinks
        for p in entries:
            os.utime(p, ns=(month_ago, month_ago), follow_symlinks=follow)
        assert standalone.touch_tree(bundle) == len(entries)
        for p in entries:
            st = os.lstat(p)
            assert st.st_atime_ns > month_ago + 29 * 86400 * 10**9, f"{p}: access time not refreshed"
            assert st.st_mtime_ns == month_ago, f"{p}: modification time changed"
        print("unpacked bundle: access times refreshed, modification times kept  OK")

        standalone.notes.clear()
        standalone.prepare(frozen=True, environ={"SSL_CERT_FILE": "/x"}, bundle=bundle)
        note = standalone.notes[0]
        assert note.startswith(f"standalone: unpacked in {bundle} ") and \
            "access times refreshed every 6 h, so temp cleaners leave it alone" in note, note
        onedir = os.path.join(work, "onedir", "_internal")
        os.makedirs(onedir)
        standalone.notes.clear()
        standalone.prepare(frozen=True, platform="win32", environ={}, bundle=onedir)
        standalone.prepare(frozen=False, platform="win32", environ={}, bundle=bundle)
        assert standalone.notes == [], "nothing to keep: a one-folder build, a source run"
        if not WINDOWS:
            assert " — locked, " in note, note
            assert_locked(bundle)
            print("unpacked bundle: locked while the process runs  OK")
            if sweep_with_tmpfiles(tmp):
                assert os.path.isfile(catalog), "systemd-tmpfiles removed a file of the locked directory"
                print("systemd-tmpfiles (age 0) skips the locked directory and still removes one "
                      "a killed run left behind  OK")
            else:
                print("SKIP systemd-tmpfiles sweep: no systemd-tmpfiles 242+ here")
    finally:
        standalone.notes.clear()
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- helpers
def free_ports(n):
    """``n`` distinct free ports (held together while they are picked)."""
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def wait_port(port, timeout):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def make_test_pki(d):
    """A test CA and a 127.0.0.1 server certificate it signed (the fake OIDC provider)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def build(subject, issuer, pub, signer, ca, sans=()):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(pub)
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(hours=1))
             .not_valid_after(now + datetime.timedelta(days=2))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             .add_extension(x509.KeyUsage(digital_signature=not ca, key_encipherment=False,
                                          content_commitment=False, data_encipherment=False,
                                          key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                          encipher_only=False, decipher_only=False), critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()),
                            critical=False))
        if sans:
            b = b.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
        return b.sign(signer, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ser2netweb test CA")])
    ca = build(ca_name, ca_name, ca_key.public_key(), ca_key, True)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = build(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]), ca_name,
                 key.public_key(), ca_key, False,
                 [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")])
    paths = {n: os.path.join(d, n) for n in ("ca.pem", "server.pem", "server.key")}
    with open(paths["ca.pem"], "wb") as fh:
        fh.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(paths["server.pem"], "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(paths["server.key"], "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return paths


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def rs256_jwt(key, kid, claims) -> str:
    """An id_token signed with this RSA key (cryptography only: authlib is the binary's)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    body = b64url(json.dumps(claims).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{b64url(signature)}"


def rsa_jwk(key, kid) -> dict:
    numbers = key.public_key().public_numbers()

    def raw(n: int) -> bytes:
        return n.to_bytes((n.bit_length() + 7) // 8, "big")

    return {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
            "n": b64url(raw(numbers.n)), "e": b64url(raw(numbers.e))}


class _Provider(http.server.BaseHTTPRequestHandler):
    """Discovery, JWKS and a token endpoint whose id_token carries server.claims."""

    def _json(self, doc) -> None:
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        iss = self.server.issuer
        if self.path == "/.well-known/openid-configuration":
            return self._json({"issuer": iss, "authorization_endpoint": iss + "/authorize",
                               "token_endpoint": iss + "/token", "jwks_uri": iss + "/jwks"})
        if self.path == "/jwks":
            return self._json({"keys": [rsa_jwk(self.server.key, "k1")]})
        self.send_error(404)

    def do_POST(self):
        if self.path != "/token":
            return self.send_error(404)
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0))
                                     .decode())
        if form.get("code") != ["CODE"] or form.get("client_secret") != ["s3cret"]:
            return self.send_error(400)
        now = int(time.time())
        claims = dict(self.server.claims, iss=self.server.issuer, aud="ser2net", sub="s-1",
                      iat=now, exp=now + 300)
        self._json({"id_token": rs256_jwt(self.server.key, "k1", claims),
                    "token_type": "Bearer", "access_token": "a"})

    def log_message(self, *args):
        pass


def start_provider(pki):
    """A fake OIDC provider over HTTPS with the test CA's server certificate."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["server.pem"], pki["server.key"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.issuer = f"https://127.0.0.1:{srv.server_address[1]}"
    srv.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv.claims = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# a rate without a termios constant (pyserial 3.5's BOTHER placeholder, which glibc 2.42+
# refuses through the old cfsetispeed() the glibc-2.28 build binds)
CUSTOM_BAUD = 250000


def open_pty():
    """(master fd, the slave's path as the binary sees it) for the custom-baud mapping;
    None on Windows. In a container the host's /dev/pts is mounted at /dev/hostpts."""
    if WINDOWS:
        return None
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)  # the binary opens it by name
    if IMAGE:
        path = "/dev/hostpts/" + os.path.basename(path)
    return master, path


def write_config(data_dir, ui, raw_port, issuer, mqtt_port, pty=None, baud_port=0):
    serial = "COM250" if WINDOWS and not IMAGE else "/dev/ttyNOPE250"
    cfg = {
        "admin_ui": {"bind_ip": "127.0.0.1", "port": ui},
        "users": [{"username": "admin", "password_hash": hash_password(PASSWORD), "role": "admin",
                   "pwd_version": 1}],
        "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:1",
                 "user_dn_template": "uid={username},dc=example,dc=org"},
        "oidc": {"enabled": True, "issuer": issuer, "client_id": "ser2net", "client_secret": "s3cret",
                 "admin_group": "ser2net-admins"},
        "mappings": [{"name": "standalone-raw", "serial": {"port": serial},
                      "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                                  "port": raw_port},
                      "mqtt": {"enabled": True, "host": "127.0.0.1", "port": mqtt_port,
                               "base_topic": "ser2net/standalone"}}],
    }
    if pty:
        cfg["mappings"].append({
            "name": "standalone-baud", "serial": {"port": pty[1], "baudrate": CUSTOM_BAUD},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": baud_port}})
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


def image_ca_path():
    """Where the binary should find the CA bundle in IMAGE: the first of
    standalone.CA_BUNDLES the image has (some carry several — current Fedora has the
    Debian path too), else SER2NETWEB_STANDALONE_CA for images without ca-certificates."""
    script = ("for f in " + " ".join(standalone.CA_BUNDLES) +
              '; do if [ -f "$f" ]; then echo "$f"; exit 0; fi; done')
    out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", IMAGE, "-c", script],
                         capture_output=True, text=True, timeout=300)
    found = out.stdout.strip()
    assert found or IMAGE_CA, f"{IMAGE} has no CA bundle and SER2NETWEB_STANDALONE_CA is not set"
    return found or IMAGE_CA


class Launch:
    """The binary, natively with a minimal environment or in a clean container."""

    def __init__(self, work, pki):
        self.work, self.pki = work, pki
        self.ca_path = image_ca_path() if IMAGE else ""
        self.tmp = os.path.join(work, "tmp")
        self.empty = os.path.join(work, "emptybin")  # PATH: nothing to find
        for d in (self.tmp, self.empty):
            os.makedirs(d, exist_ok=True)
        self.name = "s2n-standalone-" + uuid.uuid4().hex[:8]

    def command(self, args):
        if not IMAGE:
            return [BIN] + args
        user = [] if WINDOWS else ["--user", f"{os.getuid()}:{os.getgid()}"]
        pts = [] if WINDOWS else ["-v", "/dev/pts:/dev/hostpts"]  # the PTY mapping's device
        return ["docker", "run", "--rm", "--init", "--name", self.name, "--network", "host", *user,
                *pts, "-e", "HOME=/work", "-e", "TMPDIR=/work/tmp", "-e", "PATH=/work/emptybin",
                "-v", f"{self.work}:/work", "-v", f"{BIN}:/opt/ser2netweb/ser2netweb:ro",
                "-v", f"{self.pki['ca.pem']}:{self.ca_path}:ro",
                IMAGE, "/opt/ser2netweb/ser2netweb"] + [a.replace(self.work, "/work") for a in args]

    def env(self):
        if IMAGE:
            return dict(os.environ)  # for the docker client; the container gets -e only
        if WINDOWS:
            keep = ("SystemRoot", "WINDIR", "SystemDrive", "USERNAME", "USERPROFILE", "COMPUTERNAME")
            env = {k: os.environ[k] for k in keep if k in os.environ}
            env.update(TEMP=self.tmp, TMP=self.tmp)
        else:
            env = {"HOME": self.work, "TMPDIR": self.tmp}
        env.update(PATH=self.empty, SSL_CERT_FILE=self.pki["ca.pem"])  # native: CA given explicitly
        return env

    def output(self, args, extra_env=None):
        env = dict(self.env(), **(extra_env or {}))
        out = subprocess.run(self.command(args), env=env, capture_output=True, timeout=300)
        return (out.stdout + out.stderr).decode("utf-8", "replace").strip()

    def version(self):
        return self.output(["--version"])

    def start(self, data_dir):
        self.log = open(os.path.join(self.work, "stdout.log"), "wb")  # never an unread pipe
        args = ["--data-dir", data_dir, "--config", os.path.join(data_dir, "config.json")]
        self.proc = subprocess.Popen(self.command(args), env=self.env(), stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self):
        import psutil
        try:
            kids = psutil.Process(self.proc.pid).children(recursive=True)
        except psutil.Error:
            kids = []
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for k in kids:  # the onefile child, if the parent didn't take it down
            try:
                k.kill()
            except psutil.Error:
                pass
        if IMAGE:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=60)
        self.log.close()


# ---------------------------------------------------------------- bundle analysis
def extraction_dir(tmp):
    dirs = [os.path.join(tmp, d) for d in os.listdir(tmp) if d.startswith("_MEI")]
    assert len(dirs) == 1, f"expected one PyInstaller extraction dir in {tmp}: {dirs}"
    return dirs[0]


def bundled_binaries(bundle):
    for d, _, files in os.walk(bundle):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                head = fh.read(4)
            if head == b"\x7fELF" or head[:2] == b"MZ":
                yield p


def bundle_problems_linux(bundle):
    from elftools.elf.dynamic import DynamicSection
    from elftools.elf.elffile import ELFFile
    from elftools.elf.gnuversions import GNUVerNeedSection

    names = {f for _, _, fs in os.walk(bundle) for f in fs}
    problems, newest, count = [], (0, 0), 0
    for path in [BIN, *bundled_binaries(bundle)]:
        count += 1
        rel = os.path.relpath(path, bundle) if path != BIN else "<bootloader>"
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            for sec in elf.iter_sections():
                if isinstance(sec, DynamicSection):
                    for tag in sec.iter_tags("DT_NEEDED"):
                        if tag.needed not in names and not LINUX_BASE.match(tag.needed):
                            problems.append(f"{rel} needs {tag.needed}: neither bundled nor part of glibc")
                elif isinstance(sec, GNUVerNeedSection):
                    for _, auxes in sec.iter_versions():
                        for aux in auxes:
                            if not aux.name.startswith("GLIBC_"):
                                continue
                            if aux.name == "GLIBC_PRIVATE":
                                problems.append(f"{rel} needs GLIBC_PRIVATE (tied to one glibc build)")
                                continue
                            v = tuple(int(x) for x in aux.name[6:].split("."))
                            newest = max(newest, v)
                            if v > MAX_GLIBC:
                                problems.append(f"{rel} needs {aux.name} (> glibc "
                                                f"{'.'.join(map(str, MAX_GLIBC))})")
    return problems, f"{count} ELF files, newest glibc symbol {'.'.join(map(str, newest))}"


def bundle_problems_windows(bundle):
    import pefile

    names = {f.lower() for _, _, fs in os.walk(bundle) for f in fs}
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    problems, count = [], 0
    for path in [BIN, *bundled_binaries(bundle)]:
        count += 1
        rel = os.path.relpath(path, bundle) if path != BIN else "<bootloader>"
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for imp in getattr(pe, attr, []):
                dll = imp.dll.decode(errors="replace").lower()
                if dll in names or dll.startswith(("api-ms-win-", "ext-ms-win-")):
                    continue
                if WIN_REDIST.match(dll):
                    problems.append(f"{rel} imports {dll}: a redistributable runtime that is not bundled")
                elif not os.path.isfile(os.path.join(system32, dll)):
                    problems.append(f"{rel} imports {dll}: not bundled and not a Windows system DLL")
        pe.close()
    for must in ("vcruntime140.dll",):
        if must not in names:
            problems.append(f"{must} is not bundled")
    if not any(re.fullmatch(r"python3\d+\.dll", n) for n in names):
        problems.append("the Python DLL is not bundled")
    return problems, f"{count} PE files"


def check_version_resource(exe):
    """The Windows exe carries a VS_VERSIONINFO built from ser2netweb.__version__ (ser2netweb.spec
    + tools/win_version_info.py), so Explorer shows a version/publisher instead of nothing
    and the exe no longer uses PyInstaller's default console icon."""
    import pefile

    pe = pefile.PE(exe, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
    strings = {}
    for info in getattr(pe, "FileInfo", []):
        for entry in (info if isinstance(info, list) else [info]):
            for table in getattr(entry, "StringTable", []) or []:
                for key, val in table.entries.items():
                    strings[key.decode("utf-8", "replace")] = val.decode("utf-8", "replace")
    has_icon = any(getattr(e, "name", None) and e.name.string == b"RT_GROUP_ICON" or
                   getattr(e, "id", None) == 14  # RT_GROUP_ICON
                   for e in pe.DIRECTORY_ENTRY_RESOURCE.entries)
    pe.close()
    assert strings.get("ProductName") == "ser2netweb", strings
    assert strings.get("OriginalFilename") == "ser2netweb.exe", strings
    assert strings.get("ProductVersion") == __version__, strings
    assert strings.get("FileVersion", "").startswith(__version__), strings
    assert has_icon, "no RT_GROUP_ICON resource (custom icon not embedded)"
    print(f"windows exe: version resource (ProductName={strings['ProductName']}, "
          f"ProductVersion={strings['ProductVersion']}) and an icon  OK")


def mapped_libraries(pids):
    """Every DLL / shared object the processes have mapped."""
    import psutil
    seen = set()
    for pid in pids:
        if WINDOWS:
            for m in psutil.Process(pid).memory_maps():
                if m.path.lower().endswith((".dll", ".pyd", ".exe")):
                    seen.add(m.path)
        else:
            with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split(None, 5)
                    if len(parts) == 6 and ".so" in os.path.basename(parts[5].strip()):
                        seen.add(parts[5].strip())
    return seen


def loaded_problems(bundle, pids):
    problems = []
    norm = os.path.normcase
    bundle_n = norm(os.path.realpath(bundle)) + os.sep
    bundled = {norm(f) for _, _, fs in os.walk(bundle) for f in fs}
    sysroot = norm(os.environ.get("SystemRoot", r"C:\Windows")) + os.sep
    # Windows Defender injects its AMSI provider (MpOAV.dll) into processes itself
    defender = tuple(norm(os.path.join(os.environ.get(var, default), *sub)) + os.sep
                     for var, default, sub in (
                         ("ProgramData", r"C:\ProgramData", ("Microsoft", "Windows Defender")),
                         ("ProgramFiles", r"C:\Program Files", ("Windows Defender",))))
    libs = mapped_libraries(pids)
    for path in sorted(libs):
        p = norm(os.path.realpath(path))
        base = os.path.basename(path)
        if p.startswith(bundle_n) or p == norm(os.path.realpath(BIN)):
            continue
        if WINDOWS:
            # a VC++ runtime in System32 is only the machine's own copy when the
            # bundle carries the same DLL (the static check requires that)
            if p.startswith(sysroot) and (not WIN_REDIST.match(base) or norm(base) in bundled):
                continue
            if p.startswith(defender):
                continue
            problems.append(f"loaded from outside the bundle and Windows: {path}")
        elif not (linux_base(base) or "/gconv/" in p):
            problems.append(f"loaded from outside the bundle and glibc: {path}")
    return problems, len(libs)


# ---------------------------------------------------------------- binary part
class Session:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def csrf(self):
        name = ui_cookie("ser2net_csrf", urllib.parse.urlsplit(self.base).port)
        return next((c.value for c in self.jar if c.name == name), "")

    def request(self, path, fields=None, timeout=30):
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET")
        try:
            with self.op.open(req, timeout=timeout) as r:
                return r.status, r.headers, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode(errors="replace")


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def test_binary():
    if not BIN:
        print("SKIP standalone executable checks: set SER2NETWEB_STANDALONE_BIN=<built binary> "
              "(the CI 'standalone' jobs do)")
        return
    where = f"in a clean {IMAGE} container" if IMAGE else "natively"
    if IMAGE:
        subprocess.run(["docker", "pull", "-q", IMAGE], check=True, timeout=600,
                       capture_output=True)
    work = tempfile.mkdtemp(prefix="ser2net_standalone_")
    data = os.path.join(work, "data")
    pki = make_test_pki(work)
    provider = start_provider(pki)
    pty = open_pty()
    run = Launch(work, pki)
    try:
        version = run.version()
        assert version == f"ser2netweb {__version__}", f"--version printed {version!r}"
        print(f"{where}: --version -> {version}  OK")

        # both platforms now ship a python-build-standalone interpreter with a supported
        # OpenSSL (>= 3.5); a build that fell back to an EOL OpenSSL 3.0 must fail here
        about = run.output(["--about"])
        m = re.search(r"OpenSSL (\d+)\.(\d+)", about)
        assert m and (int(m.group(1)), int(m.group(2))) >= (3, 5), \
            f"{where}: bundled OpenSSL is below the supported 3.5 series:\n{about}"
        print(f"{where}: --about reports {m.group(0)} (a supported series)  OK")

        licenses = run.output(["--licenses"])
        for needle in ("AGPL-3.0-only", "CPython", "OpenSSL", "ldap3 2.", "LGPL", "paho-mqtt",
                       "cryptography", "pyinstaller", "starlette", "pyserial",
                       "htmx.org 2.", "@xterm/xterm 6."):
            assert needle in licenses, f"--licenses does not cover {needle!r}"
        # python-build-standalone's native libraries (the python.org build folded these
        # into its own LICENSE.txt; PBS — now used on Windows too — keeps them apart)
        for lib in ("openssl-3", "libffi", "zlib"):
            assert f"LICENSE.{lib} " in licenses, f"--licenses misses {lib}"
        if not WINDOWS:
            assert "LICENSE.mpdecimal " in licenses, "--licenses misses mpdecimal"
        print(f"{where}: --licenses covers ser2netweb, CPython/OpenSSL and every bundled package  OK")

        # the build's SBOM (next to the executable after a build; not in the distro jobs)
        sbom = os.path.join(ROOT, "build", "ser2netweb.cdx.json")
        if os.path.isfile(sbom) and not IMAGE:
            with open(sbom, encoding="utf-8") as fh:
                comps = json.load(fh)["components"]
            names = {c["name"] for c in comps}
            assert {"cpython", "openssl", "zlib", "htmx.org", "@xterm/xterm"} <= names, sorted(names)
            # (an out-of-date license override fails tests/test_third_party.py, the vendor job)
            unnamed = [c["name"] for c in comps if "expression" not in c["licenses"][0]]
            print(f"{where}: SBOM lists {len(comps)} components"
                  + (f"; without an SPDX license: {unnamed}" if unnamed else ", all with SPDX licenses")
                  + "  OK")

        if not IMAGE:  # LGPL: the bundled ldap3 can be replaced without rebuilding
            override = os.path.join(work, "override")
            os.makedirs(os.path.join(override, "ldap3"))
            with open(os.path.join(override, "ldap3", "__init__.py"), "w") as fh:
                fh.write('__version__ = "0.0-override-test"\n')
            about = run.output(["--about"])
            assert re.search(r"ldap3\s+2\.\d", about), f"bundled ldap3 not found:\n{about}"
            assert re.search(r"pyasn1\s+0\.\d", about), f"bundled pyasn1 not found:\n{about}"
            assert "not installed" not in about and "failed to import" not in about, about
            about = run.output(["--about"], {"SER2NETWEB_LIB_OVERRIDE": override})
            assert re.search(r"ldap3\s+0\.0-override-test", about), \
                f"SER2NETWEB_LIB_OVERRIDE did not replace ldap3:\n{about}"
            print(f"{where}: SER2NETWEB_LIB_OVERRIDE loads a replacement ldap3 (LGPL)  OK")
        if WINDOWS and not IMAGE:
            check_package_managers(run, work)

        # the ports picked just before the start: the runs above take a while, and a port
        # freed that long is anyone's
        ui, raw, mqtt, baud = free_ports(4)
        write_config(data, ui, raw, provider.issuer, mqtt, pty, baud)
        run.start(data)
        if not wait_port(ui, 90):
            raise AssertionError(f"UI did not come up {where}:\n" + read(run.log.name)[-3000:])
        if not WINDOWS:  # the unpacked copy it runs from is locked; a real tmp cleaner skips it
            bundle = extraction_dir(run.tmp)
            assert_locked(bundle)
            print(f"{where}: the unpacked bundle is locked while the binary runs  OK")
            files = [os.path.join(d, f) for d, _, fs in os.walk(bundle) for f in fs]
            if sweep_with_tmpfiles(run.tmp):
                gone = [p for p in files if not os.path.lexists(p)]
                assert not gone, f"systemd-tmpfiles removed {len(gone)} files of the running binary: {gone[:5]}"
                print(f"{where}: systemd-tmpfiles (age 0) leaves all {len(files)} files alone; "
                      "the checks below run after that sweep  OK")
        s = Session(f"http://127.0.0.1:{ui}")
        s.request("/login")
        st, _, _ = s.request("/login", {"username": "admin", "password": PASSWORD, "_csrf": s.csrf()})
        assert st == 303, f"login failed ({st})"

        st, _, page = s.request("/settings")
        assert st == 200 and "is not installed" not in page, "LDAP/OIDC library missing from the bundle"
        assert "failed to import" not in page and "missing from this executable" not in page, \
            "LDAP/OIDC library in the bundle does not import"
        # (before a certificate is generated: from then on, until a restart, the sign-in
        # cookies are marked Secure while the UI still answers over plain HTTP)
        sso = Session(f"http://127.0.0.1:{ui}")
        st, headers, page = sso.request("/auth/oidc/login")
        location = headers.get("Location", "") if headers else ""
        assert st == 303 and location.startswith(provider.issuer + "/authorize?"), \
            f"OIDC discovery over HTTPS failed ({st}):\n" + read(os.path.join(data, "all.log"))[-1500:]
        print(f"{where}: OIDC discovery over a certificate-checked HTTPS connection  OK")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        provider.claims = {"preferred_username": "sso.user", "groups": ["/plant/ser2net-admins"],
                           "nonce": query["nonce"][0]}
        st, headers, _ = sso.request("/auth/oidc/callback?" + urllib.parse.urlencode(
            {"code": "CODE", "state": query["state"][0]}))
        assert st == 303 and headers.get("Location") == "/", \
            f"OIDC callback failed ({st}):\n" + read(os.path.join(data, "all.log"))[-1500:]
        st, _, page = sso.request("/settings")
        assert st == 200 and "sso.user" in page, f"no session after the OIDC callback ({st})"
        assert "login: sso.user (admin, oidc)" in read(os.path.join(data, "all.log"))
        print(f"{where}: OIDC callback: token exchange, RS256 id_token check, signed in  OK")

        # (before the certificate below: once TLS is configured, new cookies are Secure-only)
        import markupsafe
        with open(os.path.join(ROOT, "ser2netweb", "locale", "tr.json"), encoding="utf-8") as fh:
            tr = json.load(fh)
        st, headers, _ = s.request("/lang/tr?next=/settings")
        assert st == 303 and headers and headers.get("Location") == "/settings", f"/lang/tr: {st}"
        st, _, page = s.request("/settings")
        english = [en for en in ("Settings", "Web UI access", "Change your password", "Default language")
                   if str(markupsafe.escape(tr[en])) not in page]
        assert st == 200 and 'lang="tr"' in page and not english, \
            f"Turkish chosen, yet these stay English (translations not bundled?): {english} ({st})"
        s.request("/lang/en?next=/settings")
        print(f"{where}: TR / EN -> the Settings page is in Turkish (translations bundled)  OK")

        st, headers, page = s.request("/settings/tls/generate", {"_csrf": s.csrf()})
        # done, and the UI (plain HTTP until now) takes new connections over HTTPS at once:
        # the page itself is the answer, over this connection (a redirect would be followed
        # over HTTP, which the UI serves no more), and it says where to go on
        where_to = headers.get("Location", "") if headers else ""
        assert st == 200 and not where_to, f"generate: {st} {where_to!r}"
        assert "Self-signed certificate generated" in page and \
            f"use HTTPS now: continue at https://127.0.0.1:{ui}/settings" in page, \
            re.sub(r"\s+", " ", page[page.find("banner"):][:400])
        for f in ("cert.pem", "key.pem"):
            assert os.path.getsize(os.path.join(data, "tls", f)) > 0, f
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            os.path.join(data, "tls", "cert.pem"), os.path.join(data, "tls", "key.pem"))
        unverified = ssl.create_default_context()
        unverified.check_hostname, unverified.verify_mode = False, ssl.CERT_NONE
        with urllib.request.urlopen(f"https://127.0.0.1:{ui}/healthz", timeout=30,
                                    context=unverified) as r:
            assert r.status == 200, f"/healthz over HTTPS: {r.status}"
        print(f"{where}: Settings -> Generate self-signed certificate, no openssl on PATH; the "
              "UI serves HTTPS at once  OK")


        with socket.create_connection(("127.0.0.1", raw), timeout=5):
            time.sleep(0.8)
        log = read(os.path.join(data, "all.log"))
        assert f"listening on 127.0.0.1:{raw} (raw, keepalive" in log, log[-1500:]
        assert "client connected: 127.0.0.1" in log, log[-1500:]
        # (paho-mqtt imported: the mapping's publisher connects; without it the log says
        # "MQTT publishing disabled: ...")
        assert f"MQTT: connecting to 127.0.0.1:{mqtt}" in log and \
            "MQTT publishing disabled" not in log, "paho-mqtt missing from the bundle?"
        assert "not installed" not in log and "failed to import" not in log, log[-1500:]
        assert "UI translation is missing" not in log, log[:1500]
        kept = re.search(r"standalone: unpacked in [^\n]*_MEI[^\n]* — (locked, )?access times "
                         r"refreshed every 6 h, so temp cleaners leave it alone", log)
        assert kept and (WINDOWS or kept.group(1)), log[:1500]
        if IMAGE:
            assert f"TLS: using the system CA bundle {run.ca_path}" in log, \
                f"binary did not pick {run.ca_path}:\n" + log[:1500]
        elif not WINDOWS:
            assert "TLS: CA certificates from SSL_CERT_FILE" in log, log[:1500]
        print(f"{where}: UI, raw TCP mapping, MQTT/LDAP/OIDC libraries present  OK")

        if pty:
            check_custom_baud(pty, baud, data, where)

        if not IMAGE:
            import psutil
            bundle = extraction_dir(run.tmp)
            assert os.path.isfile(os.path.join(bundle, "ldap3", "__init__.py")), \
                "ldap3 must be bundled as replaceable source files, not inside the archive"
            problems, summary = (bundle_problems_windows if WINDOWS else bundle_problems_linux)(bundle)
            assert not problems, "bundle needs things from outside:\n  " + "\n  ".join(problems)
            print(f"bundle: {summary}; everything they need is bundled or part of the OS  OK")
            pids = [run.proc.pid] + [p.pid for p in psutil.Process(run.proc.pid).children(recursive=True)]
            problems, n = loaded_problems(bundle, pids)
            assert not problems, "running process uses outside libraries:\n  " + "\n  ".join(problems)
            print(f"running: all {n} mapped libraries come from the bundle or the OS  OK")
            if WINDOWS:
                check_version_resource(BIN)
    finally:
        run_ok = hasattr(run, "proc")
        if run_ok:
            run.stop()
        if pty:
            os.close(pty[0])
        provider.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nPASS: standalone executable {where}")


def _run_exe(exe, env, *args):
    p = subprocess.run([exe, *args], env=env, capture_output=True, timeout=300)
    return (p.stdout + p.stderr).decode("utf-8", "replace")


def _default_data_dir(exe, env):
    """The data folder --help names ("here: ...") for this exe."""
    helptext = " ".join(_run_exe(exe, dict(env, COLUMNS="500"), "--help").split())
    m = re.search(r"here: (.+?)\. For --install-service", helptext)
    assert m, helptext
    return m.group(1)


def check_package_managers(run, work):
    """The exe where winget (its portable package folder), Scoop (apps\\<app>\\<version>, with
    the manifest.json and install.json it writes) and Chocolatey (lib\\<id>\\tools, next to
    <id>.nupkg) put it: --about names the package manager, --help the data folder it uses
    without --data-dir."""
    base = os.path.join(os.path.realpath(work), "managers")  # (long names, not RUNNER~1)
    env = dict(run.env(), LOCALAPPDATA=os.path.join(base, "local"))
    user = os.path.join(base, "local", "ser2netweb")
    winget = os.path.join(base, "WinGet", "Packages",
                          "haliskilic.ser2netweb_Microsoft.Winget.Source_8wekyb3d8bbwe")
    scoop = os.path.join(base, "scoop", "apps", "ser2netweb", "3.0.0")
    choco = os.path.join(base, "chocolatey", "lib", "ser2netweb", "tools")
    for name, folder, marks, data in (
            ("winget", winget, [], user),
            ("Scoop", scoop, ["manifest.json", "install.json"], os.path.join(scoop, "data")),
            ("Chocolatey", choco, [os.path.join("..", "ser2netweb.nupkg")], user)):
        os.makedirs(folder)
        for mark in marks:
            with open(os.path.join(folder, mark), "w", encoding="utf-8") as fh:
                fh.write("{}")
        exe = os.path.join(folder, "ser2netweb.exe")
        try:
            os.link(BIN, exe)
        except OSError:
            shutil.copy2(BIN, exe)
        about = _run_exe(exe, env, "--about")
        assert f"Build: standalone executable, {name} package" in about, f"{name}:\n{about}"
        got = _default_data_dir(exe, env)
        assert os.path.normcase(got) == os.path.normcase(data), f"{name}: {got}, not {data}"
    # a data folder 3.0.0 made next to winget's exe stays in use
    os.makedirs(os.path.join(winget, "data"))
    with open(os.path.join(winget, "data", "config.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    got = _default_data_dir(os.path.join(winget, "ser2netweb.exe"), env)
    assert os.path.normcase(got) == os.path.normcase(os.path.join(winget, "data")), got
    print("natively: the exe as winget, Scoop and Chocolatey install it names its package "
          "manager; its data in the user's folder (winget, Chocolatey; a data\\ next to it "
          "with a config.json kept) or the persisted data\\ (Scoop)  OK")


def check_custom_baud(pty, port, data, where):
    """The PTY mapping at CUSTOM_BAUD is open (not "device missing") and relays what the
    device sends to a client."""
    master, path = pty
    log_path = os.path.join(data, "all.log")
    opened = f"[standalone-baud] serial open: {path} @ {CUSTOM_BAUD}N81"
    deadline = time.time() + 20
    while opened not in read(log_path) and time.time() < deadline:
        time.sleep(0.2)
    log = read(log_path)
    assert opened in log, f"{where}: the {CUSTOM_BAUD} baud port did not open:\n" + "\n".join(
        ln for ln in log.splitlines() if "standalone-baud" in ln)[-1500:]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        time.sleep(0.5)
        os.write(master, b"at 250000 baud\n")
        c.settimeout(5)
        got = b""
        while len(got) < 15:
            chunk = c.recv(100)
            if not chunk:
                break
            got += chunk
    assert got == b"at 250000 baud\n", f"{where}: the device's data did not arrive: {got!r}"
    print(f"{where}: a PTY mapping at {CUSTOM_BAUD} baud opens and relays the device's data  OK")


def main():
    test_ca_bundle_choice()
    test_spec_bundles_app_data()
    test_spec_names_the_executable()
    test_unpacked_dir_kept()
    test_binary()


if __name__ == "__main__":
    main()
