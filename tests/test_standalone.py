"""The standalone executables must run with nothing else installed (#26).

Portable part (always runs): app.standalone points a frozen Linux build's
bundled OpenSSL at the distribution's CA bundle (Debian/Ubuntu, RHEL/Fedora,
openSUSE, Alpine layouts), leaves an admin's SSL_CERT_FILE / SSL_CERT_DIR alone,
warns when there is none, and does nothing for source runs or on Windows.

Binary part — opt-in: SER2NET_STANDALONE_BIN=<built ser2net or ser2net.exe> (the
CI 'standalone' jobs build the executables and set it):
  1. bundle: every ELF/PE inside needs only libraries that are bundled or part of
     the OS base (glibc; Windows system DLLs) — Linux: no glibc symbol newer than
     SER2NET_MAX_GLIBC (default 2.28); Windows: VC++ runtime and Python inside;
  2. run with an EMPTY PATH (no python, no openssl, nothing to borrow) and no
     PYTHON* variables: --version; it boots and serves the UI; a raw TCP mapping
     accepts a client; MQTT, LDAP and OIDC libraries are inside (no "not installed");
     Settings → Generate self-signed certificate works (no openssl program); OIDC
     discovery over a certificate-checked HTTPS connection works (bundled OpenSSL
     + CA certificates);
  3. every shared library the running processes mapped comes from the bundle or
     the OS base (Linux /proc/<pid>/maps, Windows module list);
  4. SER2NET_STANDALONE_IMAGE=<docker image> runs part 2 inside a clean container
     of that distribution (no Python, no openssl, empty PATH) instead. The test CA
     is mounted over the CA bundle the image has (or, for base images that ship
     without ca-certificates, at SER2NET_STANDALONE_CA=<the distribution's path>),
     so the OIDC check also proves the binary found it on its own.
Test-side needs: cryptography; pyelftools (Linux) or pefile (Windows); docker for 4.

Run: python3 tests/test_standalone.py
"""
import datetime
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
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from app import __version__, standalone  # noqa: E402
from app.web.auth import hash_password  # noqa: E402

BIN = os.path.abspath(os.environ["SER2NET_STANDALONE_BIN"]) if os.environ.get("SER2NET_STANDALONE_BIN") else ""
IMAGE = os.environ.get("SER2NET_STANDALONE_IMAGE", "")
IMAGE_CA = os.environ.get("SER2NET_STANDALONE_CA", "")
MAX_GLIBC = tuple(int(x) for x in os.environ.get("SER2NET_MAX_GLIBC", "2.28").split("."))
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


# ---------------------------------------------------------------- helpers
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ser2net test CA")])
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


class _Provider(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path != "/.well-known/openid-configuration":
            self.send_error(404)
            return
        iss = self.server.issuer
        body = json.dumps({"issuer": iss, "authorization_endpoint": iss + "/authorize",
                           "token_endpoint": iss + "/token", "jwks_uri": iss + "/jwks"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_provider(pki):
    """A fake OIDC provider over HTTPS with the test CA's server certificate."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["server.pem"], pki["server.key"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.issuer = f"https://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def write_config(data_dir, ui, raw_port, issuer, mqtt_port):
    serial = "COM250" if WINDOWS and not IMAGE else "/dev/ttyNOPE250"
    cfg = {
        "admin_ui": {"bind_ip": "127.0.0.1", "port": ui},
        "users": [{"username": "admin", "password_hash": hash_password(PASSWORD), "role": "admin",
                   "pwd_version": 1}],
        "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:1",
                 "user_dn_template": "uid={username},dc=example,dc=org"},
        "oidc": {"enabled": True, "issuer": issuer, "client_id": "ser2net", "client_secret": "s3cret"},
        "mappings": [{"name": "standalone-raw", "serial": {"port": serial},
                      "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                                  "port": raw_port},
                      "mqtt": {"enabled": True, "host": "127.0.0.1", "port": mqtt_port,
                               "base_topic": "ser2net/standalone"}}],
    }
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


def image_ca_path():
    """Where the binary should find the CA bundle in IMAGE: the first of
    standalone.CA_BUNDLES the image has (some carry several — current Fedora has the
    Debian path too), else SER2NET_STANDALONE_CA for images without ca-certificates."""
    script = ("for f in " + " ".join(standalone.CA_BUNDLES) +
              '; do if [ -f "$f" ]; then echo "$f"; exit 0; fi; done')
    out = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", IMAGE, "-c", script],
                         capture_output=True, text=True, timeout=300)
    found = out.stdout.strip()
    assert found or IMAGE_CA, f"{IMAGE} has no CA bundle and SER2NET_STANDALONE_CA is not set"
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
        return ["docker", "run", "--rm", "--init", "--name", self.name, "--network", "host", *user,
                "-e", "HOME=/work", "-e", "TMPDIR=/work/tmp", "-e", "PATH=/work/emptybin",
                "-v", f"{self.work}:/work", "-v", f"{BIN}:/opt/ser2net/ser2net:ro",
                "-v", f"{self.pki['ca.pem']}:{self.ca_path}:ro",
                IMAGE, "/opt/ser2net/ser2net"] + [a.replace(self.work, "/work") for a in args]

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

    def version(self):
        out = subprocess.run(self.command(["--version"]), env=self.env(), capture_output=True,
                             text=True, timeout=300)
        return (out.stdout + out.stderr).strip()

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
    sysroot = norm(os.environ.get("SystemRoot", r"C:\Windows")) + os.sep
    libs = mapped_libraries(pids)
    for path in sorted(libs):
        p = norm(os.path.realpath(path))
        base = os.path.basename(path)
        if p.startswith(bundle_n) or p == norm(os.path.realpath(BIN)):
            continue
        if WINDOWS:
            if p.startswith(sysroot) and not WIN_REDIST.match(base):
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
        return next((c.value for c in self.jar if c.name == "ser2net_csrf"), "")

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
        print("SKIP standalone executable checks: set SER2NET_STANDALONE_BIN=<built binary> "
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
    ui, raw, mqtt = free_port(), free_port(), free_port()
    write_config(data, ui, raw, provider.issuer, mqtt)
    run = Launch(work, pki)
    try:
        version = run.version()
        assert version == f"ser2net {__version__}", f"--version printed {version!r}"
        print(f"{where}: --version -> {version}  OK")

        run.start(data)
        if not wait_port(ui, 90):
            raise AssertionError(f"UI did not come up {where}:\n" + read(run.log.name)[-3000:])
        s = Session(f"http://127.0.0.1:{ui}")
        s.request("/login")
        st, _, _ = s.request("/login", {"username": "admin", "password": PASSWORD, "_csrf": s.csrf()})
        assert st == 303, f"login failed ({st})"

        st, _, page = s.request("/settings")
        assert st == 200 and "is not installed" not in page, "LDAP/OIDC library missing from the bundle"
        st, _, page = s.request("/settings/tls/generate", {"_csrf": s.csrf()})
        assert "Self-signed certificate generated" in page, \
            re.sub(r"\s+", " ", page[page.find("banner"):][:300])
        for f in ("cert.pem", "key.pem"):
            assert os.path.getsize(os.path.join(data, "tls", f)) > 0, f
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            os.path.join(data, "tls", "cert.pem"), os.path.join(data, "tls", "key.pem"))
        print(f"{where}: Settings -> Generate self-signed certificate, no openssl on PATH  OK")

        st, headers, page = s.request("/auth/oidc/login")
        location = headers.get("Location", "") if headers else ""
        assert st == 303 and location.startswith(provider.issuer + "/authorize?"), \
            f"OIDC discovery over HTTPS failed ({st}):\n" + read(os.path.join(data, "all.log"))[-1500:]
        print(f"{where}: OIDC discovery over a certificate-checked HTTPS connection  OK")

        with socket.create_connection(("127.0.0.1", raw), timeout=5):
            time.sleep(0.8)
        log = read(os.path.join(data, "all.log"))
        assert f"listening on 127.0.0.1:{raw} (raw, keepalive" in log, log[-1500:]
        assert "client connected: 127.0.0.1" in log, log[-1500:]
        assert "MQTT publishing serial lines to 127.0.0.1" in log, "paho-mqtt missing from the bundle?"
        assert "not installed" not in log, log[-1500:]
        if IMAGE:
            assert f"TLS: using the system CA bundle {run.ca_path}" in log, \
                f"binary did not pick {run.ca_path}:\n" + log[:1500]
        elif not WINDOWS:
            assert "TLS: CA certificates from SSL_CERT_FILE" in log, log[:1500]
        print(f"{where}: UI, raw TCP mapping, MQTT/LDAP/OIDC libraries present  OK")

        if not IMAGE:
            import psutil
            bundle = extraction_dir(run.tmp)
            problems, summary = (bundle_problems_windows if WINDOWS else bundle_problems_linux)(bundle)
            assert not problems, "bundle needs things from outside:\n  " + "\n  ".join(problems)
            print(f"bundle: {summary}; everything they need is bundled or part of the OS  OK")
            pids = [run.proc.pid] + [p.pid for p in psutil.Process(run.proc.pid).children(recursive=True)]
            problems, n = loaded_problems(bundle, pids)
            assert not problems, "running process uses outside libraries:\n  " + "\n  ".join(problems)
            print(f"running: all {n} mapped libraries come from the bundle or the OS  OK")
    finally:
        run_ok = hasattr(run, "proc")
        if run_ok:
            run.stop()
        provider.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nPASS: standalone executable {where}")


def main():
    test_ca_bundle_choice()
    test_binary()


if __name__ == "__main__":
    main()
