"""What ser2net keeps on disk (ser2netweb/storage.py):
  * the clock retention goes by never runs ahead of the time the process has run, so a
    clock stepped ahead deletes nothing early;
  * the data disk's free space (shutil.disk_usage faked: no disk is filled): going below
    the floor is logged and sends the opt-in disk_low notification once, room again is
    logged; /metrics reports free / total bytes and what traces and logs take, and
    Settings -> Retention shows them (a real server, on loopback).

Run: python3 tests/test_storage.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from collections import namedtuple
from unittest import mock

from _server import Browser, Server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ser2netweb.state as state_mod
from ser2netweb import notify, storage
from ser2netweb.config import AppConfig, ConfigStore, MappingConfig, NotificationSettings
from ser2netweb.web.auth import hash_password
from ser2netweb.state import AppState

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


GiB = 1024 ** 3
Usage = namedtuple("Usage", "total used free")


async def _disk_watch():
    """Below the floor: one warning and one disk_low; just above it: still low; with 10%
    to spare: room again; low again: another one."""
    free = [10 * GiB]
    fake = lambda _p: Usage(32 * GiB, 32 * GiB - free[0], free[0])

    class State:
        data_dir = tempfile.gettempdir()

        def __init__(self):
            self.logs = []
            self.config = AppConfig()
            self.config.notifications = NotificationSettings.from_dict(
                {"enabled": True, "events": ["disk_low"], "repeat_s": 0,
                 "webhook_url": "https://example.invalid/hook"})

        def log(self, m):
            self.logs.append(m)
    st = State()
    n = notify.Notifier(st)
    assert storage.floor(32 * GiB) == GiB
    with mock.patch.object(storage.shutil, "disk_usage", fake):
        await n.check_disk()
        assert not n.disk_low and st.logs == [] and n._queue.empty()
        free[0] = GiB - 1
        await n.check_disk()
        await n.check_disk()
        assert n.disk_low and n._queue.qsize() == 1, n._queue.qsize()
        event, mapping, detail = n._queue.get_nowait()
        assert (event, mapping) == ("disk_low", "") and detail.startswith(
            "1024 MB free of 32768 MB on the disk of "), detail
        assert st.logs == [f"WARNING: the data disk is almost full: {detail}"], st.logs
        free[0] = int(GiB * 1.05)
        await n.check_disk()
        assert n.disk_low and len(st.logs) == 1
        free[0] = int(GiB * 1.1) + 1
        await n.check_disk()
        assert not n.disk_low and st.logs[-1].startswith("the data disk has room again: "), st.logs
        free[0] = 100
        await n.check_disk()
        assert n._queue.qsize() == 1
        st.config.notifications.events = list(notify_default())
        n.disk_low = False
        await n.check_disk()
        assert n._queue.qsize() == 1, "disk_low is sent only when chosen"
        assert st.logs[-1].startswith("WARNING: the data disk is almost full"), st.logs
    assert notify.title("disk_low") == "data disk almost full"
    print("disk watch: below the floor one warning and one disk_low (opt-in), room again "
          "with 10% to spare  OK")


def notify_default():
    from ser2netweb.config import NOTIFY_DEFAULT_EVENTS
    assert "disk_low" not in NOTIFY_DEFAULT_EVENTS
    return NOTIFY_DEFAULT_EVENTS


def test_usage():
    d = tempfile.mkdtemp()
    try:
        for rel, n in (("traces/a-t.log", 3000), ("traces/a-t.log.1", 2000), ("logs/a.log", 700),
                       ("all.log", 100), ("all.log.1", 50), ("audit.log", 20),
                       ("config.json", 999)):
            os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
            with open(os.path.join(d, rel), "wb") as fh:
                fh.write(b"x" * n)
        os.makedirs(os.path.join(d, "traces", "sub"))
        with mock.patch.object(storage.shutil, "disk_usage",
                               lambda _p: Usage(8 * GiB, 7 * GiB, GiB)):
            u = storage.usage(d)
        assert u == {"free": GiB, "total": 8 * GiB, "floor": int(8 * GiB * 0.05),
                     "traces": 5000, "logs": 870}, u
        with mock.patch.object(storage.shutil, "disk_usage", side_effect=OSError(5, "EIO")):
            assert storage.usage(d)["free"] is None
        assert storage.floor(400 * 1024 ** 2) == 100 * 1024 ** 2, "a quarter of a small disk"
        assert storage.floor(2 * GiB) == 256 * 1024 ** 2 and storage.floor(100 * GiB) == GiB
        assert [storage.size_text(n) for n in (0, 5000, 3 * 1048576, int(1.25 * GiB))] == \
            ["0 KB", "5 KB", "3 MB", "1.2 GB"]
        print("usage: free / total, floor, traces and logs counted  OK")
    finally:
        shutil.rmtree(d)


def test_metrics_and_panel():
    users = [{"username": "admin", "password_hash": hash_password("storage-pass-1"),
              "role": "admin", "pwd_version": 1}]
    with Server({"users": users}) as srv:
        os.makedirs(os.path.join(srv.data, "traces"), exist_ok=True)
        with open(os.path.join(srv.data, "traces", "abc-t.log"), "wb") as fh:
            fh.write(b"t" * 4321)
        b = Browser(srv.base)
        b.login("admin", "storage-pass-1")
        st, _h, body = b.get("/metrics")
        gauges = dict(ln.split(" ", 1) for ln in body.splitlines()
                      if ln.startswith(("ser2net_data_", "ser2net_traces_", "ser2net_logs_")))
        assert set(gauges) == {"ser2net_data_free_bytes", "ser2net_data_size_bytes",
                               "ser2net_traces_bytes", "ser2net_logs_bytes"}, body
        assert 0 < int(gauges["ser2net_data_free_bytes"]) <= int(gauges["ser2net_data_size_bytes"])
        assert gauges["ser2net_traces_bytes"] == "4321" and int(gauges["ser2net_logs_bytes"]) > 0
        st, _h, page = b.get("/settings")
        assert st == 200 and "The data folder’s disk: " in page and "Traces take 4 KB" in page
        print("/metrics: data disk free / size, traces and logs bytes; Settings -> Retention "
              "shows them  OK")


def main():
    test_clock()
    asyncio.run(_jumps())
    asyncio.run(_disk_watch())
    test_usage()
    test_metrics_and_panel()
    print("\nPASS: retention clock, disk space")


if __name__ == "__main__":
    main()
