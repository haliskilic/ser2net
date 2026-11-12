"""What the web UI tells the browser, end to end against a running server:

  * the Content-Security-Policy: scripts from the UI's own files only, no <base>, no
    plugins, no framing, forms post only to the UI itself;
  * Cache-Control: no-store on every response but the static files - pages, fragments,
    downloads, redirects, refusals and the REST API - so no secret a page shows (a new
    API token, the MQTT password in the mapping form, a backup) stays in the browser.

Run: python3 tests/test_browser_hardening.py
"""
import os
import re
import urllib.request

from _server import Browser, Server, free_port

from app.web.auth import hash_password

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
    print("\nPASS: browser hardening")


if __name__ == "__main__":
    main()
