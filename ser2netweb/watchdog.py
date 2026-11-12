"""Event loop watchdog: a stuck event loop ends the process, so it is started again.

Every bridge, the Modbus gateway, MQTT, the cluster and the web UI run on one asyncio
event loop. When a call blocks it (a write to a device, file or console that never
returns), all of them stop, but the process lives on: systemd, Docker and the Windows
Service Control Manager only act when it exits, a failing /healthz only marks a container
"unhealthy", and paho-mqtt's own thread keeps the MQTT sessions alive, so the 'online'
status and the Sparkplug death certificate are never sent and SCADA sees stale values as
live ones.

A heartbeat task on the loop notes the time every BEAT_S seconds. A daemon thread checks
it: once the loop has missed its heartbeat for ``limit`` seconds (SER2NETWEB_LOOP_WATCHDOG,
default DEFAULT_LIMIT_S, 0 = off), it writes every thread's stack (faulthandler) to
<data>/hang.log, which names the call that blocks, and ends the process with EXIT_CODE;
the service manager (restart policy) starts it again, and the next start reports it.

Why 90 s: a loop that is busy, however much, still runs its heartbeat within about a
second, because its work is cut into short callbacks; only a loop that never gets back to
them misses it. The longest legitimate holds known are seconds (a log write under heavy
disk pressure, swapping on a small board), so 90 s is an order of magnitude more, and
above what a Docker health check (3 tries 30 s apart) or a Kubernetes liveness probe
waits. /healthz itself stays a plain liveness answer ("ok" while the loop runs), so a
slow but working instance is never reported unhealthy for being busy.

The thread measures with time.monotonic() and time.sleep() (not Event.wait). When its own
sleep took much longer than asked, the whole process was frozen (the machine slept, the
container was paused, a VM was suspended) and the loop could not have run either: it
starts measuring again instead of ending a process that is fine. A loop that keeps
running but spins (a callback that reschedules itself at once) still beats and is not
caught; neither is a stall in native code that holds Python's global interpreter lock
(it looks like a frozen process). Standard library only.
"""
from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import sys
import threading
import time
from typing import Callable, Mapping, Optional

from . import clock, envvars, sdnotify

ENV = "SER2NETWEB_LOOP_WATCHDOG"  # (or SER2NET_LOOP_WATCHDOG, its 2.9.x name)
DEFAULT_LIMIT_S = 90.0
BEAT_S = 1.0         # the heartbeat's interval on the loop
CHECK_S = 1.0        # how often the thread looks at it
FROZEN_S = 5.0       # the thread's sleep took this much longer: the process was frozen
SLOW_BEAT_S = 2.0    # a heartbeat this late is logged (at most once a minute)
EXIT_CODE = 70       # EX_SOFTWARE
EXIT_AFTER_S = 10.0  # the exit comes this long after the limit at the latest
HANG_LOG = "hang.log"


def limit_from_env(env: Optional[Mapping[str, str]] = None) -> tuple[float, str]:
    """(seconds, a note for the start-up log or ""): SER2NETWEB_LOOP_WATCHDOG, 0 = off."""
    raw = envvars.text(ENV, env)
    if not raw:
        return DEFAULT_LIMIT_S, ""
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not 0 <= value <= 86400:  # (NaN too)
        return DEFAULT_LIMIT_S, (f"WARNING: {envvars.source(ENV, env)}={raw!r} is not a "
                                 f"number of seconds "
                                 f"(0 = off, at most 86400) — the event loop watchdog uses "
                                 f"{DEFAULT_LIMIT_S:g} s")
    return value, ""


def previous_hang(data_dir: str) -> str:
    """When the previous run was ended by the watchdog (hang.log is there): its hang.log is
    kept as hang.log.1 and the start-up log line about it is returned, else ""."""
    path = os.path.join(data_dir, HANG_LOG)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
        os.replace(path, path + ".1")
    except OSError:
        return ""
    return (f"WARNING: the previous run was ended by the event loop watchdog ({first}) — "
            f"the threads' stacks are in {path}.1")


class LoopWatchdog:
    """The heartbeat (a task on the loop) and the thread that watches it. ``lag`` is how
    late the last heartbeat was (/metrics), ``slow_beats`` how many were SLOW_BEAT_S late."""

    def __init__(self, data_dir: str, log: Callable[[str], None], limit_s: float,
                 exit_process: Callable[[int], None] = os._exit) -> None:
        self.data_dir = data_dir
        self.log = log
        self.limit_s = limit_s
        self.lag = 0.0
        self.slow_beats = 0
        self._exit = exit_process
        self.beat = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[asyncio.Task] = None
        self._said = 0.0   # time.monotonic() of the last "slow heartbeat" line
        self._ping_s = sdnotify.watchdog_interval()
        self._pinged = 0.0

    @property
    def hang_log(self) -> str:
        return os.path.join(self.data_dir, HANG_LOG)

    def start(self) -> None:
        """Begin the heartbeat (on the running loop) and, with a limit, the watching thread."""
        self.beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heartbeat(),
                                                            name="loop-heartbeat")
        if self.limit_s > 0:
            self._thread = threading.Thread(target=self._watch, name="loop-watchdog",
                                            daemon=True)
            self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _heartbeat(self) -> None:
        try:
            await self._beats()
        finally:  # cancelled: the loop ends (also on an error before stop()), so does the thread
            self._stop.set()

    async def _beats(self) -> None:
        while True:
            asked = time.monotonic()
            await asyncio.sleep(BEAT_S)
            now = time.monotonic()
            self.beat = now
            self.lag = max(0.0, now - asked - BEAT_S)
            if self.lag >= SLOW_BEAT_S:
                self.slow_beats += 1
                if now - self._said >= 60:
                    self._said = now
                    self.log(f"event loop: held up for {self.lag:.1f} s (the mappings and "
                             "the web UI waited meanwhile)")
            if self._ping_s is not None and now - self._pinged >= self._ping_s:
                self._pinged = now
                sdnotify.watchdog()

    def _watch(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            time.sleep(CHECK_S)
            now = time.monotonic()
            if now - last > CHECK_S + FROZEN_S:
                self.beat = max(self.beat, now)  # the process was frozen: measure anew
            last = now
            if now - self.beat > self.limit_s and not self._stop.is_set():
                self._fire(now - self.beat)
                return

    def _fire(self, stuck: float) -> None:
        """Write every thread's stack to hang.log, then end the process (after
        EXIT_AFTER_S at the latest, should writing block too)."""
        backstop = threading.Timer(EXIT_AFTER_S, self._exit, (EXIT_CODE,))
        backstop.daemon = True
        backstop.start()
        line = (f"{clock.stamp()} the event loop did not run for "
                f"{stuck:.0f} s (limit {self.limit_s:g} s, {ENV})")
        with contextlib.suppress(Exception):
            with open(self.hang_log, "w", encoding="utf-8") as fh:
                fh.write(line + "\n\n")
                fh.flush()
                faulthandler.dump_traceback(file=fh, all_threads=True)
        with contextlib.suppress(Exception):
            # straight to the file and to stderr: the log's own handlers may be what blocks
            with open(os.path.join(self.data_dir, "all.log"), "a", encoding="utf-8") as fh:
                fh.write(f"{line} — ser2net exits ({EXIT_CODE}) to be started again; the "
                         f"threads' stacks are in {self.hang_log}\n")
        with contextlib.suppress(Exception):
            print(f"ser2net: {line} — exiting; see {self.hang_log}", file=sys.stderr, flush=True)
        self._exit(EXIT_CODE)


def start(data_dir: str, log: Callable[[str], None],
          env: Optional[Mapping[str, str]] = None) -> LoopWatchdog:
    """The watchdog for the running loop, started; its start-up line is logged."""
    limit, note = limit_from_env(env)
    if note:
        log(note)
    dog = LoopWatchdog(data_dir, log, limit)
    dog.start()
    if limit > 0:
        log(f"event loop watchdog: on — ser2net exits (to be started again) when its event "
            f"loop does not run for {limit:g} s ({ENV}=0: off)")
    else:
        log(f"event loop watchdog: off ({ENV}=0)")
    return dog
