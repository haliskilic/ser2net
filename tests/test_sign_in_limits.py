"""Limits on what a request without an account can make the server read, write or keep.

Checks:
  * request bodies: the sign-in, setup and sign-out forms at most 64 KB, a configuration
    import 16 MB, everything else (forms, the REST API, cluster peers) 2 MB; a larger one
    is answered 413 before the handler reads it (declared Content-Length) or as soon as it
    passes the limit (chunked); a file part in the sign-in form is refused (400);
  * a failed sign-in records the name typed only when it is an account's (letter case
    aside), else "(unknown user)", in all.log, audit.log and the notification; a name
    over 256 characters isn't tried (not sent to the directory); the LDAP failure line
    doesn't repeat the name;
  * audit records: every field cut to its limit (the all.log copy too), control
    characters, NEL and U+2028/U+2029 can't split one, all.log messages cut at 2000
    characters, no unread copy of the log kept in memory;
  * failed sign-ins past 20 in 10 minutes are counted, then summed up in one record;
  * console / hex view opens: one record per account (open access: per address),
    mapping and kind in 5 minutes, the next one saying how many it stands for; at most
    50 in 10 minutes, then summed up;
    directory sign-ins refused for their name (a local account's) count towards the
    same limit;
  * a REST API request refused for its token (none, wrong, expired): one api_auth_failed
    record per address a minute (the next says how many there were), at most 20 in 10
    minutes, then summed up; its all.log line, like any request refused before its
    handler, once per address and answer a minute, the next saying how many more;
  * the login rate limiter keeps a bounded number of addresses and failure times;
  * logouts: one account's ended cookies are remembered up to its share; past that the
    account is signed out everywhere (one save) instead of anyone's ended cookie being
    forgotten;
  * an open-access visitor's sign-out adds no audit record.

Most checks call the web app in this process (no socket); one boots a real server.
Run: python3 tests/test_sign_in_limits.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import tempfile
import time

from _optional import need
from _server import Browser, Server

import app.state as st
from app.config import AppConfig, ConfigStore, LdapSettings
from app.state import AppState, FailedLoginRecords, LineLimiter, ViewRecords
from app.web import auth, ldap_auth
from app.web.server import MAX_BODY_BYTES, build_app

PW = "limits-pass-1"
TOKEN = "s2n_sign-in-limits-token"
CSRF = "limits-csrf-token"
KB = 1024
MB = 1024 * KB


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _audit_rows(state) -> list[list[str]]:
    return [ln.split("\t") for ln in _read(state.audit_path).split("\n") if ln]


# ---------------------------------------------------------------- in-process requests
async def call(app, method: str, path: str, body: bytes = b"", *, headers=None,
               chunked: bool = False, declared: int | None = None, client: str = "127.0.0.1",
               piece: int = 64 * KB):
    """One request to the ASGI app: (status, headers, body text, bytes the app read).
    The body goes in `piece`-sized messages; it declares `declared` (default: its
    length) as Content-Length, or none with chunked=True."""
    hdrs = [(b"host", b"127.0.0.1:8080")]
    hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if not chunked:
        hdrs.append((b"content-length", str(len(body) if declared is None else declared).encode()))
    pieces = [body[i:i + piece] for i in range(0, len(body), piece)] or [b""]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": hdrs, "client": (client, 50000), "server": ("127.0.0.1", 8080)}
    done = asyncio.Event()
    read, out = [0], {"status": 0, "headers": {}, "body": b""}

    async def receive():
        if pieces:
            chunk = pieces.pop(0)
            read[0] += len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(pieces)}
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
    return out["status"], out["headers"], out["body"].decode("utf-8", "replace"), read[0]


def form(fields: dict) -> bytes:
    import urllib.parse
    return urllib.parse.urlencode({"_csrf": CSRF, **fields}).encode()


def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "s2nlimits7c1f"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in {"_csrf": CSRF, **fields}.items()]
    for k, (fname, content) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                     f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                     + content + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


FORM = {"Content-Type": "application/x-www-form-urlencoded", "Cookie": f"ser2net_csrf={CSRF}"}


def signed_in(state, name: str = "admin") -> dict:
    user = state.config.get_user(name)
    session = auth.issue_session(state.config.secret_key, 3600, user.username, user.pwd_version)
    return {"Cookie": f"ser2net_csrf={CSRF}; ser2net_session={session}", "X-CSRF-Token": CSRF}


def _state(d: str, config: dict) -> AppState:
    return AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig.from_dict(config))


async def _in_process(d: str) -> None:
    config = {"users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": auth.hash_token(TOKEN), "api_token_role": "operator",
              "notifications": {"enabled": True, "events": ["login_failed"], "repeat_s": 0},
              "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:1",
                       "user_dn_template": "uid={username},ou=people,dc=ex,dc=com"}}
    state = _state(d, config)
    app = build_app(state)
    all_log = state.log_path

    # ---- sign-in form: body limits ----
    huge = form({"username": "u" * MB, "password": "x"})
    s, _h, text, read = await call(app, "POST", "/login", huge, headers=FORM)
    assert s == 413 and read == 0, (s, read, text[:80])
    s, _h, _t, read = await call(app, "POST", "/login", huge, headers=FORM, chunked=True)
    assert s in (400, 413) and read <= 128 * KB, (s, read)  # a field over 16 KB: 400
    s, _h, _t, read = await call(app, "POST", "/login", b"", headers=FORM, declared=MB)
    assert s == 413 and read == 0, (s, read)
    body, ctype = multipart({"password": "x"}, {"username": ("name.txt", b"admin")})
    s, _h, text, _r = await call(app, "POST", "/login", body,
                                 headers={**FORM, "Content-Type": ctype})
    assert s == 400, (s, text[:120])
    assert "failed login" not in _read(all_log), "a refused body must not count as a sign-in"
    print("sign-in form: over 64 KB -> 413 before it is read (declared or chunked); a file "
          "part -> 400  OK")

    # ---- failed sign-ins: what is recorded ----
    secret = "Hunter2-Correct-Horse"           # a password typed into the name field
    cases = [(secret, auth.UNKNOWN_USER), ("ADMIN", "ADMIN"), ("admin", "admin"),
             ("n" * 300, auth.UNKNOWN_USER), ("x" * 16000, auth.UNKNOWN_USER)]
    for i, (name, _label) in enumerate(cases):
        before = _read(all_log)
        s, _h, text, _r = await call(app, "POST", "/login",
                                     form({"username": name, "password": "wrong-pass"}),
                                     headers=FORM, client=f"10.0.0.{i + 1}")
        assert s == 401, (s, text[:120])
        new = _read(all_log)[len(before):]
        tried_ldap = "LDAP" in new
        if len(name) > auth.LOGIN_NAME_MAX:
            assert not tried_ldap, f"a {len(name)}-character name was sent to the directory"
        elif name == secret:
            assert tried_ldap, "an unknown name is tried against the directory"
    logged, audited = _read(all_log), _read(state.audit_path)
    assert secret not in logged and secret not in audited, "the typed name reached a log"
    assert "n" * 300 not in logged and "x" * 300 not in audited
    for i, (_name, label) in enumerate(cases):
        assert f"failed login for {label!r} from 10.0.0.{i + 1}" in logged, label
    users = [r[4] for r in _audit_rows(state) if r[2] == "login_failed"]
    assert users == [label for _n, label in cases], users
    assert max(len(ln) for ln in logged.split("\n")) <= st.LOG_MESSAGE_MAX + 60
    assert max(len(ln) for ln in audited.split("\n")) <= 120
    queued = []
    while not state.notifier._queue.empty():
        queued.append(state.notifier._queue.get_nowait()[2])
    assert queued == [f"user {label!r} from 10.0.0.{i + 1}" for i, (_n, label) in enumerate(cases)]
    print("failed sign-in: an account's name (any case) as typed, any other name as "
          "'(unknown user)' in all.log, audit.log and the notification; over 256 characters "
          "not tried; short lines  OK")

    # ---- signed-in requests: the default limit and the import's ----
    admin = signed_in(state)
    s, _h, _t, read = await call(app, "POST", "/api/mappings/save", b"name=x",
                                 headers={**admin, "Content-Type": FORM["Content-Type"]},
                                 declared=MAX_BODY_BYTES + 1)
    assert s == 413 and read == 0, (s, read)
    s, _h, _t, read = await call(app, "POST", "/api/mappings/save",
                                 b"name=" + b"x" * (3 * MB),
                                 headers={**admin, "Content-Type": FORM["Content-Type"]},
                                 chunked=True)
    assert s in (400, 413) and read <= MAX_BODY_BYTES + 64 * KB, (s, read)  # 400: a 1 MB field
    big = b'{"mappings": []}' + b" " * (3 * MB)
    body, ctype = multipart({"step": "preview", "mode": "merge"}, {"file": ("big.json", big)})
    s, _h, text, read = await call(app, "POST", "/settings/config/import", body,
                                   headers={**admin, "Content-Type": ctype})
    assert s == 200 and read == len(body) and "Import preview" in text, (s, read)
    s, _h, _t, read = await call(app, "POST", "/settings/config/import", b"",
                                 headers={**admin, "Content-Type": ctype}, declared=17 * MB)
    assert s == 413 and read == 0, (s, read)
    body, ctype = multipart({"step": "preview"}, {"file": ("big.json", b" " * (17 * MB))})
    s, _h, _t, read = await call(app, "POST", "/settings/config/import", body,
                                 headers={**admin, "Content-Type": ctype}, chunked=True,
                                 piece=MB)
    assert s == 413 and read <= 17 * MB, (s, read)
    s, _h, _t, read = await call(app, "POST", "/logout", b"",
                                 headers={**admin, "Content-Type": FORM["Content-Type"]},
                                 declared=100 * KB)
    assert s == 413 and read == 0, (s, read)
    print("signed in: forms over 2 MB -> 413; an import up to 16 MB is read, a larger one "
          "-> 413; sign-out over 64 KB -> 413  OK")

    # ---- REST API (bearer token) ----
    api = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    s, _h, text, _r = await call(app, "POST", "/api/v1/mappings", b"not json", headers=api)
    assert s == 400 and "invalid_json" in text, (s, text)
    s, _h, _t, read = await call(app, "POST", "/api/v1/mappings", b" " * (3 * MB),
                                 headers=api, chunked=True)
    assert s == 413 and read <= MAX_BODY_BYTES + 64 * KB, (s, read)
    s, _h, _t, read = await call(app, "PUT", "/api/v1/mappings/nosuch", b"{}", headers=api,
                                 declared=3 * MB)
    assert s == 413 and read == 0, (s, read)
    print("REST API: a JSON body over 2 MB -> 413 (not 'invalid_json'), a bad one still 400  OK")

    # ---- open access: a visitor's sign-out is no audit record ----
    state.config.admin_ui.require_login = False
    before = len(_audit_rows(state))
    s, h, _t, _r = await call(app, "POST", "/logout", form({}), headers=FORM)
    assert s == 303 and h.get("location") == "/login", s
    assert len(_audit_rows(state)) == before, _audit_rows(state)[before:]
    s, _h, _t, _r = await call(app, "POST", "/logout", form({}),
                               headers={**FORM, **signed_in(state)})
    assert s == 303 and _audit_rows(state)[-1][2:] == ["logout", "", "admin"], _audit_rows(state)[-1]
    state.config.admin_ui.require_login = True
    print("sign-out: recorded for an account, not for an open-access visitor  OK")

    # ---- first-run setup ----
    fresh = _state(os.path.join(d, "fresh"), {})
    setup_app = build_app(fresh)
    s, _h, _t, read = await call(setup_app, "POST", "/setup", b"", headers=FORM, declared=100 * KB)
    assert s == 413 and read == 0, (s, read)
    s, _h, _t, read = await call(setup_app, "POST", "/setup",
                                 form({"username": "admin", "password": "p" * (100 * KB)}),
                                 headers=FORM, chunked=True)
    assert s in (400, 413) and not fresh.config.users, (s, fresh.config.users)
    s, h, _t, _r = await call(setup_app, "POST", "/setup",
                              form({"username": "admin", "password": PW, "password2": PW}),
                              headers=FORM)
    assert s == 303 and fresh.config.get_user("admin") is not None, s
    print("first-run setup: over 64 KB -> 413, a normal form still creates the admin  OK")


def test_in_process():
    d = tempfile.mkdtemp(prefix="s2n_limits_")
    try:
        asyncio.run(_in_process(d))
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- unit checks
def test_audit_and_log_caps():
    d = tempfile.mkdtemp(prefix="s2n_limits_")
    try:
        state = _state(d, {})
        assert not hasattr(state, "logs"), "no copy of the log is kept in memory"
        state.audit("1" * MB, "a" * MB, "d" * MB, user="u" * MB)
        row = _audit_rows(state)[-1]
        assert [len(f) for f in row[1:]] == list(st.AUDIT_FIELD_MAX), [len(f) for f in row]
        echo = [ln for ln in _read(state.log_path).split("\n") if " AUDIT " in ln][-1]
        assert len(echo) < 1000, len(echo)
        state.audit("10.0.0.1", "mapping_save", "Pompa\u20281\x85b\u2029c\x9b[31m", user="op1")
        with open(state.audit_path, "a", encoding="utf-8") as fh:  # as an older version kept it
            fh.write("2026-11-01 10:00:00\t10.0.0.2\tmapping_save\tPLC\u2028x\x85y\top2\n")
        recs = state.read_audit()
        assert recs[0]["user"] == "op2" and recs[0]["detail"] == "PLC\u2028x\x85y", recs[0]
        assert recs[1]["user"] == "op1" and recs[1]["detail"] == "Pompa 1 b c [31m", recs[1]
        assert len(recs) == 3, recs
        state.log("y" * MB)
        last = _read(state.log_path).rstrip("\n").split("\n")[-1]
        assert len(last) <= st.LOG_MESSAGE_MAX + 60 and "more characters)" in last, len(last)
        print("audit fields cut to (64, 64, 500, 256), the all.log copy too; NEL / U+2028 / "
              "C1 can't split a record (old records read whole); log lines cut at 2000  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def test_failed_login_records():
    got = []

    def audit(ip, action, detail="", user=""):
        got.append((ip, action, detail, user))

    rec = FailedLoginRecords(audit, limit=3, window_s=0.4)
    assert [rec.add(f"10.0.0.{i}", auth.UNKNOWN_USER) for i in range(3)] == [True] * 3
    for ip in ["10.0.0.9"] * 5 + ["10.0.1.1", "10.0.1.2", "10.0.1.3"]:
        assert not rec.add(ip, auth.UNKNOWN_USER)
    assert len(got) == 3 and all(g[2] == "" for g in got), got
    rec.flush()  # no event loop here: the next add() after the window, or stop_engine
    ip, action, detail, user = got[3]
    assert (ip, action, user) == ("", "login_failed", auth.UNKNOWN_USER), got[3]
    assert detail.startswith("8 more failed sign-ins since ") and detail.endswith(
        "not recorded one by one: 5 from 10.0.0.9, 1 from 10.0.1.1, 1 from 10.0.1.2, "
        "1 from other addresses"), detail
    rec.flush()
    assert len(got) == 4, "nothing more to write"

    got.clear()
    rec = FailedLoginRecords(audit, limit=1, window_s=0.4)
    rec.add("10.0.0.1", "admin")
    rec.add("10.0.0.5", "admin")
    rec.add("10.0.0.5", auth.UNKNOWN_USER)
    time.sleep(0.45)
    assert rec.add("10.0.0.7", "admin"), "a new window: recorded one by one again"
    assert [(g[0], g[3]) for g in got] == [("10.0.0.1", "admin"), ("10.0.0.5", ""),
                                          ("10.0.0.7", "admin")], got
    assert got[1][2].endswith(": 2 from 10.0.0.5"), got[1]

    async def timed():
        got.clear()
        rec = FailedLoginRecords(audit, limit=1, window_s=0.3)
        rec.add("10.0.0.1", "admin")
        rec.add("10.0.0.2", "admin")
        assert len(got) == 1
        await asyncio.sleep(0.5)  # no other sign-in: the window's end writes the sum
        assert len(got) == 2 and got[1][0] == "10.0.0.2" and got[1][3] == "admin", got

    asyncio.run(timed())

    d = tempfile.mkdtemp(prefix="s2n_limits_")
    try:
        state = _state(d, {})
        for i in range(st.FAILED_LOGIN_RECORDS + 30):
            state.failed_logins.add(f"2001:db8::{i:x}", auth.UNKNOWN_USER)
        assert len(_audit_rows(state)) == st.FAILED_LOGIN_RECORDS
        asyncio.run(state.stop_engine())
        rows = _audit_rows(state)
        assert len(rows) == st.FAILED_LOGIN_RECORDS + 1 and rows[-1][3].startswith("30 more"), rows[-1]
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)
    print("failed sign-ins: 20 per 10 minutes recorded one by one, the rest summed up in one "
          "record (at the window's end, the next sign-in or shutdown)  OK")


def test_view_records():
    got = []

    def audit(ip, action, detail="", user=""):
        got.append((ip, action, detail, user))

    rec = ViewRecords(audit, limit=4, window_s=30, repeat_s=0.3)
    anon = ("(no login)", "10.0.0.1", "console_open", "m1")
    assert rec.open("10.0.0.1", "(no login)", "console_open", "CON (read-only)", anon)
    for _ in range(5):  # the same visitor, mapping and kind again: counted
        assert not rec.open("10.0.0.1", "(no login)", "console_open", "CON (read-only)", anon)
    assert rec.open("10.0.0.2", "(no login)", "console_open", "CON (read-only)",
                    ("(no login)", "10.0.0.2", "console_open", "m1")), "another address"
    assert rec.open("10.0.0.1", "bob", "hex_view_open", "CON (device output only)",
                    ("bob", "", "hex_view_open", "m1")), "another kind"
    assert len(got) == 3, got
    time.sleep(0.35)
    assert rec.open("10.0.0.1", "(no login)", "console_open", "CON (read-only)", anon)
    assert got[3][2].startswith("CON (read-only); opened 5 more times after ") and \
        got[3][2].endswith(", not recorded one by one"), got[3]
    for i in range(3):  # the window's 4 records are used up: counted, then summed up
        assert not rec.open(f"10.0.1.{i}", "(no login)", "console_open", "X (read-only)",
                            ("(no login)", f"10.0.1.{i}", "console_open", "m2"))
    rec.flush()
    ip, action, detail, user = got[4]
    assert (ip, action, user) == ("", "console_open", "(no login)"), got[4]
    assert detail.startswith("3 more console and hex view opens since "), detail
    rec = ViewRecords(audit, limit=10**6, window_s=60, repeat_s=60)
    rec.MAX_KEYS = 50
    for i in range(200):
        rec.open(f"10.9.{i // 256}.{i % 256}", "(no login)", "console_open", "X",
                 ("(no login)", f"10.9.{i // 256}.{i % 256}", "console_open", "m"))
    assert len(rec._seen) <= rec.MAX_KEYS
    print("console / hex view opens: once per account or address, mapping and kind in the "
          "repeat time (the next record counts the rest), at most the limit, then summed up; "
          "keys bounded  OK")


def test_line_limiter():
    lim = LineLimiter(every_s=0.3)
    assert lim.line(("1.2.3.4", 401), "A") == "A"
    assert [lim.line(("1.2.3.4", 401), "A") for _ in range(4)] == [None] * 4
    assert lim.line(("1.2.3.4", 303), "B") == "B" and lim.line(("5.6.7.8", 401), "C") == "C"
    time.sleep(0.35)
    again = lim.line(("1.2.3.4", 401), "A")
    assert again.startswith("A (4 more like it since ") and \
        again.endswith(", not logged one by one)"), again
    assert lim.line(("1.2.3.4", 401), "A") is None
    lim = LineLimiter(every_s=60)
    lim.MAX_KEYS = 50
    assert all(lim.line((f"10.0.{i // 256}.{i % 256}", 401), "x") == "x" for i in range(200))
    assert len(lim._keys) <= lim.MAX_KEYS, "keys bounded (past that: logged, not counted)"
    print("refused-request lines: once per key in the time, the next one counts the rest; "
          "keys bounded  OK")


async def _api_refusals(d: str) -> None:
    state = _state(d, {
        "users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                   "role": "admin", "pwd_version": 1}],
        "api_token_hash": auth.hash_token(TOKEN), "api_token_role": "viewer",
        "api_tokens": [{"id": "0123456789abcdef", "name": "old-ci", "role": "operator",
                        "sha256": auth.hash_token("s2n_expired"), "expires": 1}]})
    app = build_app(state)
    state.api_auth_failures.repeat_s = state.refused_lines.every_s = 0.4

    async def get(path, token=None, client="10.0.0.1", method="GET"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return (await call(app, method, path, headers=headers, client=client))[0]

    def records():
        return [r for r in _audit_rows(state) if r[2] == "api_auth_failed"]

    assert await get("/api/v1/mappings", "s2n_wrong") == 401
    for _ in range(3):
        assert await get("/api/v1/mappings", "s2n_wrong") == 401
    assert await get("/api/v1/status", None, "10.0.0.2") == 401
    assert await get("/api/v1/mappings", "s2n_expired", "10.0.0.3") == 401
    assert [(r[1], r[3], r[4]) for r in records()] == [
        ("10.0.0.1", "GET /api/v1/mappings: wrong token", ""),
        ("10.0.0.2", "GET /api/v1/status: no token", ""),
        ("10.0.0.3", "GET /api/v1/mappings: expired token", "api-token:old-ci")], records()
    await asyncio.sleep(0.45)
    assert await get("/api/v1/mappings", "s2n_wrong") == 401
    assert records()[-1][3] == ("GET /api/v1/mappings: wrong token; 3 more after "
                                f"{records()[0][0][11:]}, not recorded one by one"), records()[-1]
    # a viewer token's write, a read and a quiet poll: all.log only (not audited)
    assert await get("/api/v1/mappings", TOKEN, method="POST") == 403
    assert await get("/api/v1/mappings", TOKEN) == 200
    assert await get("/api/v1/status", TOKEN) == 200 and await get("/api/v1/health") == 200
    assert len(records()) == 4
    log = _read(state.log_path)
    assert log.count("HTTP 10.0.0.1 GET /api/v1/mappings -> 401\n") == 1, log
    assert "HTTP 10.0.0.1 GET /api/v1/mappings -> 401 (3 more like it since " in log, log
    assert "HTTP 10.0.0.3 GET /api/v1/mappings -> 401 by api-token:old-ci\n" in log, log
    assert "HTTP 10.0.0.1 POST /api/v1/mappings -> 403 by api-token\n" in log, log
    assert "HTTP 10.0.0.1 GET /api/v1/mappings -> 200 by api-token\n" in log, log
    assert "GET /api/v1/status -> 200" not in log and "/api/v1/health" not in log, log
    # past the limit in the window: counted, then summed up in one record
    for i in range(st.FAILED_LOGIN_RECORDS + 5):
        await get("/api/v1/ports", "s2n_wrong", f"10.1.0.{i}")
    assert len(records()) == st.FAILED_LOGIN_RECORDS, len(records())
    state.api_auth_failures.flush()
    last = records()[-1]
    assert last[3].startswith("9 more refused REST API requests since ") and last[1] == "", last


def test_api_refusals():
    d = tempfile.mkdtemp(prefix="s2n_limits_")
    try:
        asyncio.run(_api_refusals(d))
        print("REST API: a request refused for its token is audited once per address a minute "
              "(the next counts the rest), bounded; all.log: requests but quiet polls, by "
              "token, refusals once a minute  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


async def _directory_refusals(d: str) -> None:
    state = _state(d, {
        "users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                   "role": "admin", "pwd_version": 1}],
        "notifications": {"enabled": True, "events": ["login_failed"], "repeat_s": 0},
        "ldap": {"enabled": True, "server_uri": "ldap://127.0.0.1:1",
                 "user_dn_template": "uid={username},ou=people,dc=ex,dc=com"}})
    app = build_app(state)
    limit = st.FAILED_LOGIN_RECORDS
    for i in range(limit + 5):  # the directory knows "ADMIN": the local admin's name
        s, _h, text, _r = await call(app, "POST", "/login",
                                     form({"username": "ADMIN", "password": "dir-pass"}),
                                     headers=FORM, client=f"10.2.0.{i + 1}")
        assert s == 403 and "local account named admin" in text, (s, text[:160])
    s, _h, _t, _r = await call(app, "POST", "/login",
                               form({"username": "nobody", "password": "wrong-pass"}),
                               headers=FORM, client="10.2.1.1")
    assert s == 401, s
    rows = _audit_rows(state)
    assert [r[2] for r in rows] == ["ldap_login_denied"] * limit, rows
    assert rows[0][3:] == ["local account admin", "ADMIN"], rows[0]
    queued = 0
    while not state.notifier._queue.empty():
        queued += 1
        state.notifier._queue.get_nowait()
    assert queued == limit + 6, "notified like a failed sign-in"
    await state.stop_engine()
    rows = _audit_rows(state)
    assert len(rows) == limit + 1 and rows[-1][2] == "login_failed", rows[-1]
    assert rows[-1][3].startswith("6 more failed sign-ins since "), rows[-1]
    assert all(r[2] != "ldap_login_denied" for r in rows[limit:]), rows[limit:]


def test_directory_refusals_bounded():
    d = tempfile.mkdtemp(prefix="s2n_limits_dir_")
    real = ldap_auth.authenticate
    ldap_auth.authenticate = lambda *a, **k: ["staff"]  # the directory accepts any password
    try:
        asyncio.run(_directory_refusals(d))
    finally:
        ldap_auth.authenticate = real
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)
    print("directory sign-ins refused for a local account's name: recorded one by one within "
          "the failed sign-ins' limit, then summed up with them  OK")


async def _logouts(d: str) -> None:
    state = _state(d, {"users": [
        {"username": "admin", "password_hash": auth.hash_password(PW), "role": "admin",
         "pwd_version": 1},
        {"username": "bob", "password_hash": auth.hash_password(PW), "role": "viewer",
         "pwd_version": 7}]})
    app = build_app(state)
    state.revoked_sessions.PER_USER = 3
    secret = state.config.secret_key

    async def logout(token):
        s, h, _t, _r = await call(app, "POST", "/logout", form({}), headers={
            **FORM, "Cookie": f"ser2net_csrf={CSRF}; ser2net_session={token}"})
        assert s == 303 and h.get("location") == "/login", s

    victim = auth.issue_session(secret, 3600, "admin", 1)
    await logout(victim)  # someone else's cookie, ended earlier (a copy may exist)
    bobs = [auth.issue_session(secret, 3600, "bob", 7) for _ in range(4)]
    for t in bobs[:3]:
        await logout(t)
    assert state.config.get_user("bob").pwd_version == 7 and not os.path.exists(state.store.path)
    live = auth.issue_session(secret, 3600, "bob", 7)  # bob's session in another browser
    await logout(bobs[3])  # no room for it: bob is signed out everywhere
    assert state.config.get_user("bob").pwd_version == 8
    with open(state.store.path, encoding="utf-8") as fh:
        saved = AppConfig.from_dict(json.load(fh)).get_user("bob")
    assert saved.pwd_version == 8, "the sign-out must be saved"
    for t in bobs + [live]:
        assert auth.session_user(state.config, t, state.revoked_sessions) is None
    assert victim in state.revoked_sessions, "another account's ended cookie was forgotten"
    assert auth.session_user(state.config, victim, state.revoked_sessions) is None
    assert "'bob' signed out everywhere: too many of its sessions" in _read(state.log_path)


def test_revocation_list_full():
    d = tempfile.mkdtemp(prefix="s2n_limits_logout_")
    try:
        asyncio.run(_logouts(d))
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)
    print("logouts past an account's share: that account is signed out everywhere (saved); "
          "another account's ended cookie stays refused  OK")


def test_rate_limiter_bounds():
    lim = auth.LoginRateLimiter(max_attempts=3, window_s=0.3)
    for i in range(2000):
        assert not lim.blocked(f"10.1.{i // 256}.{i % 256}")
    assert not lim._fails, "checking an address must not keep it"
    for i in range(lim.MAX_ADDRESSES + 500):
        lim.record_failure(f"2001:db8::{i:x}")
    assert len(lim._fails) == lim.MAX_ADDRESSES
    assert "2001:db8::0" not in lim._fails and f"2001:db8::{lim.MAX_ADDRESSES + 499:x}" in lim._fails
    lim.record_failure("2001:db8::1f4")          # failing again: kept longest
    lim.record_failure("2001:db8::ffff")
    assert "2001:db8::1f4" in lim._fails and "2001:db8::1f5" not in lim._fails
    for _ in range(10):
        lim.record_failure("10.9.9.9")
    assert len(lim._fails["10.9.9.9"]) == 3 and lim.blocked("10.9.9.9")
    time.sleep(0.35)
    assert not lim.blocked("10.9.9.9") and "10.9.9.9" not in lim._fails
    print(f"rate limiter: at most {lim.MAX_ADDRESSES} addresses, max_attempts times each; "
          "an address that only checks or whose failures expired isn't kept; still blocks  OK")


def test_ldap_failure_line():
    if not need("ldap3", what="the LDAP failure log line"):
        return
    lines = []

    def refuse(server, user, password):
        raise RuntimeError("automatic bind not successful - invalidCredentials")

    s = LdapSettings(enabled=True, server_uri="ldap://h",
                     user_dn_template="uid={username},ou=people,dc=ex,dc=com")
    typed = "Typed-Password-9"
    assert ldap_auth.authenticate(s, typed, "pw", lines.append, server=object(),
                                  conn_factory=refuse) is None
    assert lines and all(typed not in ln for ln in lines), lines
    assert lines[0].startswith("LDAP authentication failed: RuntimeError"), lines
    print("LDAP: a refused bind is logged without the name typed  OK")


# ---------------------------------------------------------------- a real server
def _raw(port: int, head: str, body: bytes = b"") -> int:
    """Send a request over a socket (headers + the start of a body) and return the status
    the server answers with before reading the rest."""
    with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
        s.sendall(head.encode() + b"\r\n\r\n" + body)
        data = b""
        while b"\r\n" not in data:
            got = s.recv(4096)
            if not got:
                break
            data += got
    return int(data.split(b" ", 2)[1])


def test_live():
    users = [{"username": "admin", "password_hash": auth.hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    with Server({"users": users}) as srv:
        b = Browser(srv.base)
        csrf = b.csrf()
        head = (f"POST /login HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\nConnection: close\r\n"
                f"Content-Type: application/x-www-form-urlencoded\r\n"
                f"Cookie: ser2net_csrf={csrf}\r\nContent-Length: {MB}")
        assert _raw(srv.port, head, b"_csrf=" + csrf.encode() + b"&username=") == 413
        st_, _h, _p = b.post("/login", {"username": "Typed-Into-The-Name-Field", "password": "x"})
        assert st_ == 401
        b.login("admin", PW)
        out = srv.output()
        assert "failed login for '(unknown user)' from 127.0.0.1" in out, out[-1500:]
        assert "Typed-Into-The-Name-Field" not in out
        audit = _read(os.path.join(srv.data, "audit.log"))
        assert "\tlogin_failed\t\t(unknown user)\n" in audit and "Typed-Into" not in audit, audit
    print("server: a 1 MB sign-in body -> 413 without reading it; an unknown name is "
          "'(unknown user)' in the log and audit.log  OK")


def main():
    test_audit_and_log_caps()
    test_failed_login_records()
    test_directory_refusals_bounded()
    test_view_records()
    test_line_limiter()
    test_api_refusals()
    test_revocation_list_full()
    test_rate_limiter_bounds()
    test_ldap_failure_line()
    test_in_process()
    test_live()
    print("\nPASS: request body limits, failed sign-in records, bounded logs and limiter")


if __name__ == "__main__":
    main()
