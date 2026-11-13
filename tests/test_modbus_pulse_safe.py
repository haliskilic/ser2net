"""Modbus points from MQTT: a coil's pulse (pulse_ms) and safe values (safe_value /
safe_after_s).

A gateway started by its own start() against the fake RTU slave and the fake MQTT client of
tests/test_modbus_mqtt_write.py (no broker, no hardware). Checks:
  * pulse: a write of true to a coil with pulse_ms is followed by a write of false after
    the pulse, with a request of its own: the bus is free meanwhile (the poll goes on), the
    read-back publishes 1, then 0, and the log names both writes; a second true starts the
    pulse over (one false follows), a false ends it (none follows); a write of true whose
    reply did not come is followed by false all the same, a slave's own exception is not;
  * a mapping that stops in the middle of a pulse writes false before its port closes
    (at once, not after the pulse), says so and publishes the 0 before its MQTT connection
    closes; the same for a Sparkplug DCMD;
  * safe values: once the broker connection has been away for a point's safe_after_s its
    safe_value is written, once, through the write queue (logged with why; the point's
    read-back follows); not when the broker is back sooner; again at the next outage; from
    the mapping's start too when the broker never comes; a write that fails is tried
    again; the mapping's status has it (safe: waiting, applied, ok) and the dashboard row
    and the notifier (safe_values, opt-in) report it;
  * Sparkplug B with a primary host goes by that host's STATE, not by the broker;
  * a read-only mapping never writes a safe value (and says so at start);
  * the edit rules (ModbusPoll.validate_input) refuse what can't work, validate() (a stored
    config) refuses none of it, and a stored value the edits refuse is ignored with a
    warning in the log.

Run: python3 tests/test_modbus_pulse_safe.py
"""
import asyncio
import json
import time
from contextlib import suppress

import test_modbus_mqtt_write as base
from test_modbus_mqtt_write import COILS, REGS, SEEN, FakeMqtt, last, started, wait

from ser2netweb import notify
from ser2netweb.config import (
    NOTIFY_DEFAULT_EVENTS, NOTIFY_EVENTS, ConfigError, MappingConfig, ModbusPoint,
)
from ser2netweb.engine import modbus, sparkplug
from ser2netweb.engine import modbus_gateway as gw
from ser2netweb.web.routes.forms import (
    _build_mapping_dict, _modbus_points_json, _preserve_unmanaged_fields,
)

ON, OFF = bytes([5, 0, 0, 0xFF, 0]), bytes([5, 0, 0, 0, 0])


def pump(**more):
    return dict({"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool",
                 "writable": True}, **more)


SETPOINT = {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16",
            "writable": True}


def reset():
    SEEN.clear()
    COILS.update({0: False, 1: False, 2: False})
    REGS.update({10: 0})
    base.WRITE_TAKES[0] = 0.0
    base.ON_REQUEST.clear()


async def connack(fake):
    fake.on_connect(fake, None, {}, 0)
    await asyncio.sleep(0.05)  # (the publisher handles it on the loop)


async def lost(fake):
    fake.on_disconnect(fake, None, 7)  # (paho 1.x: MQTT_ERR_CONN_LOST)
    await asyncio.sleep(0.05)


async def test_pulse(port):
    reset()
    fake, logs = FakeMqtt(), []
    async with started(port, fake, points=[pump(pulse_ms=300)], logs=logs) as r:
        await connack(fake)
        assert await wait(lambda: last(fake, "plant/a/pump") == b"0")
        SEEN.clear()
        fake.deliver("plant/a/pump/set", b"1")
        assert await wait(lambda: last(fake, "plant/a/pump/set/result") == b"ok")
        began = time.monotonic()
        assert COILS[0] is True and await wait(lambda: last(fake, "plant/a/pump") == b"1")
        assert "pump" in r._pulses
        assert await wait(lambda: OFF in SEEN), SEEN
        took = time.monotonic() - began
        assert 0.2 < took < 1.5, took
        assert COILS[0] is False and await wait(lambda: last(fake, "plant/a/pump") == b"0")
        between = SEEN[SEEN.index(ON) + 1:SEEN.index(OFF)]
        assert sum(p[0] == 1 for p in between) >= 2, "the poll goes on during the pulse"
        assert await wait(lambda: "pump" not in r._pulses)
        assert any("wrote 'pump' = True" in m and "a pulse: off again in 300 ms" in m
                   for m in logs), logs
        assert any("wrote 'pump' = False (unit 1, FC5; pulse of 300 ms over)" in m
                   for m in logs), logs
        print("pulse: true, then false 300 ms later with a request of its own; the poll "
              "goes on meanwhile; 1 then 0 published, both writes logged  OK")

        SEEN.clear()
        fake.deliver("plant/a/pump/set", b"1")
        assert await wait(lambda: SEEN.count(ON) == 1)
        await asyncio.sleep(0.15)
        fake.deliver("plant/a/pump/set", b"1")  # again: the pulse starts over
        assert await wait(lambda: SEEN.count(ON) == 2)
        again = time.monotonic()
        assert await wait(lambda: OFF in SEEN)
        assert time.monotonic() - again > 0.2
        await asyncio.sleep(0.5)
        assert SEEN.count(OFF) == 1 and COILS[0] is False, SEEN
        SEEN.clear()
        fake.deliver("plant/a/pump/set", b"1")
        assert await wait(lambda: SEEN.count(ON) == 1 and "pump" in r._pulses)
        fake.deliver("plant/a/pump/set", b"0")  # switched off by a command
        assert await wait(lambda: SEEN.count(OFF) == 1 and "pump" not in r._pulses)
        await asyncio.sleep(0.5)
        assert SEEN.count(OFF) == 1, "no second false after the command's own"
        print("pulse: a second true starts it over (one false follows), a false ends it "
              "(none follows)  OK")

        # the slave carries the write out, but its reply comes after the response timeout
        r._resp_timeout = 0.3
        SEEN.clear()
        logs.clear()

        def slow_on(pdu):
            base.WRITE_TAKES[0] = 0.5 if pdu == ON else 0.0
        base.ON_REQUEST.append(slow_on)
        fake.deliver("plant/a/pump/set", b"1")
        assert await wait(lambda: (last(fake, "plant/a/pump/set/result") or b"").startswith(
            b"error: no reply"))
        assert any("pulse 'pump': the write of true was sent but not confirmed" in m
                   for m in logs), logs
        assert await wait(lambda: COILS[0] is True, 1.0)  # (it was carried out, late)
        assert await wait(lambda: COILS[0] is False and "pump" not in r._pulses, 5.0), logs
        base.ON_REQUEST.clear()
        base.WRITE_TAKES[0] = 0.0
        print("pulse: a true whose reply did not come is followed by false all the same  OK")
    assert gw._slave_exception(bytes([0x85, 2])) and not gw._slave_exception(
        bytes([0x85, modbus.GATEWAY_TARGET_FAILED])) and not gw._slave_exception(b"")


async def test_pulse_again_while_off_waits(port):
    """A second true that holds the bus when the first pulse ends: the first pulse's false,
    which waits for the bus behind it, is dropped (it would switch the coil off right after
    the second true); the second pulse's false follows a whole pulse later."""
    reset()
    fake, logs = FakeMqtt(), []
    times = []

    def slow_on(pdu):
        times.append((time.monotonic(), pdu))
        base.WRITE_TAKES[0] = 0.6 if pdu == ON else 0.0
    async with started(port, fake, points=[pump(pulse_ms=1000)], logs=logs) as r:
        await connack(fake)
        base.ON_REQUEST.append(slow_on)
        try:
            fake.deliver("plant/a/pump/set", b"1")
            assert await wait(lambda: last(fake, "plant/a/pump/set/result") == b"ok")
            # the first pulse ends 1 s after this; the second true goes on the bus 0.7 s
            # after it and holds the bus for 0.6 s, over the end of the first pulse
            await asyncio.sleep(0.7)
            fake.deliver("plant/a/pump/set", b"1")
            assert await wait(lambda: sum(p == ON for _t, p in times) == 2)
            second = next(t for t, p in reversed(times) if p == ON) + 0.6  # (its reply)
            assert await wait(lambda: any(p == OFF for _t, p in times), 4.0), times
            await asyncio.sleep(0.5)
            offs = [t for t, p in times if p == OFF]
            assert len(offs) == 1 and offs[0] - second > 0.7, (
                [round(t - second, 2) for t in offs], logs)
            assert COILS[0] is False and not r._pulses and not r._pulse_tasks
        finally:
            base.ON_REQUEST.clear()
            base.WRITE_TAKES[0] = 0.0
    print("pulse: a second true over the end of the first pulse: the first false is dropped, "
          "one false follows a whole pulse later  OK")


async def test_pulse_off_retried(port):
    """A false that can't be written (the serial port is down at the end of the pulse) is
    tried again until it is written, not given up after a few tries; at a stop with the
    port still down it is given up after a few tries, and the log says the coil may still
    be on."""
    reset()
    fake, logs = FakeMqtt(), []
    later, gw.PULSE_RETRY_LATER_S = gw.PULSE_RETRY_LATER_S, 0.4
    try:
        async with started(port, fake, points=[pump(pulse_ms=200)], logs=logs) as r:
            await connack(fake)
            fake.deliver("plant/a/pump/set", b"1")
            assert await wait(lambda: COILS[0] is True and "pump" in r._pulses)
            r._link.down = True  # (as while the port is being opened again)
            assert await wait(lambda: any("false could not be written" in m for m in logs),
                              4.0), logs
            assert any("the coil may still be ON. Tried again every 0.4 s" in m for m in logs)
            await asyncio.sleep(1.0)  # (more tries, every 0.4 s)
            assert COILS[0] is True and "pump" in r._pulses
            r._link.down = False
            assert await wait(lambda: COILS[0] is False and not r._pulses, 3.0), logs
            assert any("wrote 'pump' = False (unit 1, FC5; pulse of 200 ms over, after "
                       in m and "failed tries)" in m for m in logs), logs

            logs.clear()
            fake.deliver("plant/a/pump/set", b"1")
            assert await wait(lambda: COILS[0] is True and "pump" in r._pulses)
            r._link.down = True
            began = time.monotonic()
        took = time.monotonic() - began
        assert took < 5.0, took
        assert COILS[0] is True, "the port was down: nothing could be written"
        assert any("error: pulse 'pump': the mapping stopped before false could be written "
                   "(unit 1, 3 tries:" in m and "the coil may still be ON" in m
                   for m in logs), logs
        assert not r._pulses and not r._pulse_tasks
    finally:
        gw.PULSE_RETRY_LATER_S = later
    print("pulse: a false that can't be written is tried again until it is; a stop gives it "
          "a few tries and says the coil may still be ON  OK")


async def test_pulse_survives_stop(port):
    for fmt in ("plain", "sparkplug_b"):
        reset()
        fake, logs = FakeMqtt(), []
        async with started(port, fake, fmt=fmt, points=[pump(pulse_ms=60000)], logs=logs) as r:
            await connack(fake)
            await asyncio.sleep(0.2)
            if fmt == "plain":
                fake.deliver("plant/a/pump/set", b"1")
            else:
                cmd = sparkplug.Payload(timestamp=1, seq=None, metrics=[sparkplug.Metric(
                    name="pump", datatype=sparkplug.DataType.Boolean, value=True)])
                fake.deliver("spBv1.0/ser2net/DCMD/Line_3_PLC/unit1",
                             sparkplug.encode_payload(cmd))
            assert await wait(lambda: COILS[0] is True and "pump" in r._pulses), (fmt, logs)
            began = time.monotonic()
        assert time.monotonic() - began < 3.0, "the stop does not wait for the pulse"
        assert COILS[0] is False and SEEN[-1] == OFF, (fmt, SEEN[-3:])
        if fmt == "plain":  # ... and says so on the point's topic before it goes offline
            topics = [t for t, _p in fake.published]
            assert last(fake, "plant/a/pump") == b"0", fake.published[-4:]
            assert fake.published[-1] == ("plant/a/status", b"offline"), fake.published[-3:]
            assert topics[-2] == "plant/a/pump", "the 0, then the offline status"
        assert any("wrote 'pump' = False (unit 1, FC5; the mapping is stopping)" in m
                   for m in logs), logs
        assert logs.index(next(m for m in logs if "the mapping is stopping" in m)) \
            < logs.index("modbus gateway stopped")
        assert not r._pulses
    print("pulse: a mapping stopped during a pulse writes false before its port closes "
          "(plain /set and Sparkplug DCMD)  OK")


SAFE_POINTS = [dict(SETPOINT, safe_value=7, safe_after_s=1),
               pump(safe_value=False, safe_after_s=1.5),
               {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"}]


def safe_writes():
    return [p for p in SEEN if p[:3] == bytes([6, 0, 10]) or p[:3] == bytes([5, 0, 0])]


async def test_safe_values(port):
    reset()
    fake, logs = FakeMqtt(), []
    async with started(port, fake, points=SAFE_POINTS, logs=logs) as r:
        st = r.status.as_dict()["safe"]
        assert st["state"] == "waiting" and st["watching"] == "broker" and st["since"], st
        assert any(m.startswith("safe values: 2 point(s) are written") for m in logs), logs
        await connack(fake)
        assert r.status.as_dict()["safe"]["state"] == "ok"
        assert r.status.as_dict()["safe"]["since"] is None
        fake.deliver("plant/a/setpoint/set", b"55")
        fake.deliver("plant/a/pump/set", b"1")
        assert await wait(lambda: REGS[10] == 55 and COILS[0] is True)
        assert await wait(lambda: last(fake, "plant/a/pump/set/result") == b"ok")
        await asyncio.sleep(1.8)
        assert REGS[10] == 55 and COILS[0] is True, "nothing while the broker is there"

        before = notify.snapshot({"m": r.status.as_dict()})
        SEEN.clear()
        await lost(fake)
        down = time.monotonic()
        assert await wait(lambda: r.status.as_dict()["safe"]["state"] == "waiting")
        assert await wait(lambda: REGS[10] == 7, 3.0), logs
        assert 0.8 < time.monotonic() - down < 2.0
        assert COILS[0] is True, "the pump's 1.5 s are not over"
        assert await wait(lambda: COILS[0] is False, 3.0)
        assert await wait(lambda: r.status.as_dict()["safe"]["state"] == "applied")
        assert time.monotonic() - down > 1.4
        st = r.status.as_dict()["safe"]
        assert [(p["name"], p["value"], p["after_s"], bool(p["written"]), p["error"])
                for p in st["points"]] == [("setpoint", 7, 1.0, True, ""),
                                           ("pump", False, 1.5, True, "")], st
        assert any("wrote 'setpoint' = 7 (unit 1, FC6, from its safe value: no broker "
                   "for 1 s)" in m for m in logs), logs
        assert any("wrote 'pump' = False" in m and "no broker for 1.5 s" in m for m in logs)
        await asyncio.sleep(1.2)
        assert len(safe_writes()) == 2, ("written once per outage", safe_writes())
        events = notify.status_events(before, {"m": r.status.as_dict()}, {"m": "Line 3"})
        assert events == [("safe_values", "m:safe", "Line 3",
                           "setpoint = 7, pump = false (no broker for 1.5 s)")], events
        after = notify.snapshot({"m": r.status.as_dict()})
        assert notify.status_events(after, {"m": r.status.as_dict()}, {"m": "Line 3"}) == []
        print("safe values: written once the broker has been away for safe_after_s (each "
              "point its own), once, logged, in the status, a safe_values event  OK")

        await connack(fake)
        assert r.status.as_dict()["safe"]["state"] == "ok"
        assert any("safe values: the broker is back; 'setpoint', 'pump' keep(s)" in m
                   for m in logs), logs
        assert REGS[10] == 7, "the safe value stays until a command changes it"
        fake.deliver("plant/a/setpoint/set", b"55")
        assert await wait(lambda: REGS[10] == 55)
        SEEN.clear()
        await lost(fake)
        await asyncio.sleep(0.5)
        await connack(fake)  # back before the point's safe_after_s
        await asyncio.sleep(1.5)
        assert REGS[10] == 55 and not safe_writes(), "back in time: nothing is written"
        await lost(fake)
        assert await wait(lambda: REGS[10] == 7, 3.0), "the next outage writes it again"
        print("safe values: nothing when the broker is back in time; again at the next "
              "outage  OK")

    # the broker never comes after the start; a write that fails is tried again
    reset()
    fake, logs = FakeMqtt(), []
    retry, gw.SAFE_RETRY_S = gw.SAFE_RETRY_S, 0.4
    try:
        REGS[10] = 55
        async with started(port, fake, points=[dict(SETPOINT, safe_value=7, safe_after_s=1)],
                           logs=logs) as r:
            r._resp_timeout = 0.3

            def slow_first(pdu):
                first = pdu[0] == 6 and sum(p[0] == 6 for p in SEEN) == 0
                base.WRITE_TAKES[0] = 0.6 if first else 0.0
            base.ON_REQUEST.append(slow_first)
            assert await wait(lambda: r.status.as_dict()["safe"]["points"][0]["error"], 4.0)
            assert r.status.as_dict()["safe"]["state"] == "waiting"
            assert await wait(lambda: r.status.as_dict()["safe"]["state"] == "applied", 4.0)
            assert REGS[10] == 7 and r.status.as_dict()["safe"]["points"][0]["error"] == ""
            assert any("safe value of 'setpoint' not written (no reply" in m for m in logs)
    finally:
        gw.SAFE_RETRY_S = retry
        base.ON_REQUEST.clear()
        base.WRITE_TAKES[0] = 0.0
    assert r.status.as_dict()["safe"] == {}, "nothing once stopped"
    print("safe values: from the mapping's start when the broker never comes; a failed "
          "write is tried again  OK")

    # read-only: never
    reset()
    fake, logs = FakeMqtt(), []
    REGS[10] = 55
    async with started(port, fake, points=SAFE_POINTS, logs=logs,
                       network={"read_only": True}) as r:
        await asyncio.sleep(1.6)
        assert REGS[10] == 55 and not safe_writes() and r.status.as_dict()["safe"] == {}
        assert any(m.startswith("safe values: not written, the mapping is read-only")
                   for m in logs), logs
    print("safe values: a read-only mapping writes none, and says so  OK")


async def test_safe_values_primary_host(port):
    reset()
    fake, logs = FakeMqtt(), []
    REGS[10] = 55
    state = "spBv1.0/STATE/scada"
    async with started(port, fake, fmt="sparkplug_b",
                       points=[dict(SETPOINT, safe_value=7, safe_after_s=1)], logs=logs,
                       mqtt={"sparkplug_primary_host_id": "scada"}) as r:
        assert r.status.as_dict()["safe"]["watching"] == "primary host"
        await connack(fake)
        fake.deliver(state, json.dumps({"online": True, "timestamp": 1}).encode())
        assert await wait(lambda: r.status.as_dict()["safe"]["state"] == "ok")
        await asyncio.sleep(1.3)
        assert REGS[10] == 55
        SEEN.clear()
        fake.deliver(state, json.dumps({"online": False, "timestamp": 2}).encode())
        assert await wait(lambda: r.status.as_dict()["safe"]["state"] == "waiting")
        await connack(fake)  # the new session's CONNACK: the broker is there, the host is not
        assert await wait(lambda: REGS[10] == 7, 3.0), logs
        assert any("from its safe value: no primary host 'scada' for 1 s" in m for m in logs)
        events = notify.status_events({"m": ("running", set(), "", {}, ())},
                                      {"m": r.status.as_dict()}, {"m": "Line 3"})
        assert events == [("safe_values", "m:safe", "Line 3",
                           "setpoint = 7 (no primary host for 1 s)")], events
        fake.deliver(state, json.dumps({"online": True, "timestamp": 3}).encode())
        assert await wait(lambda: r.status.as_dict()["safe"]["state"] == "ok")
    # with the broker connected but the host never online, the timer runs from the start
    reset()
    fake = FakeMqtt()
    REGS[10] = 55
    async with started(port, fake, fmt="sparkplug_b",
                       points=[dict(SETPOINT, safe_value=7, safe_after_s=1)],
                       mqtt={"sparkplug_primary_host_id": "scada"}):
        await connack(fake)
        assert await wait(lambda: REGS[10] == 7, 3.0)
    print("safe values: a Sparkplug B node with a primary host goes by that host's STATE  OK")


def test_edit_rules():
    def mapping(point):
        return MappingConfig.from_dict({
            "name": "gw", "kind": "net", "serial": {"port": "/dev/ttyS0"},
            "network": {"mode": "server", "protocol": "modbus", "port": 1502},
            "mqtt": {"enabled": True, "host": "b", "base_topic": "t"},
            "modbus_poll": {"points": [point]}})

    good = [pump(pulse_ms=50), pump(pulse_ms=600000), pump(safe_value=False, safe_after_s=1),
            pump(pulse_ms=500, safe_value=False, safe_after_s=86400),
            dict(SETPOINT, safe_value=0, safe_after_s=30.5),
            dict(SETPOINT, min=0, max=10, safe_value=10, safe_after_s=5),
            dict(SETPOINT, allowed=[1, 2], safe_value=2, safe_after_s=5),
            dict(SETPOINT, poll=False, safe_value=0, safe_after_s=5)]
    for p in good:
        m = mapping(p)
        m.validate()
        m.validate_input()
    bad = [
        (pump(pulse_ms=49), "pulse_ms must be"), (pump(pulse_ms=600001), "pulse_ms must be"),
        (pump(pulse_ms=100.5), "pulse_ms must be"), (pump(pulse_ms="100"), "pulse_ms must be"),
        (pump(pulse_ms=True), "pulse_ms must be"),
        (dict(SETPOINT, pulse_ms=100), "pulse_ms is for a writable coil"),
        (dict(pump(pulse_ms=100), writable=False), "pulse_ms is for a writable coil"),
        (pump(safe_value=False), "belong together"), (pump(safe_after_s=5), "belong together"),
        (dict(pump(safe_value=False, safe_after_s=5), writable=False), 'needs "writable": true'),
        (pump(safe_value=0, safe_after_s=5), "a coil's safe_value is true or false"),
        (dict(SETPOINT, safe_value=True, safe_after_s=5), "safe_value must be a number"),
        (dict(SETPOINT, safe_value="0", safe_after_s=5), "safe_value must be a number"),
        (dict(SETPOINT, safe_value=float("nan"), safe_after_s=5), "safe_value must be a number"),
        (dict(SETPOINT, safe_value=0, safe_after_s=0.5), "safe_after_s must be"),
        (dict(SETPOINT, safe_value=0, safe_after_s=86401), "safe_after_s must be"),
        (dict(SETPOINT, safe_value=0, safe_after_s="5"), "safe_after_s must be"),
        (pump(pulse_ms=100, safe_value=True, safe_after_s=5), "can only be false"),
        (dict(SETPOINT, min=0, max=10, safe_value=11, safe_after_s=5), "outside the point's"),
        (dict(SETPOINT, min=1, safe_value=0, safe_after_s=5), "outside the point's"),
        (dict(SETPOINT, allowed=[1, 2], safe_value=3, safe_after_s=5), "outside the point's"),
        (pump(allowed=[True], safe_value=False, safe_after_s=5), "outside the point's"),
        (dict(SETPOINT, safe_value=40000, safe_after_s=5), "does not fit its type int16"),
        (dict(SETPOINT, scale=0.1, safe_value=4000, safe_after_s=5), "does not fit"),
        (dict(SETPOINT, dtype="uint16", safe_value=-1, safe_after_s=5), "does not fit"),
    ]
    for p, why in bad:
        m = mapping(p)
        m.validate()  # a stored config keeps loading
        try:
            m.validate_input()
        except ConfigError as e:
            assert why in str(e), (p, str(e))
        else:
            raise AssertionError(f"accepted: {p}")
        point = m.modbus_poll.points[0]
        assert gw._pulse_ms(point) is None or p.get("pulse_ms") == 100, p
    # stored values the edits refuse are not used
    assert gw._pulse_ms(ModbusPoint.from_dict(pump(pulse_ms=49))) is None
    assert gw._pulse_ms(ModbusPoint.from_dict(dict(SETPOINT, pulse_ms=100))) is None
    assert gw._pulse_ms(ModbusPoint.from_dict(pump(pulse_ms=100))) == 100
    assert gw._safe(ModbusPoint.from_dict(pump(safe_value=0, safe_after_s=5))) is None
    assert gw._safe(ModbusPoint.from_dict(dict(SETPOINT, safe_value=0, safe_after_s=0))) is None
    assert gw._safe(ModbusPoint.from_dict(dict(SETPOINT, safe_value=0))) is None
    assert gw._safe(ModbusPoint.from_dict(dict(SETPOINT, safe_value=0, safe_after_s=5))) \
        == (0, 5.0)
    # ... nor one write_point would refuse (it would be tried again and again)
    for p in (dict(SETPOINT, min=1, safe_value=0, safe_after_s=5),
              dict(SETPOINT, allowed=[1, 2], safe_value=3, safe_after_s=5),
              dict(SETPOINT, safe_value=40000, safe_after_s=5),
              pump(allowed=[True], safe_value=False, safe_after_s=5)):
        assert gw._safe(ModbusPoint.from_dict(p)) is None, p
    # defaults: nothing new for a point of 3.0, and the keys are stored
    plain = ModbusPoint.from_dict(pump())
    assert (plain.pulse_ms, plain.safe_value, plain.safe_after_s) == (None, None, None)
    stored = mapping(pump(pulse_ms=500, safe_value=False, safe_after_s=30)).to_dict()
    kept = stored["modbus_poll"]["points"][0]
    assert (kept["pulse_ms"], kept["safe_value"], kept["safe_after_s"]) == (500, False, 30)
    assert "safe_values" in NOTIFY_EVENTS and "safe_values" not in NOTIFY_DEFAULT_EVENTS
    print("edit rules: pulse_ms (a writable coil, 50-600000 ms), safe_value with "
          "safe_after_s (a writable point, its type, its limits, 1-86400 s); a stored "
          "config is never refused, a stored bad value is not used  OK")


def test_form_keys():
    """pulse_ms, safe_value and safe_after_s are in the points JSON when set; a form that
    can't carry them (a node's from before 3.1: no marker) keeps each point's stored ones
    by name, where they still fit the point as posted; this version's form: its JSON is
    what counts."""
    stored = MappingConfig.from_dict({
        "id": "abcdef012345", "name": "gw", "kind": "net", "serial": {"port": "COM7"},
        "network": {"mode": "server", "protocol": "modbus", "port": 502},
        "modbus_poll": {"points": [pump(pulse_ms=500, safe_value=False, safe_after_s=30),
                                   dict(SETPOINT, safe_value=7, safe_after_s=60),
                                   dict(SETPOINT, name="plain", address=11),
                                   pump(name="valve", address=1, pulse_ms=200)]}})
    shown = json.loads(_modbus_points_json(stored))
    assert (shown[0]["pulse_ms"], shown[0]["safe_value"], shown[0]["safe_after_s"]) \
        == (500, False, 30) and (shown[1]["safe_value"], shown[1]["safe_after_s"]) == (7, 60)
    assert not {"pulse_ms", "safe_value", "safe_after_s"} & set(shown[2]), "not set: not shown"

    def saved(points, **markers):
        form = {"id": stored.id, "name": "gw", "kind": "net", "enabled": "on",
                "serial_port": "COM7", "serial_baudrate": "9600", "network_mode": "server",
                "network_protocol": "modbus", "network_bind_ip": "0.0.0.0",
                "network_port": "502", "serial_usb_match_shown": "1",
                "modbus_point_options_shown": "1", "modbus_points": json.dumps(points),
                **markers}
        new = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(new, stored, form)
        new.validate()
        new.validate_input()
        return [(p.pulse_ms, p.safe_value, p.safe_after_s) for p in new.modbus_poll.points]

    older = [pump(), dict(SETPOINT), dict(SETPOINT, name="plain", address=11),
             pump(name="valve", address=1, writable=False)]  # (the valve: read-only now)
    assert saved(older) == [(500, False, 30), (None, 7, 60), (None, None, None),
                            (None, None, None)]
    assert saved(older, modbus_point_safety_shown="1") == [(None, None, None)] * 4
    edited = [pump(pulse_ms=250), dict(SETPOINT, safe_value=9, safe_after_s=5)]
    assert saved(edited, modbus_point_safety_shown="1") == [(250, None, None), (None, 9, 5)]
    print("form: pulse_ms / safe_value / safe_after_s shown when set, kept for a form from "
          "before 3.1 (where they still fit), else as posted  OK")


async def test_stored_bad_values(port):
    reset()
    fake, logs = FakeMqtt(), []
    points = [pump(pulse_ms=5), dict(SETPOINT, safe_value="x", safe_after_s=1),
              dict(SETPOINT, name="limited", address=11, min=10, safe_value=0,
                   safe_after_s=1)]
    async with started(port, fake, points=points, logs=logs) as r:
        await connack(fake)
        assert any("point 'pump': pulse_ms 5 ignored" in m for m in logs), logs
        assert any("point 'setpoint': safe_value 'x' / safe_after_s 1 ignored" in m
                   for m in logs), logs
        assert any("point 'limited': safe_value 0 / safe_after_s 1 ignored (a writable point, "
                   "a value of its type within its min / max / allowed" in m for m in logs)
        fake.deliver("plant/a/pump/set", b"1")
        assert await wait(lambda: COILS[0] is True)
        await asyncio.sleep(0.4)
        assert COILS[0] is True and not r._pulses and r.status.as_dict()["safe"] == {}
    print("a stored pulse_ms / safe value the edits refuse (also one outside the point's "
          "limits) is ignored, with a warning  OK")


async def main():
    test_edit_rules()
    test_form_keys()
    port = base.free_port()
    srv = await asyncio.start_server(base.fake_slave, "127.0.0.1", port)
    try:
        await test_pulse(port)
        await test_pulse_again_while_off_waits(port)
        await test_pulse_off_retried(port)
        await test_pulse_survives_stop(port)
        await test_safe_values(port)
        await test_safe_values_primary_host(port)
        await test_stored_bad_values(port)
    finally:
        srv.close()
        with suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=2)
    print("\nPASS: Modbus pulse_ms and safe values")


if __name__ == "__main__":
    asyncio.run(main())
