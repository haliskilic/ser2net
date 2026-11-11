"""Per-mapping log retention: age (>15d) + size (>100MB) auto-trim; an import that
replaces the mappings, or a restored backup, closes the log files of the mappings that
are gone (the files stay until the next start prunes them).

Run: python3 tests/test_log_maintenance.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.state import _trim_log_file, AppState, LOG_MAX_AGE_DAYS
from app.config import AppConfig, ConfigStore, MappingConfig


def _close_loggers():
    # AppState opens FileHandlers that keep log files open; close them so the temp
    # dir can be removed on Windows (open files can't be deleted there).
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def ts(offset_s):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + offset_s))


def test_age_trim():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "x.log")
        with open(p, "w") as fh:
            fh.write(f"{ts(-20*86400)} OLD-A\n{ts(-16*86400)} OLD-B\n"
                     f"{ts(-3600)} NEW-A\n{ts(-60)} NEW-B\n")
        cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
        changed = _trim_log_file(p, cutoff, 100 * 1024 * 1024)
        content = open(p).read()
        assert changed
        assert "OLD-A" not in content and "OLD-B" not in content, content
        assert "NEW-A" in content and "NEW-B" in content, content
        print("age trim (>15d dropped, recent kept) OK")
    finally:
        shutil.rmtree(d)


def test_size_cap():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "y.log")
        with open(p, "w") as fh:
            for i in range(400):
                fh.write(f"{ts(-60)} line {i:04d} some payload text\n")
        before = os.path.getsize(p)
        changed = _trim_log_file(p, time.time() - LOG_MAX_AGE_DAYS * 86400, 2048)
        after = os.path.getsize(p)
        content = open(p).read()
        assert changed and before > 2048 and after <= 2048, (before, after)
        assert content[0].isdigit(), "must start on a clean line boundary"
        assert "line 0399" in content and "line 0000" not in content, "keep newest, drop oldest"
        print(f"size cap (kept last {after}B of {before}B, newest retained) OK")
    finally:
        shutil.rmtree(d)


async def _integration():
    d = tempfile.mkdtemp()
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        cfg = AppConfig()
        m = MappingConfig(name="M1")
        cfg.mappings.append(m)
        state = AppState(store, cfg)

        path = state.mapping_log_path(m.id)
        os.makedirs(state.logs_dir, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(f"{ts(-20*86400)} ANCIENT\n{ts(-60)} fresh event 1\n")

        log = state.make_mapping_logger(m)   # opens the file handler (append)
        await state.run_log_maintenance()    # should drop ANCIENT, keep fresh

        after = open(path).read()
        assert "ANCIENT" not in after, after
        assert "fresh event 1" in after, after

        # logging must still work after the rewrite (handler reattached)
        log("fresh event 2")
        after2 = open(path).read()
        assert "fresh event 2" in after2, after2
        print("integration (old dropped + handler reattached, logging continues) OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


async def _replaced():
    import json

    from test_login_throttle import CSRF, call

    from app.web import auth
    from app.web.server import build_app

    d = tempfile.mkdtemp()
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        cfg = AppConfig.from_dict({
            "users": [{"username": "admin", "password_hash": auth.hash_password("logs-pass-1"),
                       "role": "admin", "pwd_version": 1}],
            "mappings": [{"name": "First", "enabled": False, "serial": {"port": "/dev/ttyS0"}}]})
        store.save(cfg, validate=False)
        state = AppState(store, cfg)
        app = build_app(state)
        await state.supervisor.start_all(cfg)
        first = cfg.mappings[0].id
        session = auth.issue_session(cfg.secret_key, 3600, "admin", 1)
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Cookie": f"ser2net_csrf={CSRF}; ser2net_session={session}"}
        # a file without ids (like a classic ser2net.yaml): new ids at every import
        upload = json.dumps({"version": 1, "mappings": [
            {"name": n, "enabled": False, "serial": {"port": "/dev/ttyS0"},
             "network": {"port": port}} for n, port in (("A", 7001), ("B", 7002))]})
        seen = dict(state._mapping_loggers)
        for _ in range(3):  # the 2.7-style one-step import: replace everything
            s, _h, page = await call(app, "POST", "/settings/config/import", {"file": upload},
                                     headers=headers)
            assert s == 200 and "Imported 2 mappings" in page, page[page.find("banner"):][:300]
            now = {m.id for m in state.config.mappings}
            assert set(state._mapping_loggers) == now, (state._mapping_loggers, now)
            seen.update(state._mapping_loggers)
        gone = {mid: lg for mid, lg in seen.items() if mid not in now}
        assert len(gone) == 5 and all(not lg.handlers for lg in gone.values()), gone
        assert all(os.path.exists(state.mapping_log_path(mid)) for mid in gone), \
            "the files stay until the next start"
        # restoring the first backup brings "First" back and closes the imported ones
        oldest = state.store.backup_names()[-1]
        s, _h, page = await call(app, "POST", "/settings/backups/restore", {"name": oldest},
                                 headers=headers)
        assert s == 200 and [m.id for m in state.config.mappings] == [first], page[-300:]
        assert set(state._mapping_loggers) == {first}
        assert all(not seen[mid].handlers for mid in now)
        await state.supervisor.stop_all()
        state.prune_mapping_logs()
        assert sorted(os.listdir(state.logs_dir)) == [f"{first}.log"]
        print("replace import (x3, new ids each time) and backup restore: the log files of "
              "the mappings that are gone are closed; removed at the next start  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_age_trim()
    test_size_cap()
    asyncio.run(_integration())
    asyncio.run(_replaced())
    print("\nPASS: per-mapping log retention (age + size)")
