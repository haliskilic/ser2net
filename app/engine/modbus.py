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

import struct

MBAP_LEN = 7  # txn(2) + proto(2) + length(2) + unit(1)
MAX_PDU = 253  # Modbus PDU max (256-byte ADU minus addr+CRC)

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
    Code 0x0B = gateway target device failed to respond (RTU timeout)."""
    return bytes([(function | 0x80) & 0xFF, code & 0xFF])


GATEWAY_TARGET_FAILED = 0x0B  # exception code for a non-responding RTU slave


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
