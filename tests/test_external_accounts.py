"""LDAP / OIDC accounts in the Users panel, with no directory or provider needed (the
accounts are written into config.json the way earlier sign-ins left them):

  * an LDAP/OIDC name is kept in one spelling (NFC, no white space around it); any
    printable name works, but one with control or invisible characters (a right-to-left
    override, a zero-width space), a name over 255 characters or one in parentheses
    (how the audit log marks "(no login)") can't be an account's;
  * every account can be signed out and deleted from the Users panel, also one named
    CORP\\jdoe or a/b#c?d%41: the forms send the name in a field, not in the URL (the
    older /settings/users/<name>/... URLs still work for plain names);
  * with open access, the visitor's pseudo-user "(anonymous)" is not an account of that
    name an older install may have: that account is not marked "you" and can be deleted;
  * an LDAP name in any letter case is one person: signing out or deleting the account
    covers the other spellings earlier versions made (never the last admin, never your
    own row), and a new local account can't differ from an existing name only in case;
  * each account's last sign-in comes from the audit log (also v2.7's oidc_login lines
    and LDAP names typed in another letter case) and every session issued since; it is
    shown in the Users panel. Removing unused LDAP/OIDC accounts is off by default
    (external_users_expire_days 0, also for an upgraded config): nothing goes. Once an
    admin sets 30-3650 days, accounts not signed in for that long are removed at once,
    at start-up and hourly — never admins, local accounts or the last accounts, and one
    with no sign-in on record only when the audit log goes back that far for its kind of
    sign-in — each logged and audited. Deleting one says it can come back while the
    directory allows it.

Run: python3 tests/test_external_accounts.py
"""
import html
import http.cookiejar
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import Browser, Server

from app.config import EXTERNAL_NAME_MAX, AppConfig, User, external_name, external_name_problem
from app.signins import SignIns, sign_in_of
from app.web import auth
from app.web.auth import hash_password

PW = "external-pass-1"


def local(name, role="admin"):
    return {"username": name, "password_hash": hash_password(PW), "role": role,
            "pwd_version": 1, "source": "local"}


def shadow(name, source="ldap", role="viewer", version=4):
    return {"username": name, "password_hash": "", "role": role, "pwd_version": version,
            "source": source}


def users(srv) -> dict:
    return {u["username"]: u for u in srv.saved_config()["users"]}


def browser_as(srv, name, version) -> Browser:
    """A browser with a session cookie of this (LDAP/OIDC) account."""
    b = Browser(srv.base)
    token = auth.issue_session(srv.saved_config()["secret_key"], 600, name, version)
    b.jar.set_cookie(http.cookiejar.Cookie(
        0, "ser2net_session", token, None, False, "127.0.0.1", False, False, "/", True,
        False, None, False, None, None, {}))
    return b


def test_names():
    assert external_name("  José ") == "José" and external_name("jdoe") == "jdoe"
    for ok in ("CORP\\jdoe", "a/b#c?d%41", "jdoe@corp.local", "John Smith", "İsmail Öztürk",
               "ali_x.com#EXT#@corp.onmicrosoft.com", "x" * EXTERNAL_NAME_MAX, "(x"):
        assert external_name_problem(ok) is None, ok
    for bad, why in (("", "empty"), ("x" * (EXTERNAL_NAME_MAX + 1), "longer than 255"),
                     ("jdoe\x00", "U+0000"), ("ad\u202emin", "U+202E"),
                     ("ad\u200bmin", "U+200B"), ("a\tb", "U+0009"), ("a\u2028b", "U+2028"),
                     ("(anonymous)", "parentheses"), ("(no login)", "parentheses")):
        problem = external_name_problem(bad)
        assert problem and why in problem, (bad, problem)
    print("LDAP/OIDC names: one spelling (NFC, trimmed); invisible characters, over-long "
          "names and names in parentheses refused  OK")


ODD = ("CORP\\jdoe", "a/b#c?d%41", "ali_x.com#EXT#@corp.onmicrosoft.com")


def test_manage_any_name():
    config = {"users": [local("admin"), *(shadow(n, "oidc" if "#" in n else "ldap") for n in ODD),
                        shadow("plain", "ldap")],
              "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:9",
                       "user_dn_template": "uid={username}"},
              "oidc": {"enabled": True, "issuer": "https://idp.invalid", "client_id": "c",
                       "client_secret": "s"}}
    with Server(config) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        _, _, page = admin.get("/settings")
        for name in ODD:
            field = f'<input type="hidden" name="username" value="{html.escape(name)}">'
            assert field in page, (name, page[page.find("<table"):][:3000])
        assert 'action="/settings/users/delete"' in page and "/settings/users/CORP" not in page

        st, _, page = admin.post("/settings/users/signout", {"username": "CORP\\jdoe"})
        assert st == 200 and "&#39;CORP\\jdoe&#39; was signed out everywhere." in page, page[-800:]
        assert users(srv)["CORP\\jdoe"]["pwd_version"] == 5
        for name in ODD:
            st, _, page = admin.post("/settings/users/delete", {"username": name})
            assert st == 200 and f"User &#39;{html.escape(name, quote=False)}&#39; deleted." in page, \
                (name, page[-800:])
            assert name not in users(srv), name
        # the older URL form still works for a plain name
        st, _, page = admin.post("/settings/users/plain/delete")
        assert st == 200 and "plain" not in users(srv), page[-500:]
        st, _, page = admin.post("/settings/users/delete", {"username": "nobody"})
        assert "User not found." in page
        st, _, page = admin.post("/settings/users/delete", {"username": "admin"})
        assert "You cannot delete your own account." in page and "admin" in users(srv)
    print("Users panel: accounts named CORP\\jdoe, a/b#c?d%41 or with #EXT# can be signed "
          "out and deleted; the older URLs still work  OK")


def test_anonymous_is_not_an_account():
    config = {"admin_ui": {"require_login": False, "anonymous_role": "admin"},
              "users": [local("admin"), shadow("(anonymous)", "oidc", "viewer")],
              "oidc": {"enabled": True, "issuer": "https://idp.invalid", "client_id": "c",
                       "client_secret": "s"}}
    with Server(config) as srv:
        visitor = Browser(srv.base)          # no sign-in: open access as admin
        st, _, page = visitor.get("/settings")
        assert st == 200, st
        row = page[page.find("<td class=\"mono\">(anonymous)"):][:2500]
        assert row and "you</span>" not in row.split("</tr>")[0], row[:600]
        assert 'name="username" value="(anonymous)"' in row, row[:2000]
        st, _, page = visitor.post("/settings/users/delete", {"username": "(anonymous)"})
        assert "You cannot delete your own account." not in page, page[-800:]
        assert "(anonymous)" not in users(srv)
    print("open access: the visitor is not the account named (anonymous)  OK")


def test_letter_case():
    config = {"users": [local("admin"), shadow("hkilic", role="admin", version=3),
                        shadow("Hkilic", role="admin", version=8), shadow("HKILIC", version=2),
                        shadow("jdoe")],
              "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:9",
                       "user_dn_template": "uid={username}"}}
    with Server(config) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        for name, existing in (("ADMIN", "admin"), ("Admin", "admin"), ("JDOE", "jdoe")):
            st, _, page = admin.post("/settings/users", {"username": name, "role": "viewer",
                                                         "password": PW, "password2": PW})
            assert f"User &#39;{existing}&#39; already exists." in page, (name, page[-600:])
        assert len(users(srv)) == 5
        st, _, page = admin.post("/settings/users/signout", {"username": "Hkilic"})
        assert [users(srv)[n]["pwd_version"] for n in ("hkilic", "Hkilic", "HKILIC")] == [4, 9, 3]
        # signed in as Hkilic: deleting hkilic takes HKILIC along, never your own row
        me = browser_as(srv, "Hkilic", 9)
        st, _, page = me.post("/settings/users/delete", {"username": "hkilic"})
        assert "Its accounts in another letter case were deleted too: HKILIC." in page, page[-800:]
        assert set(users(srv)) == {"admin", "Hkilic", "jdoe"}
        st, _, page = admin.post("/settings/users/delete", {"username": "Hkilic"})
        assert "deleted too" not in page and set(users(srv)) == {"admin", "jdoe"}
        log = open(os.path.join(srv.data, "audit.log"), encoding="utf-8").read()
        assert "\tuser_delete\thkilic\tHkilic" in log and "\tuser_delete\tHKILIC\tHkilic" in log
    print("letter case: an LDAP user's other spellings are signed out and deleted with it; "
          "a new local account can't differ only in case  OK")


NOW = time.time()


def record(days_ago, action, detail="", user="") -> dict:
    """An audit record (as AppState.read_audit gives it) from `days_ago` days back."""
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - days_ago * 86400)),
            "ip": "10.0.0.7", "action": action, "detail": detail, "user": user}


def test_sign_ins():
    assert sign_in_of(record(1, "login", "viewer, ldap", "jdoe")) == ("jdoe", "ldap")
    assert sign_in_of(record(1, "oidc_login", "a@b.c", "a@b.c")) == ("a@b.c", "oidc")
    assert sign_in_of(record(1, "oidc_login", "v27.user")) == ("v27.user", "oidc")  # v2.7
    assert sign_in_of(record(1, "first_run_setup", "admin", "admin")) == ("admin", "local")
    assert sign_in_of(record(1, "first_run_setup", "boss", "(command line)")) is None
    assert sign_in_of(record(1, "user_create", "x:viewer", "admin")) is None
    assert sign_in_of(record(1, "login_failed", "", "jdoe")) is None

    seen = SignIns()
    assert seen.expired([User("x", source="ldap", role="viewer")], 90, NOW) == []  # not read yet
    seen.load([record(1, "login", "viewer, ldap", "fresh"),       # newest first, like the log
               record(5, "login", "admin, local", "admin"),
               record(100, "login", "viewer, ldap", "Stale"),     # typed with a capital
               record(150, "oidc_login", "old.sso", "old.sso"),
               record(200, "oidc_login", "v27.user"),
               record(300, "first_run_setup", "boss", "(command line)"),
               record(400, "login", "admin, ldap", "ldap.admin")])
    day = 86400
    assert abs(seen.last(User("STALE", source="ldap")) - (NOW - 100 * day)) < 2
    assert seen.last(User("stale", source="oidc")) is None          # OIDC: exact names
    assert abs(seen.last(User("v27.user", source="oidc")) - (NOW - 200 * day)) < 2
    assert seen.last(User("boss")) is None                          # made, never signed in
    users = [User("admin", role="admin"), User("fresh", source="ldap", role="viewer"),
             User("stale", source="ldap", role="operator"), User("old.sso", source="oidc", role="viewer"),
             User("v27.user", source="oidc", role="viewer"), User("never", source="oidc", role="viewer"),
             User("never2", source="ldap", role="viewer"), User("ldap.admin", source="ldap", role="admin"),
             User("boss", role="viewer")]
    gone = [u.username for u in seen.expired(users, 90, NOW)]
    assert gone == ["stale", "old.sso", "v27.user", "never", "never2"], gone
    assert [u.username for u in seen.expired(users, 180, NOW)] == ["v27.user", "never", "never2"]
    assert seen.expired(users, 0, NOW) == []
    # a sign-in now counts at once
    seen.note("stale", "ldap")
    assert "stale" not in [u.username for u in seen.expired(users, 90, NOW)]
    # an account without a sign-in on record: only once the log goes back that far
    short = SignIns()
    short.load([record(10, "login", "viewer, ldap", "fresh")])
    assert short.expired([User("never2", source="ldap", role="viewer"), User("admin", role="admin")],
                         90, NOW) == []
    # never the last accounts (setup would open again)
    assert seen.expired([User("stale", source="ldap", role="viewer")], 90, NOW) == []
    # the setting: off unless an admin sets it (a new config, or one without the key);
    # odd values corrected with a note
    assert AppConfig().external_users_expire_days == 0
    assert AppConfig.from_dict({"users": []}).external_users_expire_days == 0
    assert seen.expired(users, AppConfig.from_dict({}).external_users_expire_days, NOW) == []
    for value, days in ((0, 0), (45, 45), (-3, 0), (7, 30), (99999, 3650), ("x", 0)):
        cfg = AppConfig.from_dict({"external_users_expire_days": value})
        assert cfg.external_users_expire_days == days, (value, cfg.external_users_expire_days)
        assert bool(cfg.load_notes) == (value not in (0, 45)), (value, cfg.load_notes)
    assert AppConfig().to_dict()["external_users_expire_days"] == 0
    print("sign-ins: from the audit log (v2.7 lines, LDAP in any letter case); unused LDAP/OIDC "
          "accounts found — never admins, local or the last accounts  OK")


def audit_line(days_ago, action, detail, user) -> str:
    r = record(days_ago, action, detail, user)
    return "\t".join((r["time"], r["ip"], action, detail, user)) + "\n"


def test_expiry():
    config = {"users": [local("admin"), shadow("fresh"), shadow("stale"), shadow("month.old"),
                        shadow("gone.sso", "oidc"), shadow("boss.sso", "oidc", role="admin")],
              "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:9",
                       "user_dn_template": "uid={username}"},
              "oidc": {"enabled": True, "issuer": "https://idp.invalid", "client_id": "c",
                       "client_secret": "s"}}
    everyone = {"admin", "fresh", "stale", "month.old", "gone.sso", "boss.sso"}
    srv = Server(config)    # no external_users_expire_days: an upgraded config
    with open(os.path.join(srv.data, "audit.log"), "w", encoding="utf-8") as fh:
        fh.write(audit_line(200, "oidc_login", "someone.else", "someone.else")
                 + audit_line(120, "login", "viewer, ldap", "Stale")
                 + audit_line(45, "login", "viewer, ldap", "month.old")
                 + audit_line(2, "login", "viewer, ldap", "fresh"))
    with srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        for _ in range(100):   # the audit log is read at start-up
            _, _, page = admin.get("/settings")
            if f"last sign-in: {record(2, '')['time'][:16]}" in page:
                break
            time.sleep(0.1)
        # off by default: every account stays
        assert set(users(srv)) == everyone, users(srv)
        assert 'name="expire_days" min="0" max="3650" value="0"' in page
        assert "Off by default: an account that stops signing in stays until you delete it." in page
        today = time.strftime("%Y-%m-%d")
        assert f"last sign-in: {today}" in page.split("boss.sso")[0].split("<td class=\"mono\">admin")[1]
        assert f"last sign-in: {record(2, '')['time'][:16]}" in page
        assert page.split("<td class=\"mono\">boss.sso")[1].find("last sign-in: —") >= 0
        assert "whether that person can sign in again is up to the directory" in page

        for bad in ("10", "abc", "4000", "-1"):
            st, _, page = admin.post("/settings/users/expiry", {"expire_days": bad})
            assert "Enter 0 (never) or a number of days from 30 to 3650." in page, bad
        assert srv.saved_config()["external_users_expire_days"] == 0
        # turned on: applied at once
        st, _, page = admin.post("/settings/users/expiry", {"expire_days": "90"})
        assert ("Saved: LDAP / OIDC accounts that haven’t signed in for 90 days are removed "
                "(checked every hour). Removed now: stale, gone.sso.") in page, page[-800:]
        assert set(users(srv)) == {"admin", "fresh", "month.old", "boss.sso"}, users(srv)
        assert srv.saved_config()["external_users_expire_days"] == 90
        log = open(os.path.join(srv.data, "all.log"), encoding="utf-8").read()
        assert (f"LDAP account 'stale' (viewer) removed: no sign-in for 90 days (last: "
                f"{record(120, '')['time'][:10]}). It is made again at their next sign-in while "
                f"LDAP still gives them a role.") in log, log[-2000:]
        assert "OIDC account 'gone.sso' (viewer) removed: no sign-in for 90 days (last: none on record)" in log
        audit = open(os.path.join(srv.data, "audit.log"), encoding="utf-8").read()
        assert "\tusers_expiry\t90\tadmin" in audit and \
            "\tuser_expired\tstale: ldap, no sign-in for 90 days" in audit, audit[-800:]
        assert audit.rstrip("\n").endswith("\t(maintenance)"), audit[-300:]

        # once on, the pass at start-up (and every hour) removes them
        srv.stop()
        cfg = srv.saved_config()
        cfg["external_users_expire_days"] = 30
        with open(srv.config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        srv.start()
        removed = "LDAP account 'month.old' (viewer) removed: no sign-in for 30 days"
        for _ in range(100):   # config.json is saved first, then each removal logged
            if removed in open(os.path.join(srv.data, "all.log"), encoding="utf-8").read():
                break
            time.sleep(0.1)
        assert set(users(srv)) == {"admin", "fresh", "boss.sso"}, users(srv)
        assert removed in open(os.path.join(srv.data, "all.log"), encoding="utf-8").read()

        admin = Browser(srv.base)
        admin.login("admin", PW)
        st, _, page = admin.post("/settings/users/expiry", {"expire_days": "0"})
        assert "never removed for not signing in" in page
        assert srv.saved_config()["external_users_expire_days"] == 0
        st, _, page = admin.post("/settings/users/delete", {"username": "fresh"})
        assert ("User &#39;fresh&#39; deleted. They are signed out; while LDAP still gives them "
                "a role, their next sign-in makes a new account.") in page, page[-800:]
    print("unused LDAP/OIDC accounts: kept by default; once turned on, removed at once and at "
          "start-up (logged, audited), never admins; Users shows the last sign-in  OK")


def main():
    test_names()
    test_manage_any_name()
    test_anonymous_is_not_an_account()
    test_letter_case()
    test_sign_ins()
    test_expiry()
    print("\nPASS: LDAP/OIDC accounts in the Users panel")


if __name__ == "__main__":
    main()
