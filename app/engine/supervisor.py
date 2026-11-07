"""Supervisor: owns the set of MappingRunners and applies config changes.

The web layer mutates + persists the AppConfig, then calls this to reconcile the
running bridges. Editing a mapping at runtime stops the old runner (awaiting full
teardown so the listening socket is released) before starting the new one, which
avoids address-in-use / port races.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable, Optional, Protocol

from ..config import AppConfig, MappingConfig
from . import serial_io
from .bridge import MappingRunner, RunnerStatus


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

    # ---------------- bulk ----------------
    async def start_all(self, config: AppConfig) -> None:
        self._order = [m.id for m in config.mappings]
        for m in config.mappings:
            if m.enabled:
                await self.apply_mapping(m)
            else:
                self._ensure_stopped_placeholder(m)

    async def stop_all(self) -> None:
        async with self._lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for r in runners:
            with contextlib.suppress(Exception):
                await r.stop()

    # ---------------- per-mapping ----------------
    def _ensure_stopped_placeholder(self, mapping: MappingConfig) -> None:
        """Register a non-running runner so status is queryable for disabled mappings."""
        if mapping.id not in self._runners:
            try:
                r = make_runner(mapping, self._logger_factory(mapping), self.trace_dir)
            except Exception:  # see apply_mapping; start-up already warned about it
                return
            r.status.state = "stopped"
            self._runners[mapping.id] = r
            if mapping.id not in self._order:
                self._order.append(mapping.id)

    async def apply_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        """Create/replace and (if enabled) start the runner for a mapping."""
        async with self._lock:
            old = self._runners.pop(mapping.id, None)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.stop()

        try:
            runner = make_runner(mapping, self._logger_factory(mapping), self.trace_dir)
        except Exception as e:  # a hand-edited value of the wrong type: only this one is down
            self.log(f"[{mapping.name}] start failed: {e}")
            return False, str(e)
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
            self.log(f"[{mapping.name}] start failed: {e}")
            return False, str(e)

    async def start_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        return await self.apply_mapping(mapping)

    async def stop_mapping(self, mapping_id: str) -> None:
        async with self._lock:
            runner = self._runners.get(mapping_id)
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.stop()

    async def restart_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        await self.stop_mapping(mapping.id)
        return await self.apply_mapping(mapping)

    async def remove_mapping(self, mapping_id: str) -> None:
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

    def devices_in_use(self) -> set[str]:
        """The serial devices that running mappings hold open (for the Ports page): one per
        mapping, both ports of a serial-to-serial bridge."""
        out: set[str] = set()
        for r in list(self._runners.values()):
            if r.status.state == "running":
                out.update(d for d in r.status.devices if d)
        return out
