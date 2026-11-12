"""How a Modbus gateway reads and writes its poll points' values.

  * word order: ABCD (the default, as before), CDAB (low register first), BADC (each
    register's bytes swapped) and DCBA, for reads and for MQTT writes, on 32- and 64-bit
    values; int64 / uint64 / float64 (four registers), text in registers ("string",
    "length" registers, high or low byte first) and one bit of a register ("bit");
  * Sparkplug B datatypes: the existing types keep theirs, the new ones get Int64, UInt64,
    Double, String and Boolean;
  * end to end against a fake RTU slave (TCP loopback): a poll publishes each type's value,
    an MQTT write of a CDAB / 64-bit point sends the registers in the point's order (FC16),
    and a string or a bit of a register is never written;
  * block reads (off by default): a unit's points of one function that lie close together
    are read with one request (at most 125 registers / 2000 bits, up to the gap of unused
    registers), giving the values single reads give; a block the slave refuses with
    exception 02 is read point by point from then on (logged once); a block without a reply
    is no reply for each of its points, so the unit goes offline as with single reads; a
    write from MQTT takes its turn between two blocks, and the value read before it is not
    published after its read-back.
Cross-platform (binds 127.0.0.1 only).

Run: python3 tests/test_modbus_point_reads.py
"""
import asyncio
import math
import struct
from contextlib import suppress

from _server import ROOT, free_port  # noqa: F401  (sys.path setup)

from app.config import ConfigError, MappingConfig
from app.engine import modbus, sparkplug
from app.engine.modbus_gateway import (UNIT_OFFLINE_AFTER, ModbusGatewayRunner, decode_point,
                                       plan_blocks)
from app.engine.sparkplug_node import SparkplugNode

TIMEOUT = 0.2


def regs_bytes(regs):
    return b"".join(struct.pack(">H", r) for r in regs)


def test_word_orders():
    # 230.5 as float32 is 43 66 80 00; uint32 100000 is 00 01 86 a0
    cases = {"ABCD": [0x4366, 0x8000], "CDAB": [0x8000, 0x4366],
             "BADC": [0x6643, 0x0080], "DCBA": [0x0080, 0x6643]}
    for order, regs in cases.items():
        assert modbus.decode_value(regs_bytes(regs), "float32", order=order) == 230.5, order
        assert modbus.encode_value(230.5, "float32", order=order) == regs, order
    assert modbus.decode_value(regs_bytes([0x86A0, 0x0001]), "uint32", order="CDAB") == 100000
    assert modbus.decode_value(regs_bytes([0x86A0, 0x0001]), "uint32") == 2258632705  # as before
    # 64-bit: the same rule over four registers
    v = 0x0102030405060708
    for order, regs in (("ABCD", [0x0102, 0x0304, 0x0506, 0x0708]),
                        ("CDAB", [0x0708, 0x0506, 0x0304, 0x0102]),
                        ("BADC", [0x0201, 0x0403, 0x0605, 0x0807]),
                        ("DCBA", [0x0807, 0x0605, 0x0403, 0x0201])):
        assert modbus.encode_value(v, "uint64", order=order) == regs, order
        assert modbus.decode_value(regs_bytes(regs), "uint64", order=order) == v, order
        assert modbus.decode_value(regs_bytes(regs), "int64", order=order) == v, order
    assert modbus.decode_value(regs_bytes([0xFFFF] * 4), "int64") == -1
    assert modbus.decode_value(regs_bytes([0xFFFF] * 4), "uint64") == 2 ** 64 - 1
    assert modbus.encode_value(-2, "int64", order="CDAB") == [0xFFFE, 0xFFFF, 0xFFFF, 0xFFFF]
    pi = modbus.encode_value(math.pi, "float64")
    assert modbus.decode_value(regs_bytes(pi), "float64") == math.pi and len(pi) == 4
    assert modbus.decode_value(regs_bytes(modbus.encode_value(1.5, "float64", order="DCBA")),
                               "float64", order="DCBA") == 1.5
    assert modbus.decode_value(regs_bytes([1, 2, 3, 4]), "int64", 0.5) == 0x0001000200030004 * 0.5
    # 16-bit values: CDAB changes nothing, BADC / DCBA swap the bytes
    assert modbus.decode_value(regs_bytes([0x1234]), "uint16", order="CDAB") == 0x1234
    assert modbus.decode_value(regs_bytes([0x1234]), "uint16", order="BADC") == 0x3412
    assert modbus.encode_value(0x1234, "uint16", order="DCBA") == [0x3412]
    for dtype in ("int64", "uint64"):
        lo, hi = modbus._INT_RANGE[dtype]
        for bad in (lo - 1, hi + 1):
            try:
                modbus.encode_value(bad, dtype)
                raise AssertionError(f"{dtype} took {bad}")
            except ValueError:
                pass
    for value, dtype in ((float("inf"), "float64"), (1e39, "float32"), (10 ** 400, "float64")):
        try:
            modbus.encode_value(value, dtype)
            raise AssertionError(f"{dtype} took {value:g}")
        except ValueError:
            pass
    print("word orders ABCD / CDAB / BADC / DCBA; int64, uint64, float64  OK")


def test_strings_and_bits():
    assert modbus.decode_string(b"AB\x00Cjunk") == "AB"
    assert modbus.decode_string(b"BA\x00C", "BADC") == "ABC"
    assert modbus.decode_string(b"PUMP-3    ") == "PUMP-3"
    assert modbus.decode_string(b"\xc3\xbc\xff") == "ü�"
    assert modbus.decode_string(b"") == ""
    assert modbus.register_bit(b"\x80\x01", 0) and modbus.register_bit(b"\x80\x01", 15)
    assert not modbus.register_bit(b"\x80\x01", 1)
    p = MappingConfig.from_dict({"modbus_poll": {"points": [
        {"name": "s", "fn": 3, "dtype": "string", "length": 3, "word_order": "BADC"},
        {"name": "b", "fn": 4, "dtype": "bool", "bit": 3},
        {"name": "f", "fn": 3, "dtype": "float32", "word_order": "CDAB", "scale": 2}]}}
    ).modbus_poll.points
    assert decode_point(p[0], b"OMEFTI") == "MOFEIT"
    assert decode_point(p[0], b"xxOMEFI\x00junk", offset=1) == "MOFE"
    assert decode_point(p[1], b"\x00\x08") is True and decode_point(p[1], b"\x00\x07") is False
    assert decode_point(p[2], regs_bytes([0, 0, 0x8000, 0x4366]), offset=2) == 461.0
    print("strings (high or low byte first, NUL and padding dropped) and register bits  OK")


def test_config_rules():
    def gw(*points):
        return MappingConfig.from_dict({
            "name": "gw", "serial": {"port": "/dev/ser2net-test-gw"},
            "network": {"protocol": "modbus", "port": 5020},
            "mqtt": {"enabled": True, "host": "b", "base_topic": "plant/gw"},
            "modbus_poll": {"points": list(points)}})

    good = [{"name": "a", "fn": 3, "dtype": "float32", "word_order": "CDAB"},
            {"name": "b", "fn": 4, "dtype": "int64", "word_order": "DCBA"},
            {"name": "c", "fn": 3, "dtype": "uint64", "writable": True, "max": 10 ** 12},
            {"name": "d", "fn": 3, "dtype": "float64", "scale": 0.5},
            {"name": "e", "fn": 3, "dtype": "string", "length": 8, "word_order": "BADC"},
            {"name": "f", "fn": 4, "dtype": "bool", "bit": 15},
            {"name": "g", "fn": 1, "dtype": "bool"}]
    m = gw(*good)
    m.validate()
    m.validate_input()
    old = gw({"name": "a", "fn": 3}).modbus_poll.points[0]
    assert (old.word_order, old.bit, old.length) == ("ABCD", None, None), "defaults as before"

    def refused(point, words, stored_ok=True):
        m = gw(point)
        if stored_ok:
            m.validate()  # loads and saves: only an edit refuses it
        try:
            if not stored_ok:
                m.validate()
            m.validate_input()
            raise AssertionError(f"took {point}")
        except ConfigError as e:
            assert words in str(e), (point, str(e))
    refused({"name": "a", "fn": 3, "word_order": "cdab"}, "word_order must be one of ABCD")
    refused({"name": "a", "fn": 3, "word_order": 1}, "word_order must be one of")
    refused({"name": "a", "fn": 1, "dtype": "bool", "word_order": "BADC"}, "not for a bit")
    refused({"name": "a", "fn": 3, "dtype": "string", "length": 2, "word_order": "CDAB"},
            "the low byte first")
    refused({"name": "a", "fn": 1, "dtype": "bool", "bit": 2}, "\"bit\" reads one bit")
    refused({"name": "a", "fn": 3, "dtype": "uint16", "bit": 2}, "\"bit\" reads one bit")
    refused({"name": "a", "fn": 3, "length": 2}, "\"length\" is for a \"string\"")
    refused({"name": "a", "fn": 3, "dtype": "string", "length": 2, "scale": 0.1},
            "can't be scaled")
    refused({"name": "a", "fn": 3, "dtype": "string", "length": 2, "writable": True},
            "can't write a string")
    refused({"name": "a", "fn": 3, "dtype": "bool", "bit": 1, "writable": True},
            "can't write a string or a bit")
    # what validate() refused before stays refused when the bit / length is missing
    refused({"name": "a", "fn": 3, "dtype": "bool"}, "'bool' with the \"bit\"", stored_ok=False)
    refused({"name": "a", "fn": 3, "dtype": "bool", "bit": 16}, "\"bit\" (0-15)", stored_ok=False)
    refused({"name": "a", "fn": 3, "dtype": "string"}, "needs its \"length\"", stored_ok=False)
    refused({"name": "a", "fn": 3, "dtype": "string", "length": 126}, "1-125",
            stored_ok=False)
    refused({"name": "a", "fn": 1, "dtype": "int64"}, "use dtype 'bool'", stored_ok=False)
    print("new point keys: off by default; an edit refuses what can't be used  OK")


def test_stored_unusable_keys():
    """A stored word order or scale the edits refuse (hand-edited config.json): the point is
    read as ABCD / unscaled, with a warning at start."""
    m = MappingConfig.from_dict({
        "name": "gw", "serial": {"port": "fake"}, "network": {"protocol": "modbus"},
        "mqtt": {"enabled": True, "host": "b", "base_topic": "plant/gw"},
        "modbus_poll": {"points": [{"name": "v", "fn": 3, "dtype": "uint32",
                                    "word_order": "XYZW", "scale": "x"}]}})
    m.validate()
    logs = []
    ModbusGatewayRunner(m, logger=logs.append)._log_policy()
    assert "warning: point 'v': word_order 'XYZW' ignored (not ABCD, CDAB, BADC or DCBA): " \
           "read as ABCD" in logs, logs
    assert "warning: point 'v': scale 'x' ignored (not a number): its values are not scaled" \
           in logs, logs
    assert decode_point(m.modbus_poll.points[0], regs_bytes([1, 2])) == 0x10002
    print("a stored unusable word order or scale: read as ABCD / unscaled, warned about  OK")


def test_sparkplug_datatypes():
    D = sparkplug.DataType
    for dtype, want in (("uint16", D.UInt16), ("int16", D.Int16), ("uint32", D.UInt32),
                        ("int32", D.Int32), ("float32", D.Float), ("bool", D.Boolean),
                        ("int64", D.Int64), ("uint64", D.UInt64), ("float64", D.Double),
                        ("string", D.String)):
        assert sparkplug.datatype_for(dtype) == want, dtype
    assert sparkplug.datatype_for("int16", 0.1) == D.Double
    assert sparkplug.datatype_for("uint64", 2) == D.Double
    assert sparkplug.datatype_for("string", 0.5) == D.String
    assert sparkplug.datatype_for("bool", 0.5) == D.Boolean
    # a DCMD without a datatype carries an int64 as the raw unsigned 64-bit field
    m = MappingConfig.from_dict({
        "name": "spb", "serial": {"port": "fake"}, "network": {"protocol": "modbus"},
        "mqtt": {"enabled": True, "host": "b", "payload_format": "sparkplug_b"},
        "modbus_poll": {"points": [
            {"name": "total", "fn": 3, "dtype": "int64", "writable": True},
            {"name": "big", "fn": 3, "address": 4, "dtype": "uint64", "writable": True}]}})
    wrote = []
    node = SparkplugNode(m, lambda _line: None, lambda p, v, **_k: wrote.append((p.name, v)))
    node.on_device_command("spBv1.0/ser2net/DCMD/spb/unit1", sparkplug.encode_payload(
        sparkplug.Payload(timestamp=0, seq=None, metrics=[
            sparkplug.Metric(name="total", datatype=D.Int64, value=-3),
            sparkplug.Metric(name="big", datatype=D.UInt64, value=2 ** 64 - 3)]),
        include_datatype=False))
    assert wrote == [("total", -3), ("big", 2 ** 64 - 3)], wrote
    print("Sparkplug B datatypes: unchanged for the existing types, new ones mapped; an "
          "untyped int64 DCMD is read as signed  OK")


def test_plan_blocks():
    def pts(*specs):
        return MappingConfig.from_dict({"modbus_poll": {"points": [
            dict({"name": f"p{i}", "fn": 3}, **s) for i, s in enumerate(specs)]}}
        ).modbus_poll.points

    def plan(points, gap):
        return [(b.fn, b.start, b.count, [p.name for p in b.points], b.single)
                for b in plan_blocks(points, gap)]
    points = pts({"address": 5}, {"address": 0}, {"address": 1, "dtype": "uint32"},
                 {"address": 200}, {"address": 3, "dtype": "string", "length": 2},
                 {"fn": 4, "address": 0}, {"fn": 1, "dtype": "bool", "address": 0},
                 {"fn": 1, "dtype": "bool", "address": 10})
    assert plan(points, 0) == [(3, 0, 6, ["p1", "p2", "p4", "p0"], False),
                               (3, 200, 1, ["p3"], False), (4, 0, 1, ["p5"], False),
                               (1, 0, 1, ["p6"], False), (1, 10, 1, ["p7"], False)]
    assert plan(points, 9)[-1] == (1, 0, 11, ["p6", "p7"], False)
    assert plan(points, 124)[0][:3] == (3, 0, 6), "200 is beyond one read of 125"
    assert plan(pts({"address": 0}, {"address": 124}), 124) == [(3, 0, 125, ["p0", "p1"], False)]
    assert len(plan(pts({"address": 0}, {"address": 125}), 124)) == 2
    assert len(plan(pts({"address": 0}, {"address": 2}), 0)) == 2, "a gap of 1 > 0"
    assert plan(pts({"address": 0, "dtype": "uint64"}, {"address": 1}), 0) == [
        (3, 0, 4, ["p0", "p1"], False)], "overlapping points share the block"
    coils = pts({"fn": 1, "dtype": "bool", "address": 0},
                {"fn": 1, "dtype": "bool", "address": 1999})
    assert plan(coils, 124)[0][:3] == (1, 0, 1) and len(plan(coils, 2000)) == 1
    bits = pts({"dtype": "bool", "bit": 0, "address": 7}, {"dtype": "bool", "bit": 9, "address": 7})
    assert plan(bits, 0) == [(3, 7, 1, ["p0", "p1"], False)], "two bits of one register"
    junk = pts({"address": "x"}, {"address": 1})
    assert plan(junk, 0) == [(3, 0, 0, ["p0"], True), (3, 1, 1, ["p1"], False)]
    print("block plan: per function, in address order, within the gap and one read  OK")


class Slave:
    """A fake RTU slave on TCP: registers per (unit, address), FC3/4 reads, FC6/16 writes.
    ``strict``: a read of an address it has no register at gets exception 02; ``silent``:
    units that never answer; ``delay``: seconds before each reply."""

    def __init__(self, regs, strict=False, delay=0.0):
        self.regs = dict(regs)
        self.strict, self.delay, self.silent = strict, delay, set()
        self.seen = []  # (unit, pdu)

    async def serve(self, reader, writer):
        buf = bytearray()
        with suppress(ConnectionError, OSError):
            while data := await reader.read(512):
                buf += data
                with suppress(ValueError):
                    unit, pdu = modbus.rtu_unwrap(bytes(buf))
                    buf.clear()
                    self.seen.append((unit, bytes(pdu)))
                    if unit in self.silent:
                        continue
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    writer.write(modbus.rtu_wrap(unit, self.answer(unit, pdu)))
                    await writer.drain()

    def reads(self):
        return [(pdu[0], *struct.unpack(">HH", pdu[1:5])) for _u, pdu in self.seen]

    def answer(self, unit, pdu):
        fn, addr, qty = pdu[0], *struct.unpack(">HH", pdu[1:5])
        if fn in (3, 4):
            if self.strict and any((unit, addr + i) not in self.regs for i in range(qty)):
                return bytes([fn | 0x80, 2])
            data = regs_bytes(self.regs.get((unit, addr + i), 0) for i in range(qty))
            return bytes([fn, len(data)]) + data
        if fn == 6:
            self.regs[(unit, addr)] = qty
            return pdu
        if fn == 16:
            for i in range(qty):
                self.regs[(unit, addr + i)] = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
            return pdu[:5]
        return bytes([fn | 0x80, 1])


class Published(dict):
    """The plain-MQTT values a runner published: the last one per point, and all in order."""

    def __init__(self):
        super().__init__()
        self.order = []

    def publish_value(self, name, payload):
        self[name] = payload
        self.order.append((name, payload))

    def publish_raw(self, *_a, **_k):
        pass


async def runner_for(slave, points, logs, **poll):
    server = await asyncio.start_server(slave.serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    m = MappingConfig.from_dict({
        "name": "types", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                    "port": free_port()},
        "mqtt": {"enabled": True, "host": "broker", "base_topic": "plant/t"},
        "modbus_poll": dict({"interval_s": 0.05, "points": points}, **poll)})
    m.validate()
    m.validate_input()
    r = ModbusGatewayRunner(m, logger=logs.append)
    r._resp_timeout = TIMEOUT
    sr, sw = await asyncio.open_connection("127.0.0.1", port)
    r._link.attach(sr, sw)
    published = Published()
    r._mqtt = published
    return r, server, sw, published


async def close(server, sw):
    sw.close()
    server.close()
    with suppress(Exception):
        await asyncio.wait_for(server.wait_closed(), timeout=2)


async def gateway_types():
    slave = Slave({(1, 0): 0x8000, (1, 1): 0x4366,                    # float32 CDAB 230.5
                   (1, 10): 0x0807, (1, 11): 0x0605, (1, 12): 0x0403, (1, 13): 0x0201,
                   (1, 20): 0x4F4D, (1, 21): 0x4546, (1, 22): 0x0000,  # "MOFE" BADC
                   (1, 30): 0x0008,                                    # bit 3 set
                   (1, 40): 0x0001, (1, 41): 0x86A0})                  # uint32 ABCD
    points = [{"name": "volts", "fn": 3, "address": 0, "dtype": "float32", "word_order": "CDAB"},
              {"name": "energy", "fn": 3, "address": 10, "dtype": "uint64", "word_order": "DCBA",
               "writable": True},
              {"name": "tag", "fn": 3, "address": 20, "dtype": "string", "length": 3,
               "word_order": "BADC"},
              {"name": "alarm", "fn": 3, "address": 30, "dtype": "bool", "bit": 3},
              {"name": "count", "fn": 3, "address": 40, "dtype": "uint32"},
              {"name": "sp", "fn": 3, "address": 50, "dtype": "int32", "word_order": "CDAB",
               "writable": True}]
    logs = []
    r, server, sw, published = await runner_for(slave, points, logs)
    try:
        await r._poll_unit(r._units[1])
        assert published == {"volts": b"230.5", "energy": b"72623859790382856", "tag": b"MOFE",
                             "alarm": b"1", "count": b"100000", "sp": b"0"}, published
        assert [struct.unpack(">H", pdu[3:5])[0] for _u, pdu in slave.seen] == [
            2, 4, 3, 1, 2, 2], "each point reads its own registers"
        print("poll: CDAB float, DCBA uint64, BADC string, a register's bit, ABCD uint32  OK")

        slave.seen.clear()
        assert await r.write_point(r.mapping.modbus_poll.points[5], -2) == -2
        _unit, req = slave.seen[0]
        assert req[0] == 16 and req[6:10] == bytes.fromhex("fffeffff"), req.hex(" ")
        assert await r.write_point(r.mapping.modbus_poll.points[1], 2 ** 64 - 2) == 2 ** 64 - 2
        _unit, req = slave.seen[2]
        assert req[0] == 16 and req[5] == 8 and req[6:14] == bytes.fromhex("feff" + "ff" * 6), \
            req.hex(" ")
        print("MQTT write: registers sent in the point's word order (FC16), read back  OK")

        tag, alarm = r.mapping.modbus_poll.points[2:4]
        tag.writable = alarm.writable = True  # (as a hand-edited config.json could have it)
        for p in (tag, alarm):
            try:
                await r.write_point(p, 1)
                raise AssertionError(f"wrote {p.name}")
            except ValueError as e:
                assert "MQTT can't write it" in str(e), e
        assert len(slave.seen) == 4, "nothing was sent for a string or a register's bit"
        print("a string or a bit of a register is never written  OK")
    finally:
        await close(server, sw)


BLOCK_REGS = {(1, a): 100 + a for a in (0, 1, 2, 3, 4, 5, 7, 8)}
BLOCK_POINTS = [{"name": "a", "fn": 3, "address": 0},
                {"name": "b", "fn": 3, "address": 1, "dtype": "uint32"},
                {"name": "c", "fn": 3, "address": 4, "writable": True},
                {"name": "d", "fn": 3, "address": 7},
                {"name": "e", "fn": 3, "address": 8, "dtype": "bool", "bit": 2},
                {"name": "f", "fn": 4, "address": 0}]


async def gateway_blocks():
    singles = Slave(BLOCK_REGS)
    logs = []
    r, server, sw, plain = await runner_for(singles, BLOCK_POINTS, logs)
    await r._poll_unit(r._units[1])
    await close(server, sw)
    assert len(singles.seen) == 6, singles.reads()

    slave = Slave(BLOCK_REGS)
    r, server, sw, published = await runner_for(slave, BLOCK_POINTS, logs, block_reads=True,
                                                block_gap=1)
    try:
        r._log_policy()
        assert "block reads: 6 point(s) with 3 request(s) per poll (at most 1 unused " \
               "register(s) or coil(s) within a block)" in logs, logs
        await r._poll_unit(r._units[1])
        assert dict(published) == dict(plain), (published, plain)
        assert slave.reads() == [(3, 0, 5), (3, 7, 2), (4, 0, 1)], slave.reads()
        print("block reads: 3 requests instead of 6, the same values  OK")
    finally:
        await close(server, sw)

    # a block the slave refuses (exception 02): its points one by one, from then on
    slave = Slave({k: v for k, v in BLOCK_REGS.items() if k != (1, 3)}, strict=True)
    logs = []
    r, server, sw, published = await runner_for(slave, BLOCK_POINTS[:4], logs,
                                                block_reads=True, block_gap=3)
    try:
        await r._poll_unit(r._units[1])
        assert slave.reads() == [(3, 0, 8), (3, 0, 1), (3, 1, 2), (3, 4, 1), (3, 7, 1)], \
            slave.reads()
        assert published == {"a": b"100", "b": b"6619238", "c": b"104", "d": b"107"}, published
        refused = [line for line in logs if "block read" in line]
        assert refused == ["unit 1: block read of 8 registers from 0 (FC3) refused (modbus "
                           "exception 0x02); its 4 points are read one at a time from now on"], logs
        slave.seen.clear()
        await r._poll_unit(r._units[1])
        assert len(slave.seen) == 4 and len([x for x in logs if "block read" in x]) == 1
        assert r._units[1].state == "online"
        print("block refused with exception 02: read point by point from then on, logged "
              "once  OK")

        # no reply to a block: no reply for each of its points; the unit goes offline
        slave.silent.add(1)
        for _ in range(UNIT_OFFLINE_AFTER):
            await r._poll_unit(r._units[1])
        u = r._units[1]
        assert u.state == "offline" and u.cause == "no reply", (u.state, u.cause)
        assert u.errors == 4 * UNIT_OFFLINE_AFTER, u.errors
        print("no reply to a block: each of its points failed, the unit offline after "
              f"{UNIT_OFFLINE_AFTER} polls  OK")
    finally:
        await close(server, sw)

    # a write takes its turn between two blocks; the value read before it is not published
    slave = Slave(BLOCK_REGS, delay=0.05)
    logs = []
    r, server, sw, published = await runner_for(slave, BLOCK_POINTS[:4], logs,
                                                block_reads=True, block_gap=1)
    try:
        poll = asyncio.create_task(r._poll_unit(r._units[1]))
        while not slave.seen:
            await asyncio.sleep(0.005)
        answers = []
        r._write_soon(r.mapping.modbus_poll.points[2], 9, lambda err, v: answers.append((err, v)))
        await asyncio.wait_for(poll, timeout=5)
        for _ in range(200):
            if answers:
                break
            await asyncio.sleep(0.01)
        assert answers == [(None, 9)], answers
        fns = [(pdu[0], struct.unpack(">H", pdu[1:3])[0]) for _u, pdu in slave.seen]
        assert fns == [(3, 0), (6, 4), (3, 7), (3, 4)], fns
        c = [v for n, v in published.order if n == "c"]
        assert c == [b"9"], f"the value read before the write was published: {c}"
        print("a write between two blocks: block, write, block, read-back; no stale value  OK")
    finally:
        await close(server, sw)


def main():
    test_word_orders()
    test_strings_and_bits()
    test_config_rules()
    test_stored_unusable_keys()
    test_sparkplug_datatypes()
    test_plan_blocks()
    asyncio.run(gateway_types())
    asyncio.run(gateway_blocks())
    print("\nPASS: Modbus point reads and writes")


if __name__ == "__main__":
    main()
