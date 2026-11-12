"""Sparkplug B payload codec for the Modbus edge mode (pure stdlib, no deps).

Eclipse Sparkplug B 3.0 puts protobuf (proto2) ``Payload`` messages on MQTT topics
``spBv1.0/<group>/<NBIRTH|NDATA|NCMD|...>/<edge node>[/<device>]``. ser2netweb only
needs the scalar part of sparkplug_b.proto -- metrics with a name/alias, timestamp,
datatype and one plain value -- so instead of shipping the protobuf runtime plus
generated code in the offline bundle, those few fields are hand-coded here on top
of the protobuf wire format:

  field key  = varint(field_number << 3 | wire_type)
  wire types   0 varint, 1 fixed 8 bytes LE, 2 varint length + bytes, 5 fixed 4 bytes LE

Signed integer metrics travel in the unsigned int_value (uint32) / long_value
(uint64) fields as two's complement of the field width (Int16 -1 -> 0xFFFFFFFF);
decoding sign-extends them again from the metric's datatype.

The decoder is defensive because NCMD/DCMD payloads come off the network: fields it
does not model (metadata, properties, datasets, templates, extensions, anything
newer) are skipped by wire type, and truncated or malformed input raises ValueError
-- never IndexError -- so the caller can log and drop a bad message. Everything here
is side-effect free.
"""
from __future__ import annotations

import enum
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

TOPIC_NAMESPACE = "spBv1.0"
MESSAGE_TYPES = ("NBIRTH", "NDEATH", "NDATA", "NCMD",
                 "DBIRTH", "DDEATH", "DDATA", "DCMD", "STATE")
BDSEQ = "bdSeq"                    # birth/death sequence metric of NBIRTH + NDEATH
REBIRTH = "Node Control/Rebirth"   # NCMD metric a host sets to true to ask for a new NBIRTH


class DataType(enum.IntEnum):
    """Sparkplug B metric datatypes (the sparkplug_b.proto DataType enum). Only the
    scalar types listed in _VALUE_FIELDS can be encoded; the rest are named so they
    can be recognised (and passed through raw) when decoding."""
    Unknown = 0
    Int8 = 1
    Int16 = 2
    Int32 = 3
    Int64 = 4
    UInt8 = 5
    UInt16 = 6
    UInt32 = 7
    UInt64 = 8
    Float = 9
    Double = 10
    Boolean = 11
    String = 12
    DateTime = 13
    Text = 14
    UUID = 15
    DataSet = 16
    Bytes = 17
    File = 18
    Template = 19
    PropertySet = 20
    PropertySetList = 21
    Int8Array = 22
    Int16Array = 23
    Int32Array = 24
    Int64Array = 25
    UInt8Array = 26
    UInt16Array = 27
    UInt32Array = 28
    UInt64Array = 29
    FloatArray = 30
    DoubleArray = 31
    BooleanArray = 32
    StringArray = 33
    DateTimeArray = 34


@dataclass
class Metric:
    """One Sparkplug metric. ``value`` is a plain Python value for ``datatype`` (int,
    float, bool, str or bytes) and is not sent when ``is_null`` is set. An empty
    ``name`` is left off the wire (DATA messages may address a metric by alias only)."""
    name: str = ""
    datatype: int = 0
    value: Any = None
    alias: int | None = None
    timestamp: int | None = None
    is_null: bool = False


@dataclass
class Payload:
    """A Sparkplug B payload: send time (ms since the epoch), metrics and the 0..255
    message sequence number (None for NDEATH, which carries none)."""
    timestamp: int | None
    metrics: list[Metric]
    seq: int | None
    uuid: str | None = None
    body: bytes | None = None


# ---------------------------------------------------------------------------
# protobuf wire format
# ---------------------------------------------------------------------------
_VARINT, _I64, _LEN, _SGROUP, _EGROUP, _I32 = 0, 1, 2, 3, 4, 5   # wire types
_U32_MAX = (1 << 32) - 1
_U64_MAX = (1 << 64) - 1
_MAX_FIELD = (1 << 29) - 1                                       # largest field number

# Metric "value" oneof members
_F_INT, _F_LONG, _F_FLOAT, _F_DOUBLE, _F_BOOL, _F_STRING, _F_BYTES = range(10, 17)


def _varint(value: int) -> bytes:
    """Encode an unsigned integer (0..2**64-1) as a protobuf base-128 varint."""
    if not 0 <= value <= _U64_MAX:
        raise ValueError(f"varint out of range: {value}")
    out = bytearray()
    while value > 0x7F:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode the varint at data[pos]; return (value, next position). Raises
    ValueError if it is truncated or longer than the 10 bytes of a 64-bit value."""
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value & _U64_MAX, pos   # like protobuf, bits beyond 64 are dropped
    raise ValueError("varint longer than 10 bytes")


def _key(number: int, wire_type: int) -> bytes:
    return _varint(number << 3 | wire_type)


def _field_varint(number: int, value: int) -> bytes:
    return _key(number, _VARINT) + _varint(value)


def _field_len(number: int, value: bytes) -> bytes:
    return _key(number, _LEN) + _varint(len(value)) + value


def _read_key(data: bytes, pos: int) -> tuple[int, int, int]:
    """Read a field key at data[pos]; return (field number, wire type, next position)."""
    key, pos = _read_varint(data, pos)
    number = key >> 3
    if not 1 <= number <= _MAX_FIELD:
        raise ValueError(f"invalid protobuf field number {number}")
    return number, key & 7, pos


def _value_span(data: bytes, pos: int, wire_type: int) -> tuple[int, int]:
    """(start, end) of a non-group field value that begins at data[pos]."""
    if wire_type == _VARINT:
        return pos, _read_varint(data, pos)[1]
    if wire_type == _I64:
        size = 8
    elif wire_type == _I32:
        size = 4
    elif wire_type == _LEN:
        size, pos = _read_varint(data, pos)
    else:  # a stray end-group, or 6/7 which are not protobuf wire types
        raise ValueError(f"unexpected protobuf wire type {wire_type}")
    if size > len(data) - pos:
        raise ValueError("truncated protobuf field")
    return pos, pos + size


def _skip_group(data: bytes, pos: int, number: int) -> int:
    """Skip a (deprecated proto2) group up to its matching end-group key. Iterative,
    so nested groups in hostile input can't exhaust the stack."""
    open_groups = [number]
    while open_groups:
        inner, wire_type, pos = _read_key(data, pos)
        if wire_type == _SGROUP:
            open_groups.append(inner)
        elif wire_type == _EGROUP:
            if open_groups.pop() != inner:
                raise ValueError("mismatched protobuf end-group")
        else:
            pos = _value_span(data, pos, wire_type)[1]
    return pos


def _fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """Walk one message: yield (field number, wire type, value) with an int value for
    varints and the raw bytes for fixed-width / length-delimited fields. Groups are
    skipped; malformed input raises ValueError."""
    pos = 0
    while pos < len(data):
        number, wire_type, pos = _read_key(data, pos)
        if wire_type == _SGROUP:
            pos = _skip_group(data, pos, number)
        elif wire_type == _VARINT:
            value, pos = _read_varint(data, pos)
            yield number, wire_type, value
        else:
            start, pos = _value_span(data, pos, wire_type)
            yield number, wire_type, data[start:pos]


def _uint(value: object, bits: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >> bits:
        raise ValueError(f"{what} must be an integer 0..{(1 << bits) - 1}, got {value!r}")
    return value


def _text(value: object, what: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a str, got {value!r}")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates
        raise ValueError(f"{what} is not encodable as UTF-8") from None


def _blob(value: object, what: str) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{what} must be bytes, got {value!r}")
    return bytes(value)


def _utf8(raw: bytes, what: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{what} is not valid UTF-8") from None


# ---------------------------------------------------------------------------
# metric values
# ---------------------------------------------------------------------------
# integer datatypes -> (signed, bit width)
_INT_TYPES: dict[int, tuple[bool, int]] = {
    DataType.Int8: (True, 8), DataType.Int16: (True, 16),
    DataType.Int32: (True, 32), DataType.Int64: (True, 64),
    DataType.UInt8: (False, 8), DataType.UInt16: (False, 16),
    DataType.UInt32: (False, 32), DataType.UInt64: (False, 64),
    DataType.DateTime: (False, 64),          # ms since the epoch
}

# supported datatype -> value fields accepted when decoding; the first one is encoded.
# Decoding also takes the "other" integer / float field, since encoders differ
# (e.g. UInt32 is sometimes sent in long_value).
_VALUE_FIELDS: dict[int, tuple[int, ...]] = {
    **{dt: (_F_INT, _F_LONG) if bits <= 32 else (_F_LONG, _F_INT)
       for dt, (_, bits) in _INT_TYPES.items()},
    DataType.Float: (_F_FLOAT, _F_DOUBLE),
    DataType.Double: (_F_DOUBLE, _F_FLOAT),
    DataType.Boolean: (_F_BOOL,),
    DataType.String: (_F_STRING,),
    DataType.Text: (_F_STRING,),
    DataType.Bytes: (_F_BYTES,),
}


def _type_name(datatype: int) -> str:
    try:
        return DataType(datatype).name
    except ValueError:  # also what DataType() raises for non-int junk
        return f"datatype {datatype!r}"


def _label(metric: Metric) -> str:
    if metric.name:
        return f"metric {metric.name!r}"
    return "metric" if metric.alias is None else f"metric alias {metric.alias}"


def _signed(raw: int, bits: int) -> int:
    """Read a ``bits``-wide two's complement pattern (0 <= raw < 2**bits) as signed."""
    return raw - (1 << bits) if raw >> (bits - 1) else raw


def _encode_value(datatype: int, value: Any, label: str) -> bytes:
    number = _VALUE_FIELDS[datatype][0]
    if datatype in _INT_TYPES:
        signed, bits = _INT_TYPES[datatype]
        lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{label}: {value!r} is not a valid {_type_name(datatype)} "
                             f"({lo}..{hi})")
        # negative values as two's complement of the whole 32- / 64-bit field
        return _field_varint(number, value & (_U32_MAX if number == _F_INT else _U64_MAX))
    if number in (_F_FLOAT, _F_DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label}: {_type_name(datatype)} value must be a number, "
                             f"got {value!r}")
        try:  # NaN / inf pass through; a finite value too big for the type does not
            if number == _F_FLOAT:
                return _key(number, _I32) + struct.pack("<f", float(value))
            return _key(number, _I64) + struct.pack("<d", float(value))
        except OverflowError:
            raise ValueError(f"{label}: {value!r} is out of range for "
                             f"{_type_name(datatype)}") from None
    if number == _F_BOOL:
        if not isinstance(value, int) or value not in (0, 1):  # never truthy text like "false"
            raise ValueError(f"{label}: Boolean value must be True/False, got {value!r}")
        return _field_varint(number, int(value))
    if number == _F_STRING:
        return _field_len(number, _text(value, f"{label} value"))
    return _field_len(number, _blob(value, f"{label} value"))


def _int_from_wire(raw: int, field_bits: int, metric: Metric) -> int:
    """Wire integer (unsigned, ``field_bits`` wide) -> Python int for the metric's
    integer datatype; ValueError if it doesn't fit that type."""
    signed, bits = _INT_TYPES[metric.datatype]
    if not signed:
        if raw >> bits:
            raise ValueError(f"{_label(metric)}: {raw} is out of range for "
                             f"{_type_name(metric.datatype)}")
        return raw
    value = _signed(raw, field_bits)          # sign-extended across the field (the norm)
    if -(1 << (bits - 1)) <= value < 1 << (bits - 1):
        return value
    if bits < field_bits and not raw >> bits:  # the narrow form some encoders emit,
        return _signed(raw, bits)              # e.g. Int16 -1 as 0xFFFF
    raise ValueError(f"{_label(metric)}: {raw:#x} is out of range for "
                     f"{_type_name(metric.datatype)}")


def _decode_value(metric: Metric, number: int, wire: Any) -> Any:
    fields = _VALUE_FIELDS.get(metric.datatype)
    if fields is None:  # no datatype (as NDATA/NCMD should be sent) or an unmodelled one
        return wire
    if number not in fields:
        raise ValueError(f"{_label(metric)}: {_type_name(metric.datatype)} value "
                         f"in the wrong field ({number})")
    if metric.datatype in _INT_TYPES:
        return _int_from_wire(wire, 32 if number == _F_INT else 64, metric)
    return wire


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------
def _encode_metric(metric: Metric, include_datatype: bool) -> bytes:
    label = _label(metric)
    out = bytearray()
    if metric.name:
        out += _field_len(1, _text(metric.name, f"{label} name"))
    if metric.alias is not None:
        out += _field_varint(2, _uint(metric.alias, 64, f"{label} alias"))
    if metric.timestamp is not None:
        out += _field_varint(3, _uint(metric.timestamp, 64, f"{label} timestamp"))
    datatype = metric.datatype
    if datatype and datatype not in _VALUE_FIELDS:
        raise ValueError(f"{label}: unsupported Sparkplug datatype {_type_name(datatype)}")
    if datatype and include_datatype:
        out += _field_varint(4, int(datatype))
    if metric.is_null:
        out += _field_varint(7, 1)
    elif not datatype:
        raise ValueError(f"{label}: a datatype is needed to encode a value")
    elif metric.value is None:
        raise ValueError(f"{label}: value is None (set is_null=True for a null metric)")
    else:
        out += _encode_value(datatype, metric.value, label)
    return bytes(out)


def encode_payload(payload: Payload, *, include_datatype: bool = True) -> bytes:
    """Serialize a Payload to Sparkplug B protobuf bytes (fields in field-number order,
    as protobuf encoders write them). Raises ValueError for anything the schema can't
    carry: integers out of range, a seq outside 0..255, an unsupported datatype, or a
    value that doesn't match its metric's datatype.

    ``include_datatype=False`` leaves the metric datatypes off the wire while still
    encoding each value by its datatype: Sparkplug 3.0 wants the datatype in NBIRTH /
    DBIRTH and says it SHOULD NOT be repeated in NDATA / DDATA / NCMD / DCMD."""
    out = bytearray()
    if payload.timestamp is not None:
        out += _field_varint(1, _uint(payload.timestamp, 64, "payload timestamp"))
    for metric in payload.metrics:
        out += _field_len(2, _encode_metric(metric, include_datatype))
    if payload.seq is not None:
        out += _field_varint(3, _uint(payload.seq, 8, "payload seq"))
    if payload.uuid is not None:
        out += _field_len(4, _text(payload.uuid, "payload uuid"))
    if payload.body is not None:
        out += _field_len(5, _blob(payload.body, "payload body"))
    return bytes(out)


def _decode_metric(data: bytes) -> Metric:
    metric = Metric()
    value: tuple[int, Any] | None = None   # (field number, wire value) of the value oneof
    for number, wire_type, raw in _fields(data):
        if isinstance(raw, int):           # varint fields
            if number == 2:
                metric.alias = raw
            elif number == 3:
                metric.timestamp = raw
            elif number == 4:
                metric.datatype = raw & _U32_MAX
            elif number == 7:
                metric.is_null = raw != 0
            elif number == _F_INT:
                value = (number, raw & _U32_MAX)   # uint32: protobuf keeps the low 32 bits
            elif number == _F_LONG:
                value = (number, raw)
            elif number == _F_BOOL:
                value = (number, raw != 0)
        elif wire_type == _LEN:
            if number == 1:
                metric.name = _utf8(raw, "metric name")
            elif number == _F_STRING:
                value = (number, _utf8(raw, "metric string value"))
            elif number == _F_BYTES:
                value = (number, raw)
        elif number == _F_FLOAT and wire_type == _I32:
            value = (number, struct.unpack("<f", raw)[0])
        elif number == _F_DOUBLE and wire_type == _I64:
            value = (number, struct.unpack("<d", raw)[0])
        # everything else -- is_historical/is_transient, metadata (8), properties (9),
        # dataset/template/extension values (17-19), unknown fields, a known field with
        # the wrong wire type -- is skipped, as protobuf does
    if value is not None and not metric.is_null:
        metric.value = _decode_value(metric, *value)
    return metric


def decode_payload(data: bytes, max_metrics: int | None = None) -> Payload:
    """Parse Sparkplug B protobuf bytes into a Payload. Values are converted by each
    metric's datatype (signed types sign-extended, range-checked); a metric without a
    datatype -- as NDATA/NCMD metrics are normally sent -- or with a datatype this codec
    doesn't model keeps its plain wire value (int_value as unsigned 32-bit, etc.).
    Unknown fields are skipped; truncated or malformed input raises ValueError, and so do
    more than ``max_metrics`` metrics, as soon as the one too many is seen (the rest is
    not decoded)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"decode_payload needs bytes, got {type(data).__name__}")
    payload = Payload(timestamp=None, metrics=[], seq=None)
    for number, wire_type, raw in _fields(bytes(data)):
        if isinstance(raw, int):           # varint fields
            if number == 1:
                payload.timestamp = raw
            elif number == 3:
                payload.seq = raw
        elif wire_type == _LEN:
            if number == 2:
                if max_metrics is not None and len(payload.metrics) >= max_metrics:
                    raise ValueError(f"more than {max_metrics} metric(s)")
                payload.metrics.append(_decode_metric(raw))
            elif number == 4:
                payload.uuid = _utf8(raw, "payload uuid")
            elif number == 5:
                payload.body = raw
    return payload


# ---------------------------------------------------------------------------
# topics, ids, sequence numbers
# ---------------------------------------------------------------------------
_ID_UNSAFE = re.compile(r"[/+#\s\x00-\x1f\x7f]")


def _check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "/+#"):
        raise ValueError(f"Sparkplug {what} must be a non-empty string without "
                         f"'/', '+' or '#', got {value!r}")
    return value


def topic(group_id: str, message_type: str, edge_node_id: str,
          device_id: str | None = None) -> str:
    """Build spBv1.0/<group_id>/<message_type>/<edge_node_id>[/<device_id>]. Device
    messages (DBIRTH/DDEATH/DDATA/DCMD) need a device_id, node messages must not have
    one. Raises ValueError for an unknown message type or an id that is empty or
    contains '/', '+' or '#'. STATE topics have a different shape (no group or node):
    use state_topic()."""
    if message_type == "STATE":
        raise ValueError("STATE topics are spBv1.0/STATE/<host_id>: use state_topic()")
    if message_type not in MESSAGE_TYPES:
        raise ValueError(f"unknown Sparkplug message type {message_type!r}")
    parts = [TOPIC_NAMESPACE, _check_id(group_id, "group_id"), message_type,
             _check_id(edge_node_id, "edge_node_id")]
    if message_type.startswith("D"):
        if device_id is None:
            raise ValueError(f"{message_type} needs a device_id")
        parts.append(_check_id(device_id, "device_id"))
    elif device_id is not None:
        raise ValueError(f"{message_type} is a node message and takes no device_id")
    return "/".join(parts)


def state_topic(host_id: str) -> str:
    """spBv1.0/STATE/<host_id>: where a Sparkplug 3.0 host application publishes its
    online/offline STATE (an edge node subscribes to it to follow its primary host)."""
    return f"{TOPIC_NAMESPACE}/STATE/{_check_id(host_id, 'host_id')}"


def sanitize_id(text: str) -> str:
    """Make a valid Sparkplug id from free text such as a hostname or mapping name:
    surrounding whitespace is stripped, '/', '+', '#', inner whitespace and control
    characters become '_', and an empty result falls back to "node"."""
    return _ID_UNSAFE.sub("_", (text or "").strip()) or "node"


def next_seq(seq: int) -> int:
    """The message sequence number after ``seq``: 0..255, wrapping back to 0."""
    return (seq + 1) % 256


_DTYPE_DATATYPES = {
    "uint16": DataType.UInt16, "int16": DataType.Int16,
    "uint32": DataType.UInt32, "int32": DataType.Int32,
    "float32": DataType.Float, "bool": DataType.Boolean,
    "uint64": DataType.UInt64, "int64": DataType.Int64,
    "float64": DataType.Double, "string": DataType.String,
}


def datatype_for(dtype: str, scale: float = 1.0) -> int:
    """Sparkplug datatype for a polled Modbus value of ``dtype`` (see
    modbus.decode_value). A scaled value (scale other than 0/1, the same rule as
    decode_value) is a float, so it maps to Double; "bool" (coils, discrete inputs, a
    register's bit) maps to Boolean and "string" to String whatever the scale."""
    try:
        datatype = _DTYPE_DATATYPES[dtype]
    except KeyError:
        raise ValueError(f"unknown dtype {dtype!r}") from None
    if datatype not in (DataType.Boolean, DataType.String) and scale and scale != 1.0:
        return DataType.Double
    return datatype
