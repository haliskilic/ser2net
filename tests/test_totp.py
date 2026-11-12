"""Two-factor sign-in (TOTP) for local accounts.

  * app/web/totp.py gives the RFC 4226 / RFC 6238 test vectors (stdlib only), accepts the
    current time step and one either side, never a step at or before the last one accepted
    (replay guard), takes a code typed with spaces, and matches nothing for a bad secret;
  * config: totp_secret is written only when set (a config.json without it stays as it
    was), a hand-edited one is normalized, an unusable one is noted at start-up (the account
    can't sign in: fail closed);
  * `ser2net --set-password NAME` (the recovery command, also while ser2net runs) turns the
    account's two-factor sign-in off with the new password; undone if saving fails;
  * sign-in: the right password of an account with two-factor sign-in gives no session but
    the code step (/auth/totp); a wrong code is refused (and recorded), the right one gives
    the session, the same code twice is refused; too many wrong codes for one account block
    it for a while, even the right code; accounts without it sign in as before; a wrong
    code's notification leaves out personal data when Settings -> Notifications says so;
  * Settings: a local account sets it up with a code and its password (its other sessions
    end, this one stays), turns it off with its password and a code; an admin turns it off
    for another account in the Users panel (that account is signed out), not for itself.

Run: python3 tests/test_totp.py
"""
import base64
import html
import json
import re
import subprocess
import sys
import time

from _server import ROOT, Browser, Server

from app import accounts
from app.config import AppConfig, User
from app.web import totp
from app.web.auth import hash_password

PW = "totp-pass-1"
NEW_PW = "totp-pass-2"
ADMIN_SECRET = totp.new_secret()


def test_algorithm():
    key = b"12345678901234567890"
    # RFC 4226 appendix D
    assert [totp.hotp(key, i) for i in range(10)] == [
        "755224", "287082", "359152", "969429", "338314",
        "254676", "287922", "162583", "399871", "520489"]
    # RFC 6238 appendix B, SHA-1 (8 digits)
    for when, want in ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                       (1234567890, "89005924"), (2000000000, "69279037"),
                       (20000000000, "65353130")):
        assert totp.hotp(key, totp.step_at(when), 8) == want, when
    secret = base64.b32encode(key).decode()
    now = 1_700_000_015.0
    step = totp.step_at(now)
    for offset in (-1, 0, 1):
        code = totp.code_at(secret, now + offset * totp.STEP_S)
        assert totp.verify(secret, code, now=now) == step + offset, offset
    for offset in (-2, 2):
        assert totp.verify(secret, totp.code_at(secret, now + offset * 30), now=now) is None
    code = totp.code_at(secret, now)
    assert totp.verify(secret, code, last_step=step, now=now) is None, "replayed"
    assert totp.verify(secret, code, last_step=step - 1, now=now) == step
    assert totp.verify(secret, f"{code[:3]} {code[3:]}", now=now) == step, "typed with a space"
    for bad in ("", "12345", "1234567", "abcdef", None, code + "0"):
        assert totp.verify(secret, bad, now=now) is None, bad
    for bad_secret in ("", "short", "not base32!!!!!!!!", None, 123):
        assert totp.verify(bad_secret, code, now=now) is None, bad_secret
    fresh = totp.new_secret()
    assert totp.is_secret(fresh) and len(fresh) == 32, fresh
    uri = totp.otpauth_uri(fresh, "jo smith")
    assert uri.startswith("otpauth://totp/ser2net%3Ajo%20smith?secret=" + fresh), uri
    assert "issuer=ser2net" in uri and "period=30" in uri and "digits=6" in uri
    assert totp.grouped("ABCDEFGHIJ") == "ABCD EFGH IJ"
    print("totp: RFC 4226 / 6238 vectors, ±1 step, replay guard, spaces, bad secrets  OK")


def test_config_and_recovery():
    cfg = AppConfig.from_dict({"users": [
        {"username": "a", "password_hash": "x", "role": "admin"},
        {"username": "b", "password_hash": "x", "totp_secret": "  " + ADMIN_SECRET.lower() + "==="},
        {"username": "c", "password_hash": "x", "totp_secret": "not-a-secret"},
        {"username": "d", "password_hash": "x", "totp_secret": None}]})
    users = {u.username: u for u in cfg.users}
    assert users["b"].totp_secret == ADMIN_SECRET and users["b"].totp_on
    assert users["c"].totp_on and not users["d"].totp_on
    assert [n for n in cfg.load_notes if "user 'c': totp_secret is not" in n
            and "--set-password c" in n], cfg.load_notes
    saved = {u["username"]: u for u in cfg.to_dict()["users"]}
    assert "totp_secret" not in saved["a"] and "totp_secret" not in saved["d"], saved["a"]
    assert saved["b"]["totp_secret"] == ADMIN_SECRET
    assert not User(source="ldap", totp_secret=ADMIN_SECRET).totp_on, "local accounts only"

    change = accounts.apply(cfg, {"action": "set_password", "username": "b",
                                  "password_hash": hash_password(NEW_PW)})
    assert users["b"].totp_secret == "" and "two-factor sign-in was turned off" in change.message
    assert change.detail == "b (two-factor sign-in off)", change.detail
    change.undo()
    assert users["b"].totp_secret == ADMIN_SECRET, "undo brings it back"
    change = accounts.apply(cfg, {"action": "set_password", "username": "a",
                                  "password_hash": hash_password(NEW_PW)})
    assert "two-factor" not in change.message and change.detail == "a"
    print("config: totp_secret written only when set, normalized, an unusable one noted; "
          "--set-password turns it off (and undoes)  OK")


def code(secret, ahead=0):
    """The code `ahead` steps from now (one ahead is still accepted, and later than one
    just used)."""
    return totp.code_at(secret, time.time() + ahead * totp.STEP_S)


def test_live():
    config = {"users": [
        {"username": "admin", "password_hash": hash_password(PW), "role": "admin",
         "pwd_version": 1, "totp_secret": ADMIN_SECRET},
        {"username": "op", "password_hash": hash_password(PW), "role": "operator", "pwd_version": 1},
        {"username": "eve", "password_hash": hash_password(PW), "role": "viewer", "pwd_version": 1,
         "totp_secret": totp.new_secret()}]}
    with Server(config) as srv:
        base = srv.base

        # ---- sign-in with the code step ----
        b = Browser(base)
        st, h, _ = b.post("/login", {"username": "admin", "password": PW})
        assert st == 303 and h["Location"] == "/auth/totp", (st, h["Location"])
        assert b.cookie("ser2net_session") is None, "a session before the code"
        pending = b.cookie("ser2net_2fa")
        assert pending is not None and pending.path == "/auth/totp", pending
        assert b.get("/")[0] == 303, "no access before the code"
        st, h, page = b.get("/auth/totp")
        assert st == 200 and 'name="code"' in page and "admin" in page, st
        assert h["Cache-Control"] == "no-store"
        st, _, page = b.post("/auth/totp", {"code": "000000" if code(ADMIN_SECRET) != "000000"
                                            else "111111"})
        assert st == 401 and "That code is not right" in page and b.cookie("ser2net_session") is None
        used = code(ADMIN_SECRET)
        st, h, _ = b.post("/auth/totp", {"code": used})
        assert st == 303 and h["Location"] == "/" and b.cookie("ser2net_session") is not None, st
        assert b.get("/")[0] == 200 and b.cookie("ser2net_2fa") is None
        print("sign-in: password -> code step (no session yet); wrong code refused, the right "
              "one signs in  OK")

        replay = Browser(base)
        replay.post("/login", {"username": "admin", "password": PW})
        st, _, _ = replay.post("/auth/totp", {"code": used})
        assert st == 401 and replay.cookie("ser2net_session") is None, "a code used twice"
        st, _, _ = replay.post("/auth/totp", {"code": code(ADMIN_SECRET, 1)})
        assert st == 303 and replay.cookie("ser2net_session") is not None
        nocookie = Browser(base)
        st, _, page = nocookie.post("/auth/totp", {"code": code(ADMIN_SECRET)})
        assert st == 401 and "Sign in again" in page, st
        assert nocookie.get("/auth/totp")[0] == 303
        op = Browser(base)
        op.login("op", PW)   # no two-factor sign-in: as before
        audit = open(srv.data + "/audit.log", encoding="utf-8").read()
        assert "\tlogin_failed\ttwo-factor code\tadmin" in audit, audit[-800:]
        assert "\tlogin\tadmin, local, two-factor\tadmin" in audit, audit[-800:]
        print("sign-in: the same code twice is refused, the next one works; no pending sign-in "
              "-> start again; accounts without it as before; audited  OK")

        # ---- Settings: set it up (op) ----
        other = Browser(base)
        other.login("op", PW)
        st, h, page = op.get("/settings/totp")
        assert st == 200 and h["Cache-Control"] == "no-store" and "otpauth://totp/" in page
        secret = re.search(r"secret=([A-Z2-7]+)", html.unescape(page)).group(1)
        enrol = html.unescape(re.search(r'name="enrol" value="([^"]+)"', page).group(1))
        for fields, want in (({"enrol": enrol, "password": "wrong-pass", "code": code(secret)},
                              "Current password is incorrect."),
                             ({"enrol": enrol, "password": PW, "code": "12345"},
                              "That code is not right"),
                             ({"enrol": "forged", "password": PW, "code": code(secret)},
                              "This setup has expired")):
            st, _, page = op.post("/settings/totp/enable", fields)
            assert st == 400 and want in page, (fields, st)
        assert "totp_secret" not in next(u for u in srv.saved_config()["users"] if u["username"] == "op")
        st, _, page = op.post("/settings/totp/enable", {"enrol": enrol, "password": PW,
                                                        "code": code(secret)})
        assert st == 200 and "Two-factor sign-in is on" in page, page[:300]
        saved = next(u for u in srv.saved_config()["users"] if u["username"] == "op")
        assert saved["totp_secret"] == secret and saved["pwd_version"] == 2
        assert op.get("/")[0] == 200, "this session stays"
        assert other.get("/")[0] == 303, "the other sessions end"
        relog = Browser(base)
        st, h, _ = relog.post("/login", {"username": "op", "password": PW})
        assert h["Location"] == "/auth/totp", "op now needs a code"
        st, _, page = op.get("/settings")
        assert "Turn off…" in page, "Settings shows it is on"
        print("settings: set up with a code and the password (other sessions end, this one "
              "stays); signing in then takes a code  OK")

        # ---- turn it off (op), with password and code ----
        st, _, page = op.post("/settings/totp/disable", {"password": PW, "code": "000001"})
        assert st == 400 and "That code is not right" in page
        st, _, page = op.post("/settings/totp/disable", {"password": "nope-nope", "code": code(secret, 1)})
        assert st == 400 and "Current password is incorrect." in page
        st, _, page = op.post("/settings/totp/disable", {"password": PW, "code": code(secret, 1)})
        assert st == 200 and "Two-factor sign-in is off" in page, page[:300]
        assert "totp_secret" not in next(u for u in srv.saved_config()["users"] if u["username"] == "op")
        assert op.get("/")[0] == 200
        print("settings: turned off with the password and a code  OK")

        # ---- admin: turn it off for another account (eve), not for itself ----
        eve = Browser(base)
        assert eve.post("/login", {"username": "eve", "password": PW})[1]["Location"] == "/auth/totp"
        st, _, page = b.get("/settings")
        assert 'action="/settings/users/totp-reset"' in page and "Turn off 2FA" in page
        st, _, page = b.post("/settings/users/totp-reset", {"username": "admin"})
        assert "To turn off your own two-factor sign-in" in page
        st, _, page = b.post("/settings/users/totp-reset", {"username": "op"})
        assert "doesn’t use two-factor sign-in" in page
        st, _, page = op.post("/settings/users/totp-reset", {"username": "eve"})
        assert st == 403, "admins only"
        st, _, page = b.post("/settings/users/totp-reset", {"username": "eve"})
        assert st == 200 and "Two-factor sign-in is off for &#39;eve&#39;" in page, page[:300]
        saved = next(u for u in srv.saved_config()["users"] if u["username"] == "eve")
        assert "totp_secret" not in saved and saved["pwd_version"] == 2
        eve.login("eve", PW)   # the password alone again
        print("users panel: an admin turns it off for another account (signed out), not for "
              "itself; operators can't  OK")

        # ---- too many wrong codes for one account ----
        lock = Browser(base)
        lock.post("/login", {"username": "admin", "password": PW})
        for _ in range(5):
            st, _, _ = lock.post("/auth/totp", {"code": "999999" if code(ADMIN_SECRET) != "999999"
                                                else "888888"})
            assert st == 401, st
        st, _, page = lock.post("/auth/totp", {"code": code(ADMIN_SECRET, 1)})
        assert st == 429 and "Too many wrong codes" in page and lock.cookie("ser2net_session") is None
        print("sign-in: 5 wrong codes for an account -> refused for a while, even the right "
              "code  OK")

        # ---- recovery: --set-password turns it off, while ser2net runs ----
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", srv.data,
                            "--set-password", "admin"], cwd=ROOT, capture_output=True, text=True,
                           timeout=120, input=NEW_PW + "\n", env=srv.env)
        assert p.returncode == 0 and "two-factor sign-in was turned off" in p.stdout, p.stdout + p.stderr
        saved = next(u for u in srv.saved_config()["users"] if u["username"] == "admin")
        assert "totp_secret" not in saved
        fresh = Browser(base)
        fresh.login("admin", NEW_PW)   # no code step any more
        assert b.get("/")[0] == 303, "the admin's sessions were ended"
        assert "two-factor sign-in off" in open(srv.data + "/audit.log", encoding="utf-8").read()
        print("recovery: ser2net --set-password turns two-factor sign-in off (running "
              "instance), the new password alone signs in  OK")
        assert json.dumps(srv.saved_config()).count("totp_secret") == 0


def test_wrong_code_notification():
    from _asgi import App

    secret = totp.new_secret()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1, "totp_secret": secret}],
           "notifications": {"enabled": True, "events": ["login_failed"],
                             "mask_personal_data": True}}
    with App(cfg) as a:
        sent = []
        a.state.notifier.emit = lambda event, key, mapping, detail="": sent.append(  # type: ignore[method-assign]
            (event, detail)) or True
        jar = "ser2net_csrf=c"
        st, h, _ = a.call("POST", "/login", headers={"Cookie": jar}, client="10.0.0.9",
                          fields={"username": "admin", "password": PW, "_csrf": "c"})
        assert st == 303, st
        pending = next(v.split(";")[0] for k, v in h if k == "set-cookie"
                       and v.startswith("ser2net_2fa="))
        wrong = f"{(int(totp.code_at(secret, time.time())) + 1) % 1000000:06d}"
        st, _, _ = a.call("POST", "/auth/totp", headers={"Cookie": f"{jar}; {pending}"},
                          client="10.0.0.9", fields={"code": wrong, "_csrf": "c"})
        assert st == 401, st
    assert sent == [("login_failed", "user (name left out) from 10.0.0.x: wrong two-factor "
                                     "code")], sent
    print("a wrong code's notification: no account name and the address's network with "
          "'leave out personal data'  OK")


def main():
    test_algorithm()
    test_config_and_recovery()
    test_live()
    test_wrong_code_notification()
    print("\nPASS: two-factor sign-in (TOTP)")


if __name__ == "__main__":
    main()
