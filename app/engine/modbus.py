"""Modbus framing helpers for the RTU<->TCP gateway (pure stdlib, no deps).

A Modbus *gateway* lets Modbus/TCP masters on the network talk to Modbus/RTU
slaves on a serial bus. The two encodings share the same PDU (function code +
data); they differ only in the envelope:

  RTU ADU:  [unit][PDU][CRC16-lo][CRC16-hi]
  TCP ADU:  [txn-hi][txn-lo][0][0][len-hi][len-lo][unit][PDU]   (MBAP header)

So gatewaying is: strip one envelope, apply the other, preserving unit id and
(for TCP) the transaction id. These helpers are deliberately side-effect free so
they can be unit-tested without any serial hardware.
"""
from __future__ import annotations

import math
import struct
from typing import NamedTuple, Optional

MBAP_LEN = 7  # txn(2) + proto(2) + length(2) + unit(1)
MAX_PDU = 253  # Modbus PDU max (256-byte ADU minus addr+CRC)
MAX_ADU = MAX_PDU + 3  # an RTU frame at most: unit + PDU + CRC

READ_HOLDING = 3   # read holding registers
READ_INPUT = 4     # read input registers


def crc16(data: bytes) -> int:
    """Modbus RTU CRC-16 (polynomial 0xA001, init 0xFFFF), returned as an int."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def rtu_wrap(unit: int, pdu: bytes) -> bytes:
    """Build an RTU ADU: unit + PDU + CRC16 (little-endian, as Modbus requires)."""
    body = bytes([unit & 0xFF]) + pdu
    crc = crc16(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def rtu_unwrap(frame: bytes) -> tuple[int, bytes]:
    """Validate an RTU ADU and return (unit, PDU). Raises ValueError on a short
    frame or a CRC mismatch."""
    if len(frame) < 4:  # unit + at least 1 PDU byte + 2 CRC
        raise ValueError("RTU frame too short")
    body, crc_lo, crc_hi = frame[:-2], frame[-2], frame[-1]
    if crc16(body) != (crc_lo | (crc_hi << 8)):
        raise ValueError("RTU CRC mismatch")
    return body[0], body[1:]


# The length of a slave's reply (Modbus Application Protocol v1.1b3), counted as an RTU
# frame (unit + PDU + CRC): fixed for these function codes ...
_FIXED_REPLY = {5: 8, 6: 8, 7: 5, 11: 8, 15: 8, 16: 8, 22: 10}
# ... and 5 + the byte count that follows the function code for these (FC1-4 reads,
# 12 comm event log, 17 report server id, 20/21 file records, 23 read/write registers)
_COUNTED_REPLY = frozenset({1, 2, 3, 4, 12, 17, 20, 21, 23})
_READ_FIFO_QUEUE = 24  # 6 + a two-byte byte count


def rtu_reply_length(head: bytes | bytearray) -> Optional[int]:
    """The length of the RTU reply that starts with ``head`` (its unit id first), from the
    function code and byte count at its start, as libmodbus frames replies: the whole
    frame's length once ``head`` holds them, else the least it can be (read that many
    bytes, then ask again). None when the function code does not tell (FC8 diagnostics,
    FC43, user-defined and unknown codes): such a reply ends where the line goes quiet."""
    if len(head) < 2:
        return 4  # unit + function + CRC: no frame is shorter
    function = head[1]
    if function & 0x80:  # an exception: unit, function | 0x80, exception code, CRC
        return 5
    if function in _FIXED_REPLY:
        return _FIXED_REPLY[function]
    if function in _COUNTED_REPLY:
        return 5 + head[2] if len(head) > 2 else 5
    if function == _READ_FIFO_QUEUE:
        return 6 + int.from_bytes(head[2:4], "big") if len(head) > 3 else 6
    return None


# write requests whose reply is their echo: the whole request, or its first 5 bytes
# (function, start address, quantity)
_ECHO_REPLY = frozenset({5, 6, 22})
_ECHO_HEAD_REPLY = frozenset({15, 16})


def reply_mismatch(request: bytes, reply: bytes) -> str:
    """Why the PDU ``reply`` is not the answer to the PDU ``request``, or "" when it can be.
    It has to be an exception for the request's function, or carry that function and: for
    a read of bits or registers (FC1-4, FC23) the byte count the quantity read gives; for
    a write, its echo (FC5, FC6, FC22: the whole request; FC15, FC16: its first 5 bytes).
    Other functions are matched by function code only.

    A read's reply carries no address, so the reply to an earlier read of the same unit,
    function and quantity passes: only waiting for the line to go quiet after a request
    that got no reply in time keeps such a late reply apart (the gateway does)."""
    function = request[0] if request else 0
    if not reply:
        return "an empty reply"
    if reply[0] == (function | 0x80) & 0xFF:
        return ""
    if reply[0] != function:
        return f"an FC{reply[0]} reply to FC{function}"
    if function in (1, 2, 3, 4, 23) and len(request) >= 5:
        quantity = int.from_bytes(request[3:5], "big")
        want = (quantity + 7) // 8 if function in (1, 2) else 2 * quantity
        got = reply[1] if len(reply) > 1 else 0
        if got != want:
            unit = "bit" if function in (1, 2) else "register"
            return f"{got} data bytes for {quantity} {unit}(s) of FC{function}"
    elif function in _ECHO_REPLY or function in _ECHO_HEAD_REPLY:
        echo = bytes(request) if function in _ECHO_REPLY else bytes(request[:5])
        if bytes(reply) != echo:
            return f"an FC{function} reply that is not the request's echo ({bytes(reply).hex(' ')})"
    return ""


class ReplyScan(NamedTuple):
    span: Optional[tuple[int, int]]  # buf[start:end] is the reply (None: not found)
    pending: bool  # a frame from the unit has begun and may still be completed
    started: bool  # a frame from the unit began in buf at all
    rejected: str = ""  # why the last whole frame from the unit was not the reply


def find_reply(buf: bytes | bytearray, unit: int, request: bytes, *,
               quiet: bool = False) -> ReplyScan:
    """Look for the reply to ``request`` (a PDU sent to ``unit``) in ``buf``, the bytes read
    off the line since it was sent. They may begin with bytes no request is waiting for
    (the late tail of an earlier reply, noise), which are skipped.

    A reply starts with ``unit`` and the request's function code (or its exception code),
    is as long as its header says (rtu_reply_length), passes its CRC and answers the
    request (reply_mismatch); a whole frame that answers something else is skipped too.
    ``quiet``: the line has gone quiet since buf's last byte, so a frame whose length its
    header does not give, or that fails its CRC at that length (a slave that miscounts),
    may end with buf: it is then tried as a whole too."""
    function = request[0] if request else 0
    starts = (function, (function | 0x80) & 0xFF)
    pending = started = False
    rejected = ""
    for start in range(len(buf) - 1):
        if buf[start] != unit or buf[start + 1] not in starts:
            continue
        started = True
        length = rtu_reply_length(buf[start:start + 4])
        ends = []
        if length is None:
            pending = True  # it ends where the line goes quiet
        elif length <= MAX_ADU:
            if start + length <= len(buf):
                ends.append(start + length)
            else:
                pending = True
        if quiet and len(buf) - start <= MAX_ADU and len(buf) not in ends:
            ends.append(len(buf))
        for end in ends:
            try:
                _unit, pdu = rtu_unwrap(bytes(buf[start:end]))
            except ValueError:
                continue
            why = reply_mismatch(request, pdu)
            if not why:
                return ReplyScan((start, end), pending, started, rejected)
            rejected = why
    return ReplyScan(None, pending, started, rejected)


def build_tcp_adu(txn: int, unit: int, pdu: bytes) -> bytes:
    """Build a Modbus/TCP ADU (MBAP header + PDU)."""
    length = len(pdu) + 1  # unit id + PDU
    return (bytes([(txn >> 8) & 0xFF, txn & 0xFF, 0, 0,
                   (length >> 8) & 0xFF, length & 0xFF, unit & 0xFF]) + pdu)


def take_tcp_adu(buf: bytearray) -> tuple[int, int, bytes] | None:
    """Pop one complete Modbus/TCP ADU from a streaming buffer.

    Returns (txn, unit, pdu) and removes the consumed bytes from ``buf``; returns
    None if a full frame isn't buffered yet. Raises ValueError on a framing error
    (non-zero protocol id or an absurd length) so the caller can drop the peer.
    """
    if len(buf) < MBAP_LEN:
        return None
    txn = (buf[0] << 8) | buf[1]
    proto = (buf[2] << 8) | buf[3]
    length = (buf[4] << 8) | buf[5]
    unit = buf[6]
    if proto != 0:
        raise ValueError("not Modbus/TCP (protocol id != 0)")
    if not (1 <= length <= MAX_PDU + 1):
        raise ValueError(f"invalid MBAP length {length}")
    total = MBAP_LEN + (length - 1)  # length counts unit id, already in the header
    if len(buf) < total:
        return None
    pdu = bytes(buf[MBAP_LEN:total])
    del buf[:total]
    return txn, unit, pdu


def exception_pdu(function: int, code: int) -> bytes:
    """A Modbus exception PDU: function|0x80 followed by the exception code.
    Code 0x0B = gateway target device failed to respond (RTU timeout), 0x0A = gateway
    path unavailable (the serial port is down)."""
    return bytes([(function | 0x80) & 0xFF, code & 0xFF])


ILLEGAL_FUNCTION = 0x01       # exception code: the function is not allowed here
GATEWAY_PATH_UNAVAILABLE = 0x0A  # exception code: no path to the slaves (the port is down)
GATEWAY_TARGET_FAILED = 0x0B  # exception code for a non-responding RTU slave

# Requests that only read (Modbus Application Protocol v1.1b3): what a read-only gateway
# still forwards. FC1-4 read bits/registers; 7 read exception status, 11/12 comm event
# counter/log, 17 report server id, 20 read file record, 24 read FIFO queue.
_READ_FUNCTIONS = frozenset({1, 2, 3, 4, 7, 11, 12, 17, 20, 24})
DIAGNOSTICS = 8               # FC8: some sub-functions restart a slave or silence it
# ... so only the ones that return data: query data echo, diagnostic register, counters
_READ_DIAGNOSTICS = frozenset({0x00, 0x02, *range(0x0B, 0x13)})
ENCAPSULATED_INTERFACE = 43   # FC43: only MEI type 14 (read device identification) reads
READ_DEVICE_ID = 0x0E


def is_read_request(pdu: bytes) -> bool:
    """True if a request PDU only reads. False for writes (FC5/6/15/16, 21-23), state-changing
    diagnostics (restart, listen-only, clear counters), other FC43 types, the user-defined
    and every unknown function code, and an empty PDU: anything not known to be a read."""
    if not pdu:
        return False
    function = pdu[0]
    if function in _READ_FUNCTIONS:
        return True
    if function == DIAGNOSTICS:
        return len(pdu) >= 3 and int.from_bytes(pdu[1:3], "big") in _READ_DIAGNOSTICS
    if function == ENCAPSULATED_INTERFACE:
        return len(pdu) >= 2 and pdu[1] == READ_DEVICE_ID
    return False


# ---------------------------------------------------------------------------
# Register polling helpers (Modbus master: read registers, decode values)
# ---------------------------------------------------------------------------
def read_pdu(function: int, address: int, count: int) -> bytes:
    """Build a read-registers request PDU (function 3 or 4)."""
    return bytes([function & 0xFF, (address >> 8) & 0xFF, address & 0xFF,
                  (count >> 8) & 0xFF, count & 0xFF])


def response_data(pdu: bytes, function: int) -> bytes:
    """Return the data bytes of a read-response PDU. Raises ValueError on a Modbus
    exception PDU (function|0x80) or a malformed/short frame."""
    if not pdu:
        raise ValueError("empty PDU")
    if pdu[0] == ((function | 0x80) & 0xFF):
        code = pdu[1] if len(pdu) > 1 else 0
        raise ValueError(f"modbus exception 0x{code:02x}")
    if pdu[0] != function or len(pdu) < 2:
        raise ValueError("unexpected response function")
    byte_count = pdu[1]
    data = pdu[2:2 + byte_count]
    if len(data) != byte_count:
        raise ValueError("short response data")
    return data


_DTYPE_REGS = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2}
DTYPES = tuple(_DTYPE_REGS)


def dtype_registers(dtype: str) -> int:
    if dtype not in _DTYPE_REGS:
        raise ValueError(f"unknown dtype {dtype!r}")
    return _DTYPE_REGS[dtype]


def decode_value(data: bytes, dtype: str, scale: float = 1.0):
    """Decode the first `dtype` value from big-endian register bytes, multiplied by
    `scale`. Integers stay integers when scale is 1; floats/scaled values are floats."""
    need = dtype_registers(dtype) * 2
    chunk = data[:need]
    if len(chunk) < need:
        raise ValueError("not enough data for dtype")
    fmt = {"uint16": ">H", "int16": ">h", "uint32": ">I", "int32": ">i", "float32": ">f"}[dtype]
    value = struct.unpack(fmt, chunk)[0]
    if scale and scale != 1.0:
        return value * scale
    return value


# ---------------------------------------------------------------------------
# Bit reads and writes (Modbus master: FC1/2 read bits, FC5/6/15/16 write)
# ---------------------------------------------------------------------------
READ_COILS = 1                 # read coils (read/write bits)
READ_DISCRETE_INPUTS = 2       # read discrete inputs (read-only bits)
WRITE_SINGLE_COIL = 5
WRITE_SINGLE_REGISTER = 6
WRITE_MULTIPLE_COILS = 15
WRITE_MULTIPLE_REGISTERS = 16

# quantity limits from the Modbus spec: they keep every PDU within MAX_PDU
MAX_READ_BITS = 2000           # FC1/FC2
MAX_WRITE_COILS = 1968         # FC15
MAX_WRITE_REGISTERS = 123      # FC16

_WRITE_FUNCTIONS = (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER,
                    WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS)


def _check_int(value: object, lo: int, hi: int, what: str) -> int:
    """Return ``value`` if it is an int in lo..hi, else raise ValueError. bool is
    rejected too: True as an address or register value is a caller bug, not 1."""
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{what} must be an integer {lo}..{hi}, got {value!r}")
    return value


def _check_span(address: int, count: int) -> None:
    """Validate a start address and that address..address+count-1 stays inside the
    16-bit address space (the PDU can't express anything past 65535)."""
    _check_int(address, 0, 0xFFFF, "address")
    if address + count > 0x10000:
        raise ValueError(f"address {address} + quantity {count} runs past address 65535")


def _coil_state(value: object, what: str) -> bool:
    """A coil state must be a bool (or 0/1): a stray truthy value such as the string
    "0" must never switch an output ON."""
    if not isinstance(value, int) or value not in (0, 1):
        raise ValueError(f"{what} must be True/False (or 1/0), got {value!r}")
    return bool(value)


def read_bits_pdu(function: int, address: int, count: int) -> bytes:
    """Build a read-coils (FC1) or read-discrete-inputs (FC2) request PDU; same layout
    as read_pdu, with a quantity of 1..2000 bits. Raises ValueError on a bad argument."""
    if function not in (READ_COILS, READ_DISCRETE_INPUTS):
        raise ValueError(f"read_bits_pdu needs function {READ_COILS} or "
                         f"{READ_DISCRETE_INPUTS}, got {function!r}")
    _check_int(count, 1, MAX_READ_BITS, "bit count")
    _check_span(address, count)
    return read_pdu(function, address, count)


def decode_bits(data: bytes, count: int) -> list[bool]:
    """Unpack ``count`` bit states from FC1/FC2 response data (see response_data):
    bit 0 (the LSB) of the first byte is the first, lowest-addressed bit. Raises
    ValueError if ``data`` holds fewer than ceil(count / 8) bytes."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"bit count must be a non-negative integer, got {count!r}")
    need = (count + 7) // 8
    if len(data) < need:
        raise ValueError(f"not enough data for {count} bits ({len(data)} of {need} bytes)")
    return [bool((data[i // 8] >> (i % 8)) & 1) for i in range(count)]


def write_single_coil_pdu(address: int, on: bool) -> bytes:
    """Build a write-single-coil (FC5) request PDU: ON is sent as 0xFF00, OFF as 0x0000."""
    _check_int(address, 0, 0xFFFF, "address")
    value = 0xFF00 if _coil_state(on, "coil state") else 0x0000
    return bytes([WRITE_SINGLE_COIL, address >> 8, address & 0xFF, value >> 8, value & 0xFF])


def write_single_register_pdu(address: int, raw: int) -> bytes:
    """Build a write-single-register (FC6) request PDU for a raw 16-bit value 0..65535
    (use encode_value for signed/scaled values)."""
    _check_int(address, 0, 0xFFFF, "address")
    _check_int(raw, 0, 0xFFFF, "register value")
    return bytes([WRITE_SINGLE_REGISTER, address >> 8, address & 0xFF, raw >> 8, raw & 0xFF])


def write_multiple_coils_pdu(address: int, values: list[bool]) -> bytes:
    """Build a write-multiple-coils (FC15) request PDU for 1..1968 coils starting at
    ``address``: quantity, byte count, then the states packed LSB-first."""
    states = [_coil_state(v, f"coil state [{i}]") for i, v in enumerate(values)]
    count = _check_int(len(states), 1, MAX_WRITE_COILS, "coil count")
    _check_span(address, count)
    packed = bytearray((count + 7) // 8)
    for i, on in enumerate(states):
        if on:
            packed[i // 8] |= 1 << (i % 8)
    return bytes([WRITE_MULTIPLE_COILS, address >> 8, address & 0xFF,
                  count >> 8, count & 0xFF, len(packed)]) + bytes(packed)


def write_multiple_registers_pdu(address: int, registers: list[int]) -> bytes:
    """Build a write-multiple-registers (FC16) request PDU for 1..123 raw 16-bit
    register values starting at ``address``."""
    regs = [_check_int(r, 0, 0xFFFF, f"register value [{i}]") for i, r in enumerate(registers)]
    count = _check_int(len(regs), 1, MAX_WRITE_REGISTERS, "register count")
    _check_span(address, count)
    data = b"".join(r.to_bytes(2, "big") for r in regs)
    return bytes([WRITE_MULTIPLE_REGISTERS, address >> 8, address & 0xFF,
                  count >> 8, count & 0xFF, len(data)]) + data


BROADCAST = 0  # unit 0: every slave on the line carries out a write, and none answers


def broadcast_reply(pdu: bytes) -> Optional[bytes]:
    """What a gateway answers a master for a broadcast (unit 0), which no slave answers:
    for a well-formed write, the reply each slave would give it as a unicast, its echo
    (FC5, FC6, FC22: the whole request; FC15, FC16: its first 5 bytes, as
    check_write_response expects). None for anything else."""
    function = pdu[0] if pdu else 0
    if function in (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER) and len(pdu) == 5:
        return bytes(pdu)
    if function == 22 and len(pdu) == 7:  # mask write register
        return bytes(pdu)
    if (function in (WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS) and len(pdu) >= 6
            and len(pdu) == 6 + pdu[5]):
        return bytes(pdu[:5])
    return None


def check_write_response(pdu: bytes, request: bytes) -> None:
    """Check a slave's reply to a write ``request`` PDU; returns None if the write was
    acknowledged. FC5/FC6 replies echo the request exactly, FC15/FC16 replies echo its
    function + start address + quantity (the first 5 bytes). Raises ValueError on a
    Modbus exception PDU (function|0x80), a short reply or any mismatch."""
    if len(request) < 5 or request[0] not in _WRITE_FUNCTIONS:
        raise ValueError("request is not a Modbus write PDU (FC5/6/15/16)")
    function = request[0]
    if not pdu:
        raise ValueError("empty PDU")
    if pdu[0] == function | 0x80:
        code = pdu[1] if len(pdu) > 1 else 0
        raise ValueError(f"modbus exception 0x{code:02x}")
    if pdu[0] != function:
        raise ValueError(f"unexpected response function 0x{pdu[0]:02x}")
    single = function in (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER)
    expected = bytes(request) if single else bytes(request[:5])
    if len(pdu) < len(expected):
        raise ValueError(f"short write response ({len(pdu)} of {len(expected)} bytes)")
    if bytes(pdu) != expected:
        raise ValueError(f"write response {bytes(pdu).hex(' ')} does not match "
                         f"the request ({expected.hex(' ')})")


_DTYPE_FMT = {"uint16": ">H", "int16": ">h", "uint32": ">I", "int32": ">i", "float32": ">f"}
_INT_RANGE = {"uint16": (0, 0xFFFF), "int16": (-0x8000, 0x7FFF),
              "uint32": (0, 0xFFFFFFFF), "int32": (-0x80000000, 0x7FFFFFFF)}


def encode_value(value: float, dtype: str, scale: float = 1.0) -> list[int]:
    """Inverse of decode_value: the register words that hold ``value`` as ``dtype``,
    big-endian with the high word first (the order decode_value reads).

    The raw value is value / scale (a scale of 0 or 1 means unscaled, exactly as in
    decode_value). float32 stores it as is; integer dtypes round it to the nearest
    integer (ties to even, like round()). Raises ValueError for a non-number, NaN or
    infinity, or a raw value outside the dtype's range."""
    dtype_registers(dtype)  # ValueError for an unknown dtype
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"value must be a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"value must be a finite number, got {value!r}")
    raw: int | float = value
    if scale and scale != 1.0:
        if not math.isfinite(scale):
            raise ValueError(f"scale must be a finite number, got {scale!r}")
        try:
            raw = value / scale
        except OverflowError:  # an int too large to become a float
            raise ValueError(f"value {value!r} out of range for {dtype}") from None
        if not math.isfinite(raw):
            raise ValueError(f"value {value!r} / scale {scale!r} out of range for {dtype}")
    if dtype == "float32":
        try:  # float() first: a huge int then fails with OverflowError, not struct.error
            packed = struct.pack(">f", float(raw))
        except OverflowError:
            raise ValueError(f"value {value!r} out of range for float32") from None
    else:
        lo, hi = _INT_RANGE[dtype]
        number = raw if isinstance(raw, int) else round(raw)
        if not lo <= number <= hi:
            raise ValueError(f"value {value!r} (raw {number}) out of range for {dtype} "
                             f"({lo}..{hi})")
        packed = struct.pack(_DTYPE_FMT[dtype], number)
    return [int.from_bytes(packed[i:i + 2], "big") for i in range(0, len(packed), 2)]
