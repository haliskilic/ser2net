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

MBAP_LEN = 7  # txn(2) + proto(2) + length(2) + unit(1)
MAX_PDU = 253  # Modbus PDU max (256-byte ADU minus addr+CRC)


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
