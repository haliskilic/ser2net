"""A listener that can't take its address is tried again, and trying opens nothing else.

  - a mapping's start binds its listener (TCP, UDP, Modbus gateway) before it opens the
    serial port, connects to the MQTT broker or opens its trace: a start that fails to
    bind has done none of those (no DTR/RTS pulse, no broker reconnect per attempt);
  - such a failure is a BindError, whether the port is in use or the address is not on
    this machine (Python 3.11+ reports that without an errno);
  - the supervisor keeps the mapping in "error" with retrying / next_retry_s and starts
    it again after a backoff until the address can be used, logging the first failure, a
    changed error and the start that worked; any other start error is not retried;
  - Stop, a new apply, removing the mapping and stop_all() end the retry (a Stop is not
    undone later);
  - a mapping that failed at start-up is notified once (start_failed), like a later one;
  - end to end: a server whose mapping's port is taken at boot shows it as "error,
    retrying in N s" on the dashboard and in /api/v1/status, and the mapping comes up
    once the port is free.
Every listener is on 127.0.0.1 with an OS-assigned port (or the port of a socket the test
holds), so it runs on any OS.

Run: python3 tests/test_listener_retry.py
"""
import asyncio
import os
import socket
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import Browser, Server

from app import notify
from app.config import MappingConfig
from app.engine import mqtt_pub, netguard, serial_io, supervisor
from app.engine.bridge import MappingRunner
from app.engine.modbus_gateway import ModbusGatewayRunner
from app.engine.serial_link import Backoff
from app.engine.supervisor import Supervisor
from app.web.auth import hash_password

MISSING_IP = "192.0.2.1"  # TEST-NET-1: never an address of this machine


def held_port(kind=socket.SOCK_STREAM):
    """A socket on 127.0.0.1 holding an OS-assigned port (TCP: listening)."""
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    if kind == socket.SOCK_STREAM:
        s.listen()
    return s, s.getsockname()[1]


def mapping(port, mode="server", protocol="raw", bind_ip="127.0.0.1", **extra):
    return MappingConfig.from_dict(dict({
        "name": f"m-{mode}-{protocol}", "enabled": True,
        "serial": {"port": "/dev/ttyNOPE-retry"},
        "network": {"mode": mode, "protocol": protocol, "bind_ip": bind_ip, "port": port},
    }, **extra))


class Opened:
    """Counts what a start opened: serial ports (open_serial, which then waits as if the
    port were open) and MQTT broker connections."""

    def __init__(self):
        self.serial = 0
        self.mqtt = 0

    async def open_serial(self, settings):
        self.serial += 1
        await asyncio.Event().wait()

    def publisher(self, *a, **k):
        counter = self

        class FakePublisher:
            def connect(self):
                counter.mqtt += 1

            async def aclose(self):
                pass

            def feed(self, data):
                pass

        return FakePublisher()


async def _bind_first():
    opened = Opened()
    holder, port = held_port()
    udp_holder, udp_port = held_port(socket.SOCK_DGRAM)
    mqtt = {"mqtt": {"enabled": True, "host": "127.0.0.1", "port": 1}}
    cases = [
        ("tcp, port in use", MappingRunner(mapping(port, **mqtt), logger=lambda m: None)),
        ("tcp, address not here", MappingRunner(mapping(0, bind_ip=MISSING_IP, **mqtt),
                                                logger=lambda m: None)),
        ("udp, port in use", MappingRunner(mapping(udp_port, mode="udp", **mqtt),
                                           logger=lambda m: None)),
        ("udp, address not here", MappingRunner(mapping(0, mode="udp", bind_ip=MISSING_IP),
                                                logger=lambda m: None)),
        ("modbus, port in use", ModbusGatewayRunner(mapping(port, protocol="modbus"),
                                                    logger=lambda m: None)),
    ]
    try:
        with mock.patch.object(serial_io, "open_serial", opened.open_serial), \
                mock.patch.object(mqtt_pub, "MqttPublisher", opened.publisher):
            for label, runner in cases:
                try:
                    await runner.start()
                except netguard.BindError as e:
                    assert isinstance(e, OSError) and str(e), (label, e)
                else:
                    raise AssertionError(f"{label}: start() did not fail")
                await asyncio.sleep(0.05)
                assert (opened.serial, opened.mqtt) == (0, 0), (label, vars(opened))
                assert runner._serial_task is None and runner._tracer is None, label
            # once the port is free the same runner starts, and only then opens the rest
            holder.close()
            runner = cases[0][1]
            await runner.start()
            await asyncio.sleep(0.05)
            assert (opened.serial, opened.mqtt) == (1, 1), vars(opened)
            assert runner.status.state == "running", runner.status.as_dict()
            await runner.stop()
    finally:
        holder.close()
        udp_holder.close()
    print("a start that can't bind (in use / address not here; TCP, UDP, Modbus) raises "
          "BindError and opened no serial port, broker connection or trace  OK")


def test_bind_first():
    asyncio.run(_bind_first())


async def _wait_for(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            return False
        await asyncio.sleep(0.05)
    return True


async def _retry():
    opened = Opened()
    lines = []
    sup = Supervisor(logger=lines.append)
    holder, port = held_port()
    m = mapping(port)
    try:
        with mock.patch.object(serial_io, "open_serial", opened.open_serial):
            ok, msg = await sup.apply_mapping(m)
            assert not ok and msg, msg
            st = sup.status(m.id)
            assert st["state"] == "error" and st["retrying"] is True, st
            assert 0 <= st["next_retry_s"] <= 1, st
            assert lines == [f"[{m.name}] start failed: {msg} — trying again until the "
                             f"address can be used (every 30 s at most)"], lines
            # a few failed tries later: still error, logged once, nothing opened
            await asyncio.sleep(2.0)
            assert sup.status(m.id)["state"] == "error" and len(lines) == 1, (lines, sup.status(m.id))
            assert opened.serial == 0, vars(opened)
            holder.close()  # the port is free: the next try starts it
            assert await _wait_for(lambda: sup.status(m.id)["state"] == "running"), \
                (lines, sup.status(m.id))
            st = sup.status(m.id)
            assert st["retrying"] is False and st["next_retry_s"] == 0 and not st["last_error"], st
            assert lines[-1].startswith(f"[{m.name}] started after "), lines
            assert opened.serial == 1 and m.id not in sup._retries
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass
            await sup.stop_all()
    finally:
        holder.close()
    print("supervisor: a mapping whose port is busy stays 'error' (retrying), is logged "
          "once and starts by itself once the port is free  OK")


def test_retry():
    asyncio.run(_retry())


async def _retry_ends():
    opened = Opened()
    lines = []
    sup = Supervisor(logger=lines.append)
    holders = [held_port() for _ in range(4)]
    try:
        with mock.patch.object(serial_io, "open_serial", opened.open_serial):
            stop, apply_, remove, all_ = (mapping(p) for _s, p in holders)
            for m in (stop, apply_, remove, all_):
                ok, _msg = await sup.apply_mapping(m)
                assert not ok and m.id in sup._retries
            await sup.stop_mapping(stop.id)  # the user's Stop
            assert stop.id not in sup._retries
            st = sup.status(stop.id)
            assert st["state"] == "stopped" and st["retrying"] is False, st
            await sup.remove_mapping(remove.id)
            assert remove.id not in sup._retries and sup.status(remove.id) is None
            apply_.enabled = False
            await sup.apply_mapping(apply_)  # applied again, disabled now
            assert apply_.id not in sup._retries
            for s, _p in holders:
                s.close()  # every port is free now: only the one still retried comes up
            assert await _wait_for(lambda: sup.status(all_.id)["state"] == "running")
            await asyncio.sleep(1.0)
            assert sup.status(stop.id)["state"] == "stopped", sup.status(stop.id)
            assert sup.status(apply_.id)["state"] == "stopped", sup.status(apply_.id)
            assert opened.serial == 1, vars(opened)
            await sup.stop_all()
            assert not sup._retries

            # stop_all() while one is retrying ends it as well
            holder, port = held_port()
            m = mapping(port)
            await sup.apply_mapping(m)
            assert m.id in sup._retries
            await sup.stop_all()
            assert not sup._retries
            holder.close()
            await asyncio.sleep(1.5)
            assert opened.serial == 1, "a retry outlived stop_all()"

            # an error other than the bind (a TLS file gone) is not retried
            bad = mapping(0, network={"mode": "server", "protocol": "raw",
                                      "bind_ip": "127.0.0.1", "port": 0, "tls": True,
                                      "tls_cert": "/no/cert.pem", "tls_key": "/no/key.pem"})
            ok, _msg = await sup.apply_mapping(bad)
            assert not ok and bad.id not in sup._retries
            assert sup.status(bad.id)["retrying"] is False
            await sup.stop_all()
    finally:
        for s, _p in holders:
            s.close()
    print("supervisor: Stop, a new apply, remove and stop_all() end a bind retry; other "
          "start errors are not retried  OK")


def test_retry_ends():
    asyncio.run(_retry_ends())


async def _changed_error_and_cancel_mid_attempt():
    lines = []
    sup = Supervisor(logger=lines.append)
    m = mapping(4242)
    calls = []

    class Flaky(MappingRunner):
        async def start(self):
            calls.append(len(calls))
            await asyncio.sleep(0.3)  # a bind that takes a moment
            raise netguard.BindError(f"try {min(len(calls), 3)}")

    with mock.patch.object(supervisor, "make_runner",
                           lambda mm, log, trace_dir=None: Flaky(mm, logger=log)), \
            mock.patch.object(supervisor, "BIND_RETRY_MAX_S", 0.5):
        await sup.apply_mapping(m)
        assert await _wait_for(lambda: len(calls) >= 4, 10)
        changed = [line for line in lines if "start failed again" in line]
        assert changed == [f"[{m.name}] start failed again: try 2",
                           f"[{m.name}] start failed again: try 3"], lines
        # cancelled while an attempt is under way: the stop waits for it to end
        assert await _wait_for(lambda: sup.status(m.id)["next_retry_s"] == 0
                               and len(calls) >= 5, 10)
        await sup.stop_mapping(m.id)
        assert m.id not in sup._retries and sup.status(m.id)["state"] == "stopped"
        n = len(calls)
        await asyncio.sleep(1.0)
        assert len(calls) == n, "tried again after the stop"
    print("supervisor: only a changed error is logged again; a stop during an attempt "
          "waits for it and ends the retries  OK")


def test_changed_error_and_cancel_mid_attempt():
    asyncio.run(_changed_error_and_cancel_mid_attempt())


def test_boot_failure_is_notified_once():
    names = {"a": "A", "b": "B"}
    boot = {"a": {"state": "error", "last_error": "could not bind on any address", "clients": []},
            "b": {"state": "running", "clients": []}}
    prev = notify.baseline(boot)
    assert "a" not in prev and "b" in prev, prev
    events = notify.status_events(prev, boot, names)
    assert events == [("start_failed", "a", "A", "could not bind on any address")], events
    assert notify.status_events(notify.snapshot(boot), boot, names) == [], "only once"
    # a new mapping that fails to start is reported too; a new running one is not
    new = {"c": {"state": "error", "last_error": "in use", "clients": []},
           "d": {"state": "running", "clients": []}}
    events = notify.status_events({}, new, {"c": "C", "d": "D"})
    assert events == [("start_failed", "c", "C", "in use")], events
    print("notifications: a mapping that failed at start-up (or a new one that fails) "
          "sends start_failed once  OK")


def test_backoff_cap():
    b = Backoff(cap=30.0)
    pauses = [round(b.pause(), 2) for _ in range(9)]
    assert pauses == [0.6, 1.2, 2.3, 4.4, 8.0, 16.1, 30.2, 30.3, 30.4], pauses
    b = Backoff()
    assert max(round(b.pause(), 2) for _ in range(9)) == 10.4, "the serial side keeps 10 s"
    print("backoff: 0.5 s doubling, capped (30 s for a bind, 10 s by default)  OK")


def test_dashboard_shows_the_retry():
    holder, port = held_port()
    pw = "retry-pass-1"
    users = [{"username": "admin", "password_hash": hash_password(pw), "role": "admin",
              "pwd_version": 1}]
    m = {"id": "retry0000001", "name": "Busy port", "enabled": True,
         "serial": {"port": "/dev/ttyNOPE-retry"},
         "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                     "port": port}}
    try:
        with Server({"users": users, "mappings": [m]}) as srv:
            b = Browser(srv.base)
            b.login("admin", pw)
            st, _h, body = b.get("/api/status")
            assert st == 200 and "state-error" in body, body[-2000:]
            assert "retrying in" in body or "retrying now" in body, body[-2000:]
            holder.close()
            end = time.monotonic() + 20
            while True:
                st, _h, body = b.get("/api/status")
                if "retrying" not in body and "state-error" not in body:
                    break
                assert time.monotonic() < end, body[-2000:]
                time.sleep(0.3)
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass
            with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
                log = fh.read()
            assert log.count("[Busy port] start failed:") == 1, log[-3000:]
            assert "[Busy port] started after " in log, log[-3000:]
    finally:
        holder.close()
    print("server: a mapping whose port is taken at boot shows 'retrying in N s' and comes "
          "up by itself once the port is free  OK")


if __name__ == "__main__":
    test_backoff_cap()
    test_bind_first()
    test_retry()
    test_retry_ends()
    test_changed_error_and_cancel_mid_attempt()
    test_boot_failure_is_notified_once()
    test_dashboard_shows_the_retry()
    print("\nPASS: listeners that can't bind are retried; a failed bind opens nothing")
