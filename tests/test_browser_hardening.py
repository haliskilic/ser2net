"""What the web UI tells the browser, end to end against a running server:

  * the Content-Security-Policy: scripts from the UI's own files only, no <base>, no
    plugins, no framing, forms post only to the UI itself;
  * Cache-Control: no-store on every response but the static files - pages, fragments,
    downloads, redirects, refusals and the REST API - so no secret a page shows (a new
    API token, the MQTT password in the mapping form, a backup) stays in the browser;
  * a state-changing request that a page of another site made the browser send is
    refused (Sec-Fetch-Site other than same-origin / none, or an Origin that is none of
    the hosts the request claims, port included) - with a valid CSRF token too, since
    another port of the host can put its own token into the browser; this UI's own
    pages, a reverse proxy passing the browser's host name, requests without either
    header (scripts) and the cluster peer calls are not affected;
  * the CSRF token is new after a sign-in and a sign-out, and its cookie lasts as long
    as the session (a browser restart doesn't leave a session without one).

Run: python3 tests/test_browser_hardening.py
"""
import os
import re
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port, ui_cookie

from ser2netweb.web.auth import hash_password

PW = "hardening-pass-1"


def directives(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        name, *values = part.split()
        out[name] = values
    return out


def test_csp(srv) -> None:
    b = Browser(srv.base)
    b.login("admin", PW)
    for path in ("/login", "/", "/settings", "/api/status"):
        st, h, _ = b.get(path)
        assert st == 200, (path, st)
        csp = directives(h.get("Content-Security-Policy") or "")
        assert csp.get("script-src") == ["'self'"], (path, csp)
        assert csp.get("base-uri") == ["'none'"], (path, csp)
        assert csp.get("form-action") == ["'self'"], (path, csp)
        assert csp.get("object-src") == ["'none'"], (path, csp)
        assert csp.get("frame-ancestors") == ["'none'"], (path, csp)
        # the console's xterm.js adds <style> elements and style attributes
        assert csp.get("style-src") == ["'self'", "'unsafe-inline'"], (path, csp)
    print("CSP: scripts 'self', no <base>, no plugins, forms to 'self'  OK")


def test_no_store(srv) -> None:
    b = Browser(srv.base)

    def stored(h) -> str:
        return h.get("Cache-Control") or ""

    st, h, _ = b.get("/")  # not signed in: the redirect to /login
    assert st == 303 and stored(h) == "no-store", (st, stored(h))
    st, h, _ = b.post("/login", {"username": "admin", "password": PW, "_csrf": "wrong"})
    assert st == 403 and stored(h) == "no-store", (st, stored(h))   # a refusal
    assert "frame-ancestors" in (h.get("Content-Security-Policy") or "")
    b.login("admin", PW)
    st, h, body = b.post("/settings/api-token", {"api_token_role": "operator"})
    token = re.search(r"s2n_[A-Za-z0-9_-]+", body).group(0)
    assert st == 200 and stored(h) == "no-store", (st, stored(h))   # the token, shown once
    backup = sorted(os.listdir(os.path.join(srv.data, "backups")))[-1]
    for path in ("/login", "/", "/settings", "/api/status", "/api/mappings/m1/form",
                 "/api/mappings/form", "/settings/config/export", f"/settings/backups/{backup}",
                 "/settings/audit", "/settings/audit.csv", "/healthz"):
        st, h, _ = b.get(path)
        assert st == 200 and stored(h) == "no-store", (path, st, stored(h))
    for path in ("/api/v1/mappings", "/api/v1/mappings/m1", "/api/v1/health"):
        req = urllib.request.Request(srv.base + path, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert stored(r.headers) == "no-store", (path, stored(r.headers))
    for path in ("/static/app.css", "/static/htmx.min.js"):
        st, h, _ = b.get(path)
        assert st == 200 and "no-store" not in stored(h), (path, stored(h))
    print("Cache-Control: no-store on pages, fragments, downloads, refusals, the REST API; "
          "static files cacheable  OK")


CROSS_SITE = ("Refused: this request was sent by a page of another web site. If you used "
              "ser2net’s own page, its address must reach ser2net unchanged (behind a "
              "reverse proxy: as Host or X-Forwarded-Host).")


def send(b, path, fields=None, headers=None, token=None):
    """A POST from `b` with a valid CSRF token (form field and header) and `headers`."""
    token = b.csrf() if token is None else token
    data = urllib.parse.urlencode({"_csrf": token, **(fields or {})}).encode()
    req = urllib.request.Request(b.base + path, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": token,
        **(headers or {})})
    st, h, body = b._open(req)
    return st, h, body.decode("utf-8", "replace")


def test_cross_site(srv) -> None:
    b = Browser(srv.base)
    b.login("admin", PW)
    own = srv.base                                 # http://127.0.0.1:<port>
    neighbour = f"http://127.0.0.1:{srv.port + 1}"  # another port of the same host

    def users():
        return [u["username"] for u in srv.saved_config()["users"]]

    def create(name, headers):
        return send(b, "/settings/users", {"username": name, "role": "admin",
                                           "password": "new-admin-pw-1",
                                           "password2": "new-admin-pw-1"}, headers)

    for i, headers in enumerate(({"Origin": neighbour}, {"Origin": "null"},
                                 {"Origin": "http://ser2net.example.com"},
                                 {"Origin": own + "/"},
                                 {"Sec-Fetch-Site": "cross-site", "Origin": neighbour},
                                 {"Sec-Fetch-Site": "same-site", "Origin": own},
                                 {"Origin": neighbour, "X-Forwarded-Host": "ser2net.example.com"})):
        st, h, body = create(f"mallory{i}", headers)
        assert st == 403 and body.strip() == CROSS_SITE, (headers, st, body[:200])
        assert h.get("Cache-Control") == "no-store", headers
    assert not any(u.startswith("mallory") for u in users()), users()
    # every state-changing request: an htmx call with the CSRF header, the sign-in form,
    # the sign-out, an import, a DELETE
    for method, path in (("POST", "/api/mappings/save"), ("POST", "/login"),
                         ("POST", "/logout"), ("POST", "/settings/config/import"),
                         ("DELETE", "/api/mappings/m1"), ("PUT", "/api/mappings/m1")):
        token = b.csrf()
        req = urllib.request.Request(b.base + path, data=f"_csrf={token}".encode(),
                                     method=method, headers={
                                         "X-CSRF-Token": token, "Origin": neighbour,
                                         "Content-Type": "application/x-www-form-urlencoded"})
        st, _, body = b._open(req)
        assert st == 403 and body.decode().strip() == CROSS_SITE, (method, path, st)
    assert b.get("/settings")[0] == 200, "still signed in: the sign-out was refused"
    assert any(c["id"] == "m1" for c in srv.saved_config()["mappings"]), "not deleted"

    # this UI's own pages; a reverse proxy that passes the browser's host name; no header.
    # (A browser behind the TLS proxy has its cookies under the names for https: __Host-…)
    over_https = "; ".join(f"{ui_cookie(n, srv.port, 'https')}={b.cookie(n).value}"
                           for n in ("ser2net_session", "ser2net_csrf"))
    for name, headers in (("same-origin", {"Sec-Fetch-Site": "same-origin", "Origin": own}),
                          ("typed", {"Sec-Fetch-Site": "none"}),
                          ("origin", {"Origin": own}),
                          ("proxied", {"Origin": "https://ser2net.example.com",
                                       "X-Forwarded-Host": "ser2net.example.com"}),
                          ("forwarded", {"Origin": "https://gw.example.com:8443",
                                         "Forwarded": "for=10.0.0.9;host=gw.example.com:8443;"
                                                      "proto=https", "Cookie": over_https}),
                          ("script", {})):
        st, _, body = create(f"ok-{name}", headers)
        # done: back to the page (303, Post/Redirect/Get), the account made
        assert st == 303 and f"ok-{name}" in users(), (name, st, body[:300])
    # a GET is never refused for its Origin (links from other sites work as before)
    req = urllib.request.Request(b.base + "/settings", headers={"Origin": neighbour,
                                                                "Sec-Fetch-Site": "cross-site"})
    assert b._open(req)[0] == 200
    # cluster peers call from their servers: the key decides, not the Origin
    req = urllib.request.Request(b.base + "/api/cluster/control", data=b"action=start",
                                 method="POST", headers={"Origin": neighbour})
    st, _, body = b._open(req)
    assert st == 403 and b"forbidden" in body, (st, body[:200])
    # logged with the reason, at most once a minute
    lines = [ln for ln in srv.output().splitlines() if "sent by a page of another site" in ln]
    assert len(lines) == 1, lines
    assert "Origin " + repr(neighbour) in lines[0] and repr(f"127.0.0.1:{srv.port}") in lines[0], \
        lines[0]
    assert "proxy_set_header Host $http_host" in lines[0] and "X-Forwarded-Host" in lines[0]
    print("cross-site requests: refused (another port, null, another host, Sec-Fetch-Site "
          "cross-site / same-site) with a valid CSRF token too; own pages, a reverse proxy's "
          "host name, scripts, GETs and cluster peers unaffected  OK")


def csrf_cookie(h, srv):
    """The CSRF Set-Cookie of a response of `srv`: (value, max-age) or None."""
    for line in h.get_all("Set-Cookie") or []:
        if line.startswith(ui_cookie("ser2net_csrf", srv.port) + "="):
            value = line.split(";", 1)[0].split("=", 1)[1]
            m = re.search(r"Max-Age=(\d+)", line, re.I)
            return value, int(m.group(1)) if m else None
    return None


def test_csrf_token(srv) -> None:
    b = Browser(srv.base)
    st, h, _ = b.get("/login")
    before, age = csrf_cookie(h, srv)
    assert age == 8 * 3600, age  # as long as a session (default 8 hours)
    assert csrf_cookie(b.get("/login")[1], srv) is None, "set once, not on every page"
    st, h, _ = b.post("/login", {"username": "admin", "password": PW})
    assert st == 303, st
    after, age = csrf_cookie(h, srv)
    assert after != before and age == 8 * 3600, (before, after, age)
    assert b.csrf() == after
    # a page still holding the token from before (the sign-in was in another tab) gets the
    # current one with the answer to its next htmx request; an up-to-date page doesn't
    for sent, want in ((before, after), (after, None)):
        _st, h, _ = b._open(urllib.request.Request(b.base + "/api/status", headers={
            "HX-Request": "true", "X-CSRF-Token": sent}))
        assert h.get("X-CSRF-Token") == want, (sent, h.get("X-CSRF-Token"))
    # a token from before the sign-in is refused (as one a page of another site set
    # while the browser had none would be)
    st, _, body = send(b, "/settings/access", {"require_login": "on"}, token=before)
    assert st == 403 and "CSRF" in body, st
    # a renewed session keeps the token the page was drawn with, for as long as it lasts
    st, h, _ = b.post("/settings/sessions/signout-others", {}, follow=False)
    assert st == 303 and csrf_cookie(h, srv) == (after, 8 * 3600), csrf_cookie(h, srv)
    assert f'name="_csrf" value="{after}"' in b.get("/settings")[2]
    st, h, _ = b.post("/logout", {})
    assert st == 303 and csrf_cookie(h, srv)[0] not in (before, after), csrf_cookie(h, srv)
    print("CSRF token: new at sign-in and sign-out, its cookie lasts as long as a session, "
          "a renewed session keeps it, a page with the old one is given the new one  OK")


def main():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "mappings": [{"id": "m1", "name": "m1", "enabled": False,
                         "serial": {"port": "/dev/null"},
                         "network": {"port": free_port()},
                         "mqtt": {"host": "broker.example", "username": "plc",
                                  "password": "broker-secret"}}]}
    with Server(cfg) as srv:
        test_csp(srv)
        test_no_store(srv)
        test_cross_site(srv)
        test_csrf_token(srv)
    print("\nPASS: browser hardening")


if __name__ == "__main__":
    main()
