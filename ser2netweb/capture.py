"""Timed captures: trace a mapping's traffic for a while, then stop by itself.

An operator starts one in the mapping's Trace panel or with
POST /api/v1/mappings/{id}/capture (15 minutes, 1 hour, 24 hours; the REST API takes any
number of minutes up to CAPTURE_MAX_S). The mapping keeps its clients and its serial port:
only its trace writer is opened, and closed again at the end (Supervisor.retrace).

The end time is stored in the mapping's options (trace_until, Unix seconds), so a capture
goes on after a restart and ends on time; one whose time passed while ser2netweb was not
running ends before the mappings start. While this process runs the time left is counted
on the monotonic clock: a step of the system clock neither cuts a capture short nor keeps
it going. trace_until is no setting: the edits (the form, a REST POST / PUT / PATCH, an
import, a Copy) keep a mapping's own and give a new one none. However it was set anyway (by
hand in config.json, a restored backup), a capture never lasts longer than CAPTURE_MAX_S
from when this process first saw it.

Only a mapping whose trace is off can be captured: its traffic goes to
traces/<id>-capture.log. A mapping that traces already (a trace file name in its
options) is refused, and the end of a capture only clears trace_until - a trace that is
on for good is never turned off here.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Optional

from . import clock
from .config import CAPTURE_MAX_S, ConfigError

if TYPE_CHECKING:
    from .state import AppState

CHECK_S = 1.0  # how often the ends are looked at
# what the Trace panel offers, in minutes
CHOICES_MIN = (15, 60, 24 * 60)


class CaptureRefused(Exception):
    """A capture that can't begin. `code`: "not_found" (no such mapping), "tracing" (its
    trace is on already, by its trace file name) or "duration" (not 1 minute to
    CAPTURE_MAX_S)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def duration_text(seconds: float) -> str:
    """'15 min', '1 h', '24 h', '1 h 30 min' (English: the log and the audit log)."""
    minutes = max(1, round(seconds / 60))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    return f"{hours} h" if hours else f"{rest} min"


class Captures:
    """The timed captures of an AppState's mappings: begin(), end(), and the watcher that
    ends each one at its time."""

    def __init__(self, state: "AppState") -> None:
        self.state = state
        # mapping id -> (the trace_until it was armed for, its end on the monotonic clock)
        self._ends: dict[str, tuple[float, float]] = {}
        self._task: Optional[asyncio.Task] = None

    # ---------------- what the pages and the REST API ask ----------------
    def left(self, mapping) -> Optional[float]:
        """The seconds a mapping's capture still runs, None when it has none."""
        until = mapping.options.trace_until
        if not until:
            return None
        return max(0.0, self._arm(mapping.id, until) - time.monotonic())

    def until(self, mapping) -> Optional[float]:
        """When a mapping's capture ends as the system clock reads now (Unix seconds),
        None when it has none."""
        left = self.left(mapping)
        return None if left is None else time.time() + left

    async def begin(self, mapping_id: str, seconds: float) -> float:
        """Capture the mapping's traffic for `seconds` from now (a capture under way gets
        this as its new end). Returns the end (Unix seconds). Raises CaptureRefused, or
        ConfigError when config.json can't be written (nothing changed then)."""
        if not 60 <= seconds <= CAPTURE_MAX_S:
            raise CaptureRefused("duration")
        async with self.state.edit_config(backup=False):
            m = self.state.config.get_mapping(mapping_id)
            if m is None:
                raise CaptureRefused("not_found")
            if m.options.trace_both:
                raise CaptureRefused("tracing")
            until = m.options.trace_until = time.time() + seconds
            self._ends[m.id] = (until, time.monotonic() + seconds)
        await self.state.supervisor.retrace(mapping_id)
        return until

    async def end(self, mapping_id: str, until: Optional[float] = None) -> bool:
        """End the mapping's capture now. Returns whether it had one. Its trace is closed
        (not one that is on by its trace file name) even when config.json can't be
        written: ConfigError is raised then, and the next start ends it for good.
        `until`: the end the watcher found due; a capture begun again meanwhile (another
        end) is left alone, and False returned."""
        try:
            async with self.state.edit_config(backup=False, keep_on_failure=True):
                m = self.state.config.get_mapping(mapping_id)
                if m is None or not m.options.trace_until:
                    self._ends.pop(mapping_id, None)
                    return False
                if until is not None and m.options.trace_until != until:
                    return False  # (extended while this waited for the configuration)
                self._ends.pop(mapping_id, None)
                m.options.trace_until = 0.0
        finally:
            await self.state.supervisor.retrace(mapping_id)
        return True

    # ---------------- the watcher ----------------
    async def end_overdue(self) -> None:
        """Before the mappings start: end the captures whose time passed while ser2netweb
        was not running, so that none of them is traced again."""
        now = time.time()
        for m in list(self.state.config.mappings):
            until = m.options.trace_until
            if until and until <= now:
                await self._expire(m, "its time passed while ser2netweb was not running")

    async def start(self) -> None:
        self._task = asyncio.create_task(self._watch(), name="capture-watch")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _arm(self, mapping_id: str, until: float) -> float:
        """The end of a capture on the monotonic clock: counted from the first time this
        process sees its trace_until, at most CAPTURE_MAX_S."""
        armed = self._ends.get(mapping_id)
        if armed is None or armed[0] != until:
            left = min(max(until - time.time(), 0.0), float(CAPTURE_MAX_S))
            armed = self._ends[mapping_id] = (until, time.monotonic() + left)
        return armed[1]

    async def check(self) -> None:
        """End the captures whose time has come (the watcher's step)."""
        seen = set()
        for m in list(self.state.config.mappings):
            until = m.options.trace_until
            if not until:
                continue
            seen.add(m.id)
            if time.monotonic() >= self._arm(m.id, until):
                await self._expire(m, "")
        for gone in [mid for mid in self._ends if mid not in seen]:
            del self._ends[gone]

    async def _expire(self, mapping, why: str) -> None:
        name = mapping.name
        try:
            ended = await self.end(mapping.id, until=mapping.options.trace_until)
        except ConfigError as e:
            self.state.log(f"[{name}] timed capture ended, but config.json could not keep "
                           f"that ({e}): it ends again at the next start")
            ended = True
        if ended:
            self.state.log(f"[{name}] timed capture ended" + (f": {why}" if why else ""))
            self.state.audit("local", "trace_capture_end", name)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                await self.check()
            except Exception as e:  # never let the watcher die
                self.state.log(f"timed captures: watcher error: {e}")


def describe(until: float, seconds: float) -> str:
    """'15 min, until 2026-11-13 12:15:00+03:00' for the log and the audit log."""
    return f"{duration_text(seconds)}, until {clock.stamp(until)}"
