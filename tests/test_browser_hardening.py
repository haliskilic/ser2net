"""What the web UI tells the browser, end to end against a running server:

  * the Content-Security-Policy: scripts from the UI's own files only, no <base>, no
    plugins, no framing, forms post only to the UI itself.

Run: python3 tests/test_browser_hardening.py
"""
from _server import Browser, Server

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


def main():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}]}
    with Server(cfg) as srv:
        test_csp(srv)
    print("\nPASS: browser hardening")


if __name__ == "__main__":
    main()
