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
import logging
import os
import sys
import time
from collections import deque
from contextlib import suppress as _suppress

from .config import AppConfig, ConfigStore
from .engine.portlist import PortWatcher
from .engine.supervisor import Supervisor
from .web.auth import LoginRateLimiter

# Per-mapping log retention (automatic maintenance).
LOG_MAX_BYTES = 100 * 1024 * 1024     # cap each mapping log at 100 MB
LOG_MAX_AGE_DAYS = 15                  # drop entries older than 15 days
LOG_MAINTENANCE_INTERVAL = 3600       # run maintenance hourly (and once at startup)


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
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
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
    with open(tmp, "w", encoding="utf-8") as fh:
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
        self._logger = self._setup_logging(self.log_path)
        self._mapping_loggers: dict[str, logging.Logger] = {}
        self.supervisor = Supervisor(logger=self.log, logger_factory=self.make_mapping_logger)
        self.ports = PortWatcher(interval=2.0)
        self.rate_limiter = LoginRateLimiter()
        self.config_lock = asyncio.Lock()
        self.started_at = time.time()
        self._maint_task: asyncio.Task | None = None
        # per-mapping logs can contain traffic; keep the logs dir private too
        if os.name == "posix":
            try:
                os.makedirs(self.logs_dir, exist_ok=True)
                os.chmod(self.logs_dir, 0o700)
            except OSError:
                pass

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
            fileh = logging.FileHandler(log_path, encoding="utf-8")
            fileh.setFormatter(fmt)
            logger.addHandler(fileh)
        except OSError as e:
            logger.warning(f"could not open log file {log_path}: {e}")
        return logger

    def log(self, msg: str) -> None:
        self._logger.info(msg)
        self.logs.append(msg)

    def audit(self, actor_ip: str, action: str, detail: str = "") -> None:
        """Append a config-change audit record (who/what), separate from all.log."""
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{actor_ip}\t{action}\t{detail}"
        try:
            with open(os.path.join(self.data_dir, "audit.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        self._logger.info(f"AUDIT {actor_ip} {action} {detail}".rstrip())

    # ---------------- per-mapping logs ----------------
    def mapping_log_path(self, mapping_id: str) -> str:
        return os.path.join(self.logs_dir, f"{mapping_id}.log")

    def _attach_mapping_handler(self, lg: logging.Logger, mapping_id: str) -> None:
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            fh = logging.FileHandler(self.mapping_log_path(mapping_id), encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
            lg.addHandler(fh)
        except OSError as e:
            self.log(f"could not open mapping log for {mapping_id}: {e}")

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

    def read_mapping_log(self, mapping_id: str, limit: int = 1000) -> list[str]:
        """Return the mapping's log lines, newest first (most recent at index 0)."""
        path = self.mapping_log_path(mapping_id)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return []
        return lines[-limit:][::-1]

    def delete_mapping_log(self, mapping_id: str) -> None:
        lg = self._mapping_loggers.pop(mapping_id, None)
        if lg is not None:
            for h in list(lg.handlers):
                with _suppress(Exception):
                    h.close()
                lg.removeHandler(h)
        with _suppress(OSError):  # log file may never have been created (FileNotFoundError)
            os.remove(self.mapping_log_path(mapping_id))

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

    def save(self) -> None:
        """Persist config atomically (validates first; raises ConfigError on bad config)."""
        self.store.save(self.config)

    async def start_engine(self) -> None:
        self.prune_mapping_logs()  # drop logs for mappings that no longer exist
        await self.ports.start()
        await self.supervisor.start_all(self.config)
        # run an immediate pass, then hourly, to enforce log retention
        self._maint_task = asyncio.create_task(self._log_maintenance_loop(), name="log-maintenance")

    async def stop_engine(self) -> None:
        if self._maint_task is not None:
            self._maint_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._maint_task
            self._maint_task = None
        await self.supervisor.stop_all()
        await self.ports.stop()
