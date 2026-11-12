"""How long the browser is told to use only HTTPS (Strict-Transport-Security):

  * admin_ui.hsts_max_age: one year (31536000 s) by default, as before it could be set,
    also for a config.json without the key (v2.7 / v2.8); 0 sends max-age=0, which makes
    browsers forget it; a hand-edited value that isn't a whole number of seconds from 0
    to two years is put back to the default, with a load note;
  * the header is sent only with admin TLS, with the configured value, to pages and the
    REST API, and a change applies at once (behind a TLS proxy: test_secure_cookies);
    the cookies of a TLS listener are Secure;
  * Settings -> Admin TLS sets it (checked there: whole seconds, 0 to two years), says
    what it covers, and a form without the field (an older page) keeps it; all.log and
    the audit log note a change; a backup's restore preview shows a different value.

The real-server part over TLS needs cryptography (for the certificate) and runs the
form part over plain HTTP without it.

Run: python3 tests/test_hsts.py
"""
import http.cookiejar
import json
import os
import ssl
import urllib.request
from types import SimpleNamespace

from _optional import need
from _server import Browser, Server, _NoRedirect, ui_cookie

from app.config import HSTS_MAX_AGE_DEFAULT, HSTS_MAX_AGE_MAX, AdminUI, AppConfig
from app.web.auth import hash_password
from app.web.server import GuardMiddleware

PW = "hsts-pass-1"
YEAR = 31536000


def test_config() -> None:
    assert HSTS_MAX_AGE_DEFAULT == YEAR and HSTS_MAX_AGE_MAX == 2 * YEAR
    cfg = AppConfig.from_dict({"admin_ui": {"port": 8080}})   # v2.7 / v2.8: no such key
    assert cfg.admin_ui.hsts_max_age == YEAR and not cfg.load_notes, cfg.load_notes
    for value in (0, 1, 86400, 2 * YEAR):
        cfg = AppConfig.from_dict({"admin_ui": {"hsts_max_age": value}})
        assert cfg.admin_ui.hsts_max_age == value and not cfg.load_notes, value
        assert cfg.to_dict()["admin_ui"]["hsts_max_age"] == value
    for value in (-1, 2 * YEAR + 1, "86400", 1.5, True, None, [1]):
        cfg = AppConfig.from_dict({"admin_ui": {"hsts_max_age": value}})
        assert cfg.admin_ui.hsts_max_age == YEAR, value
        assert any(n.startswith(f"admin_ui.hsts_max_age {value!r} is not a whole number")
                   for n in cfg.load_notes), (value, cfg.load_notes)
        cfg.validate()   # a save is never refused for it
    print("config: one year by default (also without the key), 0 to two years kept, "
          "anything else back to the default with a load note  OK")


def test_header() -> None:
    def sent(**admin) -> str | None:
        response = SimpleNamespace(headers={})
        GuardMiddleware._security_headers(response, SimpleNamespace(admin_ui=AdminUI(**admin)),
                                          "/")
        return response.headers.get("Strict-Transport-Security")

    assert sent() is None                                       # no TLS: no HSTS
    assert sent(hsts_max_age=0) is None
    tls = {"tls_cert": "/c.pem", "tls_key": "/k.pem"}
    assert sent(**tls) == f"max-age={YEAR}"
    assert sent(**tls, hsts_max_age=86400) == "max-age=86400"
    assert sent(**tls, hsts_max_age=0) == "max-age=0"
    print("header: only with TLS, max-age as configured, 0 sends max-age=0  OK")


def test_form(srv, b) -> None:
    def saved():
        return srv.saved_config()["admin_ui"].get("hsts_max_age")

    admin = srv.saved_config()["admin_ui"]
    tls = {"tls_cert": admin.get("tls_cert", ""), "tls_key": admin.get("tls_key", "")}
    st, _, page = b.get("/settings")
    assert st == 200 and 'name="hsts_max_age"' in page and f'value="{YEAR}"' in page
    assert f'max="{2 * YEAR}"' in page and "on every port" in page
    st, _, page = b.post("/settings/tls", dict(tls, hsts_max_age="86400"))
    assert st == 200 and "Admin TLS saved" in page and saved() == 86400, (st, saved())
    for bad in ("-1", "abc", "1.5", "", str(2 * YEAR + 1), "٣"):
        st, _, page = b.post("/settings/tls", dict(tls, hsts_max_age=bad))
        assert st == 400 and "must be a whole number of seconds from 0 to 63072000" in page, bad
        assert saved() == 86400, bad
    st, _, page = b.post("/settings/tls", tls)             # an older page: no such field
    assert st == 200 and "Admin TLS saved" in page and saved() == 86400
    st, _, page = b.post("/settings/tls", dict(tls, hsts_max_age="0"))
    assert st == 200 and saved() == 0
    log = srv.output()
    assert "admin TLS: HSTS max-age set to 86400 s" in log, log[-1500:]
    assert "admin TLS: HSTS max-age set to 0 s (max-age=0: browsers forget it)" in log
    with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
        audit = fh.read()
    assert "(HSTS 86400 s)" in audit and "(HSTS 0 s)" in audit, audit[-600:]
    # a backup made before (one year) shows up in the restore preview
    backups = sorted(os.listdir(os.path.join(srv.data, "backups")))
    st, _, page = b.get(f"/settings?restore={backups[0]}")
    assert st == 200 and f"HSTS max-age (seconds): 0 → {YEAR}" in page, page[-3000:]
    print("Settings → Admin TLS: set, checked, kept by an older form, logged and audited; "
          "the restore preview shows it  OK")


def tls_browser(base: str) -> Browser:
    b = Browser(base)
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # the test's own certificate
    b.jar = http.cookiejar.CookieJar()
    b.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(b.jar),
                                       urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    return b


def main():
    test_config()
    test_header()
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    if not need("cryptography", what="HSTS over admin TLS (the form part runs over HTTP)"):
        with Server({"users": users}) as srv:
            b = Browser(srv.base)
            b.login("admin", PW)
            test_form(srv, b)
        print("\nPASS: HSTS max-age")
        return
    from app.web.selfsigned import generate
    srv = Server({"users": users})
    cert, key = os.path.join(srv.tmp, "cert.pem"), os.path.join(srv.tmp, "key.pem")
    generate(cert, key)
    with open(srv.config_path, "w", encoding="utf-8") as fh:
        json.dump({"users": users, "admin_ui": {"tls_cert": cert, "tls_key": key}}, fh)
    with srv:
        b = tls_browser(f"https://127.0.0.1:{srv.port}")
        b.login("admin", PW)
        b.get("/lang/en?next=/settings")
        # the cookies of a TLS listener are Secure (the browser never sends them over
        # http://), named __Host-…: an http:// address of the same name can't set those
        made = {c.name: c.secure for c in b.jar}
        assert made == {ui_cookie(n, srv.port, "https"): True
                        for n in ("ser2net_csrf", "ser2net_session", "ser2net_lang")}, made
        assert all(n.startswith("__Host-ser2net_") for n in made), made
        for path in ("/login", "/settings"):
            st, h, _ = b.get(path)
            assert st == 200 and h.get("Strict-Transport-Security") == f"max-age={YEAR}", path
        test_form(srv, b)
        # the change applies at once: pages and the REST API (an auth error too)
        for path in ("/", "/settings", "/api/v1/mappings"):
            st, h, _ = b.get(path)
            assert h.get("Strict-Transport-Security") == "max-age=0", (path, st)
        print("over TLS: Secure cookies; HSTS one year by default, the new value at once on "
              "pages and the REST API  OK")
    print("\nPASS: HSTS max-age")


if __name__ == "__main__":
    main()
