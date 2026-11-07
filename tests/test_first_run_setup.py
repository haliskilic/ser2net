"""First-run setup: only through this machine's names (or a one-time code), and once.

  * opened by IP address / localhost, the setup page works as before (no code);
  * through another host name in Host, X-Forwarded-Host or Forwarded (the DNS-rebinding
    guard; a reverse proxy looks the same) it answers 403 and asks for the one-time code
    from data/setup-code.txt; without it (or with a wrong one) nothing is created, with
    it the admin is created and the file removed. The start-up log names the file,
    never the code;
  * a second setup request that was let in before the first finished (its body still
    on the way) does not replace the first admin;
  * with SER2NET_ADMIN_PASSWORD_FILE set but unusable, setup in the browser stays closed;
  * demoting yourself is refused when the other admins can't sign in (LDAP off);
  * the user name fields (setup, sign-in, Add user) don't let a phone keyboard capitalise
    or correct the name; setup and Add user also check the rule in the browser, and a
    refused name is told which characters aren't allowed (a Turkish İ is easy to miss).

Run: python3 tests/test_first_run_setup.py
"""
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from _server import Browser, Server

from app.web.auth import hash_password

PW = "setup-pass-123"


def request(srv, path, host=None, data=None, headers=None, browser=None):
    """(status, body) with a Host / forwarding header the browser would send through
    another name; cookies from `browser` (CSRF)."""
    b = browser or Browser(srv.base)
    h = dict(headers or {})
    if host:
        h["Host"] = host
    body = None
    if data is not None:
        data = dict(data, _csrf=b.csrf())
        body = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(srv.base + path, data=body, headers=h,
                                 method="POST" if body is not None else "GET")
    try:
        r = b.op.open(req, timeout=20)
    except urllib.error.HTTPError as e:
        r = e
    return (r.status if hasattr(r, "status") else r.code), r.read().decode("utf-8", "replace")


def form(name="admin"):
    return {"username": name, "password": PW, "password2": PW}


def test_foreign_host():
    with Server(None) as srv:
        port = srv.port
        code_file = os.path.join(srv.data, "setup-code.txt")
        code = open(code_file, encoding="utf-8").read().strip()
        assert len(code.replace("-", "")) == 20, code
        out = srv.output()
        assert code_file in out and code not in out, "the log must name the file, not the code"

        st, page = request(srv, "/setup")
        assert st == 200 and 'name="setup_code"' not in page, "IP address: no code needed"
        st, page = request(srv, "/setup", host=f"localhost:{port}")
        assert st == 200 and 'name="setup_code"' not in page
        for kw in ({"host": f"other-name.example:{port}"},
                   {"headers": {"X-Forwarded-Host": "other-name.example"}},
                   {"headers": {"Forwarded": "for=1.2.3.4;host=other-name.example"}}):
            st, page = request(srv, "/setup", **kw)
            assert st == 403 and 'name="setup_code"' in page and "setup-code.txt" in page, (kw, st)
        print("setup page: IP / localhost as before; another host name -> 403 asking for the code  OK")

        b = Browser(srv.base)
        foreign = f"other-name.example:{port}"
        st, page = request(srv, "/setup", host=foreign, data=form("other"), browser=b)
        assert st == 403 and srv.saved_config()["users"] == [], "created an admin without the code"
        st, page = request(srv, "/setup", host=foreign, data=dict(form("other"),
                                                                    setup_code="0000-1111"), browser=b)
        assert st == 403 and "not right" in page and srv.saved_config()["users"] == []
        assert "wrong setup code" in srv.output() and code not in srv.output()
        st, _ = request(srv, "/setup", host=foreign, data=dict(form("owner"),
                                                                 setup_code=code.upper()), browser=b)
        assert st == 303, st
        users = srv.saved_config()["users"]
        assert [u["username"] for u in users] == ["owner"], users
        assert not os.path.exists(code_file), "the code must be removed once it was used"
        print("through another host name: no / wrong code refused, the right one works once  OK")


def test_second_setup_request():
    with Server(None) as srv:
        slow = Browser(srv.base)
        token = slow.csrf()
        body = urllib.parse.urlencode({"username": "late", "password": "late-pass-123",
                                       "password2": "late-pass-123", "_csrf": token}).encode()
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=20)
        s.sendall((f"POST /setup HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\n"
                   "Content-Type: application/x-www-form-urlencoded\r\n"
                   f"Content-Length: {len(body)}\r\nCookie: ser2net_csrf={token}\r\n"
                   "Connection: close\r\n\r\n").encode() + body[:-1])
        time.sleep(1.0)  # let in while there is no admin yet; waits for the rest of its body
        owner = Browser(srv.base)
        owner.setup("owner", PW)
        s.sendall(body[-1:])
        reply = b""
        while chunk := s.recv(65536):
            reply += chunk
        s.close()
        head = reply.split(b"\r\n\r\n", 1)[0].decode()
        assert " 303 " in head.splitlines()[0] and "location: /login" in head.lower(), head
        assert [u["username"] for u in srv.saved_config()["users"]] == ["owner"]
        assert owner.get("/settings")[0] == 200, "the first admin's session must stay valid"
        assert "ignored: the admin account exists already" in srv.output()
    print("a setup request let in before the first one finished does not replace that admin  OK")


def test_locked():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-dir", "pw")
    with Server(None, env={"SER2NET_ADMIN_PASSWORD_FILE": d}) as srv:
        st, page = request(srv, "/setup")
        assert st == 403 and "Setup in the browser is closed" in page and 'name="password"' not in page
        st, _ = request(srv, "/setup", data=form())
        assert st == 403 and srv.saved_config()["users"] == []
        assert "ERROR: SER2NET_ADMIN_PASSWORD_FILE" in srv.output()
        assert not os.path.exists(os.path.join(srv.data, "setup-code.txt"))
    print("SER2NET_ADMIN_PASSWORD_FILE set but unusable: setup in the browser stays closed  OK")


def test_last_usable_admin():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "dir.admin", "password_hash": "", "role": "admin", "pwd_version": 1,
              "source": "ldap"}]
    with Server({"users": users}) as srv:  # LDAP is off: dir.admin can't sign in
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, page = b.post("/settings/users/admin/role", {"role": "viewer"})
        assert "Cannot demote the last admin" in page, page[page.find("banner"):][:200]
        assert srv.saved_config()["users"][0]["role"] == "admin"
    print("demoting the only admin who can sign in is refused (the other one is LDAP, LDAP off)  OK")


NO_AUTOFIX = 'autocapitalize="none" autocorrect="off" spellcheck="false"'
LOCAL_RULE = ('maxlength="32" pattern="[A-Za-z0-9._\\-]{1,32}" title="Username must be 1-32 '
              "characters: English letters (a-z, A-Z), digits, '.', '_' or '-'.\"")


def username_input(page: str) -> str:
    """The <input ...> of the visible user name field (not a hidden one)."""
    at = page.find('<input type="text" name="username"')
    assert at >= 0, page[-1500:]
    return page[at:page.find(">", at) + 1]


def test_username_fields():
    with Server(None) as srv:
        b = Browser(srv.base)
        _, _, page = b.get("/setup")
        field = username_input(page)
        assert NO_AUTOFIX in field and LOCAL_RULE in field, field
        st, _, page = b.post("/setup", form("İsmail"))
        assert st == 400, st
        assert ("Username must be 1-32 characters: English letters (a-z, A-Z), digits, "
                "&#39;.&#39;, &#39;_&#39; or &#39;-&#39;. Not allowed: &#39;İ&#39;") in page, \
            page[page.find("banner error"):][:300]
        b.setup("admin", PW)
        _, _, page = Browser(srv.base).get("/login")
        field = username_input(page)
        assert NO_AUTOFIX in field and "pattern=" not in field and "maxlength" not in field, field
        _, _, page = b.get("/settings")
        field = username_input(page)
        assert NO_AUTOFIX in field and LOCAL_RULE in field, field
        st, _, page = b.post("/settings/users", {"username": "operatör 1", "role": "viewer",
                                                 "password": PW, "password2": PW})
        assert "Not allowed: &#39;ö&#39;, &#39; &#39;" in page, page[page.find("banner error"):][:300]
    print("user name fields: no auto-capitalisation or correction; the rule is checked in the "
          "browser and names the characters it refuses  OK")


def main():
    test_foreign_host()
    test_second_setup_request()
    test_locked()
    test_last_usable_admin()
    test_username_fields()
    print("\nPASS: first-run setup guard")


if __name__ == "__main__":
    main()
