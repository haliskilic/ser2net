"""Modbus edge mode end to end: the gateway's own start() with the real paho-mqtt client
against the in-process test broker (tests/_mqtt_broker.py), its serial port a connection to
a fake RTU slave. paho's network thread, its callbacks and the hand-off to the event loop,
the Will, retained messages and reconnects all run as they do in service. Checks:
  * plain: the retained <topic>/status "online" after the connect (QoS 0), each writable
    point's /set subscribed, polled values published; a /set published by another client
    is written (FC6 / FC5), answered "ok" on .../set/result (at QoS 1, as the /set is
    subscribed, though the mapping publishes at QoS 0) and read back, a JSON one answered
    with its id and the value read back, a list refused; the broker drops the link: its
    Will "offline", paho reconnects, "online" again, the subscriptions
    are renewed, the retained /set the broker sends again is not written again, and a live
    one is; stop: "offline";
  * stop with writes waiting (a silent slave): every one is answered, in order, and the
    answers reach the broker before the "offline";
  * a burst of /set while the slave holds a write: 32 wait, the rest get error: busy, the
    waiting ones are written in order; a /set over 64 KiB gets error: too large, a DCMD
    over 256 KiB is ignored (logged);
  * Sparkplug B: the NDEATH Will (bdSeq 0, QoS 1), NBIRTH (bdSeq 0) and a DBIRTH per unit;
    a DCMD from the broker writes its point and the new value comes back in a DDATA, as
    does the value of a DCMD that changes nothing; the
    link dropped: the broker publishes the NDEATH Will, the next session's Will and NBIRTH
    carry bdSeq 1; an NCMD rebirth gives a new NBIRTH; stop: NDEATH with the session's
    bdSeq.
Needs paho-mqtt (skips without it; SER2NET_OPTIONAL_TESTS=1 makes that a failure). Runs
with paho-mqtt 2.x and 1.6.

Run: python3 tests/test_modbus_mqtt_broker.py
"""
import asyncio
import contextlib
import struct
import time

from _server import ROOT, free_port  # noqa: F401  (sys.path setup)

from _mqtt_broker import MqttBroker
from _optional import need

from app.config import MappingConfig
from app.engine import modbus, serial_io, sparkplug
from app.engine.modbus_gateway import ModbusGatewayRunner

POINTS = [
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
    {"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool", "writable": True},
]


class Slave:
    """A fake RTU slave behind a TCP socket, unit 1: input registers 0/1 (float32 23.5),
    holding registers, coils; answers FC1/3/4/5/6/16. ``silent``: it takes requests and
    answers none. ``requests``: the (unit, PDU) of each one."""

    def __init__(self):
        self.regs = {0: 0x41BC, 1: 0, 10: 5}
        self.coils = {0: False}
        self.silent = False
        self.hold: asyncio.Event | None = None  # set: answers to writes wait for it
        self.requests: list = []
        self.port = 0
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)

    def writes(self) -> list:
        return [pdu for _u, pdu in self.requests if pdu[0] in (5, 6, 15, 16)]

    def answer(self, pdu: bytes) -> bytes:
        fn = pdu[0]
        addr, qty = struct.unpack(">HH", pdu[1:5])
        if fn == 1:
            return bytes([1, 1, int(self.coils.get(addr, False))])
        if fn in (3, 4):
            data = b"".join(struct.pack(">H", self.regs.get(addr + i, 0)) for i in range(qty))
            return bytes([fn, len(data)]) + data
        if fn == 5:
            self.coils[addr] = qty == 0xFF00
            return pdu
        if fn == 6:
            self.regs[addr] = qty
            return pdu
        if fn == 16:
            for i in range(qty):
                self.regs[addr + i] = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
            return pdu[:5]
        return bytes([fn | 0x80, 1])

    async def _serve(self, reader, writer):
        buf = bytearray()
        with contextlib.suppress(ConnectionError, OSError):
            while data := await reader.read(512):
                buf += data
                with contextlib.suppress(ValueError):
                    unit, pdu = modbus.rtu_unwrap(bytes(buf))
                    buf.clear()
                    self.requests.append((unit, pdu))
                    if self.silent:
                        continue
                    if self.hold is not None and pdu[0] in (5, 6, 15, 16):
                        await self.hold.wait()
                    writer.write(modbus.rtu_wrap(unit, self.answer(pdu)))
                    await writer.drain()


@contextlib.asynccontextmanager
async def gateway(broker, slave, fmt="plain", logs=None, mqtt=None, points=POINTS,
                  timeout=0.3):
    """A gateway started by its own start(), with paho connecting to ``broker`` (paho
    retries 0.2-0.5 s after a lost link); its serial port is a connection to ``slave``."""
    m = MappingConfig.from_dict({
        "name": "edge", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                    "port": free_port()},
        "options": {"modbus_response_timeout_s": timeout},
        "mqtt": dict({"enabled": True, "host": "127.0.0.1", "port": broker.port,
                      "base_topic": "plant/a", "payload_format": fmt}, **(mqtt or {})),
        "modbus_poll": {"interval_s": 0.2, "points": points}})
    m.validate()
    m.validate_input()

    async def open_bus(settings):
        sr, sw = await asyncio.open_connection("127.0.0.1", slave.port)
        return sr, sw, None, settings.port
    real_open, serial_io.open_serial = serial_io.open_serial, open_bus
    r = ModbusGatewayRunner(m, logger=logs.append if logs is not None else (lambda _m: None))
    try:
        await r.start()
        r._mqtt._client.reconnect_delay_set(min_delay=0.2, max_delay=0.5)
        yield r
    finally:
        await asyncio.wait_for(r.stop(), timeout=15)
        serial_io.open_serial = real_open


async def until(cond, t=10.0) -> bool:
    end = time.monotonic() + t
    while not cond():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.02)
    return True


async def plain_checks(broker):
    logs = []
    status = "plant/a/status"
    async with Slave() as slave, gateway(broker, slave, logs=logs):
        assert await until(lambda: broker.topics(status) == [(b"online", 0, True)]), \
            broker.messages
        assert broker.connects[-1]["will"] == (status, b"offline", 0, True)
        assert await until(lambda: {"plant/a/setpoint/set", "plant/a/pump/set"}
                           <= set(broker.subscriptions)), broker.subscriptions
        assert "plant/a/temp/set" not in broker.subscriptions, "only writable points"
        assert await until(lambda: b"23.5" in broker.payloads("plant/a/temp"))
        print("plain: retained 'online' after the connect (QoS 0), /set subscribed for the "
              "writable points, polled values published  OK")

        result = "plant/a/setpoint/set/result"
        broker.publish("plant/a/setpoint/set", b"42")
        assert await until(lambda: broker.topics(result) == [(b"ok", 1, False)]), \
            broker.topics(result)
        assert slave.regs[10] == 42 and await until(
            lambda: b"42" in broker.payloads("plant/a/setpoint"))
        broker.publish("plant/a/setpoint/set", b'{"value": 43, "id": "j1"}')
        assert await until(lambda: broker.payloads(result)[-1:] == [
            b'{"id":"j1","ok":true,"value":43}']), broker.payloads(result)
        broker.publish("plant/a/setpoint/set", b"[1, 2]")  # one point, one value
        assert await until(lambda: broker.payloads(result)[-1].startswith(
            b"error: not a number")), broker.payloads(result)
        assert slave.regs[10] == 43 and slave.regs.get(11, 0) == 0
        broker.publish("plant/a/pump/set", b"on", retain=True)
        assert await until(lambda: slave.coils[0] is True
                           and broker.payloads("plant/a/pump/set/result") == [b"ok"])
        print("plain: a /set from another client, through paho's on_message, is written, "
              "answered ok and read back  OK")

        # the link breaks: the Will, then paho reconnects, and the broker sends the
        # retained /set again (flagged retained): not written a second time
        slave.coils[0] = False  # switched off locally meanwhile
        writes = len(slave.writes())
        subscribed = len(broker.subscriptions)
        broker.drop()
        assert await until(lambda: broker.topics(status) == [
            (b"online", 0, True), (b"offline", 0, True), (b"online", 0, True)]), \
            broker.topics(status)
        assert await until(lambda: len(broker.subscriptions) >= subscribed + 2)
        assert await until(lambda: any("ignored the retained message on 'plant/a/pump/set'"
                                       in m for m in logs)), logs
        await asyncio.sleep(0.3)
        assert slave.coils[0] is False and len(slave.writes()) == writes, slave.writes()
        broker.publish("plant/a/pump/set", b"1")
        assert await until(lambda: slave.coils[0] is True
                           and broker.payloads("plant/a/pump/set/result") == [b"ok", b"ok"])
        print("plain: link dropped -> Will 'offline', reconnect -> 'online', subscriptions "
              "renewed, the retained /set not written again, a live one is  OK")
    assert broker.topics(status)[-1] == (b"offline", 0, True)
    print("plain: stop -> retained 'offline'  OK")


async def stop_checks(broker):
    result, status = "plant/a/setpoint/set/result", "plant/a/status"
    async with Slave() as slave:
        async with gateway(broker, slave):
            assert await until(lambda: broker.topics(status)
                               and "plant/a/setpoint/set" in broker.subscriptions)
            slave.silent = True
            for n in (1, 2, 3):
                broker.publish("plant/a/setpoint/set", str(n).encode())
            assert await until(lambda: any(pdu[0] == 6 for _u, pdu in slave.requests))
        # stopped meanwhile: the write on the bus got no reply, the other two were waiting
    assert await until(lambda: broker.topics(status)[-1:] == [(b"offline", 0, True)])
    with broker._lock:
        order = [t for t, _p, _q, _r in broker.messages if t in (result, status)]
    got = broker.payloads(result)
    assert got == [b"error: no reply from unit 1 within 0.3 s"] + [
        b"error: cancelled: mapping stopped"] * 2, got
    assert order[-4:] == [result] * 3 + [status], order
    assert len([pdu for _u, pdu in slave.requests if pdu[0] == 6]) == 1
    print("stop with writes waiting: each answered, in order, before 'offline'  OK")


async def flood_checks(broker):
    from app.engine.modbus_gateway import SET_MAX_BYTES, WRITE_QUEUE_MAX
    from app.engine.sparkplug_node import CMD_MAX_BYTES
    result = "plant/a/setpoint/set/result"
    async with Slave() as slave, gateway(broker, slave, timeout=5.0):
        assert await until(lambda: "plant/a/setpoint/set" in broker.subscriptions)
        slave.hold = asyncio.Event()
        broker.publish("plant/a/setpoint/set", b"100")
        assert await until(lambda: any(pdu[0] == 6 for _u, pdu in slave.requests))
        for n in range(WRITE_QUEUE_MAX + 3):
            broker.publish("plant/a/setpoint/set", str(101 + n).encode())
        assert await until(lambda: broker.payloads(result) == [b"error: busy"] * 3), \
            broker.payloads(result)
        slave.hold.set()
        assert await until(lambda: len(broker.payloads(result)) == 3 + 1 + WRITE_QUEUE_MAX)
        assert broker.payloads(result)[3:] == [b"ok"] * (1 + WRITE_QUEUE_MAX)
        written = [struct.unpack(">H", pdu[3:5])[0] for _u, pdu in slave.requests
                   if pdu[0] == 6]
        assert written == list(range(100, 101 + WRITE_QUEUE_MAX)), written
        broker.publish("plant/a/setpoint/set", b"7" * (SET_MAX_BYTES + 1))
        assert await until(lambda: broker.payloads(result)[-1].startswith(
            b"error: too large: "))
    print("burst of /set: 32 wait, the rest error: busy, written in order; a /set over "
          "64 KiB -> error: too large  OK")

    logs = []
    async with Slave() as slave, gateway(broker, slave, "sparkplug_b", logs=logs):
        assert await until(lambda: "spBv1.0/ser2net/DCMD/edge/+" in broker.subscriptions)
        broker.publish(node_topic("DCMD", "unit1"), command(sparkplug.Metric(
            name="setpoint", datatype=sparkplug.DataType.String, value="x" * CMD_MAX_BYTES)))
        assert await until(lambda: any("Sparkplug DCMD for unit1 ignored: " in m
                                       and "a command may have" in m for m in logs)), logs
        assert not slave.writes()
    print("a DCMD over 256 KiB is ignored and logged  OK")


def node_topic(kind, dev=None):
    return sparkplug.topic("ser2net", kind, "edge", dev)


def decoded(broker, kind, dev=None):
    return [sparkplug.decode_payload(p) for p in broker.payloads(node_topic(kind, dev))]


def bdseq(payload) -> int:
    return {m.name: m.value for m in payload.metrics}[sparkplug.BDSEQ]


def command(*metrics) -> bytes:
    return sparkplug.encode_payload(sparkplug.Payload(timestamp=int(time.time() * 1000),
                                                      metrics=list(metrics), seq=None),
                                    include_datatype=False)


async def sparkplug_checks(broker):
    logs = []
    async with Slave() as slave, gateway(broker, slave, "sparkplug_b", logs=logs):
        assert await until(lambda: decoded(broker, "DBIRTH", "unit1")), broker.messages
        will_topic, will, qos, retain = broker.connects[-1]["will"]
        assert (will_topic, qos, retain) == (node_topic("NDEATH"), 1, False)
        assert bdseq(sparkplug.decode_payload(will)) == 0
        assert [bdseq(p) for p in decoded(broker, "NBIRTH")] == [0]
        names = {m.name for m in decoded(broker, "DBIRTH", "unit1")[0].metrics}
        assert names == {"temp", "setpoint", "pump"}, names
        print("sparkplug: NDEATH Will (bdSeq 0, QoS 1), NBIRTH bdSeq 0, DBIRTH for unit1  OK")

        broker.publish(node_topic("DCMD", "unit1"), command(sparkplug.Metric(
            name="setpoint", datatype=sparkplug.DataType.Int16, value=9)))
        assert await until(lambda: slave.regs[10] == 9)
        def setpoints():
            return [m.value for d in decoded(broker, "DDATA", "unit1") for m in d.metrics
                    if m.name == "setpoint"]
        assert await until(lambda: setpoints()[-1:] == [9]), setpoints()
        reported = len(setpoints())
        broker.publish(node_topic("DCMD", "unit1"), command(sparkplug.Metric(
            name="setpoint", datatype=sparkplug.DataType.Int16, value=9)))  # no change
        assert await until(lambda: len(setpoints()) == reported + 1), setpoints()
        assert setpoints()[-1] == 9
        print("sparkplug: a DCMD through paho's on_message writes the point, DDATA brings "
              "the new value  OK")

        broker.drop()
        assert await until(lambda: [bdseq(p) for p in decoded(broker, "NDEATH")] == [0])
        assert await until(lambda: [bdseq(p) for p in decoded(broker, "NBIRTH")] == [0, 1])
        assert bdseq(sparkplug.decode_payload(broker.connects[-1]["will"][1])) == 1
        broker.publish(node_topic("NCMD"), command(sparkplug.Metric(
            name=sparkplug.REBIRTH, datatype=sparkplug.DataType.Boolean, value=True)))
        assert await until(lambda: [bdseq(p) for p in decoded(broker, "NBIRTH")] == [0, 1, 1])
        print("sparkplug: link dropped -> the broker publishes the NDEATH Will (bdSeq 0); "
              "the next session's Will and NBIRTH carry bdSeq 1; an NCMD rebirth  OK")
    assert [bdseq(p) for p in decoded(broker, "NDEATH")] == [0, 1]
    print("sparkplug: stop -> NDEATH with the session's bdSeq  OK")


async def main():
    with MqttBroker() as broker:
        await plain_checks(broker)
    with MqttBroker() as broker:
        await stop_checks(broker)
    with MqttBroker() as broker:
        await flood_checks(broker)
    with MqttBroker() as broker:
        await sparkplug_checks(broker)


if __name__ == "__main__":
    if need("paho.mqtt", what="the Modbus edge mode against a test MQTT broker"):
        import paho.mqtt
        asyncio.run(main())
        print(f"\nPASS: Modbus edge mode with paho-mqtt {paho.mqtt.__version__} against a "
              "test broker")
