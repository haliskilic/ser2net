"""The clock-step watch (ser2netweb/clockwatch.py): a system clock set while ser2netweb runs.

Checks:
  * a step is how much further the wall clock went than the steady clock between two
    looks: forward and back are found with their size, one under STEP_MIN_S (NTP's
    slewing) is not, and a resume from suspend - both clocks went on - is not a step; the
    steady clock is one that counts the time suspended: CLOCK_BOOTTIME on Linux,
    CLOCK_MONOTONIC on macOS, GetTickCount64 on Windows (checked on the Windows CI legs;
    time.monotonic stops while suspended there since Python 3.13), never time.monotonic
    where GetTickCount64 can be had;
  * every step is counted, noted with a "!!" line in each running trace and offered to
    the notifier as clock_step; the log line and the audit record are written at most
    once a minute, the next one saying how many steps were held back;
  * the "!!" note is written into the trace file in turn with the traffic, not into a
    trace that is paused or closing;
  * clock_step is an opt-in notification event with a title, a Settings checkbox and a
    Turkish label; a running server answers ser2net_clock_steps_total in /metrics and
    clock_steps in /api/v1/status.

The system clock is never touched: the watch is given the clocks to read.

Run: python3 tests/test_clock_step.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

from _server import Browser, Server

from ser2netweb import clockwatch, notify
from ser2netweb.config import (NOTIFY_DEFAULT_EVENTS, NOTIFY_EVENTS, MappingOptions,
                               NotificationSettings)
from ser2netweb.engine import bridge
from ser2netweb.engine.supervisor import Supervisor
from ser2netweb.web.auth import hash_password, hash_token

TOKEN = "s2n_clock-step-token"


class Clocks:
    """A wall clock and a steady clock the test moves."""

    def __init__(self):
        self.wall, self.ref = 1_800_000_000.0, 5000.0

    def run(self, seconds, step=0.0, asleep=0.0):
        """`seconds` pass; the wall clock is also set by `step`; the machine was suspended
        for `asleep` seconds (both clocks count them, as CLOCK_BOOTTIME does)."""
        self.wall += seconds + asleep + step
        self.ref += seconds + asleep


def test_look():
    clocks, seen = Clocks(), []
    watch = clockwatch.ClockWatch(lambda step, now: seen.append((round(step, 3), now)),
                                  wall=lambda: clocks.wall, reference=lambda: clocks.ref)
    assert watch.look() is None, "the first look only takes the clocks"
    clocks.run(5)
    assert watch.look() is None and not seen
    clocks.run(5, step=3600)
    assert round(watch.look(), 3) == 3600 and seen == [(3600.0, clocks.wall)], seen
    clocks.run(5)
    assert watch.look() is None, "a step is reported once"
    clocks.run(5, step=-12.5)
    assert round(watch.look(), 3) == -12.5 and seen[-1][0] == -12.5
    for small in (0.4, -0.9, 0.002):  # slewing, a small correction
        clocks.run(5, step=small)
        assert watch.look() is None, small
    clocks.run(5, asleep=8 * 3600)
    assert watch.look() is None, "a resume from suspend is not a step"
    clocks.run(600)  # the event loop was held up: both clocks went on
    assert watch.look() is None
    clocks.run(5, step=1.0)
    assert watch.look() is not None and watch.steps == 3, watch.steps
    assert clockwatch.describe(3600, 0).startswith("set forward by 3600.0 s (it now reads ")
    assert clockwatch.describe(-12.5, 0).startswith("set back by 12.5 s (it now reads ")
    a, b = clockwatch.steady(), clockwatch.steady()
    assert b >= a
    if sys.platform.startswith("linux"):
        assert abs(clockwatch.steady() - time.clock_gettime(time.CLOCK_BOOTTIME)) < 1.0, \
            "Linux: CLOCK_BOOTTIME, which counts the time suspended"
    # the clock each system compares with: one that counts the time suspended
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    want = {"linux": "CLOCK_BOOTTIME", "darwin": "CLOCK_MONOTONIC", "win32": "GetTickCount64"}
    if platform in want:
        assert clockwatch.REFERENCE == want[platform], (sys.platform, clockwatch.REFERENCE)
    if sys.platform == "win32":  # (the Windows CI legs)
        import ctypes
        ticks = ctypes.windll.kernel32.GetTickCount64
        ticks.restype = ctypes.c_uint64
        assert abs(clockwatch.steady() - ticks() / 1000) < 1.0, \
            "Windows: GetTickCount64 (time.monotonic stops while suspended since Python 3.13)"
    # without clock_gettime (Windows) GetTickCount64 comes before time.monotonic
    saved = clockwatch._tick_count, getattr(time, "clock_gettime", None)
    try:
        if saved[1] is not None:
            del time.clock_gettime
        clockwatch._tick_count = lambda: (lambda: 42.0)
        name, read = clockwatch._reference()
        assert (name, read()) == ("GetTickCount64", 42.0), name
        clockwatch._tick_count = lambda: None
        assert clockwatch._reference()[0] == "monotonic"
    finally:
        clockwatch._tick_count = saved[0]
        if saved[1] is not None:
            time.clock_gettime = saved[1]
    assert clockwatch._tick_count() is None or sys.platform == "win32"
    print("look: forward and back steps with their size; slewing, a resume and a held-up "
          "event loop are no steps  OK")


class State:
    def __init__(self, events=("clock_step",)):
        self.logs, self.audits, self.notes = [], [], []
        outer = self
        self.supervisor = type("S", (), {
            "annotate_traces": lambda s, text: outer.notes.append(text) or 1})()
        self.config = type("C", (), {})()
        self.config.notifications = NotificationSettings.from_dict({
            "enabled": True, "events": list(events), "repeat_s": 300,
            "webhook_url": "https://x/y"})

    def log(self, msg):
        self.logs.append(msg)

    def audit(self, ip, action, detail="", user=""):
        self.audits.append((ip, action, detail))


def test_report():
    async def run():
        st = State()
        st.notifier = notify.Notifier(st)
        report = clockwatch.Reporter(st)
        now = time.time()
        report(3600.0, now)
        assert len(st.logs) == 1 and st.logs[0].startswith(
            "WARNING: the system clock was set forward by 3600.0 s (it now reads "), st.logs
        assert "jump here" in st.logs[0]
        assert st.audits == [("local", "clock_step", clockwatch.describe(3600.0, now))], st.audits
        assert st.notes == ["the system clock was " + clockwatch.describe(3600.0, now)]
        event, mapping, detail = st.notifier._queue.get_nowait()
        assert (event, mapping) == ("clock_step", "") and "set forward by 3600.0 s" in detail
        for _ in range(3):  # a clock that keeps being set: one line a minute
            report(-2.0, now)
        assert len(st.logs) == 1 and len(st.audits) == 1, "rate-limited"
        assert len(st.notes) == 4, "every step is noted in the traces"
        assert st.notifier._queue.empty(), "the notifier's repeat limit holds the rest back"
        report._next = 0.0  # a minute later
        report(5.0, now)
        assert len(st.logs) == 2 and "(3 more since the last such line)" in st.logs[1], st.logs
        assert st.audits[1][2].endswith("(3 more since the last such line)"), st.audits
        quiet = State(events=())
        quiet.notifier = notify.Notifier(quiet)
        clockwatch.Reporter(quiet)(10.0, now)
        assert quiet.notifier._queue.empty() and len(quiet.logs) == 1 and len(quiet.audits) == 1, \
            "the notification is opt-in; the log and the audit record are not"

    asyncio.run(run())
    assert "clock_step" in NOTIFY_EVENTS and "clock_step" not in NOTIFY_DEFAULT_EVENTS
    assert notify.title("clock_step") != "clock_step"
    print("report: counted, logged and audited at most once a minute, noted in the traces, "
          "offered as the opt-in clock_step notification  OK")


def test_trace_note():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m1-t.log")
            tracer = bridge.Tracer(path, MappingOptions(trace_both="t.log"))
            runner = type("R", (), {"tracer": tracer})()
            idle = type("R", (), {"tracer": None})()
            sup = Supervisor.__new__(Supervisor)
            sup._runners = {"m1": runner, "m2": idle}
            tracer.write("ser>net", b"before")
            assert sup.annotate_traces("the system clock was set forward by 3600.0 s") == 1
            tracer.write("net>ser", b"after")
            await tracer.aclose()
            assert sup.annotate_traces("late") == 0, "not into a trace that is closing"
            with open(path, "rb") as fh:
                lines = fh.read().decode().splitlines()
            assert [ln[20:] for ln in lines] == [
                "ser>net before", "!! the system clock was set forward by 3600.0 s",
                "net>ser after"], lines
            # in a hex trace the note is text too, and it is no traffic: one that can't be
            # written is not counted as bytes not traced
            hexed = bridge.Tracer(os.path.join(tmp, "m4-t.log"),
                                  MappingOptions(trace_both="t.log", trace_hexdump=True))
            hexed.write("ser>net", b"AB")
            hexed.annotate("the system clock was set back by 5.0 s")
            await hexed.aclose()
            with open(os.path.join(tmp, "m4-t.log"), "rb") as fh:
                lines = fh.read().decode().splitlines()
            assert [ln[20:] for ln in lines] == [
                "ser>net [2] 41 42", "!! the system clock was set back by 5.0 s"], lines
            assert [n for _line, n in hexed._lines(0.0, "!!", b"note")] == [0]
            # a paused trace takes no note (it says how many bytes are missing instead)
            paused = bridge.Tracer(os.path.join(tmp, "m3-t.log"), MappingOptions())
            paused._set_error("disk full")
            paused.annotate("x")
            assert paused._queued == 0
            paused._set_error("")
            await paused.aclose()

    asyncio.run(run())
    print('trace note: a "!!" line in turn with the traffic, in every running trace  OK')


def test_live():
    users = [{"username": "admin", "password_hash": hash_password("clock-pass-1"),
              "role": "admin", "pwd_version": 1}]
    with Server({"users": users, "api_token_hash": hash_token(TOKEN),
                 "api_token_role": "viewer", "admin_ui": {"language": "en"}}) as srv:
        b = Browser(srv.base)
        b.login("admin", "clock-pass-1")
        _st, _h, body = b.get("/metrics")
        assert re.search(r"^ser2net_clock_steps_total 0$", body, re.M), body[:800]
        assert "# TYPE ser2net_clock_steps_total counter" in body
        req = urllib.request.Request(srv.base + "/api/v1/status",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.load(r)["clock_steps"] == 0
        _st, _h, page = b.get("/settings")
        assert re.search(r'name="notify_event_clock_step" >\s*System clock stepped', page), \
            "its checkbox, not ticked"
        b.get("/lang/tr")
        _st, _h, page = b.get("/settings")
        assert "sistem saati değiştirildi" in page
        assert "clock_step" not in srv.output(), "a start is not a step"
    print("live: /metrics and /api/v1/status count the steps (none at a start); the event "
          "has its Settings checkbox, off by default  OK")


def main():
    test_look()
    test_report()
    test_trace_note()
    test_live()
    print("\nPASS: clock-step watch")


if __name__ == "__main__":
    main()
