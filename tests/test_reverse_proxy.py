"""The web UI behind a reverse proxy, end to end against a running server:

  * the console and hex-view WebSockets take the same rule as the UI's forms
    (auth.cross_site): accepted when the browser says same-origin (Sec-Fetch-Site), or
    when its Origin is one of the hosts the request claims - Host, X-Forwarded-Host,
    Forwarded host= - port included (default ports normalised), so a proxy that
    rewrites Host but passes the browser's address on works; refused for another port,
    another host, Origin null, Sec-Fetch-Site cross-site / same-site; a session is
    still needed; a refusal is logged (at most once a minute) with what to set on the
    proxy;
  * `also`: an address the UI is published under is accepted as an Origin too;
  * the SSO redirect URI: with oidc.redirect_uri blank it is made from the request as
    it has been since v2.3 (X-Forwarded-Proto or the scheme, X-Forwarded-Host or Host;
    never Forwarded or X-Forwarded-Port, which would change what working installs
    registered at their provider); every sign-in start logs it; Settings -> Single
    sign-on shows the value for its own request (or the configured one), warns about an
    http:// one for a host other than loopback, and gives the browser what it needs to
    show the value for its own address when a proxy changed it.

Run: python3 tests/test_reverse_proxy.py
"""
import html
import http.server
import json
import socket
import threading
import time
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port

from app.config import OidcSettings
from app.web import auth, oidc_auth
from app.web.auth import hash_password

PW = "proxy-pass-1"
MID = "m1"


def ws_handshake(port: int, path: str, headers: dict) -> int:
    """The HTTP status the server answers a WebSocket handshake with (101 accepted),
    every header as given - Host included, which a WebSocket library would set itself."""
    lines = [f"GET {path} HTTP/1.1", "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        data = b""
        while b"\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return int(data.split(b" ", 2)[1])


def test_matcher() -> None:
    def headers(**h):
        return {k.replace("_", "-").lower(): v for k, v in h.items()}

    # the hosts the request claims, port included, default ports normalised
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="s2n.example.com")) == ""
    assert auth.cross_site(headers(origin="https://s2n.example.com",
                                   host="s2n.example.com:443")) == ""
    assert auth.cross_site(headers(origin="http://[::1]:8080", host="[::1]:8080")) == ""
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="ser2net:8080",
                                   x_forwarded_host="s2n.example.com")) == ""
    assert auth.cross_site(headers(origin="https://gw.example.com:8443", host="127.0.0.1:8080",
                                   forwarded='for=10.0.0.9;host="gw.example.com:8443"')) == ""
    assert auth.cross_site(headers(origin="https://gw.example.com:8443", host="gw.example.com"))
    # the browser's word: same-origin (also behind a proxy that rewrites Host) / none
    assert auth.cross_site(headers(sec_fetch_site="same-origin", origin="https://a.example",
                                   host="ser2net:8080")) == ""
    assert auth.cross_site(headers(sec_fetch_site="cross-site", origin="http://h:1", host="h:1"))
    assert auth.cross_site(headers(sec_fetch_site="same-site", origin="http://h:1", host="h:1"))
    # an address the UI is published under (also=): that origin only
    published = ("https://s2n.example.com",)
    rewritten = headers(origin="https://s2n.example.com", host="127.0.0.1:8080")
    assert auth.cross_site(rewritten)
    assert auth.cross_site(rewritten, also=published) == ""
    assert auth.cross_site(headers(origin="https://s2n.example.com:443", host="127.0.0.1:8080"),
                           also=published) == ""
    for other in ("http://s2n.example.com", "https://s2n.example.com:8443",
                  "https://evil.example", "null"):
        assert auth.cross_site(headers(origin=other, host="127.0.0.1:8080"), also=published), other
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="127.0.0.1:8080"),
                           also=("not an origin", "https://s2n.example.com/path")), "a path"
    print("origin check: claimed hosts with ports, Sec-Fetch-Site, published origins  OK")


def test_websockets(srv) -> None:
    b = Browser(srv.base)
    b.login("admin", PW)
    session = b.cookie("ser2net_session").value
    own = f"127.0.0.1:{srv.port}"

    def status(path, **h):
        hdr = {"Host": own, "Cookie": f"ser2net_session={session}"}
        hdr.update({k.replace("_", "-"): v for k, v in h.items() if v is not None})
        return ws_handshake(srv.port, path, hdr)

    for kind in ("console", "traffic"):
        path = f"/api/mappings/{MID}/{kind}"
        accepted = (
            ("same origin", {"Origin": "http://" + own}),
            ("no Origin (a script)", {}),
            ("browser says same-origin, Host rewritten",
             {"Host": "ser2net:8080", "Origin": "https://s2n.example.com",
              "Sec-Fetch-Site": "same-origin"}),
            ("Host rewritten, X-Forwarded-Host",
             {"Host": "ser2net:8080", "Origin": "https://s2n.example.com",
              "X-Forwarded-Host": "s2n.example.com", "X-Forwarded-Proto": "https"}),
            ("Host rewritten, Forwarded host=",
             {"Host": "127.0.0.1:8080", "Origin": "https://gw.example.com:8443",
              "Forwarded": "for=10.0.0.9;host=gw.example.com:8443;proto=https"}),
            ("Host with the default port", {"Host": "s2n.example.com:443",
                                            "Origin": "https://s2n.example.com"}),
        )
        for name, h in accepted:
            assert status(path, **h) == 101, (kind, name)
        refused = (
            ("another port", {"Origin": f"http://127.0.0.1:{srv.port + 1}"}),
            ("another host", {"Origin": "http://attacker.example"}),
            ("Origin null", {"Origin": "null"}),
            ("Host rewritten, no X-Forwarded-Host", {"Host": "ser2net:8080",
                                                    "Origin": "https://s2n.example.com"}),
            ("X-Forwarded-Host without the public port",
             {"Host": "ser2net:8080", "Origin": "https://gw.example.com:8443",
              "X-Forwarded-Host": "gw.example.com"}),
            ("Sec-Fetch-Site cross-site", {"Origin": "http://" + own,
                                           "Sec-Fetch-Site": "cross-site"}),
            ("Sec-Fetch-Site same-site", {"Origin": "http://" + own,
                                          "Sec-Fetch-Site": "same-site"}),
        )
        for name, h in refused:
            assert status(path, **h) == 403, (kind, name)
        # a matching Origin doesn't stand in for the session
        assert ws_handshake(srv.port, path, {"Host": own, "Origin": "http://" + own}) == 403
    time.sleep(0.3)
    lines = [ln for ln in srv.output().splitlines() if "opened by a page of another site" in ln]
    assert len(lines) == 1, lines   # at most once a minute
    assert (f"refused WebSocket /api/mappings/{MID}/console from 127.0.0.1" in lines[0]
            and f"Origin 'http://127.0.0.1:{srv.port + 1}'" in lines[0]
            and "proxy_set_header Host $http_host" in lines[0]
            and "ProxyPreserveHost On" in lines[0] and "X-Forwarded-Host" in lines[0]), lines[0]
    print("WebSockets: same origin, a proxy passing the browser's address (X-Forwarded-Host, "
          "Forwarded, Sec-Fetch-Site same-origin) accepted; other sites refused, logged once "
          "with the proxy settings  OK")


def fake_request(headers: dict, scheme: str = "http"):
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": "/auth/oidc/login",
                    "scheme": scheme, "server": ("10.0.0.5", 8080), "query_string": b"",
                    "headers": [(k.lower().encode("latin-1"), v.encode("latin-1"))
                                for k, v in headers.items()]})


# (request headers, request scheme) -> the redirect URI with oidc.redirect_uri blank: the
# derivation of every version since 2.3, which installs have registered at their provider
DERIVED = (
    ({"Host": "127.0.0.1:8080"}, "http", "http://127.0.0.1:8080/auth/oidc/callback"),
    ({}, "http", "http://10.0.0.5:8080/auth/oidc/callback"),
    ({"Host": "s2n.example.com"}, "https", "https://s2n.example.com/auth/oidc/callback"),
    ({"Host": "ser2net:8080", "X-Forwarded-Proto": "https",
      "X-Forwarded-Host": "s2n.example.com"}, "http",
     "https://s2n.example.com/auth/oidc/callback"),
    ({"Host": "s2n.example.com:8443", "X-Forwarded-Proto": "https"}, "http",
     "https://s2n.example.com:8443/auth/oidc/callback"),
    # not read: X-Forwarded-Port, Forwarded
    ({"Host": "gw.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-Port": "8443"},
     "http", "https://gw.example.com/auth/oidc/callback"),
    ({"Host": "127.0.0.1:8080", "Forwarded": "for=10.0.0.9;host=gw.example.com;proto=https"},
     "http", "http://127.0.0.1:8080/auth/oidc/callback"),
)


def test_redirect_uri() -> None:
    blank = OidcSettings(enabled=True)
    for headers, scheme, want in DERIVED:
        assert oidc_auth.redirect_uri(blank, fake_request(headers, scheme)) == want, headers
    pinned = OidcSettings(enabled=True, redirect_uri="  https://sso.example.com/auth/oidc/callback ")
    assert oidc_auth.redirect_uri(pinned, fake_request({"Host": "127.0.0.1:8080"})) == \
        "https://sso.example.com/auth/oidc/callback"
    for uri, plain in (("http://s2n.example.com/auth/oidc/callback", True),
                       ("http://192.168.1.20:8080/auth/oidc/callback", True),
                       ("HTTP://s2n.example.com/x", True),
                       ("http://127.0.0.1:8080/auth/oidc/callback", False),
                       ("http://localhost:8080/auth/oidc/callback", False),
                       ("http://[::1]:8080/auth/oidc/callback", False),
                       ("https://s2n.example.com/auth/oidc/callback", False),
                       ("http://[broken/x", False), ("", False)):
        assert oidc_auth.redirect_uri_is_plain_http(uri) is plain, uri
    print("SSO redirect URI: the configured one, else made from the request as before "
          "(no Forwarded / X-Forwarded-Port); http:// for a host other than loopback  OK")


class _Discovery(http.server.BaseHTTPRequestHandler):
    """An identity provider's discovery document (loopback http:// is allowed)."""

    def do_GET(self):  # noqa: N802 (http.server API)
        iss = f"http://127.0.0.1:{self.server.server_address[1]}"
        body = json.dumps({"issuer": iss, "authorization_endpoint": iss + "/authorize",
                           "token_endpoint": iss + "/token", "jwks_uri": iss + "/jwks"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_sso_redirect(srv, issuer: str) -> None:
    b = Browser(srv.base)
    b.login("admin", PW)
    own = f"127.0.0.1:{srv.port}"

    def sign_in_redirect(headers) -> str:
        st, h, _ = b._open(urllib.request.Request(srv.base + "/auth/oidc/login", headers=headers))
        location = h.get("Location") or ""
        assert st == 303 and location.startswith(issuer + "/authorize?"), (st, location)
        return urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["redirect_uri"][0]

    # what a sign-in sends: pinned end to end (the request's scheme is the server's: http)
    for headers, _scheme, want in DERIVED[3:]:
        assert sign_in_redirect(headers) == want, headers
    assert sign_in_redirect({}) == f"http://{own}/auth/oidc/callback"
    log = srv.output()
    assert (f"OIDC login init from 127.0.0.1: redirect URI 'http://{own}/auth/oidc/callback' "
            "(made from the request; Settings → Single sign-on (OIDC) shows it)") in log, log[-2000:]
    assert "redirect URI 'https://s2n.example.com/auth/oidc/callback'" in log

    def settings(headers=None) -> str:
        st, _, body = b._open(urllib.request.Request(srv.base + "/settings", headers=headers or {}))
        assert st == 200, st
        return body.decode()

    def shown(page) -> tuple[str, bool]:
        i = page.index('id="oidc-redirect"')
        tag = page[page.rindex("<p", 0, i):page.index(">", i)]
        uri = html.unescape(tag.split('data-uri="', 1)[1].split('"', 1)[0])
        return uri, " data-auto" in tag

    http_warning = "most identity providers accept http:// only for localhost"
    page = settings()
    assert shown(page) == (f"http://{own}/auth/oidc/callback", True), shown(page)
    assert f'<span class="mono">http://{own}/auth/oidc/callback</span>' in page
    assert 'id="oidc-redirect-mismatch" hidden' in page and "data-browser-uri" in page
    assert http_warning not in page and "&lt;this-url&gt;" not in page
    page = settings({"Host": "ser2net.lan:8080"})
    assert shown(page) == ("http://ser2net.lan:8080/auth/oidc/callback", True)
    assert http_warning in page
    page = settings({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "s2n.example.com"})
    assert shown(page) == ("https://s2n.example.com/auth/oidc/callback", True)
    assert http_warning not in page

    # a Redirect URI that is set is shown as it is (saved with SSO off: the form wants an
    # https:// issuer, this test's provider is loopback http://)
    fixed = "http://sso-only.example.com/auth/oidc/callback"
    st, _, page = b.post("/settings/oidc", {"oidc_issuer": issuer, "oidc_client_id": "ser2net",
                                            "oidc_redirect_uri": fixed})
    assert st == 200 and srv.saved_config()["oidc"]["redirect_uri"] == fixed, st
    assert shown(page) == (fixed, False) and http_warning not in page, shown(page)
    assert "as set below" in page
    print("SSO redirect URI end to end: what a sign-in sends and logs, what Settings shows "
          "(made from its request / as set), the http:// warning  OK")


def main():
    test_matcher()
    test_redirect_uri()
    provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Discovery)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    issuer = f"http://127.0.0.1:{provider.server_address[1]}"
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "oidc": {"enabled": True, "issuer": issuer, "client_id": "ser2net"},
           "mappings": [{"id": MID, "name": "proxied", "enabled": True,
                         "serial": {"port": "/dev/ser2net-proxy-test"},
                         "network": {"mode": "server", "protocol": "raw",
                                     "bind_ip": "127.0.0.1", "port": free_port()}}]}
    try:
        with Server(cfg) as srv:
            test_websockets(srv)
            test_sso_redirect(srv, issuer)
    finally:
        provider.shutdown()
        provider.server_close()
    print("\nPASS: reverse proxy")


if __name__ == "__main__":
    main()
