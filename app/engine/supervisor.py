"""Supervisor: owns the set of MappingRunners and applies config changes.

The web layer mutates + persists the AppConfig, then calls this to reconcile the
running bridges. Editing a mapping at runtime stops the old runner (awaiting full
teardown so the listening socket is released) before starting the new one, which
avoids address-in-use / port races.

A mapping whose listener can't take its address (netguard.BindError: the port is in use,
or the address is not on this machine yet, as at boot before DHCP) stays in state "error"
and is started again after a backoff (up to BIND_RETRY_MAX_S between tries), for as long
as it is meant to run: until it starts, or it is applied, stopped or removed again. Such a
start opens nothing before its bind (bridge.MappingRunner.start), so a retry neither
pulses the serial lines nor reconnects to a broker.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable, Optional, Protocol

from ..config import AppConfig, MappingConfig
from . import portlist, serial_io
from .bridge import MappingRunner, RunnerStatus
from .netguard import BindError
from .serial_link import Backoff

# stop_all(): the runners stop side by side, for at most this long together; one that
# takes longer is left to finish on its own (and logged), so it can't hold up the rest
STOP_ALL_TIMEOUT_S = 15.0
# a listener that could not bind is tried again after the serial reconnect's backoff
# (0.5 s doubling), at most this many seconds apart
BIND_RETRY_MAX_S = 30.0


class Runner(Protocol):
    """What the supervisor and the web layer use of a mapping's runner: the transparent
    bridge (bridge.MappingRunner) or the Modbus gateway (modbus_gateway.ModbusGatewayRunner)."""

    mapping: MappingConfig
    status: RunnerStatus

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    # browser console (what the device sends) and live hex view (both directions)
    def add_monitor(self, mon: Any) -> None: ...

    def discard_monitor(self, mon: Any) -> None: ...

    def add_traffic_monitor(self, mon: Any) -> None: ...

    def discard_traffic_monitor(self, mon: Any) -> None: ...

    # the browser console typing to the device; False when nothing was written
    async def serial_write(self, data: bytes) -> bool: ...


def make_runner(mapping: MappingConfig, logger, trace_dir: Optional[str] = None) -> Runner:
    """Pick the runner for a mapping: the Modbus RTU<->TCP gateway when the network
    protocol is 'modbus', otherwise the transparent serial<->network bridge."""
    if mapping.kind == "net" and mapping.network.protocol == "modbus":
        from .modbus_gateway import ModbusGatewayRunner
        return ModbusGatewayRunner(mapping, logger=logger, trace_dir=trace_dir)
    return MappingRunner(mapping, logger=logger, trace_dir=trace_dir)


class _Unbuildable:
    """Stands in for a mapping whose runner can't be built from its settings (a hand-edited
    value of the wrong type), so its status says why and the rest keeps running; starting
    it fails with that error."""

    def __init__(self, error: Exception):
        self.error = f"its settings can't be used: {error}"
        self.status = RunnerStatus(state="stopped")

    async def start(self) -> None:
        raise RuntimeError(self.error)

    async def stop(self) -> None:
        pass

    # the console attaches to any runner: there is nothing to watch or write to here
    def add_monitor(self, mon) -> None:
        pass

    def discard_monitor(self, mon) -> None:
        pass

    async def serial_write(self, data: bytes) -> None:
        pass


def _build_runner(mapping: MappingConfig, logger, trace_dir: Optional[str]):
    try:
        return make_runner(mapping, logger, trace_dir)
    except Exception as e:  # only this mapping is down
        return _Unbuildable(e)


def _consume_error(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # a runner that failed to stop: nothing more to do about it


def _name(runner: Any) -> str:
    mapping = getattr(runner, "mapping", None)
    return getattr(mapping, "name", None) or "?"


class Supervisor:
    def __init__(self, logger: Optional[Callable[[str], None]] = None,
                 logger_factory: Optional[Callable[[MappingConfig], Callable[[str], None]]] = None,
                 trace_dir: Optional[str] = None):
        self._runners: dict[str, Runner] = {}
        self.trace_dir = trace_dir  # data/traces: the only place traces are written
        self._order: list[str] = []
        self.log = logger or (lambda m: None)
        # per-mapping logger: defaults to the global logger with a "[name]" prefix
        self._logger_factory = logger_factory or (
            lambda mapping: (lambda msg: self.log(f"[{mapping.name}] {msg}"))
        )
        self._lock = asyncio.Lock()
        self._stopping: set[asyncio.Future] = set()  # stop_all()'s runners still stopping
        self._retries: dict[str, asyncio.Task] = {}  # mapping id -> its bind retry
        self._ports: Optional[set[str]] = None  # the serial ports last seen (ports_changed)

    # ---------------- bulk ----------------
    async def start_all(self, config: AppConfig) -> None:
        self._order = [m.id for m in config.mappings]
        for m in config.mappings:
            if m.enabled:
                await self.apply_mapping(m)
            else:
                self._ensure_stopped_placeholder(m)

    async def stop_all(self) -> None:
        """Stop every runner, side by side, waiting at most STOP_ALL_TIMEOUT_S: a runner
        still stopping then (a device or broker that doesn't answer) is logged and left to
        finish in the background, instead of keeping the others running (an import or a
        restore stops all, then starts the new set) or the process from exiting."""
        retries = [asyncio.ensure_future(self._cancel_retry(mid)) for mid in list(self._retries)]
        if retries:  # (an attempt under way ends first: a bind, nothing that can hang)
            await asyncio.wait(retries, timeout=STOP_ALL_TIMEOUT_S)
        async with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        if not runners:
            return
        stops = {asyncio.ensure_future(r.stop()): r for r in runners}
        for task in stops:
            self._stopping.add(task)  # (a task left running must not be garbage-collected)
            task.add_done_callback(self._stopping.discard)
            task.add_done_callback(_consume_error)
        _done, pending = await asyncio.wait(stops, timeout=STOP_ALL_TIMEOUT_S)
        if pending:
            names = ", ".join(sorted(_name(stops[t]) for t in pending))
            self.log(f"stopping: {len(pending)} mapping(s) did not stop within "
                     f"{STOP_ALL_TIMEOUT_S:g} s, left to finish on their own: {names}")

    # ---------------- per-mapping ----------------
    def _ensure_stopped_placeholder(self, mapping: MappingConfig) -> None:
        """Register a non-running runner so status is queryable for disabled mappings."""
        if mapping.id not in self._runners:
            r = _build_runner(mapping, self._logger_factory(mapping), self.trace_dir)
            r.status.state = "stopped"
            self._runners[mapping.id] = r
            if mapping.id not in self._order:
                self._order.append(mapping.id)

    async def apply_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        """Create/replace and (if enabled) start the runner for a mapping. A start that
        fails because the listener can't bind is tried again later (_retry)."""
        await self._cancel_retry(mapping.id)
        async with self._lock:
            old = self._runners.pop(mapping.id, None)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.stop()

        runner = _build_runner(mapping, self._logger_factory(mapping), self.trace_dir)
        if mapping.id not in self._order:
            self._order.append(mapping.id)
        async with self._lock:
            self._runners[mapping.id] = runner

        if not mapping.enabled:
            runner.status.state = "stopped"
            return True, "disabled"
        try:
            # a stored URL port fails the start once, instead of retrying it forever
            for side in mapping.serial_sides():
                serial_io.refuse_url(side.port)
            await runner.start()
            return True, "started"
        except Exception as e:
            runner.status.state = "error"
            runner.status.last_error = str(e)
            if isinstance(e, BindError):
                self.log(f"[{mapping.name}] start failed: {e} — trying again until the "
                         f"address can be used (every {BIND_RETRY_MAX_S:g} s at most)")
                self._retry_later(mapping, runner, str(e))
            else:
                self.log(f"[{mapping.name}] start failed: {e}")
            return False, str(e)

    def _retry_later(self, mapping: MappingConfig, runner: Runner, error: str) -> None:
        backoff = Backoff(cap=BIND_RETRY_MAX_S)
        pause = backoff.pause()
        runner.status.retry_at = time.monotonic() + pause  # (the status says so at once)
        self._retries[mapping.id] = asyncio.create_task(
            self._retry(mapping, runner, error, backoff, pause),
            name=f"bind-retry:{mapping.name}")

    async def _retry(self, mapping: MappingConfig, runner: Runner, error: str,
                     backoff: Backoff, pause: float) -> None:
        """Start ``runner`` again, with a backoff, while its listener can't bind. Logs a
        changed error and the start that worked; any other error ends the retries."""
        st = runner.status
        try:
            while True:
                st.retry_at = time.monotonic() + pause
                await asyncio.sleep(pause)
                st.retry_at = time.monotonic()  # trying now
                # asyncio.wait, which cancels nothing: a stop or a new apply that cancels
                # the retry waits for an attempt under way to end, then stops what it started
                attempt = asyncio.ensure_future(runner.start())
                attempt.add_done_callback(_consume_error)
                try:
                    await asyncio.wait((attempt,))
                except asyncio.CancelledError:
                    await asyncio.wait((attempt,))
                    raise
                e = attempt.exception()
                if e is None:
                    self.log(f"[{mapping.name}] started after {backoff.attempt} "
                             f"retr{'y' if backoff.attempt == 1 else 'ies'}")
                    return
                st.state, st.last_error = "error", str(e)
                if not isinstance(e, BindError):
                    self.log(f"[{mapping.name}] start failed: {e}")
                    return
                if str(e) != error:
                    error = str(e)
                    self.log(f"[{mapping.name}] start failed again: {e}")
                pause = backoff.pause()
        finally:
            st.retry_at = 0.0
            if self._retries.get(mapping.id) is asyncio.current_task():
                del self._retries[mapping.id]

    async def _cancel_retry(self, mapping_id: str) -> None:
        """End a mapping's bind retry (and an attempt under way) before it is applied,
        stopped or removed: a retry left running would undo a Stop."""
        task = self._retries.pop(mapping_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def start_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        return await self.apply_mapping(mapping)

    async def stop_mapping(self, mapping_id: str) -> None:
        await self._cancel_retry(mapping_id)
        async with self._lock:
            runner = self._runners.get(mapping_id)
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.stop()

    async def restart_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        await self.stop_mapping(mapping.id)
        return await self.apply_mapping(mapping)

    async def remove_mapping(self, mapping_id: str) -> None:
        await self._cancel_retry(mapping_id)
        async with self._lock:
            runner = self._runners.pop(mapping_id, None)
        if mapping_id in self._order:
            self._order.remove(mapping_id)
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.stop()

    # ---------------- status ----------------
    def get_runner(self, mapping_id: str) -> Optional[Runner]:
        return self._runners.get(mapping_id)

    def status(self, mapping_id: str) -> Optional[dict]:
        r = self._runners.get(mapping_id)
        return r.status.as_dict() if r else None

    def all_status(self) -> dict[str, dict]:
        # iterate a snapshot so a concurrent apply/remove can't change the dict
        # mid-iteration (defensive; also future-proofs against an async as_dict)
        return {mid: r.status.as_dict() for mid, r in list(self._runners.items())}

    def ports_changed(self, ports: list[dict]) -> None:
        """PortWatcher's on_change: when a serial port appeared, the mappings waiting to
        open theirs again try now (a re-plugged adapter is used within the watcher's poll,
        not after up to 10 s of backoff)."""
        names = {n for p in ports for n in portlist.names(p)}
        new, self._ports = (names - self._ports if self._ports is not None else names), names
        if not new:
            return
        for r in list(self._runners.values()):
            if r.status.state in ("device-missing", "reconnecting"):
                wake = getattr(r, "wake_serial", None)
                if wake is not None:
                    wake()

    def devices_in_use(self) -> set[str]:
        """The serial devices that running mappings hold open (for the Ports page): one per
        mapping, both ports of a serial-to-serial bridge."""
        out: set[str] = set()
        for r in list(self._runners.values()):
            if r.status.state == "running":
                out.update(d for d in r.status.devices if d)
        return out
