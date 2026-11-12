"""Modbus write from MQTT (plain topics and Sparkplug B DCMD) + coil/discrete polling.

A Modbus-gateway mapping polls a fake RTU slave (TCP loopback, a real register/coil
map answering FC1/2/3/4/5/6/15/16) and talks to a fake MQTT client, so this runs
anywhere without a broker or hardware. Checks:
  * coils / discrete inputs are polled (FC1/FC2) and published as 1/0;
  * <topic>/<point>/set writes a holding register (FC6, signed int16; FC16 for 32-bit)
    or a coil (FC5), answers ok / error on .../set/result and republishes the read-back
    value; read-only points get no subscription;
  * a command writes exactly its own point: a JSON list (or any other value that is not
    one coil state / one number) is refused before anything reaches the bus, a point is
    found by its exact topic (a '/' in a name can't redirect a write to another point),
    a point that is not writable is never written, and error answers / log lines quote
    at most a short prefix of a large payload;
  * Sparkplug B: NDEATH as the Will (bdSeq), NBIRTH after connect (bdSeq, Rebirth) and
    a DBIRTH per Modbus unit (devices unit1, unit2) with its points, DDATA per device
    only for changed values with seq counting on, a DCMD writes a point of that device
    (not of another, and NCMD writes nothing), a coil takes only a Boolean, NCMD asks for
    a rebirth (only with a Boolean true), the next session gets bdSeq + 1, NDEATH on stop;
  * retained commands, through the gateway's own start() (paho replaced by a stand-in
    whose broker keeps retained messages and replays them on subscribe): a retained /set
    is not written again after a reconnect or by a new runner unless the mapping opts in,
    a retained NCMD / DCMD never is, and an empty message (clearing one) is no command;
  * Read-only refuses MQTT / Sparkplug writes as well (checked when a command arrives),
    and the start-up log warns about commands over a broker link without TLS / username.

Run: python3 tests/test_modbus_mqtt_write.py
"""
import asyncio
import contextlib
import socket
import struct
import sys
import types
from contextlib import suppress
from unittest import mock

from _server import ROOT  # noqa: F401  (sys.path setup)

from app.config import MappingConfig
from app.engine import modbus, serial_io, sparkplug
from app.engine.modbus_gateway import ModbusGatewayRunner
from app.engine.mqtt_pub import _matches

REGS = {10: 0, 20: 0, 21: 0, 0: 0x41BC, 1: 0x0000}   # input regs 0/1 = float32 23.5
COILS = {0: False, 1: False, 2: False}
DISCRETE = {5: True}
SEEN = []   # every request PDU the fake slave got


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def answer(pdu: bytes) -> bytes:
    SEEN.append(pdu)
    fn = pdu[0]
    addr, qty = struct.unpack(">HH", pdu[1:5])
    if fn in (1, 2):
        table = COILS if fn == 1 else DISCRETE
        bits = [table.get(addr + i, False) for i in range(qty)]
        data = bytes(sum(1 << j for j, b in enumerate(bits[k:k + 8]) if b) for k in range(0, qty, 8))
        return bytes([fn, len(data)]) + data
    if fn in (3, 4):
        data = b"".join(struct.pack(">H", REGS.get(addr + i, 0)) for i in range(qty))
        return bytes([fn, len(data)]) + data
    if fn == 5:
        COILS[addr] = qty == 0xFF00
        return pdu
    if fn == 6:
        REGS[addr] = qty
        return pdu
    if fn == 15:
        packed = pdu[6:]
        for i in range(qty):
            COILS[addr + i] = bool(packed[i // 8] >> (i % 8) & 1)
        return pdu[:5]
    if fn == 16:
        for i in range(qty):
            REGS[addr + i] = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
        return pdu[:5]
    return bytes([fn | 0x80, 1])


async def fake_slave(reader, writer):
    buf = bytearray()
    with suppress(ConnectionError, OSError):
        while data := await reader.read(512):
            buf += data
            with suppress(ValueError):
                unit, pdu = modbus.rtu_unwrap(bytes(buf))
                buf.clear()
                writer.write(modbus.rtu_wrap(unit, answer(pdu)))
                await writer.drain()


class Msg:
    """What paho hands to on_message."""

    def __init__(self, topic, payload, retain):
        self.topic, self.payload, self.retain = topic, payload, retain


class FakeMqtt:
    """The gateway's MQTT client, with the broker behind it: what the gateway publishes is
    recorded and, like a broker, the last retained message of each topic is kept and sent
    to every new subscription with the retain flag set, through paho's on_message (which
    MqttPublisher.connect sets)."""

    def __init__(self, retained=None):
        self.published, self.subscribed, self.wills = [], [], []
        self.retained = dict(retained or {})
        self.on_message = None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, bytes(payload), qos, retain))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload)))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        for t, payload in list(self.retained.items()):
            if _matches(topic, t):
                self.on_message(self, None, Msg(t, payload, True))

    def deliver(self, topic, payload, retain=False):
        """Another client publishes: a retained message is kept (an empty one clears it),
        and subscribers get it live, without the retain flag (MQTT 3.1.1, 3.3.1.3)."""
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        if any(_matches(sub, topic) for sub in self.subscribed):
            self.on_message(self, None, Msg(topic, payload, False))

    def username_pw_set(self, username, password=None):
        pass

    def tls_set(self):
        pass

    def connect_async(self, host, port, keepalive=60):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


@contextlib.contextmanager
def fake_paho(client):
    """paho.mqtt.client replaced by a module whose Client() is ``client``, so the real
    start() -> MqttPublisher.connect() wiring runs without paho or a broker."""
    mod = types.ModuleType("paho.mqtt.client")
    mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mod.Client = lambda *a, **k: client
    pkg, sub = types.ModuleType("paho"), types.ModuleType("paho.mqtt")
    pkg.mqtt, sub.client = sub, mod
    with mock.patch.dict(sys.modules, {"paho": pkg, "paho.mqtt": sub, "paho.mqtt.client": mod}):
        yield


@contextlib.asynccontextmanager
async def started(slave_port, fake, fmt="plain", points=None, logs=None, mqtt=None,
                  network=None):
    """A gateway started by its own start(): the serial port is a connection to the fake
    slave, paho is ``fake``; the broker's CONNACK is up to the test (fake.on_connect)."""
    m = MappingConfig.from_dict({
        "name": "Line 3 PLC", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": dict({"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                         "port": free_port()}, **(network or {})),
        "mqtt": dict({"enabled": True, "host": "broker", "base_topic": "plant/a",
                      "payload_format": fmt}, **(mqtt or {})),
        "modbus_poll": {"interval_s": 0.1, "points": points or ONE_POINT}})
    m.validate()

    async def open_bus(settings):
        sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
        return sr, sw, None, settings.port
    real_open, serial_io.open_serial = serial_io.open_serial, open_bus
    r = ModbusGatewayRunner(m, logger=logs.append if logs is not None else (lambda _m: None))
    try:
        with fake_paho(fake):
            await r.start()
        assert await wait(lambda: r._link.is_open), r.status.as_dict()
        yield r
    finally:
        await asyncio.wait_for(r.stop(), timeout=10)
        serial_io.open_serial = real_open


POINTS = [
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
    {"name": "total", "unit": 1, "fn": 3, "address": 20, "dtype": "uint32", "writable": True},
    {"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool", "writable": True},
    {"name": "alarm", "unit": 1, "fn": 2, "address": 5, "dtype": "bool"},
]
# Sparkplug: a second unit on the bus is a second device of the edge node
SP_POINTS = POINTS + [{"name": "level", "unit": 2, "fn": 3, "address": 30, "dtype": "uint16"}]


async def gateway(fmt, slave_port, fake, points=POINTS, logs=None, mqtt=None, network=None):
    m = MappingConfig.from_dict({
        "name": "Line 3 PLC", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": dict({"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                         "port": free_port()}, **(network or {})),
        "mqtt": dict({"enabled": True, "host": "broker", "base_topic": "plant/a",
                      "payload_format": fmt}, **(mqtt or {})),
        "modbus_poll": {"interval_s": 0.1, "points": points}})
    m.validate()
    log = logs.append if logs is not None else (lambda _m: None)
    r = ModbusGatewayRunner(m, logger=log)
    r._resp_timeout = 0.5
    sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
    r._link.attach(sr, sw)
    if fmt == "sparkplug_b":
        from app.engine.sparkplug_node import SparkplugNode
        r._sparkplug = SparkplugNode(m, log, r._write_soon)
        r._mqtt = r._sparkplug.publisher()
    else:  # what start() subscribes: each writable point's own /set topic
        from app.engine.mqtt_pub import MqttPublisher
        subs = dict.fromkeys(r._set_points, r._on_set_message)
        r._mqtt = MqttPublisher(m.mqtt, logger=log, client=fake, subscriptions=subs,
                                accept_retained=m.mqtt.apply_retained_set)
    if fmt == "sparkplug_b":
        r._mqtt._client = fake
    r._mqtt.connect()
    r._mqtt.handle_connect()
    return r, sw


async def wait(cond, t=3.0):
    for _ in range(int(t / 0.02)):
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


def last(fake, topic):
    vals = [p for t, p in fake.published if t == topic]
    return vals[-1] if vals else None


async def test_plain(slave_port):
    fake = FakeMqtt()
    r, sw = await gateway("plain", slave_port, fake)
    poll = asyncio.create_task(r._poll_loop())
    try:
        assert sorted(fake.subscribed) == ["plant/a/pump/set", "plant/a/setpoint/set", "plant/a/total/set"]
        assert await wait(lambda: last(fake, "plant/a/alarm") == b"1" and last(fake, "plant/a/temp") == b"23.5")
        assert last(fake, "plant/a/pump") == b"0"
        print("plain: coils / discrete inputs polled (FC1/FC2), only writable points subscribed  OK")

        r._mqtt.handle_message("plant/a/setpoint/set", b"-12")
        assert await wait(lambda: last(fake, "plant/a/setpoint/set/result") == b"ok")
        assert REGS[10] == 0xFFF4 and await wait(lambda: last(fake, "plant/a/setpoint") == b"-12")
        r._mqtt.handle_message("plant/a/total/set", b"70000")
        assert await wait(lambda: REGS[20] == 1 and REGS[21] == 70000 - 65536), REGS
        fc16 = [p for p in SEEN if p[0] == 16]
        assert fc16 and fc16[-1][:6] == bytes([16, 0, 20, 0, 2, 4]), "a uint32 is its own 2 registers"
        r._mqtt.handle_message("plant/a/pump/set", b"on")
        assert await wait(lambda: COILS[0] is True and last(fake, "plant/a/pump") == b"1")
        print("plain: setpoint -12 (FC6), total 70000 (FC16), pump on (FC5); read back  OK")

        r._mqtt.handle_message("plant/a/setpoint/set", b"abc")
        assert await wait(lambda: (last(fake, "plant/a/setpoint/set/result") or b"").startswith(b"error: not a number"))
        r._mqtt.handle_message("plant/a/setpoint/set", b"40000")
        assert await wait(lambda: b"out of range" in (last(fake, "plant/a/setpoint/set/result") or b"")
                          or b"range" in (last(fake, "plant/a/setpoint/set/result") or b""))
        print("plain: bad values answered with error: … on .../set/result  OK")
    finally:
        r._stop.set()
        poll.cancel()
        with suppress(BaseException):
            await poll
        sw.close()


# a writable coil next to a read-only one, and a writable point whose name has a '/'
ONE_POINT = [
    {"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool", "writable": True},
    {"name": "valve", "unit": 1, "fn": 1, "address": 1, "dtype": "bool"},
    {"name": "line/valve", "unit": 1, "fn": 1, "address": 2, "dtype": "bool", "writable": True},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
]


async def test_plain_one_point(slave_port):
    """A /set writes its own point and nothing else, whatever the payload."""
    fake, logs = FakeMqtt(), []
    r, sw = await gateway("plain", slave_port, fake, ONE_POINT, logs)
    pts = {p.name: p for p in r.mapping.modbus_poll.points}
    COILS.update({0: False, 1: False, 2: False})
    SEEN.clear()

    async def answer_of(point, payload):
        topic = f"plant/a/{point}/set"
        before = len([t for t, _p in fake.published if t == topic + "/result"])
        r._mqtt.handle_message(topic, payload)
        assert await wait(lambda: len([t for t, _p in fake.published
                                       if t == topic + "/result"]) > before), (point, payload)
        return last(fake, topic + "/result")

    def writes():
        return [p for p in SEEN if p[0] in (5, 6, 15, 16)]

    try:
        assert sorted(fake.subscribed) == ["plant/a/line/valve/set", "plant/a/pump/set",
                                           "plant/a/setpoint/set"], fake.subscribed
        # a JSON list on one coil is not several coils (the next one is read-only)
        for bad in (b"[1, 1]", b"[1]", b"[" + b"1," * 1967 + b"1]", b"2", b"yes", b"1.0",
                    b"true false", b'"on"', b"\x00"):
            res = await answer_of("pump", bad)
            assert res.startswith(b"error: a coil takes one state"), (bad[:20], res)
            assert len(res) < 120, f"the answer quotes the whole payload: {len(res)} bytes"
        await asyncio.sleep(0.1)
        assert writes() == [] and COILS == {0: False, 1: False, 2: False}, (writes(), COILS)
        print("plain: a JSON list / any non-state on a coil -> error, nothing on the bus  OK")

        # the point is the topic's, exactly: 'line/valve' is not the read-only 'valve'
        assert await answer_of("line/valve", b"1") == b"ok"
        assert (COILS[1], COILS[2]) == (False, True), COILS
        assert writes() == [bytes([5, 0, 2, 0xFF, 0])], writes()
        r._on_set_message("plant/a/valve/set", b"1")      # not a writable point's topic
        r._mqtt.handle_message("plant/a/valve/set", b"1")
        await asyncio.sleep(0.1)
        assert COILS[1] is False and last(fake, "plant/a/valve/set/result") is None
        print("plain: a point is found by its exact topic; a read-only point is never written  OK")

        # a register takes one number of its own type, nothing else
        for bad in (b"[1, 2]", b"1_000", b"nan", b"inf", b"0b11", b"1 2", b"true", b"9" * 100000):
            res = await answer_of("setpoint", bad)
            assert res.startswith(b"error: not a number"), (bad[:20], res)
            assert len(res) < 120, len(res)
        assert (await answer_of("setpoint", b"1e400")).startswith(b"error: value must be a finite")
        for text, raw in ((b"0x10", 16), (b"010", 10), (b"+7", 7), (b"-0x2", 0xFFFE), (b"12.6", 13)):
            assert await answer_of("setpoint", text) == b"ok", text
            assert REGS[10] == raw, (text, REGS[10])
        assert all(p[0] == 6 for p in writes()[1:]), writes()
        print("plain: registers take one decimal / 0x / fractional number, written as the "
              "point's type; lists and other text -> error  OK")

        # write_point itself (every command path ends there) takes one value of the type
        SEEN.clear()
        for point, value, match in ((pts["pump"], [True, True], "is a coil"),
                                    (pts["pump"], 1, "is a coil"),
                                    (pts["pump"], "true", "is a coil"),
                                    (pts["valve"], True, "not writable"),
                                    (pts["setpoint"], True, "takes a number"),
                                    (pts["setpoint"], [1, 2], "takes a number"),
                                    (pts["setpoint"], "5", "takes a number")):
            try:
                await r.write_point(point, value)
            except ValueError as e:
                assert match in str(e), (value, str(e))
            else:
                raise AssertionError(f"write_point({point.name}, {value!r}) was accepted")
        assert SEEN == [], SEEN
        assert all(len(line) < 300 for line in logs), max(logs, key=len)[:200]
        print("plain: write_point refuses lists, wrong types and non-writable points; "
              "log lines stay short  OK")
    finally:
        sw.close()


def bus_writes():
    return [p for p in SEEN if p[0] in (5, 6, 15, 16)]


async def test_retained_replay(slave_port):
    """A command is written once, when it is published. The broker sends the retained copy
    of a /set or a Sparkplug command again to every new subscription -- after a reconnect,
    and to every new runner (a config save, Start, restart, a service start) -- and that
    copy is not written again unless the mapping opts in (plain /set only)."""
    pump_set = "plant/a/pump/set"
    # a controller left "on" retained on the pump's /set; the pump is off by now
    COILS[0] = False
    SEEN.clear()
    fake, logs = FakeMqtt(retained={pump_set: b"on"}), []
    async with started(slave_port, fake, logs=logs) as r:
        fake.on_connect(fake, None, {}, 0)             # CONNACK: subscribe -> the replay
        await asyncio.sleep(0.3)
        assert COILS[0] is False and bus_writes() == [], (COILS, bus_writes())
        assert last(fake, pump_set + "/result") is None
        assert any("ignored the retained message on 'plant/a/pump/set'" in m for m in logs), logs
        # a live command is written, once
        fake.deliver(pump_set, b"on", retain=True)
        assert await wait(lambda: COILS[0] is True and last(fake, pump_set + "/result") == b"ok")
        assert len(bus_writes()) == 1
        # switched off locally; the connection drops and comes back: nothing is replayed
        COILS[0] = False
        r._mqtt._paho_on_disconnect(fake, None)
        fake.on_connect(fake, None, {}, 0)
        await asyncio.sleep(0.3)
        assert COILS[0] is False and len(bus_writes()) == 1, bus_writes()
        # clearing the retained message (an empty one, delivered live) is not a command
        answers = len([t for t, _p in fake.published if t == pump_set + "/result"])
        fake.deliver(pump_set, b"", retain=True)
        await asyncio.sleep(0.2)
        assert len([t for t, _p in fake.published if t == pump_set + "/result"]) == answers
        assert pump_set not in fake.retained and len(bus_writes()) == 1
    print("plain: a retained /set is not written again on reconnect / a new runner; a live "
          "one is written once; an empty message is no command  OK")

    # opted in: the retained /set is written after each connect, and the log says so
    SEEN.clear()
    logs = []
    async with started(slave_port, FakeMqtt(retained={pump_set: b"on"}), logs=logs,
                       mqtt={"apply_retained_set": True}) as r:
        r._mqtt._client.on_connect(r._mqtt._client, None, {}, 0)
        assert await wait(lambda: COILS[0] is True and len(bus_writes()) == 1), bus_writes()
        assert any("applying the retained message on 'plant/a/pump/set'" in m for m in logs)
    print("plain + apply_retained_set: the retained /set is written after connect  OK")

    # Sparkplug B: NCMD / DCMD are never retained; a retained one is ignored even with the
    # plain-format option on, a live one is written
    node = "spBv1.0/ser2net/{}/Line_3_PLC"
    dcmd = sparkplug.encode_payload(sparkplug.Payload(0, [sparkplug.Metric(
        "pump", sparkplug.DataType.Boolean, True)], None), include_datatype=False)
    rebirth = sparkplug.encode_payload(sparkplug.Payload(0, [sparkplug.Metric(
        sparkplug.REBIRTH, sparkplug.DataType.Boolean, True)], None), include_datatype=False)
    COILS[0] = False
    SEEN.clear()
    logs = []
    fake = FakeMqtt(retained={node.format("DCMD") + "/unit1": dcmd, node.format("NCMD"): rebirth})
    async with started(slave_port, fake, "sparkplug_b", logs=logs,
                       mqtt={"apply_retained_set": True}) as r:
        fake.on_connect(fake, None, {}, 0)
        await asyncio.sleep(0.3)
        births = [t for t, _p in fake.published if t == node.format("NBIRTH")]
        assert COILS[0] is False and bus_writes() == [] and len(births) == 1, (COILS, births)
        assert sum("ignored the retained message" in m for m in logs) == 2, logs
        fake.deliver(node.format("DCMD") + "/unit1", dcmd)
        assert await wait(lambda: COILS[0] is True and len(bus_writes()) == 1), bus_writes()
    print("sparkplug: retained NCMD / DCMD ignored on subscribe, a live DCMD is written  OK")


async def test_read_only(slave_port):
    """Read-only refuses MQTT and Sparkplug writes too: a /set gets error: mapping is
    read-only, a DCMD is logged and dropped, and nothing reaches the bus. It is checked when
    a command arrives, so it follows the mapping without a restart."""
    COILS[0] = False
    SEEN.clear()
    fake, logs = FakeMqtt(), []
    r, sw = await gateway("plain", slave_port, fake, ONE_POINT, logs, network={"read_only": True})
    try:
        assert "plant/a/pump/set" in fake.subscribed, "still subscribed: refused with an answer"
        r._mqtt.handle_message("plant/a/pump/set", b"1")
        assert await wait(lambda: last(fake, "plant/a/pump/set/result")
                          == b"error: mapping is read-only")
        try:
            await r.write_point(r._set_points["plant/a/pump/set"], True)
        except ValueError as e:
            assert str(e) == "mapping is read-only", e
        else:
            raise AssertionError("write_point wrote on a read-only mapping")
        await asyncio.sleep(0.1)
        assert COILS[0] is False and bus_writes() == [], bus_writes()
        assert any("write 'pump' from MQTT refused: the mapping is read-only" in m for m in logs)
        r.mapping.network.read_only = False            # e.g. a later in-place toggle
        r._mqtt.handle_message("plant/a/pump/set", b"1")
        assert await wait(lambda: COILS[0] is True and last(fake, "plant/a/pump/set/result") == b"ok")
    finally:
        sw.close()
    print("plain + read-only: /set answered error: mapping is read-only, nothing on the bus  OK")

    COILS[0] = False
    SEEN.clear()
    fake, logs = FakeMqtt(), []
    r, sw = await gateway("sparkplug_b", slave_port, fake, SP_POINTS, logs,
                          network={"read_only": True})
    try:
        r._mqtt.handle_message(sparkplug.topic("ser2net", "DCMD", "Line_3_PLC", "unit1"),
                               sparkplug.encode_payload(sparkplug.Payload(0, [sparkplug.Metric(
                                   "pump", sparkplug.DataType.Boolean, True)], None)))
        await asyncio.sleep(0.3)
        assert COILS[0] is False and bus_writes() == [], bus_writes()
        assert any("DCMD for unit1/'pump' refused: the mapping is read-only" in m for m in logs), logs
    finally:
        sw.close()
    print("sparkplug + read-only: DCMD logged and refused, nothing on the bus  OK")

    # at start: what read-only means here, or a warning when commands come in without TLS
    for network, mqtt, expect in (({"read_only": True}, {}, "writes to writable points are refused too"),
                                  ({}, {}, "without TLS or a username"),
                                  ({}, {"tls": True}, "without a username")):
        logs = []
        async with started(slave_port, FakeMqtt(), logs=logs, mqtt=mqtt, network=network):
            pass
        assert any(expect in m for m in logs), (expect, logs)
        assert any("warning: writable points take MQTT commands" in m for m in logs) \
            == (not network), logs
    logs = []
    async with started(slave_port, FakeMqtt(), logs=logs,
                       mqtt={"tls": True, "username": "ser2net", "password": "x"}):
        pass
    assert not any("warning:" in m for m in logs), logs
    print("start-up log: read-only scope, warning for commands without TLS / username  OK")


async def test_sparkplug(slave_port):
    fake, logs = FakeMqtt(), []
    REGS[10], REGS[30] = 5, 42
    r, sw = await gateway("sparkplug_b", slave_port, fake, SP_POINTS, logs)
    t = lambda kind, dev=None: sparkplug.topic("ser2net", kind, "Line_3_PLC", dev)

    def decoded(kind, dev=None):
        return [sparkplug.decode_payload(p) for tp, p in fake.published if tp == t(kind, dev)]

    def writes(cmd_topic, metric):
        r._mqtt.handle_message(cmd_topic, sparkplug.encode_payload(
            sparkplug.Payload(timestamp=0, seq=None, metrics=[metric])))

    try:
        will_topic, will_payload, will_qos, will_retain = fake.wills[-1]
        death = sparkplug.decode_payload(will_payload)
        assert will_topic == t("NDEATH") and will_qos == 1 and not will_retain
        assert death.metrics[0].name == "bdSeq" and death.metrics[0].value == 0
        assert t("NCMD") in fake.subscribed and "spBv1.0/ser2net/DCMD/Line_3_PLC/+" in fake.subscribed
        nbirth = decoded("NBIRTH")[-1]
        node = {m.name: m for m in nbirth.metrics}
        assert nbirth.seq == 0 and node["bdSeq"].value == 0 and node["Node Control/Rebirth"].value is False
        assert set(node) == {"bdSeq", "Node Control/Rebirth"}, "the points are device metrics"
        unit1, unit2 = decoded("DBIRTH", "unit1")[-1], decoded("DBIRTH", "unit2")[-1]
        names = {m.name: m for m in unit1.metrics}
        assert (unit1.seq, unit2.seq) == (1, 2)
        assert set(names) == {"temp", "setpoint", "total", "pump", "alarm"} and names["temp"].is_null
        assert names["pump"].datatype == sparkplug.DataType.Boolean
        assert [m.name for m in unit2.metrics] == ["level"]
        print("sparkplug: NDEATH Will (bdSeq 0, QoS 1), NBIRTH seq 0 (bdSeq, Rebirth), "
              "a DBIRTH per Modbus unit with its points  OK")

        poll = asyncio.create_task(r._poll_loop())
        assert await wait(lambda: decoded("DDATA", "unit1") and decoded("DDATA", "unit2"))
        await asyncio.sleep(0.4)
        d1, d2 = decoded("DDATA", "unit1"), decoded("DDATA", "unit2")
        first = {m.name: m.value for m in d1[0].metrics}
        assert first["temp"] == 23.5 and first["setpoint"] == 5
        assert [(m.name, m.value) for m in d2[0].metrics] == [("level", 42)]
        assert len(d1) == 1 and len(d2) == 1, "no DDATA when nothing changed"
        assert (d1[0].seq, d2[0].seq) == (3, 4), "seq counts on from the births"

        setpoint = sparkplug.Metric(name="setpoint", datatype=sparkplug.DataType.Int16, value=7)
        writes(t("DCMD", "unit2"), setpoint)    # not a point of unit2
        writes(t("NCMD"), setpoint)             # points are written with a DCMD
        writes(t("DCMD", "unit9"), setpoint)    # no such device
        await asyncio.sleep(0.3)
        assert REGS[10] == 5, "the write reached the bus without a DCMD to its device"
        writes(t("DCMD", "unit1"), setpoint)
        assert await wait(lambda: REGS[10] == 7)
        assert await wait(lambda: any(m.name == "setpoint" and m.value == 7
                                      for d in decoded("DDATA", "unit1") for m in d.metrics))
        assert decoded("DDATA", "unit1")[-1].seq == 5, decoded("DDATA", "unit1")[-1].seq
        # a command without a datatype (allowed in Sparkplug 3.0): -3 arrives as 2**32 - 3
        r._mqtt.handle_message(t("DCMD", "unit1"), sparkplug.encode_payload(sparkplug.Payload(
            timestamp=0, seq=None, metrics=[sparkplug.Metric(
                name="setpoint", datatype=sparkplug.DataType.Int16, value=-3)]),
            include_datatype=False))
        assert await wait(lambda: REGS[10] == 0xFFFD), REGS[10]
        # a coil can't be switched on by a value that isn't a boolean (String "false")
        COILS[0] = False
        writes(t("DCMD", "unit1"), sparkplug.Metric(name="pump", datatype=sparkplug.DataType.String,
                                                    value="false"))
        await asyncio.sleep(0.3)
        assert COILS[0] is False, "a String 'false' switched the coil on"
        # ... nor by a number, and a point that isn't writable takes nothing
        SEEN.clear()
        for metric in (sparkplug.Metric(name="pump", datatype=sparkplug.DataType.Int32, value=1),
                       sparkplug.Metric(name="pump", datatype=sparkplug.DataType.Double, value=1.0),
                       sparkplug.Metric(name="temp", datatype=sparkplug.DataType.Float, value=1.0),
                       sparkplug.Metric(name="alarm", datatype=sparkplug.DataType.Boolean, value=False),
                       sparkplug.Metric(name="x" * 5000, datatype=sparkplug.DataType.Boolean, value=True)):
            writes(t("DCMD", "unit1"), metric)
        await asyncio.sleep(0.3)
        assert COILS[0] is False and DISCRETE[5] is True, (COILS, DISCRETE)
        assert not [p for p in SEEN if p[0] in (5, 6, 15, 16)], SEEN
        assert all(len(line) < 300 for line in logs), max(logs, key=len)[:200]
        writes(t("DCMD", "unit1"), sparkplug.Metric(name="pump", datatype=sparkplug.DataType.Boolean,
                                                    value=True))
        assert await wait(lambda: COILS[0] is True)
        assert [p for p in SEEN if p[0] in (5, 6, 15, 16)] == [bytes([5, 0, 0, 0xFF, 0])], SEEN
        births = len(decoded("NBIRTH")), len(decoded("DBIRTH", "unit1")), len(decoded("DBIRTH", "unit2"))
        for value, datatype in ((1, sparkplug.DataType.Int32), ("true", sparkplug.DataType.String)):
            writes(t("NCMD"), sparkplug.Metric(name="Node Control/Rebirth", datatype=datatype,
                                               value=value))
        again = len(decoded("NBIRTH")), len(decoded("DBIRTH", "unit1")), len(decoded("DBIRTH", "unit2"))
        assert again == births, "a rebirth needs a Boolean true"
        writes(t("NCMD"), sparkplug.Metric(name="Node Control/Rebirth",
                                           datatype=sparkplug.DataType.Boolean, value=True))
        again = len(decoded("NBIRTH")), len(decoded("DBIRTH", "unit1")), len(decoded("DBIRTH", "unit2"))
        assert again == tuple(n + 1 for n in births), (births, again)
        assert {m.name: m.value for m in decoded("DBIRTH", "unit2")[-1].metrics} == {"level": 42}
        print("sparkplug: DDATA by exception per device (seq on), DCMD write -> FC6 only to its "
              "device, rebirth -> NBIRTH + DBIRTHs with the last values  OK")

        r._mqtt._paho_on_disconnect(None, None)  # connection lost: next session = bdSeq 1
        assert sparkplug.decode_payload(fake.wills[-1][1]).metrics[0].value == 1
        r._mqtt.handle_connect()
        birth = decoded("NBIRTH")[-1]
        assert {m.name: m.value for m in birth.metrics}["bdSeq"] == 1 and birth.seq == 0
        assert decoded("DBIRTH", "unit2")[-1].seq == 2
        r._stop.set()
        poll.cancel()
        with suppress(BaseException):
            await poll
        await r.stop()
        death = decoded("NDEATH")[-1]
        assert death.metrics[0].value == 1
        print("sparkplug: reconnect -> bdSeq 1 in Will + NBIRTH + DBIRTHs; stop -> NDEATH  OK")
    finally:
        sw.close()


async def main():
    port = free_port()
    srv = await asyncio.start_server(fake_slave, "127.0.0.1", port)
    try:
        await test_plain(port)
        await test_plain_one_point(port)
        await test_retained_replay(port)
        await test_read_only(port)
        await test_sparkplug(port)
    finally:
        srv.close()
        with suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=2)
    print("\nPASS: Modbus write from MQTT + Sparkplug B edge node")


if __name__ == "__main__":
    asyncio.run(main())
