"""Ending web-UI sessions: which actions sign a session out, and that nothing brings
an ended one back.

  * a new account starts at a random session version, so deleting a user and creating
    the same name again (even with another role) does not revive the old cookies;
  * logout ends that session on the server: a copy of its cookie is refused, the same
    user's other sessions stay;
  * switching LDAP (or OIDC) off ends its accounts' sessions — but is refused while it
    would leave no admin who can sign in;
  * an admin can sign a user out everywhere, or set a local user's new password (which
    signs them out too); anyone can sign out their own other sessions;

Run: python3 tests/test_session_revocation.py
"""
import http.cookiejar
import urllib.error
import urllib.request

from _server import Browser, Server

from app.config import AppConfig, new_pwd_version
from app.web import auth
from app.web.auth import hash_password

PW = "revoke-pass-1"
LDAP_ON = {"enabled": True, "server_uri": "ldap://127.0.0.1:9", "user_dn_template": "uid={username}"}


def user(name, role):
    return {"username": name, "password_hash": hash_password(PW), "role": role, "pwd_version": 1}


def directory_user(name, role, source="ldap"):
    return {"username": name, "password_hash": "", "role": role, "pwd_version": 5, "source": source}


def session_for(srv, name, version=5):
    """A valid session cookie for an LDAP/OIDC shadow account (no directory needed)."""
    return auth.issue_session(srv.saved_config()["secret_key"], 600, name, version)


def browser_with(srv, token: str) -> Browser:
    """A browser signed in with this session cookie."""
    b = Browser(srv.base)
    b.jar.set_cookie(http.cookiejar.Cookie(
        0, "ser2net_session", token, None, False, "127.0.0.1", False, False, "/", True,
        False, None, False, None, None, {}))
    return b


def cookie_of(b: Browser) -> str:
    return b.cookie("ser2net_session").value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def signed_in(srv, token: str) -> bool:
    """Does a request that carries only this session cookie get the settings page?"""
    req = urllib.request.Request(srv.base + "/settings",
                                 headers={"Cookie": f"ser2net_session={token}"})
    try:
        r = urllib.request.build_opener(_NoRedirect).open(req, timeout=20)
    except urllib.error.HTTPError as e:
        r = e
    st = r.status if hasattr(r, "status") else r.code
    if st == 303:
        assert r.headers.get("Location") == "/login", r.headers.get("Location")
        return False
    assert st == 200, st
    return True


def test_versions():
    assert len({new_pwd_version() for _ in range(50)}) > 45, "first versions must be random"
    print("new accounts start at a random session version  OK")


def test_revoked_list():
    cfg = AppConfig.from_dict({"users": [user("admin", "admin")]})
    revoked = auth.RevokedSessions()
    token = auth.issue_session(cfg.secret_key, 600, "admin", 1)
    other = auth.issue_session(cfg.secret_key, 600, "admin", 1)  # another sign-in, same second
    assert other != token
    assert auth.session_user(cfg, token, revoked) is not None
    assert not revoked.add(cfg.secret_key, "garbage") and not revoked.add("other-key", token)
    assert revoked.add(cfg.secret_key, token)
    assert auth.session_user(cfg, token, revoked) is None
    assert auth.session_user(cfg, token) is not None, "without the list: the old behaviour"
    old_max, revoked.MAX_ENTRIES = revoked.MAX_ENTRIES, 1
    try:
        assert revoked.add(cfg.secret_key, other) and token not in revoked and other in revoked
    finally:
        revoked.MAX_ENTRIES = old_max
    print("revocation list: only valid sessions are added, bounded, checked by session_user  OK")


def test_directory_switch():
    cfg = AppConfig.from_dict({"users": [user("admin", "admin"), directory_user("d", "admin"),
                                         directory_user("s", "viewer", "oidc")],
                               "ldap": LDAP_ON})
    d, s = (auth.issue_session(cfg.secret_key, 600, n, 5) for n in ("d", "s"))
    assert auth.session_user(cfg, d) is not None and auth.session_user(cfg, s) is None
    assert cfg.usable_admin_count() == 2
    cfg.ldap.enabled = False
    assert auth.session_user(cfg, d) is None and cfg.usable_admin_count() == 1
    print("session_user: an LDAP/OIDC account's session counts only while that method is on  OK")


def test_ldap_off(srv, admin):
    token = session_for(srv, "dir.user")
    assert signed_in(srv, token), "an LDAP session while LDAP is on"
    st, _, page = admin.post("/settings/ldap", {})  # Enable LDAP unticked
    assert st == 200 and "LDAP accounts are signed out" in page, page[page.find("banner"):][:300]
    assert srv.saved_config()["ldap"]["enabled"] is False
    assert not signed_in(srv, token), "an LDAP session survived switching LDAP off"
    print("LDAP switched off: its accounts' sessions end at once  OK")


def test_last_directory_admin():
    config = {"users": [directory_user("dir.admin", "admin")], "ldap": LDAP_ON}
    with Server(config) as srv:
        st, _, page = browser_with(srv, session_for(srv, "dir.admin")).post("/settings/ldap", {})
        assert st == 200 and "no admin could sign in any more" in page, page[page.find("banner"):][:300]
        assert srv.saved_config()["ldap"]["enabled"] is True, "LDAP was switched off anyway"
    print("switching LDAP off is refused while every admin is an LDAP account  OK")


def test_logout(srv):
    a, b = Browser(srv.base), Browser(srv.base)
    a.login("bob", PW)
    b.login("bob", PW)
    ta, tb = cookie_of(a), cookie_of(b)
    st, h, _ = a.post("/logout")
    assert st == 303 and h.get("Location") == "/login", st
    assert not signed_in(srv, ta), "a copy of a logged-out session cookie still works"
    assert signed_in(srv, tb), "logout must end only its own session"
    print("logout: a copy of that cookie is refused; the user's other session stays  OK")


def test_user_signout(srv, admin):
    bob = Browser(srv.base)
    bob.login("bob", PW)
    token = cookie_of(bob)
    st, _, page = admin.post("/settings/users/bob/signout")
    assert st == 200 and "was signed out everywhere" in page, page[page.find("banner"):][:200]
    assert not signed_in(srv, token)
    Browser(srv.base).login("bob", PW)  # signing in again works
    print("admin: Sign out ends all of a user's sessions  OK")


def test_set_password(srv, admin):
    bob = Browser(srv.base)
    bob.login("bob", PW)
    token = cookie_of(bob)
    new = "reset-pass-2"
    for who, problem in (("admin", "use the form at the top"), ("dir.user", "signs in with LDAP"),
                         ("nobody", "User not found")):
        st, _, page = admin.post("/settings/users/password", {"username": who, "password": new,
                                                              "password2": new})
        assert problem in page, (who, page[page.find("banner"):][:200])
    st, _, page = admin.post("/settings/users/password", {"username": "bob", "password": new,
                                                          "password2": "other-pass"})
    assert "do not match" in page
    st, _, page = admin.post("/settings/users/password", {"username": "bob", "password": new,
                                                          "password2": new})
    assert st == 200 and "New password set for &#39;bob&#39;" in page, page[page.find("banner"):][:200]
    assert not signed_in(srv, token), "the user's sessions must end with the new password"
    st, _, _ = Browser(srv.base).post("/login", {"username": "bob", "password": PW})
    assert st == 401, "the old password still works"
    Browser(srv.base).login("bob", new)
    st, _, page = admin.get("/settings")
    assert 'action="/settings/users/password"' in page and '<option value="bob">' in page
    assert '<option value="admin">' not in page and '<option value="dir.user">' not in page
    print("admin: Set password for another local user (signs them out; not self / LDAP)  OK")


def test_signout_others(srv):
    a, b = Browser(srv.base), Browser(srv.base)
    a.login("admin", PW)
    b.login("admin", PW)
    tb = cookie_of(b)
    st, _, page = a.post("/settings/sessions/signout-others")
    assert st == 200 and "Your other sessions were signed out" in page
    assert signed_in(srv, cookie_of(a)), "this session must stay signed in"
    assert not signed_in(srv, tb)
    print("Sign out my other sessions: the others end, this one stays  OK")


def test_recreated_user(srv, admin):
    bob = Browser(srv.base)
    bob.login("bob", PW)
    old = cookie_of(bob)
    assert signed_in(srv, old)
    assert admin.post("/settings/users/bob/delete")[0] == 200
    assert not signed_in(srv, old)
    st, _, page = admin.post("/settings/users", {"username": "bob", "role": "admin",
                                                 "password": PW, "password2": PW})
    assert st == 200 and "created" in page, page[page.find("banner"):][:200]
    assert not signed_in(srv, old), "a re-created user name revived the old account's cookie"
    print("delete + create the same name again (now admin): the old cookie stays dead  OK")


def main():
    test_versions()
    test_revoked_list()
    test_directory_switch()
    config = {"users": [user("admin", "admin"), user("bob", "viewer"),
                        directory_user("dir.user", "viewer")], "ldap": LDAP_ON}
    with Server(config) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        test_recreated_user(srv, admin)
        test_logout(srv)
        test_ldap_off(srv, admin)
        test_user_signout(srv, admin)
        test_set_password(srv, admin)
        test_signout_others(srv)
    test_last_directory_admin()
    print("\nPASS: session revocation")


if __name__ == "__main__":
    main()
