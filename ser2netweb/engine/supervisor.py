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

The operations on one mapping (apply, restart, stop, remove, a bind retry's attempt, and
its part of start_all / stop_all) take turns: each runs to its end before the next one for
that mapping begins, so two of them can never build two runners, one of which nobody
tracks any more while it keeps the port and the serial device. Operations on different
mappings still run side by side. Given a `lookup`, an operation reads the mapping's config
when its turn comes, so one that waited (or a copy a request read earlier) can't undo a
later change.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from typing import Any, Callable, Optional, Protocol

from .. import tlsreload
from ..config import AppConfig, MappingConfig, mqtt_start_warnings, mqtt_warnings
from . import bindhint, portlist, serial_io
from .bridge import MappingRunner, RunnerStatus, remove_files
from .netguard import BindError
from .serial_link import Backoff

# a runner in these states runs: the serial side retries a missing device on its own
ALIVE_STATES = ("running", "reconnecting", "device-missing")
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


def make_runner(mapping: MappingConfig, logger, trace_dir: Optional[str] = None,
                state_dir: Optional[str] = None) -> Runner:
    """Pick the runner for a mapping: the Modbus RTU<->TCP gateway when the network
    protocol is 'modbus', otherwise the transparent serial<->network bridge. ``state_dir``:
    the data folder, where a runner keeps state across restarts (None: in memory only)."""
    if mapping.kind == "net" and mapping.network.protocol == "modbus":
        from .modbus_gateway import ModbusGatewayRunner
        return ModbusGatewayRunner(mapping, logger=logger, trace_dir=trace_dir,
                                   state_dir=state_dir)
    return MappingRunner(mapping, logger=logger, trace_dir=trace_dir)


class _Unbuildable:
    """Stands in for a mapping whose runner can't be built from its settings (a hand-edited
    value of the wrong type), so its status says why and the rest keeps running; starting
    it fails with that error."""

    def __init__(self, error: Exception):
        self.error = f"its settings can't be used: {error}"
        self.status = RunnerStatus(state="stopped")
        self._viewers: set = set()  # consoles and hex views: closed when it is replaced

    async def start(self) -> None:
        raise RuntimeError(self.error)

    async def stop(self) -> None:
        for mon in list(self._viewers):
            mon.close()
        self._viewers.clear()

    # the console and the hex view attach to any runner: there is nothing to watch or
    # write to here
    def add_monitor(self, mon) -> None:
        self._viewers.add(mon)

    def discard_monitor(self, mon) -> None:
        self._viewers.discard(mon)

    add_traffic_monitor = add_monitor
    discard_traffic_monitor = discard_monitor

    async def serial_write(self, data: bytes) -> None:
        pass


def _build_runner(mapping: MappingConfig, logger, trace_dir: Optional[str],
                  state_dir: Optional[str] = None):
    try:
        return make_runner(mapping, logger, trace_dir, state_dir)
    except Exception as e:  # only this mapping is down
        return _Unbuildable(e)


def _consume_error(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # a runner that failed to stop: nothing more to do about it


async def _retried_start(runner: Runner, log: Callable[[str], None], backoff: Backoff) -> None:
    """A bind retry's attempt (Supervisor._retry). Once the runner runs, its status stops
    saying it retries and the start is logged in the same step of the event loop, not in
    the next one, when the retry sees the attempt done: a status read in between said
    'running' and 'retrying' at once."""
    await runner.start()
    runner.status.retry_at = 0.0
    log(f"started after {backoff.attempt} retr{'y' if backoff.attempt == 1 else 'ies'}")


def _name(runner: Any) -> str:
    mapping = getattr(runner, "mapping", None)
    return getattr(mapping, "name", None) or "?"


class Supervisor:
    def __init__(self, logger: Optional[Callable[[str], None]] = None,
                 logger_factory: Optional[Callable[[MappingConfig], Callable[[str], None]]] = None,
                 trace_dir: Optional[str] = None,
                 lookup: Optional[Callable[[str], Optional[MappingConfig]]] = None,
                 state_dir: Optional[str] = None):
        self._runners: dict[str, Runner] = {}
        self.trace_dir = trace_dir  # data/traces: the only place traces are written
        self.state_dir = state_dir  # data/: what runners keep across restarts (Sparkplug bdSeq)
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
        self._stopping: set[asyncio.Future] = set()  # stop_all()'s stops still under way
        # the mappings whose stop_all() stop is still under way after its time limit: their
        # turn is held until it ends (start_all and remove_mapping don't wait for it)
        self._late: set[str] = set()
        self._retries: dict[str, asyncio.Task] = {}  # mapping id -> its bind retry
        self._ports: Optional[set[str]] = None  # the serial ports last seen (ports_changed)

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
        """Start every enabled mapping (a placeholder runner for each disabled one), each in
        its turn. A mapping whose stop_all() stop is still under way (it outlived
        STOP_ALL_TIMEOUT_S) is started in the background once that stop ends, so it can't
        hold up the others (an import or a restore stops all, then starts all)."""
        self._order = [m.id for m in config.mappings]
        # (in one pass: each against those started before it)
        notes = mqtt_start_warnings(config.mappings)
        for m, mqtt_notes in zip(list(config.mappings), notes, strict=True):
            if m.id in self._late:
                self._logger_factory(m)("start waits for the previous stop to end")
                task = asyncio.ensure_future(self.apply_mapping(m, mqtt_notes))
                self._stopping.add(task)
                task.add_done_callback(self._stopping.discard)
                task.add_done_callback(_consume_error)
                continue
            async with self._turn(m.id):
                current = self._current(m)
                if current is None:
                    continue  # deleted meanwhile
                if current.enabled:
                    await self._apply(current.id, current, mqtt_notes)
                else:
                    self._ensure_stopped_placeholder(current)

    async def stop_all(self) -> None:
        """Stop every runner, side by side, each in its mapping's turn, waiting at most
        STOP_ALL_TIMEOUT_S: a stop still under way then (a device or broker that doesn't
        answer, or an operation on that mapping that doesn't end) is logged and left to
        finish in the background, instead of keeping the others running (an import or a
        restore stops all, then starts the new set) or the process from exiting. Loops
        until no runner is left (also one an operation under way adds meanwhile)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_ALL_TIMEOUT_S
        while self._runners:
            stops = {asyncio.ensure_future(self._stop_in_turn(mid)): (mid, _name(runner))
                     for mid, runner in list(self._runners.items())}
            for task in stops:
                self._stopping.add(task)  # (a task left running must not be garbage-collected)
                task.add_done_callback(self._stopping.discard)
                task.add_done_callback(_consume_error)
            _done, pending = await asyncio.wait(stops, timeout=max(0.0, deadline - loop.time()))
            if pending:
                names = ", ".join(sorted(stops[t][1] for t in pending))
                self.log(f"stopping: {len(pending)} mapping(s) did not stop within "
                         f"{STOP_ALL_TIMEOUT_S:g} s, left to finish on their own: {names}")
                for task in pending:
                    mid = stops[task][0]
                    self._late.add(mid)
                    task.add_done_callback(functools.partial(self._stop_ended, mid))
                return

    def _stop_ended(self, mapping_id: str, _task: asyncio.Future) -> None:
        """A stop that outlived stop_all's limit has ended: its mapping's turn is free."""
        self._late.discard(mapping_id)

    async def _stop_in_turn(self, mapping_id: str) -> None:
        """stop_all's part for one mapping: its bind retry ends (an attempt under way holds
        the turn, so it ends first) and its runner goes."""
        async with self._turn(mapping_id):
            await self._cancel_retry(mapping_id)
            runner = self._runners.pop(mapping_id, None)
            if runner is not None:
                await self._stop_runner(runner)

    # ---------------- per-mapping ----------------
    def _ensure_stopped_placeholder(self, mapping: MappingConfig) -> None:
        """Register a non-running runner so status is queryable for disabled mappings."""
        if mapping.id not in self._runners:
            r = _build_runner(mapping, self._logger_factory(mapping), self.trace_dir,
                              self.state_dir)
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

    async def apply_mapping(self, mapping: MappingConfig,
                            mqtt_notes: Optional[list[str]] = None) -> tuple[bool, str]:
        """Create/replace and (if enabled) start the runner for a mapping, from its config
        when its turn comes. A start that fails because the listener can't bind is tried
        again later (_retry). ``mqtt_notes``: its mqtt_warnings, when the caller has them
        already (start_all)."""
        async with self._turn(mapping.id):
            return await self._apply(mapping.id, self._current(mapping), mqtt_notes)

    async def _apply(self, mapping_id: str, mapping: Optional[MappingConfig],
                     mqtt_notes: Optional[list[str]] = None) -> tuple[bool, str]:
        """apply_mapping in the mapping's turn. None: it was deleted meanwhile, so its
        runner only goes."""
        await self._cancel_retry(mapping_id)
        old = self._runners.pop(mapping_id, None)
        if old is not None:
            await self._stop_runner(old)
        if mapping is None:
            if mapping_id in self._order:
                self._order.remove(mapping_id)
            return False, "the mapping was deleted meanwhile"

        log = self._logger_factory(mapping)
        runner = _build_runner(mapping, log, self.trace_dir, self.state_dir)
        if mapping.id not in self._order:
            self._order.append(mapping.id)
        self._runners[mapping.id] = runner

        if not mapping.enabled:
            runner.status.state = "stopped"
            return True, "disabled"
        # what it shares over MQTT with the other enabled mappings (which the edits refuse
        # or warn about), and its own MQTT topics the edits refuse: said at each start
        if mqtt_notes is None:
            mqtt_notes = mqtt_warnings(mapping, [
                r.mapping for mid, r in list(self._runners.items())
                if mid != mapping.id and isinstance(getattr(r, "mapping", None), MappingConfig)])
        for line in mqtt_notes:
            log(f"warning: {line}")
        try:
            # a stored URL port fails the start once, instead of retrying it forever
            for side in mapping.serial_sides():
                serial_io.refuse_url(side.port)
            await runner.start()
            return True, "started"
        except Exception as e:
            runner.status.state = "error"
            runner.status.last_error = str(e)
            # why the system refused a listener's port, and what to do (None otherwise)
            hint = bindhint.for_mapping(e, mapping)
            runner.status.hint = bindhint.note(hint)
            # all.log ("[name] ...") and the mapping's own log
            if isinstance(e, BindError):
                log(f"start failed: {e} — trying again until the address can be used "
                    f"(every {BIND_RETRY_MAX_S:g} s at most)")
                self._retry_later(mapping, runner, str(e))
            else:
                log(f"start failed: {e}")
            if hint:
                log(bindhint.explain(hint))
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
        """Start ``runner`` again, with a backoff, while its listener can't bind. Each
        attempt runs in the mapping's turn, so no apply, stop or removal overlaps it; they
        end the retry in their turn (_cancel_retry), so the runner a retry starts is
        always the one tracked. Logs a changed error and the start that worked (all.log and
        the mapping's own log, as apply_mapping does); any other error ends the retries."""
        st = runner.status
        log = self._logger_factory(mapping)
        try:
            while True:
                st.retry_at = time.monotonic() + pause
                await asyncio.sleep(pause)
                async with self._turn(mapping.id):
                    if self._runners.get(mapping.id) is not runner:
                        return  # (replaced or removed: that ended this retry too)
                    st.retry_at = time.monotonic()  # trying now
                    # asyncio.wait, which cancels nothing: a cancelled retry (the event loop
                    # ending) waits for an attempt under way to end
                    attempt = asyncio.ensure_future(_retried_start(runner, log, backoff))
                    attempt.add_done_callback(_consume_error)
                    try:
                        await asyncio.wait((attempt,))
                    except asyncio.CancelledError:
                        await asyncio.wait((attempt,))
                        raise
                    e = attempt.exception()
                    if e is None:
                        return
                    st.state, st.last_error = "error", str(e)
                    hint = bindhint.for_mapping(e, mapping)
                    st.hint = bindhint.note(hint)
                    if not isinstance(e, BindError):
                        log(f"start failed: {e}")
                        return
                    if str(e) != error:
                        error = str(e)
                        log(f"start failed again: {e}")
                        if hint:
                            log(bindhint.explain(hint))
                pause = backoff.pause()
        finally:
            st.retry_at = 0.0
            if self._retries.get(mapping.id) is asyncio.current_task():
                del self._retries[mapping.id]

    async def _cancel_retry(self, mapping_id: str) -> None:
        """End a mapping's bind retry before it is applied, stopped or removed: a retry
        left running would undo a Stop. Called in the mapping's turn, so the retry is
        waiting (its pause, or for the turn), never in the middle of an attempt."""
        task = self._retries.pop(mapping_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def start_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        return await self.apply_mapping(mapping)

    async def ensure_running(self, mapping: MappingConfig) -> tuple[bool, str, bool]:
        """apply_mapping, unless the mapping's runner is what its config asks for already:
        built from the same settings, and running (ALIVE_STATES) when it is enabled or
        stopped when it is not. So a start of a running mapping keeps its clients, serial
        port and MQTT connection; one that stopped or failed (also one waiting for its bind
        retry) is started again. Returns (ok, message, changed): changed is False when
        nothing was done."""
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
        """Stop a mapping's runner (kept, stopped, for its status) and its bind retry —
        unless its config has it enabled by now: a start saved after this stop takes its
        own turn. Returns whether a runner was stopped (False: none, or stopped already)."""
        async with self._turn(mapping_id):
            current = self._lookup(mapping_id) if self._lookup is not None else None
            if current is not None and current.enabled:
                return False
            await self._cancel_retry(mapping_id)
            runner = self._runners.get(mapping_id)
            if runner is None or runner.status.state == "stopped":
                return False
            await self._stop_runner(runner)
            return True

    async def restart_mapping(self, mapping: MappingConfig) -> tuple[bool, str]:
        return await self.apply_mapping(mapping)  # which stops the old runner first

    async def remove_mapping(self, mapping_id: str) -> None:
        if mapping_id in self._late and mapping_id not in self._runners:
            # its stop_all() stop holds the turn and has its runner: nothing left to stop
            # here (an operation waiting for the turn reads the config then: it's gone)
            if mapping_id in self._order:
                self._order.remove(mapping_id)
            return
        async with self._turn(mapping_id):
            await self._cancel_retry(mapping_id)
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

    async def retrace(self, mapping_id: str) -> bool:
        """In the mapping's turn: its running runner opens or closes its trace as the
        options ask for now (a timed capture begun or ended), keeping its clients and its
        serial port. A runner that is not running opens its trace when it starts.
        Returns whether a trace was opened or closed."""
        async with self._turn(mapping_id):
            runner = self._runners.get(mapping_id)
            change = getattr(runner, "retrace", None)
            if runner is None or change is None:
                return False
            # a runner kept across a merge import was built from an equal mapping that is
            # no longer the configuration's object: it takes the capture's end from that
            built = getattr(runner, "mapping", None)
            current = self._lookup(mapping_id) if self._lookup is not None else None
            if current is not None and built is not None and built is not current:
                built.options.trace_until = current.options.trace_until
            if runner.status.state not in ALIVE_STATES:
                return False
            return bool(await change())

    async def renew_tls(self, mapping_id: str, build: Callable[[], Any]) -> Optional[str]:
        """In the mapping's turn: its running TLS listener gets a context made by build()
        (tlsreload.prepare) for the connections it accepts from now on; those open keep
        theirs. Returns None when it has none (not running, no TLS), "" once renewed, else
        why not (the context in use stays)."""
        async with self._turn(mapping_id):
            listener = getattr(self._runners.get(mapping_id), "listener", None)
            if listener is None or listener.ssl is None:
                return None
            prepared = await tlsreload.prepare(build)
            if not prepared.usable or prepared.context is None:
                return prepared.error or prepared.later or "no TLS context"
            listener.ssl = prepared.context
            return ""

    def annotate_traces(self, text: str) -> int:
        """Put a "!!" note into every running trace (bridge.Tracer.annotate): where the
        system clock was stepped. Returns how many traces got it."""
        count = 0
        for runner in list(self._runners.values()):
            tracer = getattr(runner, "tracer", None)
            if tracer is not None and tracer.active:
                tracer.annotate(text)
                count += 1
        return count

    async def renew_traces(self, begun_before: float) -> int:
        """Have every running trace whose file holds traffic from before `begun_before`
        begin a new one (bridge.Tracer.renew_if_older, in its writer thread). Returns how
        many did."""
        waits = []
        for runner in list(self._runners.values()):
            tracer = getattr(runner, "tracer", None)
            done = tracer.renew_if_older(begun_before) if tracer is not None else None
            if done is not None:
                waits.append(asyncio.wrap_future(done))
        results = await asyncio.gather(*waits, return_exceptions=True)
        return sum(r is True for r in results)

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
