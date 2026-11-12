"""Edge mode: the health of each polled Modbus unit, and what MQTT / Sparkplug B makes of it.

A Modbus-gateway mapping polls a fake RTU slave (TCP loopback) that answers for units
1-3 and can be told to stay silent for a unit, or for one address (some slaves ignore a
request for a register they don't have), or to answer an address with an exception; the
MQTT client is a stand-in that records what is published. Checks:
  * a unit that gives no reply to any of its points in 3 polls in a row is offline: one
    log line (no line per point and cycle), a Sparkplug DDEATH for its device and its
    values dropped; from then on one point per cycle is read, each in turn, so it holds the
    bus for one response timeout per cycle; births (a rebirth, a reconnect) leave it out;
  * when it answers again all its points are read and it gets a DBIRTH with those values
    (one log line); DDATA then goes on as before;
  * a slave exception is an answer: a unit whose point gets 0x02 stays online, and the
    point is logged once (with the unit), not every cycle; a point the slave ignores on a
    unit that answers is logged once, its metric is null in a birth, and when it reads
    again that is logged and it is sent again;
  * the serial port gone for 3 polls: every polled unit offline (one line for all), DDEATH
    for each, nothing read meanwhile; back: each unit answers and is born again. A unit
    that was already silent stays offline for that reason;
  * metric timestamps are the read times: a point read after one that timed out is
    stamped that much later;
  * plain MQTT: an offline unit is polled once per cycle, nothing is published for it, and
    its values are published again when it answers; with the unit status option, each
    unit's state goes to <base_topic>/unit<N>/status (retained) on every change and after
    each reconnect, and without it there is no such topic;
  * Modbus/TCP masters are not affected: a request to an offline unit still goes out;
  * the status lists each unit (state, cause, failed reads) and /metrics has
    ser2net_modbus_unit_up and ser2net_modbus_unit_poll_errors_total per unit (a unit not
    polled yet has no _up line); the notifier sees unit_offline / unit_back.

Plain MQTT with value_format "json" (and retain): each value is {"value", "ts" (its read
time), "quality": "good"}, retained; a point whose read fails on a live unit and the points
of a unit that went offline are published once as quality "bad" (value null), and good
again once they read; a NaN reading is null; text values are unchanged.

Run: python3 tests/test_modbus_unit_health.py
"""
import asyncio
import socket
import struct
import time
from contextlib import suppress

from _server import ROOT  # noqa: F401  (sys.path setup)

from app import notify
from app.config import MappingConfig
from app.engine import modbus, sparkplug
from app.engine.modbus_gateway import UNIT_OFFLINE_AFTER, ModbusGatewayRunner
from app.engine.sparkplug_node import SparkplugNode

TIMEOUT = 0.15
REGS = {(1, 0): 0x41BC, (1, 1): 0, (1, 10): 5, (2, 30): 42, (2, 31): 43, (3, 40): 7}
SILENT_UNITS: set = set()
SILENT_ADDRS: set = set()    # (unit, address) the slave ignores
EXCEPTIONS: dict = {}        # (unit, address) -> exception code
SEEN: list = []              # (unit, pdu) of every request


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def fake_slave(reader, writer):
    buf = bytearray()
    with suppress(ConnectionError, OSError):
        while data := await reader.read(512):
            buf += data
            with suppress(ValueError):
                unit, pdu = modbus.rtu_unwrap(bytes(buf))
                buf.clear()
                SEEN.append((unit, pdu))
                fn, addr, qty = pdu[0], *struct.unpack(">HH", pdu[1:5])
                if unit in SILENT_UNITS or (unit, addr) in SILENT_ADDRS or unit == 0:
                    continue
                if (unit, addr) in EXCEPTIONS:
                    reply = bytes([fn | 0x80, EXCEPTIONS[(unit, addr)]])
                elif fn in (3, 4):
                    data = b"".join(struct.pack(">H", REGS.get((unit, addr + i), 0))
                                    for i in range(qty))
                    reply = bytes([fn, len(data)]) + data
                else:
                    reply = pdu
                writer.write(modbus.rtu_wrap(unit, reply))
                await writer.drain()


class FakeMqtt:
    def __init__(self):
        self.published, self.wills, self.subscribed = [], [], []
        self.on_message = None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, bytes(payload)))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload), retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


POINTS = [
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "ghost", "unit": 1, "fn": 3, "address": 99, "dtype": "uint16"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
    {"name": "level", "unit": 2, "fn": 3, "address": 30, "dtype": "uint16"},
    {"name": "level2", "unit": 2, "fn": 3, "address": 31, "dtype": "uint16"},
    {"name": "bad", "unit": 3, "fn": 3, "address": 40, "dtype": "uint16"},
    {"name": "all", "unit": 0, "fn": 3, "address": 50, "dtype": "uint16", "writable": True},
]


async def gateway(slave_port, fmt, logs, points=POINTS, **mqtt):
    m = MappingConfig.from_dict({
        "name": "Line 3 PLC", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                    "port": free_port()},
        "mqtt": dict({"enabled": True, "host": "broker", "base_topic": "plant/a",
                      "payload_format": fmt}, **mqtt),
        "modbus_poll": {"interval_s": 0.05, "points": points}})
    m.validate()
    r = ModbusGatewayRunner(m, logger=logs.append)
    r._resp_timeout = TIMEOUT
    sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
    r._link.attach(sr, sw)
    fake = FakeMqtt()
    if fmt == "sparkplug_b":
        r._sparkplug = SparkplugNode(m, logs.append, r._write_soon)
        r._mqtt = r._sparkplug.publisher()
    else:  # what start() makes
        from app.engine.mqtt_pub import MqttPublisher
        r._mqtt = MqttPublisher(m.mqtt, logger=logs.append, on_connect=(
            r._publish_unit_states if m.mqtt.unit_status else None))
    r._mqtt._client = fake
    r._mqtt.connect()
    r._mqtt.handle_connect()
    return r, sw, fake


async def wait(cond, t=8.0):
    for _ in range(int(t / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def topic(kind, dev=None):
    return sparkplug.topic("ser2net", kind, "Line_3_PLC", dev)


def sent(fake, kind, dev=None):
    return [sparkplug.decode_payload(p) for t, p, _r in fake.published if t == topic(kind, dev)]


def values(payload):
    return {m.name: (None if m.is_null else m.value) for m in payload.metrics}


def requests_to(unit):
    return [p for u, p in SEEN if u == unit]


async def metrics_of(runner):
    """/metrics, for a node that runs this one gateway."""
    from app.config import AppConfig
    from app.web.routes import system

    class State:
        config = AppConfig(mappings=[runner.mapping])

        class supervisor:
            @staticmethod
            def all_status():
                return {runner.mapping.id: runner.status.as_dict()}

    class Ctx:
        state = State
        render = None
    endpoint = next(r.endpoint for r in system.routes(Ctx) if r.path == "/metrics")
    return (await endpoint(None)).body.decode()


async def stop(r, sw, poll):
    r._stop.set()
    poll.cancel()
    with suppress(BaseException):
        await poll
    sw.close()


async def test_sparkplug_unit_health(slave_port):
    logs = []
    SILENT_UNITS.clear()
    SILENT_ADDRS.clear()
    EXCEPTIONS.clear()
    SILENT_ADDRS.add((1, 99))           # 'ghost': the slave ignores that register
    EXCEPTIONS[(3, 40)] = 0x02          # 'bad': illegal data address, every time
    r, sw, fake = await gateway(slave_port, "sparkplug_b", logs)
    r._report_units()  # what start() does
    assert [(u["unit"], u["state"]) for u in r.status.as_dict()["units"]] == [
        (1, "unknown"), (2, "unknown"), (3, "unknown")], "unit 0 is not polled"
    assert "ser2net_modbus_unit_up" not in await metrics_of(r)
    births = {d: sent(fake, "DBIRTH", d) for d in ("unit0", "unit1", "unit2", "unit3")}
    assert all(len(b) == 1 for b in births.values()), births
    poll = asyncio.create_task(r._poll_loop())
    try:
        assert await wait(lambda: sent(fake, "DDATA", "unit2") and sent(fake, "DDATA", "unit1"))
        first = values(sent(fake, "DDATA", "unit1")[0])
        assert first == {"temp": 23.5, "setpoint": 5}, first
        stamp = {m.name: m.timestamp for m in sent(fake, "DDATA", "unit1")[0].metrics}
        assert stamp["setpoint"] - stamp["temp"] >= TIMEOUT * 1000 * 0.9, \
            f"setpoint was read after the ghost timed out, but stamped {stamp}"
        print("metric timestamps are read times: the point after a timeout is stamped later  OK")

        # a slave exception is an answer; a point the slave ignores, on a unit that answers
        await asyncio.sleep(1.0)
        assert r._units[3].state == "online" and r._units[1].state == "online"
        assert not sent(fake, "DDEATH", "unit3") and not sent(fake, "DDEATH", "unit1")
        assert [m for m in logs if "'bad'" in m] == [
            "poll 'bad' (unit 3): modbus exception 0x02"], [m for m in logs if "bad" in m]
        assert [m for m in logs if "'ghost'" in m] == [
            f"poll 'ghost' (unit 1): no reply from unit 1 within {TIMEOUT:g} s"], logs
        print("exception 0x02 and an ignored register: the unit stays online, each point "
              "logged once (with its unit)  OK")

        # unit 2 stops answering
        names = {r.mapping.id: r.mapping.name}
        snap = notify.snapshot({r.mapping.id: r.status.as_dict()})
        SILENT_UNITS.add(2)
        seen = len(logs)
        assert await wait(lambda: sent(fake, "DDEATH", "unit2")), logs[seen:]
        death = sent(fake, "DDEATH", "unit2")[-1]
        assert death.metrics == [] and death.seq is not None, death
        assert [m for m in logs[seen:] if "unit 2" in m] == [
            f"unit 2: offline, no reply to any of its 2 points in {UNIT_OFFLINE_AFTER} polls "
            "in a row; polling one point per cycle until it answers"], logs[seen:]
        assert not any(m.startswith("poll 'level") for m in logs), logs
        assert r._units[2].state == "offline" and r._units[2].cause == "no reply"
        # from then on: one request per cycle, the two points in turn
        before = len(requests_to(2))
        cycles = r._units[2].probe
        await asyncio.sleep(1.2)
        probes = requests_to(2)[before:]
        assert 2 <= len(probes) <= r._units[2].probe - cycles + 1, probes
        assert len({p[1:3] for p in probes}) == 2, f"not each point in turn: {probes}"
        assert len(sent(fake, "DDEATH", "unit2")) == 1, "DDEATH once"
        assert sent(fake, "DDATA", "unit2")[-1].seq < death.seq, "no DDATA after the DDEATH"
        print(f"unit 2 silent: DDEATH after {UNIT_OFFLINE_AFTER} polls, one log line, then "
              "one probe per cycle (its points in turn)  OK")

        # the status (dashboard, REST API) and /metrics say so
        units = {u["unit"]: u for u in r.status.as_dict()["units"]}
        assert sorted(units) == [1, 2, 3], units
        assert (units[2]["state"], units[2]["cause"]) == ("offline", "no reply"), units[2]
        assert units[2]["errors"] >= 2 * UNIT_OFFLINE_AFTER and units[2]["since"] > 0
        assert units[1]["state"] == units[3]["state"] == "online", units
        assert units[3]["errors"] >= 1 and units[1]["errors"] >= 1, units
        body = await metrics_of(r)
        lbl = 'mapping="Line 3 PLC",id="{}",unit="{}"'
        for unit, up in ((1, 1), (2, 0), (3, 1)):
            assert f"ser2net_modbus_unit_up{{{lbl.format(r.mapping.id, unit)}}} {up}" in body, body
            assert any(line.startswith("ser2net_modbus_unit_poll_errors_total{"
                                       + lbl.format(r.mapping.id, unit) + "} ")
                       for line in body.splitlines()), body
        events = notify.status_events(snap, {r.mapping.id: r.status.as_dict()}, names)
        assert events == [("unit_offline", f"{r.mapping.id}:unit2", "Line 3 PLC", "unit 2")], events
        snap = notify.snapshot({r.mapping.id: r.status.as_dict()})
        print("status units (state, cause, errors), /metrics ser2net_modbus_unit_up / "
              "_poll_errors_total, a unit_offline notification  OK")

        # a rebirth and a reconnect leave it out
        r._sparkplug.birth()
        assert [len(sent(fake, "DBIRTH", d)) for d in ("unit0", "unit1", "unit2", "unit3")] \
            == [2, 2, 1, 2]
        unit1 = values(sent(fake, "DBIRTH", "unit1")[-1])
        assert unit1["ghost"] is None and unit1["temp"] == 23.5, unit1
        assert values(sent(fake, "DBIRTH", "unit3")[-1]) == {"bad": None}
        r._mqtt._paho_on_disconnect(fake, None)
        r._mqtt.handle_connect()
        assert len(sent(fake, "DBIRTH", "unit2")) == 1 and len(sent(fake, "DBIRTH", "unit1")) == 3
        print("births (rebirth, reconnect) leave the offline unit out; a failing point is "
              "null  OK")

        # it answers again, with new values: a DBIRTH with them, then DDATA as before
        REGS[(2, 30)], REGS[(2, 31)] = 100, 101
        seen = len(logs)
        SILENT_UNITS.discard(2)
        assert await wait(lambda: len(sent(fake, "DBIRTH", "unit2")) == 2), logs[seen:]
        assert values(sent(fake, "DBIRTH", "unit2")[-1]) == {"level": 100, "level2": 101}
        back = [m for m in logs[seen:] if "unit 2" in m]
        assert len(back) == 1 and back[0].startswith("unit 2 answers again (offline for "), back
        REGS[(2, 30)] = 102
        assert await wait(lambda: values(sent(fake, "DDATA", "unit2")[-1]) == {"level": 102})
        assert r._units[2].state == "online"
        events = notify.status_events(snap, {r.mapping.id: r.status.as_dict()}, names)
        assert [e[0] for e in events] == ["unit_back"], events
        print("unit 2 answers again: DBIRTH with the values just read, one log line, DDATA "
              "goes on  OK")

        # a point that fails for a while on a unit that answers, then reads again
        EXCEPTIONS[(1, 10)] = 0x04
        seen = len(logs)
        assert await wait(lambda: any("'setpoint'" in m for m in logs[seen:]))
        r._sparkplug.birth()
        assert values(sent(fake, "DBIRTH", "unit1")[-1])["setpoint"] is None, \
            "a failing point is not reported with its old value"
        del EXCEPTIONS[(1, 10)]
        assert await wait(lambda: "poll 'setpoint' reads again" in logs)
        assert await wait(lambda: values(sent(fake, "DDATA", "unit1")[-1]).get("setpoint") == 5)
        assert sum("'setpoint'" in m for m in logs[seen:]) == 2, logs[seen:]
        print("a point failing on a live unit: logged once, null in a birth, 'reads again' "
              "and sent again  OK")

        # the serial port gone for 3 polls: every polled unit offline, nothing read
        SILENT_UNITS.add(3)                       # unit 3 also stops answering first
        assert await wait(lambda: r._units[3].state == "offline")
        seen, asked = len(logs), len(SEEN)
        r._link.down = True
        assert await wait(lambda: sent(fake, "DDEATH", "unit1")), logs[seen:]
        assert len(SEEN) == asked, "read while the port was down"
        assert [m for m in logs[seen:] if "offline" in m] == [
            f"units 1, 2: offline, the serial port has been down for {UNIT_OFFLINE_AFTER} polls"
        ], logs[seen:]
        assert (r._units[1].cause, r._units[2].cause, r._units[3].cause) == \
            ("port down", "port down", "no reply")
        assert len(sent(fake, "DDEATH", "unit3")) == 1 and not sent(fake, "DDEATH", "unit0")
        births = [len(sent(fake, "DBIRTH", d)) for d in ("unit0", "unit1", "unit2", "unit3")]
        r._sparkplug.birth()
        assert [len(sent(fake, "DBIRTH", d)) for d in ("unit0", "unit1", "unit2", "unit3")] == \
            [births[0] + 1] + births[1:], "a birth while the port is down: unit 0 only"
        # back: units 1 and 2 answer and are born again; unit 3 stays silent
        r._link.down = False
        assert await wait(lambda: len(sent(fake, "DBIRTH", "unit1")) == births[1] + 1
                          and len(sent(fake, "DBIRTH", "unit2")) == births[2] + 1), births
        await asyncio.sleep(0.6)
        assert r._units[3].state == "offline" and r._units[3].cause == "no reply"
        assert not any("unit 3" in m for m in logs[seen:]), logs[seen:]
        print("port down for 3 polls: units offline (one line), DDEATH each, nothing read; "
              "back: born again; a silent unit stays offline  OK")

        # Modbus/TCP masters are not affected: a request to the offline unit goes out
        before = len(requests_to(3))
        rpdu = await r._transaction(3, modbus.read_pdu(3, 40, 1))
        assert rpdu == bytes([0x83, 0x0B]) and len(requests_to(3)) > before
        print("a master's request to an offline unit still goes on the bus  OK")
    finally:
        await stop(r, sw, poll)
        SILENT_UNITS.clear()
        SILENT_ADDRS.clear()
        EXCEPTIONS.clear()


async def test_plain_unit_health(slave_port):
    points = [p for p in POINTS if p["unit"] in (1, 2) and p["name"] != "ghost"]
    for unit_status in (False, True):
        logs = []
        r, sw, fake = await gateway(slave_port, "plain", logs, points, unit_status=unit_status)
        poll = asyncio.create_task(r._poll_loop())

        def level():
            return [p for t, p, _r in fake.published if t == "plant/a/level"]

        def states(unit):
            return [(p, retain) for t, p, retain in fake.published
                    if t == f"plant/a/unit{unit}/status"]
        try:
            assert await wait(lambda: level())
            SILENT_UNITS.add(2)
            assert await wait(lambda: r._units[2].state == "offline")
            published, before = len(level()), len(requests_to(2))
            await asyncio.sleep(0.8)
            assert len(level()) == published, "published for an offline unit"
            probes = len(requests_to(2)) - before
            assert 1 <= probes <= 8, probes
            SILENT_UNITS.discard(2)
            assert await wait(lambda: len(level()) > published)
            assert sum("unit 2" in m for m in logs) == 2, logs
            if not unit_status:
                assert not any("/unit" in t for t, _p, _r in fake.published), fake.published
                print("plain MQTT: an offline unit is probed once per cycle, nothing published "
                      "for it until it answers; no unit status topic unless asked for  OK")
                continue
            assert states(1) == [(b"online", True)], states(1)
            assert states(2) == [(b"online", True), (b"offline", True), (b"online", True)], \
                states(2)
            r._mqtt._paho_on_disconnect(fake, None)
            r._mqtt.handle_connect()               # reconnected: the states again
            assert states(1)[-1] == (b"online", True) and len(states(1)) == 2
            assert len(states(2)) == 4
            print("plain MQTT + unit status: retained <topic>/unit<N>/status online / offline on "
                  "each change and after a reconnect  OK")
        finally:
            await stop(r, sw, poll)
            SILENT_UNITS.clear()


def test_encode_value():
    from app.engine.modbus_gateway import encode_value
    assert encode_value(True, "text", 1) == b"1" and encode_value(False, "text", 1) == b"0"
    assert encode_value(21.5, "text", 1) == b"21.5" and encode_value(7, "csv", 1) == b"7"
    assert encode_value(True, "json", 5) == b'{"value":true,"ts":5,"quality":"good"}'
    assert encode_value(7, "json", 5) == b'{"value":7,"ts":5,"quality":"good"}'
    for odd in (float("nan"), float("inf")):
        assert encode_value(odd, "json", 5) == b'{"value":null,"ts":5,"quality":"good"}'
    assert encode_value(None, "json", 5, good=False) == b'{"value":null,"ts":5,"quality":"bad"}'
    print("value payloads: text 1 / 0 / number (also for an unknown format); JSON with "
          "true / false, null for NaN, quality  OK")


async def test_plain_json_values(slave_port):
    import json
    points = [p for p in POINTS if p["unit"] in (1, 2)]  # temp, ghost, setpoint, level, level2
    logs = []
    r, sw, fake = await gateway(slave_port, "plain", logs, points, value_format="json",
                                retain=True)
    poll = asyncio.create_task(r._poll_loop())

    def msgs(name):
        return [(json.loads(p), retain) for t, p, retain in fake.published
                if t == f"plant/a/{name}"]

    def quality(name):
        return [m["quality"] for m, _r in msgs(name)]
    try:
        t0 = int(time.time() * 1000)
        assert await wait(lambda: msgs("temp") and msgs("level"))
        (temp, kept), = msgs("temp")[:1]
        assert kept is True and temp["quality"] == "good" and temp["value"] == 23.5, temp
        assert t0 <= temp["ts"] <= int(time.time() * 1000), (t0, temp)
        assert msgs("level")[0][0]["value"] == REGS[(2, 30)], msgs("level")

        EXCEPTIONS[(1, 99)] = 0x02                    # 'ghost' fails, unit 1 answers
        assert await wait(lambda: "bad" in quality("ghost"))
        await asyncio.sleep(0.4)                      # several cycles: "bad" once
        assert quality("ghost").count("bad") == 1, quality("ghost")
        bad = msgs("ghost")[-1]
        assert bad == ({"value": None, "ts": bad[0]["ts"], "quality": "bad"}, True), bad
        EXCEPTIONS.pop((1, 99))
        assert await wait(lambda: quality("ghost")[-1] == "good")

        SILENT_UNITS.add(2)                           # unit 2 stops answering
        assert await wait(lambda: r._units[2].state == "offline")
        assert await wait(lambda: quality("level")[-1] == "bad" == quality("level2")[-1])
        await asyncio.sleep(0.4)
        assert quality("level").count("bad") == 1 and quality("level2").count("bad") == 1
        SILENT_UNITS.discard(2)
        assert await wait(lambda: quality("level")[-1] == "good" == quality("level2")[-1])
        assert all(q == "good" for q in quality("temp")), "unit 1 was fine all along"
        print("JSON values: value, read time, quality good; bad once for a failing point and "
              "for an offline unit's points, good again after; retained  OK")
    finally:
        await stop(r, sw, poll)
        SILENT_UNITS.clear()
        EXCEPTIONS.clear()


async def main():
    port = free_port()
    srv = await asyncio.start_server(fake_slave, "127.0.0.1", port)
    started = time.monotonic()
    try:
        await test_sparkplug_unit_health(port)
        await test_plain_unit_health(port)
        test_encode_value()
        await test_plain_json_values(port)
    finally:
        srv.close()
        with suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=2)
    print(f"\nPASS: Modbus unit health ({time.monotonic() - started:.1f} s)")


if __name__ == "__main__":
    asyncio.run(main())
