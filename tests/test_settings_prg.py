"""Settings forms: Post/Redirect/Get, a refused form shown again, and where it says so.

Every Settings form used to answer with the page itself, at the form's URL: the TR / EN
switch then led to that URL (a plain 405, a 404 after a restore), a reload or Back posted
the form again (a new REST API token, a new certificate, a restore that restarts every
mapping), a refused form came back filled in from the stored settings (what was typed was
lost) with HTTP 200, and every message was at the top of the page. Checks:
  * a form that did its work answers 303 to /settings#<its section>; that page says so in
    the section, once (the next visit shows no message), and its TR / EN links lead back
    to /settings;
  * a new REST API token / cluster key: never in the redirect, shown once on the page it
    leads to (Cache-Control: no-store), not in another browser or to another user;
  * "Save and send a test" whose test fails: saved, so a redirect too, with the error;
  * a refused form: 400 with the reason in its section, filled in with what was sent but
    never a password or key (it says so), data-page-url and TR / EN links to /settings;
  * the import preview: 303 to /settings?import=<id>#import, shown again on a reload or a
    language switch, to that user only; applying it redirects; the one-step import of 2.7
    scripts still answers the page (200);
  * a default language saved for visitors who haven't picked one: the message after the
    redirect is in that language;
  * the flash store itself: one browser and user, used once, expires.

Run: python3 tests/test_settings_prg.py
"""
import json
import os
import re
import urllib.parse
import urllib.request
from unittest import mock

from _server import Browser, Server, free_port

from app.config import ConfigStore
from app.web.auth import hash_password
from app.web.routes import settings as settings_routes

PW = "prg-pass-123"


def test_flash_store():
    flash = settings_routes._Flash()
    flash.put("browser-a", "admin", section="api-token", ok="done", error=None, new_api_token="t")
    assert flash.take("browser-b", "admin") is None, "another browser"
    assert flash.take("browser-a", "admin")["new_api_token"] == "t"
    assert flash.take("browser-a", "admin") is None, "used once"
    flash.put("browser-a", "admin", section="x", ok="done", error=None)
    assert flash.take("browser-a", "op") is None, "another user in that browser"
    assert flash.take("browser-a", "admin") is None, "...and then gone for the first one too"
    flash.put("", "admin", section="x", ok="done", error=None)
    assert flash.take("", "admin") is None, "no browser cookie: nothing kept"
    now = 1000.0
    with mock.patch.object(settings_routes.time, "monotonic", lambda: now):
        flash.put("browser-a", "admin", section="x", ok="old", error=None)
        now += settings_routes.FLASH_TTL_S + 1
        assert flash.take("browser-a", "admin") is None, "expired"
        for i in range(150):
            flash.put(f"b{i}", "admin", section="x", ok=str(i), error=None)
        assert len(flash._items) <= 100 and flash.take("b149", "admin")["ok"] == "149"
    print("flash: per browser and user, used once, expires, bounded  OK")


def test_sections_exist():
    """Every section a Settings form names has its place for the message, with that id."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    with open(os.path.join(root, "app", "web", "routes", "settings.py"), encoding="utf-8") as fh:
        code = fh.read()
    with open(os.path.join(root, "app", "web", "templates", "settings.html"), encoding="utf-8") as fh:
        html = fh.read()
    named = set(re.findall(r'(?:_done|_failed|refused)\(request, "([a-z-]+)"', code))
    named |= set(re.findall(r'section="([a-z-]+)"', code))
    known = re.search(r"section if section in \[([^\]]*)\]", html).group(1)
    assert len(named) >= 10, named
    for sid in named:
        assert f"{{{{ feedback('{sid}') }}}}" in html and f'id="{sid}"' in html, sid
        assert f"'{sid}'" in known, sid
    print(f"sections: each of the {len(named)} a form names has its message place  OK")


def section(page: str, sid: str) -> str:
    """The <section id=sid> of a page."""
    start = page.index(f'id="{sid}"')
    return page[start:page.index("</section>", start)]


def feedback(page: str) -> list[str]:
    return re.findall(r'<div class="banner (?:ok|error)" role="(?:status|alert)" tabindex="-1" '
                      r'data-feedback>(.*?)</div>', page, re.S)


def lang_next(page: str) -> str:
    """Where the page's TR link leads back to."""
    m = re.search(r'href="/lang/tr\?next=([^"]*)"', page)
    return urllib.parse.unquote(m.group(1).replace("&amp;", "&"))


def done(b: Browser, path: str, fields: dict, sid: str, **kw) -> str:
    """Post a form that does its work: 303 to its section; the page it leads to."""
    st, h, body = b.post(path, fields, follow=False, **kw)
    assert st == 303 and h.get("Location") == f"/settings#{sid}", (path, st, h.get("Location"),
                                                                   body[:300])
    st, h, page = b.get("/settings")
    assert st == 200, st
    return page


def refused(b: Browser, path: str, fields: dict, sid: str) -> str:
    st, _h, page = b.post(path, fields, follow=False)
    assert st == 400, (path, st, page[:300])
    assert f'<main data-page-url="/settings#{sid}">' in page, "the page's own address"
    assert lang_next(page) == f"/settings#{sid}", lang_next(page)
    msgs = feedback(page)
    assert len(msgs) == 1 and msgs[0] in section(page, sid), (sid, msgs)
    return page


def main():
    test_flash_store()
    test_sections_exist()
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "admin2", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "op", "password_hash": hash_password(PW), "role": "operator", "pwd_version": 1},
             # a directory account: the Users panel shows the account-expiry form
             {"username": "dir.user", "source": "ldap", "role": "viewer", "pwd_version": 1}]
    m = {"id": "aaaaaaaaaaa1", "name": "A", "enabled": False, "kind": "net",
         "serial": {"port": "/dev/ser2net-prg-1"},
         "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": free_port()}}
    with Server({"users": users, "mappings": [m]}) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)

        # ---- done: 303 to the section, said there, once ----
        page = done(admin, "/settings/users", {"username": "u1", "role": "viewer",
                                               "password": "user-pass-1", "password2": "user-pass-1"},
                    "users")
        assert feedback(page) == ["User &#39;u1&#39; created."], feedback(page)
        assert "u1" in section(page, "users") and "created" in section(page, "users")
        assert "created" not in section(page, "account"), "not at the top of the page"
        assert lang_next(page) == "/settings" and "data-page-url" not in page
        page = admin.get("/settings")[2]
        assert feedback(page) == [], "the message is shown once"
        st, h, _ = admin.get("/lang/tr?next=" + urllib.parse.quote(lang_next(page), safe=""))
        assert st == 303 and h.get("Location") == "/settings" and admin.get("/settings")[0] == 200
        admin.get("/lang/en?next=/settings")
        print("a form that did its work: 303 to its section, said there once; TR / EN lead "
              "back to /settings  OK")

        # ---- the REST API token: shown once, not via the URL, not elsewhere ----
        st, h, body = admin.post("/settings/api-token", {"api_token_role": "operator"}, follow=False)
        assert st == 303 and h.get("Location") == "/settings#api-token"
        assert "s2n_" not in h.get("Location") + body
        other = Browser(srv.base)        # the same account in another browser
        other.login("admin", PW)
        assert "s2n_" not in other.get("/settings")[2], "another browser must not get the token"
        st, h, page = admin.get("/settings")
        token = re.search(r'<div class="mono token-box">(s2n_[A-Za-z0-9_\-]+)</div>', page).group(1)
        assert h.get("Cache-Control") == "no-store", h.get("Cache-Control")
        assert token in section(page, "api-token") and "New API token generated" in section(page, "api-token")
        req = urllib.request.Request(srv.base + "/api/v1/status",
                                     headers={"Authorization": f"Bearer {token}"})
        assert urllib.request.urlopen(req, timeout=10).status == 200
        st, h, page = admin.get("/settings")
        # (every page is no-store since 2.9: GuardMiddleware._security_headers)
        assert "s2n_" not in page, "shown only once"
        # the flash belongs to the user: someone else signing in in that browser sees nothing
        shared = Browser(srv.base)
        shared.login("admin", PW)
        shared.post("/settings/api-token", {"api_token_role": "viewer"}, follow=False)
        shared.login("admin2", PW)
        page = shared.get("/settings")[2]
        assert "REST API token" in page and "s2n_" not in page, "another user in the same browser"
        print("REST API token: not in the redirect, shown once (no-store), only to that browser "
              "and user  OK")

        # ---- a generated cluster key: the same ----
        disc = str(free_port())
        st, h, body = admin.post("/settings/cluster", {"cluster_form": "2", "enabled": "on",
                                                       "discovery_port": disc, "then": "generate"},
                                 follow=False)
        assert st == 303 and h.get("Location") == "/settings#cluster-settings"
        key = srv.saved_config()["cluster"]["key"]
        assert key not in h.get("Location") + body
        st, h, page = admin.get("/settings")
        assert f'<div class="mono token-box">{key}</div>' in section(page, "cluster-settings")
        assert h.get("Cache-Control") == "no-store" and key not in admin.get("/settings")[2]
        print("generated cluster key: shown once on the page the redirect leads to  OK")

        # ---- refused forms: 400, the reason in the section, what was sent shown again ----
        ldap = {"ldap_enabled": "on", "ldap_server_uri": "ldap://typed.example", "ldap_tls_shown": "1",
                "ldap_bind_password": "typed-bind-secret", "ldap_admin_group": "typed-admins",
                "ldap_nested_groups_shown": "1"}
        page = refused(admin, "/settings/ldap", ldap, "ldap")
        form = section(page, "ldap")
        assert "LDAP needs either a user DN template" in form and "type them once more" in form
        assert 'value="ldap://typed.example"' in form and 'value="typed-admins"' in form
        assert 'name="ldap_enabled" checked' in form
        assert "typed-bind-secret" not in page, "a password never goes into the page"
        assert srv.saved_config()["ldap"]["server_uri"] == "", "nothing was saved"

        oidc = {"oidc_enabled": "on", "oidc_issuer": "http://idp.typed.example",
                "oidc_client_id": "typed-client", "oidc_client_secret": "typed-oidc-secret"}
        page = refused(admin, "/settings/oidc", oidc, "oidc")
        form = section(page, "oidc")
        assert "OIDC issuer must be an https:// URL." in form
        assert 'value="http://idp.typed.example"' in form and 'value="typed-client"' in form
        assert "typed-oidc-secret" not in page

        notify = {"notify_enabled": "on", "notify_event_device_lost": "on",
                  "notify_webhook_url": "ftp://typed.example/hook", "notify_email_to": "ops@typed.example",
                  "notify_smtp_host": "smtp.typed.example", "notify_smtp_port": "2525",
                  "notify_repeat_min": "7", "notify_webhook_format": "slack"}
        page = refused(admin, "/settings/notifications", notify, "notifications")
        form = section(page, "notifications")
        assert "The webhook URL must start with https://" in form
        assert 'value="ops@typed.example"' in form and 'value="smtp.typed.example"' in form
        assert 'value="2525"' in form and 'value="7"' in form and '<option value="slack" selected' in form
        assert "ftp://typed.example" not in page, "the webhook URL is kept secret"

        page = refused(admin, "/settings/cluster", {"cluster_form": "2", "enabled": "on",
                                                    "discovery_port": disc, "advertise_ip": "not-an-ip",
                                                    "peers": "10.0.0.7:8080\nnot a peer!",
                                                    "key": "typed-cluster-key-0123456789"},
                       "cluster-settings")
        form = section(page, "cluster-settings")
        assert 'value="not-an-ip"' in form and "10.0.0.7:8080\nnot a peer!</textarea>" in form
        assert 'name="keep_old_key">' in form, "an unticked 'keep the current key' stays unticked"
        assert "typed-cluster-key" not in page and srv.saved_config()["cluster"]["key"] == key

        cert = "/nonexistent/typed-cert.pem"
        page = refused(admin, "/settings/tls", {"tls_cert": cert, "tls_key": "/nonexistent/k.pem"}, "tls")
        assert f'value="{cert}"' in section(page, "tls") and "TLS file not found" in section(page, "tls")

        page = refused(admin, "/settings/access", {"anonymous_role": "operator", "session_minutes": "1",
                                                   "default_language": "tr"}, "access")
        form = section(page, "access")
        assert 'name="require_login" ' in form and 'name="require_login" checked' not in form
        assert '<option value="operator" selected' in form and 'value="1" required' in form
        assert '<option value="tr" selected' in form
        assert srv.saved_config()["admin_ui"]["require_login"] is True

        page = refused(admin, "/settings/users", {"username": "u2", "role": "operator",
                                                  "password": "user-pass-1", "password2": "other-pass-1"},
                       "users")
        create = page[page.index('action="/settings/users" '):]
        create = create[:create.index("</form>")]
        assert 'name="username" value="u2"' in create and "Passwords do not match." in page
        assert '<option value="operator" selected>' in create, "the role chosen stays"
        assert '<option value="viewer" selected>' not in create
        page = refused(admin, "/settings/users/password", {"username": "u1", "password": "short",
                                                           "password2": "short"}, "users")
        assert '<option value="u1" selected>' in page, "the account chosen stays chosen"
        page = refused(admin, "/settings/users/expiry", {"expire_days": "3"}, "users")
        assert 'value="3" required' in page
        print("refused forms: 400, the reason in their section, filled in with what was sent "
              "(never a password or key); the page names /settings as its address  OK")

        # ---- saved, but the test notification failed: a redirect, with the error ----
        dead = free_port()
        page = done(admin, "/settings/notifications",
                    {"notify_enabled": "on", "notify_event_device_lost": "on",
                     "notify_webhook_url": f"http://127.0.0.1:{dead}/hook", "then": "test"},
                    "notifications")
        msgs = feedback(page)
        assert len(msgs) == 1 and "Saved, but the test failed" in msgs[0], msgs
        assert 'role="alert"' in section(page, "notifications")
        assert srv.saved_config()["notifications"]["enabled"] is True
        print("Save and send a test, test failed: saved, redirected, the error in its section  OK")

        # ---- the import preview: its own address ----
        export = json.dumps({"version": 1, "mappings": [dict(m, id="ffffffffff01", name="B",
                                                             network=dict(m["network"], port=free_port()))]})
        st, h, _ = admin.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                              files={"file": ("m.json", export.encode())}, follow=False)
        loc = h.get("Location") or ""
        assert st == 303 and re.fullmatch(r"/settings\?import=[A-Za-z0-9_\-]+#import", loc), (st, loc)
        url = loc.split("#")[0]
        for _ in range(2):   # a reload shows it again
            page = admin.get(url)[2]
            assert "Import preview" in section(page, "import")
        assert lang_next(page) == url, "the language switch keeps the preview"
        op = Browser(srv.base)
        op.login("op", PW)
        page = op.get(url)[2]
        assert "expired" in section(page, "import") and "Import preview" not in page, "another user"
        token = re.search(r'name="token" value="([^"]+)"', admin.get(url)[2]).group(1)
        page = done(admin, "/settings/config/import/apply", {"token": token, "mode": "merge"}, "import")
        assert "Imported: 1 added" in section(page, "import")
        assert "expired" in section(admin.get(url)[2], "import"), "applied: gone"
        st, _h, page = admin.post("/settings/config/import", {}, follow=False,
                                  files={"file": ("m.json", export.encode())})
        assert st == 200 and "Imported 1 mapping (replaced" in section(page, "import"), (st, page[:300])
        assert '<main data-page-url="/settings#import">' in page
        print("import preview: 303 to its own address (reload, language, that user only); the "
              "one-step import still answers the page  OK")

        # ---- a restore (it restarts every mapping): redirected too ----
        name = ConfigStore(srv.config_path).backup_names()[0]
        page = done(admin, "/settings/backups/restore", {"name": name}, "backups")
        assert "Restored" in section(page, "backups") and feedback(page)[0].startswith("Restored")
        print("restoring a backup: 303 to the backups, said there  OK")

        # ---- a saved default language: the message after the redirect is in it ----
        fresh = Browser(srv.base)          # no language picked: the default applies
        fresh.login("admin", PW)
        page = done(fresh, "/settings/access", {"require_login": "on", "anonymous_role": "viewer",
                                                "session_minutes": "480", "default_language": "tr"},
                    "access")
        assert feedback(page) == ["Web arayüzü için giriş yeniden zorunlu."], feedback(page)
        assert '<html lang="tr">' in page
        done(fresh, "/settings/access", {"require_login": "on", "anonymous_role": "viewer",
                                         "session_minutes": "480", "default_language": "auto"}, "access")
        print("a saved default language: the page after the redirect says so in that language  OK")
    print("\nPASS: Settings forms redirect after a change, a refused one keeps what was typed")


if __name__ == "__main__":
    main()
