"""Telnet and RFC 2217 commands: BREAK, answers, and their order with the data (no
sockets, no serial hardware).

  - telnet: IAC BRK is a serial BREAK at its place in the data (also split across two
    reads), IAC IAC BRK is data; an option ser2net doesn't support is refused every time
    it is asked for (RFC 1143), a DONT / WONT for one that is off gets no answer;
  - RFC 2217: an empty SIGNATURE is answered with "ser2net" or the mapping's signature
    (an IAC in it escaped), a client's own signature gets no answer; the SET-CONTROL
    queries answer the port's BREAK / DTR / RTS state and its inbound flow control; flow
    control is one mode at a time, a DSR/DTR port says so (and "no flow control" leaves
    it on); flow controls pyserial can't set and reserved values are refused with the
    current setting and logged; the masks are acknowledged; IAC BRK is a BREAK;
  - read-only: the queries answer the real state, SET-CONTROL is acknowledged as asked
    (pyserial's rfc2217:// open() needs that) and changes nothing;
  - while the port is closed a query answers what the client set (applied at reopen);
  - order: a command that acts on the port comes back at its place in the data
    (PortCommand), and so does every command after it in that read; from_network()
    carries them out at once, as before;
  - the bridge: the data before an RFC 2217 setting change has left the port before the
    change (at most SETTLE_MAX_S), a BREAK holds the line BREAK_S after the data before
    it, never on a read-only mapping or without a port; a BREAK an RFC 2217 client left on
    is turned off when it disconnects.

Run: python3 tests/test_protocol_commands.py
"""
import asyncio
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import ConfigError, MappingConfig
from ser2netweb.engine import bridge
from ser2netweb.engine.protocols import make_session
from ser2netweb.engine.protocols.base import BREAK, PortCommand
from ser2netweb.engine.protocols.rfc2217 import Rfc2217Session
from ser2netweb.engine.protocols.telnet import TelnetSession

IAC, SB, SE, BRK = 255, 250, 240, 243
WILL, WONT, DO, DONT = 251, 252, 253, 254
COM_PORT_OPTION = 44


def sb(*payload: int) -> bytes:
    body = bytes([COM_PORT_OPTION, *payload]).replace(b"\xff", b"\xff\xff")
    return bytes([IAC, SB]) + body + bytes([IAC, SE])


def answer(code: int, value: bytes) -> bytes:
    body = bytes([COM_PORT_OPTION, code]) + value.replace(b"\xff", b"\xff\xff")
    return bytes([IAC, SB]) + body + bytes([IAC, SE])


CONTROL = lambda n: answer(105, bytes([n]))


class FakeSerial:
    """What RFC 2217 reads and sets on a serial.Serial; records each change (and when)."""

    def __init__(self, events=None, **settings):
        d = self.__dict__
        d["events"] = events if events is not None else []
        d.update(baudrate=9600, bytesize=8, parity="N", stopbits=1, xonxoff=False,
                 rtscts=False, dsrdtr=False, rts=True, dtr=True, break_condition=False,
                 cts=False, dsr=False, ri=False, cd=False)
        d.update(settings)

    def __setattr__(self, name, value):
        self.events.append((name, value, time.monotonic()))
        self.__dict__[name] = value

    def reset_input_buffer(self):
        self.events.append(("purge", "in", time.monotonic()))

    def reset_output_buffer(self):
        self.events.append(("purge", "out", time.monotonic()))


def session(ser=None, **kw):
    logs = []
    s = Rfc2217Session(ser if ser is not None else FakeSerial(), log=logs.append, **kw)
    s.take_net_out()  # the initial negotiation
    return s, logs


# ---- telnet --------------------------------------------------------------------------

def test_telnet_break():
    t = TelnetSession()
    t.take_net_out()  # the initial negotiation
    assert t.from_network_parts(b"ab" + bytes([IAC, BRK]) + b"cd") == [b"ab", BREAK, b"cd"]
    assert t.from_network_parts(bytes([IAC, BRK])) == [BREAK]
    # split across two reads
    assert t.from_network_parts(b"x" + bytes([IAC])) == [b"x"]
    assert t.from_network_parts(bytes([BRK]) + b"y") == [BREAK, b"y"]
    # an escaped 0xFF followed by 0xF3 is data
    assert t.from_network_parts(b"\xff\xff\xf3") == [b"\xff\xf3"]
    # from_network() (no port to keep the order with) leaves the BREAK out, as before
    assert t.from_network(b"a" + bytes([IAC, BRK]) + b"b") == b"ab"
    assert t.take_net_out() == b""  # a BREAK has no answer
    print("telnet: IAC BRK is a BREAK at its place in the data (split reads too), IAC IAC "
          "0xF3 is data  OK")


def test_telnet_refuses_every_time():
    t = TelnetSession()
    t.take_net_out()
    for _ in range(3):
        t.from_network(bytes([IAC, DO, 24]))
        assert t.take_net_out() == bytes([IAC, WONT, 24])
        t.from_network(bytes([IAC, WILL, 31]))
        assert t.take_net_out() == bytes([IAC, DONT, 31])
    t.from_network(bytes([IAC, DONT, 24, IAC, WONT, 31]))  # off already: no answer
    assert t.take_net_out() == b""
    t.from_network(bytes([IAC, DO, 0]))  # BINARY, which it does already: no answer either
    assert t.take_net_out() == b""
    print("telnet: an unsupported option is refused each time it is asked for (RFC 1143)  OK")


# ---- RFC 2217 answers ------------------------------------------------------------------

def test_signature():
    s, logs = session()
    s.from_network(sb(0))
    assert s.take_net_out() == answer(100, b"ser2net")
    s.from_network(sb(0, *b"client 1.0"))  # the client's own: nothing to answer
    assert s.take_net_out() == b""
    s, logs = session(signature="Line 3 \xff ü")
    s.from_network(sb(0))
    assert s.take_net_out() == answer(100, "Line 3 \xff ü".encode())
    assert not logs, logs
    m = MappingConfig.from_dict({"name": "s", "serial": {"port": "/dev/ttyX"},
                                 "network": {"protocol": "rfc2217", "port": 4001},
                                 "options": {"rfc2217_signature": "x" * 129}})
    try:
        m.validate_input()
    except ConfigError as e:
        assert "128" in str(e), e
    else:
        raise AssertionError("a 129-character signature was taken")
    m.validate()  # (a stored one keeps loading)
    s = make_session("rfc2217", FakeSerial(), signature=12)  # a hand-edited number
    s.take_net_out()
    s.from_network(sb(0))
    assert s.take_net_out() == answer(100, b"ser2net")
    print("RFC 2217: SIGNATURE answered (default, configured, escaped); a client's own is "
          "not; at most 128 characters at edit time  OK")


def test_control_queries():
    ser = FakeSerial(dtr=False, rts=True)
    s, logs = session(ser)
    s.from_network(sb(5, 4) + sb(5, 7) + sb(5, 10))
    assert s.take_net_out() == CONTROL(6) + CONTROL(9) + CONTROL(11)
    s.from_network(sb(5, 5) + sb(5, 8) + sb(5, 12))  # BREAK on, DTR on, RTS off
    assert s.take_net_out() == CONTROL(5) + CONTROL(8) + CONTROL(12)
    assert (ser.break_condition, ser.dtr, ser.rts) == (True, True, False)
    s.from_network(sb(5, 4) + sb(5, 7) + sb(5, 10))
    assert s.take_net_out() == CONTROL(5) + CONTROL(8) + CONTROL(12)
    assert not logs, logs
    print("RFC 2217: SET-CONTROL queries answer the BREAK / DTR / RTS state  OK")


def test_flow_control():
    ser = FakeSerial()
    s, logs = session(ser)
    for value, want, inbound in ((2, (True, False, False), 15), (3, (False, True, False), 16),
                                 (1, (False, False, False), 14)):
        s.from_network(sb(5, value) + sb(5, 0) + sb(5, 13))
        assert (ser.xonxoff, ser.rtscts, ser.dsrdtr) == want, (value, vars(ser))
        assert s.take_net_out() == CONTROL(value) + CONTROL(value) + CONTROL(inbound)
    # a DSR/DTR port: reported (DSR out, DTR in); "no flow control" leaves it on, as before
    ser = FakeSerial(dsrdtr=True)
    s, logs = session(ser)
    s.from_network(sb(5, 0) + sb(5, 13))
    assert s.take_net_out() == CONTROL(19) + CONTROL(18)
    s.from_network(sb(5, 1))
    assert s.take_net_out() == CONTROL(1) and ser.dsrdtr is True
    s.from_network(sb(5, 3))  # hardware flow control: RTS/CTS instead
    assert (ser.xonxoff, ser.rtscts, ser.dsrdtr) == (False, True, False)
    assert s.take_net_out() == CONTROL(3)
    assert not logs, logs
    # what pyserial can't set, and reserved values: the current setting, logged
    ser = FakeSerial(xonxoff=True)
    s, logs = session(ser)
    for value, current in ((14, 15), (15, 15), (16, 15), (18, 15), (17, 2), (19, 2), (20, 2),
                           (200, 2)):
        s.from_network(sb(5, value))
        assert s.take_net_out() == CONTROL(current), value
    assert (ser.xonxoff, ser.rtscts, ser.dsrdtr) == (True, False, False)
    assert logs and all("refused" in ln for ln in logs), logs
    print("RFC 2217: flow control one mode at a time, DSR/DTR reported, inbound / DCD / DTR "
          "/ DSR / reserved values refused with the current setting  OK")


def test_masks_acknowledged():
    s, logs = session()
    s.from_network(sb(10, 0x0F) + sb(11, 0xFF))
    assert s.take_net_out() == answer(110, b"\x0f") + answer(111, b"\xff")
    assert s._pm.linstate_mask == 0x0F and s._pm.modemstate_mask == 0xFF
    print("RFC 2217: SET-LINESTATE-MASK / SET-MODEMSTATE-MASK acknowledged  OK")


def test_read_only():
    ser = FakeSerial()
    s, logs = session(ser, read_only=True)
    s.from_network(sb(5, 9) + sb(5, 12) + sb(5, 5) + sb(5, 3))  # DTR, RTS off, BREAK, HW
    # acknowledged as asked (pyserial clients' open() needs it), nothing changed
    assert s.take_net_out() == CONTROL(9) + CONTROL(12) + CONTROL(5) + CONTROL(3)
    assert (ser.dtr, ser.rts, ser.break_condition, ser.rtscts) == (True, True, False, False)
    s.from_network(sb(5, 7) + sb(5, 10) + sb(5, 4) + sb(5, 0))  # the queries: the real state
    assert s.take_net_out() == CONTROL(8) + CONTROL(11) + CONTROL(6) + CONTROL(1)
    assert not s.break_left_on()
    print("read-only RFC 2217: SET-CONTROL acknowledged and ignored, queries answer the real "
          "state  OK")


def test_port_closed():
    ser = FakeSerial()
    s, logs = session(ser)
    s.serial_changed(None)
    s.take_net_out()
    s.from_network(sb(5, 7) + sb(5, 9) + sb(5, 7) + sb(5, 4))
    assert s.take_net_out() == CONTROL(9) + CONTROL(9) + CONTROL(9) + CONTROL(6), logs
    ser2 = FakeSerial()
    s.serial_changed(ser2)  # reopened: given what the client set
    assert ser2.dtr is False
    print("RFC 2217 without a port: queries answer what the client set, the reopened port "
          "gets it  OK")


def test_parts_keep_order():
    ser = FakeSerial()
    s, logs = session(ser)
    parts = s.from_network_parts(
        b"a" + sb(1, 0, 0, 0x4B, 0) + b"b" + sb(0) + sb(12, 2) + b"c" + bytes([IAC, BRK]) + b"d")
    kinds = [p if isinstance(p, bytes) else ("BREAK" if p is BREAK else p.settle) for p in parts]
    assert kinds == [b"a", True, b"b", False, False, b"c", "BREAK", b"d"], kinds
    assert ser.baudrate == 9600 and s.take_net_out() == b"", "nothing applied while parsing"
    for p in parts:
        if isinstance(p, PortCommand):
            p.apply()
    assert ser.baudrate == 19200
    assert s.take_net_out() == (answer(101, struct.pack("!I", 19200)) + answer(100, b"ser2net")
                                + answer(112, b"\x02"))
    # a command that changes nothing, before any that does: carried out at once
    parts = s.from_network_parts(b"a" + sb(1, 0, 0, 0, 0) + b"b")
    assert parts == [b"ab"] and s.take_net_out() == answer(101, struct.pack("!I", 19200))
    # from_network(): carried out at once, in order (as before)
    assert s.from_network(b"x" + sb(1, 0, 0, 0x25, 0x80) + b"y" + sb(1, 0, 0, 0, 0)) == b"xy"
    assert s.take_net_out() == answer(101, struct.pack("!I", 9600)) * 2
    print("RFC 2217: commands that act on the port, and those after them, come back at their "
          "place in the data; from_network() applies them in order  OK")


# ---- the bridge ----------------------------------------------------------------------

class FakeTransport:
    def __init__(self, events):
        self.events = events
        self.pending = 0  # what the port has not sent yet

    def get_write_buffer_size(self):
        return self.pending

    def is_closing(self):
        return False

    def abort(self):
        pass


class FakePortWriter:
    """The serial port's StreamWriter: records each write (and when)."""

    def __init__(self, events):
        self.events = events
        self.transport = FakeTransport(events)

    def write(self, data):
        self.events.append(("write", bytes(data), time.monotonic()))

    def is_closing(self):
        return False

    async def drain(self):
        pass


class NetWriter:
    def __init__(self):
        self.transport = self
        self.sent = bytearray()

    def write(self, data):
        self.sent += data

    async def drain(self):
        pass

    def abort(self):
        pass


def runner_with_port(protocol, events, read_only=False, logs=None):
    m = MappingConfig.from_dict({
        "name": "cmd", "serial": {"port": "/dev/ttyX", "baudrate": 9600},
        "network": {"protocol": protocol, "bind_ip": "127.0.0.1", "port": 4001,
                    "read_only": read_only}})
    runner = bridge.MappingRunner(m, logger=(logs.append if logs is not None else None))
    ser = FakeSerial(events)
    runner._link.writer = FakePortWriter(events)
    runner._link.serial = ser
    runner._link.ready.set()
    return runner, ser


async def pump(runner, *chunks):
    reader = asyncio.StreamReader()
    for c in chunks:
        reader.feed_data(c)
    reader.feed_eof()
    conn = bridge._ClientConn(runner, reader, NetWriter(), "127.0.0.1", 1)
    m = runner.mapping
    conn._session = make_session(m.network.protocol, runner.serial_instance,
                                 read_only=m.network.read_only)
    conn._session.take_net_out()
    try:
        await conn._pump_net_to_serial()
    except ConnectionError:
        pass  # closed by peer (EOF)
    return conn


async def _settings_after_data():
    events = []
    runner, ser = runner_with_port("rfc2217", events)
    transport = runner._link.writer.transport
    transport.pending = 100  # the data before the change takes ~0.15 s to go out

    async def drains():
        await asyncio.sleep(0.15)
        transport.pending = 0

    t0 = time.monotonic()
    task = asyncio.create_task(drains())
    await pump(runner, b"old" + sb(1, 0, 0, 0x4B, 0) + b"new")
    await task
    names = [(e[0], e[1]) for e in events]
    assert names == [("write", b"old"), ("baudrate", 19200), ("write", b"new")], names
    assert events[1][2] - t0 >= 0.14, "the change waited for the data before it"
    # a port that never reports an empty queue: the change waits SETTLE_MAX_S at most
    events.clear()
    runner, ser = runner_with_port("rfc2217", events)
    runner._link.writer.transport.pending = 10**6
    t0 = time.monotonic()
    await pump(runner, b"x" * 2000 + sb(1, 0, 0, 0x4B, 0))
    waited = events[-1][2] - t0
    assert events[-1][:2] == ("baudrate", 19200), events[-1]
    assert bridge.SETTLE_MAX_S - 0.1 <= waited <= bridge.SETTLE_MAX_S + 1, waited
    # nothing written before it (pyserial's open: four settings at once): no wait at all
    events.clear()
    runner, ser = runner_with_port("rfc2217", events)
    runner._link.writer.transport.pending = 10**6
    t0 = time.monotonic()
    await pump(runner, sb(1, 0, 0, 0x4B, 0) + sb(2, 7) + sb(3, 3) + sb(4, 2))
    assert time.monotonic() - t0 < 0.5
    assert (ser.baudrate, ser.bytesize, ser.parity, ser.stopbits) == (19200, 7, "E", 2)


def test_settings_after_data():
    asyncio.run(_settings_after_data())
    print("bridge: an RFC 2217 setting change waits until the data before it has left the "
          "port (at most SETTLE_MAX_S; no wait without data)  OK")


async def _break():
    events, logs = [], []
    runner, ser = runner_with_port("telnet", events, logs=logs)
    t0 = time.monotonic()
    await pump(runner, b"before" + bytes([IAC, BRK]) + b"after")
    names = [(e[0], e[1]) for e in events]
    assert names == [("write", b"before"), ("break_condition", True),
                     ("break_condition", False), ("write", b"after")], names
    held = events[2][2] - events[1][2]
    assert bridge.BREAK_S - 0.02 <= held < bridge.BREAK_S + 0.5, held
    assert events[1][2] - t0 < 1.0
    assert any("serial BREAK sent (client 127.0.0.1:1)" in ln for ln in logs), logs
    # RFC 2217's IAC BRK too
    events.clear()
    runner, ser = runner_with_port("rfc2217", events)
    await pump(runner, bytes([IAC, BRK]))
    assert [(e[0], e[1]) for e in events] == [("break_condition", True),
                                             ("break_condition", False)], events
    # read-only: none, logged
    events.clear()
    logs.clear()
    runner, ser = runner_with_port("telnet", events, read_only=True, logs=logs)
    await pump(runner, b"x" + bytes([IAC, BRK]))
    assert events == [], events
    assert any("not sent: the mapping is read-only" in ln for ln in logs), logs
    # no port open: none, logged
    logs.clear()
    runner, ser = runner_with_port("telnet", events, logs=logs)
    runner._link.serial = None
    assert await runner.send_break("console") is False
    assert any("not sent: the serial port is not open" in ln for ln in logs), logs
    # a port without break control: logged, the session goes on
    logs.clear()

    class NoBreak(FakeSerial):
        def __setattr__(self, name, value):
            if name == "break_condition":
                raise OSError(25, "Inappropriate ioctl for device")
            super().__setattr__(name, value)

    runner, ser = runner_with_port("telnet", events, logs=logs)
    runner._link.serial = NoBreak(events)
    assert await runner.send_break("console") is False
    assert any("Inappropriate ioctl" in ln for ln in logs), logs
    # two at once: one after the other
    events.clear()
    runner, ser = runner_with_port("telnet", events)
    sent = await asyncio.gather(runner.send_break("a"), runner.send_break("b"))
    assert sent == [True, True]
    assert [e[1] for e in events] == [True, False, True, False], events


def test_break():
    asyncio.run(_break())
    print("bridge: a BREAK holds the line BREAK_S after the data before it (telnet and RFC "
          "2217), one at a time; none on a read-only mapping, without a port, or on a port "
          "without break control (logged)  OK")


async def _break_left_on():
    events, logs = [], []
    runner, ser = runner_with_port("rfc2217", events, logs=logs)
    conn = await pump(runner, sb(5, 5))  # BREAK on, then the client goes away
    assert ser.break_condition is True
    conn._release_break()
    assert ser.break_condition is False
    assert any("serial BREAK turned off: 127.0.0.1:1 left it on" in ln for ln in logs), logs
    # turned off by the client itself: nothing to do
    logs.clear()
    runner, ser = runner_with_port("rfc2217", events, logs=logs)
    conn = await pump(runner, sb(5, 5) + sb(5, 6))
    events.clear()
    conn._release_break()
    assert events == [] and not any("BREAK" in ln for ln in logs), (events, logs)


def test_break_left_on():
    asyncio.run(_break_left_on())
    print("bridge: a BREAK an RFC 2217 client left on is turned off when it leaves  OK")


def main():
    test_telnet_break()
    test_telnet_refuses_every_time()
    test_signature()
    test_control_queries()
    test_flow_control()
    test_masks_acknowledged()
    test_read_only()
    test_port_closed()
    test_parts_keep_order()
    test_settings_after_data()
    test_break()
    test_break_left_on()
    print("\nPASS: telnet / RFC 2217 commands (BREAK, answers, order)")


if __name__ == "__main__":
    main()
