"""How a mapping keeps trying a serial port it can't open (no socat; PTY parts POSIX only).

  - the log: the first failure and each change of the error, then the same error only
    every FAILING_SUMMARY_S with how many attempts failed since when, and how long it was
    down once the port opens again (FailureLog; also connect-out failures);
  - the state: device-missing while the port is not there (ENOENT; it stays so across the
    retries, nothing overwrites it with reconnecting), reconnecting right after an open
    port went away; open_failures and device_lost count the two (reconnects both, as
    before), also in /metrics;
  - start() never writes "running" over what the serial side reported: a port that fails
    at once stays device-missing (listening mappings: UDP, TCP server, Modbus gateway);
  - a port that appears (PortWatcher's on_change -> Supervisor.ports_changed) wakes the
    mappings waiting to open theirs again (state device-missing / reconnecting) at once,
    instead of after their backoff (up to ~10 s); one that is running is left alone;
  - a mapping bound to a USB identity finds its port in the PortWatcher's list when that is
    fresh, else by enumerating in a thread: never on the event loop;
  - serial<->serial bridge: while one port is missing, the other one is not opened and
    closed again at every retry (each open pulses DTR/RTS: a board that resets on DTR
    would reset every few seconds). The side that failed is opened first next time, so
    with B missing A is opened once, and again only once B is there; with A missing, B is
    never opened. Its state and counters follow the same rules.
The retries are made fast (Backoff.pause patched); ports are PTYs, a missing one a path
that does not exist yet (a symlink to a PTY "plugs it in", closing the PTY's master end
unplugs it).

Run: python3 tests/test_serial_retries.py
"""
import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import threading
import time
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import AppConfig, MappingConfig, SerialSettings
from ser2netweb.engine import portlist, serial_io, serial_link
from ser2netweb.engine.bridge import MappingRunner
from ser2netweb.engine.serial_link import FAILING_SUMMARY_S, Backoff, FailureLog
from ser2netweb.engine.supervisor import Supervisor


class Pty:
    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)

    def close(self):
        os.close(self.master)


def _fast_pause(self):
    self.attempt += 1
    return 0.05


@contextlib.contextmanager
def fast_retries():
    with mock.patch.object(serial_link.Backoff, "pause", _fast_pause):
        yield


@contextlib.contextmanager
def counted_opens(opens):
    real = serial_io.open_serial

    async def counting(settings):
        opens.append(settings.port)
        return await real(settings)

    with mock.patch.object(serial_io, "open_serial", counting):
        yield


async def until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def _bridge_side_missing(folder):
    a, b = Pty(), Pty()
    missing = os.path.join(folder, "b-port")
    logs, opens = [], []
    m = MappingConfig.from_dict({"name": "sb", "kind": "serialbridge",
                                 "serial": {"port": a.path}, "serial_b": {"port": missing}})
    runner = MappingRunner(m, logger=logs.append)
    try:
        with fast_retries(), counted_opens(opens):
            await runner.start()
            st = runner.status
            await until(lambda: st.open_failures >= 6)  # (each counted once it has failed)
            assert opens.count(a.path) == 1, opens
            assert st.state == "device-missing", st.as_dict()
            assert sum(ln.startswith("serial bridge error:") for ln in logs) == 1, logs
            os.symlink(b.path, missing)  # B is plugged in
            await until(lambda: runner.status.state == "running")
            assert opens.count(a.path) == 2 and opens[-2:] == [missing, a.path], opens
            assert any(ln.startswith("serial bridge open:") and "(back after " in ln
                       for ln in logs), logs
            b.close()  # B unplugged while the bridge runs
            await until(lambda: st.device_lost == 1)
            await until(lambda: st.state == "device-missing")
            await runner.stop()

        # the other way round: with A missing, B is never opened
        opens.clear()
        m = MappingConfig.from_dict({"name": "sb2", "kind": "serialbridge",
                                     "serial": {"port": os.path.join(folder, "a-port")},
                                     "serial_b": {"port": b.path}})
        runner = MappingRunner(m, logger=logs.append)
        with fast_retries(), counted_opens(opens):
            await runner.start()
            await until(lambda: len(opens) >= 6)
            assert b.path not in opens, opens
    finally:
        await runner.stop()
        a.close()


def test_failure_log():
    lines = []
    clock = [1000.0]
    with mock.patch.object(serial_link.time, "monotonic", lambda: clock[0]):
        log = FailureLog(lines.append, "serial error")
        assert log.recovered() == "", "nothing failed"
        assert log.failed("could not open port x: ENOENT") is True
        for _ in range(500):  # ~1.4 h of retries every 10 s
            clock[0] += 10
            if clock[0] - 1000 >= FAILING_SUMMARY_S:
                break
            assert log.failed("could not open port x: ENOENT") is False
        log.failed("could not open port x: ENOENT")  # the summary is due
        clock[0] += 10
        log.failed("could not open port x: EACCES")  # changed: logged at once
        log.failed("could not open port x: EACCES")
        back = log.recovered()
        log.failed("could not open port x: EACCES")  # a new outage: logged again
    assert len(lines) == 4, lines
    assert lines[0] == "serial error: could not open port x: ENOENT", lines
    assert lines[1].startswith("serial error: could not open port x: ENOENT (still failing: "
                               "91 attempts since "), lines[1]
    assert lines[2] == "serial error: could not open port x: EACCES", lines
    assert back.startswith(" (back after ") and back.endswith(")"), back
    assert lines[3] == "serial error: could not open port x: EACCES", lines
    print("failure log: the first failure and each change, the same error every "
          f"{FAILING_SUMMARY_S:g} s with the attempts since when, how long it was down  OK")


async def _states_and_counters(folder):
    link = os.path.join(folder, "port")
    logs = []
    m = MappingConfig.from_dict({"name": "raw", "serial": {"port": link},
                                 "network": {"mode": "client", "protocol": "raw",
                                             "remote_host": "127.0.0.1", "remote_port": 9}})
    runner = MappingRunner(m, logger=logs.append)
    st = runner.status
    seen = set()
    real = runner._link._failed

    async def failed(e, was_open):  # every state right after a failure (the dashboard's)
        await real(e, was_open)
        seen.add(st.state)

    runner._link._failed = failed
    pty = None
    try:
        with fast_retries():
            task = asyncio.ensure_future(runner._link.run(runner._serial_session))
            await until(lambda: st.open_failures >= 8)
            assert seen == {"device-missing"} and st.state == "device-missing", (seen, st)
            assert st.device_lost == 0 and st.reconnects >= st.open_failures - 1, st
            errors = [ln for ln in logs if ln.startswith("serial error:")]
            assert len(errors) == 1 and "No such file or directory" in errors[0], logs

            pty = Pty()
            os.symlink(pty.path, link)  # plugged in
            await until(lambda: st.state == "running")
            opened = [ln for ln in logs if ln.startswith("serial open:")]
            assert len(opened) == 1 and " (back after " in opened[0], opened

            seen.clear()
            pty.close()  # unplugged while open
            pty = None
            await until(lambda: st.device_lost == 1)
            assert "reconnecting" in seen, seen
            await until(lambda: st.state == "device-missing")  # the next open: no device
            failures = st.open_failures
            assert st.as_dict()["open_failures"] == failures and st.as_dict()["device_lost"] == 1
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        await runner._link.close()
        if pty is not None:
            pty.close()


def test_states_and_counters():
    if os.name != "posix":
        print("SKIP (part): the PTY checks are POSIX only")
        return
    folder = tempfile.mkdtemp(prefix="s2n_retries_")
    try:
        asyncio.run(_states_and_counters(folder))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("serial port: device-missing while it is not there (kept across retries), "
          "reconnecting after a loss; open_failures / device_lost counted; one log line for "
          "many failed opens, then how long it was down  OK")


async def _metrics():
    from ser2netweb.web.routes import system

    class State:
        config = AppConfig.from_dict({"mappings": [{"id": "m1", "name": "plc", "serial":
                                                    {"port": "/dev/ttyX"}}]})

        class supervisor:
            @staticmethod
            def all_status():
                return {"m1": {"state": "device-missing", "reconnects": 9,
                               "open_failures": 7, "device_lost": 2}}

    class Ctx:
        state = State
        render = None

    metrics = next(r.endpoint for r in system.routes(Ctx) if r.path == "/metrics")
    return (await metrics(None)).body.decode().splitlines()


def test_metrics():
    body = asyncio.run(_metrics())
    lbl = 'mapping="plc",id="m1"'
    for line in (f"ser2net_mapping_reconnects_total{{{lbl}}} 9",
                 f"ser2net_mapping_open_failures_total{{{lbl}}} 7",
                 f"ser2net_mapping_device_lost_total{{{lbl}}} 2"):
        assert line in body, body
    print("/metrics: reconnects as before, open failures and device losses apart  OK")


async def _wake():
    backoff = Backoff()
    stop = asyncio.Event()
    with mock.patch.object(Backoff, "pause", lambda self: 30.0):
        t0 = time.monotonic()
        waiting = asyncio.ensure_future(backoff.wait(stop))
        await asyncio.sleep(0.05)
        backoff.wake()
        assert await waiting is True and time.monotonic() - t0 < 1, "woken: try now"
        waiting = asyncio.ensure_future(backoff.wait(stop))
        await asyncio.sleep(0.05)
        stop.set()
        assert await waiting is False, "stopped"
    backoff.wake()  # (nothing waits: nothing happens)

    # the supervisor wakes the mappings waiting for their port when a port appears
    sup = Supervisor()
    woken = []

    def runner(name, state):
        r = types.SimpleNamespace(status=types.SimpleNamespace(state=state))
        r.wake_serial = lambda: woken.append(name)
        return r

    sup._runners = {"a": runner("a", "device-missing"), "b": runner("b", "reconnecting"),
                    "c": runner("c", "running"), "d": runner("d", "stopped")}
    ports = [{"device": "/dev/ttyS0", "by_id": None}]
    sup.ports_changed(ports)
    woken.clear()
    sup.ports_changed([])  # a port went away: nothing to try
    assert woken == [], woken
    sup.ports_changed([{"device": "/dev/ttyUSB0", "by_id": "/dev/serial/by-id/usb-x"}])
    assert sorted(woken) == ["a", "b"], woken


async def _replug_wakes(folder):
    link = os.path.join(folder, "port")
    m = MappingConfig.from_dict({"name": "raw", "serial": {"port": link},
                                 "network": {"mode": "client", "protocol": "raw",
                                             "remote_host": "127.0.0.1", "remote_port": 9}})
    runner = MappingRunner(m)
    sup = Supervisor()
    sup._runners = {m.id: runner}
    pty = Pty()
    task = None
    try:
        with mock.patch.object(Backoff, "pause", lambda self: 30.0):
            task = asyncio.ensure_future(runner._link.run(runner._serial_session))
            await until(lambda: runner.status.open_failures == 1)
            os.symlink(pty.path, link)  # plugged in; the next try is 30 s away
            await asyncio.sleep(0.1)
            assert runner.status.state == "device-missing"
            t0 = time.monotonic()
            sup.ports_changed([{"device": pty.path, "by_id": None}])
            await until(lambda: runner.status.state == "running", timeout=2)
            assert time.monotonic() - t0 < 1, "opened at once, not after the backoff"
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await runner._link.close()
        pty.close()


def test_a_new_port_wakes_the_waiting_mappings():
    asyncio.run(_wake())
    if os.name == "posix":
        folder = tempfile.mkdtemp(prefix="s2n_retries_")
        try:
            asyncio.run(_replug_wakes(folder))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    else:
        print("SKIP (part): the PTY re-plug check is POSIX only")
    print("a port that appears: the mappings waiting for theirs try at once (a running one is "
          "left alone); a pause can be woken or stopped  OK")


async def _resolved_off_the_loop():
    from serial.tools import list_ports
    settings = SerialSettings.from_dict({"port": "/dev/ttyUSB0",
                                         "match": {"vid": "0403", "pid": "6001",
                                                   "serial_number": "A1"}})
    loop_thread = threading.current_thread()
    calls = []

    def comports():
        calls.append(threading.current_thread())
        return [types.SimpleNamespace(device="/dev/ttyUSB5", vid=0x0403, pid=0x6001,
                                      serial_number="A1", location=None)]

    with mock.patch.object(list_ports, "comports", comports):
        portlist._remember([{"device": "/dev/ttyUSB7", "vid": "0403", "pid": "6001",
                             "serial_number": "A1", "location": None}])
        try:
            assert await serial_io._resolve(settings) == ("/dev/ttyUSB7", ["/dev/ttyUSB7"]) \
                and not calls, \
                "a fresh watcher list is used"
            taken, ports = portlist._recent
            portlist._recent = (taken - 60, ports)  # a minute old: not used
            assert (await serial_io._resolve(settings))[0] == "/dev/ttyUSB5"
            assert calls and loop_thread not in calls, "enumerated in a thread"
        finally:
            portlist._recent = (0.0, [])
    plain = SerialSettings(port="/dev/ttyACM0")
    assert await serial_io._resolve(plain) == ("/dev/ttyACM0", [])


def test_usb_identity_resolved_off_the_event_loop():
    asyncio.run(_resolved_off_the_loop())
    print("USB identity: found in the watcher's fresh list, else enumerated in a thread, "
          "never on the event loop  OK")


async def _start_keeps_the_serial_state():
    from ser2netweb.engine.modbus_gateway import ModbusGatewayRunner

    async def missing(settings):
        raise serial_io_error()

    def serial_io_error():
        import errno

        import serial
        return serial.SerialException(errno.ENOENT, "could not open port /dev/ttyNOPE: "
                                                    "[Errno 2] No such file or directory")

    cases = [("udp", "raw", MappingRunner), ("server", "raw", MappingRunner),
             ("server", "modbus", ModbusGatewayRunner)]
    with mock.patch.object(serial_io, "open_serial", missing), fast_retries():
        for mode, protocol, cls in cases:
            m = MappingConfig.from_dict({
                "name": f"{mode}-{protocol}", "serial": {"port": "/dev/ttyNOPE"},
                "network": {"mode": mode, "protocol": protocol, "bind_ip": "127.0.0.1",
                            "port": 0}})
            runner = cls(m)
            try:
                await runner.start()
                await until(lambda: runner.status.open_failures >= 1)
                for _ in range(10):
                    assert runner.status.state == "device-missing", (mode, runner.status)
                    await asyncio.sleep(0.02)
            finally:
                await runner.stop()


def test_start_keeps_the_serial_state():
    asyncio.run(_start_keeps_the_serial_state())
    print("start(): a serial port that fails at once stays device-missing (UDP, TCP server, "
          "Modbus gateway)  OK")


def test_bridge_opens_the_missing_side_first():
    if os.name != "posix":
        print("SKIP (part): the PTY checks are POSIX only")
        return
    folder = tempfile.mkdtemp(prefix="s2n_retries_")
    try:
        asyncio.run(_bridge_side_missing(folder))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("serial<->serial bridge: with B missing A is opened once (then again only once B "
          "is there), with A missing B is never opened  OK")


if __name__ == "__main__":
    test_failure_log()
    test_states_and_counters()
    test_metrics()
    test_start_keeps_the_serial_state()
    test_a_new_port_wakes_the_waiting_mappings()
    test_usb_identity_resolved_off_the_event_loop()
    test_bridge_opens_the_missing_side_first()
    print("\nPASS: how a mapping keeps trying a serial port it can't open")
