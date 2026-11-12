"""Admin UI TLS changes apply to new connections while ser2net runs, and what the UI really
serves decides the cookies' Secure flag, HSTS, the cluster beacon's scheme and the Settings
page's "Listening on" (a saved change that waits for the next start doesn't).

  * Settings -> Admin TLS: a pair that can't be used is refused (config.json unchanged, the
    UI goes on as it was); a usable pair is served to the next connection, plain HTTP is
    not; the same paths saved again with renewed files serve the new certificate; a key
    with a passphrase is refused without its password (tls_key_password: never asked for
    on a console) and served at once with it; blank fields switch TLS off again; Generate
    serves its certificate at once;
  * cookies are Secure (and HSTS is sent) exactly while the UI serves TLS;
  * the cluster beacon advertises the served port and scheme, and the Settings page's
    address hint fits the installation (environment, Windows service, console).

Run: python3 tests/test_tls_live.py   (the certificates need cryptography)
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
import tempfile
import urllib.request

from _optional import need
from _server import Browser, Server, _NoRedirect

from ser2netweb import console
from ser2netweb.config import AppConfig
from ser2netweb.engine.cluster import ClusterDiscovery
from ser2netweb.web.auth import hash_password

PW = "tls-live-pass-1"


def _no_verify() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def https_browser(port: int, jar=None) -> Browser:
    b = Browser(f"https://127.0.0.1:{port}")
    if jar is not None:
        b.jar = jar
    b.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(b.jar), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=_no_verify()))
    return b


def served_cert(port: int) -> bytes | None:
    """The certificate (DER) a new connection to the UI gets; None: it speaks no TLS."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, \
                _no_verify().wrap_socket(raw) as tls:
            return tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None


def plain_http(port: int) -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return r.read() == b"ok"
    except OSError:
        return False


def der(path: str) -> bytes:
    with open(path, encoding="ascii") as fh:
        return ssl.PEM_cert_to_DER_cert(fh.read())


def set_cookie_lines(headers) -> list[str]:
    return headers.get_all("Set-Cookie") or []


def test_beacon_and_hints() -> None:
    cfg = AppConfig()
    cfg.admin_ui.bind_ip, cfg.admin_ui.port = "10.1.2.3", 8443
    cfg.admin_ui.tls_cert, cfg.admin_ui.tls_key = "c.pem", "k.pem"
    disco = ClusterDiscovery(cfg, lambda _m: None)
    assert disco.advertised() == ("10.1.2.3", 8443, "https"), "without a listener: config"
    disco.serving = lambda: ("10.1.2.3", 8080, False)  # saved, not served yet
    assert disco.advertised() == ("10.1.2.3", 8080, "http"), disco.advertised()

    assert console.reconfigure_hint("/data", environ={"SER2NET_BIND_IP": "0.0.0.0"}) == ("env", "")
    kind, cmd = console.reconfigure_hint("/opt/s2n data", environ={})
    assert kind == "console" and cmd.endswith("--reconfigure --data-dir " + (
        '"/opt/s2n data"' if os.name == "nt" else "'/opt/s2n data'")), cmd
    kind, cmd = console.reconfigure_hint("/x", "ser2net", environ={})
    assert kind == "service" and cmd.endswith("--install-service --reconfigure"), cmd
    kind, cmd = console.reconfigure_hint("/x", "s2n-b", environ={})
    assert cmd.endswith("--install-service --reconfigure --service-name s2n-b"), cmd
    print("beacon: the served port and scheme; address hint per installation  OK")


def test_live_tls() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    from _pki import TestCA

    tmp = tempfile.mkdtemp(prefix="s2n_tlslive_")
    try:
        ca = TestCA(tmp)
        cert_a, key_a = ca.issue(tmp, "a", ["127.0.0.1"])
        cert_b, key_b = ca.issue(tmp, "b", ["127.0.0.1"])
        cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW),
                          "role": "admin", "pwd_version": 1}]}
        with Server(cfg) as srv:
            port = srv.port
            b = Browser(srv.base)
            b.login("admin", PW)

            # a pair that doesn't belong together: refused, nothing changes
            st, _, page = b.post("/settings/tls", {"tls_cert": cert_a, "tls_key": key_b})
            assert "The TLS certificate and key could not be loaded" in page, page[-1500:]
            assert srv.saved_config()["admin_ui"]["tls_cert"] == ""
            assert plain_http(port) and served_cert(port) is None
            st, h, page = b.get("/settings")
            assert not any("Secure" in c for c in set_cookie_lines(h))
            print("a mismatched pair: refused, config.json and the plain UI as they were  OK")

            # a usable pair: served to the next connection, at once
            st, _, page = b.post("/settings/tls", {"tls_cert": cert_a, "tls_key": key_a})
            assert f"use HTTPS now: continue at https://127.0.0.1:{port}/settings" in page, \
                page[page.find("banner"):][:500]
            assert srv.saved_config()["admin_ui"]["tls_cert"] == cert_a
            assert served_cert(port) == der(cert_a), "the new connection must get the certificate"
            assert not plain_http(port), "plain HTTP must not be served next to TLS"
            assert "admin UI: TLS certificate loaded, new connections use it (TLS is on now)" \
                in srv.output()
            s = https_browser(port)
            st, h, _ = s.get("/login")
            cookies = set_cookie_lines(h)
            assert cookies and all("Secure" in c for c in cookies), cookies
            assert h.get("Strict-Transport-Security"), "HSTS while TLS is served"
            s.login("admin", PW)
            st, _, page = s.get("/settings")
            assert f'<span class="mono">127.0.0.1:{port}</span>' in page and \
                '<span class="tag">TLS</span>' in page
            assert "used from the next start" not in page
            print("a usable pair: the next connection gets it, plain HTTP is gone, cookies "
                  "Secure, HSTS  OK")

            # renewed files at the same paths, saved again: the new certificate is served
            shutil.copyfile(cert_b, cert_a)
            shutil.copyfile(key_b, key_a)
            st, _, page = s.post("/settings/tls", {"tls_cert": cert_a, "tls_key": key_a})
            assert "use HTTPS now" in page, page[-800:]
            assert served_cert(port) == der(cert_b)
            print("renewed files saved again: new connections get the new certificate  OK")

            # a key with a passphrase: refused without its password (nothing changes), served
            # at once with it
            with open(key_a, "rb") as fh:
                key = load_pem_private_key(fh.read(), None)
            locked = os.path.join(tmp, "locked.key")
            with open(locked, "wb") as fh:
                fh.write(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(b"secret-1")))
            st, _, page = s.post("/settings/tls", {"tls_cert": cert_a, "tls_key": locked})
            assert st == 400 and "password-protected" in page, (st, page[-800:])
            assert srv.saved_config()["admin_ui"]["tls_key"] == key_a
            assert served_cert(port) == der(cert_b), "the certificate in use stays"
            st, _, page = s.post("/settings/tls", {"tls_cert": cert_a, "tls_key": locked,
                                                   "tls_key_password": "secret-1"})
            assert "use HTTPS now" in page and "secret-1" not in page, page[-800:]
            assert srv.saved_config()["admin_ui"]["tls_key"] == locked
            assert served_cert(port) == der(cert_b)
            print("a key with a passphrase: refused without its password, served at once with "
                  "it  OK")

            # blank fields: TLS off for new connections; cookies no longer Secure
            st, _, page = s.post("/settings/tls", {"tls_cert": "", "tls_key": ""})
            assert f"use plain HTTP now: continue at http://127.0.0.1:{port}/settings" in page, \
                page[-800:]
            assert plain_http(port) and served_cert(port) is None
            assert "admin UI: TLS is off now, new connections use plain HTTP" in srv.output()
            b = Browser(srv.base)
            st, h, _ = b.get("/login")
            assert not any("Secure" in c for c in set_cookie_lines(h))
            assert not h.get("Strict-Transport-Security")
            b.login("admin", PW)

            # a refused change (the stored key password removed) leaves plain HTTP served, and
            # the cookies follow what is served
            st, _, page = b.post("/settings/tls", {"tls_cert": cert_a, "tls_key": locked,
                                                   "tls_key_password_clear": "on"})
            assert st == 400 and "password-protected" in page and plain_http(port), st
            st, h, page = Browser(srv.base).get("/login")
            assert set_cookie_lines(h) and not any("Secure" in c for c in set_cookie_lines(h)), \
                "no Secure cookie on the plain HTTP the UI still serves"
            st, _, page = b.get("/settings")
            assert '<span class="tag">HTTP</span>' in page
            assert "used from the next start" not in page
            print("TLS off: plain HTTP at once; a refused change keeps the cookies for what is "
                  "served  OK")

            # Generate: the self-signed certificate is served at once
            st, _, page = b.post("/settings/tls/generate", {})
            assert "Self-signed certificate generated" in page and "use HTTPS now" in page, \
                page[-800:]
            admin = srv.saved_config()["admin_ui"]
            assert served_cert(port) == der(admin["tls_cert"]) and not plain_http(port)
            s = https_browser(port)
            s.login("admin", PW)
            st, _, page = s.get("/settings")
            assert "used from the next start" not in page
            print("Generate: its certificate is served at once  OK")
            log = srv.output()
        assert "Traceback" not in log, log[-3000:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    test_beacon_and_hints()
    if need("cryptography", what="the admin TLS certificates (made in-process)"):
        test_live_tls()
    print("\nPASS: admin TLS applies without a restart")


if __name__ == "__main__":
    main()
