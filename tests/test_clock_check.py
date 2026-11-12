"""A system clock that reads earlier than it can is reported, never changed.

  * clock.behind(): the latest time the installation's files show the clock read already
    - the newest configuration backup (named in UTC), config.json's modification time, or
    the day this version was made - and whether the clock reads more than SLACK_S before
    it; a clock that is right (or a little behind: an NTP step back) is not reported;
  * the start-up log gets one WARNING then (prepare_config's notes: before anything at
    start-up writes config.json, so its earlier time still counts);
  * Settings -> Admin interface shows the server's time (UTC, local and its zone), the same
    warning for admins only (it names the newest backup's time), and an element that
    app.js fills when this browser's clock and the server's differ by more than 2 minutes
    (<body data-server-time>: the server's clock when it rendered the page);
  * nothing here sets a clock: the tests move the wall clock of the module they check
    (tests/_clock.py).

Run: python3 tests/test_clock_check.py
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _clock import WallClock                                     # noqa: E402

from app import clock, runtime                                   # noqa: E402
from app.config import AppConfig, ConfigStore                    # noqa: E402
from app.state import AppState                                   # noqa: E402
from app.web import auth                                         # noqa: E402
from app.web.routes import settings as settings_routes           # noqa: E402
from app.web.server import build_app                             # noqa: E402

DAY = 86400.0
PW = "clock-check-1"


def utc(*fields) -> float:
    return float(calendar.timegm((*fields, 0, 0, 0)[:9]))


def _data(backup: str | None = None, config_time: float | None = None) -> str:
    """A data folder with a config.json (modified at config_time) and maybe a backup."""
    d = tempfile.mkdtemp(prefix="s2n-clockchk-")
    path = os.path.join(d, "config.json")
    cfg = AppConfig.from_dict({
        "admin_ui": {"bind_ip": "127.0.0.1", "port": 8080},
        "users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                   "role": "admin", "pwd_version": 1},
                  {"username": "watcher", "password_hash": auth.hash_password(PW),
                   "role": "viewer", "pwd_version": 1}]})
    ConfigStore(path).save(cfg, validate=False)
    if backup:
        os.makedirs(os.path.join(d, "backups"), exist_ok=True)
        with open(os.path.join(d, "backups", backup), "w", encoding="utf-8") as fh:
            json.dump(cfg.to_dict(), fh)
    if config_time is not None:
        os.utime(path, (config_time, config_time))
    return d


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def test_behind():
    d = _data("config-20261111T120000Z.json", config_time=utc(2026, 11, 10, 8, 0, 0))
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        newest = utc(2026, 11, 11, 12, 0, 0)
        assert clock.behind(store, newest + DAY) is None, "a clock that is right"
        assert clock.behind(store, newest - clock.SLACK_S + 1) is None, "a small step back"
        assert clock.behind(store, newest - 3600) == ("backup", newest)
        assert clock.behind(store, utc(2009, 1, 1, 0, 0, 0)) == ("backup", newest), \
            "the latest of the known times is named"
        os.utime(store.path, (newest + 7200, newest + 7200))
        assert clock.behind(store, newest + 3600) == ("config", newest + 7200)
        shutil.rmtree(os.path.join(d, "backups"))
        os.remove(store.path)
        assert clock.behind(store, utc(2009, 1, 1, 0, 0, 0)) == ("release", clock.FLOOR)
        assert clock.behind(store, clock.FLOOR + 1) is None
        assert clock.FLOOR <= time.time(), "the floor can't be in the future"
        line = clock.warning(store, utc(2009, 1, 1, 0, 0, 5))
        assert line.startswith("WARNING: the system clock reads 2009-01-01 00:00:05 UTC, "
                               "before this version of ser2net was made (2026-11-01)"), line
        assert "never sets the clock" in line and clock.warning(store) == ""
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("clock.behind: the newest backup, config.json's time, the release day; a right "
          "clock and a small step back aren't reported  OK")


def test_start_up_warning():
    newest = utc(2026, 11, 11, 12, 0, 0)
    d = _data("config-20261111T120000Z.json", config_time=newest - 600)
    try:
        path = os.path.join(d, "config.json")
        for k in ("SER2NET_BIND_IP", "SER2NET_PORT", "SER2NET_ADMIN_PASSWORD_FILE"):
            os.environ.pop(k, None)
        with WallClock.patch(clock, shift=newest - 3 * DAY - time.time()):
            _store, cfg = runtime.prepare_config(path)
        found = [n for n in cfg.load_notes if "system clock" in n]
        assert len(found) == 1 and found[0].startswith(
            "WARNING: the system clock reads 2026-11-08 12:00:"), cfg.load_notes
        assert "earlier than the newest configuration backup (2026-11-11 12:00:00 UTC)" in found[0]
        _store, cfg = runtime.prepare_config(path)
        assert not [n for n in cfg.load_notes if "system clock" in n], cfg.load_notes

        # config.json written while the clock was a day ahead: its time counts even when
        # this start writes config.json (here: it has no instance_id yet)
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc.pop("instance_id", None)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        ahead = time.time() + DAY
        os.utime(path, (ahead, ahead))
        _store, cfg = runtime.prepare_config(path)
        found = [n for n in cfg.load_notes if "system clock" in n]
        assert len(found) == 1 and "earlier than config.json was last written" in found[0], \
            cfg.load_notes
        assert json.load(open(path, encoding="utf-8")).get("instance_id"), "not saved"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("start-up: one WARNING when the clock reads earlier than the newest backup or "
          "config.json's time; none when it is right  OK")


async def _get(app, path: str, cookie: str) -> tuple[int, str]:
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8080"), (b"cookie", cookie.encode())],
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8080)}
    out = {"status": 0, "body": b""}
    sent = asyncio.Event()

    async def receive():
        if not sent.is_set():
            sent.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    await asyncio.wait_for(app(scope, receive, send), 30)
    return out["status"], out["body"].decode("utf-8", "replace")


def test_settings():
    d = _data(time.strftime("config-%Y%m%dT%H%M%SZ.json", time.gmtime()),
              config_time=time.time() - 3600)
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        state = AppState(store, store.load())
        app = build_app(state)

        def page(user: str) -> str:
            session = auth.issue_session(state.config.secret_key, 3600, user, 1)
            status, text = asyncio.run(_get(app, "/settings", f"ser2net_session={session}"))
            assert status == 200, (status, text[:300])
            return text

        before = int(time.time())
        text = page("admin")
        served = re.search(r'<body data-server-time="(\d+)"', text)
        assert served and before <= int(served[1]) <= time.time() + 1, "data-server-time"
        shown = re.search(r'Server time: <span class="mono">([0-9: -]{19}) UTC</span> · local '
                          r'<span class="mono">([0-9: -]{19})</span> \((UTC[^)]*\)?)\)', text)
        assert shown, text[text.find("Admin interface"):][:900]
        assert abs(calendar.timegm(time.strptime(shown[1], "%Y-%m-%d %H:%M:%S")) - before) < 5
        assert shown[3] == clock.zone(), (shown[3], clock.zone())
        assert "data-clock-skew hidden" in text and "earlier than the newest" not in text
        print("Settings: the server's time (UTC, local, zone) and <body data-server-time>; no "
              "warning while the clock is right  OK")

        with WallClock.patch(settings_routes, shift=-2 * DAY):
            text = page("admin")
            assert "The server’s clock reads " in text and \
                "earlier than the newest configuration backup" in text, \
                text[text.find("Admin interface"):][:1200]
            assert "ser2net never sets the clock" in text
            viewer = page("watcher")
            assert "Server time: " in viewer and "The server’s clock reads" not in viewer, \
                "the warning names the newest backup's time: admins only"
        print("Settings: a clock behind the newest backup is shown to admins (not to a "
              "viewer)  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)

    js = open(os.path.join(os.path.dirname(__file__), "..", "app", "web", "static", "app.js"),
              encoding="utf-8").read()
    check = js[js.index(") Clock check: the server's clock"):]
    assert "data-server-time" in check and "[data-clock-skew]" in check and "<= 120" in check
    print("app.js: compares the browser's clock with data-server-time (2 minutes)  OK")


def main():
    test_behind()
    test_start_up_warning()
    test_settings()
    print("\nPASS: a clock that reads earlier than it can is reported (start-up log, "
          "Settings); the server's time is shown")


if __name__ == "__main__":
    main()
