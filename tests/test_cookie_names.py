"""The web UI's cookie names keep instances on one host name apart.

Browsers keep cookies per host name, not per port or scheme, so two ser2net instances
reached as http://host:8080 and http://host:8081 (side-by-side services, containers,
SSH tunnels to localhost, the fleet view's Open link to a peer on the same address) used
to share one session, CSRF, language and SSO cookie: signing in to one signed the other
out, and after an https:// address had left Secure cookies an http:// one of the same
name could not set its own. Checks:
  * the names: plain on port 8080 at '/' (as before), <name>_<8 hex of the port> on any
    other port (also none in the address, i.e. 80/443), the public URL's path in the tag,
    __Host- over HTTPS (__Secure- for a cookie whose Path isn't /), pinned values;
  * a sign-in from before the upgrade (the plain names) keeps working: a session cookie
    that is this UI's is moved to its new name (same token, same expiry) and deleted
    under the old one, one of another instance is left alone, the CSRF token and the
    language are taken over, a sign-out ends it, the console accepts it;
  * two real servers on two ports of 127.0.0.1 with one cookie jar (as a browser keeps
    them): both stay signed in, a sign-out of one leaves the other.

Run: python3 tests/test_cookie_names.py
"""
from __future__ import annotations

import http.cookiejar
import time
import urllib.request

from _asgi import App, cookies
from _server import Browser, Server, _NoRedirect, ui_cookie
from starlette.requests import Request

from ser2netweb.web import auth, proxy

PW = "cookie-names-pass-1"
USERS = [{"username": "admin", "password_hash": auth.hash_password(PW), "role": "admin",
          "pwd_version": 1}]


def scope(host: str | None = "127.0.0.1:8080", scheme: str = "http", root: str = "",
          headers: list | None = None, cookie: str = "") -> dict:
    hdrs = ([(b"host", host.encode())] if host is not None else []) + (headers or [])
    if cookie:
        hdrs.append((b"cookie", cookie.encode()))
    return {"type": "http", "scheme": scheme, "path": root + "/", "root_path": root,
            "headers": hdrs}


def test_names() -> None:
    name = proxy.cookie_name
    assert name(scope(), "ser2net_session") == "ser2net_session"
    assert name(scope(host=None), "ser2net_session") == "ser2net_session"
    assert name(scope("127.0.0.1:8081"), "ser2net_session") == "ser2net_session_2809c4a5"
    assert name(scope("gw.example.com:8081"), "ser2net_csrf") == "ser2net_csrf_2809c4a5"
    assert name(scope("gw.example.com"), "ser2net_session") == "ser2net_session_e7ac0786"
    assert name(scope("[::1]:9000"), "ser2net_lang") == "ser2net_lang_a5fcac53"
    assert name(scope("[::1]"), "ser2net_lang") == "ser2net_lang_e7ac0786"
    # over HTTPS: its scheme, or a first X-Forwarded-Proto / Forwarded proto= of https
    assert name(scope(scheme="https"), "ser2net_session") == "__Host-ser2net_session"
    assert name(scope(scheme="wss"), "ser2net_session") == "__Host-ser2net_session"
    xfp = [(b"x-forwarded-proto", b"https")]
    assert name(scope(headers=xfp), "ser2net_csrf") == "__Host-ser2net_csrf"
    assert name(scope(headers=[(b"forwarded", b"for=1.2.3.4;proto=https")]), "ser2net_csrf") \
        == "__Host-ser2net_csrf"
    assert name(scope(headers=[(b"x-forwarded-proto", b"http, https")]), "ser2net_csrf") \
        == "ser2net_csrf"
    assert name(scope(scheme="https"), "ser2net_oidc", "/auth/oidc") == "__Secure-ser2net_oidc"
    assert name(scope("h:8443", scheme="https"), "ser2net_session") \
        == "__Host-ser2net_session_e7c23153"
    # under the public URL's path (at 8080 the path's tag alone, as before)
    assert name(scope(root="/ser2net"), "ser2net_session") == "ser2net_session_6a162d13"
    assert name(scope("h:8081", root="/ser2net"), "ser2net_session") \
        == "ser2net_session_3cc2eac5"
    assert name(scope(scheme="https", root="/ser2net"), "ser2net_session") \
        == "__Secure-ser2net_session_6a162d13"
    # an http:// and an https:// instance, or one instance with TLS on and then off: the
    # names never match, so a Secure cookie of the one can't block the other's
    for port in ("8080", "8081", "8443"):
        assert name(scope(f"h:{port}"), "ser2net_csrf") != name(scope(f"h:{port}", "https"),
                                                                "ser2net_csrf")
    # the names of 2.8 and older that a request may still carry
    legacy = proxy.legacy_cookie_name
    assert legacy(scope(), "ser2net_session") is None   # the same name
    assert legacy(scope("h:8081"), "ser2net_session") == "ser2net_session"
    assert legacy(scope(scheme="https"), "ser2net_session") == "ser2net_session"
    assert legacy(scope(scheme="https"), "ser2net_oidc", "/auth/oidc") == "ser2net_oidc"
    assert legacy(scope(root="/ser2net"), "ser2net_session") is None   # 2.8 had no path
    # what the tests use agrees
    assert ui_cookie("ser2net_session", 8081) == "ser2net_session_2809c4a5"
    assert ui_cookie("ser2net_session", 8080, "https") == "__Host-ser2net_session"
    # reading: the new name, else the old one (unless asked not to)
    req = Request(scope("h:8081", cookie="ser2net_oidc=old; ser2net_csrf=o"))
    assert auth.cookie(req, "ser2net_oidc", path="/auth/oidc") == "old"
    assert auth.cookie(req, "ser2net_csrf", legacy=False) is None
    req = Request(scope("h:8081", cookie="ser2net_csrf=o; ser2net_csrf_2809c4a5=n"))
    assert auth.cookie(req, "ser2net_csrf") == "n"
    print("names: plain at 8080 '/', tagged by any other port and the path, __Host- / "
          "__Secure- over HTTPS; the plain names of 2.8 read as a fallback  OK")


def test_upgrade_in_process() -> None:
    port = "127.0.0.1:8081"
    new, old = "ser2net_session_2809c4a5", "ser2net_session"
    with App({"users": USERS, "mappings": [{"id": "m1", "name": "Line", "enabled": False,
                                             "serial": {"port": "/dev/ttyS0"}}]}) as a:
        cfg = a.state.config
        token = auth.issue_session(cfg.secret_key, 3600, "admin", 1)
        # a sign-in from 2.8 under the old name: taken, moved, the old one deleted
        st, h, page = a.call("GET", "/settings", host=port,
                             headers={"Cookie": f"{old}={token}; ser2net_csrf=old-csrf"})
        assert st == 200, st
        made = cookies(h)
        assert made[new][0] == token and 3590 <= int(made[new][1].split("max-age=")[1]
                                                     .split(";")[0]) <= 3600, made
        assert made[old][0] == "" and "max-age=0" in made[old][1], made
        # the CSRF token is taken over (an open page's forms keep working) and set anew
        assert made["ser2net_csrf_2809c4a5"][0] == "old-csrf", made
        assert 'name="csrf-token" content="old-csrf"' in page
        assert "ser2net_csrf" not in made, "the old CSRF cookie may be another instance's"
        # with both, the new one counts and nothing is moved again
        st, h, _ = a.call("GET", "/settings", host=port,
                          headers={"Cookie": f"{new}={token}; {old}=x.y; "
                                             "ser2net_csrf_2809c4a5=old-csrf"})
        assert st == 200 and cookies(h) == {}, cookies(h)
        # a form posted from a page drawn before the upgrade (only the old cookies)
        st, _h, _ = a.call("POST", "/api/mappings/m1/stop", host=port,
                           headers={"Cookie": f"{old}={token}; ser2net_csrf=old-csrf",
                                    "X-CSRF-Token": "old-csrf"})
        assert st == 200, st
        # another instance's session under the old name: not taken, not deleted
        other = auth.issue_session("another-instance-key", 3600, "admin", 1)
        st, h, _ = a.call("GET", "/settings", host=port, headers={"Cookie": f"{old}={other}"})
        assert st == 303 and old not in cookies(h), (st, cookies(h))
        # on port 8080 the old name is the name: nothing to move
        st, h, _ = a.call("GET", "/settings", headers={"Cookie": f"{old}={token}"})
        assert st == 200 and old not in cookies(h), cookies(h)
        # a static file's response sets no session cookie (it may be cached)
        st, h, _ = a.call("GET", "/static/app.css", host=port, headers={"Cookie": f"{old}={token}"})
        assert st == 200 and old not in cookies(h) and new not in cookies(h), cookies(h)
        # the language picked under 2.8
        st, _, page = a.call("GET", "/login", host=port, headers={"Cookie": "ser2net_lang=tr"})
        assert st == 200 and '<html lang="tr">' in page, page[:200]
        # the console accepts such a session too (past the sign-in check: not 1008)
        msg = a.websocket("/api/mappings/m1/console", host=port,
                          headers={"Cookie": f"{old}={token}", "Origin": f"http://{port}"})
        assert msg.get("code") != 1008, msg
        # over HTTPS (a TLS proxy) the session moves to __Host-…, the old one deleted Secure
        st, h, _ = a.call("GET", "/settings", headers={"Cookie": f"{old}={token}",
                                                       "X-Forwarded-Proto": "https"})
        made = cookies(h)
        assert st == 200 and made["__Host-ser2net_session"][0] == token, made
        assert made[old][0] == "" and "secure" in made[old][1], made
        # a sign-out with the old cookie ends that session and deletes it
        st, h, _ = a.call("POST", "/logout", host=port, fields={"_csrf": "old-csrf"},
                          headers={"Cookie": f"{old}={token}; ser2net_csrf=old-csrf"})
        made = cookies(h)
        assert st == 303 and made[old][0] == "" and made[new][0] == "", made
        st, _h, _ = a.call("GET", "/settings", host=port, headers={"Cookie": f"{new}={token}"})
        assert st == 303, "ended by the sign-out"
    print("upgrade: a 2.8 session cookie is moved to the new name (its own only), the CSRF "
          "token and language taken over, the console accepts it, a sign-out ends it  OK")


def _jar_signed_in(b: Browser) -> bool:
    st, h, _ = b.get("/settings")
    assert st == 200 or (st == 303 and h.get("Location") == "/login?next=%2Fsettings"), st
    return st == 200


def test_two_instances() -> None:
    with Server({"users": USERS}) as one, Server({"users": USERS}) as two:
        jar = http.cookiejar.CookieJar()      # one browser: cookies by host, not port
        a, b = Browser(one.base), Browser(two.base)
        for br in (a, b):
            br.jar = jar
            br.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                                _NoRedirect())
        a.login("admin", PW)
        b.login("admin", PW)
        assert _jar_signed_in(a) and _jar_signed_in(b), "both stay signed in"
        names = {c.name for c in jar}
        assert {ui_cookie("ser2net_session", one.port),
                ui_cookie("ser2net_session", two.port)} <= names, names
        st, _, _ = b.post("/logout", {})
        assert st == 303
        time.sleep(0.1)
        assert _jar_signed_in(a) and not _jar_signed_in(b), "a sign-out leaves the other"
    print("two instances on one host name, one cookie jar: both signed in, a sign-out of "
          "one leaves the other  OK")


def main() -> None:
    test_names()
    test_upgrade_in_process()
    test_two_instances()
    print("\nPASS: cookie names per port, path and scheme")


if __name__ == "__main__":
    main()
