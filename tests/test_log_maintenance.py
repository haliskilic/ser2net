"""Per-mapping log retention: age (>15d) + size (>100MB) auto-trim; the age trim never
cuts into the last lines the Log panel shows, so a clock step can't empty it; an import
that replaces the mappings, or a restored backup, closes the log files of the mappings that
are gone (the files stay until the next start prunes them).

The trim streams: a file within its limits is left alone after its first line, what is
kept is copied in chunks byte for byte, in about constant memory, and lines the mapping
logs meanwhile are kept.

Only a line feed ends a line (CRLF kept as it is): a CR, NEL or U+2028 inside a message
doesn't, and a line without a time (the rest of a message an older version wrote with
a line break in it) goes or stays with the line before it - it can't stop the age trim.

Run: python3 tests/test_log_maintenance.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.state as state_mod
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


def test_undated_lines():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "z.log")
        recent = f"{ts(-3600)} NEW-A\u2028x\r\nrest of NEW-A\n{ts(-60)} NEW-B\r\n"
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"{ts(-20*86400)} OLD point 'a\u2028b' failed\r\n"
                     f"rest of an old message\n"
                     f"{ts(-16*86400)} OLD-B\rstill OLD-B\x85 and \u2029 too\r\n" + recent)
        cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
        assert _trim_log_file(p, cutoff, 100 * 1024 * 1024)
        with open(p, encoding="utf-8", newline="") as fh:
            content = fh.read()
        assert content == recent, content
        assert not _trim_log_file(p, cutoff, 100 * 1024 * 1024), "nothing more to trim"
        # the orphan line an older version left at the top: skipped, not a stop
        new = f"{ts(-60)} NEW\n"
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"x\u2028y orphan\n{ts(-17*86400)} OLD\n" + new)
        assert _trim_log_file(p, cutoff, 100 * 1024 * 1024)
        assert open(p, encoding="utf-8", newline="").read() == new
        # no line with a time at all: kept (its age is unknown)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("no time here\nnor here\n")
        assert not _trim_log_file(p, cutoff, 100 * 1024 * 1024)
        print("age trim: only line feeds end a line; undated lines follow their record "
              "(an orphan no longer stops the trim)  OK")
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


def _cutoff():
    return time.time() - LOG_MAX_AGE_DAYS * 86400


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def test_within_limits_untouched():
    """A file within its limits is neither rewritten nor read beyond its first line."""
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "w.log")
        with open(p, "w") as fh:
            for i in range(2000):
                fh.write(f"{ts(-3600)} line {i}\n")
        before = os.stat(p)
        reads = []
        real_open = open

        def counting_open(*a, **kw):
            fh = real_open(*a, **kw)
            real_readline = fh.readline
            fh.readline = lambda *ra: reads.append(1) or real_readline(*ra)
            return fh
        with mock.patch("builtins.open", counting_open):
            assert not _trim_log_file(p, _cutoff(), 1024 * 1024, keep_lines=1000)
        after = os.stat(p)
        assert (after.st_ino, after.st_mtime_ns, after.st_size) == \
            (before.st_ino, before.st_mtime_ns, before.st_size)
        assert len(reads) == 1, f"read {len(reads)} lines of a file with nothing to trim"
        assert not os.path.exists(p + ".tmp")
        print("a file within its limits: one line read, not rewritten  OK")
    finally:
        shutil.rmtree(d)


def test_streaming_memory_and_bytes():
    """Over its cap: cut to 90% of it from a line's start, byte for byte (CRLF, invalid
    UTF-8, non-ASCII), in about constant memory; the next pass leaves it."""
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "big.log")
        line = lambda i, age: (f"{ts(age)} poll 'sıcaklık {i:06d}' fehlgeschlagen   x"
                               .encode() + b" \xff\xfe\r\n")
        with open(p, "wb") as fh:
            for i in range(30000):  # about 30 days, the first ones old
                fh.write(line(i, -(30000 - i) * 86.4))
        data = _bytes(p)
        size, cap = len(data), 256 * 1024
        assert size > 4 * cap
        tracemalloc.start()
        try:
            assert _trim_log_file(p, _cutoff(), cap)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        kept = _bytes(p)
        assert len(kept) <= cap * 0.9 and len(kept) > cap * 0.9 - 200, len(kept)
        assert data.endswith(kept) and data[-len(kept) - 1:-len(kept)] == b"\n", "a line's start"
        # about one copy chunk (1 MiB), not several times the file (reading it whole took
        # about 4.7 times its size)
        assert peak < 2 * 1024 * 1024 < 1.5 * size, f"peak {peak} B for a {size} B file"
        assert not _trim_log_file(p, _cutoff(), cap), "trimmed again at once"
        # the age filter alone: lines older than 15 days go, from a line's start
        head = data[:data.index(b"\n", 1200000) + 1]  # up to about 13 days ago
        with open(p, "wb") as fh:
            fh.write(head)
        assert _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
        rest = _bytes(p)
        assert head.endswith(rest) and head[-len(rest) - 1:-len(rest)] == b"\n"
        first = state_mod._parse_log_ts(rest[:40].decode("utf-8", "replace"))
        assert first is not None and first >= _cutoff() - 1, first
        print(f"over the cap: {size} B -> {len(kept)} B byte for byte, peak memory {peak} B  OK")
    finally:
        shutil.rmtree(d)


def test_line_feed_only_and_undated():
    """Only a line feed ends a line; a line without a time follows its record."""
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "z.log")
        recent = f"{ts(-3600)} NEW-A x\r\nrest of NEW-A\n{ts(-60)} NEW-B\r\n"
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"{ts(-20*86400)} OLD point 'a b' failed\r\n"
                     f"rest of an old message\n"
                     f"{ts(-16*86400)} OLD-B\rstill OLD-B\x85 and   too\r\n" + recent)
        assert _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
        assert _bytes(p).decode() == recent
        assert not _trim_log_file(p, _cutoff(), 100 * 1024 * 1024), "nothing more to trim"
        new = f"{ts(-60)} NEW\n"
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"x y orphan\n{ts(-17*86400)} OLD\n" + new)
        assert _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
        assert _bytes(p).decode() == new
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("no time here\nnor here\n")
        assert not _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"{ts(-20*86400)} OLD\n{ts(-19*86400)} OLD too\n")
        assert _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
        assert _bytes(p) == b"", "every line older: nothing left (without a floor)"
        print("only line feeds end a line; undated lines follow their record  OK")
    finally:
        shutil.rmtree(d)


def test_lines_logged_meanwhile_kept():
    """The handler stays attached: what is logged while the file is trimmed (another
    thread, as the event loop does) is all kept, in order."""
    d = tempfile.mkdtemp()
    lg = logging.getLogger("ser2net.test.trim")
    try:
        p = os.path.join(d, "m.log")
        with open(p, "w") as fh:
            for i in range(3000):
                fh.write(f"{ts(-20*86400)} old {i} " + "z" * 60 + "\n")
        fh = logging.FileHandler(p, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg.handlers[:] = [fh]
        lg.propagate, lg.level = False, logging.INFO
        go, done = threading.Event(), threading.Event()

        def writer():
            go.wait()
            for i in range(3000):
                lg.info(f"new {i}")
            done.set()
        real_copy = shutil.copyfileobj

        def slow_copy(src, dst, length=0):
            go.set()  # log while the kept part is copied ...
            time.sleep(0.05)
            return real_copy(src, dst, length)
        t = threading.Thread(target=writer)
        t.start()
        with mock.patch.object(state_mod.shutil, "copyfileobj", slow_copy):
            assert _trim_log_file(p, _cutoff(), 100 * 1024 * 1024, handlers=[fh])
        t.join()
        lg.info("after")  # ... and after the swap, into the new file
        fh.flush()
        lines = _bytes(p).decode().splitlines()
        assert not any(" old " in ln for ln in lines), lines[:3]
        assert [ln.split(" ", 2)[2] for ln in lines] == [f"new {i}" for i in range(3000)] + ["after"]
        assert os.stat(p).st_ino == os.fstat(fh.stream.fileno()).st_ino, "writes the new file"
        print("lines logged during the trim and after it: all kept, in order  OK")
    finally:
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)
        shutil.rmtree(d)


def test_swap_failures():
    """A swap that keeps failing leaves the file as it was and no .tmp; a file deleted
    meanwhile (its mapping was) is not made again."""
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "f.log")
        text = f"{ts(-20*86400)} OLD\n{ts(-60)} NEW\n"
        with open(p, "w") as fh:
            fh.write(text)
        with mock.patch.object(state_mod.os, "replace",
                               side_effect=PermissionError(13, "in use")) as rep, \
                mock.patch.object(state_mod.time, "sleep"):
            try:
                _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
                raise AssertionError("no error")
            except PermissionError:
                pass
        assert rep.call_count == state_mod._REPLACE_TRIES
        assert open(p).read() == text and os.listdir(d) == ["f.log"], os.listdir(d)
        real_copy = shutil.copyfileobj

        def delete_meanwhile(src, dst, length=0):
            real_copy(src, dst, length)
            os.remove(p)
        with mock.patch.object(state_mod.shutil, "copyfileobj", delete_meanwhile):
            assert not _trim_log_file(p, _cutoff(), 100 * 1024 * 1024)
        assert os.listdir(d) == [], os.listdir(d)
        print("failed swap: file unchanged, no .tmp; deleted meanwhile: not made again  OK")
    finally:
        shutil.rmtree(d)


def _lines_file(path, stamps, tag="line"):
    with open(path, "w", encoding="utf-8") as fh:
        for i, t in enumerate(stamps):
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f" {tag} {i}\n")


def test_clock_steps():
    """A clock one year ahead, or lines stamped 2009 by a clock that was behind and is
    then corrected: the last 1000 lines stay (they were all dropped before); the size cap
    still applies."""
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "c.log")
        now = time.time()
        stamps = [now - 6 * 86400 + i * 300 for i in range(1500)]  # 1-6 days old
        ahead = now + 365 * 86400 - LOG_MAX_AGE_DAYS * 86400       # cutoff a year ahead
        _lines_file(p, stamps)
        assert _trim_log_file(p, ahead, 100 * 1024 * 1024, keep_lines=0)
        assert _bytes(p) == b"", "without the floor a step ahead empties the log"
        _lines_file(p, stamps)
        assert _trim_log_file(p, ahead, 100 * 1024 * 1024, keep_lines=1000)
        kept = _bytes(p).decode().splitlines()
        assert len(kept) == 1000 and kept[0].endswith(" line 500") and \
            kept[-1].endswith(" line 1499"), (len(kept), kept[:1], kept[-1:])
        assert not _trim_log_file(p, ahead, 100 * 1024 * 1024, keep_lines=1000)
        # stamped 2009 (no RTC, before time sync), date corrected since
        old_clock = time.mktime((2009, 1, 1, 0, 0, 0, 0, 0, -1))
        _lines_file(p, [old_clock + i for i in range(1200)])
        assert _trim_log_file(p, _cutoff(), 100 * 1024 * 1024, keep_lines=1000)
        kept = _bytes(p).decode().splitlines()
        assert len(kept) == 1000 and kept[0].startswith("2009-") and kept[0].endswith(" line 200")
        # ... and a recent line before them protects them all, as before
        _lines_file(p, [time.time() - 3600] + [old_clock + i for i in range(1200)])
        assert not _trim_log_file(p, _cutoff(), 100 * 1024 * 1024, keep_lines=1000)
        # the size cap goes on cutting into those lines
        _lines_file(p, stamps[:1000])
        assert _trim_log_file(p, ahead, 4096, keep_lines=1000)
        assert os.path.getsize(p) <= 4096
        print("clock a year ahead / 2009 stamps corrected: last 1000 lines kept; size cap "
              "still cuts  OK")
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
        handler = state._mapping_loggers[m.id].handlers[0]
        await state.run_log_maintenance()    # the last 1000 lines stay
        assert open(path).read().startswith(f"{ts(-20*86400)} ANCIENT")
        with mock.patch.object(state_mod, "LOG_VIEW_LINES", 0):
            await state.run_log_maintenance()    # should drop ANCIENT, keep fresh
        assert state._mapping_loggers[m.id].handlers == [handler], "the handler stays"

        after = open(path).read()
        assert "ANCIENT" not in after, after
        assert "fresh event 1" in after, after

        # logging must still work after the rewrite (handler reattached)
        log("fresh event 2")
        after2 = open(path).read()
        assert "fresh event 2" in after2, after2
        print("integration (old dropped + handler reattached, logging continues) OK")

        # 1500 lines, all 20 days old: the Log panel's 1000 stay
        _lines_file(path, [time.time() - 20 * 86400 + i for i in range(1500)])
        await state.run_log_maintenance()
        shown = state.read_mapping_log(m.id)
        assert len(shown) == 1000 and shown[0].endswith(" line 1499"), (len(shown), shown[:1])
        print("integration: a mapping idle for over 15 days keeps its last 1000 lines  OK")
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
        assert s == 303 and [m.id for m in state.config.mappings] == [first], page[-300:]
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
    test_undated_lines()
    test_size_cap()
    test_within_limits_untouched()
    test_streaming_memory_and_bytes()
    test_line_feed_only_and_undated()
    test_lines_logged_meanwhile_kept()
    test_swap_failures()
    test_clock_steps()
    asyncio.run(_integration())
    asyncio.run(_replaced())
    print("\nPASS: per-mapping log retention (age + size)")
