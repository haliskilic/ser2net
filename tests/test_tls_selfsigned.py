"""Settings → "Generate self-signed certificate" must not need an external program (#26).

It used to run the `openssl` command, which Windows doesn't have (nor do many
minimal Linux installs), so the button could only fail there. The certificate is
now made in-process with the `cryptography` package — bundled in the standalone
executables — and the openssl command is only a fallback for source installs.

  1. with cryptography and an EMPTY PATH: a TLS server loads the certificate and
     key, and a client that trusts it completes a verified handshake to
     "localhost" and 127.0.0.1 (subject alternative names); key file is 0600 (POSIX)
  2. without cryptography: falls back to the openssl command when one exists, and
     with neither there is a clear error instead of a crash
  3. the real server started with an EMPTY PATH: POST /settings/tls/generate saves
     the pair and points admin_ui.tls_cert / tls_key at it
Parts 1 and 3 need cryptography (pip install cryptography) and are skipped without.

Run: python3 tests/test_tls_selfsigned.py
"""
import http.cookiejar
import json
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
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from app.web import selfsigned  # noqa: E402
from app.web.auth import hash_password  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False


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
                s.recv(1)
        except (OSError, ssl.SSLError) as e:
            errors.append(e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with socket.create_connection(lsock.getsockname(), timeout=5) as raw:
            with cli_ctx.wrap_socket(raw, server_hostname=server_name) as s:
                s.sendall(b"x")
    finally:
        t.join(5)
        lsock.close()
    assert not errors, f"server side failed: {errors}"


def test_cryptography_without_openssl():
    if not HAVE_CRYPTOGRAPHY:
        print("cryptography not installed: in-process generation  SKIP")
        return
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
        days = (c.not_valid_after_utc - c.not_valid_before_utc).days
        assert 824 <= days <= 826, days
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
    if not HAVE_CRYPTOGRAPHY:
        print("cryptography not installed: web button with an empty PATH  SKIP")
        return
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
        print("server with an empty PATH: Settings → Generate self-signed certificate works  OK")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)


def main():
    test_cryptography_without_openssl()
    test_fallbacks_without_cryptography()
    test_web_button_with_empty_path()
    print("\nPASS: self-signed certificate without an external openssl")


if __name__ == "__main__":
    main()
