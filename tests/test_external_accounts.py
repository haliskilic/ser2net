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
    name an older install may have: that account is not marked "you" and can be deleted.

Run: python3 tests/test_external_accounts.py
"""
import html
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import Browser, Server

from app.config import EXTERNAL_NAME_MAX, external_name, external_name_problem
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


def test_names():
    assert external_name("  José ") == "José" and external_name("jdoe") == "jdoe"
    for ok in ("CORP\\jdoe", "a/b#c?d%41", "jdoe@corp.local", "John Smith", "İsmail Öztürk",
               "ali_x.com#EXT#@corp.onmicrosoft.com", "x" * EXTERNAL_NAME_MAX, "(x"):
        assert external_name_problem(ok) is None, ok
    for bad, why in (("", "empty"), ("x" * (EXTERNAL_NAME_MAX + 1), "longer than 255"),
                     ("jdoe\x00", "U+0000"), ("ad‮min", "U+202E"),
                     ("ad​min", "U+200B"), ("a\tb", "U+0009"), ("a b", "U+2028"),
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


def main():
    test_names()
    test_manage_any_name()
    test_anonymous_is_not_an_account()
    print("\nPASS: LDAP/OIDC accounts in the Users panel")


if __name__ == "__main__":
    main()
