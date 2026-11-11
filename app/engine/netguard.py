"""The process's open files: their limit, how many are in use, and what the mappings need.

Every serial port, listener, client connection, log and trace file and MQTT connection
holds a file descriptor, and a process may have at most its soft RLIMIT_NOFILE open at
once: 1024 under systemd unless the unit raises it. Past it, whatever opens the next
file fails, wherever that is (a serial reopen, a client, a config save). So:
  - ``raise_fd_limit`` lifts the soft limit toward the hard one at start-up (at most
    FD_LIMIT_TARGET), with prlimit() on Linux: the shipped unit's system call filter
    denies setrlimit() but allows prlimit64. Nothing in the process waits with select()
    any more (serial_compat, mqtt_pub.PollSelect), so descriptors above 1024 work;
  - ``fd_budget_warning`` says when the configured mappings need about as many as the
    limit allows;
  - ``open_fds`` / ``fd_limit`` are what /metrics reports.
Windows has no such limit for sockets and serial ports (they are handles): each of these
answers None or "" there.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

# the soft open-file limit raise_fd_limit aims for: far above what a few hundred
# mappings need, without handing child processes a huge limit
FD_LIMIT_TARGET = 65536
# what a process holds open before any mapping: stdio, all.log, the event loop, the admin
# listener and its connections, the executor's threads' files
FD_BASE = 40


class LogEvery:
    """A log line that can repeat many times a second (dropped data, a UDP peer that keeps
    changing, refused connections): written at most once per ``interval`` seconds, saying
    how many were held back since the last one."""

    def __init__(self, log: Callable[[str], None], interval: float = 60.0) -> None:
        self._log = log
        self._interval = interval
        self._next = 0.0
        self._held = 0

    def __call__(self, msg: str) -> None:
        now = time.monotonic()
        if now < self._next:
            self._held += 1
            return
        if self._held:
            msg += f" ({self._held} more since the last such line)"
        self._held = 0
        self._next = now + self._interval
        self._log(msg)


def _resource() -> Any:
    try:
        import resource
    except ImportError:  # Windows
        return None
    return resource


def fd_limit() -> Optional[int]:
    """The soft open-file limit (None: unlimited, or Windows)."""
    resource = _resource()
    if resource is None:
        return None
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return None if soft == resource.RLIM_INFINITY else int(soft)


def open_fds() -> Optional[int]:
    """How many files this process has open (None where that can't be counted cheaply)."""
    if os.name != "posix":
        return None
    for folder in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(folder)) - 1  # less the one listdir() itself opened
        except OSError:
            continue
    return None


def _shown(value: int, resource: Any) -> str:
    return "unlimited" if value == resource.RLIM_INFINITY else str(value)


def raise_fd_limit(target: int = FD_LIMIT_TARGET) -> str:
    """Raise the soft open-file limit toward the hard limit, at most to ``target`` (never
    lower it). Returns the line for the start-up log ("" on Windows)."""
    resource = _resource()
    if resource is None:
        return ""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = target if hard == resource.RLIM_INFINITY else min(hard, target)
    if soft == resource.RLIM_INFINITY or soft >= want:
        return f"open files: limit {_shown(soft, resource)}"
    try:
        if hasattr(resource, "prlimit"):  # Linux: prlimit64, never the setrlimit() call
            resource.prlimit(0, resource.RLIMIT_NOFILE, (want, hard))
        else:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (OSError, ValueError) as e:
        return f"open files: limit {soft} (raising it to {want} failed: {e})"
    return f"open files: limit raised from {soft} to {want} (hard limit {_shown(hard, resource)})"


def fd_estimate(config: Any) -> int:
    """About how many files the configured mappings hold open: per mapping its log file
    (disabled ones too), and per enabled one its serial port(s), its socket (a listener, a
    UDP socket or the connection out), each client it admits, its trace file and an MQTT
    connection (its socket and paho's wake-up pair)."""
    need = FD_BASE
    for m in config.mappings:
        need += 1
        if not m.enabled:
            continue
        need += len(m.serial_sides())
        need += 1 if m.options.trace_both else 0
        need += 3 if m.mqtt.enabled else 0
        if m.kind == "net":
            need += 1 + (m.network.max_connections if m.network.mode == "server" else 0)
    return need


def fd_budget_warning(config: Any, limit: Optional[int] = None) -> str:
    """A start-up warning when the mappings need over 80% of the open-file limit ("" when
    they fit, or there is no such limit)."""
    limit = fd_limit() if limit is None else limit
    if limit is None:
        return ""
    need = fd_estimate(config)
    if need <= limit * 0.8:
        return ""
    return (f"WARNING: the {len(config.mappings)} mappings need about {need} open files, "
            f"close to this process's limit of {limit}: beyond it serial ports, clients and "
            "files fail to open. Raise the limit (LimitNOFILE= in the systemd unit, "
            "--ulimit nofile= for Docker) or split the mappings over more instances.")
