"""The web UI's cookies are Secure whenever the browser used HTTPS, also behind a reverse
proxy that ends TLS; HSTS goes through such a proxy only when asked for.

Checks:
  * over plain HTTP the session, CSRF, language and SSO state cookies are as before: no
    Secure (and no HSTS);
  * with admin TLS, a request scheme of https (a trusted proxy's X-Forwarded-Proto), or a
    first X-Forwarded-Proto / Forwarded proto= of https from any client, all four are
    Secure and named __Host-… (the SSO state cookie __Secure-…) - deleting them too
    (sign-out); http, or https only in a
    later list entry, isn't (the raw header can only make cookies stricter: browsers
    refuse Secure cookies and HSTS over http, and a page can't add those headers);
  * HSTS: not through a proxy by default; with Settings -> Reverse proxy -> "Send HSTS
    on HTTPS through the proxy", the max-age of Admin TLS on requests that came over
    HTTPS only; saved, logged, audited, in a backup's restore preview;
  * config.json: admin_ui.proxy_hsts is optional (false), a value that isn't true or
    false is put back to false with a load note.

Over a real TLS listener: test_hsts. Calls the web app in this process (no socket).
Run: python3 tests/test_secure_cookies.py
"""
from __future__ import annotations

import os

from _asgi import App, cookies, header

from app.config import AppConfig
from app.web import auth, oidc_auth

PW = "secure-cookie-pass-1"
CSRF = "secure-cookies-csrf"
NAMES = ("ser2net_session", "ser2net_csrf", "ser2net_lang", "ser2net_oidc")


def named(secure: bool, https: bool | None = None) -> dict[str, bool]:
    """{cookie name: Secure?} of all four for a request to 127.0.0.1:8080: over HTTPS
    (`https`, by default `secure`) named __Host-… (__Secure-… for the SSO state cookie,
    whose Path isn't /), so that an http:// address of the host name can set its own."""
    https = secure if https is None else https
    return {(("__Secure-" if n == "ser2net_oidc" else "__Host-") if https else "") + n: secure
            for n in NAMES}


def csrf_name(headers: dict) -> str:
    """The CSRF cookie's name for a request with these headers (as made() sends them)."""
    https = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https" or \
        "proto=https" in headers.get("forwarded", "").split(",")[0]
    return ("__Host-" if https else "") + "ser2net_csrf"


def _cfg(**admin) -> dict:
    return {"users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                       "role": "admin", "pwd_version": 1}],
            "oidc": {"enabled": True, "issuer": "https://idp.example", "client_id": "ser2net",
                     "client_secret": "s"},
            "admin_ui": admin}


def made(a: App, **headers) -> dict[str, bool]:
    """{cookie: Secure?} of a sign-in, a new CSRF token, the language and an SSO start."""
    extra = {k.replace("_", "-"): v for k, v in headers.items()}
    base = {"Cookie": f"{csrf_name(extra)}={CSRF}", **extra}
    out: dict[str, bool] = {}
    for method, path, fields in (("POST", "/login", {"username": "admin", "password": PW,
                                                     "_csrf": CSRF}),
                                 ("GET", "/lang/tr?next=/", None),
                                 ("GET", "/auth/oidc/login", None)):
        st, h, _ = a.call(method, path, fields=fields, headers=base)
        assert st == 303, (path, st)
        out.update({k: "; secure" in v[1] for k, v in cookies(h).items()})
    assert len(out) == len(NAMES), out
    return out


def deleted(a: App, **headers) -> dict[str, bool]:
    """{cookie: Secure?} of what a sign-out deletes."""
    extra = {k.replace("_", "-"): v for k, v in headers.items()}
    prefix = csrf_name(extra)[:-len("ser2net_csrf")]
    st, h, _ = a.call("POST", "/login", fields={"username": "admin", "password": PW, "_csrf": CSRF},
                      headers={"Cookie": f"{prefix}ser2net_csrf={CSRF}", **extra})
    session = cookies(h)[f"{prefix}ser2net_session"][0]
    st, h, _ = a.call("POST", "/logout", fields={"_csrf": CSRF},
                      headers={"Cookie": f"{prefix}ser2net_csrf={CSRF}; "
                                         f"{prefix}ser2net_session={session}", **extra})
    assert st == 303, st
    gone = cookies(h)
    assert gone[f"{prefix}ser2net_session"][0] == "", gone
    assert "max-age=0" in gone[f"{prefix}ser2net_session"][1], gone
    return {k: "; secure" in v[1] for k, v in gone.items()}


def test_cookies() -> None:
    real = oidc_auth.build_authorize_url
    oidc_auth.build_authorize_url = lambda *a, **k: "https://idp.example/authorize?state=x"
    try:
        with App(_cfg()) as a:
            assert made(a) == named(False)
            for h in ({"x_forwarded_proto": "https"}, {"x_forwarded_proto": "HTTPS, http"},
                      {"forwarded": 'for=198.51.100.7;proto=https;host="s2n.example.com"'},
                      {"forwarded": "proto=https, proto=http"}):
                assert made(a, **h) == named(True), h
            for h in ({"x_forwarded_proto": "http"}, {"x_forwarded_proto": "http, https"},
                      {"forwarded": "for=1.2.3.4, proto=https"}, {"forwarded": "proto=http"}):
                assert made(a, **h) == named(False), h
            assert deleted(a) == {"ser2net_session": False, "ser2net_csrf": False}
            assert deleted(a, x_forwarded_proto="https") == {"__Host-ser2net_session": True,
                                                             "__Host-ser2net_csrf": True}
            # no HSTS through a proxy by default, none over plain HTTP
            st, h, _ = a.call("GET", "/login", headers={"X-Forwarded-Proto": "https"})
            assert header(h, "strict-transport-security") is None
        with App(_cfg(trusted_proxies=["127.0.0.1"])) as a:   # the scheme from the proxy
            assert made(a, x_forwarded_proto="https") == named(True)
        # admin TLS: Secure; named by the request's scheme, which is https over a real TLS
        # listener (test_hsts) and not in this process
        with App(_cfg(tls_cert="/c.pem", tls_key="/k.pem")) as a:
            assert made(a) == named(True, https=False)
    finally:
        oidc_auth.build_authorize_url = real
    print("cookies: Secure (and __Host- / __Secure-) with admin TLS, a https scheme or a first "
          "X-Forwarded-Proto / Forwarded proto= of https; not over plain HTTP  OK")


def test_proxy_hsts() -> None:
    cfg = AppConfig.from_dict({})
    assert cfg.admin_ui.proxy_hsts is False and cfg.to_dict()["admin_ui"]["proxy_hsts"] is False
    cfg = AppConfig.from_dict({"admin_ui": {"proxy_hsts": "yes"}})
    assert cfg.admin_ui.proxy_hsts is False and any("proxy_hsts" in n for n in cfg.load_notes)
    cfg.validate()
    with App(_cfg(hsts_max_age=600)) as a:
        st, h, _ = a.call("POST", "/login", fields={"username": "admin", "password": PW,
                                                    "_csrf": CSRF},
                          headers={"Cookie": f"ser2net_csrf={CSRF}"})
        jar = f"ser2net_csrf={CSRF}; ser2net_session={cookies(h)['ser2net_session'][0]}"
        st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
        assert st == 200 and 'name="proxy_hsts" >' in page, st
        assert "Send HSTS on HTTPS through the proxy" in page
        st, _, _ = a.call("POST", "/settings/proxy", fields={"_csrf": CSRF},   # (off: saved)
                          headers={"Cookie": jar})
        assert st == 303 and a.state.config.admin_ui.proxy_hsts is False
        st, _, _ = a.call("POST", "/settings/proxy", fields={"_csrf": CSRF, "proxy_hsts": "on"},
                          headers={"Cookie": jar})
        assert st == 303 and a.state.config.admin_ui.proxy_hsts is True, st
        st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
        assert a.state.store.load().admin_ui.proxy_hsts is True
        assert 'name="proxy_hsts" checked>' in page
        assert "reverse proxy settings: HSTS through the proxy on" in a.lines
        assert any(r["action"] == "reverse_proxy_set" and r["detail"] == "HSTS through the proxy on"
                   for r in a.state.read_audit())
        for headers, want in (({"X-Forwarded-Proto": "https"}, "max-age=600"),
                              ({"Forwarded": "proto=https"}, "max-age=600"),
                              ({}, None), ({"X-Forwarded-Proto": "http"}, None)):
            for path in ("/login", "/api/v1/health"):
                st, h, _ = a.call("GET", path, headers=headers)
                assert header(h, "strict-transport-security") == want, (path, headers)
        backups = sorted(os.listdir(os.path.join(a.tmp, "backups")))
        st, _, page = a.call("GET", f"/settings?restore={backups[-1]}", headers={"Cookie": jar})
        assert st == 200 and "HSTS through the reverse proxy: enabled → disabled" in page
    print("HSTS through a TLS proxy: off by default; when switched on, the Admin TLS max-age "
          "on HTTPS requests only  OK")


def main() -> None:
    for name in ("SER2NET_TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS", "SER2NET_PUBLIC_URL"):
        os.environ.pop(name, None)
    test_cookies()
    test_proxy_hsts()
    print("\nPASS: Secure cookies behind a TLS proxy")


if __name__ == "__main__":
    main()
