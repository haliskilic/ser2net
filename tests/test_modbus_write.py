"""Modbus write / bit-access unit tests (FC1/2 bit reads, FC5/6/15/16 writes).

Pure stdlib, cross-platform -- request PDUs are checked byte-for-byte against the
examples in the Modbus Application Protocol spec v1.1b3 (section 6), plus the
write-echo check, range errors and encode_value/decode_value round trips. Run:
    python3 tests/test_modbus_write.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine import modbus


def expect_value_error(fn, *args, match=""):
    """Call fn(*args) and require a ValueError (whose message contains `match`)."""
    try:
        fn(*args)
    except ValueError as e:
        assert match in str(e), (fn.__name__, args, str(e))
        return str(e)
    raise AssertionError(f"expected ValueError from {fn.__name__}{args!r}")


def regs_to_bytes(regs):
    return b"".join(r.to_bytes(2, "big") for r in regs)


def test_function_codes():
    assert (modbus.READ_COILS, modbus.READ_DISCRETE_INPUTS) == (1, 2)
    assert (modbus.WRITE_SINGLE_COIL, modbus.WRITE_SINGLE_REGISTER) == (5, 6)
    assert (modbus.WRITE_MULTIPLE_COILS, modbus.WRITE_MULTIPLE_REGISTERS) == (15, 16)
    print("function code constants FC1/2/5/6/15/16  OK")


def test_read_bits_spec_examples():
    # spec 6.1: read coils 20..38 -> request 01 00 13 00 13, response 01 03 CD 6B 05
    assert modbus.read_bits_pdu(modbus.READ_COILS, 19, 19) == bytes.fromhex("0100130013")
    data = modbus.response_data(bytes.fromhex("0103CD6B05"), modbus.READ_COILS)
    assert data == bytes.fromhex("CD6B05"), data.hex(" ")
    bits = modbus.decode_bits(data, 19)
    assert bits == [bool(b) for b in (1, 0, 1, 1, 0, 0, 1, 1,    # CD: coils 20..27, LSB first
                                      1, 1, 0, 1, 0, 1, 1, 0,    # 6B: coils 28..35
                                      1, 0, 1)], bits            # 05: coils 36..38
    # spec 6.2: read discrete inputs 197..218 -> 02 00 C4 00 16, response 02 03 AC DB 35
    assert modbus.read_bits_pdu(modbus.READ_DISCRETE_INPUTS, 196, 22) == bytes.fromhex("0200C40016")
    data = modbus.response_data(bytes.fromhex("0203ACDB35"), modbus.READ_DISCRETE_INPUTS)
    assert modbus.decode_bits(data, 22) == [bool(b) for b in (0, 0, 1, 1, 0, 1, 0, 1,
                                                              1, 1, 0, 1, 1, 0, 1, 1,
                                                              1, 0, 1, 0, 1, 1)]
    print("read_bits_pdu + response_data + decode_bits match the spec FC1/FC2 examples  OK")


def test_response_data_for_bit_reads():
    # the existing FC3/4 helper also serves FC1/2: byte count + packed bits
    assert modbus.response_data(bytes([1, 1, 0x05]), 1) == b"\x05"
    assert modbus.response_data(bytes([2, 2, 0xFF, 0x01]), 2) == b"\xff\x01"
    assert modbus.response_data(bytes([1, 0]), 1) == b""
    expect_value_error(modbus.response_data, bytes([0x81, 0x02]), 1, match="modbus exception 0x02")
    expect_value_error(modbus.response_data, bytes([0x82, 0x0B]), 2, match="modbus exception 0x0b")
    for bad in (bytes([1, 3, 0xCD]),     # byte count 3, only 1 data byte
                bytes([2, 1, 0xFF]),     # FC2 data for an FC1 read
                bytes([1]),              # no byte count
                b""):
        expect_value_error(modbus.response_data, bad, 1)
    # FC3/FC4 behaviour unchanged
    assert modbus.response_data(bytes([3, 2, 0x12, 0x34]), 3) == b"\x12\x34"
    print("response_data handles FC1/FC2 replies, exceptions and short frames  OK")


def test_decode_bits_edges():
    assert modbus.decode_bits(b"", 0) == []
    assert modbus.decode_bits(b"\x80", 8) == [False] * 7 + [True]
    assert modbus.decode_bits(b"\xff\x01", 9) == [True] * 9
    assert modbus.decode_bits(b"\x01\xff\xff", 1) == [True]          # extra bytes ignored
    assert modbus.decode_bits(b"\xfe", 3) == [False, True, True]     # padding bits ignored
    expect_value_error(modbus.decode_bits, b"\xff", 9, match="not enough data")
    expect_value_error(modbus.decode_bits, b"", 1)
    expect_value_error(modbus.decode_bits, b"\xff", -1)
    print("decode_bits: LSB-first order, byte boundaries, short data -> ValueError  OK")


def test_read_bits_pdu_range_checks():
    assert modbus.read_bits_pdu(1, 0, modbus.MAX_READ_BITS) == bytes([1, 0, 0, 0x07, 0xD0])
    assert modbus.read_bits_pdu(2, 0xFFFF, 1) == bytes([2, 0xFF, 0xFF, 0, 1])
    expect_value_error(modbus.read_bits_pdu, 3, 0, 1, match="function")
    expect_value_error(modbus.read_bits_pdu, 5, 0, 1, match="function")
    expect_value_error(modbus.read_bits_pdu, 1, 0, 0, match="bit count")
    expect_value_error(modbus.read_bits_pdu, 1, 0, 2001, match="bit count")
    expect_value_error(modbus.read_bits_pdu, 1, -1, 1, match="address")
    expect_value_error(modbus.read_bits_pdu, 1, 65536, 1, match="address")
    expect_value_error(modbus.read_bits_pdu, 1, 0xFFFF, 2, match="past address 65535")
    expect_value_error(modbus.read_bits_pdu, 1, 1.0, 1, match="address")
    expect_value_error(modbus.read_bits_pdu, 1, 0, True, match="bit count")
    print("read_bits_pdu range checks (function, 1..2000 bits, address span)  OK")


def test_write_single_coil():
    # spec 6.5: write coil 173 ON -> 05 00 AC FF 00
    assert modbus.write_single_coil_pdu(172, True) == bytes.fromhex("0500ACFF00")
    assert modbus.write_single_coil_pdu(172, False) == bytes.fromhex("0500AC0000")
    assert modbus.write_single_coil_pdu(0xFFFF, 1) == bytes.fromhex("05FFFFFF00")  # 1/0 accepted
    assert modbus.write_single_coil_pdu(0, 0) == bytes.fromhex("0500000000")
    for bad_state in (2, -1, "0", "true", None, 1.0):
        expect_value_error(modbus.write_single_coil_pdu, 1, bad_state, match="coil state")
    expect_value_error(modbus.write_single_coil_pdu, 65536, True, match="address")
    expect_value_error(modbus.write_single_coil_pdu, -1, True, match="address")
    print("write_single_coil_pdu: FF00/0000 values, strict bool state, address range  OK")


def test_write_single_register():
    # spec 6.6: write register 2 to 0x0003 -> 06 00 01 00 03
    assert modbus.write_single_register_pdu(1, 3) == bytes.fromhex("0600010003")
    assert modbus.write_single_register_pdu(0xFFFF, 0xFFFF) == bytes.fromhex("06FFFFFFFF")
    assert modbus.write_single_register_pdu(0, 0) == bytes.fromhex("0600000000")
    for bad_raw in (-1, 65536, 1.5, True, "3", None):
        expect_value_error(modbus.write_single_register_pdu, 1, bad_raw, match="register value")
    expect_value_error(modbus.write_single_register_pdu, 70000, 1, match="address")
    print("write_single_register_pdu: spec vector + raw 0..65535 / address range  OK")


def test_write_multiple_coils():
    # spec 6.11: 10 coils from coil 20 (address 19) -> 0F 00 13 00 0A 02 CD 01
    states = [1, 0, 1, 1, 0, 0, 1, 1, 1, 0]
    expected = bytes.fromhex("0F0013000A02CD01")
    assert modbus.write_multiple_coils_pdu(19, states) == expected
    assert modbus.write_multiple_coils_pdu(19, [bool(s) for s in states]) == expected
    assert modbus.write_multiple_coils_pdu(0, [True] * 9) == bytes.fromhex("0F0000000902FF01")
    assert modbus.write_multiple_coils_pdu(0xFFFF, [True]) == bytes.fromhex("0FFFFF00010101")
    # the 1968-coil maximum fills the PDU to 6 + 246 bytes (within MAX_PDU)
    big = modbus.write_multiple_coils_pdu(0, [False] * modbus.MAX_WRITE_COILS)
    assert big[3:6] == bytes([0x07, 0xB0, 246]) and len(big) == 252 <= modbus.MAX_PDU
    expect_value_error(modbus.write_multiple_coils_pdu, 0, [], match="coil count")
    expect_value_error(modbus.write_multiple_coils_pdu, 0, [True] * 1969, match="coil count")
    expect_value_error(modbus.write_multiple_coils_pdu, 0, [True, 2], match="coil state [1]")
    expect_value_error(modbus.write_multiple_coils_pdu, 0, [True, "1"], match="coil state [1]")
    expect_value_error(modbus.write_multiple_coils_pdu, 0xFFFF, [True, True], match="past address")
    expect_value_error(modbus.write_multiple_coils_pdu, -1, [True], match="address")
    print("write_multiple_coils_pdu: spec vector, LSB-first packing, 1..1968 limits  OK")


def test_write_multiple_registers():
    # spec 6.12: registers 2..3 = 0x000A, 0x0102 -> 10 00 01 00 02 04 00 0A 01 02
    assert (modbus.write_multiple_registers_pdu(1, [0x000A, 0x0102])
            == bytes.fromhex("100001000204000A0102"))
    assert modbus.write_multiple_registers_pdu(0xFFFF, [0xFFFF]) == bytes.fromhex("10FFFF000102FFFF")
    big = modbus.write_multiple_registers_pdu(0, [0] * modbus.MAX_WRITE_REGISTERS)
    assert big[3:6] == bytes([0, 123, 246]) and len(big) == 252 <= modbus.MAX_PDU
    expect_value_error(modbus.write_multiple_registers_pdu, 0, [], match="register count")
    expect_value_error(modbus.write_multiple_registers_pdu, 0, [0] * 124, match="register count")
    expect_value_error(modbus.write_multiple_registers_pdu, 0, [1, 65536], match="register value [1]")
    expect_value_error(modbus.write_multiple_registers_pdu, 0, [-1], match="register value [0]")
    expect_value_error(modbus.write_multiple_registers_pdu, 0, [1.0], match="register value [0]")
    expect_value_error(modbus.write_multiple_registers_pdu, 0xFFFE, [1, 2, 3], match="past address")
    print("write_multiple_registers_pdu: spec vector, 1..123 registers, value range  OK")


def test_check_write_response():
    fc5 = modbus.write_single_coil_pdu(172, True)
    fc6 = modbus.write_single_register_pdu(1, 3)
    fc15 = modbus.write_multiple_coils_pdu(19, [1, 0, 1, 1, 0, 0, 1, 1, 1, 0])
    fc16 = modbus.write_multiple_registers_pdu(1, [0x000A, 0x0102])
    # acknowledged writes (no exception): FC5/6 echo the request, FC15/16 echo
    # fn + address + quantity
    modbus.check_write_response(fc5, fc5)
    modbus.check_write_response(fc6, fc6)
    modbus.check_write_response(bytes.fromhex("0F0013000A"), fc15)
    modbus.check_write_response(bytes.fromhex("1000010002"), fc16)
    modbus.check_write_response(bytearray(fc6), fc6)
    # exception replies carry the code in the message, like response_data
    expect_value_error(modbus.check_write_response, bytes([0x85, 0x02]), fc5, match="modbus exception 0x02")
    expect_value_error(modbus.check_write_response, bytes([0x86, 0x03]), fc6, match="modbus exception 0x03")
    expect_value_error(modbus.check_write_response, bytes([0x8F, 0x04]), fc15, match="modbus exception 0x04")
    expect_value_error(modbus.check_write_response, bytes([0x90]), fc16, match="modbus exception 0x00")
    # mismatches: other value / address / quantity, wrong function, short or empty reply
    expect_value_error(modbus.check_write_response, bytes.fromhex("0500AC0000"), fc5, match="does not match")
    expect_value_error(modbus.check_write_response, bytes.fromhex("0600020003"), fc6, match="does not match")
    expect_value_error(modbus.check_write_response, bytes.fromhex("0F0013000B"), fc15, match="does not match")
    expect_value_error(modbus.check_write_response, bytes.fromhex("1000010003"), fc16, match="does not match")
    expect_value_error(modbus.check_write_response, fc16, fc16, match="does not match")  # too long
    expect_value_error(modbus.check_write_response, bytes.fromhex("0300010003"), fc6,
                       match="unexpected response function 0x03")
    expect_value_error(modbus.check_write_response, fc6[:4], fc6, match="short write response")
    expect_value_error(modbus.check_write_response, bytes.fromhex("0F0013"), fc15, match="short")
    expect_value_error(modbus.check_write_response, b"", fc6, match="empty PDU")
    # the request itself must be a write PDU
    expect_value_error(modbus.check_write_response, fc6, modbus.read_pdu(3, 1, 1), match="write PDU")
    expect_value_error(modbus.check_write_response, fc6, b"\x06\x00", match="write PDU")
    print("check_write_response: echo rules, exception codes, mismatch/short replies  OK")


def test_write_through_rtu_chain():
    # what a Modbus master does: request -> RTU frame -> slave echo -> unwrap -> check
    req = modbus.write_multiple_registers_pdu(0x10, modbus.encode_value(23.5, "float32"))
    assert req == bytes.fromhex("1000100002" "04" "41BC0000"), req.hex(" ")
    rtu_req = modbus.rtu_wrap(7, req)
    unit, pdu = modbus.rtu_unwrap(rtu_req)
    reply = modbus.rtu_wrap(unit, pdu[:5])          # FC16 reply echoes fn + addr + qty
    runit, rpdu = modbus.rtu_unwrap(reply)
    assert runit == 7
    modbus.check_write_response(rpdu, req)
    print("FC16 float32 write survives the RTU round trip and the echo check  OK")


def test_encode_value_vectors():
    assert modbus.encode_value(1234, "uint16") == [0x04D2]
    assert modbus.encode_value(65535, "uint16") == [0xFFFF]
    assert modbus.encode_value(-1, "int16") == [0xFFFF]
    assert modbus.encode_value(-32768, "int16") == [0x8000]
    assert modbus.encode_value(65536, "uint32") == [0x0001, 0x0000]     # high word first
    assert modbus.encode_value(70000, "uint32") == [0x0001, 0x1170]
    assert modbus.encode_value(-1, "int32") == [0xFFFF, 0xFFFF]
    assert modbus.encode_value(-100000, "int32") == [0xFFFE, 0x7960]
    assert modbus.encode_value(23.5, "float32") == [0x41BC, 0x0000]
    assert modbus.encode_value(-1.5, "float32") == [0xBFC0, 0x0000]
    assert modbus.encode_value(10.0, "uint16", scale=0.1) == [100]
    assert modbus.encode_value(23.4, "int16", scale=0.1) == [234]       # 233.99999... -> 234
    assert modbus.encode_value(-12.3, "int16", scale=0.1) == [0xFF85]   # -123
    assert modbus.encode_value(5, "int32", scale=10) == [0x0000, 0x0000]  # 0.5 -> 0 (ties to even)
    assert modbus.encode_value(15, "int32", scale=10) == [0x0000, 0x0002]  # 1.5 -> 2
    assert modbus.encode_value(12.6, "uint16") == [13]                  # nearest integer
    assert modbus.encode_value(5, "uint16", scale=0) == [5]             # 0 = unscaled, as decode_value
    print("encode_value: known register words for every dtype, scale and rounding  OK")


def test_encode_decode_round_trip():
    cases = [
        (0, "uint16", 1.0), (65535, "uint16", 1.0), (12345, "uint16", 1.0),
        (-32768, "int16", 1.0), (-1, "int16", 1.0), (32767, "int16", 1.0),
        (0, "uint32", 1.0), (4294967295, "uint32", 1.0), (123456789, "uint32", 1.0),
        (-2147483648, "int32", 1.0), (-123456, "int32", 1.0), (2147483647, "int32", 1.0),
        (23.5, "float32", 1.0), (-0.15625, "float32", 1.0), (3.0e38, "float32", 1.0),
        (1.0e-30, "float32", 1.0),
        (23.4, "uint16", 0.1), (-12.3, "int16", 0.1), (-3276.8, "int16", 0.1),
        (6553.5, "uint16", 0.1), (-214748.3648, "int32", 0.001), (429496.7295, "uint32", 0.0001),
        (123.456, "float32", 0.1), (-40.0, "int16", 0.5), (1500, "uint16", 100),
    ]
    for value, dtype, scale in cases:
        regs = modbus.encode_value(value, dtype, scale)
        assert len(regs) == modbus.dtype_registers(dtype) and all(0 <= r <= 0xFFFF for r in regs)
        back = modbus.decode_value(regs_to_bytes(regs), dtype, scale)
        if dtype == "float32":
            tol = abs(value) * 1e-6
        else:
            tol = abs(scale) / 2 + abs(value) * 1e-12   # rounding to a whole raw step
        assert abs(back - value) <= tol, (value, dtype, scale, regs, back)
        if dtype != "float32" and scale == 1.0:
            assert back == value and isinstance(back, int), (value, dtype, back)
    # float32 stores the nearest single-precision value, exactly what decode_value returns
    regs = modbus.encode_value(0.1, "float32")
    assert modbus.decode_value(regs_to_bytes(regs), "float32") == struct.unpack(">f", struct.pack(">f", 0.1))[0]
    print("decode_value(encode_value(v)) == v for all dtypes, negatives, float32, scale 0.1  OK")


def test_encode_value_errors():
    for value, dtype in ((65536, "uint16"), (-1, "uint16"), (32768, "int16"), (-32769, "int16"),
                         (2 ** 32, "uint32"), (-1, "uint32"), (2 ** 31, "int32"),
                         (-(2 ** 31) - 1, "int32"), (65535.5, "uint16"), (10 ** 400, "uint16")):
        expect_value_error(modbus.encode_value, value, dtype, match="out of range")
    expect_value_error(modbus.encode_value, 1e39, "float32", match="out of range for float32")
    expect_value_error(modbus.encode_value, 10 ** 400, "float32", match="out of range")
    expect_value_error(modbus.encode_value, 6553.6, "uint16", 0.1, match="out of range")  # raw 65536
    expect_value_error(modbus.encode_value, 1e300, "float32", 1e-300, match="out of range")
    for bad in (float("nan"), float("inf"), float("-inf")):
        expect_value_error(modbus.encode_value, bad, "float32", match="finite")
        expect_value_error(modbus.encode_value, bad, "int16", match="finite")
    for bad in ("12", None, True, b"\x01", [1]):
        expect_value_error(modbus.encode_value, bad, "uint16", match="must be a number")
    expect_value_error(modbus.encode_value, 1, "float64", match="unknown dtype")
    expect_value_error(modbus.encode_value, 1, "uint16", float("nan"), match="scale")
    expect_value_error(modbus.encode_value, 1, "uint16", float("inf"), match="scale")
    print("encode_value rejects out-of-range, non-finite and non-numeric values  OK")


def main():
    test_function_codes()
    test_read_bits_spec_examples()
    test_response_data_for_bit_reads()
    test_decode_bits_edges()
    test_read_bits_pdu_range_checks()
    test_write_single_coil()
    test_write_single_register()
    test_write_multiple_coils()
    test_write_multiple_registers()
    test_check_write_response()
    test_write_through_rtu_chain()
    test_encode_value_vectors()
    test_encode_decode_round_trip()
    test_encode_value_errors()
    print("\nPASS: Modbus writes + bit reads (FC1/2/5/6/15/16, encode_value)")


if __name__ == "__main__":
    main()
