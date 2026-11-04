"""Modbus write from MQTT (plain topics and Sparkplug B DCMD) + coil/discrete polling.

A Modbus-gateway mapping polls a fake RTU slave (TCP loopback, a real register/coil
map answering FC1/2/3/4/5/6/15/16) and talks to a fake MQTT client, so this runs
anywhere without a broker or hardware. Checks:
  * coils / discrete inputs are polled (FC1/FC2) and published as 1/0;
  * <topic>/<point>/set writes a holding register (FC6, signed int16; FC16 for 32-bit)
    or a coil (FC5, or FC15 for a JSON list), answers ok / error on .../set/result and
    republishes the read-back value; read-only points get no subscription;
  * Sparkplug B: NDEATH as the Will (bdSeq), NBIRTH after connect (bdSeq, Rebirth) and
    a DBIRTH per Modbus unit (devices unit1, unit2) with its points, DDATA per device
    only for changed values with seq counting on, a DCMD writes a point of that device
    (not of another, and NCMD writes nothing), NCMD asks for a rebirth, the next session
    gets bdSeq + 1, NDEATH on stop.

Run: python3 tests/test_modbus_mqtt_write.py
"""
import asyncio
import socket
import struct
from contextlib import suppress

from _server import ROOT  # noqa: F401  (sys.path setup)

from app.config import MappingConfig
from app.engine import modbus, sparkplug
from app.engine.modbus_gateway import ModbusGatewayRunner

REGS = {10: 0, 20: 0, 21: 0, 0: 0x41BC, 1: 0x0000}   # input regs 0/1 = float32 23.5
COILS = {0: False, 1: False, 2: False}
DISCRETE = {5: True}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def answer(pdu: bytes) -> bytes:
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


class FakeMqtt:
    def __init__(self):
        self.published, self.subscribed, self.wills = [], [], []

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, bytes(payload), qos, retain))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload)))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


POINTS = [
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
    {"name": "total", "unit": 1, "fn": 3, "address": 20, "dtype": "uint32", "writable": True},
    {"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool", "writable": True},
    {"name": "alarm", "unit": 1, "fn": 2, "address": 5, "dtype": "bool"},
]
# Sparkplug: a second unit on the bus is a second device of the edge node
SP_POINTS = POINTS + [{"name": "level", "unit": 2, "fn": 3, "address": 30, "dtype": "uint16"}]


async def gateway(fmt, slave_port, fake, points=POINTS):
    m = MappingConfig.from_dict({
        "name": "Line 3 PLC", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                    "port": free_port()},
        "mqtt": {"enabled": True, "host": "broker", "base_topic": "plant/a", "payload_format": fmt},
        "modbus_poll": {"interval_s": 0.1, "points": points}})
    m.validate()
    r = ModbusGatewayRunner(m, logger=lambda _m: None)
    r._resp_timeout = 0.5
    sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
    r._sreader, r._swriter, r.serial_instance = sr, sw, object()
    r._serial_ready.set()
    if fmt == "sparkplug_b":
        from app.engine.sparkplug_node import SparkplugNode
        r._sparkplug = SparkplugNode(m, lambda _m: None, r._write_soon)
        r._mqtt = r._sparkplug.publisher()
    else:
        from app.engine.mqtt_pub import MqttPublisher
        subs = {f"plant/a/{p['name']}/set": r._on_set_message for p in points if p.get("writable")}
        r._mqtt = MqttPublisher(m.mqtt, logger=lambda _m: None, client=fake, subscriptions=subs)
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
        r._mqtt.handle_message("plant/a/pump/set", b"on")
        assert await wait(lambda: COILS[0] is True and last(fake, "plant/a/pump") == b"1")
        r._mqtt.handle_message("plant/a/pump/set", b"[1, 0, 1]")
        assert await wait(lambda: (COILS[0], COILS[1], COILS[2]) == (True, False, True)), COILS
        print("plain: setpoint -12 (FC6), total 70000 (FC16), pump on (FC5), [1,0,1] (FC15); read back  OK")

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


async def test_sparkplug(slave_port):
    fake = FakeMqtt()
    REGS[10], REGS[30] = 5, 42
    r, sw = await gateway("sparkplug_b", slave_port, fake, SP_POINTS)
    t = lambda kind, dev=None: sparkplug.topic("ser2net", kind, "Line_3_PLC", dev)  # noqa: E731

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
        births = len(decoded("NBIRTH")), len(decoded("DBIRTH", "unit1")), len(decoded("DBIRTH", "unit2"))
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
        await test_sparkplug(port)
    finally:
        srv.close()
        with suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=2)
    print("\nPASS: Modbus write from MQTT + Sparkplug B edge node")


if __name__ == "__main__":
    asyncio.run(main())
