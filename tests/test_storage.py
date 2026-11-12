"""What ser2net keeps on disk (app/storage.py): the clock retention goes by never runs
ahead of the time the process has run, so a clock stepped ahead deletes nothing early.

Run: python3 tests/test_storage.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.state as state_mod
from app import storage
from app.config import AppConfig, ConfigStore, MappingConfig
from app.state import AppState

YEAR = 365 * 86400


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def test_clock():
    real = time.time
    assert abs(storage.now() - real()) < 5
    with mock.patch("time.time", lambda: real() + YEAR):  # stepped ahead
        assert abs(storage.now() - real()) < 5, "followed a step ahead"
    with mock.patch("time.time", lambda: real() - 86400):  # stepped back
        assert abs(storage.now() - (real() - 86400)) < 5, "didn't follow a step back"
    print("retention clock: a step ahead is not followed, a step back is  OK")


async def _jumps():
    d = tempfile.mkdtemp()
    try:
        cfg = AppConfig()
        m = MappingConfig(name="M1")
        cfg.mappings.append(m)
        st = AppState(ConfigStore(os.path.join(d, "config.json")), cfg)
        logs = []
        st.log = logs.append
        trace = os.path.join(st.traces_dir, f"{m.id}-t.log")
        with open(trace, "wb") as fh:
            fh.write(b"x")
        three_days = time.time() - 3 * 86400
        os.utime(trace, (three_days, three_days))
        log_path = st.mapping_log_path(m.id)
        with open(log_path, "w") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(three_days)) + " x\n")
        st.config.trace_max_age_days = 7
        real = time.time
        with mock.patch("time.time", lambda: real() + 30 * 86400), \
                mock.patch.object(state_mod, "LOG_VIEW_LINES", 0):
            assert await st.run_trace_maintenance() == 0
            await st.run_log_maintenance()
        assert os.path.exists(trace) and os.path.getsize(log_path) > 0, logs
        # what the same clock would have deleted had it been followed
        with mock.patch("time.time", lambda: real() + 30 * 86400), \
                mock.patch.object(storage, "now", lambda: real() + 30 * 86400), \
                mock.patch.object(state_mod, "LOG_VIEW_LINES", 0):
            assert await st.run_trace_maintenance() == 1
            await st.run_log_maintenance()
        assert not os.path.exists(trace) and os.path.getsize(log_path) == 0, logs
        print("a clock 30 days ahead: no trace file or mapping log line deleted early  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_clock()
    asyncio.run(_jumps())
    print("\nPASS: retention clock")


if __name__ == "__main__":
    main()
