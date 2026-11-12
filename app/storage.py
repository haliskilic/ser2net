"""What ser2net keeps on disk: the clock its retention goes by.

Retention deletes by age (the mapping logs' 15 days, Settings -> Retention). ``now()`` is
the time it compares with: the system clock, but never ahead of the system clock at
start-up plus the time this process has run (measured with a clock no step changes). A
clock stepped ahead while ser2net runs (a typo, a bad time source) so deletes nothing
early; one stepped back is followed at once (that only keeps more). After a restart the
system clock counts again: by then the step is either corrected or a real one.
"""
from __future__ import annotations

import time


def _elapsed() -> float:
    """Seconds since an arbitrary start that no clock step changes: CLOCK_BOOTTIME on
    Linux (it also counts a suspend), else time.monotonic()."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if boottime is not None:
        try:
            return time.clock_gettime(boottime)
        except OSError:
            pass
    return time.monotonic()


_STARTED = (time.time(), _elapsed())


def now() -> float:
    """The time retention compares ages with (see the module docstring)."""
    wall, elapsed = _STARTED
    return min(time.time(), wall + (_elapsed() - elapsed))
