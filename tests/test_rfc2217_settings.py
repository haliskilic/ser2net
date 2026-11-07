"""RFC 2217 requests the server can't carry out are refused as RFC 2217 says: answered
with the current setting, the client stays connected (no sockets, no serial hardware).

  - malformed or reserved values (a short SET-BAUDRATE, SET-DATASIZE 9, SET-PARITY 6,
    SET-STOPSIZE 4, a value missing) get the current setting as the answer, a SET-CONTROL,
    mask or PURGE-DATA without a (defined) value and an IAC SE without IAC SB are
    ignored; nothing changes on the port, the next request works, each is logged;
  - a value the serial port refuses (Windows SetCommState, a baud rate the OS can't
    take: pyserial stores a new value before the driver sees it) does not stay on the
    serial object: the answer, the cached value and the next change all use the old one;
    a flow control change the port refuses is undone in full, a purge that fails is
    still acknowledged;
  - the bridge logs what a client sends that is refused at most once a minute.

Run: python3 tests/test_rfc2217_settings.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import serial
from serial.serialutil import SerialBase

from app.config import MappingConfig
from app.engine import bridge
from app.engine.protocols.rfc2217 import Rfc2217Session

IAC, SB, SE = 255, 250, 240
COM_PORT_OPTION = 44


def sb(*payload: int) -> bytes:
    body = bytes([COM_PORT_OPTION, *payload]).replace(b"\xff", b"\xff\xff")
    return bytes([IAC, SB]) + body + bytes([IAC, SE])


def answer(code: int, value: bytes) -> bytes:
    """What the server sends for a SET-* request: the server code and the setting."""
    body = bytes([COM_PORT_OPTION, code]) + value.replace(b"\xff", b"\xff\xff")
    return bytes([IAC, SB]) + body + bytes([IAC, SE])


BAUD = lambda b: answer(101, struct.pack("!I", b))  # noqa: E731
DATASIZE = lambda n: answer(102, bytes([n]))  # noqa: E731
PARITY = lambda n: answer(103, bytes([n]))  # noqa: E731
STOPSIZE = lambda n: answer(104, bytes([n]))  # noqa: E731
CONTROL = lambda n: answer(105, bytes([n]))  # noqa: E731


class FakeSerial:
    def __init__(self):
        self.baudrate = 9600
        self.bytesize = 8
        self.parity = "N"
        self.stopbits = 1
        self.xonxoff = False
        self.rtscts = False
        self.dsrdtr = False
        self.rts = True
        self.dtr = True
        self.break_condition = False
        self.cts = self.dsr = self.ri = self.cd = False
        self.purges = 0

    def reset_input_buffer(self):
        self.purges += 1

    def reset_output_buffer(self):
        self.purges += 1

    def settings(self):
        return (self.baudrate, self.bytesize, self.parity, self.stopbits,
                self.xonxoff, self.rtscts, self.dsrdtr)


class DriverSerial(SerialBase):
    """An open pyserial port whose driver refuses some settings: pyserial's own setters
    store the value, then reconfigure the port (_reconfigure_port), which raises like
    Windows SetCommState (SerialException) or termios (OverflowError, termios.error)."""

    def __init__(self, refuse):
        self.refuse = refuse  # (self) -> exception to raise, or None
        super().__init__()
        self.is_open = True
        self.purge_error = None

    def _reconfigure_port(self, force_update=False):
        exc = self.refuse(self)
        if exc is not None:
            raise exc

    def _update_rts_state(self):
        pass

    def _update_dtr_state(self):
        pass

    def _update_break_state(self):
        pass

    cts = dsr = ri = cd = property(lambda self: False)

    def reset_input_buffer(self):
        if self.purge_error:
            raise self.purge_error

    def reset_output_buffer(self):
        pass


def session_for(ser, read_only=False):
    logs = []
    s = Rfc2217Session(ser, read_only=read_only, log=logs.append)
    s.take_net_out()  # the opening negotiation
    return s, logs


# ---- malformed and reserved values -------------------------------------------------

MALFORMED = [
    # request, answer (the current setting), what the log says
    (sb(1), BAUD(9600), "SET-BAUDRATE without a value refused"),
    (sb(1, 0, 0x25), BAUD(9600), "SET-BAUDRATE 00 25 refused"),
    (sb(2), DATASIZE(8), "SET-DATASIZE without a value refused"),
    (sb(2, 4), DATASIZE(8), "SET-DATASIZE 04 refused"),
    (sb(2, 9), DATASIZE(8), "SET-DATASIZE 09 refused"),
    (sb(2, 255), DATASIZE(8), "SET-DATASIZE ff refused"),
    (sb(3), PARITY(1), "SET-PARITY without a value refused"),
    (sb(3, 6), PARITY(1), "SET-PARITY 06 refused"),
    (sb(3, 127), PARITY(1), "SET-PARITY 7f refused"),
    (sb(4), STOPSIZE(1), "SET-STOPSIZE without a value refused"),
    (sb(4, 4), STOPSIZE(1), "SET-STOPSIZE 04 refused"),
    (sb(4, 255), STOPSIZE(1), "SET-STOPSIZE ff refused"),
    (sb(5), b"", "SET-CONTROL without a value ignored"),
    (sb(10), b"", "SET-LINESTATE-MASK without a value ignored"),
    (sb(11), b"", "SET-MODEMSTATE-MASK without a value ignored"),
    (sb(12), b"", "PURGE-DATA without a value ignored"),
    (sb(12, 0), b"", "PURGE-DATA 00 ignored"),
    (sb(12, 4), b"", "PURGE-DATA 04 ignored"),
    (bytes([IAC, SE]), b"", "IAC SE without IAC SB ignored"),
]


def test_malformed_values_answered_with_current_setting():
    for read_only in (False, True):
        for request, expected, logged in MALFORMED:
            ser = FakeSerial()
            s, logs = session_for(ser, read_only)
            before = ser.settings()
            assert s.from_network(b"x" + request + b"y") == b"xy", request.hex()
            assert s.take_net_out() == expected, (request.hex(), expected.hex())
            assert ser.settings() == before and ser.purges == 0, request.hex()
            assert len(logs) == 1 and logged in logs[0], (request.hex(), logs)
            # the session goes on: the next request is carried out
            s.from_network(sb(1, 0, 0, 0x4B, 0))  # 19200
            if not read_only:
                assert ser.baudrate == 19200, request.hex()
            assert s.take_net_out() == BAUD(ser.baudrate)
    print("malformed / reserved values: answered with the current setting or ignored, "
          "logged, the session goes on (read-only too)  OK")


def test_valid_values_still_work():
    ser = FakeSerial()
    s, logs = session_for(ser)
    s.from_network(sb(2, 7) + sb(3, 3) + sb(4, 2) + sb(1, 0, 1, 0xC2, 0))
    assert (ser.bytesize, ser.parity, ser.stopbits, ser.baudrate) == (7, "E", 2, 115200)
    assert s.take_net_out() == DATASIZE(7) + PARITY(3) + STOPSIZE(2) + BAUD(115200)
    s.from_network(sb(4, 3) + sb(3, 5) + sb(10, 0x0F) + sb(11, 0xF0) + sb(12, 3))
    assert (ser.stopbits, ser.parity, ser.purges) == (1.5, "S", 2)
    assert s._pm.linstate_mask == 0x0F and s._pm.modemstate_mask == 0xF0
    assert not logs, logs
    print("valid SET-* / mask / purge requests work as before, nothing logged  OK")


# ---- values the serial port refuses -------------------------------------------------

def _refuse_baud_above(limit, exc):
    return lambda ser: exc if ser._baudrate > limit else None


def test_refused_baud_rate_is_not_kept():
    for exc in (serial.SerialException("SetCommState failed"),
                OverflowError("signed integer is greater than maximum"),
                OSError(22, "Invalid argument")):
        ser = DriverSerial(_refuse_baud_above(1_000_000, exc))
        ser.baudrate = 9600
        s, logs = session_for(ser)
        s.from_network(sb(1, 0xFF, 0xFF, 0xFF, 0xFF))  # 4294967295
        assert s.take_net_out() == BAUD(9600), type(exc)
        assert ser._baudrate == 9600, (type(exc), ser._baudrate)
        assert len(logs) == 1 and "the serial port refused baudrate 4294967295" in logs[0] \
            and "it stays 9600" in logs[0], logs
        # a query reports what the port runs at, the next change works
        s.from_network(sb(1, 0, 0, 0, 0))
        assert s.take_net_out() == BAUD(9600)
        s.from_network(sb(1, 0, 0, 0x4B, 0))
        assert ser._baudrate == 19200 and s.take_net_out() == BAUD(19200)
    print("a baud rate the port refuses (SerialException / OverflowError / OSError) is not "
          "kept: answered and cached as the old one, the next change works  OK")


def test_refused_framing_is_not_kept():
    # Windows: 1.5 stop bits only with 5 data bits
    def dcb_rule(ser):
        if ser._stopbits == 1.5 and ser._bytesize != 5:
            return serial.SerialException("SetCommState failed")
        return None

    ser = DriverSerial(dcb_rule)
    s, logs = session_for(ser)
    s.from_network(sb(4, 3))  # 1.5 stop bits with 8 data bits
    assert s.take_net_out() == STOPSIZE(1) and ser._stopbits == 1, ser._stopbits
    # with the stop bits not kept, the next changes are not blocked by them
    s.from_network(sb(1, 0, 0, 0x4B, 0) + sb(2, 5) + sb(4, 3))
    assert (ser._baudrate, ser._bytesize, ser._stopbits) == (19200, 5, 1.5)
    assert s.take_net_out() == BAUD(19200) + DATASIZE(5) + STOPSIZE(3)
    assert len(logs) == 1 and "refused stopbits 1.5" in logs[0], logs
    print("a stop bits / data bits combination the port refuses is not kept; the next "
          "changes go through  OK")


def test_refused_flow_control_is_undone():
    # a driver that can't do XON/XOFF and RTS/CTS at once
    ser = DriverSerial(lambda ser: serial.SerialException("SetCommState failed")
                       if ser._xonxoff and ser._rtscts else None)
    ser.xonxoff = True
    s, logs = session_for(ser)
    s.from_network(sb(5, 3))  # use hardware flow control
    assert s.take_net_out() == CONTROL(2), "answer: the flow control in effect (XON/XOFF)"
    assert (ser._xonxoff, ser._rtscts) == (True, False)
    assert any("SET-CONTROL 03 failed" in ln for ln in logs), logs
    s.from_network(sb(5, 1) + sb(5, 3))  # no flow control, then hardware: fine now
    assert (ser._xonxoff, ser._rtscts) == (False, True)
    assert s.take_net_out() == CONTROL(1) + CONTROL(3)
    print("a flow control change the port refuses is undone and answered with what is in "
          "effect  OK")


def test_failed_purge_is_acknowledged():
    ser = DriverSerial(lambda ser: None)
    ser.purge_error = serial.SerialException("purge failed")
    s, logs = session_for(ser)
    assert s.from_network(b"a" + sb(12, 1) + b"b") == b"ab"
    assert s.take_net_out() == answer(112, b"\x01")
    assert any("PURGE-DATA 01 failed" in ln for ln in logs), logs
    print("a purge that fails is acknowledged (pyserial clients purge on open) and logged  OK")


# ---- the bridge -------------------------------------------------------------------

class _Writer:
    def __init__(self):
        self.transport = self


def test_bridge_logs_refusals_once_a_minute():
    m = MappingConfig.from_dict({
        "name": "r", "serial": {"port": "/dev/ttyX"},
        "network": {"protocol": "rfc2217", "bind_ip": "127.0.0.1", "port": 48502}})
    logs = []
    runner = bridge.MappingRunner(m, logger=logs.append)
    conn = bridge._ClientConn(runner, None, _Writer(), "10.0.0.7", 5000)
    s = Rfc2217Session(FakeSerial(), log=conn._protocol_log)
    for _ in range(50):
        s.from_network(sb(3, 9))
    assert len(logs) == 1 and logs[0].startswith("client 10.0.0.7:5000: RFC 2217 SET-PARITY 09 "
                                                 "refused"), logs
    print("bridge: refused requests are logged with the client, at most once a minute  OK")


def main():
    test_malformed_values_answered_with_current_setting()
    test_valid_values_still_work()
    test_refused_baud_rate_is_not_kept()
    test_refused_framing_is_not_kept()
    test_refused_flow_control_is_undone()
    test_failed_purge_is_acknowledged()
    test_bridge_logs_refusals_once_a_minute()
    print("\nPASS: RFC 2217 refuses what it can't carry out with the current setting")


if __name__ == "__main__":
    main()
