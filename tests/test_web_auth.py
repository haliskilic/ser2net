"""Web auth lifecycle: CSRF enforcement, login rate-limiting, and session
revocation on password change (pwd_version); a cookie, CSRF token or SSO state with
bytes no token has (non-ASCII) is a mismatch (4xx), not a server error; the SSO state
cookie and the session cookie are signed with different keys, so neither is taken for
the other, and a signed token with fields of other types is no session.
Run: python3 tests/test_web_auth.py
"""
import http.client
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
UI = 8087
BASE = f"http://127.0.0.1:{UI}"

from _server import Server, server_log, show_log_on_failure

from app.web import auth


def raw(port, method, path, headers, body=None):
    """A request whose header values are sent as given (bytes too); returns the status."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path)
        for k, v in headers.items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_auth_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    out = server_log(tmp)  # the server's output: a file, never an unread pipe
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=out, stderr=subprocess.STDOUT)
    out.close()  # (the server has its own copy)
    try:
        assert wait_port(UI), "server did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next((c.value for c in jar if c.name == "ser2net_csrf"), "")

        def session():
            return next((c.value for c in jar if c.name == "ser2net_session"), "")

        def post(path, data, header=True):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            if header:
                req.add_header("X-CSRF-Token", csrf())
            try:
                return op.open(req, timeout=10)
            except urllib.error.HTTPError as e:
                return e

        op.open(BASE + "/setup")
        post("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})
        old_session = session()
        assert old_session, "no session after setup"
        print("setup + session issued  OK")

        # 1) CSRF: an /api POST without the X-CSRF-Token header is rejected
        r = post("/api/mappings/save", {"name": "x"}, header=False)
        assert getattr(r, "code", getattr(r, "status", None)) == 403, r
        print("CSRF: /api POST without token -> 403  OK")

        # non-ASCII bytes in a session cookie, a CSRF cookie/header or a form's _csrf
        # field are a mismatch (no session / 403), never a server error
        bad = "ü".encode()  # the UTF-8 bytes of a letter
        for cookie in (b"ser2net_session=" + bad + b".x", b'ser2net_session="\\303\\274.x"'):
            assert raw(UI, "GET", "/", {"Cookie": cookie}) == 303, cookie
            assert raw(UI, "GET", "/api/status", {"Cookie": cookie}) == 401, cookie
        form = urllib.parse.urlencode({"name": "x"}).encode()
        ct = {"Content-Type": "application/x-www-form-urlencoded"}
        sess = f"ser2net_session={session()}".encode()
        assert raw(UI, "POST", "/api/mappings/save", {
            **ct, "Cookie": f"ser2net_csrf={csrf()}; ".encode() + sess, "X-CSRF-Token": bad}, form) == 403
        assert raw(UI, "POST", "/api/mappings/save", {
            **ct, "Cookie": b"ser2net_csrf=" + bad + b"; " + sess,
            "X-CSRF-Token": csrf().encode()}, form) == 403
        login = urllib.parse.urlencode({"password": "x", "_csrf": "ü"}).encode()
        assert raw(UI, "POST", "/login", {**ct, "Cookie": f"ser2net_csrf={csrf()}".encode()},
                   login) == 403
        print("non-ASCII session cookie / CSRF cookie, header or field -> 303/401/403  OK")

        # 2) session revocation: change password, then the OLD session must be invalid
        r = post("/settings/password",
                 {"current": "supersecret1", "password": "newsecret123",
                  "password2": "newsecret123", "_csrf": csrf()})
        assert getattr(r, "status", 200) == 200
        new_session = session()
        assert new_session and new_session != old_session, "session should be refreshed"
        # request with ONLY the old cookie -> redirected to /login
        req = urllib.request.Request(BASE + "/")
        req.add_header("Cookie", f"ser2net_session={old_session}")
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.geturl().endswith("/login"), f"old session still valid: {resp.geturl()}"
        print("session revocation: old session invalid after password change  OK")

        # 3) login rate-limit: repeated wrong passwords -> 429
        jar2 = http.cookiejar.CookieJar()
        op2 = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar2))
        op2.open(BASE + "/login")  # get a csrf cookie
        tok = next((c.value for c in jar2 if c.name == "ser2net_csrf"), "")
        codes = []
        for _ in range(11):
            req = urllib.request.Request(BASE + "/login",
                                         data=urllib.parse.urlencode(
                                             {"password": "wrong", "_csrf": tok}).encode(),
                                         method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                codes.append(op2.open(req, timeout=10).status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        assert 429 in codes, f"expected a 429 after repeated failures, got {codes}"
        assert codes.count(401) >= 5, codes
        print(f"rate-limit: wrong logins -> {codes.count(401)}x401 then 429  OK")

        print("\nPASS: CSRF + session revocation + login rate-limit")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        show_log_on_failure(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_oidc_state_odd_values():
    """The SSO callback compares the state it gets with the signed one in its cookie:
    a non-ASCII state or cookie is a mismatch (400), not a server error."""
    secret = "s" * 64
    with Server({"secret_key": secret,
                 "users": [{"username": "admin", "role": "admin", "pwd_version": 1,
                            "password_hash": auth.hash_password("adminpass123")}],
                 "oidc": {"enabled": True, "issuer": "https://idp.example.com",
                          "client_id": "ser2net", "client_secret": "sek"}}) as srv:
        signed = auth.sign_payload(secret, {"s": "abc", "n": "n", "r": "https://h/cb"}).encode()
        for state, cookie in (("%C3%BC", signed), ("abc", "ü".encode() + b"." + signed)):
            st = raw(srv.port, "GET", f"/auth/oidc/callback?code=x&state={state}",
                     {"Cookie": b"ser2net_oidc=" + cookie})
            assert st == 400, (state, st)
    print("SSO callback: non-ASCII state or state cookie -> 400  OK")


def test_token_kinds():
    secret = "s" * 64
    state_cookie = auth.sign_payload(secret, {"s": "abc", "n": "n", "r": "https://h/cb",
                                              "v": "pkce-verifier"})
    session = auth.issue_session(secret, 600, "admin", 1)
    assert auth.decode_session(secret, state_cookie) is None
    assert auth.read_payload(secret, session) is None
    assert auth.read_payload(secret, state_cookie)["v"] == "pkce-verifier"
    assert auth.decode_session(secret, session)["u"] == "admin"

    def signed(doc):  # what only the holder of the session key could make
        payload = auth._b64e(json.dumps(doc).encode())
        return f"{payload}.{auth._sign(secret, payload)}"

    exp = int(time.time()) + 600
    for doc in ({"u": "admin", "v": "x", "exp": exp}, {"u": 5, "v": 1, "exp": exp},
                {"u": "admin", "v": 1, "exp": "soon"}, {"u": "admin", "v": True, "exp": exp},
                {"u": "admin", "v": 1}, ["admin", 1], "admin"):
        assert auth.decode_session(secret, signed(doc)) is None, doc
    assert auth.decode_session(secret, signed({"u": "admin", "v": 1, "exp": exp})), \
        "a token as 2.8 issued it (no 'n')"
    with Server({"secret_key": secret,
                 "users": [{"username": "admin", "role": "admin", "pwd_version": 1,
                            "password_hash": auth.hash_password("adminpass123")}],
                 "oidc": {"enabled": True, "issuer": "https://idp.example.com",
                          "client_id": "ser2net", "client_secret": "sek"}}) as srv:
        for path in ("/", "/settings", "/api/mappings"):
            st = raw(srv.port, "GET", path, {"Cookie": "ser2net_session=" + state_cookie})
            assert st in (303, 401), (path, st)
        st = raw(srv.port, "GET", "/auth/oidc/callback?code=x&state=",
                 {"Cookie": "ser2net_oidc=" + session})
        assert st == 400, st
        assert "Traceback" not in srv.output()
    print("SSO state cookie as the session cookie, and the reverse: refused, no server "
          "error; odd field types are no session  OK")


if __name__ == "__main__":
    main()
    test_oidc_state_odd_values()
    test_token_kinds()
