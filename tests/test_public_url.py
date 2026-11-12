"""The address the web UI is published under behind a reverse proxy (admin_ui.public_url /
SER2NET_PUBLIC_URL), and the path it is then served under too.

Checks:
  * the value: http:// or https:// with a host name, optional port and a path of plain
    segments, no user name, query or fragment, no path that starts like one of the UI's
    own pages (every top-level route is in config.RESERVED_PATHS); kept in one spelling;
    an unusable one in config.json is left out with a load note (no validate() rule);
    SER2NET_PUBLIC_URL wins for one run when it is usable, a start-up warning when not;
  * Settings -> Reverse proxy -> Public URL: saved (normalised), refused with the reason,
    logged and audited, in the restore preview; read-only while the environment sets it;
    Settings -> Admin interface shows it;
  * its origin is this UI's own: forms and the console / hex-view WebSockets of a page at
    it are accepted when the proxy rewrites Host (refused without the setting);
  * a blank SSO redirect URI becomes <public URL>/auth/oidc/callback (and Settings says
    so); without a public URL it is made from the request as before;
  * notifications end with the public URL (the generic webhook JSON gets "url"), and are
    unchanged without one;
  * the start-up warning for plain HTTP on a network address names the TLS proxy;
  * with a path: requests under it (passed on by the proxy, or stripped and announced
    with exactly that X-Forwarded-Prefix) are served with every link, form, htmx and
    WebSocket address, redirect, the OpenAPI servers entry and the language switch under
    it, and cookies of their own (a name with a tag of the path, Path=<path>); the CSRF
    check and the REST API's token gate hold there (also with open access), the cluster
    peer endpoints stay public; '/' is served exactly as before (cookie names, links);
  * under the path too: the two-factor code step (its cookie under the path, the session
    after the code) and the Location of a mapping made through the REST API;
  * the page a sign-in leads back to (?next=): a page asked for without a session sends
    to the sign-in page with its path under the base path left out, and the sign-in -
    through the two-factor code step too - goes back to it under the base path;
  * no template, redirect or app.js address is written root-relative any more.

Calls the web app in this process (no socket).
Run: python3 tests/test_public_url.py
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time

from _asgi import App, cookies, header

from app import notify, runtime
from app.config import (
    RESERVED_PATHS, AdminUI, AppConfig, normalize_public_url, public_url_problem,
)
from app.web import auth, oidc_auth, proxy, totp

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PW = "public-url-pass-1"
CSRF = "public-url-csrf"
PUBLIC = "https://gw.example.com/ser2net"
TAG = proxy.cookie_name({"root_path": "/ser2net", "path": "/"}, "")   # "_<8 hex>"


def _cfg(**admin) -> dict:
    return {"users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                       "role": "admin", "pwd_version": 1}],
            "mappings": [{"id": "m1", "name": "line", "serial": {"port": "/dev/s2n-test"},
                          "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 47001}}],
            "admin_ui": admin}


def test_value() -> None:
    good = {"https://S2N.Example.com/": "https://s2n.example.com",
            "https://s2n.example.com:443": "https://s2n.example.com",
            "http://s2n:80/ser2net/": "http://s2n/ser2net",
            " https://gw.example.com:8443/Plant/line-1 ": "https://gw.example.com:8443/Plant/line-1",
            "https://[2001:DB8::1]:8443": "https://[2001:db8::1]:8443",
            "http://192.168.1.20:8080": "http://192.168.1.20:8080", "": ""}
    for value, kept in good.items():
        assert public_url_problem(value) is None and normalize_public_url(value) == kept, value
    bad = {"ftp://x": "http:// or https://", "s2n.example.com": "http:// or https://",
           "https://user:pw@x": "http:// or https://", "https:///x": "http:// or https://",
           "https://x/?a=1": "query", "https://x/#top": "fragment", "https://x:0": "port",
           "https://x:65536": "port", "https://x/a b": "http://", "https://x/a/../b": "path",
           "https://x//b": "path", "https://x/%2e": "path", "https://-x.example": "http://",
           "https://x/static": "/static: that is a page", "https://x/API/v": "/API",
           "https://x/Settings": "/Settings", 7: "http://", "https://x/ı": "http://"}
    for value, why in bad.items():
        assert why in (public_url_problem(value) or ""), (value, public_url_problem(value))
    # every first segment of the UI's routes is reserved
    with App(_cfg()) as a:
        firsts = {r.path.strip("/").split("/")[0] for r in a.app.routes if r.path.strip("/")}
    assert firsts <= RESERVED_PATHS, firsts - RESERVED_PATHS
    cfg = AppConfig.from_dict({"admin_ui": {"public_url": "https://GW.example.com/ser2net/"}})
    assert cfg.admin_ui.public_url == PUBLIC and not cfg.load_notes
    for value in ("https://x/login", 5, ["https://x"]):
        cfg = AppConfig.from_dict({"admin_ui": {"public_url": value}})
        assert cfg.admin_ui.public_url == "" and any("public_url" in n for n in cfg.load_notes)
        cfg.validate()
    assert AppConfig.from_dict({}).to_dict()["admin_ui"]["public_url"] == ""
    admin = AdminUI(public_url="https://s2n.example.com")
    assert proxy.public_url(admin, {}) == "https://s2n.example.com"
    assert proxy.public_url(admin, {"SER2NET_PUBLIC_URL": "https://Other.example/"}) == \
        "https://other.example"
    assert proxy.public_url(admin, {"SER2NET_PUBLIC_URL": "nope"}) == "https://s2n.example.com"
    assert any("WARNING: SER2NET_PUBLIC_URL 'nope' is not used" in n
               for n in proxy.start_notes(admin, {"SER2NET_PUBLIC_URL": "nope"}))
    assert proxy.origin_of(PUBLIC) == "https://gw.example.com" and proxy.path_of(PUBLIC) == "/ser2net"
    assert proxy.origin_of("") == "" and proxy.path_of("https://s2n.example.com") == ""
    print("public URL: what is accepted, how it is kept, reserved paths, the environment  OK")


def test_warning_and_notifications() -> None:
    lan = AdminUI(bind_ip="0.0.0.0")
    assert "Configure TLS" in runtime.plain_http_warning(lan)
    assert "docs/REVERSE-PROXY.md" in runtime.plain_http_warning(lan)
    via = runtime.plain_http_warning(lan, "https://s2n.example.com")
    assert via.startswith("WARNING: the admin UI is published as https://s2n.example.com "
                          "through a TLS reverse proxy, but it also answers plain HTTP on "
                          "0.0.0.0:8080"), via
    both = runtime.plain_http_warning(AdminUI(bind_ip=""), "https://s2n.example.com")
    assert "plain HTTP on *:8080 —" in both, both   # every interface, IPv4 and IPv6
    v6 = runtime.plain_http_warning(AdminUI(bind_ip="2001:db8::5"), "https://s2n.example.com")
    assert "plain HTTP on [2001:db8::5]:8080 —" in v6, v6
    assert runtime.plain_http_warning(lan, "http://s2n.example.com").startswith(
        "WARNING: admin UI is bound to a network address over plain HTTP")
    assert runtime.plain_http_warning(AdminUI()) == ""
    assert runtime.plain_http_warning(AdminUI(bind_ip="0.0.0.0", tls_cert="c", tls_key="k")) == ""

    head, text = notify.message("device_lost", "PLC", "gone", PUBLIC)
    assert text.endswith("\ngone\n" + PUBLIC), text
    assert notify.message("device_lost", "PLC", "gone")[1].endswith("\ngone")
    plain = notify.webhook_body("generic", "device_lost", "PLC", "gone")
    assert "url" not in plain and not plain["text"].endswith(PUBLIC)
    body = notify.webhook_body("generic", "device_lost", "PLC", "gone", url=PUBLIC)
    assert body["url"] == PUBLIC and body["text"].endswith(PUBLIC)
    card = notify.webhook_body("teams", "device_lost", "PLC", "gone", url=PUBLIC)
    assert card["attachments"][0]["content"]["body"][-1]["text"] == PUBLIC
    for fmt in ("slack", "discord"):
        assert PUBLIC in json.dumps(notify.webhook_body(fmt, "device_lost", "PLC", "", url=PUBLIC))
    # the notifier sends the public URL it has at that moment
    sent = []
    real = notify.send_webhook
    notify.send_webhook = lambda s, *args, **kw: sent.append(args)
    try:
        with App(_cfg(public_url=PUBLIC)) as a:
            a.state.config.notifications.webhook_url = "https://hooks.example/x"
            errors = asyncio.run(a.state.notifier.deliver("test", "", "hello"))
            a.state.config.admin_ui.public_url = ""
            asyncio.run(a.state.notifier.deliver("test", "", "hello"))
    finally:
        notify.send_webhook = real
    assert errors == [] and sent == [("test", "", "hello", PUBLIC), ("test", "", "hello", "")], sent
    print("start-up warning names the TLS proxy; notifications link to the public URL  OK")


def _sign_in(a: App, prefix: str = "", headers=None):
    cookie = proxy.cookie_name({"root_path": prefix, "path": "/"}, "ser2net_csrf")
    st, h, _ = a.call("POST", prefix + "/login",
                      fields={"username": "admin", "password": PW, "_csrf": CSRF},
                      headers={"Cookie": f"{cookie}={CSRF}", **(headers or {})})
    assert st == 303, st
    return h, f"{cookie}={CSRF}; " + "; ".join(f"{k}={v[0]}" for k, v in cookies(h).items()
                                              if k.startswith("ser2net_session"))


def test_origins_and_sso() -> None:
    real = oidc_auth.build_authorize_url
    asked = []
    oidc_auth.build_authorize_url = lambda oidc, uri, *a, **k: asked.append(uri) or "https://idp/x"
    rewritten = {"Host": "127.0.0.1:8080", "Origin": "https://s2n.example.com"}
    try:
        with App(dict(_cfg(), oidc={"enabled": True, "issuer": "https://idp.example",
                                    "client_id": "c", "client_secret": "s"})) as a:
            _h, jar = _sign_in(a)
            form = {"_csrf": CSRF, "require_login": "on", "anonymous_role": "viewer"}
            st, _, _ = a.call("POST", "/settings/access", fields=form,
                              headers={**rewritten, "Cookie": jar})
            assert st == 403, st
            ws = a.websocket("/api/mappings/m1/console", headers={**rewritten, "Cookie": jar})
            assert ws == {"type": "websocket.close", "code": 1008, "reason": ""}, ws
            a.call("GET", "/auth/oidc/login", headers={"Host": "127.0.0.1:8080"})
            # the setting (saved once as it is: the restore preview below has a backup)
            st, h, _ = a.call("POST", "/settings/proxy", fields={"_csrf": CSRF, "public_url": ""},
                              headers={"Cookie": jar})
            assert st == 303 and header(h, "location") == "/settings#reverse-proxy", (st, h)
            # (nothing changed, so nothing was saved: config.json is written here, so that
            # the change below has a backup to compare)
            a.state.store.save(a.state.config)
            assert os.path.exists(a.state.store.path)
            for bad, why in (("https://s2n.example.com/login", "/login: that is a page"),
                             ("s2n.example.com", "http:// or https://")):
                st, _, page = a.call("POST", "/settings/proxy", fields={"_csrf": CSRF,
                                                                        "public_url": bad},
                                     headers={"Cookie": jar})
                assert st == 400 and why in page and a.state.config.admin_ui.public_url == "", bad
                assert f'name="public_url" value="{bad}"' in page, "the form keeps what was sent"
            a.call("GET", "/settings", headers={"Cookie": jar})   # (the first save's message)
            st, _, _ = a.call("POST", "/settings/proxy",
                              fields={"_csrf": CSRF, "public_url": "HTTPS://S2N.example.com/"},
                              headers={"Cookie": jar})
            assert st == 303, st
            st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
            assert st == 200 and "Reverse proxy settings saved." in page
            assert a.state.config.admin_ui.public_url == "https://s2n.example.com"
            assert a.state.store.load().admin_ui.public_url == "https://s2n.example.com"
            assert "reverse proxy settings: public URL https://s2n.example.com" in a.lines
            assert any(r["action"] == "reverse_proxy_set" for r in a.state.read_audit())
            assert ('Published as <a class="mono" href="https://s2n.example.com">'
                    'https://s2n.example.com</a>') in page
            assert 'name="public_url" value="https://s2n.example.com"' in page
            # now a page of the public URL is this UI's own
            st, _, _ = a.call("POST", "/settings/access", fields=form,
                              headers={**rewritten, "Cookie": jar})
            assert st == 303, st   # done: back to the page
            ws = a.websocket("/api/mappings/m1/console", headers={**rewritten, "Cookie": jar})
            # past the origin check and the session: accepted (then closed 4004: no runner)
            assert ws["type"] == "websocket.accept", ws
            for other in ("http://s2n.example.com", "https://s2n.example.com:8443",
                          "https://evil.example"):
                st, _, _ = a.call("POST", "/settings/access", fields=form,
                                  headers={"Host": "127.0.0.1:8080", "Origin": other, "Cookie": jar})
                assert st == 403, other
            # SSO: the redirect URI from the public URL, and Settings says so
            a.call("GET", "/auth/oidc/login", headers={"Host": "127.0.0.1:8080"})
            assert asked == ["http://127.0.0.1:8080/auth/oidc/callback",
                             "https://s2n.example.com/auth/oidc/callback"], asked
            st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
            assert 'data-uri="https://s2n.example.com/auth/oidc/callback">' in page
            assert "so it is made from the public URL" in page
            oldest = a.state.store.backup_names()[-1]   # (the newest is what config.json holds)
            st, _, page = a.call("GET", f"/settings?restore={oldest}", headers={"Cookie": jar})
            assert "Public URL: https://s2n.example.com → —" in page
        os.environ["SER2NET_PUBLIC_URL"] = PUBLIC
        try:
            with App(dict(_cfg(public_url="https://s2n.example.com"))) as a:
                _h, jar = _sign_in(a)
                st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
                assert f'<input type="url" value="{PUBLIC}" disabled>' in page
                assert "Set by <span class=\"mono\">SER2NET_PUBLIC_URL</span>" in page
                st, _, _ = a.call("POST", "/settings/proxy", fields={"_csrf": CSRF},
                                  headers={"Cookie": jar})
                assert st == 303 and a.state.config.admin_ui.public_url == "https://s2n.example.com"
        finally:
            del os.environ["SER2NET_PUBLIC_URL"]
    finally:
        oidc_auth.build_authorize_url = real
    print("public URL: its pages' forms and WebSockets accepted, the SSO redirect URI, "
          "Settings (saved / refused / from the environment)  OK")


def test_base_path() -> None:
    real = oidc_auth.build_authorize_url
    oidc_auth.build_authorize_url = lambda *a, **k: "https://idp/x"
    try:
        with App(dict(_cfg(public_url=PUBLIC), oidc={"enabled": True, "issuer": "https://i.example",
                                                     "client_id": "c", "client_secret": "s"})) as a:
            _base_path(a)
    finally:
        oidc_auth.build_authorize_url = real


def _base_path(a: App) -> None:
    # '/' as before: names, paths, links
    st, h, page = a.call("GET", "/login")
    assert st == 200 and cookies(h)["ser2net_csrf"][1].count("path=/;") == 1, h
    assert 'href="/static/app.css' in page and 'action="/login"' in page
    assert '<meta name="ser2net-base" content="">' in page
    h, jar = _sign_in(a)
    assert header(h, "location") == "/"
    st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
    assert 'action="/settings/proxy"' in page and 'href="/lang/tr?next=/settings"' in page

    def check_page(page: str) -> None:
        assert '<meta name="ser2net-base" content="/ser2net">' in page
        assert 'href="/ser2net/static/app.css' in page and 'src="/ser2net/static/app.js' in page
        assert re.search(r'(?:href|src|action|hx-[a-z]+|data-ws(?:-hex)?)="/(?!ser2net/)', page) \
            is None, re.findall(r'\S+="/(?!ser2net/)[^"]*"', page)[:5]

    for headers, path in (({}, "/ser2net/login"), ({"X-Forwarded-Prefix": "/ser2net/"}, "/login")):
        st, h, page = a.call("GET", path, headers=headers)
        assert st == 200, (path, st)
        check_page(page)
        name = f"ser2net_csrf{TAG}"
        assert set(cookies(h)) == {name} and "path=/ser2net;" in cookies(h)[name][1], h
    # another X-Forwarded-Prefix: '/' as before
    st, h, page = a.call("GET", "/login", headers={"X-Forwarded-Prefix": "/other"})
    assert 'href="/static/app.css' in page and set(cookies(h)) == {"ser2net_csrf"}
    # the path itself, a redirect to the sign-in page under it
    st, h, _ = a.call("GET", "/ser2net")
    assert st == 303 and header(h, "location") == "/ser2net/login", (st, h)
    st, h, _ = a.call("GET", "/", headers={"X-Forwarded-Prefix": "/ser2net"})
    assert st == 303 and header(h, "location") == "/ser2net/login", (st, h)

    h, jar = _sign_in(a, "/ser2net")
    assert header(h, "location") == "/ser2net/"
    session = cookies(h)[f"ser2net_session{TAG}"]
    assert "path=/ser2net;" in session[1], session
    # a cookie of an instance at '/' of the same site doesn't get in the way
    jar_both = jar + "; ser2net_session=from-another-instance; ser2net_csrf=other"
    for path in ("/ser2net/", "/ser2net/settings", "/ser2net/settings/audit"):
        st, _, page = a.call("GET", path, headers={"Cookie": jar_both})
        assert st == 200, (path, st)
        check_page(page)
    assert 'hx-get="/ser2net/api/status"' in a.call("GET", "/ser2net/", headers={"Cookie": jar})[2]
    st, _, page = a.call("GET", "/ser2net/settings", headers={"Cookie": jar})
    assert 'href="/ser2net/lang/tr?next=/settings"' in page
    assert 'action="/ser2net/settings/proxy"' in page
    assert 'Spec: <a href="/ser2net/api/v1/openapi.json">' in page
    st, h, _ = a.call("GET", "/ser2net/lang/tr?next=/settings", headers={"Cookie": jar})
    assert st == 303 and header(h, "location") == "/ser2net/settings"
    assert "path=/ser2net;" in cookies(h)[f"ser2net_lang{TAG}"][1]
    st, h, body = a.call("GET", "/ser2net/static/app.js")
    assert st == 200 and "function basePath()" in body
    # the dashboard's fragments: htmx and WebSocket addresses under the path
    st, _, page = a.call("GET", "/ser2net/api/mappings/m1/console-view", headers={"Cookie": jar})
    assert st == 200 and 'data-ws="/ser2net/api/mappings/m1/console"' in page, page[:300]
    st, _, page = a.call("GET", "/ser2net/api/status", headers={"Cookie": jar})
    assert st == 200 and 'hx-get="/ser2net/api/mappings/m1/form"' in page
    # the checks hold under the path: CSRF, cross-site, session
    st, _, body = a.call("POST", "/ser2net/api/mappings/m1/stop", headers={"Cookie": jar})
    assert st == 403 and "CSRF validation failed" in body, (st, body)
    st, _, _ = a.call("POST", "/ser2net/api/mappings/m1/stop",
                      headers={"Cookie": jar, "X-CSRF-Token": CSRF, "Origin": "https://evil.example"})
    assert st == 403
    st, _, _ = a.call("POST", "/ser2net/api/mappings/m1/stop",
                      headers={"Cookie": jar, "X-CSRF-Token": CSRF, "Origin": "https://gw.example.com",
                               "Host": "127.0.0.1:8080"})
    assert st == 200, st
    st, h, _ = a.call("GET", "/ser2net/settings", headers={"Cookie": "ser2net_session=x"})
    assert st == 303 and header(h, "location") == "/ser2net/login?next=%2Fsettings", h
    # WebSockets: the path's own session cookie
    ws = a.websocket("/ser2net/api/mappings/m1/console", headers={"Cookie": jar})
    assert ws["type"] == "websocket.accept", ws
    ws = a.websocket("/ser2net/api/mappings/m1/console",
                     headers={"Cookie": "ser2net_session=" + session[0]})
    assert ws["code"] == 1008, ws
    # SSO state cookie under the path
    st, h, _ = a.call("GET", "/ser2net/auth/oidc/login")
    assert "path=/ser2net/auth/oidc;" in cookies(h)[f"ser2net_oidc{TAG}"][1], h
    # the REST API: its token, also with open access; the OpenAPI document's server
    a.state.config.admin_ui.require_login = False
    try:
        st, _, body = a.call("GET", "/ser2net/api/v1/status")
        assert st == 401 and "api_token_not_configured" in body, (st, body)
        st, _, _ = a.call("GET", "/ser2net/settings")   # open access, the UI itself
        assert st == 200
    finally:
        a.state.config.admin_ui.require_login = True
    for prefix in ("", "/ser2net"):
        st, _, body = a.call("GET", prefix + "/api/v1/openapi.json")
        assert json.loads(body)["servers"] == [{"url": prefix + "/api/v1"}], body[:200]
    # the cluster nodes' own endpoints: public (the key decides), like at '/'
    for prefix in ("", "/ser2net"):
        st, h, _ = a.call("GET", prefix + "/api/cluster/local")
        assert st == 403 and header(h, "location") is None, (prefix, st)
    # sign-out under the path
    st, h, _ = a.call("POST", "/ser2net/logout", fields={"_csrf": CSRF}, headers={"Cookie": jar})
    assert st == 303 and header(h, "location") == "/ser2net/login"
    assert "path=/ser2net;" in cookies(h)[f"ser2net_session{TAG}"][1]
    print("under the public URL's path: links, forms, htmx, WebSockets, redirects, OpenAPI, "
          "its own cookies; CSRF / token / session checks hold; '/' as before  OK")


def test_base_path_totp_and_rest() -> None:
    secret, token = totp.new_secret(), "s2n_public-url-operator-token"
    cfg = _cfg(public_url=PUBLIC)
    cfg["users"].append({"username": "twofa", "password_hash": auth.hash_password(PW),
                         "role": "operator", "pwd_version": 1, "totp_secret": secret})
    cfg.update(api_token_hash=auth.hash_token(token), api_token_role="operator")
    with App(cfg) as a:
        csrf = f"ser2net_csrf{TAG}={CSRF}"
        st, h, _ = a.call("POST", "/ser2net/login", headers={"Cookie": csrf},
                          fields={"username": "twofa", "password": PW, "_csrf": CSRF})
        assert st == 303 and header(h, "location") == "/ser2net/auth/totp", (st, h)
        pending = cookies(h)[f"ser2net_2fa{TAG}"]
        assert "path=/ser2net/auth/totp;" in pending[1], pending
        assert not any(k.startswith("ser2net_session") for k in cookies(h)), h
        jar = f"{csrf}; ser2net_2fa{TAG}={pending[0]}"
        st, _, page = a.call("GET", "/ser2net/auth/totp", headers={"Cookie": jar})
        assert st == 200 and 'action="/ser2net/auth/totp"' in page, (st, page[:300])
        st, h, _ = a.call("POST", "/ser2net/auth/totp", headers={"Cookie": jar},
                          fields={"code": totp.code_at(secret, time.time()), "_csrf": CSRF})
        assert st == 303 and header(h, "location") == "/ser2net/", (st, h)
        got = cookies(h)
        assert "path=/ser2net;" in got[f"ser2net_session{TAG}"][1], got
        assert got[f"ser2net_csrf{TAG}"][0] != CSRF, "a new CSRF token after the sign-in"
        assert "max-age=0" in got[f"ser2net_2fa{TAG}"][1].lower(), got
        st, h, body = a.call("POST", "/ser2net/api/v1/mappings",
                             headers={"Authorization": f"Bearer {token}"},
                             json_body={"name": "made", "enabled": False,
                                        "serial": {"port": "/dev/s2n-test-2"},
                                        "network": {"bind_ip": "127.0.0.1", "port": 4012}})
        made = json.loads(body)
        assert st == 201 and header(h, "location") == f"/ser2net/api/v1/mappings/{made['id']}", h
    print("under the public URL's path: the two-factor code step and a REST Location  OK")


def test_next_through_base_path_and_code_step() -> None:
    secret = totp.new_secret()
    cfg = _cfg(public_url=PUBLIC)
    cfg["users"].append({"username": "twofa", "password_hash": auth.hash_password(PW),
                         "role": "operator", "pwd_version": 1, "totp_secret": secret})
    with App(cfg) as a:
        st, h, _ = a.call("GET", "/ser2net/settings/audit?q=x")
        assert st == 303 and header(h, "location") == \
            "/ser2net/login?next=%2Fsettings%2Faudit%3Fq%3Dx", (st, h)
        csrf = f"ser2net_csrf{TAG}={CSRF}"
        st, _, page = a.call("GET", "/ser2net/login?next=%2Fsettings%2Faudit%3Fq%3Dx",
                             headers={"Cookie": csrf})
        assert 'name="next" value="/settings/audit?q=x"' in page, page[:2000]
        # the password alone: straight back to the page (proxy.redirect: under the path)
        st, h, _ = a.call("POST", "/ser2net/login", headers={"Cookie": csrf},
                          fields={"username": "admin", "password": PW, "_csrf": CSRF,
                                  "next": "/settings/audit?q=x"})
        assert st == 303 and header(h, "location") == "/ser2net/settings/audit?q=x", (st, h)
        # password, then the code: the pending cookie carries the page to the code step
        st, h, _ = a.call("POST", "/ser2net/login", headers={"Cookie": csrf},
                          fields={"username": "twofa", "password": PW, "_csrf": CSRF,
                                  "next": "/settings/audit?q=x"})
        assert st == 303 and header(h, "location") == "/ser2net/auth/totp", (st, h)
        jar = f"{csrf}; ser2net_2fa{TAG}={cookies(h)[f'ser2net_2fa{TAG}'][0]}"
        st, h, _ = a.call("POST", "/ser2net/auth/totp", headers={"Cookie": jar},
                          fields={"code": totp.code_at(secret, time.time()), "_csrf": CSRF})
        assert st == 303 and header(h, "location") == "/ser2net/settings/audit?q=x", (st, h)
        # another site's address is never followed (local_path), here neither
        st, h, _ = a.call("POST", "/ser2net/login", headers={"Cookie": csrf},
                          fields={"username": "admin", "password": PW, "_csrf": CSRF,
                                  "next": "//evil.example/x"})
        assert st == 303 and header(h, "location") == "/ser2net/", (st, h)
    print("?next= under the public URL's path: the guard leaves the path out, the sign-in "
          "and the two-factor code step lead back under it  OK")


def test_no_root_relative_addresses() -> None:
    """Every address in a template goes under the request's path prefix ({{ base }})."""
    names = r'\b(?:href|src|action|formaction|hx-(?:get|post|put|patch|delete)|data-ws(?:-hex)?)'
    attr = re.compile(names + r'\s*=\s*["\']/')
    # an address made by an expression: {{ base }}{{ '/a' if x else '/b' }}
    expression = re.compile(names + r'\s*=\s*"\{\{(?!\s*base\b)[^}]*[\'"]/')
    linked = re.compile(r"""href=\\?["']/""")   # a link in a translated text
    literal = re.compile(r"""['"]/(?:api|auth|settings|login|logout|setup|static|lang)\b""")
    for path in sorted(glob.glob(os.path.join(ROOT, "app", "web", "templates", "*.html"))):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for rx in (attr, expression, linked):
            assert not rx.search(text), (os.path.basename(path), rx.search(text))
    with open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8") as fh:
        assert not linked.search(fh.read()), "a translated link with a root-relative address"
    for path in glob.glob(os.path.join(ROOT, "app", "web", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        assert not re.search(r'RedirectResponse\(\s*["\']/', text), path
    with open(os.path.join(ROOT, "app", "web", "static", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    for m in literal.finditer(js):
        assert js[max(0, m.start() - 13):m.start()] == "basePath() + ", js[m.start() - 40:m.end()]
    print("templates, redirects and app.js write no root-relative address  OK")


def main() -> None:
    for name in ("SER2NET_TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS", "SER2NET_PUBLIC_URL"):
        os.environ.pop(name, None)
    test_value()
    test_warning_and_notifications()
    test_origins_and_sso()
    test_base_path()
    test_base_path_totp_and_rest()
    test_next_through_base_path_and_code_step()
    test_no_root_relative_addresses()
    print("\nPASS: public URL and path")


if __name__ == "__main__":
    main()
