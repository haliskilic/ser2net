"""How sign-in attempts are limited.

Checks:
  * attempts are counted before the password is checked: a burst of concurrent wrong
    sign-ins from one address gets exactly `max_attempts` password checks, the rest 429;
    a sign-in that works resets the count.

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

from app.config import AppConfig, ConfigStore
from app.state import AppState
from app.web import auth
from app.web.server import build_app

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
    # another address isn't held up; a sign-in that works resets that address's count
    state.rate_limiter.reset("127.0.0.1")
    for i in range(limit - 1):
        assert (await login(app, "admin", f"wrong-{i}"))[0] == 401
    assert (await login(app, "admin", PW))[0] == 303
    assert (await login(app, "admin", "wrong-again"))[0] == 401, "a sign-in resets the count"
    print(f"burst of 40 concurrent wrong sign-ins from one address: {limit} password checks, "
          f"{40 - limit}x429; a sign-in that works resets the count  OK")


def test_burst_counted_before_check():
    run(_burst, {"users": [_user("admin")]})


def main():
    test_burst_counted_before_check()
    print("\nPASS: sign-in attempts are limited")


if __name__ == "__main__":
    main()
