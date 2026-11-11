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
    assert modbus.exception_pdu(0x10, modbus.GATEWAY_PATH_UNAVAILABLE) == bytes([0x90, 0x0A])
    print("exception_pdu sets the high bit; 0x0B gateway-timeout, 0x0A path unavailable  OK")


def test_read_request_allow_list():
    """What a read-only gateway still forwards: only requests known to read (an allow-list,
    so a write-ish or unknown function code is refused rather than let through)."""
    reads = [
        bytes.fromhex("0100130013"), bytes.fromhex("0200c40016"),       # FC1/FC2 read bits
        bytes.fromhex("03006b0003"), bytes.fromhex("0400080001"),       # FC3/FC4 registers
        b"\x07", b"\x0b", b"\x0c", b"\x11",                             # exc. status, events, id
        bytes.fromhex("140e0600040001000206000300090002"),             # FC20 read file record
        bytes.fromhex("1804de"),                                       # FC24 read FIFO queue
        bytes.fromhex("080000a537"),                                   # FC8/00 echo query data
        bytes.fromhex("0800020000"),                                   # FC8/02 diagnostic register
        *(bytes([8, 0, sub, 0, 0]) for sub in range(0x0B, 0x13)),      # FC8 counters
        bytes.fromhex("2b0e0100"),                                     # FC43/14 device id
    ]
    for pdu in reads:
        assert modbus.is_read_request(pdu), pdu.hex(" ")
    refused = [
        bytes.fromhex("0500acff00"), bytes.fromhex("0600010003"),       # FC5/FC6
        bytes.fromhex("0f0013000a02cd01"), bytes.fromhex("100001000204000a0102"),  # 15/16
        bytes.fromhex("1507060004000700"),                             # FC21 write file record
        bytes.fromhex("16000400f20025"),                               # FC22 mask write
        bytes.fromhex("1700030006000e00030600ff00ff00ff"),             # FC23 read/WRITE
        bytes.fromhex("0800010000"), bytes.fromhex("080001ff00"),       # FC8/01 restart
        bytes.fromhex("0800030a00"),                                   # FC8/03 change delimiter
        bytes.fromhex("0800040000"),                                   # FC8/04 force listen only
        bytes.fromhex("08000a0000"), bytes.fromhex("0800140000"),       # FC8 clear counters
        bytes.fromhex("0800130000"), bytes.fromhex("0800ff0000"),       # FC8 reserved
        b"\x08", b"\x08\x00",                                          # FC8 without a sub-function
        bytes.fromhex("2b0d0000"), b"\x2b",                             # FC43 CANopen / no MEI type
        b"\x00", b"\x09", b"\x0a", b"\x0d", b"\x0e",                    # invalid / legacy program
        b"\x41\x00", b"\x48", b"\x64", b"\x6e",                          # user-defined ranges
        b"\x83\x0b", b"\xff",                                           # exception codes, junk
        b"",                                                           # empty PDU (MBAP length 1)
    ]
    for pdu in refused:
        assert not modbus.is_read_request(pdu), pdu.hex(" ")
    assert modbus.ILLEGAL_FUNCTION == 0x01
    assert modbus.exception_pdu(0x06, modbus.ILLEGAL_FUNCTION) == bytes([0x86, 0x01])
    print(f"read-only allow-list: {len(reads)} reads pass, {len(refused)} writes/unknown refused  OK")


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


def test_rtu_reply_length():
    """A reply's length comes from its header, as libmodbus frames replies; a function code
    whose replies have no fixed layout gives None (the gateway then waits for silence)."""
    cases = [
        ("0183", 5),            # exception: unit, 0x83, code, CRC
        ("01030a", 15),         # FC3 with a byte count of 10: 5 + 10
        ("0101", 5),            # FC1 before its byte count has come: at least 5
        ("010402", 7), ("010201", 6), ("011702", 7),  # FC4, FC2, FC23
        ("010c08", 13), ("011104", 9), ("011406", 11), ("011506", 11),  # FC12/17/20/21
        ("0105", 8), ("0106", 8), ("010f", 8), ("0110", 8),  # write echoes
        ("0116", 10), ("0107", 5), ("010b", 8),             # FC22, FC7, FC11
        ("0118", 6), ("011800", 6), ("01180006", 12),       # FC24: 6 + a 16-bit byte count
        ("01", 4), ("", 4),     # not even a function code yet: no frame is shorter than 4
    ]
    for head, want in cases:
        got = modbus.rtu_reply_length(bytes.fromhex(head))
        assert got == want, (head, got, want)
    for head in ("0108", "012b", "0141", "0164", "0100"):  # FC8, FC43, user-defined, invalid
        assert modbus.rtu_reply_length(bytes.fromhex(head)) is None, head
    # every frame built for a known function code has exactly the length its header gives
    for pdu in (bytes([3, 4, 0, 1, 0, 2]), bytes([1, 1, 0x05]), bytes([6, 0, 1, 0, 3]),
                bytes([0x10, 0, 1, 0, 2]), bytes([0x16, 0, 4, 0, 0xF2, 0, 0x25]),
                bytes([0x18, 0, 6, 0, 2, 1, 0xB8, 0x12, 0x84]), bytes([0x83, 2])):
        frame = modbus.rtu_wrap(9, pdu)
        assert modbus.rtu_reply_length(frame) == len(frame), pdu.hex(" ")
    print("rtu_reply_length: exceptions, counted reads, fixed writes, FC24; None for FC8/43/"
          "user-defined  OK")


def test_find_reply():
    """The reply to a request is found in what came off the line: by unit, function code and
    length, skipping bytes in front of it; a frame whose length its header does not give is
    tried as a whole once the line is quiet."""
    req = bytes([3, 0, 0, 0, 10])
    reply = modbus.rtu_wrap(1, bytes([3, 20]) + bytes(range(20)))  # 10 registers: 25 bytes
    scan = modbus.find_reply(reply, 1, req)
    assert scan.span == (0, len(reply)) and scan.started, scan
    # complete as soon as it is as long as its header says: nothing more is waited for
    assert modbus.find_reply(reply + b"\x00\x01", 1, req).span == (0, len(reply))
    # cut short: not found, but it has begun and may still complete
    scan = modbus.find_reply(reply[:9], 1, req)
    assert scan.span is None and scan.pending and scan.started, scan
    assert modbus.find_reply(reply[:9], 1, req, quiet=True).span is None
    # a stale tail and noise in front: skipped (even a tail that starts like a reply)
    tail = reply[-6:]
    for junk in (b"\x55\xaa", tail, b"\x01\x03\x10", bytes([1, 3, 0, 0, 0]), b"\x01"):
        scan = modbus.find_reply(junk + reply, 1, req)
        assert scan.span == (len(junk), len(junk) + len(reply)), (junk.hex(" "), scan)
    # another unit, another function, garbage: not a reply, nothing pending
    for other in (modbus.rtu_wrap(2, bytes([3, 20]) + bytes(range(20))),
                  modbus.rtu_wrap(1, bytes([4, 2, 0, 1])), b"\x55" * 40):
        scan = modbus.find_reply(other, 1, req, quiet=True)
        assert scan.span is None and not scan.pending and not scan.started, other.hex(" ")
    # an exception for the request's function is its reply
    exc = modbus.rtu_wrap(1, bytes([0x83, 2]))
    assert modbus.find_reply(exc, 1, req).span == (0, 5)
    # a user-defined function code: a whole frame, but only once the line is quiet
    user = modbus.rtu_wrap(1, bytes([0x41, 1, 2, 3, 4, 5]))
    scan = modbus.find_reply(user, 1, b"\x41\x00")
    assert scan.span is None and scan.pending, scan
    assert modbus.find_reply(user, 1, b"\x41\x00", quiet=True).span == (0, len(user))
    assert modbus.find_reply(user[:5], 1, b"\x41\x00", quiet=True).span is None
    # a slave that miscounts (byte count 20, 22 data bytes): its CRC fails at the counted
    # length, so the whole frame is tried once the line is quiet
    miscounted = modbus.rtu_wrap(1, bytes([3, 20]) + bytes(22))
    assert modbus.find_reply(miscounted, 1, req).span is None
    assert modbus.find_reply(miscounted, 1, req, quiet=True).span == (0, len(miscounted))
    # a byte count past the largest frame never makes anything pending forever
    assert not modbus.find_reply(bytes([1, 3, 0xFF]) + b"\x00" * 3, 1, req).pending
    print("find_reply: reply found by unit/function/length behind stale bytes; other units "
          "and functions skipped; silence ends unsized frames  OK")


def test_reply_mismatch():
    """A reply has to answer its request: the same function (or its exception), the byte
    count of the quantity read, a write's echo; other functions by function code only."""
    ok = [
        ("0300000002", "030400010002"), ("0300000002", "8302"),      # registers / exception
        ("040010007d", "04fa" + "00" * 250),                         # 125 input registers
        ("0100000013", "0103cd6b05"), ("0200000001", "020101"),      # 19 coils, 1 input
        ("1700030006000e00030600ff00ff00ff", "170c" + "00" * 12),     # FC23 reads 6
        ("0600010003", "0600010003"), ("0500acff00", "0500acff00"),  # echoes
        ("0f0013000a02cd01", "0f0013000a"), ("100001000204000a0102", "1000010002"),
        ("16000400f20025", "16000400f20025"),
        ("080000a537", "080000a537"), ("2b0e0100", "2b0e01000000"), ("4100", "41ff"),
        ("11", "1102aaff"), ("07", "076d"),                           # no layout checked
    ]
    for req, reply in ok:
        why = modbus.reply_mismatch(bytes.fromhex(req), bytes.fromhex(reply))
        assert why == "", (req, reply, why)
    bad = [
        ("0600010003", "0302000a", "FC3 reply to FC6"),
        ("0300000002", "0414" + "00" * 20, "FC4 reply to FC3"),
        ("0300000002", "0314" + "00" * 20, "20 data bytes for 2 register(s)"),  # 10 regs
        ("0300c80001", "030400010002", "4 data bytes for 1 register"),
        ("0100000013", "010101", "1 data bytes for 19 bit(s)"),
        ("0600010003", "0600010007", "not the request's echo"),     # another value
        ("0600010003", "0600020003", "not the request's echo"),     # another register
        ("100001000204000a0102", "1000010001", "not the request's echo"),
        ("0f0013000a02cd01", "0f0013000a02cd01", "not the request's echo"),
        ("0300000002", "", "empty"),
    ]
    for req, reply, want in bad:
        why = modbus.reply_mismatch(bytes.fromhex(req), bytes.fromhex(reply))
        assert want in why, (req, reply, why)
    # find_reply skips a whole frame that answers something else and keeps looking
    req = bytes.fromhex("0600010003")
    stale = modbus.rtu_wrap(1, bytes.fromhex("0302000a"))
    echo = modbus.rtu_wrap(1, req)
    scan = modbus.find_reply(stale + echo, 1, req)
    assert scan.span == (len(stale), len(stale) + len(echo)), scan
    scan = modbus.find_reply(modbus.rtu_wrap(1, bytes.fromhex("0600010007")), 1, req, quiet=True)
    assert scan.span is None and "echo" in scan.rejected and not scan.pending, scan
    print(f"reply_mismatch: {len(ok)} replies answer their request, {len(bad)} do not "
          "(function, byte count, echo); find_reply skips them  OK")


def main():
    test_register_read_and_decode()
    test_crc_and_rtu_known_vector()
    test_rtu_round_trip_and_bad_crc()
    test_tcp_adu_build_and_take()
    test_take_tcp_adu_streaming_and_errors()
    test_exception_pdu()
    test_read_request_allow_list()
    test_gateway_conversion_chain()
    test_rtu_reply_length()
    test_find_reply()
    test_reply_mismatch()
    print("\nPASS: Modbus framing (RTU<->TCP)")


if __name__ == "__main__":
    main()
