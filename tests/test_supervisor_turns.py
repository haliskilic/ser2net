"""Operations on one mapping take turns in the supervisor.

Starting, stopping, restarting, saving and deleting a mapping can come from several
places at once (the row's Stop and Restart buttons, a bulk action, the REST API, a
cluster peer, an import or a restore, two admins). Each one used to stop the old runner
outside the supervisor's lock and then overwrite the runner it tracks, so two of them
could leave a second runner nobody tracked: it kept the port and the serial device while
the tracked one failed with "address already in use", and Stop / Delete never reached it.
Checks, with runners that open nothing (a shared dict stands for the port):
  * two applies / restarts of one mapping at once leave one runner, the tracked one, and
    it holds the port; a delete while it starts leaves nothing running;
  * different mappings still start side by side;
  * an operation reads the mapping's config when its turn comes: a stale copy builds the
    runner of the current config, a deleted mapping is not started again, a stop that a
    later start overtook leaves the mapping running;
  * stop_all also stops what an operation under way starts; a failing stop() is logged;
  * a real bridge / Modbus gateway stopped while its listener comes up closes it (and
    the gateway starts no MQTT client).

Run: python3 tests/test_supervisor_turns.py   (binds 127.0.0.1 port 0 in the last part)
"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import bridge, modbus_gateway, netguard, supervisor
from app.engine.bridge import MappingRunner, RunnerStatus
from app.engine.modbus_gateway import ModbusGatewayRunner
from app.engine.supervisor import Supervisor

PORTS: dict = {}     # port -> the fake runner listening on it
LIVE: set = set()    # fake runners started and not stopped yet
STOP_FAILS: set = set()


class FakeRunner:
    """Takes the port a moment after start() begins, and gives it back at once in stop(),
    which then goes on for a while (flushing the serial port and the trace), as a real
    runner does."""

    def __init__(self, mapping, logger=None, trace_dir=None):
        self.mapping = mapping
        self.status = RunnerStatus(state="stopped")
        self.stopped = False

    async def start(self):
        await asyncio.sleep(0.02)  # binding
        if self.stopped:
            return
        port = self.mapping.network.port
        if port in PORTS:
            raise OSError(98, "address already in use")
        PORTS[port] = self
        LIVE.add(self)
        self.status.state = "running"

    async def stop(self):
        self.stopped = True
        port = self.mapping.network.port
        if PORTS.get(port) is self:
            del PORTS[port]
        await asyncio.sleep(0.05)  # flushing
        LIVE.discard(self)
        self.status.state = "stopped"
        if self.mapping.name in STOP_FAILS:
            raise RuntimeError("the device did not answer")

    def add_monitor(self, mon): pass
    def discard_monitor(self, mon): pass
    def add_traffic_monitor(self, mon): pass
    def discard_traffic_monitor(self, mon): pass
    async def serial_write(self, data): return False


def mapping(mid, port, name=None, enabled=True):
    return MappingConfig.from_dict({
        "id": mid, "name": name or mid, "enabled": enabled,
        "serial": {"port": f"/dev/s2n-turns-{mid}"},
        "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port}})


def reset():
    PORTS.clear()
    LIVE.clear()
    STOP_FAILS.clear()


def one_runner(sup, mid, port):
    tracked = sup.get_runner(mid)
    assert tracked is not None and tracked.status.state == "running", \
        tracked and tracked.status.as_dict()
    assert PORTS.get(port) is tracked, "the tracked runner must hold the port"
    assert LIVE == {tracked}, f"{len(LIVE)} runners live, one tracked"


async def test_same_mapping_takes_turns():
    reset()
    sup = Supervisor()
    m = mapping("aaaaaaaaaaa1", 7001)
    await sup.apply_mapping(m)
    await asyncio.gather(sup.apply_mapping(m), sup.apply_mapping(m))
    one_runner(sup, m.id, 7001)
    await asyncio.gather(sup.restart_mapping(m), sup.restart_mapping(m), sup.apply_mapping(m))
    one_runner(sup, m.id, 7001)
    # a delete while a start is under way: nothing keeps running
    start = asyncio.ensure_future(sup.apply_mapping(m))
    await asyncio.sleep(0.005)
    await sup.remove_mapping(m.id)
    await start
    # (without a lookup the start ran first, and the remove after it stopped what it began)
    assert sup.get_runner(m.id) is None and not LIVE and not PORTS, (LIVE, PORTS)
    assert sup._turns == {}, sup._turns
    print("one mapping: concurrent apply / restart / delete leave one tracked runner or none  OK")


async def test_other_mappings_side_by_side():
    reset()
    sup = Supervisor()
    both = asyncio.Event()
    seen = []

    class Waits(FakeRunner):
        async def start(self):
            seen.append(self.mapping.id)
            if len(seen) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)  # times out if the starts were serialized
            await super().start()

    supervisor.make_runner, real = (lambda m, log, trace_dir=None: Waits(m)), supervisor.make_runner
    try:
        results = await asyncio.gather(sup.apply_mapping(mapping("bbbbbbbbbbb1", 7011)),
                                       sup.apply_mapping(mapping("bbbbbbbbbbb2", 7012)))
    finally:
        supervisor.make_runner = real
    assert results == [(True, "started"), (True, "started")], results
    await sup.stop_all()
    assert not LIVE and not sup._runners
    print("different mappings start side by side  OK")


async def test_current_config_wins():
    reset()
    config = {"m": mapping("ccccccccccc1", 7021)}
    sup = Supervisor(lookup=lambda mid: config["m"] if config["m"] and config["m"].id == mid else None)
    stale = config["m"]
    config["m"] = mapping("ccccccccccc1", 7022)  # edited meanwhile
    await sup.apply_mapping(stale)
    assert sup.get_runner(stale.id).mapping is config["m"] and 7022 in PORTS and 7021 not in PORTS
    # a stop saved before a start that got its turn first: the mapping stays running
    await sup.stop_mapping(stale.id)
    one_runner(sup, stale.id, 7022)
    config["m"] = mapping("ccccccccccc1", 7022, enabled=False)
    await sup.stop_mapping(stale.id)
    assert sup.get_runner(stale.id).status.state == "stopped" and not LIVE
    # deleted meanwhile: an apply that waited doesn't start it again
    config["m"] = None
    ok, why = await sup.apply_mapping(stale)
    assert not ok and "deleted" in why and sup.get_runner(stale.id) is None and not LIVE, why
    print("each operation acts on the config when its turn comes  OK")


async def test_stop_all_and_stop_errors():
    reset()
    logs = []
    sup = Supervisor(logger=logs.append)
    a, b = mapping("ddddddddddd1", 7031, "A"), mapping("ddddddddddd2", 7032, "B")
    await sup.apply_mapping(a)
    late = asyncio.ensure_future(sup.apply_mapping(b))  # under way while everything stops
    await asyncio.sleep(0)
    STOP_FAILS.add("A")
    await sup.stop_all()
    await late
    assert not sup._runners and not LIVE and not PORTS, (sup._runners, LIVE, PORTS)
    assert any(line.startswith("[A] stopping failed") and "did not answer" in line
               for line in logs), logs
    print("stop_all stops what an operation under way started; a failing stop is logged  OK")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_real_runner_stopped_while_listening_starts():
    """stop() while start() waits for its listener: the listener is closed afterwards."""
    # (the gateway would start its MQTT client right after the listener: paho is optional,
    # so nothing may reach it here)
    gateway_extra = {"mqtt": {"enabled": True, "host": "127.0.0.1", "port": _free_port()},
                     "modbus_poll": {"points": [{"name": "t", "unit": 1, "address": 0}]}}
    for make, cfg, extra in (
            (MappingRunner, {"protocol": "raw"}, {}),
            (ModbusGatewayRunner, {"protocol": "modbus"}, gateway_extra)):
        port = _free_port()
        m = MappingConfig.from_dict({
            "name": "half-started", "enabled": True,
            "serial": {"port": "/dev/s2n-turns-missing"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port, **cfg},
            **extra})
        runner = make(m, logger=lambda _msg: None)
        listening, go = asyncio.Event(), asyncio.Event()
        real = netguard.start_server

        async def slow_start_server(*args, **kw):
            listener = await real(*args, **kw)
            listening.set()
            await go.wait()
            return listener

        for mod in (bridge, modbus_gateway):
            mod.netguard.start_server = slow_start_server
        try:
            start = asyncio.ensure_future(runner.start())
            await asyncio.wait_for(listening.wait(), 5)
            await runner.stop()
            go.set()
            await asyncio.wait_for(start, 10)
        finally:
            for mod in (bridge, modbus_gateway):
                mod.netguard.start_server = real
        assert runner._server is None and runner.status.state == "stopped", runner.status.as_dict()
        assert getattr(runner, "_mqtt", None) is None
        assert getattr(runner, "_poll_task", None) is None
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "the listener was left open"
    print("a bridge / gateway stopped while its listener comes up closes it, starts no MQTT  OK")


async def main():
    supervisor.make_runner, real = (lambda m, log, trace_dir=None: FakeRunner(m)), supervisor.make_runner
    try:
        await test_same_mapping_takes_turns()
        await test_other_mappings_side_by_side()
        await test_current_config_wins()
        await test_stop_all_and_stop_errors()
    finally:
        supervisor.make_runner = real
    await test_real_runner_stopped_while_listening_starts()
    print("\nPASS: supervisor turns")


if __name__ == "__main__":
    asyncio.run(main())
