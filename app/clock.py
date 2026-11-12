"""Times as ser2net writes them into its files.

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
"""
from __future__ import annotations

import calendar
import datetime
import re
import time

_FORMAT = "%Y-%m-%d %H:%M:%S"
_OFFSET = re.compile(r"[+-](\d\d):(\d\d)")


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
