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
    it has been since v2.3 (X-Forwarded-Proto or the scheme, X-Forwarded-Host or Host,
    of a list the first value;
    never Forwarded or X-Forwarded-Port, which would change what working installs
    registered at their provider); every sign-in start logs it; Settings -> Single
    sign-on shows the value for its own request (or the configured one), warns about an
    http:// one for a host other than loopback, and gives the browser what it needs to
    show the value for its own address when a proxy changed it;
  * through a reverse proxy set up as docs/REVERSE-PROXY.md says (a small one in this
    process: the browser's Host passed on, X-Forwarded-For / -Proto added, WebSocket
    upgrades tunnelled, TLS ending at the proxy when cryptography is there), with the
    proxy trusted and a public URL with a path: signing in, pages, forms and the console
    under the path, Secure cookies of the path's own, the client's address in the logs;
    a proxy that strips the path (X-Forwarded-Prefix) works the same; '/' still works
    directly; the start-up log says what is published and trusted.

Run: python3 tests/test_reverse_proxy.py
"""
import html
import http.cookiejar
import http.server
import json
import os
import select
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

from _optional import need
from _server import Browser, Server, _NoRedirect, free_port, ui_cookie

from ser2netweb.config import OidcSettings
from ser2netweb.web import auth, oidc_auth
from ser2netweb.web.auth import hash_password

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
    assert auth.cross_site(headers(origin="https://s2n.example.com", host="ser2netweb:8080",
                                   x_forwarded_host="s2n.example.com")) == ""
    assert auth.cross_site(headers(origin="https://gw.example.com:8443", host="127.0.0.1:8080",
                                   forwarded='for=10.0.0.9;host="gw.example.com:8443"')) == ""
    assert auth.cross_site(headers(origin="https://gw.example.com:8443", host="gw.example.com"))
    # the browser's word: same-origin (also behind a proxy that rewrites Host) / none
    assert auth.cross_site(headers(sec_fetch_site="same-origin", origin="https://a.example",
                                   host="ser2netweb:8080")) == ""
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
        hdr = {"Host": own}
        hdr.update({k.replace("_", "-"): v for k, v in h.items() if v is not None})
        # the session cookie under its name for that Host (and scheme): what a browser that
        # always reaches the UI this way has
        scope = {"type": "websocket", "scheme": "ws", "path": path,
                 "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()]}
        hdr["Cookie"] = f"{auth.proxy.cookie_name(scope, 'ser2net_session')}={session}"
        return ws_handshake(srv.port, path, hdr)

    for kind in ("console", "traffic"):
        path = f"/api/mappings/{MID}/{kind}"
        accepted = (
            ("same origin", {"Origin": "http://" + own}),
            ("no Origin (a script)", {}),
            ("browser says same-origin, Host rewritten",
             {"Host": "ser2netweb:8080", "Origin": "https://s2n.example.com",
              "Sec-Fetch-Site": "same-origin"}),
            ("Host rewritten, X-Forwarded-Host",
             {"Host": "ser2netweb:8080", "Origin": "https://s2n.example.com",
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
            ("Host rewritten, no X-Forwarded-Host", {"Host": "ser2netweb:8080",
                                                    "Origin": "https://s2n.example.com"}),
            ("X-Forwarded-Host without the public port",
             {"Host": "ser2netweb:8080", "Origin": "https://gw.example.com:8443",
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
    ({"Host": "ser2netweb:8080", "X-Forwarded-Proto": "https",
      "X-Forwarded-Host": "s2n.example.com"}, "http",
     "https://s2n.example.com/auth/oidc/callback"),
    ({"Host": "s2n.example.com:8443", "X-Forwarded-Proto": "https"}, "http",
     "https://s2n.example.com:8443/auth/oidc/callback"),
    # proxies in a row: the first value (the whole list made no usable address)
    ({"Host": "ser2netweb:8080", "X-Forwarded-Proto": "https, http",
      "X-Forwarded-Host": "s2n.example.com, gw.internal:8080"}, "http",
     "https://s2n.example.com/auth/oidc/callback"),
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

    def do_GET(self):  # http.server API
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

    session = b.cookie("ser2net_session").value

    def settings(headers=None) -> str:
        headers = dict(headers or {})
        if headers:   # the session cookie under its name for that address (Host, scheme)
            scope = {"type": "http", "scheme": "http", "path": "/settings", "headers": [
                (k.lower().encode(), v.encode()) for k, v in {"Host": own, **headers}.items()]}
            headers["Cookie"] = f"{auth.proxy.cookie_name(scope, 'ser2net_session')}={session}"
        st, _, body = b._open(urllib.request.Request(srv.base + "/settings", headers=headers))
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


class Proxy:
    """A reverse proxy in front of the test server, set up like the nginx recipe of
    docs/REVERSE-PROXY.md: Host as the browser sent it, X-Forwarded-For with the address
    the proxy was reached from added (CLIENT stands in for it: everything here is
    127.0.0.1), X-Forwarded-Proto, WebSocket upgrades tunnelled, one request per
    connection otherwise. `strip`: the path prefix is cut off and sent as
    X-Forwarded-Prefix (Traefik's StripPrefix). `tls`: (cert, key) - TLS ends here."""

    CLIENT = "198.51.100.20"

    def __init__(self, upstream: int, prefix: str = "", strip: bool = False, tls=None):
        self.upstream, self.prefix, self.strip = upstream, prefix, strip
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.ctx = None
        if tls:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*tls)
        self.scheme = "https" if tls else "http"
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.sock.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        try:
            if self.ctx is not None:
                conn = self.ctx.wrap_socket(conn, server_side=True)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return conn.close()
                data += chunk
            head, rest = data.split(b"\r\n\r\n", 1)
            line, *fields = head.decode("latin-1").split("\r\n")
            method, target, version = line.split(" ")
            headers = [f.split(":", 1) for f in fields]
            names = {k.strip().lower() for k, _v in headers}
            upgrade = "upgrade" in names
            out = [(k, v.strip()) for k, v in headers
                   if k.strip().lower() not in ("x-forwarded-proto", "x-forwarded-prefix")
                   and (upgrade or k.strip().lower() != "connection")]
            xff = [v for k, v in out if k.lower() == "x-forwarded-for"]
            out = [(k, v) for k, v in out if k.lower() != "x-forwarded-for"]
            out.append(("X-Forwarded-For", ", ".join(xff + [self.CLIENT])))
            out.append(("X-Forwarded-Proto", self.scheme))
            if not upgrade:
                out.append(("Connection", "close"))
            if self.strip and (target == self.prefix or target.startswith(self.prefix + "/")):
                target = target[len(self.prefix):] or "/"
                out.append(("X-Forwarded-Prefix", self.prefix))
            up = socket.create_connection(("127.0.0.1", self.upstream), timeout=20)
            up.sendall((f"{method} {target} {version}\r\n"
                        + "".join(f"{k}: {v}\r\n" for k, v in out) + "\r\n").encode("latin-1")
                       + rest)
            self._pump(conn, up)
        except (OSError, ValueError):
            conn.close()

    @staticmethod
    def _pump(conn, up) -> None:
        """Both directions in one thread (a TLS socket can't be read and written from two
        threads at once), until either side closes."""
        other = {conn: up, up: conn}
        try:
            while True:
                ready = [conn] if getattr(conn, "pending", lambda: 0)() else \
                    select.select([conn, up], [], [], 30)[0]
                if not ready:
                    break
                for src in ready:
                    chunk = src.recv(65536)
                    if not chunk:
                        raise EOFError
                    other[src].sendall(chunk)
        except (OSError, EOFError):
            pass
        for s in (conn, up):
            try:
                s.close()
            except OSError:
                pass


def proxied_browser(base: str) -> Browser:
    b = Browser(base)
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # the test's own certificate
    b.jar = http.cookiejar.CookieJar()
    b.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(b.jar),
                                       urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    return b


def path_cookie(name: str, proxy: Proxy) -> str:
    """The name of the UI's cookie `name` under /ser2net through `proxy`: tagged by the
    proxy's port (the Host it passes on) and the path, __Secure- over https."""
    scope = {"root_path": "/ser2net", "path": "/", "scheme": proxy.scheme,
             "headers": [(b"host", f"127.0.0.1:{proxy.port}".encode())]}
    return auth.proxy.cookie_name(scope, name)


def test_through_proxy(srv, proxied: Proxy, stripping: Proxy) -> None:
    public = f"{proxied.scheme}://127.0.0.1:{proxied.port}"
    csrf_name = path_cookie("ser2net_csrf", proxied)
    session_name = path_cookie("ser2net_session", proxied)
    log = srv.output()
    assert (f"admin UI on http://127.0.0.1:{srv.port}, published as {public}/ser2net "
            "(admin_ui.public_url)") in log, log[:1500]
    assert "trusted proxies (admin_ui.trusted_proxies): 127.0.0.1 —" in log
    b = proxied_browser(public)
    st, h, page = b.get("/ser2net/login")
    assert st == 200 and 'action="/ser2net/login"' in page, st
    assert 'href="/ser2net/static/app.css' in page
    st, h, _ = b.post("/ser2net/login", {"username": "admin", "password": PW,
                                         "_csrf": b.cookie(csrf_name).value})
    assert st == 303 and h.get("Location") == "/ser2net/", (st, dict(h))
    secure = proxied.scheme == "https"
    assert csrf_name.startswith("__Secure-") == secure, csrf_name
    # (Browser.post also fetched /login, outside the path, for a CSRF cookie of its own)
    made = {c.name: (c.path, c.secure) for c in b.jar if c.path == "/ser2net"}
    assert made == {csrf_name: ("/ser2net", secure), session_name: ("/ser2net", secure)}, made
    st, _, page = b.get("/ser2net/settings")
    assert st == 200 and f'Published as <a class="mono" href="{public}/ser2net">' in page
    # a form of the page, with the browser's Origin
    csrf = b.cookie(csrf_name).value
    st, _, _ = b._open(urllib.request.Request(
        public + "/ser2net/settings/access", method="POST", headers={
            "Origin": public, "Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"_csrf": csrf, "require_login": "on",
                                     "anonymous_role": "viewer"}).encode()))
    assert st in (200, 303), st   # (done: the page, after its redirect under the path)
    time.sleep(0.2)
    log = srv.output()
    assert f"AUDIT {Proxy.CLIENT} web_access login-required by admin" in log, log[-1500:]
    assert f"login: admin (admin, local) from {Proxy.CLIENT}" in log
    # the console, through the tunnel
    session = b.cookie(session_name).value
    raw = socket.create_connection(("127.0.0.1", proxied.port), timeout=10)
    if secure:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        raw = ctx.wrap_socket(raw)
    with raw:
        raw.sendall((f"GET /ser2net/api/mappings/{MID}/console HTTP/1.1\r\n"
                     f"Host: 127.0.0.1:{proxied.port}\r\nUpgrade: websocket\r\n"
                     "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                     f"Sec-WebSocket-Version: 13\r\nOrigin: {public}\r\n"
                     f"Cookie: {session_name}={session}\r\n\r\n").encode())
        answer = raw.recv(4096)
    assert answer.startswith(b"HTTP/1.1 101"), answer[:200]
    # a proxy that strips the path and says so
    s = proxied_browser(f"{stripping.scheme}://127.0.0.1:{stripping.port}")
    stripped_csrf = path_cookie("ser2net_csrf", stripping)
    st, h, page = s.get("/ser2net/login")
    assert st == 200 and 'action="/ser2net/login"' in page
    st, h, _ = s.post("/ser2net/login", {"username": "admin", "password": PW,
                                         "_csrf": s.cookie(stripped_csrf).value})
    assert st == 303 and h.get("Location") == "/ser2net/", st
    assert s.get("/ser2net/settings")[0] == 200
    # '/' directly, as before
    d = Browser(srv.base)
    d.login("admin", PW)
    assert {c.name: c.path for c in d.jar} == {ui_cookie("ser2net_csrf", srv.port): "/",
                                               ui_cookie("ser2net_session", srv.port): "/"}
    print(f"through a reverse proxy ({proxied.scheme}, the path passed on or stripped): "
          "sign-in, pages, forms and the console under the path, its own"
          + (" Secure" if secure else "") + " cookies, the client's address  OK")


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

    srv = Server()
    tls = None
    if need("cryptography", what="TLS at the test's reverse proxy (it then speaks plain HTTP)"):
        from ser2netweb.web.selfsigned import generate
        tls = (os.path.join(srv.tmp, "cert.pem"), os.path.join(srv.tmp, "key.pem"))
        generate(*tls)
    proxied = Proxy(srv.port, "/ser2net", tls=tls)
    stripping = Proxy(srv.port, "/ser2net", strip=True, tls=tls)
    with open(srv.config_path, "w", encoding="utf-8") as fh:
        json.dump(dict(cfg, oidc={}, admin_ui={
            "public_url": f"{proxied.scheme}://127.0.0.1:{proxied.port}/ser2net",
            "trusted_proxies": ["127.0.0.1"]}), fh)
    try:
        with srv:
            test_through_proxy(srv, proxied, stripping)
    finally:
        proxied.close()
        stripping.close()
    print("\nPASS: reverse proxy")


if __name__ == "__main__":
    main()
