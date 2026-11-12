"""What ser2netweb keeps on disk: the free space it needs, and the clock its retention goes by.

Traces can fill a disk (up to trace_max_mb x (1 + trace_keep) per mapping), and a full
disk keeps config.json, the logs and the audit log from being written. Below ``floor()``
free space on the data folder's disk (max(256 MiB, 5% of the disk), at most 1 GiB, and at
most a quarter of a small disk) a trace pauses (``check_space``, in its writer thread) and
goes on by itself once there is 10% more than that again; /metrics reports the free space
and what traces and logs take (``usage()``), and the optional disk_low notification says
when it gets low (notify.Notifier).

Retention deletes by age (the mapping logs' 15 days, Settings -> Retention). ``now()`` is
the time it compares with: the system clock, but never ahead of the system clock at
start-up plus the time this process has run (measured with a clock no step changes). A
clock stepped ahead while ser2netweb runs (a typo, a bad time source) so deletes nothing
early; one stepped back is followed at once (that only keeps more). After a restart the
system clock counts again: by then the step is either corrected or a real one.
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import time

FREE_FLOOR_MIN = 256 * 1024 * 1024
FREE_FLOOR_SHARE = 0.05
FREE_FLOOR_MAX = 1024 * 1024 * 1024
FREE_RESUME = 1.1  # a trace paused for space goes on above floor() x this


class LowSpace(OSError):
    """Too little free space on the disk to write a trace (check_space)."""


def space(path: str) -> tuple[int, int] | None:
    """(free, total) bytes of the file system `path` is on (free: what this process may
    use, without the part reserved for root), or None when that can't be read."""
    try:
        usage = shutil.disk_usage(path)
    except (OSError, ValueError):
        return None
    return usage.free, usage.total


def floor(total: int) -> int:
    """The free space below which traces pause on a disk of `total` bytes."""
    return min(max(FREE_FLOOR_MIN, min(int(total * FREE_FLOOR_SHARE), FREE_FLOOR_MAX)),
               total // 4)


def mb(n: float) -> str:
    return f"{n / 1048576:.0f} MB"


def size_text(n: float) -> str:
    """A size as the UI shows it: '1.2 GB', '340 MB', '12 KB', '0 KB'."""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return mb(n) if n >= 1048576 else f"{n / 1024:.0f} KB"


def check_space(path: str, resuming: bool = False) -> None:
    """Raise LowSpace when the disk `path` is on has less free space than floor() (with
    `resuming`: than FREE_RESUME times that, so that a trace paused for it doesn't flap);
    nothing when there is enough, or when that can't be told."""
    found = space(path)
    if found is None:
        return
    free, total = found
    need = floor(total)
    if free < (need * FREE_RESUME if resuming else need):
        raise LowSpace(errno.ENOSPC, f"the disk is almost full ({mb(free)} free; traces "
                                     f"pause below {mb(need)})")


def folder_bytes(folder: str, prefixes=None) -> int:
    """The size of the regular files in `folder` (not in its subfolders; with `prefixes`,
    only those whose name begins with one of them). Blocking."""
    total = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if prefixes is not None and not entry.name.startswith(tuple(prefixes)):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
    except OSError:
        pass
    return total


def usage(data_dir: str) -> dict:
    """The data folder's disk and what ser2netweb keeps there: free and total bytes of the
    disk (None when they can't be read), the floor traces pause below, and the bytes of the
    traces (traces/) and of the logs (logs/, all.log*, audit.log*). Blocking: a few
    directory listings."""
    found = space(data_dir)
    free, total = found if found is not None else (None, None)
    return {"free": free, "total": total, "floor": floor(total) if total else None,
            "traces": folder_bytes(os.path.join(data_dir, "traces")),
            "logs": folder_bytes(os.path.join(data_dir, "logs"))
            + folder_bytes(data_dir, ("all.log", "audit.log"))}


def _elapsed() -> float:
    """Seconds since an arbitrary start that no clock step changes: CLOCK_BOOTTIME on
    Linux (it also counts a suspend), else time.monotonic()."""
    if sys.platform != "win32":  # (no clock_gettime there)
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
