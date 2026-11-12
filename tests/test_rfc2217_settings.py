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
  - the bridge logs what a client sends that is refused at most once a minute;
  - the serial port reopened under a connected client (a new pyserial object): the
    session drives the new port, which is given the client's settings and line state,
    and leaves the old one alone; while the port is closed the client's settings can be
    read and changed (the reopened port gets them), the modem lines read low and a purge
    has nothing to do; a reopened port that refuses the client's settings ends the
    session with the reason; a read-only session never configures the reopened port;
  - the bridge: SerialLink reports each change of its pyserial object, the runner hands
    it to its RFC 2217 clients, and an RFC 2217 session never starts without a port;
  - with rfc2217_restore_on_disconnect the port goes back to the mapping's baud rate,
    data bits, parity, stop bits and flow control once the client has left (RTS/DTR left
    as they are); off (the default), what the client set stays.

Run: python3 tests/test_rfc2217_settings.py
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import serial
from serial.serialutil import SerialBase

from app.config import MappingConfig
from app.engine import bridge
from app.engine.protocols.base import ProtocolError
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
    # a driver without RTS/CTS: XON/XOFF is turned off first (one mode at a time), then
    # RTS/CTS fails, so XON/XOFF is put back
    ser = DriverSerial(lambda ser: serial.SerialException("SetCommState failed")
                       if ser._rtscts else None)
    ser.xonxoff = True
    s, logs = session_for(ser)
    s.from_network(sb(5, 3))  # use hardware flow control
    assert s.take_net_out() == CONTROL(2), "answer: the flow control in effect (XON/XOFF)"
    assert (ser._xonxoff, ser._rtscts) == (True, False)
    assert any("SET-CONTROL 03 failed" in ln for ln in logs), logs
    s.from_network(sb(5, 1) + sb(5, 0))  # no flow control, and the query: fine
    assert (ser._xonxoff, ser._rtscts) == (False, False)
    assert s.take_net_out() == CONTROL(1) + CONTROL(1)
    # a port that has both: one mode at a time
    ser = DriverSerial(lambda ser: None)
    ser.xonxoff = True
    s, logs = session_for(ser)
    s.from_network(sb(5, 3))
    assert (ser._xonxoff, ser._rtscts) == (False, True) and s.take_net_out() == CONTROL(3)
    s.from_network(sb(5, 2))
    assert (ser._xonxoff, ser._rtscts) == (True, False) and s.take_net_out() == CONTROL(2)
    assert not logs, logs
    print("a flow control change the port refuses is undone and answered with what is in "
          "effect; flow control is one mode at a time  OK")


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
        self.aborted = False

    def abort(self):
        self.aborted = True

    def write(self, data):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def is_closing(self):
        return False


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


# ---- the serial port reopened under a connected client ----------------------------

WILL_COM_PORT = bytes([IAC, 251, COM_PORT_OPTION])
MODEMSTATE = lambda n: answer(107, bytes([n]))  # noqa: E731


def test_session_follows_a_reopened_port():
    old, new = FakeSerial(), FakeSerial()
    old.cts = old.dsr = True
    s, logs = session_for(old)
    s.from_network(WILL_COM_PORT)  # the client speaks RFC 2217: modem lines are reported
    assert MODEMSTATE(0x30 | 0x03) in s.take_net_out()  # CTS, DSR (+ their change bits)
    s.from_network(sb(1, 0, 0, 0x4B, 0) + sb(3, 3) + sb(5, 9) + sb(5, 12))  # 19200 E, DTR/RTS off
    assert (old.baudrate, old.parity, old.dtr, old.rts) == (19200, "E", False, False)
    s.take_net_out()

    # the port is gone: the lines read low, settings are what the client knows and
    # changes wait for the port; the old object is not touched any more
    s.serial_changed(None)
    assert s.take_net_out() == MODEMSTATE(0x03)  # CTS and DSR dropped
    s.from_network(sb(1, 0, 0, 0, 0) + sb(1, 0, 0, 0x96, 0) + sb(12, 3) + sb(5, 8))
    assert s.take_net_out() == BAUD(19200) + BAUD(38400) + answer(112, b"\x03") + CONTROL(8)
    assert (old.baudrate, old.purges, old.dtr) == (19200, 0, False), "the old port was used"

    # reopened: the new port gets the client's settings and line state
    new.cts = True
    s.serial_changed(new)
    assert (new.baudrate, new.parity, new.bytesize, new.stopbits) == (38400, "E", 8, 1)
    assert (new.dtr, new.rts) == (True, False)
    assert s.take_net_out() == MODEMSTATE(0x10 | 0x01)
    s.from_network(sb(1, 0, 0, 0xE1, 0) + sb(12, 1))  # 57600, purge
    assert (new.baudrate, new.purges) == (57600, 1)
    assert s.take_net_out() == BAUD(57600) + answer(112, b"\x01")
    assert (old.baudrate, old.purges) == (19200, 0), "the old port was used"
    assert not logs, logs
    print("reopened port: the session drives the new one, which gets the client's settings "
          "and lines; while the port is closed the old one is left alone  OK")


def test_reopened_port_refusing_client_settings():
    s, logs = session_for(FakeSerial())
    s.from_network(sb(1, 0, 0x1E, 0x84, 0x80))  # 2000000
    new = DriverSerial(_refuse_baud_above(1_000_000, serial.SerialException("SetCommState")))
    new.baudrate = 9600
    try:
        s.serial_changed(new)
    except ProtocolError as e:
        assert str(e) == "the reopened serial port refused the client's settings " \
                         "(baudrate 2000000)", e
    else:
        raise AssertionError("the session went on with settings the port refused")
    assert new._baudrate == 9600, "the refused value stayed on the reopened port"
    assert any("refused baudrate 2000000" in ln for ln in logs), logs
    print("a reopened port that refuses the client's settings ends the session, saying "
          "which  OK")


def test_read_only_session_leaves_reopened_port_alone():
    old, new = FakeSerial(), FakeSerial()
    new.baudrate = 115200
    s, _logs = session_for(old, read_only=True)
    s.serial_changed(None)
    s.from_network(sb(1, 0, 0, 0x4B, 0))
    s.serial_changed(new)
    assert new.settings() == (115200, 8, "N", 1, False, False, False)
    s.take_net_out()
    s.from_network(sb(1, 0, 0, 0, 0))
    assert s.take_net_out() == BAUD(115200)  # the reopened port's own setting
    print("read-only: a reopened port is not configured, its settings are reported  OK")


def rfc2217_runner(logs, mode="server", **options):
    net = {"protocol": "rfc2217", "bind_ip": "127.0.0.1", "port": 48503, "mode": mode}
    if mode == "client":
        net.update(remote_host="127.0.0.1", remote_port=48504)
    m = MappingConfig.from_dict({"name": "r", "serial": {"port": "/dev/ttyX"}, "network": net,
                                 "options": options})
    return bridge.MappingRunner(m, logger=logs.append)


async def _bridge_follows_the_port():
    logs = []
    runner = rfc2217_runner(logs)
    first, second, third = FakeSerial(), FakeSerial(), DriverSerial(
        _refuse_baud_above(1_000_000, serial.SerialException("SetCommState")))
    third.baudrate = 9600
    reader = asyncio.StreamReader()
    runner._link.attach(reader, _Writer(), first, "/dev/ttyX")
    conn = bridge._ClientConn(runner, asyncio.StreamReader(), _Writer(), "10.0.0.8", 4000)
    conn._session = Rfc2217Session(runner.serial_instance, log=conn._protocol_log)
    conn._session.take_net_out()
    runner._clients.add(conn)
    conn._session.from_network(sb(1, 0, 0, 0x4B, 0))
    assert first.baudrate == 19200

    await runner._link.close()  # the port went away
    runner._link.attach(reader, _Writer(), second, "/dev/ttyX")  # and is back
    assert second.baudrate == 19200 and not conn._closing
    # a reopened port that refuses the client's settings: the client is disconnected
    conn._session.from_network(sb(1, 0, 0x1E, 0x84, 0x80))
    await runner._link.close()
    runner._link.attach(reader, _Writer(), third, "/dev/ttyX")
    assert conn._closing and conn.writer.aborted
    assert conn.close_reason == "the reopened serial port refused the client's settings " \
                                "(baudrate 2000000)", conn.close_reason

    # other protocols' sessions are left alone
    raw = bridge.MappingRunner(MappingConfig.from_dict({
        "name": "raw", "serial": {"port": "/dev/ttyX"},
        "network": {"protocol": "raw", "bind_ip": "127.0.0.1", "port": 48505}}),
        logger=logs.append)
    other = bridge._ClientConn(raw, asyncio.StreamReader(), _Writer(), "10.0.0.9", 1)
    other.serial_changed = lambda _s: (_ for _ in ()).throw(AssertionError("called"))
    raw._clients.add(other)
    raw._link.attach(reader, _Writer(), FakeSerial(), "/dev/ttyX")
    await raw._link.close()
    assert not any("not taken up" in ln for ln in logs), logs


async def _no_session_without_a_port():
    logs = []
    runner = rfc2217_runner(logs, mode="client")
    w = _Writer()
    conn = bridge._ClientConn(runner, asyncio.StreamReader(), w, "127.0.0.1", 48504)
    await conn.run()  # the port is not open
    assert conn._session is None and conn._closing
    assert conn.close_reason == "the serial port is not open", conn.close_reason
    # connect-out waits for the port, and gives up when the mapping stops meanwhile
    wait = asyncio.ensure_future(runner._serial_open())
    await asyncio.sleep(0.05)
    assert not wait.done()
    runner._link.attach(asyncio.StreamReader(), _Writer(), FakeSerial(), "/dev/ttyX")
    assert await asyncio.wait_for(wait, 1) is True
    await runner._link.close()
    wait = asyncio.ensure_future(runner._serial_open())
    await asyncio.sleep(0.05)
    runner._stop.set()
    assert await asyncio.wait_for(wait, 1) is False


async def _restore_on_disconnect():
    for on in (True, False):
        logs = []
        runner = rfc2217_runner(logs, rfc2217_restore_on_disconnect=on)
        ser = FakeSerial()  # the mapping's 9600 8N1, no flow control
        runner._link.attach(asyncio.StreamReader(), _Writer(), ser, "/dev/ttyX")
        # what an RFC 2217 client set
        ser.baudrate, ser.parity, ser.stopbits, ser.rtscts, ser.dtr = 19200, "E", 2, True, False
        client = object()
        runner._clients.add(client)
        runner._restore_port_settings()  # the client is still there: nothing yet
        assert ser.baudrate == 19200
        runner._clients.discard(client)
        runner._restore_port_settings()
        if on:
            assert ser.settings() == (9600, 8, "N", 1, False, False, False), ser.settings()
            assert ser.dtr is False, "RTS/DTR are left as they are"
            assert any("serial settings restored to the mapping's (9600N81, flow control none)"
                       in ln for ln in logs), logs
        else:
            assert ser.settings() == (19200, 8, "E", 2, False, True, False)
            assert not any("restored" in ln for ln in logs), logs
        # nothing to restore: nothing logged; and never while the mapping stops
        n = len(logs)
        runner._restore_port_settings()
        assert len(logs) == n
        ser.baudrate = 115200
        runner._stop.set()
        runner._restore_port_settings()
        assert ser.baudrate == 115200


def test_restore_on_disconnect():
    asyncio.run(_restore_on_disconnect())
    print("restore on disconnect: on, the port gets the mapping's settings back once the "
          "client has left (not RTS/DTR); off, the client's stay  OK")


def test_bridge_follows_the_port():
    asyncio.run(_bridge_follows_the_port())
    asyncio.run(_no_session_without_a_port())
    print("bridge: SerialLink's port changes reach the RFC 2217 client (a refusal "
          "disconnects it with the reason); no RFC 2217 session without a port; "
          "connect-out waits for it  OK")


def main():
    test_malformed_values_answered_with_current_setting()
    test_valid_values_still_work()
    test_refused_baud_rate_is_not_kept()
    test_refused_framing_is_not_kept()
    test_refused_flow_control_is_undone()
    test_failed_purge_is_acknowledged()
    test_bridge_logs_refusals_once_a_minute()
    test_session_follows_a_reopened_port()
    test_reopened_port_refusing_client_settings()
    test_read_only_session_leaves_reopened_port_alone()
    test_bridge_follows_the_port()
    test_restore_on_disconnect()
    print("\nPASS: RFC 2217 refuses what it can't carry out with the current setting, and "
          "follows a reopened port")


if __name__ == "__main__":
    main()
