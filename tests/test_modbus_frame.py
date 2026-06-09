"""Modbus framing unit tests (RTU<->TCP gateway primitives).

Pure stdlib, cross-platform — exercises CRC, RTU wrap/unwrap, MBAP build/parse
and the exception PDU with known Modbus test vectors. Run:
    python3 tests/test_modbus_frame.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine import modbus


def test_crc_and_rtu_known_vector():
    # Read holding registers: slave 1, fn 3, start 0, count 10.
    pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x0A])
    adu = modbus.rtu_wrap(1, pdu)
    # canonical RTU frame ends with CRC C5 CD (little-endian)
    assert adu == bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A, 0xC5, 0xCD]), adu.hex(" ")
    print("CRC16 + rtu_wrap match the canonical 01 03 00 00 00 0A C5 CD vector  OK")


def test_rtu_round_trip_and_bad_crc():
    pdu = bytes([0x03, 0x02, 0x00, 0x7B])
    adu = modbus.rtu_wrap(17, pdu)
    unit, got = modbus.rtu_unwrap(adu)
    assert unit == 17 and got == pdu, (unit, got)
    # flip a byte -> CRC mismatch
    bad = bytearray(adu)
    bad[2] ^= 0xFF
    try:
        modbus.rtu_unwrap(bytes(bad))
        raise AssertionError("expected CRC mismatch")
    except ValueError:
        pass
    # too short
    try:
        modbus.rtu_unwrap(b"\x01\x02")
        raise AssertionError("expected short-frame error")
    except ValueError:
        pass
    print("rtu_unwrap round-trips and rejects bad CRC / short frames  OK")


def test_tcp_adu_build_and_take():
    pdu = bytes([0x03, 0x00, 0x6B, 0x00, 0x03])
    adu = modbus.build_tcp_adu(0x1234, 0x11, pdu)
    # MBAP: txn=1234 proto=0000 len=0006 unit=11 then PDU
    assert adu[:7] == bytes([0x12, 0x34, 0x00, 0x00, 0x00, 0x06, 0x11]), adu[:7].hex(" ")
    assert adu[7:] == pdu

    buf = bytearray(adu)
    out = modbus.take_tcp_adu(buf)
    assert out == (0x1234, 0x11, pdu), out
    assert len(buf) == 0, "consumed bytes not removed"
    print("build_tcp_adu / take_tcp_adu round-trip with correct MBAP  OK")


def test_take_tcp_adu_streaming_and_errors():
    pdu = bytes([0x01, 0x00, 0x00, 0x00, 0x05])
    adu = modbus.build_tcp_adu(7, 1, pdu)
    # partial header -> None (wait for more)
    assert modbus.take_tcp_adu(bytearray(adu[:5])) is None
    # full header but partial body -> None
    assert modbus.take_tcp_adu(bytearray(adu[:-1])) is None
    # two frames back-to-back -> two takes
    buf = bytearray(adu + adu)
    assert modbus.take_tcp_adu(buf) == (7, 1, pdu)
    assert modbus.take_tcp_adu(buf) == (7, 1, pdu)
    assert modbus.take_tcp_adu(buf) is None
    # non-zero protocol id -> framing error
    bad = bytearray(adu)
    bad[2] = 0x01
    try:
        modbus.take_tcp_adu(bad)
        raise AssertionError("expected protocol-id error")
    except ValueError:
        pass
    print("take_tcp_adu handles streaming, back-to-back frames and bad protocol id  OK")


def test_exception_pdu():
    assert modbus.exception_pdu(0x03, modbus.GATEWAY_TARGET_FAILED) == bytes([0x83, 0x0B])
    print("exception_pdu sets the high bit and the 0x0B gateway-timeout code  OK")


def test_gateway_conversion_chain():
    # what the gateway actually does: TCP request -> RTU -> (slave) -> RTU resp -> TCP
    req_pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
    tcp_req = modbus.build_tcp_adu(0xABCD, 5, req_pdu)
    txn, unit, pdu = modbus.take_tcp_adu(bytearray(tcp_req))
    rtu_req = modbus.rtu_wrap(unit, pdu)
    assert modbus.rtu_unwrap(rtu_req) == (5, req_pdu)
    # slave answers: fn 3, byte count 4, two registers
    resp_pdu = bytes([0x03, 0x04, 0x00, 0x0A, 0x00, 0x0B])
    rtu_resp = modbus.rtu_wrap(5, resp_pdu)
    runit, rpdu = modbus.rtu_unwrap(rtu_resp)
    tcp_resp = modbus.build_tcp_adu(txn, runit, rpdu)
    assert tcp_resp[:2] == bytes([0xAB, 0xCD]), "transaction id must be echoed back"
    assert modbus.take_tcp_adu(bytearray(tcp_resp)) == (0xABCD, 5, resp_pdu)
    print("full TCP->RTU->TCP conversion preserves txn id, unit and PDU  OK")


def test_register_read_and_decode():
    import struct
    # read request PDU: read 2 holding registers at 0x006B
    assert modbus.read_pdu(3, 0x006B, 2) == bytes([3, 0x00, 0x6B, 0x00, 0x02])
    # response_data extracts the data bytes; an exception PDU raises
    resp = bytes([3, 4, 0x41, 0xBC, 0x00, 0x00])  # fn3, byte_count 4
    assert modbus.response_data(resp, 3) == bytes([0x41, 0xBC, 0x00, 0x00])
    for bad in (bytes([0x83, 0x0B]), bytes([3, 4, 0x00])):  # exception PDU / short data
        try:
            modbus.response_data(bad, 3)
            raise AssertionError("should raise")
        except ValueError:
            pass
    # decode every dtype (big-endian), incl. signed + 32-bit + float + scale
    assert modbus.decode_value(bytes([0x04, 0xD2]), "uint16") == 1234
    assert modbus.decode_value(bytes([0xFF, 0xFF]), "int16") == -1
    assert modbus.decode_value(bytes([0x00, 0x01, 0x00, 0x00]), "uint32") == 65536
    assert modbus.decode_value(bytes([0xFF, 0xFF, 0xFF, 0xFF]), "int32") == -1
    assert abs(modbus.decode_value(struct.pack(">f", 23.5), "float32") - 23.5) < 1e-6
    assert modbus.decode_value(bytes([0x00, 0x64]), "uint16", scale=0.1) == 10.0
    print("read_pdu / response_data / decode_value (dtypes + scale + exception)  OK")


def main():
    test_register_read_and_decode()
    test_crc_and_rtu_known_vector()
    test_rtu_round_trip_and_bad_crc()
    test_tcp_adu_build_and_take()
    test_take_tcp_adu_streaming_and_errors()
    test_exception_pdu()
    test_gateway_conversion_chain()
    print("\nPASS: Modbus framing (RTU<->TCP)")


if __name__ == "__main__":
    main()
