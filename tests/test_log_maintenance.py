"""Per-mapping log retention: age (>15d) + size (>100MB) auto-trim.

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


if __name__ == "__main__":
    test_age_trim()
    test_undated_lines()
    test_size_cap()
    asyncio.run(_integration())
    print("\nPASS: per-mapping log retention (age + size)")
