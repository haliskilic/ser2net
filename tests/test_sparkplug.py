"""Sparkplug B payload codec unit tests (hand-written protobuf, no deps).

Pure stdlib, cross-platform -- round-trips every supported datatype, checks the
exact wire bytes of a small payload worked out by hand from the protobuf encoding
rules, feeds the decoder unknown fields, truncated and random input, and covers
the topic / id / seq / datatype helpers; and the edge node's handling of NCMD / DCMD
commands, with the bus write replaced by a recorder. Run:
    python3 tests/test_sparkplug.py
"""
import math
import os
import random
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine import modbus, sparkplug
from app.engine.sparkplug import DataType as T
from app.engine.sparkplug import Metric, Payload


def expect_value_error(fn, *args, match="", **kwargs):
    """Call fn(*args, **kwargs) and require a ValueError (whose message contains `match`)."""
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        assert match in str(e), (fn.__name__, args, str(e))
        return str(e)
    raise AssertionError(f"expected ValueError from {fn.__name__}{args!r}")


def f32(value):
    """The float32-rounded value a Float metric decodes to."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def key(number, wire_type):
    return sparkplug._varint(number << 3 | wire_type)


def metric_bytes(*fields):
    """Wrap raw metric field bytes as a Payload.metrics entry (field 2, length-delimited)."""
    body = b"".join(fields)
    return key(2, 2) + sparkplug._varint(len(body)) + body


def test_varint_primitives():
    vectors = {0: "00", 1: "01", 127: "7f", 128: "8001", 300: "ac02", 16384: "808001",
               0xFFFFFFFF: "ffffffff0f", 2 ** 64 - 1: "ffffffffffffffffff01"}
    for value, hexstr in vectors.items():
        assert sparkplug._varint(value) == bytes.fromhex(hexstr), (value, sparkplug._varint(value))
        assert sparkplug._read_varint(bytes.fromhex("aa" + hexstr + "bb"), 1) == (
            value, 1 + len(hexstr) // 2)
    for bad in (-1, 2 ** 64):
        expect_value_error(sparkplug._varint, bad, match="out of range")
    expect_value_error(sparkplug._read_varint, b"", 0, match="truncated")
    expect_value_error(sparkplug._read_varint, b"\xff\xff", 0, match="truncated")
    expect_value_error(sparkplug._read_varint, b"\x80" * 10 + b"\x01", 0, match="longer than 10")
    print("varint encode/decode vectors, truncated and over-long varints  OK")


def test_wire_bytes_by_hand():
    payload = Payload(timestamp=300, metrics=[
        Metric(name="a", alias=5, datatype=T.Int16, value=-2),
        Metric(name="f", datatype=T.Float, value=1.5),
        Metric(alias=6, timestamp=1, datatype=T.Double, value=-2.0),
    ], seq=1)
    # Worked out from the protobuf wire format (key = field << 3 | wire type):
    #   08 AC 02        Payload.timestamp (1, varint) = 300  (300 = 0b10_0101100 -> AC 02)
    #   12 0D ...       Payload.metrics (2, len) 13 bytes:
    #     0A 01 61        name (1, len) "a"
    #     10 05           alias (2, varint) 5
    #     20 02           datatype (4, varint) 2 = Int16
    #     50 FE FF FF FF 0F  int_value (10, varint): -2 as uint32 0xFFFFFFFE
    #   12 0A ...       metrics, 10 bytes:
    #     0A 01 66        name "f"
    #     20 09           datatype 9 = Float
    #     65 00 00 C0 3F  float_value (12, fixed32): 1.5 = 0x3FC00000, little-endian
    #   12 0F ...       metrics, 15 bytes (alias only, no name):
    #     10 06           alias 6
    #     18 01           timestamp (3, varint) 1
    #     20 0A           datatype 10 = Double
    #     69 00 00 00 00 00 00 00 C0  double_value (13, fixed64): -2.0 = 0xC000000000000000, LE
    #   18 01           Payload.seq (3, varint) = 1
    expected = bytes.fromhex(
        "08ac02"
        "120d" "0a0161" "1005" "2002" "50feffffff0f"
        "120a" "0a0166" "2009" "650000c03f"
        "120f" "1006" "1801" "200a" "69" "00000000000000c0"
        "1801")
    got = sparkplug.encode_payload(payload)
    assert got == expected, got.hex(" ")
    assert sparkplug.decode_payload(expected) == payload
    print("encode_payload matches hand-computed protobuf bytes (varint, len, fixed32/64)  OK")


ROUND_TRIP_VALUES = [
    (T.Int8, [-128, -1, 0, 127]),
    (T.Int16, [-32768, -2, 0, 12345, 32767]),
    (T.Int32, [-2 ** 31, -1, 0, 2 ** 31 - 1]),
    (T.Int64, [-2 ** 63, -1, 0, 2 ** 63 - 1]),
    (T.UInt8, [0, 200, 255]),
    (T.UInt16, [0, 40000, 65535]),
    (T.UInt32, [0, 3000000000, 2 ** 32 - 1]),
    (T.UInt64, [0, 2 ** 63, 2 ** 64 - 1]),
    (T.Float, [0.0, 1.5, -23.25, 3.4028234663852886e38, 1e-45]),
    (T.Double, [0.0, 0.1, -1e300, 5e-324, 2 ** 53 + 0.0]),
    (T.Boolean, [True, False]),
    (T.String, ["", "hello", "çalışma ✓ ünïcode", "slash/plus+hash#"]),
    (T.DateTime, [0, 1730000000000, 2 ** 64 - 1]),
    (T.Text, ["", "multi\nline\ttext"]),
    (T.Bytes, [b"", b"\x00\xff\x10", bytes(range(256))]),
]


def test_round_trip_every_datatype():
    metrics = []
    for datatype, values in ROUND_TRIP_VALUES:
        for i, value in enumerate(values):
            m = Metric(name=f"{datatype.name}/{i}", datatype=datatype, value=value,
                       alias=len(metrics), timestamp=1730000000000 + i)
            # alone in a payload ...
            back = sparkplug.decode_payload(sparkplug.encode_payload(
                Payload(timestamp=1730000000123, metrics=[m], seq=len(metrics) % 256)))
            want = f32(value) if datatype == T.Float else value
            got = back.metrics[0]
            assert got.value == want and type(got.value) is type(want), (m, got)
            assert (got.name, got.datatype, got.alias, got.timestamp, got.is_null) == (
                m.name, datatype, m.alias, m.timestamp, False), got
            assert back.timestamp == 1730000000123 and back.seq == len(metrics) % 256
            metrics.append(Metric(m.name, datatype, want, m.alias, m.timestamp))
    # ... and all together, plus null metrics, an alias-only metric, uuid and body
    metrics += [Metric(name="nothing", datatype=T.Int32, is_null=True),
                Metric(name="null-string", datatype=T.String, is_null=True),
                Metric(alias=99, datatype=T.UInt16, value=5)]
    full = Payload(timestamp=0, metrics=metrics, seq=255, uuid="ser2net", body=b"\x01\x02")
    assert sparkplug.decode_payload(sparkplug.encode_payload(full)) == full
    # empty / minimal payloads; NDEATH carries no seq
    assert sparkplug.encode_payload(Payload(None, [], None)) == b""
    assert sparkplug.decode_payload(b"") == Payload(None, [], None)
    ndeath = Payload(timestamp=5, metrics=[Metric(sparkplug.BDSEQ, T.UInt64, 3)], seq=None)
    assert sparkplug.decode_payload(sparkplug.encode_payload(ndeath)) == ndeath
    # is_null wins over a (stale) value, and the value is not sent
    stale = Metric(name="t", datatype=T.Float, value=21.5, is_null=True)
    wire = sparkplug.encode_payload(Payload(None, [stale], None))
    assert wire == metric_bytes(key(1, 2) + b"\x01t", key(4, 0) + b"\x09", key(7, 0) + b"\x01")
    assert sparkplug.decode_payload(wire).metrics == [Metric(name="t", datatype=T.Float, is_null=True)]
    # NaN / infinity are legal float values (compared separately: NaN != NaN)
    odd = [Metric("n", T.Double, math.nan), Metric("i", T.Float, -math.inf)]
    odd_back = sparkplug.decode_payload(sparkplug.encode_payload(Payload(None, odd, 0))).metrics
    assert math.isnan(odd_back[0].value) and odd_back[1].value == -math.inf
    print(f"round trip of all {len(ROUND_TRIP_VALUES)} supported datatypes incl. negatives, "
          f"nulls, aliases, timestamps  OK")


def test_signed_ints_on_the_wire():
    def value_field(datatype, value):
        wire = sparkplug.encode_payload(Payload(None, [Metric(datatype=datatype, value=value)], None))
        return wire[4:]  # skip 12 <len> 20 <datatype>
    # Int8/16/32 -> int_value (uint32) as 32-bit two's complement, Int64 -> long_value
    assert value_field(T.Int16, -1) == bytes.fromhex("50ffffffff0f")
    assert value_field(T.Int8, -128) == bytes.fromhex("5080ffffff0f")
    assert value_field(T.Int32, -2 ** 31) == bytes.fromhex("508080808008")
    assert value_field(T.Int64, -1) == bytes.fromhex("58ffffffffffffffffff01")
    assert value_field(T.UInt32, 2 ** 32 - 1) == bytes.fromhex("50ffffffff0f")
    assert value_field(T.DateTime, 1) == bytes.fromhex("5801")
    assert value_field(T.Boolean, True) == bytes.fromhex("7001")
    assert value_field(T.String, "") == bytes.fromhex("7a00")
    assert value_field(T.Bytes, b"\x00") == bytes.fromhex("820101" "00")

    def decode_one(*fields):
        return sparkplug.decode_payload(metric_bytes(*fields)).metrics[0].value

    def int_value(v):
        return key(10, 0) + sparkplug._varint(v)

    def long_value(v):
        return key(11, 0) + sparkplug._varint(v)

    def dtype(t):
        return key(4, 0) + sparkplug._varint(t)

    # the narrow two's complement some encoders send decodes too (Int16 -1 as 0xFFFF)
    assert decode_one(dtype(T.Int16), int_value(0xFFFF)) == -1
    assert decode_one(dtype(T.Int8), int_value(0x80)) == -128
    assert decode_one(dtype(T.Int8), int_value(0xFFFFFF80)) == -128
    assert decode_one(dtype(T.Int32), int_value(0x7FFFFFFF)) == 2 ** 31 - 1
    assert decode_one(dtype(T.Int16), long_value(2 ** 64 - 5)) == -5         # sign-extended to 64
    assert decode_one(dtype(T.UInt32), long_value(3000000000)) == 3000000000  # UInt32 in long_value
    assert decode_one(dtype(T.Int64), long_value(2 ** 63)) == -2 ** 63
    assert decode_one(dtype(T.Float), key(13, 1) + struct.pack("<d", 0.5)) == 0.5
    # values that fit neither form are rejected rather than silently truncated
    expect_value_error(decode_one, dtype(T.Int16), int_value(0x12345), match="out of range for Int16")
    expect_value_error(decode_one, dtype(T.Int8), int_value(0xFFFF7FFF), match="out of range")
    expect_value_error(decode_one, dtype(T.UInt8), int_value(256), match="out of range for UInt8")
    expect_value_error(decode_one, dtype(T.UInt16), int_value(0xFFFFFFFF), match="out of range")
    expect_value_error(decode_one, dtype(T.Int16), key(15, 2) + b"\x011", match="wrong field")
    expect_value_error(decode_one, dtype(T.Boolean), int_value(1), match="wrong field")
    print("signed ints: 32/64-bit two's complement on encode, both forms + range checks on decode  OK")


def test_metrics_without_datatype():
    metrics = [Metric(name="i16", datatype=T.Int16, value=-1),
               Metric(alias=2, datatype=T.UInt16, value=5),
               Metric(name="f", datatype=T.Float, value=1.5),
               Metric(name=sparkplug.REBIRTH, datatype=T.Boolean, value=True),
               Metric(name="s", datatype=T.String, value="x")]
    with_types = sparkplug.encode_payload(Payload(1, metrics, 0))
    ndata = sparkplug.encode_payload(Payload(1, metrics, 0), include_datatype=False)
    assert len(with_types) - len(ndata) == 2 * len(metrics)   # one "20 xx" per metric dropped
    back = sparkplug.decode_payload(ndata).metrics
    assert all(m.datatype == 0 for m in back)
    # without a datatype the plain wire value comes back (int_value is unsigned 32-bit)
    assert [m.value for m in back] == [0xFFFFFFFF, 5, 1.5, True, "x"], back
    # null metrics need no datatype either
    assert sparkplug.decode_payload(sparkplug.encode_payload(
        Payload(None, [Metric(alias=3, is_null=True)], 1))).metrics == [Metric(alias=3, is_null=True)]
    # unmodelled datatypes pass their plain value through on decode
    uuid = metric_bytes(key(4, 0) + bytes([T.UUID]), key(15, 2) + b"\x03abc")
    assert sparkplug.decode_payload(uuid).metrics[0].value == "abc"
    arr = metric_bytes(key(4, 0) + bytes([T.Int16Array]), key(16, 2) + b"\x02\xff\xff")
    assert sparkplug.decode_payload(arr).metrics[0].value == b"\xff\xff"
    print("include_datatype=False (NDATA/NCMD style) and datatype-less decode keep plain values  OK")


def test_decode_skips_unknown_fields():
    group = key(99, 3) + key(1, 0) + b"\x01" + key(98, 3) + key(98, 4) + key(99, 4)
    unknown_99 = (key(99, 0) + b"\x96\x01"                 # varint 150
                  + key(99, 1) + bytes(range(1, 9))         # fixed64
                  + key(99, 2) + b"\x03abc"                 # length-delimited
                  + key(99, 5) + b"\x01\x02\x03\x04"        # fixed32
                  + group)                                  # (deprecated) nested groups
    assert unknown_99.startswith(bytes.fromhex("9806" "9601" "9906"))  # field 99 key = 98 06
    metric = (key(5, 0) + b"\x01"                           # is_historical
              + key(6, 0) + b"\x01"                         # is_transient
              + key(8, 2) + b"\x02\x08\x01"                 # metadata
              + key(1, 2) + b"\x01t"                        # name
              + key(9, 2) + b"\x04\x0a\x02k1"               # properties
              + unknown_99
              + key(4, 0) + bytes([T.UInt16])               # datatype
              + key(17, 2) + b"\x02\x08\x02"                # dataset_value
              + key(10, 0) + b"\x07"                        # int_value 7
              + key(1, 0) + b"\x05"                         # name with the wrong wire type
              + unknown_99)
    assert key(17, 2) == b"\x8a\x01"
    data = (unknown_99 + key(1, 0) + b"\x2a"                # timestamp 42
            + metric_bytes(metric)
            + key(6, 2) + b"\x01\x00"                       # Payload extension field 6
            + key(1, 2) + b"\x01\x00"                       # timestamp with the wrong wire type
            + key(3, 0) + b"\x07" + unknown_99)             # seq 7
    got = sparkplug.decode_payload(data)
    assert got == Payload(timestamp=42, metrics=[Metric(name="t", datatype=T.UInt16, value=7)],
                          seq=7), got
    # a DataSet metric: its value is skipped, the metric itself is kept
    ds = metric_bytes(key(1, 2) + b"\x02ds", key(4, 0) + bytes([T.DataSet]), key(17, 2) + b"\x02\x08\x02")
    assert sparkplug.decode_payload(ds).metrics == [Metric(name="ds", datatype=T.DataSet)]
    print("decode skips fields 5/6/8/9/17, field 99 of wire types 0/1/2/5, groups, wrong wire types  OK")


def test_truncated_and_malformed_input():
    payload = Payload(timestamp=1730000000000, metrics=[
        Metric(name="speed", alias=1, datatype=T.Float, value=1.5, timestamp=1730000000000),
        Metric(name="count", datatype=T.Int64, value=-7),
        Metric(name="label", datatype=T.String, value="pump"),
    ], seq=9, uuid="u")
    data = sparkplug.encode_payload(payload)
    # top-level field boundaries: cutting exactly there leaves a shorter valid payload
    parts = [sparkplug.encode_payload(Payload(payload.timestamp, [], None))]
    parts += [sparkplug.encode_payload(Payload(None, [m], None)) for m in payload.metrics]
    parts += [sparkplug.encode_payload(Payload(None, [], payload.seq, uuid=payload.uuid))]
    assert b"".join(parts) == data
    boundaries, pos = {0}, 0
    for part in parts[:-1]:
        pos += len(part)
        boundaries.add(pos)
    boundaries.add(pos + 2)     # between seq and uuid
    for cut in range(len(data)):
        if cut in boundaries:
            sparkplug.decode_payload(data[:cut])
        else:
            expect_value_error(sparkplug.decode_payload, data[:cut])
    for bad in (b"\x08",                     # key without its value
                b"\x08\x80",                 # truncated varint
                b"\x08" + b"\xff" * 10 + b"\x01",  # varint longer than 10 bytes
                b"\x12\x05\x0a",             # length 5, one byte present
                b"\x12" + sparkplug._varint(2 ** 63) + b"\x00",  # absurd length
                b"\x0d\x00\x00",             # fixed32 cut short
                b"\x09" + b"\x00" * 7,       # fixed64 cut short
                b"\x00\x00",                 # field number 0
                b"\x0e\x00", b"\x0f\x00",    # wire types 6 and 7 don't exist
                b"\x0c",                     # end-group without a start
                b"\x0b\x14",                 # group 1 closed as group 2
                b"\x0b\x08\x01",             # unterminated group
                metric_bytes(b"\x0a\x01\xff"),            # name is not UTF-8
                b"\x22\x02\xc3\x28"):                     # uuid is not UTF-8
        expect_value_error(sparkplug.decode_payload, bad)
    try:
        sparkplug.decode_payload("not bytes")
        raise AssertionError("expected TypeError")
    except TypeError:
        pass
    # fuzz: random bytes and mutated valid payloads give a Payload or ValueError -- nothing else
    rng = random.Random(4242)
    samples = [bytes(rng.randrange(256) for _ in range(rng.randrange(40))) for _ in range(3000)]
    for _ in range(3000):
        mutated = bytearray(data)
        for _ in range(rng.randint(1, 4)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        samples.append(bytes(mutated[:rng.randint(1, len(mutated))]))
    decoded = 0
    for sample in samples:
        try:
            assert isinstance(sparkplug.decode_payload(sample), Payload)
            decoded += 1
        except ValueError:
            pass
    assert 0 < decoded < len(samples), decoded
    print(f"truncation at every byte, malformed keys/lengths/groups, {len(samples)} fuzz inputs "
          f"-> only ValueError  OK")


def test_encode_rejects_bad_metrics():
    def enc(*metrics, seq=0, **fields):
        return sparkplug.encode_payload(Payload(fields.pop("timestamp", None), list(metrics), seq,
                                                **fields))
    for datatype in (T.DataSet, T.Template, T.UUID, T.File, T.Int8Array, T.PropertySet, 99, -1):
        expect_value_error(enc, Metric("m", datatype, 1), match="unsupported Sparkplug datatype")
    expect_value_error(enc, Metric("m", 0, 1), match="datatype is needed")
    expect_value_error(enc, Metric("m", T.Int16), match="is_null")
    for datatype, value in ((T.Int8, 128), (T.Int8, -129), (T.UInt8, 256), (T.UInt8, -1),
                            (T.Int16, 32768), (T.UInt16, 65536), (T.Int32, 2 ** 31),
                            (T.UInt32, 2 ** 32), (T.UInt32, -1), (T.Int64, 2 ** 63),
                            (T.UInt64, 2 ** 64), (T.UInt64, -1), (T.DateTime, -1),
                            (T.Int32, "5"), (T.Int32, 5.0), (T.Int32, True)):
        expect_value_error(enc, Metric("m", datatype, value), match="is not a valid")
    for datatype, value in ((T.Float, "1.5"), (T.Float, True), (T.Double, [1.0])):
        expect_value_error(enc, Metric("m", datatype, value), match="must be a number")
    expect_value_error(enc, Metric("m", T.Float, 1e39), match="out of range for Float")
    expect_value_error(enc, Metric("m", T.Double, 10 ** 400), match="out of range for Double")
    for value in ("false", 2, -1, 1.0):
        expect_value_error(enc, Metric("m", T.Boolean, value), match="True/False")
    expect_value_error(enc, Metric("m", T.String, b"x"), match="must be a str")
    expect_value_error(enc, Metric("m", T.Text, 5), match="must be a str")
    expect_value_error(enc, Metric("m", T.String, "\ud800"), match="UTF-8")
    expect_value_error(enc, Metric("m", T.Bytes, "x"), match="must be bytes")
    expect_value_error(enc, Metric(5, T.Int16, 1), match="name must be a str")
    for seq in (256, -1, True, 1.0):
        expect_value_error(enc, seq=seq, match="payload seq")
    for ts in (-1, 2 ** 64):
        expect_value_error(enc, timestamp=ts, match="payload timestamp")
    expect_value_error(enc, Metric("m", T.Int16, 1, alias=2 ** 64), match="alias")
    expect_value_error(enc, Metric("m", T.Int16, 1, alias=-1), match="alias")
    expect_value_error(enc, Metric("m", T.Int16, 1, timestamp=-1), match="timestamp")
    expect_value_error(enc, uuid=5, match="uuid")
    expect_value_error(enc, body="text", match="body")
    # bytearray / memoryview are fine for Bytes and body
    out = enc(Metric("b", T.Bytes, bytearray(b"\x01")), body=memoryview(b"\x02"))
    back = sparkplug.decode_payload(out)
    assert back.metrics[0].value == b"\x01" and back.body == b"\x02"
    print("encode_payload rejects unsupported datatypes, out-of-range and mistyped values  OK")


def test_seq_wrap():
    assert sparkplug.next_seq(0) == 1
    assert sparkplug.next_seq(254) == 255
    assert sparkplug.next_seq(255) == 0
    seq, seen = 0, []
    for _ in range(256):
        seen.append(seq)
        seq = sparkplug.next_seq(seq)
    assert seq == 0 and sorted(seen) == list(range(256))
    for seq in (0, 255):
        back = sparkplug.decode_payload(sparkplug.encode_payload(Payload(1, [], seq)))
        assert back.seq == seq
    print("next_seq wraps 255 -> 0 and seq 0..255 round-trips  OK")


def test_topics_and_ids():
    assert sparkplug.TOPIC_NAMESPACE == "spBv1.0"
    assert (sparkplug.BDSEQ, sparkplug.REBIRTH) == ("bdSeq", "Node Control/Rebirth")
    for mt in ("NBIRTH", "NDEATH", "NDATA", "NCMD"):
        assert sparkplug.topic("Plant 1", mt, "edge-01") == f"spBv1.0/Plant 1/{mt}/edge-01"
        expect_value_error(sparkplug.topic, "G", mt, "E", "dev", match="no device_id")
    for mt in ("DBIRTH", "DDEATH", "DDATA", "DCMD"):
        assert sparkplug.topic("G", mt, "E", "pump.3") == f"spBv1.0/G/{mt}/E/pump.3"
        expect_value_error(sparkplug.topic, "G", mt, "E", match="needs a device_id")
    for bad_id in ("", "a/b", "a+b", "a#", None, 5):
        expect_value_error(sparkplug.topic, bad_id, "NDATA", "E", match="group_id")
        expect_value_error(sparkplug.topic, "G", "NDATA", bad_id, match="edge_node_id")
        expect_value_error(sparkplug.topic, "G", "DDATA", "E", bad_id, match="device_id")
        expect_value_error(sparkplug.state_topic, bad_id, match="host_id")
    for bad_type in ("NFOO", "ndata", "", "spBv1.0"):
        expect_value_error(sparkplug.topic, "G", bad_type, "E", match="message type")
    # STATE has its own shape in Sparkplug 3.0: spBv1.0/STATE/<host_id>
    expect_value_error(sparkplug.topic, "G", "STATE", "E", match="state_topic")
    assert "STATE" in sparkplug.MESSAGE_TYPES
    assert sparkplug.state_topic("scada-1") == "spBv1.0/STATE/scada-1"
    print("topic()/state_topic(): namespace, node vs device types, id validation  OK")


def test_sanitize_id():
    assert sparkplug.sanitize_id("  Line 1/Pump#2+x  ") == "Line_1_Pump_2_x"
    assert sparkplug.sanitize_id("edge-01.local") == "edge-01.local"
    assert sparkplug.sanitize_id("a\tb\nc\x00d\x7fe") == "a_b_c_d_e"
    assert sparkplug.sanitize_id("çalışma alanı") == "çalışma_alanı"
    assert sparkplug.sanitize_id("///") == "___"
    for empty in ("", "   ", "\t\n", None):
        assert sparkplug.sanitize_id(empty) == "node", empty
    rng = random.Random(7)
    for _ in range(500):
        text = "".join(rng.choice("ab /+#\t\n\x00_çğ") for _ in range(rng.randrange(8)))
        node = sparkplug.sanitize_id(text)
        sparkplug.topic(node, "DDATA", node, node)   # always a valid id
    print("sanitize_id replaces reserved chars/whitespace, falls back to 'node'  OK")


def test_datatype_for_modbus_values():
    table = {"uint16": T.UInt16, "int16": T.Int16, "uint32": T.UInt32, "int32": T.Int32,
             "float32": T.Float, "bool": T.Boolean}
    for dtype, datatype in table.items():
        assert sparkplug.datatype_for(dtype) == datatype, dtype
        assert sparkplug.datatype_for(dtype, 0) == datatype, dtype  # 0 = unscaled, as decode_value
        want_scaled = T.Boolean if dtype == "bool" else T.Double
        assert sparkplug.datatype_for(dtype, 0.1) == want_scaled, dtype
    expect_value_error(sparkplug.datatype_for, "float64", match="unknown dtype")
    # every polled value (modbus.decode_value) encodes cleanly under its datatype
    raw = {"uint16": b"\xff\xff", "int16": b"\x80\x00", "uint32": b"\xff\xff\xff\xff",
           "int32": b"\x80\x00\x00\x00", "float32": struct.pack(">f", -23.5)}
    for dtype, data in raw.items():
        for scale in (1.0, 0.1):
            value = modbus.decode_value(data, dtype, scale)
            m = Metric(name=dtype, datatype=sparkplug.datatype_for(dtype, scale), value=value)
            back = sparkplug.decode_payload(sparkplug.encode_payload(Payload(0, [m], 0))).metrics[0]
            assert back.value == value, (dtype, scale, value, back)
    coil = modbus.decode_bits(b"\x01", 1)[0]
    m = Metric(name="coil", datatype=sparkplug.datatype_for("bool"), value=coil)
    assert sparkplug.decode_payload(sparkplug.encode_payload(Payload(0, [m], 0))).metrics[0].value is True
    print("datatype_for maps Modbus dtypes (scaled -> Double) and polled values encode  OK")


def test_edge_node_message_flow():
    # NBIRTH (with datatypes) -> NDATA by alias (no datatypes) -> host NCMD rebirth -> NDEATH
    group, node = "Plant", sparkplug.sanitize_id("edge 01")
    birth = Payload(timestamp=1730000000000, seq=0, metrics=[
        Metric(sparkplug.BDSEQ, T.UInt64, 0),
        Metric(sparkplug.REBIRTH, T.Boolean, False),
        Metric("pump/speed", T.Float, 23.5, alias=1),
    ])
    assert sparkplug.topic(group, "NBIRTH", node) == "spBv1.0/Plant/NBIRTH/edge_01"
    assert sparkplug.decode_payload(sparkplug.encode_payload(birth)) == birth
    data = Payload(timestamp=1730000001000, seq=sparkplug.next_seq(birth.seq),
                   metrics=[Metric(alias=1, datatype=T.Float, value=24.0)])
    got = sparkplug.decode_payload(sparkplug.encode_payload(data, include_datatype=False))
    assert got.seq == 1 and got.metrics == [Metric(alias=1, value=24.0)]
    ncmd = sparkplug.encode_payload(Payload(1730000002000, [Metric(sparkplug.REBIRTH, T.Boolean, True)],
                                            None), include_datatype=False)
    cmd = sparkplug.decode_payload(ncmd)
    assert [(m.name, m.value) for m in cmd.metrics] == [(sparkplug.REBIRTH, True)]
    death = Payload(timestamp=1730000003000, seq=None, metrics=[Metric(sparkplug.BDSEQ, T.UInt64, 0)])
    assert sparkplug.decode_payload(sparkplug.encode_payload(death)) == death
    print("NBIRTH / alias NDATA / NCMD rebirth / NDEATH flow round-trips  OK")


NODE_POINTS = [
    {"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool", "writable": True},
    {"name": "valve", "unit": 1, "fn": 1, "address": 1, "dtype": "bool"},
    {"name": "level", "unit": 2, "fn": 3, "address": 0, "dtype": "uint16", "writable": True},
]


def edge_node(**network):
    """A SparkplugNode for NODE_POINTS whose writes are recorded instead of reaching a bus,
    and whose births are counted instead of published (no broker, no gateway)."""
    from app.config import MappingConfig
    from app.engine.sparkplug_node import SparkplugNode
    m = MappingConfig.from_dict({
        "name": "edge", "serial": {"port": "COM3"},
        "network": dict({"protocol": "modbus", "port": 502}, **network),
        "mqtt": {"enabled": True, "host": "h", "base_topic": "t", "payload_format": "sparkplug_b"},
        "modbus_poll": {"points": NODE_POINTS}})
    m.validate()
    writes, logs, births = [], [], []
    node = SparkplugNode(m, logs.append, lambda point, value: writes.append((point.name, value)))
    node.birth = lambda: births.append(1)

    def command(kind, device, *metrics):
        topic = sparkplug.topic(node.group, kind, node.node, device)
        payload = sparkplug.encode_payload(Payload(0, list(metrics), None), include_datatype=False)
        (node.on_node_command if kind == "NCMD" else node.on_device_command)(topic, payload)
    return node, command, writes, logs, births


def test_node_commands():
    """A DCMD writes only a writable point of its own device; an NCMD writes nothing and
    asks for a rebirth only with a Boolean true; log lines quote long names short."""
    _node, command, writes, logs, births = edge_node()
    command("DCMD", "unit1", Metric("pump", T.Boolean, True), Metric("valve", T.Boolean, True),
            Metric("level", T.UInt16, 5))           # level is a point of unit2
    command("DCMD", "unit2", Metric("level", T.UInt16, 7))
    command("DCMD", "unit9", Metric("level", T.UInt16, 9))   # no such device
    command("NCMD", None, Metric("pump", T.Boolean, True))   # points are written with a DCMD
    assert writes == [("pump", True), ("level", 7)], writes
    assert sum("ignored (not a writable point)" in line for line in logs) == 2, logs
    for value, datatype in ((1, T.Int32), ("true", T.String), (False, T.Boolean)):
        command("NCMD", None, Metric(sparkplug.REBIRTH, datatype, value))
    assert births == [], "a rebirth needs a Boolean true"
    command("NCMD", None, Metric(sparkplug.REBIRTH, T.Boolean, True))
    assert births == [1]
    command("DCMD", "unit1", Metric("x" * 5000, T.Boolean, True))
    command("NCMD", None, Metric("y" * 5000, T.Boolean, True))
    assert writes == [("pump", True), ("level", 7)] and all(len(line) < 200 for line in logs), \
        max(logs, key=len)[:120]
    print("edge node: DCMD writes only its device's writable points, NCMD never writes, "
          "rebirth only on Boolean true, long names logged short  OK")


def test_node_read_only():
    """On a read-only mapping a DCMD writes nothing and is logged; it is checked for each
    command, so it follows the mapping's setting without a new session."""
    node, command, writes, logs, _births = edge_node(read_only=True)
    command("DCMD", "unit1", Metric("pump", T.Boolean, True))
    command("DCMD", "unit2", Metric("level", T.UInt16, 3))
    assert writes == [], writes
    assert sum("refused: the mapping is read-only" in line for line in logs) == 2, logs
    node.mapping.network.read_only = False
    command("DCMD", "unit1", Metric("pump", T.Boolean, True))
    assert writes == [("pump", True)], writes
    print("edge node: read-only refuses (and logs) DCMD writes, checked per command  OK")


def main():
    test_varint_primitives()
    test_wire_bytes_by_hand()
    test_round_trip_every_datatype()
    test_signed_ints_on_the_wire()
    test_metrics_without_datatype()
    test_decode_skips_unknown_fields()
    test_truncated_and_malformed_input()
    test_encode_rejects_bad_metrics()
    test_seq_wrap()
    test_topics_and_ids()
    test_sanitize_id()
    test_datatype_for_modbus_values()
    test_edge_node_message_flow()
    test_node_commands()
    test_node_read_only()
    print("\nPASS: Sparkplug B payload codec + edge node commands")


if __name__ == "__main__":
    main()
