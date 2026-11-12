"""Password hashes (app/web/auth.py).

Checks:
  * new hashes are "scrypt:16384:8:5$<salt>$<hash>" (OWASP's scrypt minimum); a hash in
    the format of 2.8.1 and older ("scrypt$<salt>$<hash>", N=2^14 r=8 p=1) keeps
    verifying, also one written by v2.7.1 (tests/fixtures/config-2.7.1.json);
  * the cost a stored hash names is used (also above OpenSSL's default 32 MiB limit),
    within bounds: a malformed or too costly one is refused, not computed;
  * passwords are hashed as Unicode NFKC: composed and decomposed letters, a no-break
    space and a space, full-width forms give the same hash; typed Turkish letters are
    NFKC already (one scrypt per check); an older hash of a password as typed keeps
    verifying (also when NFKC changes it) and its renewal makes the other forms work; a
    directory (LDAP) gets the password exactly as typed;
  * the command line's hand-over (accounts.apply) takes both formats;
  * a sign-in with an older hash renews it with today's cost: saved without touching
    pwd_version (sessions stay), best effort (a failed save is logged, the sign-in works);
    a current hash isn't saved again.

Calls the web app in this process (no socket).
Run: python3 tests/test_password_hashing.py
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets

from test_login_throttle import CountingVerify, call, login, run

from app import accounts
from app.config import AppConfig, ConfigStore
from app.state import AppState
from app.web import auth
from app.web.server import build_app

PW = "hashing-pass-1"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "config-2.7.1.json")


def legacy_hash(password: str) -> str:
    """What hash_password() wrote up to 2.8.1."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def tagged(password: str, n: int, r: int, p: int, salt: bytes = b"\x01" * 16) -> str:
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
                        maxmem=256 * 1024 * 1024)
    return f"scrypt:{n}:{r}:{p}${salt.hex()}${dk.hex()}"


def test_format():
    h = auth.hash_password(PW)
    scheme, salt, dk = h.split("$")
    assert scheme == "scrypt:16384:8:5" and len(bytes.fromhex(salt)) == 16 \
        and len(bytes.fromhex(dk)) == 32, h
    assert auth.verify_password(PW, h) and not auth.verify_password(PW + "x", h)
    assert auth.is_password_hash(h) and not auth.needs_rehash(h)
    assert h != auth.hash_password(PW), "a new salt each time"
    old = legacy_hash(PW)
    assert auth.verify_password(PW, old) and not auth.verify_password("other", old)
    assert auth.is_password_hash(old) and auth.needs_rehash(old)
    assert auth.needs_rehash(tagged(PW, 2 ** 10, 8, 1)) and auth.verify_password(
        PW, tagged(PW, 2 ** 10, 8, 1))
    with open(FIXTURE, encoding="utf-8") as fh:
        users = {u["username"]: u["password_hash"] for u in json.load(fh)["users"]}
    for name, pw in (("admin", "old-admin-pass-1"), ("op1", "old-op-pass-1"),
                     ("viewer1", "old-viewer-pass-1")):
        assert auth.verify_password(pw, users[name]) and auth.needs_rehash(users[name]), name
    print("format: new hashes scrypt:16384:8:5$salt$hash; 2.7/2.8 hashes (scrypt$salt$hash, "
          "p=1) keep verifying and are marked for renewal  OK")


def test_cost_bounds():
    big = tagged(PW, 2 ** 17, 8, 1)  # 128 MiB: past OpenSSL's default limit
    assert auth.verify_password(PW, big) and auth.is_password_hash(big)
    salt, dk = "01" * 16, "02" * 32
    for bad in (f"scrypt:16383:8:5${salt}${dk}",         # N not a power of two
                f"scrypt:2097152:8:1${salt}${dk}",       # N past 2^20
                f"scrypt:1048576:8:1${salt}${dk}",       # 1 GiB
                f"scrypt:16384:33:1${salt}${dk}",        # r past 32
                f"scrypt:16384:8:65${salt}${dk}",        # p past 64
                f"scrypt:16384:8:0${salt}${dk}",
                f"scrypt:16384:8${salt}${dk}",           # a part missing
                f"scrypt:16384:8:5:1${salt}${dk}",
                f"scrypt:１６３８４:8:5${salt}${dk}",       # not ASCII digits
                f"scrypt:-16384:8:5${salt}${dk}",
                f"bcrypt${salt}${dk}", f"scrypt${salt}", f"scrypt$${dk}",
                f"scrypt${salt}$", f"scrypt${salt}$zz", f"scrypt${salt}${dk}$x",
                f"scrypt${salt}${'02' * 65}",            # a 65-byte hash
                "", "scrypt", None, 42):
        assert not auth.is_password_hash(bad) and not auth.verify_password(PW, bad), bad
    assert not auth.verify_password(None, auth.hash_password(PW))  # type: ignore[arg-type]
    assert not auth.verify_password("\ud800", auth.hash_password(PW))  # not encodable
    print("cost: what a stored hash names is used (2^17 too: past OpenSSL's default memory "
          "limit), malformed / too costly ones are refused without computing  OK")


class CountScrypt:
    def __init__(self) -> None:
        self.n, self.real = 0, auth._scrypt

    def __call__(self, *a, **k):
        self.n += 1
        return self.real(*a, **k)

    def __enter__(self) -> "CountScrypt":
        auth._scrypt = self
        return self

    def __exit__(self, *exc) -> None:
        auth._scrypt = self.real


def nfd(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFD", text)


TURKISH = "Gizli-Şifre-ğüşıöç-İĞÜŞÖÇ-1"


def test_normalized():
    import unicodedata
    assert unicodedata.normalize("NFKC", TURKISH) == TURKISH, "typed Turkish is NFKC already"
    assert nfd(TURKISH) != TURKISH
    h = auth.hash_password(TURKISH)
    assert auth.verify_password(nfd(TURKISH), h) and auth.verify_password(TURKISH, h)
    h = auth.hash_password(nfd(TURKISH))   # set from a decomposed source
    assert auth.verify_password(TURKISH, h)
    h = auth.hash_password("correct horse 42")
    assert auth.verify_password("correct\u00a0horse\u202f42", h)    # no-break spaces
    assert auth.verify_password("ｃｏｒｒｅｃｔ horse ４２", h)        # full-width
    assert not auth.verify_password("correct  horse 42", h)
    with CountScrypt() as c:
        assert auth.verify_password(TURKISH, auth.hash_password(TURKISH))
        assert not auth.verify_password(nfd(TURKISH), auth.hash_password("other-pass-1"))
        assert not auth.verify_password("ascii-pass-1", legacy_hash("other-pass-1"))
    assert c.n == 2 + 2 + 1, f"{c.n} scrypt runs: one per check (and per hash)"
    # a hash of 2.8 made of a decomposed password (as typed): that form keeps working
    old = legacy_hash(nfd(TURKISH))
    with CountScrypt() as c:
        assert auth.verify_password(nfd(TURKISH), old)
    assert c.n == 2, "NFKC first, then the form as typed"
    old = legacy_hash("pass\u00a0word-1")
    assert auth.verify_password("pass\u00a0word-1", old)
    print("NFKC: decomposed / composed letters, no-break / plain spaces, full-width forms are "
          "one password; typed Turkish costs one scrypt; a 2.8 hash of the typed form keeps "
          "verifying  OK")


def test_command_line_hand_over():
    for h in (auth.hash_password(PW), legacy_hash(PW)):
        cfg = AppConfig.from_dict({"users": [{"username": "admin", "password_hash": "x",
                                              "role": "admin", "pwd_version": 1}]})
        change = accounts.apply(cfg, {"action": "set_password", "username": "admin",
                                      "password_hash": h})
        assert change.action == "user_password_set" and cfg.get_user("admin").password_hash == h
    print("--set-password hand-over: a request with a hash of this or an older version is "
          "taken  OK")


# ---------------------------------------------------------------- renewal at sign-in
def _state(d: str, config: dict) -> AppState:
    store = ConfigStore(os.path.join(d, "config.json"))
    cfg = AppConfig.from_dict(config)
    store.save(cfg, validate=False)
    return AppState(store, cfg)


def _saved_user(state, name: str) -> dict:
    with open(state.store.path, encoding="utf-8") as fh:
        return next(u for u in json.load(fh)["users"] if u["username"] == name)


async def _renewal(d: str, config: dict) -> None:
    state = _state(d, config)
    app = build_app(state)
    admin = state.config.get_user("admin")
    old = admin.password_hash
    session = auth.issue_session(state.config.secret_key, 3600, "admin", admin.pwd_version)
    assert (await login(app, "admin", PW))[0] == 303
    saved = _saved_user(state, "admin")
    assert saved["password_hash"].startswith("scrypt:16384:8:5$") and saved["pwd_version"] == 7
    assert admin.password_hash == saved["password_hash"] != old
    assert auth.verify_password(PW, saved["password_hash"])
    assert auth.session_user(state.config, session) is admin, "its sessions stay"
    with open(state.log_path, encoding="utf-8") as fh:
        assert "the password hash of 'admin' was renewed with today's scrypt cost" in fh.read()
    backups = state.store.backup_names()
    assert (await login(app, "admin", PW))[0] == 303
    assert state.store.backup_names() == backups, "a current hash is not saved again"
    # a wrong password renews nothing; a failed save is logged and the sign-in still works
    op = state.config.get_user("op")
    assert (await login(app, "op", "wrong", client="10.0.0.2"))[0] == 401
    assert op.password_hash == _saved_user(state, "op")["password_hash"]
    real = state.store.save

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    state.store.save = full
    try:
        assert (await login(app, "op", PW, client="10.0.0.3"))[0] == 303
    finally:
        state.store.save = real
    assert auth.needs_rehash(op.password_hash), "unchanged in memory when not saved"
    with open(state.log_path, encoding="utf-8") as fh:
        assert "could not save the renewed password hash of 'op': [Errno 28]" in fh.read()
    with CountingVerify() as verify:
        assert (await login(app, "op", PW, client="10.0.0.4"))[0] == 303
    assert len(verify.calls) == 1 and not auth.needs_rehash(op.password_hash), \
        "tried again at the next sign-in"
    # the password change form verifies an old hash too and writes a current one
    user = state.config.get_user("viewer")
    cookie = auth.issue_session(state.config.secret_key, 3600, "viewer", user.pwd_version)
    s, _h, text = await call(app, "POST", "/settings/password",
                             {"current": PW, "password": "new-viewer-pass-1",
                              "password2": "new-viewer-pass-1"},
                             headers={"Content-Type": "application/x-www-form-urlencoded",
                                      "Cookie": f"ser2net_csrf=throttle-csrf-token; "
                                                f"ser2net_session={cookie}"},
                             client="10.0.0.5")
    assert "Password updated." in text, text[-400:]
    assert _saved_user(state, "viewer")["password_hash"].startswith("scrypt:16384:8:5$")
    print("sign-in with a 2.8 hash: renewed with today's cost and saved, pwd_version and "
          "sessions kept; a current hash isn't saved again; a failed save is logged and "
          "tried again next time  OK")


async def _normalized_sign_in(d: str, config: dict) -> None:
    from app.web import ldap_auth

    state = _state(d, config)
    app = build_app(state)
    # the 2.8 hash was made of the decomposed form: the composed one never matched it
    assert (await login(app, "admin", TURKISH, client="10.1.0.1"))[0] == 401
    assert (await login(app, "admin", nfd(TURKISH), client="10.1.0.2"))[0] == 303
    assert not auth.needs_rehash(state.config.get_user("admin").password_hash)
    for form in (TURKISH, nfd(TURKISH)):  # renewed as NFKC: both forms work from now on
        assert (await login(app, "admin", form, client="10.1.0.3"))[0] == 303
    got = []

    def directory(settings, username, password, log, **kw):
        got.append(password)
        return None

    state.config.ldap.enabled = True
    real, ldap_auth.authenticate = ldap_auth.authenticate, directory
    try:
        assert (await login(app, "jdoe", nfd(TURKISH) + "\u00a0", client="10.1.0.4"))[0] == 401
    finally:
        ldap_auth.authenticate = real
    assert got == [nfd(TURKISH) + "\u00a0"], "the directory gets the password as typed"
    print("sign-in: a 2.8 hash of a decomposed password works with that form, is renewed, then "
          "both forms work; LDAP gets the password exactly as typed  OK")


def test_normalized_sign_in():
    run(_normalized_sign_in, {
        "users": [{"username": "admin", "password_hash": legacy_hash(nfd(TURKISH)),
                   "role": "admin", "pwd_version": 1}],
        "ldap": {"enabled": False, "server_uri": "ldap://127.0.0.1:1",
                 "user_dn_template": "uid={username},ou=people,dc=ex,dc=com"}})


def test_renewed_at_sign_in():
    run(_renewal, {"users": [
        {"username": "admin", "password_hash": legacy_hash(PW), "role": "admin",
         "pwd_version": 7},
        {"username": "op", "password_hash": legacy_hash(PW), "role": "operator",
         "pwd_version": 1},
        {"username": "viewer", "password_hash": legacy_hash(PW), "role": "viewer",
         "pwd_version": 1}]})


def main():
    test_format()
    test_cost_bounds()
    test_normalized()
    test_command_line_hand_over()
    test_renewed_at_sign_in()
    test_normalized_sign_in()
    print("\nPASS: password hashes")


if __name__ == "__main__":
    main()
