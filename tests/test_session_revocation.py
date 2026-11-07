"""Ending web-UI sessions: which actions sign a session out, and that nothing brings
an ended one back.

  * a new account starts at a random session version, so deleting a user and creating
    the same name again (even with another role) does not revive the old cookies.

Run: python3 tests/test_session_revocation.py
"""
import urllib.error
import urllib.request

from _server import Browser, Server

from app.config import new_pwd_version
from app.web.auth import hash_password

PW = "revoke-pass-1"


def user(name, role):
    return {"username": name, "password_hash": hash_password(PW), "role": role, "pwd_version": 1}


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
    config = {"users": [user("admin", "admin"), user("bob", "viewer")]}
    with Server(config) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        test_recreated_user(srv, admin)
    print("\nPASS: session revocation")


if __name__ == "__main__":
    main()
