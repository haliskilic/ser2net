"""Supervisor: owns the set of MappingRunners and applies config changes.

The web layer mutates + persists the AppConfig, then calls this to reconcile the
running bridges. Editing a mapping at runtime stops the old runner (awaiting full
teardown so the listening socket is released) before starting the new one, which
avoids address-in-use / port races.

The operations on one mapping (apply, restart, stop, remove, and its part of start_all /
stop_all) take turns: each runs to its end before the next one for that mapping begins, so
two of them can never build two runners, one of which nobody tracks any more while it
keeps the port and the serial device. Operations on different mappings still run side by
side. Given a `lookup`, an operation reads the mapping's config when its turn comes, so one
that waited (or a copy a request read earlier) can't undo a later change.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable, Optional, Protocol

from ..config import AppConfig, MappingConfig
from . import serial_io
from .bridge import MappingRunner, RunnerStatus, remove_files

# a runner in these states runs: the serial side retries a missing device on its own
ALIVE_STATES = ("running", "reconnecting", "device-missing")


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


class Supervisor:
    def __init__(self, logger: Optional[Callable[[str], None]] = None,
                 logger_factory: Optional[Callable[[MappingConfig], Callable[[str], None]]] = None,
                 trace_dir: Optional[str] = None,
                 lookup: Optional[Callable[[str], Optional[MappingConfig]]] = None):
        self._runners: dict[str, Runner] = {}
        self.trace_dir = trace_dir  # data/traces: the only place traces are written
        self._order: list[str] = []
        self.log = logger or (lambda m: None)
        # per-mapping logger: defaults to the global logger with a "[name]" prefix
        self._logger_factory = logger_factory or (
            lambda mapping: (lambda msg: self.log(f"[{mapping.name}] {msg}"))
        )
        # a mapping's config now, by id (AppState: config.get_mapping); None: as passed
        self._lookup = lookup
        # each mapping's turn: id -> [lock, operations holding or waiting for it]
        self._turns: dict[str, list] = {}

    @contextlib.asynccontextmanager
    async def _turn(self, mapping_id: str):
        """Wait for the mapping's turn and hold it (see the module docstring). Not
        re-entrant: the public methods take it, the _-helpers they share don't."""
        entry = self._turns.get(mapping_id)
        if entry is None:
            entry = self._turns[mapping_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:  # nobody holds or waits for it
                del self._turns[mapping_id]

    def _current(self, mapping: MappingConfig) -> Optional[MappingConfig]:
        """The mapping's config when its turn has come: None once it was deleted."""
        return mapping if self._lookup is None else self._lookup(mapping.id)

    # ---------------- bulk ----------------
    async def start_all(self, config: AppConfig) -> None:
        self._order = [m.id for m in config.mappings]
        for m in list(config.mappings):
            async with self._turn(m.id):
                current = self._current(m)
                if current is None:
                    continue  # deleted meanwhile
                if current.enabled:
                    await self._apply(current.id, current)
                else:
                    self._ensure_stopped_placeholder(current)

    async def stop_all(self) -> None:
        while self._runners:  # also one an operation under way adds meanwhile
            mapping_id = next(iter(self._runners))
            async with self._turn(mapping_id):
                runner = self._runners.pop(mapping_id, None)
                if runner is not None:
                    await self._stop_runner(runner)

    # ---------------- per-mapping ----------------
    def _ensure_stopped_placeholder(self, mapping: MappingConfig) -> None:
        """Register a non-running runner so status is queryable for disabled mappings."""
        if mapping.id not in self._runners:
            r = _build_runner(mapping, self._logger_factory(mapping), self.trace_dir)
            r.status.state = "stopped"
            self._runners[mapping.id] = r
            if mapping.id not in self._order:
                self._order.append(mapping.id)

    async def _stop_runner(self, runner: Runner) -> None:
        """Stop a runner to its end. A failure is logged (that runner may still hold its
        port or device), not raised: the operation goes on."""
        try:
            await runner.stop()
        except Exception as e:
            mapping = getattr(runner, "mapping", None)
            log = self._logger_factory(mapping) if mapping is not None else self.log
            log(f"stopping failed: {e!r}")

    async def apply_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        """Create/replace and (if enabled) start the runner for a mapping, from its config
        when its turn comes."""
        async with self._turn(mapping.id):
            return await self._apply(mapping.id, self._current(mapping))

    async def _apply(self, mapping_id: str, mapping: Optional[MappingConfig]) -> tuple[bool, str]:
        """apply_mapping in the mapping's turn. None: it was deleted meanwhile, so its
        runner only goes."""
        old = self._runners.pop(mapping_id, None)
        if old is not None:
            await self._stop_runner(old)
        if mapping is None:
            if mapping_id in self._order:
                self._order.remove(mapping_id)
            return False, "the mapping was deleted meanwhile"

        runner = _build_runner(mapping, self._logger_factory(mapping), self.trace_dir)
        if mapping.id not in self._order:
            self._order.append(mapping.id)
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
            self.log(f"[{mapping.name}] start failed: {e}")
            return False, str(e)

    async def start_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        return await self.apply_mapping(mapping)

    async def ensure_running(self, mapping: MappingConfig) -> tuple[bool, str, bool]:
        """apply_mapping, unless the mapping's runner is what its config asks for already:
        built from the same settings, and running (ALIVE_STATES) when it is enabled or
        stopped when it is not. So a start of a running mapping keeps its clients, serial
        port and MQTT connection; one that stopped or failed is started again. Returns
        (ok, message, changed): changed is False when nothing was done."""
        async with self._turn(mapping.id):
            current = self._current(mapping)
            runner = self._runners.get(mapping.id)
            built = getattr(runner, "mapping", None)
            if (current is not None and runner is not None and built is not None
                    and runner.status.state in (ALIVE_STATES if current.enabled else ("stopped",))
                    and built.to_dict() == current.to_dict()):
                return True, "running" if current.enabled else "disabled", False
            ok, message = await self._apply(mapping.id, current)
            return ok, message, True

    async def stop_mapping(self, mapping_id: str) -> bool:
        """Stop a mapping's runner (kept, stopped, for its status) — unless its config has
        it enabled by now: a start saved after this stop takes its own turn. Returns
        whether a runner was stopped (False: none, or stopped already)."""
        async with self._turn(mapping_id):
            current = self._lookup(mapping_id) if self._lookup is not None else None
            if current is not None and current.enabled:
                return False
            runner = self._runners.get(mapping_id)
            if runner is None or runner.status.state == "stopped":
                return False
            await self._stop_runner(runner)
            return True

    async def restart_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        return await self.apply_mapping(mapping)  # which stops the old runner first

    async def remove_mapping(self, mapping_id: str) -> None:
        async with self._turn(mapping_id):
            runner = self._runners.pop(mapping_id, None)
            if mapping_id in self._order:
                self._order.remove(mapping_id)
            if runner is not None:
                await self._stop_runner(runner)

    async def remove_trace_files(self, mapping_id: str, paths) -> dict:
        """Delete some of a mapping's trace files in its turn (so no start or stop swaps
        its trace writer meanwhile). While the mapping traces, its writer deletes them
        (bridge.Tracer.remove_files): never in the middle of a rotation, and the file it
        writes is closed first and begun again with the next traffic. Returns path ->
        None (gone) or the OSError that kept it."""
        async with self._turn(mapping_id):
            tracer = getattr(self._runners.get(mapping_id), "tracer", None)
            done = tracer.remove_files(paths) if tracer is not None else None
            if done is not None:
                return await asyncio.wrap_future(done)
            return await asyncio.to_thread(remove_files, paths)

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

    def devices_in_use(self) -> set[str]:
        """The serial devices that running mappings hold open (for the Ports page): one per
        mapping, both ports of a serial-to-serial bridge."""
        out: set[str] = set()
        for r in list(self._runners.values()):
            if r.status.state == "running":
                out.update(d for d in r.status.devices if d)
        return out
