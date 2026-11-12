"""Shared runtime state: config + store + supervisor + port watcher + logs.

A single AppState instance is created at startup and shared by the web layer and
the engine. Config mutations go through ``edit_config`` (under ``config_lock``): they
are persisted atomically, or undone, before the supervisor is reconciled.

All activity (logins/logouts, serial port open/close, client connect/disconnect,
mapping start/stop/edit, HTTP requests, errors) is funneled through ``log()`` and
written to ``all.log`` next to ``config.json`` (and to stdout, unless
SER2NET_LOG_STDOUT=off). The data directory holds more state: audit.log(.1), the rotated
all.log.N, backups/, logs/, traces/ and tls/. Deleting config.json alone resets the
accounts, settings and mappings but leaves those (audit.log and backups/ are shown to the
next admin; the next start prunes the per-mapping logs and traces that no mapping owns
any more); a clean start is ser2net stopped and the data directory deleted.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import logging
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from contextlib import suppress as _suppress
from logging.handlers import RotatingFileHandler
from typing import Any

from . import accounts, clock, logsetup, storage, tlsreload
from .config import (
    AppConfig, ConfigStore, ConfigWriteError, User, fold, lock_down_dir, mapping_file,
    valid_mapping_id,
)
from .i18n import _
from .engine.bridge import remove_files
from .engine.cluster import ClusterDiscovery
from .engine.portlist import PortWatcher
from .engine.supervisor import Supervisor
from .logtext import one_line
from .notify import Notifier
from .signins import SignIns
from .web.auth import LoginRateLimiter, RevokedSessions, SlowWork

# Per-mapping log retention (automatic maintenance).
LOG_MAX_BYTES = 100 * 1024 * 1024     # cap each mapping log at 100 MB
LOG_TRIM_TO = 0.9                      # ... cut to 90% of it then, so the next passes leave it
LOG_MAX_AGE_DAYS = 15                  # drop entries older than 15 days ...
LOG_VIEW_LINES = 1000                  # ... but never one of the last lines the Log panel shows
LOG_MAINTENANCE_INTERVAL = 3600       # run maintenance hourly (and once at startup)
CONFIG_CHECK_S = 5.0                   # how often config.json is looked at for outside edits
# Global log rotation (all.log via RetainingFileHandler; audit.log rotated manually).
ALL_LOG_MAX_BYTES = 10 * 1024 * 1024   # rotate all.log past 10 MB
ALL_LOG_BACKUPS = 5                    # keep 5 rotations (~60 MB total worst case)
AUDIT_LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate audit.log past 5 MB (one backup kept)
# With log_max_age_days set (Settings -> Retention), all.log and audit.log also begin a new
# file once their first record is 1/5 of that old, so each file spans at most that; the
# hourly retention deletes an older file once its first record is past the limit. audit.log
# then keeps up to AUDIT_FILES_KEPT older files (audit.log.1 ...), so the period fits.
AUDIT_FILES_KEPT = 5
# all.log could not be renamed for its rotation (Windows: another program has it open):
# written on, and rotated at the first record after this many seconds
ROLL_RETRY_S = 60.0
# One all.log message is cut after this many characters: a request (a sign-in attempt, a
# URL) needs no account, and must not write megabytes into the log at a time.
LOG_MESSAGE_MAX = 2000
# what an audit record keeps of its client IP, action, detail and user (characters)
AUDIT_FIELD_MAX = (64, 64, 500, 256)
# Failed sign-ins need no account. So that they can't push the other records out of
# audit.log, at most FAILED_LOGIN_RECORDS of them are recorded one by one in
# FAILED_LOGIN_WINDOW_S seconds; the others are counted, and one record at the end of that
# time says how many there were and from where (all.log keeps a line for every one).
FAILED_LOGIN_RECORDS = 20
FAILED_LOGIN_WINDOW_S = 600
# Opening a mapping's console or hex view needs no more than the viewer role (none at all
# with open access), so those records are bounded too: one per account (open access: per
# address), mapping and kind in VIEW_REPEAT_S seconds — the next one says how many opens
# it stands for — and at most VIEW_RECORDS in FAILED_LOGIN_WINDOW_S, then summed up as
# for failed sign-ins (all.log keeps a line for every open).
VIEW_REPEAT_S = 300
VIEW_RECORDS = 50
# A console session that wrote to its device gets a console_close record (how many bytes,
# never what): at most VIEW_RECORDS of them in FAILED_LOGIN_WINDOW_S, then summed up.
# A request to the REST API refused for its token (none, a wrong or an expired one): one
# api_auth_failed record per address in API_AUTH_REPEAT_S seconds, at most
# FAILED_LOGIN_RECORDS in FAILED_LOGIN_WINDOW_S (its all.log line: REFUSED_LOG_S).
API_AUTH_REPEAT_S = 60
# A request refused before it reached a handler (CSRF, no session, a bad API token, a
# console WebSocket without a session or from another site): one all.log line per
# address and kind in REFUSED_LOG_S seconds, the next one says how many there were.
REFUSED_LOG_S = 60
# first-run setup through a host name that isn't this machine's needs this file's code
SETUP_CODE_FILE = "setup-code.txt"
# SER2NET_LOG_STDOUT=off: the log lines go only to all.log, not to stdout as well (where
# systemd's journal or Docker's container log keep a copy of their own)
LOG_STDOUT_ENV = "SER2NET_LOG_STDOUT"
# the log trim (_trim_log_file): what it copies at a time, what of a line's start it reads
# for the line's time, and how often it tries to swap the file (Windows: a log view may
# hold it open for a moment)
_COPY_CHUNK = 1024 * 1024
_STAMP_BYTES = 64
_REPLACE_TRIES = 3
# a line's time as text ('YYYY-MM-DD HH:MM:SS', the zone's local time): one that sorts more
# than _SURELY_OLDER_S before the cutoff's is older whatever the zone was, without parsing
_STAMP = re.compile(rb"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
_SURELY_OLDER_S = 2 * 86400


def log_stdout(environ=None) -> tuple[bool, str]:
    """(whether the log lines also go to stdout, a warning for a value that is neither
    on nor off — then they do, as without it)."""
    raw = ((os.environ if environ is None else environ).get(LOG_STDOUT_ENV) or "").strip()
    value = raw.lower()
    if value in ("", "on", "1", "yes", "true"):
        return True, ""
    if value in ("off", "0", "no", "false"):
        return False, ""
    return True, (f"WARNING: {LOG_STDOUT_ENV}={raw[:40]!r} is not on or off; the log lines "
                  "also go to stdout")


class LineFormatter(logging.Formatter):
    """The format of all.log, stdout and the mapping logs: time (clock.stamp: local time
    with its UTC offset) and message, the message on one line (logtext.one_line: a line
    break or another control character in a name or a device's text becomes a space, so it
    can't start a line of its own or reach a terminal as an escape sequence). A traceback
    after it is left as it is."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(message)s")

    def formatTime(self, record, datefmt=None) -> str:  # noqa: N802 (logging API)
        return clock.stamp(record.created)

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 (logging API)
        record.message = one_line(record.message)
        return super().formatMessage(record)


def write_audit_record(path: str, actor_ip: str, action: str, detail: str = "",
                       user: str = "", *, max_age_s: float = 0) -> list[str]:
    """Append one record to the audit log at `path`: one line, tab-separated: time
    (clock.stamp: local time with its UTC offset; before 2.9 without the offset), client
    IP, action, detail, user (the user column was added in v2.8 — older lines have four
    fields). Each field is cut to its AUDIT_FIELD_MAX, and control characters in it become
    spaces (logtext.one_line), so a crafted mapping name can't forge an extra record or cut
    one short. Rotated past AUDIT_LOG_MAX_BYTES (one backup kept); with `max_age_s`
    (log_max_age_days) also once its first record is a fifth of that old, keeping
    AUDIT_FILES_KEPT older files. Best effort: never raises. Returns the fields as written."""
    fields = [clock.stamp()] + [
        one_line(str(value)[:limit])
        for value, limit in zip((actor_ip, action, detail, user), AUDIT_FIELD_MAX, strict=True)]
    line = "\t".join(fields)
    with _AUDIT_LOCK:
        try:
            # size-based rotation so audit.log can't grow forever (and by age, see above)
            if os.path.getsize(path) > AUDIT_LOG_MAX_BYTES or \
                    _audit_too_old(path, max_age_s, time.time()):
                with _suppress(OSError):
                    _rotate_audit(path, AUDIT_FILES_KEPT if max_age_s else 1)
        except OSError:
            pass  # file doesn't exist yet
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return fields


# audit.log is rotated in turn with the records written to it (write_audit_record, the
# hourly retention)
_AUDIT_LOCK = threading.Lock()


def _audit_too_old(path: str, max_age_s: float, now: float) -> bool:
    """With `max_age_s` set: whether the audit log at `path` holds a record a fifth of that
    old (its first one), so it is to begin a new file. Under _AUDIT_LOCK."""
    if not max_age_s:
        return False
    first = _first_stamp(path)
    return first is not None and now - first >= max_age_s / AUDIT_FILES_KEPT


def _rotate_audit(path: str, keep: int) -> None:
    """audit.log -> audit.log.1, .1 -> .2 ... up to audit.log.<keep>, or as far as older
    files are there (a period set before left them: the chain keeps its length, up to
    AUDIT_FILES_KEPT); the oldest beyond goes. Under _AUDIT_LOCK. Raises OSError."""
    while keep < AUDIT_FILES_KEPT and os.path.exists(f"{path}.{keep + 1}"):
        keep += 1
    for i in range(keep - 1, 0, -1):
        if os.path.exists(f"{path}.{i}"):
            os.replace(f"{path}.{i}", f"{path}.{i + 1}")
    os.replace(path, path + ".1")


def _first_stamp(path: str) -> float | None:
    """The time of the first record of a log file (all.log, audit.log): its first line in
    the first 64 KiB that starts with one. None when there is none (an empty file, none)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None
    for line in head.split(b"\n"):
        ts = _parse_log_ts(line[:_STAMP_BYTES].decode("utf-8", errors="replace"))
        if ts is not None:
            return ts
    return None


def _prune_rotated(path: str, count: int, max_age_s: float, now: float) -> list[str]:
    """Delete the rotated files path.1 ... path.<count> whose first record (else their
    modification time) is more than max_age_s before `now`. Returns their names."""
    gone = []
    for i in range(1, count + 1):
        name = f"{path}.{i}"
        try:
            first = _first_stamp(name)
            if first is None:
                first = os.path.getmtime(name)
            if first < now - max_age_s:
                os.remove(name)
                gone.append(os.path.basename(name))
        except OSError:  # (not there)
            continue
    return gone


class RetainingFileHandler(RotatingFileHandler):
    """all.log: rotated at its size and, with `max_age_s` set (log_max_age_days), also once
    its first record is a fifth of that old (max_age_s / backupCount): no file then spans
    more than that, and the hourly retention (_prune_rotated) deletes a rotated file once
    its first record is max_age_s old. So no record is kept longer, and about 80% of the
    period at least (size limits allowing). The file names stay all.log.1 ... .5."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_age_s = 0.0
        self.first = _first_stamp(self.baseFilename)  # the live file's first record
        self._retry_at = 0.0     # time.monotonic() before which a failed rotation waits
        self._failing = False    # the last rotation failed (said so in the file once)

    def too_old(self, now: float) -> bool:
        if not self.max_age_s or self.first is None:
            return False
        return now - self.first >= self.max_age_s / max(1, self.backupCount)

    def shouldRollover(self, record) -> bool:  # noqa: N802 (logging API)
        if self._retry_at and time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record)) or self.too_old(record.created)

    def doRollover(self) -> None:  # noqa: N802 (logging API)
        """Rotate: all.log -> all.log.1 -> ... -> all.log.<backupCount> (the oldest gone).
        all.log is moved aside first: when that fails (Windows: another program has it open
        without delete sharing) nothing is shifted or deleted, the record is written to
        all.log as it is and rotating is tried again after ROLL_RETRY_S. (Python's own
        rotation shifted the backups first, then dropped the record, and did both again for
        every later record: the history was gone after a few lines.)"""
        if self.backupCount <= 0:
            super().doRollover()
            return
        if self.stream:
            self.stream.close()
            self.stream = None  # (reopened below, or by emit)
        base = self.baseFilename
        aside = base + ".rotating"
        try:
            os.replace(base, aside)
        except FileNotFoundError:
            aside = ""
        except OSError as e:
            self._rotation_failed(e)
            return
        try:
            for i in range(self.backupCount - 1, 0, -1):
                older = f"{base}.{i}"
                if os.path.exists(older):
                    os.replace(older, f"{base}.{i + 1}")
            if aside:
                os.replace(aside, f"{base}.1")
        except OSError as e:  # a backup held open: put all.log back, keep what is there
            with _suppress(OSError):
                os.replace(aside, base)
            self._rotation_failed(e)
            return
        self._retry_at, self._failing = 0.0, False
        self.first = None
        if not self.delay:
            self.stream = self._open()

    def _rotation_failed(self, e: OSError) -> None:
        self._retry_at = time.monotonic() + ROLL_RETRY_S
        self.stream = self._open()
        if not self._failing:
            self._failing = True
            self.stream.write(f"{clock.stamp()} WARNING: "
                              f"{os.path.basename(self.baseFilename)} could not be rotated "
                              f"({e.strerror or e}: another program may have it open) — "
                              f"writing on, trying again every {ROLL_RETRY_S:g} s\n")

    def emit(self, record) -> None:
        super().emit(record)
        if self.first is None:
            self.first = record.created

    def roll_if_old(self, now: float) -> None:
        """Rotate now if the live file's first record is too old (a quiet log writes no
        record that would). Takes the handler's lock."""
        self.acquire()
        try:
            if self.too_old(now) and os.path.getsize(self.baseFilename) > 0:
                self.doRollover()
        finally:
            self.release()


class BoundedRecords:
    """Audit records of something any visitor can cause as often as they like: one by
    one up to `limit` in a window of `window_s` seconds; past that only counted until the
    window ends, when one `summary` record says how many more `what` there were, since
    when and from which addresses. Used on the event loop."""

    MAX_ADDRESSES = 1000  # addresses counted one by one past the limit (others: in total)

    def __init__(self, audit, limit: int, window_s: float, summary: str, what: str) -> None:
        self._audit = audit  # AppState.audit
        self.limit, self.window_s = limit, window_s
        self._summary, self._what = summary, what
        self._start: float | None = None  # time.monotonic() when the window began
        self._recorded = 0                # recorded one by one in this window
        self._more = 0                    # past the limit in this window: counted only
        self._since = ""                  # when the first of those came (wall clock)
        self._by_ip: dict[str, int] = {}
        self._user: str | None = None     # the name all of those were for ("": several)
        self._timer: asyncio.TimerHandle | None = None

    def add(self, ip: str, user: str, action: str = "", detail: str = "") -> bool:
        """One from `ip` by or for `user`, recorded as `action` (default: the summary's)
        with `detail`: True if it got its own record, False if it was only counted."""
        now = time.monotonic()
        if self._start is None or now - self._start >= self.window_s:
            self.flush()
            self._start, self._recorded = now, 0
        if self._recorded < self.limit:
            self._recorded += 1
            self._audit(ip, action or self._summary, detail, user=user)
            return True
        if not self._more:
            self._since, self._user = time.strftime("%H:%M:%S"), user
            with _suppress(RuntimeError):  # no event loop: the next add() or flush() writes it
                self._timer = asyncio.get_running_loop().call_later(
                    self._start + self.window_s - now, self.flush)
        elif user != self._user:
            self._user = ""
        self._more += 1
        if ip in self._by_ip or len(self._by_ip) < self.MAX_ADDRESSES:
            self._by_ip[ip] = self._by_ip.get(ip, 0) + 1
        return False

    def flush(self) -> None:
        """Write the record of the ones that were only counted so far, if any."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._more:
            return
        top = sorted(self._by_ip.items(), key=lambda item: -item[1])[:3]
        parts = [f"{n} from {ip}" for ip, n in top]
        rest = self._more - sum(n for _ip, n in top)
        if rest:
            parts.append(f"{rest} from other addresses")
        ip = top[0][0] if len(parts) == 1 and not rest else ""  # one address: its column
        self._audit(ip, self._summary, f"{self._more} more {self._what} since "
                    f"{self._since}, not recorded one by one: {', '.join(parts)}",
                    user=self._user or "")
        self._more, self._by_ip, self._user = 0, {}, None


class FailedLoginRecords(BoundedRecords):
    """The audit records of failed sign-ins (login_failed, and the ldap/oidc_login_denied
    of a directory sign-in refused for its name): one by one up to FAILED_LOGIN_RECORDS
    in FAILED_LOGIN_WINDOW_S seconds, then summed up in one login_failed record."""

    def __init__(self, audit, limit: int = FAILED_LOGIN_RECORDS,
                 window_s: float = FAILED_LOGIN_WINDOW_S) -> None:
        super().__init__(audit, limit, window_s, "login_failed", "failed sign-ins")


class RepeatRecords(BoundedRecords):
    """BoundedRecords of something the same caller can repeat: one record per `key` in
    `repeat_s` seconds — the ones in between are counted, and the next record of that key
    says how many (`again`, formatted with their number n and the time at) — and of those
    at most `limit` in `window_s`, then summed up in one `summary` record."""

    MAX_KEYS = 4096  # keys remembered (past that, one is recorded within the limit)

    def __init__(self, audit, limit: int, window_s: float, repeat_s: float, summary: str,
                 what: str, again: str = "{n} more after {at}") -> None:
        super().__init__(audit, limit, window_s, summary, what)
        self.repeat_s, self._again = repeat_s, again
        self._seen: dict[tuple, list] = {}  # key -> [recorded (monotonic), counted since, at]

    def note(self, ip: str, user: str, action: str, detail: str, key: tuple) -> bool:
        """One more of them: True if it got its own record."""
        now = time.monotonic()
        seen = self._seen.get(key)
        if seen is not None and now - seen[0] < self.repeat_s:
            seen[1] += 1
            return False
        if seen is not None and seen[1]:
            detail += "; " + self._again.format(n=seen[1], at=seen[2]) + \
                ", not recorded one by one"
        if key not in self._seen and len(self._seen) >= self.MAX_KEYS:
            self._seen = {k: v for k, v in self._seen.items() if now - v[0] < self.repeat_s}
        if key in self._seen or len(self._seen) < self.MAX_KEYS:
            self._seen[key] = [now, 0, time.strftime("%H:%M:%S")]
        return self.add(ip, user, action, detail)


class ViewRecords(RepeatRecords):
    """The console_open / hex_view_open records: one per `key` (account or address,
    mapping, kind) in VIEW_REPEAT_S seconds — the opens in between are counted, and the
    next record of that key says how many — and of those at most VIEW_RECORDS in
    FAILED_LOGIN_WINDOW_S, then summed up in one console_open record."""

    def __init__(self, audit, limit: int = VIEW_RECORDS, window_s: float = FAILED_LOGIN_WINDOW_S,
                 repeat_s: float = VIEW_REPEAT_S) -> None:
        super().__init__(audit, limit, window_s, repeat_s, "console_open",
                         "console and hex view opens", "opened {n} more times after {at}")

    def open(self, ip: str, user: str, action: str, detail: str, key: tuple) -> bool:
        """A console or hex view opened: True if it got its own record."""
        return self.note(ip, user, action, detail, key)


class LineLimiter:
    """all.log lines of something any visitor can cause as often as they like (a request
    refused before it reached a handler): one line per key in `every_s` seconds; the ones
    in between are counted, and the next line of that key says how many there were."""

    MAX_KEYS = 4096

    def __init__(self, every_s: float = REFUSED_LOG_S) -> None:
        self.every_s = every_s
        self._keys: dict[tuple, list] = {}  # key -> [logged (monotonic), held back, since]

    def line(self, key: tuple, text: str) -> str | None:
        """The line to log for one more of `key` (`text`, plus how many were held back
        before it), or None to hold this one back."""
        now = time.monotonic()
        seen = self._keys.get(key)
        if seen is not None and now - seen[0] < self.every_s:
            if not seen[1]:
                seen[2] = time.strftime("%H:%M:%S")
            seen[1] += 1
            return None
        if seen is not None and seen[1]:
            text += f" ({seen[1]} more like it since {seen[2]}, not logged one by one)"
        if key not in self._keys and len(self._keys) >= self.MAX_KEYS:
            self._keys = {k: v for k, v in self._keys.items() if now - v[0] < self.every_s}
        if key in self._keys or len(self._keys) < self.MAX_KEYS:
            self._keys[key] = [now, 0, ""]
        return text


def _parse_log_ts(line: str) -> float | None:
    """The epoch time a log line starts with: 'YYYY-MM-DD HH:MM:SS' with its UTC offset,
    or without one (a line written before 2.9) - clock.parse_stamp."""
    return clock.parse_stamp(line)


def _line_start(fh, offset: int) -> int:
    """The offset of the first line that begins at or after `offset` in the open binary
    file `fh` (only a line feed ends a line); its end when no line begins there."""
    if offset <= 0:
        return 0
    fh.seek(offset - 1)
    if fh.read(1) == b"\n":
        return offset
    while True:
        chunk = fh.readline(_COPY_CHUNK)
        if not chunk or chunk.endswith(b"\n"):
            return fh.tell()


def _tail_start(fh, size: int, lines: int) -> int:
    """The offset at which the last `lines` lines of the open binary file `fh` (`size`
    bytes) begin: 0 when it has no more, `size` for 0 lines. Reads backwards in chunks."""
    if lines <= 0:
        return size
    fh.seek(size - 1)
    need = lines + (fh.read(1) == b"\n")  # the last line's own line feed ends no other
    pos, found = size, 0
    while pos > 0:
        step = min(64 * 1024, pos)
        pos -= step
        fh.seek(pos)
        chunk = fh.read(step)
        count = chunk.count(b"\n")
        if found + count >= need:
            end = len(chunk)
            for _i in range(need - found):
                end = chunk.rindex(b"\n", 0, end)
            return pos + end + 1
        found += count
    return 0


@contextlib.contextmanager
def _holding(handlers):
    """Hold the locks of these logging handlers: a record logged meanwhile waits."""
    held = []
    try:
        for h in handlers:
            h.acquire()
            held.append(h)
        yield
    finally:
        for h in reversed(held):
            h.release()


def _trim_log_file(path: str, cutoff_ts: float, max_bytes: int, keep_lines: int = 0,
                   handlers=()) -> bool:
    """Trim a log file in place: drop the lines older than cutoff_ts (but none of the last
    `keep_lines`) and, if it is still over max_bytes, keep only its newest LOG_TRIM_TO
    part, from a line's start. Returns True if the file changed. Runs in a worker thread.

    Only a line feed ends a line (a CR, NEL or U+2028 inside a message doesn't). Lines are
    in time order, so the age filter keeps the file from its first line with a time at or
    after cutoff_ts. A line without a time (the rest of a message that had a line break,
    as older versions wrote it) belongs to the one before it: skipped there, it goes or
    stays with that one; nothing is dropped for its age from a file in which no line has a
    time. Lines before the size cut go anyway, so the age filter starts there.

    In constant memory: only the start of each line the trim reaches is read (a file
    within its limits: its first line), and what is kept is copied in chunks, byte for
    byte (CRLF, invalid UTF-8), to <path>.tmp, which then replaces the file. `handlers`:
    the logging FileHandlers that write to it, which stay attached: under their locks the
    lines they wrote meanwhile are copied too and their files closed for the swap (each
    opens the new one with its next record). A file deleted meanwhile (its mapping was)
    is not made again; <path>.tmp never stays."""
    older = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(cutoff_ts - _SURELY_OLDER_S)).encode()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            size_cut = _line_start(fh, size - int(max_bytes * LOG_TRIM_TO)) \
                if size > max_bytes else 0
            start, pos, dated, stop = None, size_cut, False, size
            fh.seek(pos)
            while pos < stop:
                head = fh.readline(_STAMP_BYTES)
                if not head:
                    break
                line_at, rest = pos, head
                while rest and not rest.endswith(b"\n"):  # the rest of a long line
                    rest = fh.readline(_COPY_CHUNK)
                pos = fh.tell()
                if head[:19] < older and _STAMP.match(head):
                    ts: float | None = 0.0  # (most of a long scan: strptime is slow)
                else:
                    ts = _parse_log_ts(head.decode("utf-8", errors="replace"))
                if ts is None:
                    continue
                if ts >= cutoff_ts:
                    start = line_at
                    break
                if not dated:  # the age filter cuts: never into the last keep_lines
                    dated = True
                    stop = max(size_cut, _tail_start(fh, size, keep_lines))
                    fh.seek(pos)
            if start is None:
                start = stop if dated else size_cut
    except OSError:
        return False
    if start <= 0:
        return False
    tmp = path + ".tmp"
    try:
        with open(path, "rb") as src, open(tmp, "wb") as out:
            src.seek(start)
            shutil.copyfileobj(src, out, _COPY_CHUNK)  # while the handlers write on
            copied = src.tell()
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(_REPLACE_TRIES):
            with _holding(handlers):
                if not os.path.exists(path):
                    return False
                with open(path, "rb") as src, open(tmp, "ab") as out:
                    src.seek(copied)
                    shutil.copyfileobj(src, out, _COPY_CHUNK)  # what they wrote meanwhile
                    copied = src.tell()
                    out.flush()
                    os.fsync(out.fileno())
                for h in handlers:
                    stream = h.setStream(None)
                    if stream is not None:
                        stream.close()
                try:
                    os.replace(tmp, path)
                    return True
                except PermissionError:  # Windows: a log view has it open for a moment
                    if attempt == _REPLACE_TRIES - 1:
                        raise
            time.sleep(0.2)
        return False
    finally:
        with _suppress(OSError):
            os.remove(tmp)


def _snapshot(value: Any) -> tuple:
    """What _restore needs to put `value` back as it is now, in place: the same objects
    (dataclasses, lists, dicts) with the same contents, also where a change replaced one."""
    if (dataclasses.is_dataclass(value) and not isinstance(value, type)
            and not value.__dataclass_params__.frozen):  # type: ignore[attr-defined]
        return value, {f.name: _snapshot(getattr(value, f.name))
                       for f in dataclasses.fields(value)}
    if isinstance(value, list):
        return value, [_snapshot(v) for v in value]
    if isinstance(value, dict):
        return value, {k: _snapshot(v) for k, v in value.items()}
    return value, None  # immutable: str, numbers, None, a frozen note


def _restore(snapshot: tuple) -> Any:
    value, inner = snapshot
    if inner is None:
        return value
    if isinstance(value, list):
        value[:] = [_restore(s) for s in inner]
    elif isinstance(value, dict):
        restored = {k: _restore(s) for k, s in inner.items()}
        value.clear()
        value.update(restored)
    else:
        for name, s in inner.items():
            setattr(value, name, _restore(s))
    return value


async def _to_its_end(task: asyncio.Future) -> bool:
    """Wait until `task` is done, also when the waiting task is cancelled meanwhile (it
    is not passed on). Returns whether it was: the caller raises CancelledError then."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


@dataclasses.dataclass
class ConfigEdit:
    """What AppState.edit_config did, once its block has ended."""
    saved: bool = False      # config.json was written (False: the block changed nothing)
    backed_up: bool = True   # what config.json held before is in backups/ (False: failed)


class AppState:
    def __init__(self, store: ConfigStore, config: AppConfig):
        self.store = store
        self.config = config
        self.data_dir = os.path.dirname(os.path.abspath(store.path))
        store.log = self.log  # a backup that fails, a clock behind the backups
        store.on_replaced = self._outside_edit_replaced  # (a save replaced a hand edit)
        self._outside_edit: tuple[float, float] | None = None  # (checked at, noted mtime)
        # all.log lives next to config.json; per-mapping logs live in data/logs/
        self.log_path = os.path.join(self.data_dir, "all.log")
        self.logs_dir = os.path.join(self.data_dir, "logs")
        # traffic traces: the ONLY place a trace is written (see config.trace_file_name)
        self.traces_dir = os.path.join(self.data_dir, "traces")
        stdout, stdout_problem = log_stdout()
        self._logger = self._setup_logging(self.log_path, stdout=stdout)
        if stdout_problem:
            self._logger.info(stdout_problem)
        self._mapping_loggers: dict[str, logging.Logger] = {}
        # each supervisor operation acts on the mapping's config when its turn comes
        self.supervisor = Supervisor(logger=self.log, logger_factory=self.make_mapping_logger,
                                     trace_dir=self.traces_dir, state_dir=self.data_dir,
                                     lookup=lambda mid: self.config.get_mapping(mid))
        # a serial port that appears wakes the mappings waiting to reopen theirs
        self.ports = PortWatcher(interval=2.0, on_change=self.supervisor.ports_changed)
        self.cluster = ClusterDiscovery(self.config, self.log)
        self.cluster.serving = self.admin_serving  # (beacons name what the UI serves)
        self.rate_limiter = LoginRateLimiter()
        # failed sign-ins (and password changes) per user name, letter case aside, from
        # any address: they pause that name only, never for good, and stay below the
        # usual directory lock-out thresholds (LDAP accounts)
        self.name_limiter = LoginRateLimiter(max_attempts=10, window_s=900, key=fold)
        # password hashing and checks, directory (LDAP) sign-ins: a few at a time
        self.password_work = SlowWork(limit=2, queue=8)
        self.directory_work = SlowWork(limit=4, queue=16)
        self.failed_logins = FailedLoginRecords(self.audit)  # their audit records
        self.view_records = ViewRecords(self.audit)          # console / hex view opens
        self.console_writes = BoundedRecords(self.audit, VIEW_RECORDS, FAILED_LOGIN_WINDOW_S,
                                             "console_close",
                                             "console sessions that wrote to a device")
        self.api_auth_failures = RepeatRecords(
            self.audit, FAILED_LOGIN_RECORDS, FAILED_LOGIN_WINDOW_S, API_AUTH_REPEAT_S,
            "api_auth_failed", "refused REST API requests")
        self.refused_lines = LineLimiter()  # requests refused before reaching a handler
        self.revoked_sessions = RevokedSessions()  # sessions ended by a logout
        # when each REST API token was last used: token id ('' for api_token_hash's) ->
        # epoch seconds; in memory only (a save on every request would push out backups)
        self.api_token_used: dict[str, float] = {}
        # when each account last signed in (from audit.log, then every session issued)
        self.signins = SignIns()
        # sessions issued per account (Ctx.set_session), and that count with the account's
        # pwd_version right after its "Sign out my other sessions": the same again means no
        # other session was made since, so the next click has nothing to end (and doesn't
        # save config.json, whose 50 backups each save pushes along) — in memory only
        self.sessions_issued: dict[str, int] = {}
        self.others_signed_out: dict[str, tuple[int, int]] = {}
        # LDAP/OIDC group settings that matched a sign-in's group only as part of its name,
        # which no longer counts: (source, setting) -> details, shown in Settings until
        # that section is saved (in memory only)
        self.group_warnings: dict[tuple[str, str], dict] = {}
        # the last LDAP sign-in that failed because the directory server's certificate was
        # refused (or ldap.ca_file unusable), shown in Settings until the LDAP section is
        # saved or a directory sign-in works (in memory only)
        self.ldap_tls_problem: dict | None = None
        # uploaded imports waiting for the user to confirm the preview: token -> item
        self.pending_imports: dict[str, dict] = {}
        self.notifier = Notifier(self)
        self.config_lock = asyncio.Lock()
        self.started_at = time.time()        # when this process started (wall clock)
        self.started_mono = time.monotonic()  # the same for how long it runs (uptime)
        # the admin UI listener's connection guard (runtime.py; None in tests without one),
        # the address it listens on, and how its TLS context is made from the settings
        # while ser2net runs (Settings -> Admin TLS); without them a TLS change waits for
        # the next start
        self.admin_guard: Any = None
        self.admin_served: tuple[str, int] | None = None
        self.admin_tls: Any = None
        # why the admin TLS settings saved last are not served yet ("": they are)
        self.admin_tls_waiting = ""
        # the Windows service's name when ser2net runs as one (runtime.service_main)
        self.service_name = ""
        # reloads of config.json under way (SIGHUP: app/reload.py)
        self.reloads: set[asyncio.Task] = set()
        # the event loop watchdog (runtime.py, app/watchdog.py; None in tests without one),
        # and the start-up line about a previous run it ended ("" when there was none)
        self.loop_watchdog: Any = None
        self.previous_hang = ""
        # first-run setup (see prepare_first_run)
        self.setup_code = ""
        self.setup_locked = False
        self._maint_task: asyncio.Task | None = None
        self._requests_task: asyncio.Task | None = None
        # per-mapping logs can contain raw serial traffic; keep the logs dir private
        # on both platforms (POSIX 0700 / Windows owner-only ACL).
        for d in (self.logs_dir, self.traces_dir):
            with _suppress(OSError):
                os.makedirs(d, exist_ok=True)
            lock_down_dir(d)

    # ---------------- the admin UI as it is served ----------------
    @property
    def serving_tls(self) -> bool:
        """Whether the admin UI's new connections get TLS: what the cookies' Secure flag,
        HSTS and the cluster's beacons go by. A TLS change in Settings or a reload applies
        at once (a process without a listener: from the next start)."""
        guard = self.admin_guard
        return self.config.admin_ui.tls_enabled if guard is None else guard.ssl is not None

    def admin_serving(self) -> tuple[str, int, bool]:
        """(bind IP, port, TLS) of the admin UI as it serves now. The address in config.json
        may differ: it is used from the next start."""
        admin = self.config.admin_ui
        ip, port = self.admin_served or (admin.bind_ip, admin.port)
        return ip, port, self.serving_tls

    async def prepare_admin_tls(self, admin) -> tlsreload.Prepared:
        """The admin UI's TLS context for the settings `admin` (AdminUI), made from its files
        as they are now (no TLS: a None context), to hand to use_admin_tls."""
        build = self.admin_tls
        if build is None or self.admin_guard is None:
            return tlsreload.Prepared(later="this process has no admin UI listener")
        return await tlsreload.prepare(lambda: build(admin))

    def use_admin_tls(self, prepared: tlsreload.Prepared) -> bool:
        """New connections to the admin UI get the prepared TLS context (None: plain HTTP);
        those open keep theirs. False when it isn't usable (the one in use stays)."""
        guard = self.admin_guard
        if guard is None or not prepared.usable:
            if not prepared.error:  # (files that can't be used: the change was refused)
                self.admin_tls_waiting = prepared.later or "no admin UI listener"
            return False
        self.admin_tls_waiting = ""
        was, guard.ssl = guard.ssl is not None, prepared.context
        if guard.ssl is not None:
            self.log("admin UI: TLS certificate loaded, new connections use it"
                     + ("" if was else " (TLS is on now)"))
            from .runtime import tls_certificate_note  # (runtime imports this module)
            if note := tls_certificate_note(self.config.admin_ui.tls_cert):
                self.log(note)  # its subject and validity, a WARNING when it ends soon
        elif was:
            self.log("admin UI: TLS is off now, new connections use plain HTTP")
            if self.admin_serving()[0] not in ("127.0.0.1", "::1", "localhost"):
                self.log("WARNING: admin UI is bound to a network address over plain HTTP — "
                         "the password and session cookies travel unencrypted.")
        return True

    @staticmethod
    def _setup_logging(log_path: str, stdout: bool = True) -> logging.Logger:
        """The 'ser2net' logger: all.log, and stdout too unless `stdout` is False (also
        then when all.log can't be opened, so the lines still go somewhere)."""
        logger = logging.getLogger("ser2net")
        logger.setLevel(logging.INFO)
        logsetup.close_console(logger)
        logger.handlers.clear()
        logger.propagate = False
        fmt = LineFormatter()

        # the console copy is written by a thread of its own, never on the event loop (a
        # paused console must not hold up the bridges); `fmt` is all.log's format
        console = logsetup.console_handler(sys.stdout, fmt)
        logger.addHandler(console)

        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            # rotate all.log so the global activity/audit trail can't grow without
            # bound (per-mapping logs are trimmed separately by run_log_maintenance).
            fileh = RetainingFileHandler(
                log_path, maxBytes=ALL_LOG_MAX_BYTES, backupCount=ALL_LOG_BACKUPS,
                encoding="utf-8")
            fileh.setFormatter(fmt)
            logger.addHandler(fileh)
        except OSError as e:
            logger.warning(f"could not open log file {log_path}: {e}")
        else:
            if not stdout:
                logger.removeHandler(console)
                console.close()  # (its writer thread)
        return logger

    def log(self, msg: str) -> None:
        """One line in all.log and on stdout: cut at LOG_MESSAGE_MAX characters, control
        characters written as spaces (LineFormatter)."""
        if len(msg) > LOG_MESSAGE_MAX:
            msg = f"{msg[:LOG_MESSAGE_MAX]}… ({len(msg) - LOG_MESSAGE_MAX} more characters)"
        self._logger.info(msg)

    def log_refused(self, key: tuple, msg: str) -> None:
        """log() a request refused before it reached a handler: one line per `key` (its
        address and kind) in REFUSED_LOG_S seconds (refused_lines)."""
        line = self.refused_lines.line(key, msg)
        if line is not None:
            self.log(line)

    @property
    def audit_path(self) -> str:
        return os.path.join(self.data_dir, "audit.log")

    def audit(self, actor_ip: str, action: str, detail: str = "", user: str = "") -> None:
        """Append a config-change audit record (write_audit_record), separate from
        all.log, and note it in all.log."""
        _time, ip, action, detail, user = write_audit_record(
            self.audit_path, actor_ip, action, detail, user,
            max_age_s=self.config.log_max_age_days * 86400)
        who = f" by {user}" if user else ""
        self._logger.info(f"AUDIT {ip} {action} {detail}{who}".rstrip())

    def read_audit(self) -> list[dict]:
        """Every audit record (audit.log.<AUDIT_FILES_KEPT> ... audit.log.1 + audit.log),
        newest first. Blocking file I/O: call via asyncio.to_thread."""
        records = []
        older = [f"{self.audit_path}.{i}" for i in range(AUDIT_FILES_KEPT, 0, -1)]
        for path in older + [self.audit_path]:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    # only a line feed ends a record (CRLF reads as one): splitlines()
                    # would also cut one at a U+2028 or NEL that an older version kept
                    lines = fh.read().split("\n")
            except OSError:
                continue
            for ln in lines:
                parts = ln.split("\t")
                if len(parts) < 3:
                    continue
                parts += [""] * (5 - len(parts))
                records.append({"time": parts[0], "ip": parts[1], "action": parts[2],
                                "detail": parts[3], "user": parts[4]})
        records.reverse()
        return records

    # ---------------- per-mapping logs ----------------
    def mapping_log_path(self, mapping_id: str) -> str:
        """logs/<mapping id>.log. Raises ValueError for anything but a valid mapping id."""
        return mapping_file(self.logs_dir, mapping_id, ".log")

    def _attach_mapping_handler(self, lg: logging.Logger, mapping_id: str) -> None:
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            fh = logging.FileHandler(self.mapping_log_path(mapping_id), encoding="utf-8")
            fh.setFormatter(LineFormatter())
            lg.addHandler(fh)
        except (OSError, ValueError) as e:
            self.log(f"could not open mapping log for {mapping_id!r}: {e}")

    def _mapping_logger(self, mapping_id: str) -> logging.Logger:
        lg = self._mapping_loggers.get(mapping_id)
        if lg is None:
            lg = logging.getLogger(f"ser2net.map.{mapping_id}")
            lg.setLevel(logging.INFO)
            lg.propagate = False
            lg.handlers.clear()
            self._attach_mapping_handler(lg, mapping_id)
            self._mapping_loggers[mapping_id] = lg
        return lg

    def make_mapping_logger(self, mapping):
        """Return a logger callable for a mapping: writes to all.log (prefixed with
        the mapping name) AND to the mapping's own persistent log file."""
        name, mid = mapping.name, mapping.id
        mlogger = self._mapping_logger(mid)

        def log(msg: str) -> None:
            self.log(f"[{name}] {msg}")
            mlogger.info(msg)

        return log

    def read_mapping_log(self, mapping_id: str, limit: int = LOG_VIEW_LINES,
                         tail_bytes: int = 512 * 1024) -> list[str]:
        """Return the mapping's log lines, newest first (most recent at index 0).

        Reads only the last ``tail_bytes`` of the file instead of the whole thing —
        per-mapping logs can grow to 100 MB, and loading all of that to show the last
        1000 lines wasted memory and blocked the event loop. Call via asyncio.to_thread
        from async handlers (the read still does blocking file I/O)."""
        try:
            path = self.mapping_log_path(mapping_id)
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    fh.readline()  # drop the partial first line after the seek
                data = fh.read()
        except (OSError, ValueError):
            return []
        # only a line feed (or CRLF) ends a line: splitlines() would also cut one at a
        # CR, NEL or U+2028 inside a message (an older version wrote them as they were)
        lines = data.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
        if not lines[-1]:
            lines.pop()
        return lines[-limit:][::-1]

    def _forget_mapping_logger(self, mapping_id: str) -> None:
        """Close the mapping's log file and forget its cached logger."""
        lg = self._mapping_loggers.pop(mapping_id, None)
        if lg is not None:
            for h in list(lg.handlers):
                with _suppress(Exception):
                    h.close()
                lg.removeHandler(h)

    async def forget_mappings(self, ids) -> None:
        """Close what this process keeps open for mappings that are gone without a
        deletion of their own (a replace import, a backup restore): a runner if one is
        left (its listener, serial port and trace writer) and the cached logger with its
        open log file, which stayed open until a restart before. Their files stay, so an
        undone restore finds them; the next start prunes the ones no mapping owns
        (prune_mapping_logs, prune_orphan_traces)."""
        for mid in ids:
            await self.supervisor.remove_mapping(mid)
            self._forget_mapping_logger(mid)

    def delete_mapping_log(self, mapping_id: str) -> None:
        self._forget_mapping_logger(mapping_id)
        # the log file may never have been created (FileNotFoundError)
        with _suppress(OSError, ValueError):
            os.remove(self.mapping_log_path(mapping_id))

    # ---------------- per-mapping traces ----------------
    def trace_files(self, mapping_id: str) -> list[str]:
        """A mapping's trace files in data/traces (current + rotated), newest first. They
        are the files named "<id>-..." (a valid mapping id has no "-", so the prefix of one
        mapping never matches another's files); none for an invalid id."""
        if not valid_mapping_id(mapping_id):
            return []
        prefix = f"{mapping_id}-"
        try:
            names = [n for n in os.listdir(self.traces_dir) if n.startswith(prefix)]
        except OSError:
            return []
        found = []
        for n in names:  # one may be rotated or deleted meanwhile
            path = os.path.join(self.traces_dir, n)
            with _suppress(OSError):
                st = os.stat(path)
                if stat.S_ISREG(st.st_mode):
                    found.append((st.st_mtime, path))
        return [path for _mtime, path in sorted(found, reverse=True)]

    async def delete_trace_files(self, mapping_id: str, names=None,
                                 ) -> tuple[list[str], list[tuple[str, str]]]:
        """Delete those `names` of a mapping's trace files, all of them when None. Only
        names of its own list (trace_files) are touched; the supervisor has the files a
        running trace writes deleted by that writer. Returns (the names deleted, [(name,
        why not)])."""
        paths = {os.path.basename(p): p
                 for p in await asyncio.to_thread(self.trace_files, mapping_id)}
        wanted = list(paths) if names is None else list(dict.fromkeys(names))
        failed = [(n, _("Trace file not found")) for n in wanted if n not in paths]
        found = [paths[n] for n in wanted if n in paths]
        result = (await self.supervisor.remove_trace_files(mapping_id, found)) if found else {}
        failed += [(os.path.basename(p), e.strerror or str(e))
                   for p, e in result.items() if e is not None]
        return [os.path.basename(p) for p, e in result.items() if e is None], failed

    def traced_mapping_ids(self) -> set[str]:
        """The ids that have at least one file in data/traces: what comes before the first
        "-" of a file's name (a valid mapping id has none). Blocking: one listdir."""
        try:
            names = os.listdir(self.traces_dir)
        except OSError:
            return set()
        return {n.partition("-")[0] for n in names if "-" in n}

    def delete_mapping_traces(self, mapping_id: str) -> None:
        for path in self.trace_files(mapping_id):
            with _suppress(OSError):
                os.remove(path)

    def _traces_older_than(self, cutoff: float) -> dict[str, list[str]]:
        """The trace files last written before `cutoff`, by the id their name begins with
        ("" for a name with no "-"). Blocking."""
        try:
            names = os.listdir(self.traces_dir)
        except OSError:
            return {}
        old: dict[str, list[str]] = {}
        for fn in names:
            with _suppress(OSError):
                st = os.stat(os.path.join(self.traces_dir, fn))
                if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                    mid, dash, _rest = fn.partition("-")
                    old.setdefault(mid if dash else "", []).append(fn)
        return old

    async def run_trace_maintenance(self) -> int:
        """With trace_max_age_days set (0, the default, keeps every trace): delete the
        trace files not written for that many days (their modification time, compared with
        storage.now(): a clock stepped ahead meanwhile deletes nothing early), a running
        trace's through its writer (delete_trace_files), the file it writes included. A
        running trace whose file holds traffic from before then begins a new file first
        (Supervisor.renew_traces), so that a file written into all the time goes too, that
        many days later. Logged when it did something; returns how many files went."""
        days = self.config.trace_max_age_days
        if not days:
            return 0
        cutoff = storage.now() - days * 86400
        renewed = await self.supervisor.renew_traces(cutoff)
        if renewed:
            self.log(f"trace maintenance: {renewed} trace(s) begun in a new file, as theirs "
                     f"held traffic from more than {days:g} days ago")
        old = await asyncio.to_thread(self._traces_older_than, cutoff)
        deleted, kept = 0, 0
        for mid, names in old.items():
            if self.config.get_mapping(mid) is not None:
                gone, failed = await self.delete_trace_files(mid, names)
                deleted, kept = deleted + len(gone), kept + len(failed)
            else:  # no mapping writes it (the start-up prunes these anyway)
                result = await asyncio.to_thread(
                    remove_files, [os.path.join(self.traces_dir, n) for n in names])
                deleted += sum(e is None for e in result.values())
                kept += sum(e is not None for e in result.values())
        if deleted or kept:
            self.log(f"trace maintenance: deleted {deleted} trace file(s) not written for "
                     f"{days:g} days" + (f"; {kept} could not be deleted" if kept else ""))
        return deleted

    def prune_orphan_traces(self) -> None:
        """Delete the files in data/traces that belong to no configured mapping: left by a
        replace import, a backup restore or a deleted config.json, and shown in no panel.
        A file is kept when its name begins with "<id>-" of a mapping (a valid id has no
        "-"). Logged with how many files and MB went."""
        valid = {m.id for m in self.config.mappings}
        try:
            names = os.listdir(self.traces_dir)
        except OSError:
            return
        count = size = 0
        for fn in names:
            mid, dash, _rest = fn.partition("-")
            path = os.path.join(self.traces_dir, fn)
            if (dash and mid in valid) or not os.path.isfile(path):
                continue
            try:
                n = os.path.getsize(path)
                os.remove(path)
            except OSError:
                continue
            count, size = count + 1, size + n
        if count:
            self.log(f"removed {count} trace file(s) ({size / 1048576:.1f} MB) of mappings "
                     "that no longer exist")

    def prune_mapping_logs(self) -> None:
        """Delete per-mapping log files whose mapping no longer exists: after config.json
        was deleted (no mappings), all of them go at the next start. Trace files and the
        rest of the data directory are left as they are."""
        valid = {m.id for m in self.config.mappings}
        try:
            names = os.listdir(self.logs_dir)
        except OSError:
            return
        for fn in names:
            if fn.endswith(".log") and fn[:-4] not in valid:
                with _suppress(OSError):
                    os.remove(os.path.join(self.logs_dir, fn))

    async def run_log_maintenance(self) -> None:
        """Trim every per-mapping log: drop entries older than LOG_MAX_AGE_DAYS (or the
        shorter log_max_age_days), and cap each file at LOG_MAX_BYTES (keeping the most
        recent portion), in a worker thread (_trim_log_file). With the default 15 days the
        age trim keeps the last LOG_VIEW_LINES (what its Log panel shows: a clock that
        jumps ahead, or a date corrected after lines were stamped with a wrong one, must
        not empty it); a retention period set in Settings keeps nothing older. Ages are
        measured from storage.now(). The mapping's log handler stays attached: what it
        writes meanwhile is kept."""
        set_days = self.config.log_max_age_days
        days = min(LOG_MAX_AGE_DAYS, set_days) if set_days else LOG_MAX_AGE_DAYS
        keep_lines = 0 if set_days else LOG_VIEW_LINES
        cutoff = storage.now() - days * 86400
        try:
            files = [f for f in os.listdir(self.logs_dir) if f.endswith(".log")]
        except OSError:
            return
        for fn in files:
            lg = self._mapping_loggers.get(fn[:-4])
            handlers = [h for h in lg.handlers if isinstance(h, logging.FileHandler)] \
                if lg is not None else []
            try:
                changed = await asyncio.to_thread(
                    _trim_log_file, os.path.join(self.logs_dir, fn), cutoff, LOG_MAX_BYTES,
                    keep_lines=keep_lines, handlers=handlers)
                if changed:
                    self.log(f"log maintenance: trimmed {fn} (>{days}d / "
                             f">{LOG_MAX_BYTES // 1048576}MB)")
            except Exception as e:  # never let maintenance crash the loop
                self.log(f"log maintenance error on {fn}: {e}")

    def _all_log_handler(self) -> RetainingFileHandler | None:
        return next((h for h in self._logger.handlers if isinstance(h, RetainingFileHandler)),
                    None)

    async def run_retention(self) -> int:
        """With log_max_age_days set (0, the default: only their size and count limits):
        all.log and audit.log begin a new file once their first record is a fifth of that
        old (RetainingFileHandler, write_audit_record; here also when nothing is logged),
        their older files go once their first record is past it (_prune_rotated), and so
        do the configuration backups named for an older time, but never the newest
        BACKUPS_MIN_KEPT (ConfigStore.prune_backups). Ages are measured from
        storage.now(). Logged when it deleted something; returns how many files went."""
        days = self.config.log_max_age_days
        handler = self._all_log_handler()
        if handler is not None:
            handler.max_age_s = days * 86400.0
        if not days:
            return 0
        max_age, now = days * 86400.0, storage.now()

        def run() -> tuple[list[str], list[str]]:
            logs: list[str] = []
            if handler is not None:  # in turn with what is logged meanwhile
                handler.acquire()
                try:
                    handler.roll_if_old(now)
                    logs += _prune_rotated(handler.baseFilename, handler.backupCount, max_age,
                                           now)
                finally:
                    handler.release()
            with _AUDIT_LOCK:
                with _suppress(OSError):
                    if _audit_too_old(self.audit_path, max_age, now) and \
                            os.path.getsize(self.audit_path) > 0:
                        _rotate_audit(self.audit_path, AUDIT_FILES_KEPT)
                logs += _prune_rotated(self.audit_path, AUDIT_FILES_KEPT, max_age, now)
            return logs, self.store.prune_backups(max_age, now)

        logs, backups = await asyncio.to_thread(run)
        if logs or backups:
            self.log(f"retention: deleted {len(logs)} log file(s)"
                     + (f" ({', '.join(logs)})" if logs else "")
                     + f" and {len(backups)} configuration backup(s) older than {days} days")
        return len(logs) + len(backups)

    async def _log_maintenance_loop(self) -> None:
        await self.load_sign_ins()
        while True:
            await self.run_log_maintenance()
            try:
                await self.run_retention()
            except Exception as e:  # never let maintenance crash the loop
                self.log(f"retention failed: {e}")
            try:
                await self.run_trace_maintenance()
            except Exception as e:  # never let maintenance crash the loop
                self.log(f"trace maintenance failed: {e}")
            try:
                await self.expire_external_accounts()
            except Exception as e:  # never let maintenance crash the loop
                self.log(f"removing unused LDAP/OIDC accounts failed: {e}")
            await asyncio.sleep(LOG_MAINTENANCE_INTERVAL)

    # ---------------- sign-ins: last sign-in, unused LDAP/OIDC accounts ----------------
    async def load_sign_ins(self) -> None:
        """Read the sign-ins on record in the audit log (app/signins.py)."""
        try:
            await asyncio.to_thread(lambda: self.signins.load(self.read_audit()))
        except Exception as e:  # still usable: sign-ins from now on count
            self.signins.loaded = True
            self.log(f"could not read the sign-ins in the audit log: {e}")

    async def expire_external_accounts(self) -> list[User]:
        """Remove the LDAP/OIDC accounts not signed in for external_users_expire_days
        (SignIns.expired: never local accounts, admins or the last accounts). Each is
        logged and audited; returns them."""
        days = self.config.external_users_expire_days
        async with self.config_lock:
            gone = self.signins.expired(self.config.users, days)
            if not gone:
                return []
            before = self.config.users
            self.config.users = [u for u in before if not any(u is g for g in gone)]
            try:  # removing accounts can't make a valid config invalid: like the CLI's
                await asyncio.to_thread(self.store.save, self.config, validate=False)
            except OSError as e:
                self.config.users = before
                self.log(f"could not save the removal of unused LDAP/OIDC accounts: {e}")
                return []
        for u in gone:
            last = self.signins.last(u)
            seen = time.strftime("%Y-%m-%d", time.localtime(last)) if last else "none on record"
            self.log(f"{u.source.upper()} account {u.username!r} ({u.role}) removed: no sign-in "
                     f"for {days} days (last: {seen}). It is made again at their next sign-in "
                     f"while {u.source.upper()} still gives them a role.")
            self.audit("local", "user_expired",
                       f"{u.username}: {u.source}, no sign-in for {days} days (last: {seen})",
                       user="(maintenance)")
        return gone

    # ---------------- first-run setup ----------------
    @property
    def setup_code_path(self) -> str:
        return os.path.join(self.data_dir, SETUP_CODE_FILE)

    def prepare_first_run(self, environ=None) -> None:
        """While there is no account, the setup page creates the first admin. Opened by
        this machine's IP address or name it just works; through any other name (DNS
        rebinding, but also a reverse proxy or Docker's host name) it asks for a one-time
        code, which is written to setup-code.txt in the data directory — never to a log.
        With SER2NET_ADMIN_PASSWORD_FILE / _USER set the admin comes from there, and the
        page stays closed if that failed."""
        environ = os.environ if environ is None else environ
        if self.config.password_set:
            self.first_run_done()
            return
        if accounts.provision_requested(environ):
            self.setup_locked = True
            self.log("first-run setup in the browser is closed: SER2NET_ADMIN_PASSWORD_FILE / "
                     "SER2NET_ADMIN_USER is set but created no admin (see above). Fix it and "
                     "restart, or run: ser2net --set-password <name>")
            return
        self.setup_code = "-".join(secrets.token_hex(2) for _ in range(5))
        try:
            fd = os.open(self.setup_code_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.setup_code + "\n")
            where = f"asks for the one-time code in {self.setup_code_path}"
        except OSError as e:
            where = f"can't be used (could not write {self.setup_code_path}: {e})"
        self.log("no admin account yet: open the web UI by this machine's IP address or "
                 f"localhost to create one (through another host name the setup page {where}), "
                 "or run: ser2net --set-password <name>")

    def setup_code_ok(self, value: str) -> bool:
        """The one-time code, letter case, dashes and spaces aside (compared as bytes: a
        str with non-ASCII text would make compare_digest raise)."""
        def norm(s: str) -> bytes:
            return "".join(c for c in s.lower() if c.isalnum()).encode()
        return bool(self.setup_code) and hmac.compare_digest(norm(value), norm(self.setup_code))

    def first_run_done(self) -> None:
        """The first admin exists: the setup code is no longer needed."""
        self.setup_code = ""
        with _suppress(OSError):
            os.remove(self.setup_code_path)

    # ---------------- config.json changed outside ser2net ----------------
    def _outside_edit_replaced(self, info: dict) -> None:
        """ConfigStore.on_replaced (in the thread that saves): the audit record."""
        self.audit("local", "config_outside_edit_replaced",
                   f"kept as backups/{info['backup']}" if info["backup"] else "no backup")

    async def check_config_file(self, now: float | None = None) -> None:
        """Say once (all.log) when config.json was changed outside ser2net and is not in
        use: it takes a reload, or the next save replaces it. Looked at every
        CONFIG_CHECK_S."""
        now = time.monotonic() if now is None else now
        checked, noted = self._outside_edit or (0.0, -1.0)
        if now - checked < CONFIG_CHECK_S:
            return
        changed = await asyncio.to_thread(self.store.changed_on_disk)
        if changed is None or changed == noted:
            self._outside_edit = (now, noted if changed is not None else -1.0)
            return
        self._outside_edit = (now, changed)
        if changed == 0.0:
            self.log("WARNING: config.json is gone from the data directory; ser2net goes on "
                     "with the configuration in use and writes it again at the next change")
            return
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(changed))
        self.log(f"WARNING: config.json was changed outside ser2net ({when}) and is not in "
                 "use: reload it (systemctl reload ser2net, kill -HUP, ser2net --reload or "
                 "Settings → Configuration backups). A change saved here before that "
                 "replaces it (the edited file is then kept in backups/)")

    # ---------------- `ser2net --set-password` / `--sign-out-all` while running ----------------
    async def _account_requests_loop(self) -> None:
        while True:
            await asyncio.sleep(accounts.POLL_S)
            try:
                await self.handle_account_requests()
                await self.check_config_file()
            except Exception as e:  # never let a bad request end the loop
                self.log(f"command-line request failed: {e}")

    async def handle_account_requests(self) -> None:
        """Apply what the command line left in <data>/admin-requests/ (app/accounts.py)."""
        for request_id, request in await asyncio.to_thread(accounts.take_requests, self.data_dir):
            ok, message = await self._apply_account_request(request)
            await asyncio.to_thread(accounts.answer, self.data_dir, request_id, ok, message)

    async def _apply_account_request(self, request) -> tuple[bool, str]:
        if request is None:
            return False, "the request was unreadable or too old; run the command again"
        if request.get("action") == "reload":  # `ser2net --reload`
            from .reload import reload_config
            return await reload_config(self, "command line", user=accounts.ACTOR)
        async with self.config_lock:
            try:
                change = accounts.apply(self.config, request)
            except accounts.AccountError as e:
                return False, str(e)
            try:  # like the command itself: an unrelated bad setting must not block this
                await asyncio.to_thread(self.store.save, self.config, validate=False)
            except OSError as e:
                change.undo()
                return False, f"could not save config.json: {e}"
        if change.action == "sessions_revoke_all":
            self.revoked_sessions.clear()  # signed with the old key: dead anyway
        if change.first_admin:
            self.first_run_done()
        self.log(f"command line: {change.message}")
        self.audit("local", change.action, change.detail, user=accounts.ACTOR)
        return True, change.message

    def save(self) -> None:
        """Persist config atomically (validates first; raises ConfigError on bad config)."""
        self.store.save(self.config)

    async def asave(self, *, backup: bool = True) -> bool:
        """Async wrapper around save(): the atomic write does blocking file I/O plus
        two fsyncs, so run it off the event loop to avoid stalling every bridge while
        config is persisted. Raises ConfigError on invalid config, same as save(), and
        ConfigWriteError (a ConfigError) when config.json can't be written. Changes go
        through edit_config, which undoes them when this fails. backup and the result:
        ConfigStore.save."""
        try:
            return await asyncio.to_thread(self.store.save, self.config, backup=backup)
        except OSError as e:
            raise ConfigWriteError(_("Could not save config.json: {0}").format(
                e.strerror or e)) from e

    @contextlib.asynccontextmanager
    async def edit_config(self, *, keep_on_failure: bool = False, backup: bool = True):
        """Change the configuration and save it, all or nothing:

            async with state.edit_config():
                state.config.<...> = ...   # in place, under config_lock

        The block changes self.config in place (the objects the cluster, the runners and
        the requests hold stay the ones in use); raising ConfigError there refuses the
        change. When the block ends, the change is saved — not when it changed nothing
        config.json keeps — and the save runs to its end even when the request is
        cancelled meanwhile. When the block raises or the save fails, every field is put
        back as it was (the same objects, with their contents) and the error is raised:
        ConfigError (a ConfigWriteError for a failed write) for the caller to show.
        keep_on_failure: a change that is used even when it can't be saved (a sign-in's
        account, a sign-out) stays in memory, until a restart; the error is raised too.
        backup=False: only mappings start or stop (ConfigStore.save). Yields a ConfigEdit."""
        async with self.config_lock:
            snapshot = _snapshot(self.config)
            before = self.config.to_dict()
            edit = ConfigEdit()
            try:
                yield edit
            except BaseException:
                _restore(snapshot)
                raise
            if self.config.to_dict() == before:
                return
            save = asyncio.ensure_future(self.asave(backup=backup))
            cancelled = await _to_its_end(save)
            error = asyncio.CancelledError() if save.cancelled() else save.exception()
            if error is not None:
                if not keep_on_failure:
                    _restore(snapshot)
                raise error
            edit.saved, edit.backed_up = True, save.result()
            if cancelled:
                raise asyncio.CancelledError

    async def start_engine(self) -> None:
        # drop the logs and traces of mappings that no longer exist
        self.prune_mapping_logs()
        self.prune_orphan_traces()
        await self.ports.start()
        await self.supervisor.start_all(self.config)
        await self.cluster.start()  # no-op unless the LAN cluster is enabled + keyed
        await self.notifier.start()
        # run an immediate pass, then hourly, to enforce log retention
        self._maint_task = asyncio.create_task(self._log_maintenance_loop(), name="log-maintenance")
        self._requests_task = asyncio.create_task(self._account_requests_loop(),
                                                  name="account-requests")

    async def stop_engine(self) -> None:
        for task in (self._maint_task, self._requests_task):
            if task is not None:
                task.cancel()
                with _suppress(asyncio.CancelledError):
                    await task
        self._maint_task = self._requests_task = None
        self.failed_logins.flush()  # failed sign-ins counted but not recorded yet
        for records in (self.view_records, self.console_writes, self.api_auth_failures):
            records.flush()
        await self.notifier.stop()
        await self.cluster.stop()
        await self.supervisor.stop_all()
        await self.ports.stop()
