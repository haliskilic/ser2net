"""systemd's notification protocol (sd_notify), standard library only.

Under a unit with Type=notify (the optional drop-in systemd/ser2net.service.d/notify.conf)
systemd waits for READY=1 before it counts ser2net as started, and with WatchdogSec= it
restarts ser2net when the WATCHDOG=1 pings stop (they come from the event loop, so a
loop that is stuck stops them). Everything here does nothing without $NOTIFY_SOCKET, so
the default unit (Type=simple), Docker, Windows and a start by hand are not affected.
"""
from __future__ import annotations

import os
import socket
import time
from typing import Mapping, Optional


def _address(env: Mapping[str, str]) -> Optional[str]:
    addr = env.get("NOTIFY_SOCKET") or ""
    if not addr or not hasattr(socket, "AF_UNIX"):
        return None
    if addr.startswith("@"):  # an abstract socket (Linux)
        return "\0" + addr[1:]
    return addr if addr.startswith("/") else None


def notify(*fields: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Send ``fields`` ("READY=1", "STATUS=..."), one per line, to systemd. True when sent;
    False without $NOTIFY_SOCKET or when it can't be (never raises)."""
    addr = _address(os.environ if env is None else env)
    if addr is None:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall("\n".join(fields).encode("utf-8", "replace"))
    except OSError:
        return False
    return True


def ready(status: str = "", env: Optional[Mapping[str, str]] = None) -> bool:
    """ser2net has started: its mappings run and its admin UI listens."""
    return notify("READY=1", *([f"STATUS={status}"] if status else []), env=env)


def reloading(env: Optional[Mapping[str, str]] = None) -> bool:
    """ser2net reads config.json again (systemctl reload, SIGHUP); ready() follows when it
    is done (also what Type=notify-reload waits for)."""
    fields = ["RELOADING=1"]
    try:
        fields.append(f"MONOTONIC_USEC={time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000}")
    except (AttributeError, OSError):  # (no CLOCK_MONOTONIC: Windows, which has no systemd)
        pass
    return notify(*fields, env=env)


def stopping(env: Optional[Mapping[str, str]] = None) -> bool:
    return notify("STOPPING=1", env=env)


def watchdog(env: Optional[Mapping[str, str]] = None) -> bool:
    return notify("WATCHDOG=1", env=env)


def watchdog_interval(env: Optional[Mapping[str, str]] = None) -> Optional[float]:
    """How often to send WATCHDOG=1 (half of WatchdogSec=, as systemd advises), or None
    when systemd doesn't watch this process."""
    env = os.environ if env is None else env
    if _address(env) is None:
        return None
    # meant for this process: its pid, or its parent's when that is the standalone
    # binary's launcher, which runs ser2net in a child process (NotifyAccess=all)
    pid = (env.get("WATCHDOG_PID") or "").strip()
    if pid and pid not in (str(os.getpid()), str(os.getppid())):
        return None
    try:
        usec = int((env.get("WATCHDOG_USEC") or "").strip())
    except ValueError:
        return None
    return usec / 2e6 if usec > 0 else None
