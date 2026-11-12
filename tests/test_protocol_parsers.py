"""Telnet and RFC 2217 parsing of the network stream (no sockets, no serial hardware).

Both parsers pass the data between telnet commands on in bulk (a regex scan and
bytes.replace) instead of one Python step per byte, and a subnegotiation (IAC SB ...
IAC SE) has a size limit, MAX_SUBNEGOTIATION. Checked here:
  - the output is what the per-byte parsers gave: random streams of data, escaped IACs,
    option negotiation, commands and subnegotiations, cut into chunks at random places
    (an IAC split across two reads included), against pyserial's PortManager.filter
    (RFC 2217, with the answers ser2net adds to it: SIGNATURE, the SET-CONTROL queries,
    one flow control mode at a time, mask acknowledgements) and the previous per-byte
    telnet parser: same bytes for the port, same replies to the client, same serial
    settings, same parser state. Serial data is escaped exactly like PortManager.escape;
  - a chunk without IAC is returned as it is (no copy), both ways;
  - a subnegotiation of MAX_SUBNEGOTIATION bytes is fine, one byte more ends the session
    (ProtocolError) however it is cut into reads, and the RFC 2217 buffer never holds
    more than the limit;
  - CPU: plain data costs a small fraction of what the per-byte parsers took, and data
    full of escaped IACs (an erased flash image) still less than they did;
  - the bridge logs such a disconnect with its reason, and a client whose data does not
    reach the port (read-only) lets other tasks run between its reads;
  - the pyserial internals the RFC 2217 parser builds on are there (pyserial is pinned:
    a version that renames them fails here first).

Run: python3 tests/test_protocol_parsers.py
"""
import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serial import rfc2217 as pyserial_rfc2217

from app.config import MappingConfig
from app.engine import bridge
from app.engine.protocols.base import MAX_SUBNEGOTIATION, ProtocolError
from app.engine.protocols.rfc2217 import Rfc2217Session
from app.engine.protocols.telnet import TelnetSession

IAC, SB, SE = 255, 250, 240
WILL, WONT, DO, DONT = 251, 252, 253, 254
COM_PORT_OPTION = 44


class FakeSerial:
    """The attributes RFC 2217 reads and sets on a serial.Serial."""

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
        self.purges = []

    def reset_input_buffer(self):
        self.purges.append("in")

    def reset_output_buffer(self):
        self.purges.append("out")


class Sink:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf += data


class ReferenceTelnet(TelnetSession):
    """The per-byte network parser TelnetSession had before, as the reference."""

    def from_network(self, data):
        out = bytearray()
        for byte in data:
            st = self._state
            if st == "data":
                if byte == IAC:
                    self._state = "iac"
                else:
                    out.append(byte)
            elif st == "iac":
                if byte == IAC:
                    out.append(IAC)
                    self._state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self._cmd = byte
                    self._state = "opt"
                elif byte == SB:
                    self._state = "sb"
                else:
                    self._state = "data"
            elif st == "opt":
                self._negotiate(self._cmd, byte)
                self._state = "data"
            elif st == "sb":
                if byte == IAC:
                    self._state = "sb_iac"
            elif st == "sb_iac":
                self._state = "data" if byte == SE else "sb"
        return bytes(out)


# ---- random streams --------------------------------------------------------------

def _data(rng):
    return bytes(rng.randrange(255) for _ in range(rng.randint(1, 60)))  # no 0xFF


def _sb(payload: bytes) -> bytes:
    return bytes([IAC, SB]) + payload.replace(b"\xff", b"\xff\xff") + bytes([IAC, SE])


def _com_port_command(rng) -> bytes:
    """A well-formed COM-PORT-OPTION command, values with 0xFF in them included."""
    cmd = rng.choice([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    if cmd == 1:
        value = rng.choice([0, 300, 9600, 19200, 115200, 0x00FF00FF, 0xFF]).to_bytes(4, "big")
    elif cmd == 2:
        value = bytes([rng.choice([0, 5, 6, 7, 8])])
    elif cmd == 3:
        value = bytes([rng.randrange(6)])
    elif cmd == 4:
        value = bytes([rng.randrange(4)])
    elif cmd == 5:
        value = bytes([rng.randrange(13)])
    elif cmd in (10, 11):
        value = bytes([rng.randrange(256)])
    elif cmd == 12:
        value = bytes([rng.randint(1, 3)])
    else:
        value = b""
    return _sb(bytes([COM_PORT_OPTION, cmd]) + value)


def rfc2217_stream(rng, tokens=200) -> bytes:
    out = bytearray()
    for _ in range(tokens):
        k = rng.random()
        if k < 0.35:
            out += _data(rng)
        elif k < 0.5:
            out += b"\xff\xff"
        elif k < 0.62:
            out += bytes([IAC, rng.choice([WILL, WONT, DO, DONT]),
                          rng.choice([0, 1, 3, 24, 31, COM_PORT_OPTION])])
        elif k < 0.67:
            out += bytes([IAC, rng.choice([241, 242, 243, 244, 245, 246, 247, 248, 249])])
        elif k < 0.72:
            out += _sb(bytes([24]) + _data(rng))  # not COM-PORT-OPTION: ignored
        else:
            out += _com_port_command(rng)
    return bytes(out)


def telnet_stream(rng, tokens=200) -> bytes:
    out = bytearray()
    for _ in range(tokens):
        k = rng.random()
        if k < 0.4:
            out += _data(rng)
        elif k < 0.55:
            out += b"\xff\xff"
        elif k < 0.75:
            out += bytes([IAC, rng.choice([WILL, WONT, DO, DONT]), rng.choice([0, 1, 3, 24, 31])])
        elif k < 0.8:
            out += bytes([IAC, rng.choice([241, 242, 243, 244, 245, 246, 247, 248, 249])])
        elif k < 0.95:
            out += _sb(bytes([rng.choice([24, 31, 39])]) + _data(rng) + b"\xff" + _data(rng))
        else:  # an IAC <command> inside a subnegotiation: still inside it
            out += bytes([IAC, SB, 24]) + _data(rng) + bytes([IAC, WILL]) + _data(rng) + bytes([IAC, SE])
    return bytes(out)


def chunks_of(rng, data: bytes):
    out, i = [], 0
    while i < len(data):
        n = rng.choice([1, 1, 2, 3, 5, 8, 13, 64, 200])
        out.append(data[i:i + n])
        i += n
    return out


# ---- RFC 2217 against pyserial -----------------------------------------------------

def test_pyserial_internals():
    """_PortManager keeps PortManager's parser state and calls its handlers."""
    pm = pyserial_rfc2217.PortManager(FakeSerial(), Sink())
    assert (pm.mode, pm.suboption, pm.telnet_command) == (pyserial_rfc2217.M_NORMAL, None, None)
    assert {pyserial_rfc2217.M_NORMAL, pyserial_rfc2217.M_IAC_SEEN,
            pyserial_rfc2217.M_NEGOTIATE} == {0, 1, 2}
    for name in ("_telnet_process_subnegotiation", "_telnet_negotiate_option",
                 "_telnet_process_command", "rfc2217_send_subnegotiation",
                 "check_modem_lines", "filter", "escape"):
        assert callable(getattr(pm, name, None)), name
    for name in ("COM_PORT_OPTION", "SET_CONTROL", "SET_CONTROL_REQ_FLOW_SETTING",
                 "SERVER_PURGE_DATA", "IAC", "SB", "SE", "DO", "DONT", "WILL", "WONT"):
        assert isinstance(getattr(pyserial_rfc2217, name, None), bytes), name
    print("pyserial: the PortManager state, handlers and constants the parser uses exist  OK")


class ReferencePortManager(pyserial_rfc2217.PortManager):
    """pyserial's PortManager with what ser2net answers on top of it, written plainly: an
    empty SIGNATURE gets "ser2net", the SET-CONTROL queries get the port's state, flow
    control is one mode at a time (DSR/DTR reported, and left on by "no flow control"),
    and a mask is acknowledged."""

    def _telnet_process_subnegotiation(self, sub):
        if sub[:1] != bytes([COM_PORT_OPTION]) or len(sub) < 2:
            return super()._telnet_process_subnegotiation(sub)
        cmd, value, ser = sub[1], sub[2:], self.serial
        if cmd == 0:
            if not value:
                self.rfc2217_send_subnegotiation(bytes([100]), b"ser2net")
            return None
        if cmd == 5 and value and value[0] in (0, 1, 2, 3, 4, 7, 10):
            v = value[0]
            if v in (1, 2, 3):
                ser.xonxoff, ser.rtscts = v == 2, v == 3
                if v != 1:
                    ser.dsrdtr = False
                reply = v
            elif v == 0:
                reply = 2 if ser.xonxoff else 3 if ser.rtscts else 19 if ser.dsrdtr else 1
            else:
                attr, on, off = {4: ("break_condition", 5, 6), 7: ("dtr", 8, 9),
                                 10: ("rts", 11, 12)}[v]
                reply = on if getattr(ser, attr) else off
            self.rfc2217_send_subnegotiation(bytes([105]), bytes([reply]))
            return None
        super()._telnet_process_subnegotiation(sub)
        if cmd in (10, 11) and value:
            self.rfc2217_send_subnegotiation(bytes([cmd + 100]), value[:1])
        return None


def _reference_rfc2217(chunks):
    ser, sink = FakeSerial(), Sink()
    pm = ReferencePortManager(ser, sink)
    out = b"".join(b"".join(pm.filter(c)) for c in chunks)
    return out, bytes(sink.buf), vars(ser), (pm.mode, pm.suboption)


def _ours_rfc2217(chunks):
    ser = FakeSerial()
    session = Rfc2217Session(ser)
    net = bytearray(session.initial_net_bytes())
    out = bytearray()
    for c in chunks:
        out += session.from_network(c)
        net += session.take_net_out()
    return bytes(out), bytes(net), vars(ser), (session._pm.mode, session._pm.suboption)


def test_rfc2217_same_as_pyserial():
    rng = random.Random(2217)
    for _ in range(300):
        data = rfc2217_stream(rng)
        if rng.random() < 0.3:  # stop in the middle of something
            data = data[:rng.randrange(len(data))]
        whole = [data]
        cut = chunks_of(rng, data)
        expected = _reference_rfc2217(cut)
        assert _ours_rfc2217(cut) == expected, data.hex()
        assert _ours_rfc2217(whole) == expected, data.hex()
    print("RFC 2217: random streams cut at random places parse exactly like "
          "PortManager.filter  OK")


def test_rfc2217_escape_same_as_pyserial():
    rng = random.Random(7)
    pm = pyserial_rfc2217.PortManager(FakeSerial(), Sink())
    session = Rfc2217Session(FakeSerial())
    for _ in range(200):
        data = bytes(rng.choice([0, 0xFF, 0xFE, 0x41]) for _ in range(rng.randint(0, 300)))
        assert session.from_serial(data) == b"".join(pm.escape(data)), data.hex()
    print("RFC 2217: serial data is escaped exactly like PortManager.escape  OK")


# ---- telnet against the previous parser ------------------------------------------

def _run_telnet(cls, chunks):
    session = cls()
    net = bytearray(session.initial_net_bytes())
    out = bytearray()
    for c in chunks:
        out += session.from_network(c)
        net += session.take_net_out()
    return bytes(out), bytes(net), session._state, session._local, session._remote


def test_telnet_same_as_before():
    rng = random.Random(854)
    for _ in range(300):
        data = telnet_stream(rng)
        if rng.random() < 0.3:
            data = data[:rng.randrange(len(data))]
        cut = chunks_of(rng, data)
        expected = _run_telnet(ReferenceTelnet, cut)
        assert _run_telnet(TelnetSession, cut) == expected, data.hex()
        assert _run_telnet(TelnetSession, [data]) == expected, data.hex()
    print("telnet: random streams cut at random places parse exactly like the per-byte "
          "parser  OK")


# ---- fast path -------------------------------------------------------------------

def test_no_copy_without_iac():
    plain = bytes(range(255)) * 16
    for session in (TelnetSession(), Rfc2217Session(FakeSerial())):
        assert session.from_network(plain) is plain
        assert session.from_serial(plain) is plain
    # with an IAC in the data it is parsed, and the escape is undone
    assert TelnetSession().from_network(b"a\xff\xffb") == b"a\xffb"
    assert Rfc2217Session(FakeSerial()).from_network(b"a\xff\xffb") == b"a\xffb"
    print("a chunk without IAC is passed on as it is (no copy), both ways  OK")


# ---- subnegotiation size limit --------------------------------------------------------

def _expect_limit(session, chunks):
    """Feed chunks; True once a ProtocolError says the subnegotiation is too long."""
    for c in chunks:
        try:
            session.from_network(c)
        except ProtocolError as e:
            assert "subnegotiation longer than 1024 bytes" in str(e), e
            return True
        pm = getattr(session, "_pm", None)
        if pm is not None and pm.suboption is not None:
            assert len(pm.suboption) <= MAX_SUBNEGOTIATION, len(pm.suboption)
    return False


def test_subnegotiation_limit():
    assert MAX_SUBNEGOTIATION == 1024
    rng = random.Random(1)
    body = bytes(rng.randrange(255) for _ in range(MAX_SUBNEGOTIATION))
    for make in (TelnetSession, lambda: Rfc2217Session(FakeSerial())):
        # the limit itself is fine, also in one-byte reads; the session goes on
        for cut in ([body], [body[i:i + 1] for i in range(len(body))]):
            s = make()
            assert not _expect_limit(s, [bytes([IAC, SB])] + cut + [bytes([IAC, SE]) + b"ok"])
            assert s.from_network(b"next") == b"next"
        # one byte more ends it, however it is cut
        for size in (1, 7, 100, 4096):
            data = bytes([IAC, SB]) + body + b"x"
            assert _expect_limit(make(), [data[i:i + size] for i in range(0, len(data), size)])
        # an endless subnegotiation ends at the first 4 KiB read
        s = make()
        s.from_network(bytes([IAC, SB]))
        assert _expect_limit(s, [b"A" * 4096])
        # escaped IACs inside it count too
        assert _expect_limit(make(), [bytes([IAC, SB]) + b"\xff\xff" * (MAX_SUBNEGOTIATION + 1)])
    print("subnegotiation: 1024 bytes are fine, one more ends the session, the RFC 2217 "
          "buffer never holds more  OK")


def test_rfc2217_limit_keeps_commands_working():
    # a long, but not too long, SIGNATURE-sized subnegotiation, then a baud rate change
    ser = FakeSerial()
    s = Rfc2217Session(ser)
    s.from_network(_sb(bytes([COM_PORT_OPTION, 0]) + b"S" * 1000))
    s.from_network(_sb(bytes([COM_PORT_OPTION, 1]) + (19200).to_bytes(4, "big")))
    assert ser.baudrate == 19200
    print("RFC 2217: commands after a long subnegotiation still work  OK")


# ---- CPU sanity ----------------------------------------------------------------------

def _cpu(fn, repeat=3):
    """The fastest of a few runs (perf_counter: process_time ticks in 15.6 ms steps on
    Windows), so a busy machine slows the comparison down less."""
    best = None
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        dt = time.perf_counter() - t
        best = dt if best is None else min(best, dt)
    return max(best, 1e-9)


def test_cpu():
    plain = bytes(range(255)) * 16           # 4080 bytes, no IAC
    escaped = b"\xff\xff" * 2048             # 4096 bytes of escaped 0xFF
    chunks = 128                             # 512 KiB per run

    def through(session, data):
        return lambda: [session.from_network(data) for _ in range(chunks)]

    def pyserial_filter(data):
        pm = pyserial_rfc2217.PortManager(FakeSerial(), Sink())
        return lambda: [b"".join(pm.filter(data)) for _ in range(chunks)]

    rows = []
    for label, data, factor in (("plain", plain, 20), ("escaped IACs", escaped, 1.5)):
        ours = _cpu(through(Rfc2217Session(FakeSerial()), data))
        ref = _cpu(pyserial_filter(data))
        rows.append(f"RFC 2217 {label}: {ref / ours:.0f}x")
        assert ours * factor < ref, (label, ours, ref)
        ours = _cpu(through(TelnetSession(), data))
        ref = _cpu(through(ReferenceTelnet(), data))
        rows.append(f"telnet {label}: {ref / ours:.0f}x")
        assert ours * factor < ref, (label, ours, ref)
    pm = pyserial_rfc2217.PortManager(FakeSerial(), Sink())
    session = Rfc2217Session(FakeSerial())
    serial_data = bytes(range(256)) * 16
    ours = _cpu(lambda: [session.from_serial(serial_data) for _ in range(chunks)])
    ref = _cpu(lambda: [b"".join(pm.escape(serial_data)) for _ in range(chunks)])
    rows.append(f"RFC 2217 escape: {ref / ours:.0f}x")
    assert ours * 20 < ref, (ours, ref)
    print("CPU, less than the per-byte parsers (" + ", ".join(rows) + ")  OK")


# ---- the bridge ----------------------------------------------------------------------

def test_disconnect_reason():
    try:
        TelnetSession().from_network(bytes([IAC, SB]) + b"x" * 2000)
    except ProtocolError as e:
        assert bridge.describe_disconnect(e) == \
            "protocol error: telnet subnegotiation longer than 1024 bytes"
    else:
        raise AssertionError("no ProtocolError")
    print("bridge: the disconnect reason says why  OK")


class _Writer:
    def __init__(self):
        self.transport = self

    def write(self, data):
        pass

    async def drain(self):
        pass

    def abort(self):
        pass


class _RecordingSession(TelnetSession):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def from_network_parts(self, data):
        self.events.append("read")
        return super().from_network_parts(data)


async def _pump_yields():
    m = MappingConfig.from_dict({
        "name": "ro", "serial": {"port": "/dev/ttyX"},
        "network": {"protocol": "telnet", "bind_ip": "127.0.0.1", "port": 48501,
                    "read_only": True}})
    runner = bridge.MappingRunner(m, logger=lambda _m: None)
    reader = asyncio.StreamReader()
    for _ in range(3):
        reader.feed_data(b"x" * bridge.READ_CHUNK)
    reader.feed_eof()
    conn = bridge._ClientConn(runner, reader, _Writer(), "127.0.0.1", 1)
    events = []
    conn._session = _RecordingSession(events)

    async def other_task():
        while True:
            events.append("other")
            await asyncio.sleep(0)

    other = asyncio.create_task(other_task())
    await asyncio.sleep(0)
    try:
        await conn._pump_net_to_serial()
    except ConnectionError as e:
        assert "closed by peer" in str(e), e
    other.cancel()
    reads = [i for i, e in enumerate(events) if e == "read"]
    assert len(reads) == 3, events
    assert all("other" in events[a:b] for a, b in zip(reads, reads[1:])), events


def test_pump_yields_when_nothing_is_written():
    asyncio.run(_pump_yields())
    print("bridge: a read-only client's reads let other tasks run in between  OK")


async def _endless_subnegotiation():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    logs = []
    m = MappingConfig.from_dict({
        "name": "tn", "serial": {"port": "/dev/does-not-exist"},
        "network": {"protocol": "telnet", "bind_ip": "127.0.0.1", "port": port}})
    runner = bridge.MappingRunner(m, logger=logs.append)
    await runner.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(r.read(64), timeout=2)  # the server's negotiation
        w.write(bytes([IAC, SB]))
        closed = False
        try:
            for _ in range(64):  # up to 256 KiB that never ends the subnegotiation
                w.write(b"A" * 4096)
                await w.drain()
                await asyncio.sleep(0.01)
            closed = not await asyncio.wait_for(r.read(64), timeout=2)
        except (ConnectionError, OSError):
            closed = True
        assert closed, "the client was not disconnected"
        for _ in range(40):
            if any("client disconnected" in ln for ln in logs):
                break
            await asyncio.sleep(0.05)
        line = next(ln for ln in logs if "client disconnected" in ln)
        assert "protocol error: telnet subnegotiation longer than 1024 bytes" in line, line
        assert not any("pump error" in ln for ln in logs), logs
        w.close()
        # the mapping goes on serving
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(r2.read(64), timeout=2)
        w2.close()
    finally:
        await runner.stop()


def test_endless_subnegotiation_over_tcp():
    asyncio.run(_endless_subnegotiation())
    print("telnet over TCP: an endless subnegotiation ends that client's session, with the "
          "reason in the log  OK")


def main():
    test_pyserial_internals()
    test_rfc2217_same_as_pyserial()
    test_rfc2217_escape_same_as_pyserial()
    test_telnet_same_as_before()
    test_no_copy_without_iac()
    test_subnegotiation_limit()
    test_rfc2217_limit_keeps_commands_working()
    test_cpu()
    test_disconnect_reason()
    test_pump_yields_when_nothing_is_written()
    test_endless_subnegotiation_over_tcp()
    print("\nPASS: telnet / RFC 2217 parsing (bulk data, bounded subnegotiation)")


if __name__ == "__main__":
    main()
