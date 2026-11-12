"""A stepped wall clock for the tests of what a system clock step must not change (NTP
correcting the clock of a board without an RTC after its boot, a clock set back by hand).

In place of a module's ``time``, ``WallClock`` answers time.time() moved by ``shift``
seconds; everything else (time.monotonic(), time.sleep(), ...) is the real time module.
Only that module's view changes, in this process: the system clock is never touched.

    with WallClock.patch(cluster) as clock:   # app.engine.cluster sees the stepped clock
        clock.shift = 2 * 86400               # two days forward
"""
from __future__ import annotations

import contextlib
import time as _time


class WallClock:
    def __init__(self, shift: float = 0.0) -> None:
        self.shift = shift

    def time(self) -> float:
        return _time.time() + self.shift

    def __getattr__(self, name):
        return getattr(_time, name)

    @classmethod
    @contextlib.contextmanager
    def patch(cls, *modules, shift: float = 0.0):
        """Give each module (its global ``time``) the same stepped clock while it runs."""
        clock, saved = cls(shift), [(m, m.time) for m in modules]
        for m in modules:
            m.time = clock
        try:
            yield clock
        finally:
            for m, t in saved:
                m.time = t
