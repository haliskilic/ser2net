"""Shared runtime state: config + store + supervisor + port watcher + logs.

A single AppState instance is created at startup and shared by the web layer and
the engine. Config mutations go through ``config_lock`` and are persisted
atomically before the supervisor is reconciled.

All activity (logins/logouts, serial port open/close, client connect/disconnect,
mapping start/stop/edit, HTTP requests, errors) is funneled through ``log()`` and
written to ``all.log`` next to ``config.json``. Deleting config.json + all.log
fully resets the system — those are the only two state files.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import sys
import time
from collections import deque
from contextlib import suppress as _suppress
from logging.handlers import RotatingFileHandler
from typing import Any

from . import accounts
from .config import AppConfig, ConfigStore, lock_down_dir, mapping_file, valid_mapping_id
from .engine.cluster import ClusterDiscovery
from .engine.portlist import PortWatcher
from .engine.supervisor import Supervisor
from .notify import Notifier
from .web.auth import LoginRateLimiter, RevokedSessions

# Per-mapping log retention (automatic maintenance).
LOG_MAX_BYTES = 100 * 1024 * 1024     # cap each mapping log at 100 MB
LOG_MAX_AGE_DAYS = 15                  # drop entries older than 15 days
LOG_MAINTENANCE_INTERVAL = 3600       # run maintenance hourly (and once at startup)
# Global log rotation (all.log via RotatingFileHandler; audit.log rotated manually).
ALL_LOG_MAX_BYTES = 10 * 1024 * 1024   # rotate all.log past 10 MB
ALL_LOG_BACKUPS = 5                    # keep 5 rotations (~60 MB total worst case)
AUDIT_LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate audit.log past 5 MB (one backup kept)
# first-run setup through a host name that isn't this machine's needs this file's code
SETUP_CODE_FILE = "setup-code.txt"


# control characters (incl. tab / CR / LF) that must not reach an audit record
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def write_audit_record(path: str, actor_ip: str, action: str, detail: str = "",
                       user: str = "") -> None:
    """Append one record to the audit log at `path`: one line, tab-separated: time,
    client IP, action, detail, user (the user column was added in v2.8 — older lines
    have four fields). Control characters in any field become spaces, so a crafted
    mapping name can't forge an extra record. Rotated past AUDIT_LOG_MAX_BYTES (one
    backup kept). Best effort: never raises."""
    fields = [time.strftime("%Y-%m-%d %H:%M:%S"), actor_ip, action, detail[:500], user]
    line = "\t".join(_CONTROL.sub(" ", str(f)) for f in fields)
    try:
        # size-based rotation (keep one backup) so audit.log can't grow forever
        if os.path.getsize(path) > AUDIT_LOG_MAX_BYTES:
            with _suppress(OSError):
                os.replace(path, path + ".1")
    except OSError:
        pass  # file doesn't exist yet
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _parse_log_ts(line: str) -> float | None:
    """Parse the 'YYYY-MM-DD HH:MM:SS' prefix of a log line into an epoch time."""
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, IndexError):
        return None


def _trim_log_file(path: str, cutoff_ts: float, max_bytes: int) -> bool:
    """Rewrite a log file in place keeping only lines newer than cutoff_ts and,
    if still over max_bytes, only the most recent max_bytes. Returns True if the
    file changed. Runs in a worker thread (no event-loop blocking)."""
    # newline="" so CRLF line endings (logging writes "\n" -> "\r\n" on Windows) are
    # preserved verbatim on both read and write; otherwise the read collapses them to
    # "\n", the byte-count is undercounted, and the rewritten file blows past max_bytes.
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            content = fh.read()
    except OSError:
        return False
    if not content:
        return False

    lines = content.splitlines(keepends=True)
    # 1) age filter — lines are chronological, so keep from the first recent one
    start = len(lines)
    for i, ln in enumerate(lines):
        ts = _parse_log_ts(ln)
        if ts is None or ts >= cutoff_ts:
            start = i
            break
    data = "".join(lines[start:])

    # 2) size cap — keep only the last max_bytes, trimmed to a line boundary
    encoded = data.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[-max_bytes:].decode("utf-8", errors="ignore")
        nl = text.find("\n")
        data = text[nl + 1:] if nl != -1 else text

    if data == content:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return True


class AppState:
    def __init__(self, store: ConfigStore, config: AppConfig):
        self.store = store
        self.config = config
        self.logs: deque[str] = deque(maxlen=1000)
        self.data_dir = os.path.dirname(os.path.abspath(store.path))
        # all.log lives next to config.json; per-mapping logs live in data/logs/
        self.log_path = os.path.join(self.data_dir, "all.log")
        self.logs_dir = os.path.join(self.data_dir, "logs")
        # traffic traces: the ONLY place a trace is written (see config.trace_file_name)
        self.traces_dir = os.path.join(self.data_dir, "traces")
        self._logger = self._setup_logging(self.log_path)
        self._mapping_loggers: dict[str, logging.Logger] = {}
        self.supervisor = Supervisor(logger=self.log, logger_factory=self.make_mapping_logger,
                                     trace_dir=self.traces_dir)
        self.ports = PortWatcher(interval=2.0)
        self.cluster = ClusterDiscovery(self.config, self.log)
        self.rate_limiter = LoginRateLimiter()
        self.revoked_sessions = RevokedSessions()  # sessions ended by a logout
        # uploaded imports waiting for the user to confirm the preview: token -> item
        self.pending_imports: dict[str, dict] = {}
        self.notifier = Notifier(self)
        self.config_lock = asyncio.Lock()
        self.started_at = time.time()
        # the admin UI listener's connection guard (runtime.py; None in tests without one)
        self.admin_guard: Any = None
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

    @staticmethod
    def _setup_logging(log_path: str) -> logging.Logger:
        logger = logging.getLogger("ser2net")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.propagate = False
        fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            # rotate all.log so the global activity/audit trail can't grow without
            # bound (per-mapping logs are trimmed separately by run_log_maintenance).
            fileh = RotatingFileHandler(
                log_path, maxBytes=ALL_LOG_MAX_BYTES, backupCount=ALL_LOG_BACKUPS,
                encoding="utf-8")
            fileh.setFormatter(fmt)
            logger.addHandler(fileh)
        except OSError as e:
            logger.warning(f"could not open log file {log_path}: {e}")
        return logger

    def log(self, msg: str) -> None:
        self._logger.info(msg)
        self.logs.append(msg)

    @property
    def audit_path(self) -> str:
        return os.path.join(self.data_dir, "audit.log")

    def audit(self, actor_ip: str, action: str, detail: str = "", user: str = "") -> None:
        """Append a config-change audit record (write_audit_record), separate from
        all.log, and note it in all.log."""
        write_audit_record(self.audit_path, actor_ip, action, detail, user)
        who = f" by {user}" if user else ""
        self._logger.info(_CONTROL.sub(" ", f"AUDIT {actor_ip} {action} {detail}{who}").rstrip())

    def read_audit(self) -> list[dict]:
        """Every audit record (audit.log.1 + audit.log), newest first. Blocking file
        I/O: call via asyncio.to_thread."""
        records = []
        for path in (self.audit_path + ".1", self.audit_path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
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
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
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

    def read_mapping_log(self, mapping_id: str, limit: int = 1000,
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
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-limit:][::-1]

    def delete_mapping_log(self, mapping_id: str) -> None:
        lg = self._mapping_loggers.pop(mapping_id, None)
        if lg is not None:
            for h in list(lg.handlers):
                with _suppress(Exception):
                    h.close()
                lg.removeHandler(h)
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
        paths = [os.path.join(self.traces_dir, n) for n in names]
        return sorted((p for p in paths if os.path.isfile(p)),
                      key=lambda p: os.path.getmtime(p), reverse=True)

    def delete_mapping_traces(self, mapping_id: str) -> None:
        for path in self.trace_files(mapping_id):
            with _suppress(OSError):
                os.remove(path)

    def prune_mapping_logs(self) -> None:
        """Delete per-mapping log files whose mapping no longer exists. This keeps
        the 'delete config.json + all.log to reset' model: a missing/empty config
        leaves no live mappings, so all per-mapping logs are pruned on next start."""
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
        """Trim every per-mapping log: drop entries older than LOG_MAX_AGE_DAYS and
        cap each file at LOG_MAX_BYTES (keeping the most recent portion). The file's
        logging handler is detached while its file is rewritten, then reattached."""
        cutoff = time.time() - LOG_MAX_AGE_DAYS * 86400
        try:
            files = [f for f in os.listdir(self.logs_dir) if f.endswith(".log")]
        except OSError:
            return
        for fn in files:
            mid = fn[:-4]
            path = os.path.join(self.logs_dir, fn)
            lg = self._mapping_loggers.get(mid)
            if lg is not None:  # detach so the open handler doesn't fight the rewrite
                for h in list(lg.handlers):
                    with _suppress(Exception):
                        h.close()
                    lg.removeHandler(h)
            try:
                changed = await asyncio.to_thread(_trim_log_file, path, cutoff, LOG_MAX_BYTES)
                if changed:
                    self.log(f"log maintenance: trimmed {fn} (>15d / >100MB)")
            except Exception as e:  # never let maintenance crash the loop
                self.log(f"log maintenance error on {fn}: {e}")
            finally:
                if lg is not None:
                    self._attach_mapping_handler(lg, mid)

    async def _log_maintenance_loop(self) -> None:
        while True:
            await self.run_log_maintenance()
            await asyncio.sleep(LOG_MAINTENANCE_INTERVAL)

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
        def norm(s: str) -> str:
            return "".join(c for c in s.lower() if c.isalnum())
        return bool(self.setup_code) and hmac.compare_digest(norm(value), norm(self.setup_code))

    def first_run_done(self) -> None:
        """The first admin exists: the setup code is no longer needed."""
        self.setup_code = ""
        with _suppress(OSError):
            os.remove(self.setup_code_path)

    # ---------------- `ser2net --set-password` / `--sign-out-all` while running ----------------
    async def _account_requests_loop(self) -> None:
        while True:
            await asyncio.sleep(accounts.POLL_S)
            try:
                await self.handle_account_requests()
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

    async def asave(self) -> None:
        """Async wrapper around save(): the atomic write does blocking file I/O plus
        two fsyncs, so run it off the event loop to avoid stalling every bridge while
        config is persisted. Raises ConfigError on invalid config, same as save()."""
        await asyncio.to_thread(self.store.save, self.config)

    async def start_engine(self) -> None:
        self.prune_mapping_logs()  # drop logs for mappings that no longer exist
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
        await self.notifier.stop()
        await self.cluster.stop()
        await self.supervisor.stop_all()
        await self.ports.stop()
