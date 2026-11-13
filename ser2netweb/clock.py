"""Times as ser2netweb writes them into its files, and what it can tell about the system
clock (it never sets the clock: that is the operating system's time sync, see the README).

``stamp()`` is the server's local time with its offset from UTC, as all.log, stdout, the
mapping logs (logs/<id>.log) and audit.log write it since 2.9:
``2026-11-06 12:00:00+03:00``. Without the offset, the same text could stand for two
instants (the hour repeated when daylight saving time ends) and a container's UTC lines
could not be told from the operator's local time. The first 19 characters are the
``YYYY-MM-DD HH:MM:SS`` that older versions wrote, and that is all they read of it (the
log trim, the sign-ins in the audit log): a file with old and new lines reads as one, in
this version and in an older one. ``parse_stamp()`` reads both kinds. The offset comes
from Python (datetime.astimezone), so it is the same on Linux and Windows.

Trace files keep their own format (``YYYY-MM-DD HH:MM:SS`` local time, the
classic ser2net layout tools parse); the trace panel names the server's zone instead.

``behind()`` tells when the clock reads earlier than this installation's files show it
read already (a board without an RTC that started before NTP set its clock, a flat RTC
battery): the start-up log and Settings warn then. It only reads; how long something has
been going on is measured with time.monotonic() everywhere, which no clock step changes.
"""
from __future__ import annotations

import calendar
import datetime
import os
import re
import time

_FORMAT = "%Y-%m-%d %H:%M:%S"
_OFFSET = re.compile(r"[+-](\d\d):(\d\d)")

# No ser2netweb with this code ran before this day, so a clock that reads earlier is wrong.
# It never has to move with a release: on an installation that ran for a while, the
# newest backup and config.json's time are later anyway.
FLOOR = calendar.timegm((2026, 11, 1, 0, 0, 0, 0, 0, 0))
# How far the clock may read before those times without a warning: time sync stepping
# a fast clock back, an RTC that gained a little while the machine was off.
SLACK_S = 300.0


def stamp(t: float | None = None) -> str:
    """*t* (default: now) as the server's local time with its UTC offset."""
    when = datetime.datetime.fromtimestamp(time.time() if t is None else t, datetime.timezone.utc)
    return when.astimezone().isoformat(sep=" ", timespec="seconds")


def parse_stamp(text: str) -> float | None:
    """The epoch time of the stamp a line starts with: ``YYYY-MM-DD HH:MM:SS`` followed by
    its UTC offset (stamp()), or without one (a line an older version wrote: local time).
    None when the line doesn't start with one."""
    try:
        fields = time.strptime(text[:19], _FORMAT)
    except ValueError:
        return None
    offset = _OFFSET.match(text, 19)
    if offset is not None:
        sign = -1 if text[19] == "-" else 1
        return calendar.timegm(fields) - sign * (int(offset[1]) * 3600 + int(offset[2]) * 60)
    try:
        return time.mktime(fields)
    except (OverflowError, ValueError):
        return None


def zone(t: float | None = None) -> str:
    """The server's time zone at *t* (default: now), as the UI names it: 'UTC+03:00',
    'UTC+01:00 (CET)', 'UTC'."""
    local = datetime.datetime.fromtimestamp(time.time() if t is None else t,
                                            datetime.timezone.utc).astimezone()
    offset = local.isoformat(timespec="seconds")[19:]
    name = local.tzname() or ""
    text = "UTC" if offset in ("+00:00", "-00:00") else f"UTC{offset}"
    if name and name.upper() != "UTC" and not name.startswith(("+", "-")):
        text += f" ({name})"
    return text


def utc_text(t: float) -> str:
    """*t* as 'YYYY-MM-DD HH:MM:SS' in UTC."""
    return time.strftime(_FORMAT, time.gmtime(t))


def behind(store, now: float | None = None) -> tuple[str, float] | None:
    """Whether the system clock reads earlier than the latest time this installation's
    files show it read already: (what, when) for that time - "backup" (the newest
    configuration backup, named in UTC), "config" (config.json's modification time) or
    "release" (FLOOR) - when *now* is more than SLACK_S before it, else None. *store*:
    the config.ConfigStore. Only reads (blocking file I/O: a directory listing and a
    stat)."""
    from .config import backup_time

    now = time.time() if now is None else now
    known = [("release", float(FLOOR))]
    names = store.backup_names()
    if names:
        try:
            known.append(("backup", float(calendar.timegm(
                time.strptime(backup_time(names[0]), _FORMAT)))))
        except ValueError:
            pass
    try:
        known.append(("config", os.path.getmtime(store.path)))
    except OSError:
        pass
    what, when = max(known, key=lambda k: k[1])
    return (what, when) if now < when - SLACK_S else None


def warning(store, now: float | None = None) -> str:
    """The start-up log's line when the clock reads earlier than it can (behind()), else ""."""
    now = time.time() if now is None else now
    found = behind(store, now)
    if found is None:
        return ""
    what, when = found
    reads = f"WARNING: the system clock reads {utc_text(now)} UTC"
    if what == "release":
        head = f"{reads}, before this version of ser2netweb was made ({utc_text(when)[:10]}): it is wrong"
    elif what == "backup":
        head = (f"{reads}, earlier than the newest configuration backup ({utc_text(when)} "
                "UTC): it is probably wrong (or it was ahead when that backup was made)")
    else:
        head = (f"{reads}, earlier than config.json was last written ({utc_text(when)} UTC): "
                "it is probably wrong")
    return (f"{head}. Logs, the audit log and backups get wrong times, and sign-ins and "
            "certificate checks can fail. ser2netweb never sets the clock: set it, or let the "
            "system's time sync (NTP) do it (README: Time synchronisation)")
