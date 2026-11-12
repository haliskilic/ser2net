"""How sign-in attempts are limited.

Checks:
  * attempts are counted before the password is checked: a burst of concurrent wrong
    sign-ins from one address gets exactly `max_attempts` password checks, the rest 429;
    a sign-in that works resets the count;
  * without LDAP, a name that has no local account (unknown, or an LDAP/OIDC account) is
    checked against a dummy hash: the same work as a wrong password;
  * the per-address limit counts an IPv6 address by its /64 (IPv4-mapped as IPv4);
  * failed sign-ins under one name (letter case aside) from any number of addresses:
    10 in 15 minutes, then 429 for that name only (also with the right password) until
    they expire; a sign-in that works resets it; the directory isn't asked meanwhile;
  * a wrong current password in the password change counts under that name, is logged
    and audited (password_change_failed); past the limit even the right one is refused;
  * password checks and directory sign-ins run a few at a time (auth.SlowWork); while
    too many wait already, a sign-in is answered 429 without being tried or counted;

Calls the web app in this process (no socket).
Run: python3 tests/test_login_throttle.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import urllib.parse

import _server  # noqa: F401  (puts the repo root and lib/ on sys.path)

from ser2netweb.config import AppConfig, ConfigStore
from ser2netweb.state import AppState
from ser2netweb.web import auth
from ser2netweb.web.server import build_app

PW = "throttle-pass-1"
CSRF = "throttle-csrf-token"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": f"ser2net_csrf={CSRF}"}


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


async def call(app, method: str, path: str, fields: dict | None = None, *, headers=None,
               client: str = "127.0.0.1"):
    """One request to the ASGI app: (status, headers, body text)."""
    body = urllib.parse.urlencode({"_csrf": CSRF, **(fields or {})}).encode() if fields else b""
    hdrs = [(b"host", b"127.0.0.1:8080"), (b"content-length", str(len(body)).encode())]
    hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or FORM).items()]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": hdrs, "client": (client, 50000), "server": ("127.0.0.1", 8080)}
    done, sent = asyncio.Event(), [False]
    out = {"status": 0, "headers": {}, "body": b""}

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")
            if not msg.get("more_body"):
                done.set()

    await asyncio.wait_for(app(scope, receive, send), 60)
    return out["status"], out["headers"], out["body"].decode("utf-8", "replace")


def login(app, username: str, password: str, client: str = "127.0.0.1"):
    return call(app, "POST", "/login", {"username": username, "password": password},
                client=client)


def _user(name: str, role: str = "admin") -> dict:
    return {"username": name, "password_hash": auth.hash_password(PW), "role": role,
            "pwd_version": 1}


def _state(d: str, config: dict) -> AppState:
    return AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig.from_dict(config))


class CountingVerify:
    """auth.verify_password, counting its calls and (optionally) slower, so that
    concurrent sign-ins overlap."""

    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self.delay = delay
        self.real = auth.verify_password

    def __call__(self, password: str, stored: str) -> bool:
        import time
        self.calls.append(stored)
        if self.delay:
            time.sleep(self.delay)
        return self.real(password, stored)

    def __enter__(self) -> "CountingVerify":
        auth.verify_password = self
        return self

    def __exit__(self, *exc) -> None:
        auth.verify_password = self.real


def run(coro_fn, config: dict) -> None:
    d = tempfile.mkdtemp(prefix="s2n-throttle-")
    try:
        asyncio.run(coro_fn(d, config))
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- the per-address limit
async def _burst(d: str, config: dict) -> None:
    state = _state(d, config)
    app = build_app(state)
    limit = state.rate_limiter.max_attempts
    with CountingVerify(delay=0.05) as verify:
        codes = await asyncio.gather(*(login(app, "admin", f"wrong-{i}") for i in range(40)))
    statuses = [c[0] for c in codes]
    assert statuses.count(401) == limit and statuses.count(429) == 40 - limit, statuses
    assert len(verify.calls) == limit, f"{len(verify.calls)} password checks, limit {limit}"
    # a sign-in that works resets that address's count
    state.rate_limiter.reset("127.0.0.1")
    state.name_limiter.reset("admin")
    for i in range(limit - 1):
        assert (await login(app, "admin", f"wrong-{i}"))[0] == 401
    assert (await login(app, "admin", PW))[0] == 303
    assert (await login(app, "admin", "wrong-again"))[0] == 401, "a sign-in resets the count"
    print(f"burst of 40 concurrent wrong sign-ins from one address: {limit} password checks, "
          f"{40 - limit}x429; a sign-in that works resets the count  OK")


def test_burst_counted_before_check():
    run(_burst, {"users": [_user("admin")]})


def test_ipv6_by_64():
    k = auth.address_key
    assert k("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == k("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert k("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert k("::ffff:192.0.2.7") == k("192.0.2.7") == "192.0.2.7"
    assert k("fe80::1%eth0") == "fe80::/64" and k("::1") == "::/64"
    assert k("testclient") == "testclient" and k("?") == "?"
    lim = auth.LoginRateLimiter()
    for i in range(lim.max_attempts):  # privacy addresses of one host / LAN
        assert not lim.blocked(f"2001:db8:1:2::{i + 1:x}")
        lim.record_failure(f"2001:db8:1:2::{i + 1:x}")
    assert lim.blocked("2001:db8:1:2:ffff::42") and not lim.blocked("2001:db8:1:3::1")
    assert len(lim._fails) == 1
    lim.reset("2001:db8:1:2::abcd")
    assert not lim.blocked("2001:db8:1:2::1")
    print("address limit: IPv6 by its /64 (any address in it), IPv4-mapped as IPv4  OK")


# ---------------------------------------------------------------- names without an account
async def _unknown_names(d: str, config: dict) -> None:
    state = _state(d, config)
    app = build_app(state)
    with CountingVerify() as verify:
        for i, name in enumerate(("nobody", "sso-user", "admin")):
            assert (await login(app, name, "wrong", client=f"10.0.0.{i}"))[0] == 401
    assert verify.calls[:2] == [auth.DUMMY_HASH] * 2, verify.calls
    assert verify.calls[2] == state.config.get_user("admin").password_hash
    assert auth.is_password_hash(auth.DUMMY_HASH) and not auth.verify_password("", auth.DUMMY_HASH)
    print("LDAP off: an unknown name and an OIDC account's name are checked against a dummy "
          "hash, like a wrong password  OK")


def test_unknown_names_cost_a_check():
    sso = {"username": "sso-user", "password_hash": "", "role": "viewer", "source": "oidc",
           "pwd_version": 1}
    run(_unknown_names, {"users": [_user("admin"), sso]})


# ---------------------------------------------------------------- the per-name limit
def _audit(state) -> list[list[str]]:
    try:
        with open(state.audit_path, encoding="utf-8") as fh:
            return [ln.split("\t") for ln in fh.read().split("\n") if ln]
    except OSError:
        return []


async def _per_name(d: str, config: dict) -> None:
    state = _state(d, config)
    app = build_app(state)
    lim = state.name_limiter
    assert (lim.max_attempts, lim.window_s) == (10, 900)
    spellings = ["admin", "ADMIN", "Admin"]
    for i in range(lim.max_attempts):  # a new address each time: the address limit never hits
        name = spellings[i % 3]
        assert (await login(app, name, "wrong", client=f"10.3.{i}.1"))[0] == 401
    s, _h, text = await login(app, "admin", PW, client="10.3.99.1")
    assert s == 429 and "Too many attempts" in text, "paused for this name, any address"
    assert (await login(app, "bob", PW, client="10.3.99.2"))[0] == 303, "other names aren't"
    with open(state.log_path, encoding="utf-8") as fh:
        assert "sign-ins under 'admin' are refused for now: 10 failed in 15 minutes" in fh.read()
    lim.window_s = 0.2  # the failures expire: the right password works again
    await asyncio.sleep(0.3)
    assert not lim.blocked("admin") and "admin" not in lim._fails
    lim.window_s = 900
    assert (await login(app, "admin", "wrong", client="10.3.99.3"))[0] == 401
    assert (await login(app, "admin", PW, client="10.3.99.4"))[0] == 303
    assert "admin" not in lim._fails, "a sign-in that works resets the name's count"
    # an unknown name is counted the same way (a 429 doesn't tell whether it exists)
    for i in range(lim.max_attempts):
        assert (await login(app, "nobody", "wrong", client=f"10.4.{i}.1"))[0] == 401
    assert (await login(app, "nobody", "wrong", client="10.4.99.1"))[0] == 429
    with open(state.log_path, encoding="utf-8") as fh:
        log = fh.read()
    assert "sign-ins under a name that is no account's are refused for now" in log
    assert "nobody" not in log, "a name that is no account's isn't logged"
    print("per name: 10 failed sign-ins in 15 minutes from any addresses (letter case aside) "
          "-> 429 for that name only, also with the right password; known and unknown names "
          "alike; expires; reset by a sign-in that works  OK")


def test_per_name_limit():
    run(_per_name, {"users": [_user("admin"), _user("bob", "viewer")]})


async def _per_name_directory(d: str, config: dict) -> None:
    from ser2netweb.web import ldap_auth

    asked = []

    def directory(settings, username, password, log, **kw):
        asked.append(username)
        return None  # wrong password

    real, ldap_auth.authenticate = ldap_auth.authenticate, directory
    try:
        state = _state(d, config)
        app = build_app(state)
        for i in range(12):
            await login(app, "jdoe" if i % 2 else "JDoe", "wrong", client=f"10.5.{i}.1")
    finally:
        ldap_auth.authenticate = real
    assert len(asked) == state.name_limiter.max_attempts, asked
    print(f"per name, LDAP: the directory is asked {len(asked)} times in 15 minutes for one "
          "name (any letter case), then not until they expire  OK")


def test_per_name_limit_directory():
    run(_per_name_directory, {"users": [_user("admin")], "ldap": {
        "enabled": True, "server_uri": "ldap://127.0.0.1:1",
        "user_dn_template": "uid={username},ou=people,dc=ex,dc=com"}})


async def _password_change(d: str, config: dict) -> None:
    state = _state(d, config)
    app = build_app(state)
    user = state.config.get_user("admin")
    session = auth.issue_session(state.config.secret_key, 3600, user.username, user.pwd_version)
    headers = {**FORM, "Cookie": f"ser2net_csrf={CSRF}; ser2net_session={session}"}

    def change(current: str):
        return call(app, "POST", "/settings/password",
                    {"current": current, "password": "brand-new-pass-1",
                     "password2": "brand-new-pass-1"}, headers=headers, client="10.6.0.1")

    for _ in range(state.name_limiter.max_attempts):
        s, _h, text = await change("wrong-current")
        assert s == 400 and "Current password is incorrect." in text
    s, _h, text = await change(PW)
    assert "Too many attempts" in text and auth.verify_password(PW, user.password_hash)
    failed = [r for r in _audit(state) if r[2] == "password_change_failed"]
    assert len(failed) == state.name_limiter.max_attempts and failed[0][3] == "admin", failed
    with open(state.log_path, encoding="utf-8") as fh:
        assert "password change for admin refused: wrong current password (from 10.6.0.1)" \
            in fh.read()
    assert (await login(app, "admin", PW, client="10.6.0.2"))[0] == 429, "the same name's limit"
    state.name_limiter.reset("admin")
    s, _h, text = await change(PW)
    assert s == 303 and auth.verify_password("brand-new-pass-1", user.password_hash), s
    print("password change: a wrong current password is logged, audited and counted under "
          "that name; past the limit the right one is refused too  OK")


def test_password_change_limited():
    run(_password_change, {"users": [_user("admin")]})


# ---------------------------------------------------------------- a few checks at a time
def test_slow_work_bounded():
    import threading
    import time

    async def go():
        work = auth.SlowWork(limit=2, queue=3)
        lock, now, most = threading.Lock(), [0], [0]

        def job(i):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
            time.sleep(0.05)
            with lock:
                now[0] -= 1
            return i

        tasks = [asyncio.create_task(work.run(job, i)) for i in range(5)]
        await asyncio.sleep(0)
        assert work.busy(), "2 running + 3 waiting"
        assert await asyncio.gather(*tasks) == list(range(5))
        assert most[0] == 2 and not work.busy(), most

    asyncio.run(go())
    print("SlowWork: at most `limit` jobs at a time, busy() once `queue` wait  OK")


async def _busy(d: str, config: dict) -> None:
    state = _state(d, config)
    state.password_work = auth.SlowWork(limit=1, queue=1)
    app = build_app(state)
    with CountingVerify(delay=0.3) as verify:
        answers = await asyncio.gather(*(login(app, "admin", "wrong", client=f"10.1.0.{i}")
                                         for i in range(4)))
    statuses = sorted(a[0] for a in answers)
    assert statuses == [401, 401, 429, 429], statuses
    assert len(verify.calls) == 2
    busy = [a for a in answers if a[0] == 429]
    assert all("Too many sign-ins at the same time" in a[2] for a in busy)
    assert len(state.rate_limiter._fails) == 2, "a sign-in that wasn't tried isn't counted"
    print("too many password checks waiting: 429 (not tried, not counted)  OK")


def test_busy_answers_429():
    run(_busy, {"users": [_user("admin")]})


def main():
    test_burst_counted_before_check()
    test_ipv6_by_64()
    test_unknown_names_cost_a_check()
    test_per_name_limit()
    test_per_name_limit_directory()
    test_password_change_limited()
    test_slow_work_bounded()
    test_busy_answers_429()
    print("\nPASS: sign-in attempts are limited")


if __name__ == "__main__":
    main()
