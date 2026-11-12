"""Settings → "Generate self-signed certificate" must not need an external program (#26).

It used to run the `openssl` command, which Windows doesn't have (nor do many
minimal Linux installs), so the button could only fail there. The certificate is
now made in-process with the `cryptography` package — bundled in the standalone
executables — and the openssl command is only a fallback for source installs.

  1. with cryptography and an EMPTY PATH: a TLS server loads the certificate and
     key, and a client that trusts it completes a verified handshake to
     "localhost" and 127.0.0.1 (subject alternative names); key file is 0600 (POSIX);
     it is valid from a day before it was made, for exactly 825 days in all
  2. without cryptography: falls back to the openssl command when one exists, and
     with neither there is a clear error instead of a crash
  3. the real server started with an EMPTY PATH: POST /settings/tls/generate saves
     the pair and points admin_ui.tls_cert / tls_key at it
  4. what a certificate says (certinfo, with cryptography and with the ssl module's
     decoder): subject, names, validity, self-signed or CA-issued; expired / not valid
     yet / ending soon; files that are no certificate; the start-up log's line;
     Settings → Admin TLS shows it, and generating while the server's clock reads
     earlier than it can warns (generated all the same)
Parts 1, 3 and 4 need cryptography (pip install cryptography) and are skipped without (a
SKIP line, which tests/run_all.py repeats), or fail with SER2NET_OPTIONAL_TESTS=1.

Run: python3 tests/test_tls_selfsigned.py
"""
import asyncio
import datetime
import http.cookiejar
import json
import logging
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from _clock import WallClock  # noqa: E402
from _optional import need  # noqa: E402

from app import clock, runtime  # noqa: E402
from app.web import certinfo, selfsigned  # noqa: E402
from app.web.auth import hash_password  # noqa: E402


class EmptyPath:
    """PATH pointing at an empty directory: no openssl (or anything else) to find."""

    def __enter__(self):
        self.saved, self.dir = os.environ.get("PATH", ""), tempfile.mkdtemp()
        os.environ["PATH"] = self.dir
        return self

    def __exit__(self, *exc):
        os.environ["PATH"] = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)


def verified_handshake(cert, key, server_name):
    """TLS server with cert/key; a client trusting only `cert` must verify it."""
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(cert, key)
    cli_ctx = ssl.create_default_context(cafile=cert)
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    errors = []

    def serve():
        conn, _ = lsock.accept()
        try:
            with srv_ctx.wrap_socket(conn, server_side=True) as s:
                if s.recv(1) == b"x":
                    s.sendall(b"y")
        except (OSError, ssl.SSLError) as e:
            errors.append(e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with socket.create_connection(lsock.getsockname(), timeout=5) as raw:
            with cli_ctx.wrap_socket(raw, server_hostname=server_name) as s:
                s.sendall(b"x")
                # wait for the reply: closing with the server's TLS 1.3 session tickets
                # still unread makes Windows reset the connection under the server
                assert s.recv(1) == b"y", "no reply over the verified connection"
    finally:
        t.join(5)
        lsock.close()
    assert not errors, f"server side failed: {errors}"


def test_cryptography_without_openssl():
    with tempfile.TemporaryDirectory() as d, EmptyPath():
        cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        assert selfsigned.generate(cert, key) == "cryptography"
        for name in ("localhost", "127.0.0.1"):
            verified_handshake(cert, key, name)
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(key).st_mode) == 0o600, "private key must be 0600"
        from cryptography import x509
        with open(cert, "rb") as fh:
            c = x509.load_pem_x509_certificate(fh.read())
        lifetime = c.not_valid_after_utc - c.not_valid_before_utc
        assert lifetime == datetime.timedelta(days=825), lifetime   # Apple's limit: not 826
        made = datetime.datetime.now(datetime.timezone.utc)
        assert c.not_valid_before_utc <= made - datetime.timedelta(hours=23), \
            "valid from a day before it was made (clients a little behind accept it)"
        # regenerate over an existing pair (the button may be pressed twice)
        assert selfsigned.generate(cert, key) == "cryptography"
        verified_handshake(cert, key, "localhost")
        assert not [f for f in os.listdir(d) if f.endswith(".tmp")], "temp files left behind"
    print("no openssl on PATH: certificate made in-process, verified TLS handshake  OK")


def test_fallbacks_without_cryptography():
    saved = {k: v for k, v in sys.modules.items() if k == "cryptography" or k.startswith("cryptography.")}
    for k in saved:
        del sys.modules[k]
    sys.modules["cryptography"] = None  # makes `import cryptography...` raise ImportError
    try:
        with tempfile.TemporaryDirectory() as d:
            cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
            if shutil.which("openssl"):
                assert selfsigned.generate(cert, key) == "openssl"
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
                print("no cryptography: falls back to the openssl command  OK")
            with EmptyPath():
                try:
                    selfsigned.generate(cert + "2", key + "2")
                except selfsigned.CertError as e:
                    assert "cryptography" in str(e) and "openssl" in str(e), e
                else:
                    raise AssertionError("expected CertError with neither available")
        print("neither cryptography nor openssl: clear error, no crash  OK")
    finally:
        del sys.modules["cryptography"]
        sys.modules.update(saved)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def test_web_button_with_empty_path():
    tmp = tempfile.mkdtemp(prefix="ser2net_tlsgen_")
    ui = free_port()
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": ui},
                   "users": [{"username": "admin", "password_hash": hash_password("tlsgen-pass-1"),
                              "role": "admin", "pwd_version": 1}]}, fh)
    empty = tempfile.mkdtemp()
    env = dict(os.environ, PATH=empty)
    log = open(os.path.join(tmp, "server.log"), "wb")  # never an unread pipe
    srv = subprocess.Popen([sys.executable, os.path.abspath(os.path.join(ROOT, "ser2net.py")),
                            "--no-bootstrap", "--config", cfg_path],
                           cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                           stderr=subprocess.STDOUT)
    try:
        assert wait_port(ui), "server did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        base = f"http://127.0.0.1:{ui}"

        def csrf():
            return next(c.value for c in jar if c.name == "ser2net_csrf")

        def post(path, fields):
            req = urllib.request.Request(base + path, method="POST",
                                         data=urllib.parse.urlencode(fields).encode())
            return op.open(req, timeout=30).read().decode()

        op.open(base + "/login", timeout=10)
        post("/login", {"username": "admin", "password": "tlsgen-pass-1", "_csrf": csrf()})
        body = post("/settings/tls/generate", {"_csrf": csrf()})
        assert "Self-signed certificate generated" in body, body[body.find("banner"):][:300]
        with open(cfg_path, encoding="utf-8") as fh:
            admin = json.load(fh)["admin_ui"]
        assert admin["tls_cert"].endswith("cert.pem") and admin["tls_key"].endswith("key.pem"), admin
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(admin["tls_cert"], admin["tls_key"])
        print("server with an empty PATH: Settings -> Generate self-signed certificate works  OK")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)


def make_cert(d, name, start, days, issuer=None):
    """A certificate for `name` valid from `start` for `days` days, issued by itself or by
    `issuer` (a make_cert result): (cert path, key path, cert, key)."""
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    by, by_key = (issuer[2].subject, issuer[3]) if issuer else (subject, key)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(by)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(name), x509.IPAddress(ipaddress.ip_address("10.0.0.5"))]),
                critical=False)
            .sign(by_key, hashes.SHA256()))
    cpath, kpath = os.path.join(d, name + ".pem"), os.path.join(d, name + ".key")
    with open(cpath, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kpath, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return cpath, kpath, cert, key


class NoCryptography:
    """`import cryptography` fails while it is in effect (as without the package)."""

    def __enter__(self):
        self.saved = {k: v for k, v in sys.modules.items()
                      if k == "cryptography" or k.startswith("cryptography.")}
        for k in self.saved:
            del sys.modules[k]
        sys.modules["cryptography"] = None
        return self

    def __exit__(self, *exc):
        del sys.modules["cryptography"]
        sys.modules.update(self.saved)


def test_certificate_info():
    now = datetime.datetime.now(datetime.timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        selfsigned.generate(cert, key)
        info = certinfo.read(cert)
        assert info.subject == "ser2net" and info.self_signed, info
        assert {"localhost", "127.0.0.1", "::1"} <= set(info.names), info.names
        assert info.state() == "valid" and 823 <= info.days_left() <= 824, info
        with NoCryptography():
            assert certinfo.read(cert) == info, "the ssl module's decoder reads it the same"

        ca = make_cert(d, "Plant CA", now - datetime.timedelta(days=10), 3650)
        leaf = make_cert(d, "gateway.plant", now - datetime.timedelta(days=1), 397, issuer=ca)
        cases = {"gateway.plant": "valid",
                 "old": ("expired", now - datetime.timedelta(days=900), 825),
                 "future": ("not_yet_valid", now + datetime.timedelta(days=2), 30),
                 "soon": ("expiring", now - datetime.timedelta(days=800), 810)}
        paths = {"gateway.plant": leaf[0]}
        for name, case in cases.items():
            if isinstance(case, tuple):
                paths[name] = make_cert(d, name, case[1], case[2])[0]
        for name, path in paths.items():
            got = certinfo.read(path)
            want = cases[name] if isinstance(cases[name], str) else cases[name][0]
            assert got.state() == want and got.subject == name, (name, got)
            assert got.names == [name, "10.0.0.5"], got.names
            with NoCryptography():
                assert certinfo.read(path) == got, name
        assert not certinfo.read(leaf[0]).self_signed, "issued by the CA"
        assert certinfo.read(paths["old"]).days_left() < 0
        assert 9 <= certinfo.read(paths["soon"]).days_left() <= 10
        print("certinfo: subject, names, validity, self-signed or CA-issued; expired / not "
              "valid yet / ending soon - with cryptography and with the ssl module  OK")

        junk = os.path.join(d, "junk.pem")
        with open(junk, "w") as fh:
            fh.write("-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n")
        for path, code in ((key, "not_pem"), (junk, "not_pem"),
                           (os.path.join(d, "missing.pem"), "unreadable")):
            for hidden in (False, True):
                try:
                    if hidden:
                        with NoCryptography():
                            certinfo.read(path)
                    else:
                        certinfo.read(path)
                except certinfo.CertReadError as e:
                    assert e.code == code, (path, e.code)
                else:
                    raise AssertionError(f"read {path}")
        real_ssl = certinfo.ssl
        certinfo.ssl = types.SimpleNamespace(SSLError=ssl.SSLError)   # no decoder in it
        try:
            with NoCryptography():
                try:
                    certinfo.read(cert)
                except certinfo.CertReadError as e:
                    assert e.code == "no_decoder", e.code
                else:
                    raise AssertionError("read without any decoder")
                assert runtime.tls_certificate_note(cert) == "", "nothing to say without one"
        finally:
            certinfo.ssl = real_ssl
        print("certinfo: a key, a broken PEM block and a missing file are told apart  OK")

        line = runtime.tls_certificate_note(cert)
        assert line.startswith("admin UI TLS certificate: ser2net, self-signed, valid ") and \
            "day(s) left" in line, line
        line = runtime.tls_certificate_note(paths["old"])
        assert line.startswith("WARNING: admin UI TLS certificate: old, self-signed, valid ") \
            and line.endswith(": EXPIRED"), line
        assert "NOT YET VALID" in runtime.tls_certificate_note(paths["future"])
        assert runtime.tls_certificate_note(paths["soon"]).startswith("WARNING: ")
        assert runtime.tls_certificate_note(key).endswith(": no PEM certificate in it")
        print("start-up log: the certificate's subject and validity, a WARNING when it has "
              "ended, isn't valid yet or ends soon  OK")


CSRF = "tls-info-csrf-token"


async def _call(app, method, path, cookie, body=b""):
    headers = [(b"host", b"127.0.0.1:8080"), (b"cookie", cookie.encode()),
               (b"content-type", b"application/x-www-form-urlencoded"),
               (b"content-length", str(len(body)).encode())]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8080)}
    out = {"status": 0, "body": b""}
    sent = asyncio.Event()

    async def receive():
        if not sent.is_set():
            sent.set()
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(app(scope, receive, send), 60)
    return out["status"], out["body"].decode("utf-8", "replace")


def test_settings_shows_certificate():
    from app.config import AppConfig, ConfigStore
    from app.state import AppState
    from app.web import auth
    from app.web.routes import settings as settings_routes
    from app.web.server import build_app

    now = datetime.datetime.now(datetime.timezone.utc)
    tmp = tempfile.mkdtemp(prefix="ser2net_certinfo_")
    try:
        old = make_cert(tmp, "old", now - datetime.timedelta(days=900), 825)
        store = ConfigStore(os.path.join(tmp, "config.json"))
        cfg = AppConfig.from_dict({
            "users": [{"username": "admin", "password_hash": hash_password("certinfo-pass-1"),
                       "role": "admin", "pwd_version": 1}],
            "admin_ui": {"tls_cert": old[0], "tls_key": old[1]}})
        store.save(cfg, validate=False)
        state = AppState(store, cfg)
        app = build_app(state)
        session = auth.issue_session(cfg.secret_key, 3600, "admin", 1)
        cookie = f"{auth.CSRF_COOKIE}={CSRF}; {auth.SESSION_COOKIE}={session}"

        def settings_page():
            status, page = asyncio.run(_call(app, "GET", "/settings", cookie))
            assert status == 200, (status, page[:300])
            return page[page.find("Admin TLS (HTTPS)"):][:2500].replace("<wbr>", "")

        page = settings_page()
        assert 'Certificate: <span class="mono">old</span>' in page, page
        assert 'tag tag-danger">expired<' in page and "the certificate ended at" in page, page
        assert "Valid from " in page and "names: old, 10.0.0.5" in page, page
        cfg.admin_ui.tls_cert = os.path.join(tmp, "nowhere.pem")
        assert "The certificate file can’t be read" in settings_page()
        cfg.admin_ui.tls_cert = old[1]
        assert "holds no PEM certificate" in settings_page()
        print("Settings -> Admin TLS: the certificate's subject, names and validity, flagged "
              "when it has ended; a file that isn't one is named  OK")

        # generating while the clock reads days before the last change to config.json: done,
        # with a warning that the certificate is dated from that clock
        form = urllib.parse.urlencode({"_csrf": CSRF}).encode()
        with WallClock.patch(clock, settings_routes, shift=-3 * 86400):
            status, page = asyncio.run(_call(app, "POST", "/settings/tls/generate", cookie, form))
        assert status == 200 and "Self-signed certificate generated" in page, page[:400]
        assert "which can’t be right" in page, page[page.find("banner"):][:600]
        assert certinfo.read(cfg.admin_ui.tls_cert).subject == "ser2net"
        page = settings_page()
        assert "self-signed</span>" in page and "day(s) left" in page, page
        status, page = asyncio.run(_call(app, "POST", "/settings/tls/generate", cookie, form))
        assert "Self-signed certificate generated" in page and "can’t be right" not in page
        print("Settings: generating while the clock reads earlier than it can warns (and "
              "generates); with a right clock it doesn't  OK")
    finally:
        for name in list(logging.root.manager.loggerDict):
            if name == "ser2net" or name.startswith("ser2net."):
                for h in list(logging.getLogger(name).handlers):
                    h.close()
                    logging.getLogger(name).removeHandler(h)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    have = need("cryptography", what="certificates made in-process (parts 1, 3 and 4)")
    if have:
        test_cryptography_without_openssl()
    test_fallbacks_without_cryptography()
    if have:
        test_web_button_with_empty_path()
        test_certificate_info()
        test_settings_shows_certificate()
    print("\nPASS: self-signed certificate without an external openssl; certificate validity "
          "shown")


if __name__ == "__main__":
    main()
