"""Notice when the system clock is stepped while ser2netweb runs.

A clock that is set (by hand, by time sync catching up after a boot without an RTC, by a
virtual machine host) puts a jump into the times of all.log, audit.log, the mapping logs
and the traces: lines out of order, an hour that seems to be missing. ser2netweb never
sets the clock (see clock.py) and measures durations on the monotonic clock, so nothing
breaks - but whoever reads those files later should know where the jump is.

Every CHECK_S seconds the wall clock is compared with a clock no step changes: how much
further (or less far) the wall clock went than that one since the last look is the step.
On Linux that clock is CLOCK_BOOTTIME, which counts the time a suspended machine sleeps,
so a resume is not a step; macOS and the BSDs have a CLOCK_MONOTONIC that does the same;
on Windows it is Python's monotonic clock. NTP's slewing changes both clocks alike (or by
far less than STEP_MIN_S) and is never reported.

A step of STEP_MIN_S or more
  - is counted (ser2net_clock_steps_total in /metrics, clock_steps in /api/v1/status);
  - is written to all.log and recorded in the audit log (clock_step), at most once per
    REPORT_EVERY_S (the next line says how many were held back);
  - is noted in every running trace with a "!!" line, where the times jump;
  - sends the optional clock_step notification.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Callable, Optional

from . import clock

CHECK_S = 5.0          # how often the clocks are compared
STEP_MIN_S = 1.0       # a smaller difference is not reported (the files' times are seconds)
REPORT_EVERY_S = 60.0  # the log line and the audit record: at most this often


def steady() -> float:
    """A clock that no step of the system clock changes and that, where the system has
    one, goes on while the machine is suspended (see the module docstring)."""
    get = getattr(time, "clock_gettime", None)
    for name in ("CLOCK_BOOTTIME", "CLOCK_MONOTONIC"):
        clock_id = getattr(time, name, None)
        if get is not None and clock_id is not None:
            try:
                return float(get(clock_id))
            except OSError:  # a kernel without it
                continue
    return time.monotonic()


def describe(step: float, now: float) -> str:
    """'set forward by 3600.0 s (it now reads 2026-11-13 13:00:00+03:00)' (English: the
    log, the audit log, the trace note and the notification)."""
    way = "forward" if step > 0 else "back"
    return f"set {way} by {abs(step):.1f} s (it now reads {clock.stamp(now)})"


class ClockWatch:
    """Compares the wall clock with steady() (both can be given, for tests) and reports
    its steps to `on_step(step seconds, wall time now)`."""

    def __init__(self, on_step: Callable[[float, float], None], *,
                 wall: Callable[[], float] = time.time,
                 reference: Callable[[], float] = steady) -> None:
        self._on_step = on_step
        self._wall, self._reference = wall, reference
        self._last: Optional[tuple[float, float]] = None
        self._task: Optional[asyncio.Task] = None
        self.steps = 0  # steps seen since the start

    def look(self) -> Optional[float]:
        """Compare the clocks now: the step since the last look (seconds; positive: set
        forward) when it is STEP_MIN_S or more - counted and reported - else None."""
        now = (self._wall(), self._reference())
        last, self._last = self._last, now
        if last is None:
            return None
        step = (now[0] - last[0]) - (now[1] - last[1])
        if abs(step) < STEP_MIN_S:
            return None
        self.steps += 1
        self._on_step(step, now[0])
        return step

    async def start(self) -> None:
        self.look()
        self._task = asyncio.create_task(self._run(), name="clock-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_S)
            with contextlib.suppress(Exception):  # (on_step: never let the watcher die)
                self.look()


class Reporter:
    """What an AppState does about a step (ClockWatch's on_step): the log line and the
    audit record, at most once per REPORT_EVERY_S; the "!!" note in the running traces and
    the clock_step notification for every one (the notifier has its own repeat limit)."""

    def __init__(self, state) -> None:
        self.state = state
        self._next = 0.0   # time.monotonic() from which the next line is written
        self._held = 0     # steps not written since the last line

    def __call__(self, step: float, now: float) -> None:
        text = describe(step, now)
        self.state.supervisor.annotate_traces(f"the system clock was {text}")
        notifier = getattr(self.state, "notifier", None)
        if notifier is not None:
            notifier.emit("clock_step", "clock", "", f"The system clock was {text}.")
        mono = time.monotonic()
        if mono < self._next:
            self._held += 1
            return
        more = f" ({self._held} more since the last such line)" if self._held else ""
        self._held, self._next = 0, mono + REPORT_EVERY_S
        self.state.log(f"WARNING: the system clock was {text}{more}: the times in the logs, "
                       "the audit log and the traces jump here (ser2netweb never sets the "
                       "clock; durations are not affected)")
        self.state.audit("local", "clock_step", text + more)
