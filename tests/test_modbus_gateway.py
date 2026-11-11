"""Modbus RTU<->TCP gateway end-to-end (cross-platform, no serial hardware).

The "serial" RTU bus is faked with a TCP loopback slave (the gateway only uses
the StreamReader/StreamWriter interface), so this exercises the real _on_client +
_transaction path: a Modbus/TCP master -> gateway -> RTU slave -> back, including
transaction-id integrity and the gateway-timeout exception. With serial_io.open_serial
replaced by that fake bus, the real start() / serial supervision runs too: the port
goes away (its transport is closed, as the serial library does on an I/O error) and
comes back, and the gateway must reopen it and report the outage, answering 0x0A at once
meanwhile (while a slow first open at start-up is waited for). Masters are admitted
like bridge clients (allow-list, max connections, kick_old_user, priority IPs), and a
read-only gateway answers anything that is not a read with exception 01 without
touching the bus.

How replies are read off the line runs against an in-memory bus (MemoryBus: each part of
a reply is fed after its own delay) and a thread-driven slave (real pauses between the
parts of a reply, on any platform's event loop): a reply ends when it is as long as its
header says, however it is split up, stale bytes in front of it are skipped, and nothing
holds the bus for longer than a whole frame takes after the response timeout.

Run: python3 tests/test_modbus_gateway.py
"""
import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import notify
from app.config import MappingConfig
from app.engine import modbus, serial_io
from app.engine.bridge import FROM_DEVICE, open_tracer
from app.engine.modbus_gateway import GUARD_S, SILENCE_S, TURNAROUND_S, ModbusGatewayRunner

RESPONSIVE_UNIT = 1
SILENT_UNIT = 9
WRONG_UNIT = 7   # slave answers but with a different unit id (must be rejected)
SLAVE_RESPONSE_PDU = bytes([0x03, 0x04, 0x00, 0x0A, 0x00, 0x0B])  # read regs: 2 words
READ_PDU = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
TIMEOUT_PDU = bytes([0x83, modbus.GATEWAY_TARGET_FAILED])
UNAVAILABLE_PDU = bytes([0x83, modbus.GATEWAY_PATH_UNAVAILABLE])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def fake_rtu_slave(reader, writer):
    """A minimal RTU slave: validates the request CRC, answers RESPONSIVE_UNIT,
    silently drops SILENT_UNIT (to drive the gateway-timeout path)."""
    buf = bytearray()
    try:
        while True:
            data = await reader.read(512)
            if not data:
                break
            buf += data
            with suppress(ValueError):
                unit, _pdu = modbus.rtu_unwrap(bytes(buf))  # full frame buffered?
                buf.clear()
                if unit == RESPONSIVE_UNIT:
                    writer.write(modbus.rtu_wrap(unit, SLAVE_RESPONSE_PDU))
                    await writer.drain()
                elif unit == WRONG_UNIT:        # reply, but tagged with the wrong unit id
                    writer.write(modbus.rtu_wrap(unit + 1, SLAVE_RESPONSE_PDU))
                    await writer.drain()
                # SILENT_UNIT: consume but never answer
    except (ConnectionError, OSError):
        pass


async def read_tcp_adu(reader):
    head = await asyncio.wait_for(reader.readexactly(modbus.MBAP_LEN), timeout=3)
    length = (head[4] << 8) | head[5]
    rest = await asyncio.wait_for(reader.readexactly(length - 1), timeout=3)
    txn = (head[0] << 8) | head[1]
    return txn, head[6], rest


async def request(reader, writer, txn, unit, pdu=READ_PDU):
    """One Modbus/TCP request; returns the reply PDU (txn id and unit checked)."""
    writer.write(modbus.build_tcp_adu(txn, unit, pdu))
    await writer.drain()
    rtxn, runit, rpdu = await read_tcp_adu(reader)
    assert (rtxn, runit) == (txn, unit), (hex(rtxn), runit)
    return rpdu


async def wait_for(cond, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


class FakePort:
    """Stands in for serial_io.open_serial: every open is a new TCP connection to the
    fake RTU slave, used as the port's stream pair. ``present = False`` makes an open
    fail as for an unplugged adapter; unplug() also closes the open port's transport,
    which is what the serial library does when a read or write on the port fails."""

    def __init__(self, slave_port, delay=0.0):
        self.slave_port = slave_port
        self.present = True
        self.delay = delay  # an open that takes this long (a slow driver at start-up)
        self.opens = []  # the writer of each opened port

    async def open(self, settings):
        await asyncio.sleep(self.delay)
        if not self.present:
            raise OSError(2, f"could not open port {settings.port}: No such file or directory")
        r, w = await asyncio.open_connection("127.0.0.1", self.slave_port)
        self.opens.append(w)
        return r, w, None, settings.port

    def unplug(self):
        self.present = False
        self.opens[-1].transport.abort()


def gateway_mapping(gw_port, options=None, points=None, **network):
    return MappingConfig.from_dict({
        "name": "mb", "kind": "net", "serial": {"port": "/dev/ttyUSB-fake", "baudrate": 9600},
        "network": dict({"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                         "port": gw_port, "max_connections": 4}, **network),
        "options": dict({"modbus_response_timeout_s": 0.4}, **(options or {})),
        "modbus_poll": {"interval_s": 0.1, "points": points or []}})


@contextlib.asynccontextmanager
async def running_gateway(slave_port, logs=None, options=None, **network):
    """A started gateway whose serial port is a FakePort; yields (runner, its TCP port)."""
    port = FakePort(slave_port)
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    gw_port = free_port()
    runner = ModbusGatewayRunner(gateway_mapping(gw_port, options, **network),
                                 logger=logs.append if logs is not None else (lambda _m: None))
    try:
        await runner.start()
        assert await wait_for(lambda: runner._link.is_open), runner.status.as_dict()
        yield runner, gw_port
    finally:
        await asyncio.wait_for(runner.stop(), timeout=10)
        serial_io.open_serial = real_open


def echo_answer(pdu):
    """A register read (FC3/FC4) gets that many registers (all 0); anything else its own
    PDU back (what a slave sends for an accepted single write, or an FC8/00 echo)."""
    if pdu[0] in (3, 4):
        count = int.from_bytes(pdu[3:5], "big")
        return bytes([pdu[0], 2 * count]) + bytes(2 * count)
    return pdu


def echo_slave(seen):
    """An RTU slave that records each request and answers it with echo_answer."""
    async def handle(reader, writer):
        buf = bytearray()
        try:
            while True:
                data = await reader.read(512)
                if not data:
                    break
                buf += data
                with suppress(ValueError):
                    unit, pdu = modbus.rtu_unwrap(bytes(buf))
                    buf.clear()
                    seen.append((unit, pdu))
                    writer.write(modbus.rtu_wrap(unit, echo_answer(pdu)))
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
    return handle


class MemoryBus:
    """The gateway's serial port as an in-memory stream pair (SerialLink.attach takes it):
    each request the gateway writes is recorded and passed to ``answer(unit, pdu)``, which
    returns the [(delay_s, bytes), ...] to feed back that long after the write."""

    def __init__(self, answer=None):
        self.reader = asyncio.StreamReader()
        self.answer = answer or (lambda unit, pdu: [])
        self.requests = []  # (unit, pdu) of each request, as the line carried it

    def write(self, data):
        unit, pdu = modbus.rtu_unwrap(bytes(data))
        self.requests.append((unit, pdu))
        loop = asyncio.get_running_loop()
        for delay, part in self.answer(unit, pdu):
            loop.call_later(delay, self.reader.feed_data, part)

    async def drain(self):
        pass


def memory_gateway(answer=None, baud=9600, timeout=0.3, logs=None, trace_dir=None):
    """A gateway (not started) whose serial port is a MemoryBus; returns (runner, bus)."""
    mapping = MappingConfig.from_dict({
        "name": "mb", "kind": "net", "serial": {"port": "/dev/ttyUSB-mem", "baudrate": baud},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1", "port": 1502},
        "options": {"modbus_response_timeout_s": timeout,
                    **({"trace_both": "mb.trace"} if trace_dir else {})}})
    runner = ModbusGatewayRunner(mapping, logger=logs.append if logs is not None else
                                 (lambda _m: None), trace_dir=trace_dir)
    bus = MemoryBus(answer)
    runner._link.attach(bus.reader, bus)
    return runner, bus


class Recorder:
    """A console and hex-view observer that keeps what it is given."""

    def __init__(self):
        self.console, self.traffic = [], []

    def feed(self, data):
        self.console.append(bytes(data))

    def feed_traffic(self, direction, data):
        self.traffic.append((direction, bytes(data)))

    def close(self):
        pass


def regs_reply(unit, count, base=0):
    """The RTU frame of an FC3 reply with ``count`` registers base, base+1, ..."""
    data = b"".join((base + i).to_bytes(2, "big") for i in range(count))
    return modbus.rtu_wrap(unit, bytes([3, len(data)]) + data)


def regs_of(rpdu):
    return [int.from_bytes(rpdu[2 + 2 * i:4 + 2 * i], "big") for i in range(rpdu[1] // 2)]


async def timed(coro):
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await coro
    return result, loop.time() - started


async def test_reply_framing():
    """A reply is complete when it is as long as its header says, not after a pause: split
    by pauses far longer than the 10 ms the gateway once took for the end of a frame, it
    still arrives whole; in one piece it is passed on at once."""
    read10 = modbus.read_pdu(3, 0, 10)
    frame = regs_reply(1, 10, base=100)

    runner, bus = memory_gateway(lambda u, p: [(0, frame)])
    runner._gap = 5.0  # a whole reply must not wait for any silence after it
    rpdu, took = await timed(runner._transaction(1, read10))
    assert regs_of(rpdu) == list(range(100, 110)) and took < 1.0, (rpdu, took)
    print(f"whole reply: passed on in {took * 1000:.0f} ms, without waiting for silence  OK")

    # in two or three parts, with pauses of 20 and 120 ms (USB latency, a busy event loop,
    # the Windows clock): still whole, for a sized reply and for a user-defined function
    for parts in ((frame[:9], frame[9:]), (frame[:4], frame[4:20], frame[20:])):
        for pause in (0.02, 0.12):
            runner, bus = memory_gateway(lambda u, p, parts=parts, pause=pause:
                                         [(i * pause, part) for i, part in enumerate(parts)])
            rpdu = await runner._transaction(1, read10)
            assert regs_of(rpdu) == list(range(100, 110)), (pause, rpdu.hex(" "))
    user = modbus.rtu_wrap(1, bytes([0x41]) + bytes(range(12)))
    for pause in (0.02, 0.12):
        runner, bus = memory_gateway(lambda u, p, pause=pause: [(0, user[:6]), (pause, user[6:])])
        assert await runner._transaction(1, b"\x41\x00") == user[1:-2], pause
    print("split replies (20 / 120 ms pauses, 2-3 parts, a user-defined code): whole  OK")


async def test_dropped_bytes():
    """Bytes in front of a reply (the late tail of an earlier one, noise) are skipped; they
    are counted and shown in the trace (ser>drop), the console and the hex view."""
    frame = regs_reply(1, 2, base=7)
    junk = b"\x55\xaa" + regs_reply(1, 10)[-6:]  # noise, then the tail of another reply
    with tempfile.TemporaryDirectory() as tmp:
        runner, bus = memory_gateway(lambda u, p: [(0, junk + frame)], trace_dir=tmp)
        runner.mapping.options.trace_hexdump = True
        runner._tracer = open_tracer(runner.mapping, tmp, lambda _m: None)
        seen = Recorder()
        runner.add_monitor(seen)
        runner.add_traffic_monitor(seen)
        rpdu = await runner._transaction(1, modbus.read_pdu(3, 0, 2))
        assert regs_of(rpdu) == [7, 8], rpdu.hex(" ")
        assert runner.status.bytes_in == len(junk) + len(frame), runner.status.as_dict()
        rx = [data for direction, data in seen.traffic if direction == FROM_DEVICE]
        assert rx == [junk, frame], rx
        assert b"\xaa rx (dropped) " + junk in seen.console, seen.console
        assert b"\xaa rx " + frame in seen.console, seen.console
        path = runner._tracer.path
        await runner._tracer.aclose()
        with open(path, encoding="utf-8") as fh:
            trace = fh.read()
    assert f"ser>drop [{len(junk)}] {junk.hex(' ')}" in trace, trace
    assert f"ser>net [{len(frame)}] {frame.hex(' ')}" in trace, trace
    print("bytes in front of a reply: skipped, counted, in the trace (ser>drop), console "
          "and hex view  OK")


async def test_cut_and_chattering():
    """A reply that never completes ends in 0x0B within the response timeout plus a frame's
    time (and the guard wait for a quiet line after the request before), with one log line
    saying what came (repeats are counted into the next line); a line that never goes
    quiet holds a request back by a bounded time."""
    frame = regs_reply(1, 10)
    logs = []
    runner, bus = memory_gateway(lambda u, p: [(0, frame[:9])], logs=logs)
    limit = runner._resp_timeout + modbus.MAX_ADU * runner._char_s + runner._gap + GUARD_S + 0.2
    for _ in range(3):
        rpdu, took = await timed(runner._transaction(1, modbus.read_pdu(3, 0, 10)))
        assert rpdu == TIMEOUT_PDU and took < limit, (rpdu, took)
    notes = [m for m in logs if m.startswith("unit 1: no usable reply to FC3")]
    assert len(notes) == 1 and "dropped 9 byte(s)" in notes[0] and "cut short" in notes[0], logs
    print(f"cut reply: 0x0B in {took:.2f} s, logged once (then counted)  OK")

    # a line that never stops chattering (faster than the gateway waits for stale bytes
    # before a request), before and after the request
    logs = []
    runner, bus = memory_gateway(logs=logs)
    stop = asyncio.Event()

    async def chatter():
        while not stop.is_set():
            bus.reader.feed_data(b"\x55" * 16)
            await asyncio.sleep(0)
    noise = asyncio.create_task(chatter())
    try:
        await asyncio.sleep(0.05)
        limit = (runner._resp_timeout + 2 * modbus.MAX_ADU * runner._char_s + runner._gap
                 + GUARD_S + 0.3)
        for _ in range(2):
            rpdu, took = await timed(runner._transaction(1, modbus.read_pdu(3, 0, 2)))
            assert rpdu == TIMEOUT_PDU and took < limit, (rpdu, took, limit)
    finally:
        stop.set()
        await noise
    assert sum("no request was waiting for" in m for m in logs) == 1, logs
    print(f"chattering line: each request answered 0x0B within {took:.2f} s (bounded)  OK")


async def test_wire_time():
    """The response timeout starts once the request is out on the line: a long request at a
    low baud rate takes longer than the timeout to send, and its reply still counts."""
    regs = list(range(25))
    pdu = modbus.write_multiple_registers_pdu(0, regs)  # 59 bytes: 0.54 s at 1200 baud
    runner, bus = memory_gateway(lambda u, p: [(0.6, modbus.rtu_wrap(u, p[:5]))],
                                 baud=1200, timeout=0.3)
    rpdu = await runner._transaction(1, pdu)
    assert rpdu == pdu[:5], rpdu.hex(" ")
    print("FC16 of 59 bytes at 1200 baud, timeout 0.3 s: the echo 0.6 s later counts  OK")


def register_slave(delay_of=lambda address: 0.0):
    """MemoryBus answers of a slave whose holding register N holds N, each read answered
    ``delay_of(address)`` seconds after its request came."""
    def answer(unit, pdu):
        address, count = int.from_bytes(pdu[1:3], "big"), int.from_bytes(pdu[3:5], "big")
        return [(delay_of(address), regs_reply(unit, count, base=address))]
    return answer


async def test_late_reply_dropped():
    """A reply that comes after the response timeout is not taken for the next request's:
    a read of the same unit, function and quantity at another address can't tell them apart
    (a read's reply carries no address), so after a request that got no reply in time the
    line has to be quiet for the guard time before the next request goes out. The late
    slave is logged once."""
    logs = []
    runner, bus = memory_gateway(register_slave(lambda a: 0.35 if a == 100 else 0.1),
                                 timeout=0.3, logs=logs)
    first, second = await asyncio.gather(runner._transaction(1, modbus.read_pdu(3, 100, 2)),
                                         runner._transaction(1, modbus.read_pdu(3, 200, 2)))
    assert first == TIMEOUT_PDU, first.hex(" ")
    assert regs_of(second) == [200, 201], f"the next request got {regs_of(second)}"
    late = [m for m in logs if m.startswith("unit 1 answered FC3 after the response timeout")]
    assert len(late) == 1 and "0.3 s" in late[0], logs
    print("late reply: dropped while the line settles, the next read gets its own value, "
          "the late unit is logged  OK")

    # the poll path and a queueing slave (it answers every request 0.35 s after it came,
    # in order): whatever times out, no point ever gets another point's value
    runner, bus = memory_gateway(register_slave(lambda a: 0.35), timeout=0.3)
    points = [MappingConfig.from_dict({"modbus_poll": {"points": [
        {"name": f"p{a}", "unit": 1, "fn": 3, "address": a, "dtype": "uint32"}]}}
    ).modbus_poll.points[0] for a in (10, 20, 30)]
    for p in points:
        try:
            value = await runner._read_point(p)
        except ValueError as e:
            assert "0x0b" in str(e), e
            continue
        assert value == (p.address << 16) | (p.address + 1), (p.name, value)
    print("queueing slave: every poll point gets its own value or 0x0B, never a neighbour's  OK")


async def test_reply_must_answer_request():
    """The frame from the right unit has to answer the request: an FC3 frame on the line is
    no reply to an FC6 write (the echo after it is), a 10-register reply none to a 2-register
    read, and a write's reply must be its echo; what does not answer is dropped and logged."""
    write = modbus.write_single_register_pdu(1, 3)
    stale = modbus.rtu_wrap(1, bytes.fromhex("0302000a"))
    runner, bus = memory_gateway(lambda u, p: [(0, stale), (0.02, modbus.rtu_wrap(u, p))])
    assert await runner._transaction(1, write) == write
    print("FC6: an FC3 frame in front of its echo is skipped, the echo is the reply  OK")

    logs = []
    runner, bus = memory_gateway(lambda u, p: [(0, stale)], logs=logs)
    assert await runner._transaction(1, write) == bytes([0x86, 0x0B])
    assert any(m.startswith("unit 1: no usable reply to FC6") and "none of them answers" in m
               for m in logs), logs

    # FC16: an echo of another quantity; FC5: of the other state
    runner, bus = memory_gateway(lambda u, p: [(0, modbus.rtu_wrap(u, p[:3] + b"\x00\x01"))])
    assert await runner._transaction(1, modbus.write_multiple_registers_pdu(0, [1, 2])) == \
        bytes([0x90, 0x0B])
    runner, bus = memory_gateway(lambda u, p: [(0, modbus.rtu_wrap(u, p[:3] + b"\x00\x00"))])
    assert await runner._transaction(1, modbus.write_single_coil_pdu(5, True)) == \
        bytes([0x85, 0x0B])

    # the poll path: a uint32 point (2 registers) answered with 10 is an error, not a value
    logs = []
    runner, bus = memory_gateway(lambda u, p: [(0, regs_reply(u, 10))], logs=logs)
    point = MappingConfig.from_dict({"modbus_poll": {"points": [
        {"name": "flow", "unit": 1, "fn": 3, "address": 0, "dtype": "uint32"}]}}
    ).modbus_poll.points[0]
    try:
        value = await runner._read_point(point)
        raise AssertionError(f"a 10-register reply was decoded as {value}")
    except ValueError as e:
        assert "0x0b" in str(e), e
    assert any("20 data bytes for 2 register(s)" in m for m in logs), logs
    print("no reply: a wrong function, a write echo that differs, a 10-register reply to a "
          "uint32 point -> 0x0B and a log line  OK")


async def test_broadcast():
    """Unit 0 is a broadcast: every slave carries out a write and none answers. A write goes
    on the bus and gets, after the turnaround delay (not the response timeout), the echo a
    slave would have sent; a read of unit 0 is answered 0x0B at once and never put on the
    bus; anything else is put on the bus and answered 0x0B after the turnaround."""
    runner, bus = memory_gateway(timeout=1.0)
    writes = [modbus.write_single_register_pdu(1, 3), modbus.write_single_coil_pdu(5, True),
              modbus.write_multiple_registers_pdu(10, [1, 2, 3]),
              modbus.write_multiple_coils_pdu(0, [True, False, True]),
              bytes.fromhex("16000400f20025")]
    for pdu in writes:
        reply, took = await timed(runner._transaction(modbus.BROADCAST, pdu))
        assert reply == modbus.broadcast_reply(pdu) and reply is not None, (pdu.hex(" "), reply)
        assert TURNAROUND_S * 0.8 <= took < TURNAROUND_S + 0.25, took
    assert bus.requests == [(0, pdu) for pdu in writes], bus.requests
    print(f"broadcast writes: on the bus, echoed after the turnaround ({took * 1000:.0f} ms, "
          "not the 1 s timeout)  OK")

    bus.requests.clear()
    for pdu in (modbus.read_pdu(3, 0, 2), modbus.read_bits_pdu(1, 0, 8), bytes.fromhex("080000a537")):
        reply, took = await timed(runner._transaction(modbus.BROADCAST, pdu))
        assert reply == bytes([pdu[0] | 0x80, 0x0B]) and took < 0.1, (pdu.hex(" "), reply, took)
    assert bus.requests == [], "a read of unit 0 must not reach the bus"
    reply, took = await timed(runner._transaction(modbus.BROADCAST, b"\x41\x00"))
    assert reply == b"\xc1\x0b" and took < TURNAROUND_S + 0.25, (reply, took)
    assert bus.requests == [(0, b"\x41\x00")], bus.requests
    print("unit 0 reads: 0x0B at once, nothing on the bus; other codes: sent, then 0x0B  OK")

    # a short response timeout caps the turnaround; a slave that answers a broadcast anyway
    # is dropped and does not reach the next request
    runner, bus = memory_gateway(lambda u, p: [(0.01, modbus.rtu_wrap(u, p))] if u == 0 else
                                 [(0, regs_reply(u, 2, base=5))], timeout=0.05)
    pdu = modbus.write_single_register_pdu(1, 3)
    reply, took = await timed(runner._transaction(modbus.BROADCAST, pdu))
    assert reply == pdu and took < 0.2, (reply, took)
    assert regs_of(await runner._transaction(1, modbus.read_pdu(3, 5, 2))) == [5, 6]
    print("broadcast: the turnaround is at most the response timeout; a reply to it is "
          "dropped  OK")


class ThreadSlave:
    """An RTU slave on a plain socket served by a thread, so that the pauses inside a reply
    are real (time.sleep), not event-loop timers: each request is answered with
    ``answer(unit, pdu)``, the [(pause_s, bytes), ...] to send one after the other."""

    def __init__(self, answer):
        self.answer = answer
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        with suppress(OSError):
            conn, _addr = self.sock.accept()
            with conn:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                buf = bytearray()
                while data := conn.recv(512):
                    buf += data
                    try:
                        unit, pdu = modbus.rtu_unwrap(bytes(buf))
                    except ValueError:
                        continue
                    buf.clear()
                    for pause, part in self.answer(unit, pdu):
                        time.sleep(pause)
                        conn.sendall(part)

    def close(self):
        self.sock.close()
        self.thread.join(timeout=5)


async def test_split_reply_real_pauses():
    """A slave whose reply comes in two parts with real pauses between them (5 ms, about
    an FTDI adapter's 16 ms, and more than the silence that ends an unsized frame): the
    gateway passes it on whole, through the TCP master path."""
    frame = regs_reply(1, 10, base=40)
    user = modbus.rtu_wrap(1, bytes([0x41]) + bytes(range(12)))
    for pause in (0.005, 0.016, SILENCE_S + 0.03):
        slave = ThreadSlave(lambda u, p, pause=pause: (
            [(0, frame[:9]), (pause, frame[9:])] if p[0] == 3 else [(0, user[:6]), (pause, user[6:])]))
        runner = ModbusGatewayRunner(gateway_mapping(free_port()), logger=lambda _m: None)
        sr, sw = await asyncio.open_connection("127.0.0.1", slave.port)
        runner._link.attach(sr, sw)
        server = await asyncio.start_server(runner._on_client, "127.0.0.1", 0)
        try:
            mr, mw = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            for txn in (1, 2):
                rpdu = await request(mr, mw, txn, RESPONSIVE_UNIT, modbus.read_pdu(3, 0, 10))
                assert regs_of(rpdu) == list(range(40, 50)), (pause, rpdu.hex(" "))
            assert await request(mr, mw, 3, RESPONSIVE_UNIT, b"\x41\x00") == user[1:-2], pause
            mw.close()
        finally:
            server.close()
            with suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            sw.close()
            slave.close()
    print("thread-driven slave, replies split by 5 / 16 / 80 ms: passed on whole  OK")


async def closed_by_gateway(reader, timeout=3.0):
    """True once the gateway has closed (or reset) this master's connection."""
    try:
        return await asyncio.wait_for(reader.read(1), timeout) == b""
    except (ConnectionError, OSError):
        return True


async def test_request_path(slave_port):
    gw_port = free_port()
    runner = ModbusGatewayRunner(gateway_mapping(gw_port), logger=lambda _m: None)
    runner._resp_timeout = 0.4  # keep the timeout test fast

    # wire the fake serial bus + start the gateway's TCP server (bypassing real serial)
    sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
    runner._link.attach(sr, sw)
    runner._server = await asyncio.start_server(runner._on_client, "127.0.0.1", gw_port)

    try:
        mreader, mwriter = await asyncio.open_connection("127.0.0.1", gw_port)

        # 1) normal request -> the slave's PDU comes back with the same txn id
        mwriter.write(modbus.build_tcp_adu(0x1111, RESPONSIVE_UNIT, READ_PDU))
        await mwriter.drain()
        txn, unit, pdu = await read_tcp_adu(mreader)
        assert (txn, unit, pdu) == (0x1111, RESPONSIVE_UNIT, SLAVE_RESPONSE_PDU), (hex(txn), unit, pdu)
        print("master->gateway->RTU slave->master returns the reply (txn echoed)  OK")

        # 2) transaction-id integrity across back-to-back requests
        mwriter.write(modbus.build_tcp_adu(0x2222, RESPONSIVE_UNIT, READ_PDU))
        mwriter.write(modbus.build_tcp_adu(0x3333, RESPONSIVE_UNIT, READ_PDU))
        await mwriter.drain()
        t1, _, _ = await read_tcp_adu(mreader)
        t2, _, _ = await read_tcp_adu(mreader)
        assert {t1, t2} == {0x2222, 0x3333}, (hex(t1), hex(t2))
        print("pipelined requests get responses with matching txn ids  OK")

        # 3) silent slave -> gateway returns a Modbus exception (0x0B) after timeout
        assert await request(mreader, mwriter, 0x4444, SILENT_UNIT) == TIMEOUT_PDU
        print("non-responding slave -> 0x83 0x0B gateway-timeout exception  OK")

        # 4) a reply tagged with the wrong unit id is rejected (also -> 0x0B)
        assert await request(mreader, mwriter, 0x5555, WRONG_UNIT) == TIMEOUT_PDU
        print("mismatched-unit reply rejected -> 0x83 0x0B exception  OK")

        mwriter.close()
        with suppress(Exception):
            await asyncio.wait_for(mwriter.wait_closed(), timeout=2)
    finally:
        # Python 3.12+ changed Server.wait_closed() to block until every active
        # connection finishes — bound every wait_closed() so a lingering handler
        # can't hang teardown forever (the assertions above already passed).
        runner._server.close()
        with suppress(Exception):
            await asyncio.wait_for(runner._server.wait_closed(), timeout=2)
        sw.close()


async def test_reopen_after_port_loss(slave_port):
    """The port goes away while the gateway runs (no request in flight) and comes back:
    the gateway reports the outage, answers 0x0A (gateway path unavailable) at once
    meanwhile -- queued requests don't wait a response timeout each -- and polls nothing,
    reopens the port, and the same master connection works again. A silent slave is not
    a lost port."""
    port = FakePort(slave_port)
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    gw_port = free_port()
    mapping = gateway_mapping(gw_port, points=[{"name": "level", "unit": RESPONSIVE_UNIT,
                                                "fn": 3, "address": 0, "dtype": "uint32"}])
    logs = []
    runner = ModbusGatewayRunner(mapping, logger=logs.append)
    names = {mapping.id: mapping.name}
    polled = []
    runner._publish_values = polled.append  # what the poll loop would publish
    poll = None

    def snap():
        return {mapping.id: runner.status.as_dict()}

    try:
        await runner.start()
        poll = asyncio.create_task(runner._poll_loop())  # (start() polls only with MQTT on)
        assert await wait_for(lambda: runner.status.state == "running" and port.opens), \
            runner.status.as_dict()
        mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(mr, mw, 0x0101, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        before = notify.snapshot(snap())  # running, with this master connected

        # a slave that does not answer times out, but the port is fine: no reopen
        assert await request(mr, mw, 0x0102, SILENT_UNIT) == TIMEOUT_PDU
        assert len(port.opens) == 1 and runner.status.state == "running"
        assert runner.status.reconnects == 0, runner.status.as_dict()
        print("silent slave: 0x0B, but the port stays open (a timeout is not a lost port)  OK")

        # the adapter goes away; nothing is in flight, the gateway notices anyway
        port.unplug()
        assert await wait_for(lambda: runner.status.state in ("reconnecting", "device-missing")
                              and runner.status.reconnects >= 1), runner.status.as_dict()
        lost = snap()
        assert lost[mapping.id]["last_error"], lost
        assert not runner._link.is_open and runner._link.writer is None
        events = notify.status_events(before, lost, names)
        assert [e[0] for e in events] == ["device_lost"], events
        print(f"port lost: state={runner.status.state}, reconnects={runner.status.reconnects}, "
              f"device_lost event ({lost[mapping.id]['last_error']})  OK")

        # requests during the outage get 0x0A at once: two masters with two requests each
        # are answered within one response timeout (they used to wait one timeout apiece)
        mr2, mw2 = await asyncio.open_connection("127.0.0.1", gw_port)
        loop = asyncio.get_running_loop()
        started = loop.time()
        for w, txns in ((mw, (0x0103, 0x0104)), (mw2, (0x0203, 0x0204))):
            for txn in txns:
                w.write(modbus.build_tcp_adu(txn, RESPONSIVE_UNIT, READ_PDU))

        async def two_answers(r):
            return [await read_tcp_adu(r) for _ in range(2)]
        answers = sum(await asyncio.gather(two_answers(mr), two_answers(mr2)), [])
        elapsed = loop.time() - started
        assert sorted((t, p) for t, _u, p in answers) == [
            (t, UNAVAILABLE_PDU) for t in (0x0103, 0x0104, 0x0203, 0x0204)], answers
        assert elapsed < runner._resp_timeout, f"answered after {elapsed:.2f}s"
        # nothing is polled meanwhile (the outage is logged once, by the link; only a read
        # in flight when the port went away failed)
        await asyncio.sleep(0.3)
        quiet, seen = len(polled), len(logs)
        await asyncio.sleep(0.3)
        assert len(polled) == quiet, "polled while the port was down"
        assert not any(line.startswith("poll ") for line in logs[seen:]), logs[seen:]
        mw2.close()
        print(f"port down: 4 requests from 2 masters -> 0x0A in {elapsed * 1000:.0f} ms, "
              "nothing polled  OK")

        # it comes back: a second open, running again, the same master connection works
        port.present = True
        assert await wait_for(lambda: runner.status.state == "running" and len(port.opens) == 2,
                              timeout=20), runner.status.as_dict()
        assert await request(mr, mw, 0x0105, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        assert await wait_for(lambda: len(polled) > quiet), "polling resumes"
        events = notify.status_events(notify.snapshot(lost), snap(), names)
        assert [e[0] for e in events] == ["device_back"], events
        assert sum("serial open:" in line for line in logs) == 2, logs
        assert any("serial error:" in line for line in logs), logs
        print("port back: reopened (2nd open), running, device_back event, master served "
              "again, polling resumes  OK")
        mw.close()
    finally:
        serial_io.open_serial = real_open
        if poll is not None:
            poll.cancel()
            with suppress(asyncio.CancelledError):
                await poll
        await asyncio.wait_for(runner.stop(), timeout=10)
    assert runner.status.state == "stopped" and not runner._link.is_open


async def test_port_lost_during_request(slave_port):
    """The port goes away while a request waits for its reply: the master gets 0x0A (gateway
    path unavailable) as soon as the gateway notices, not 0x0B after the response timeout."""
    port = FakePort(slave_port)
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    gw_port = free_port()
    runner = ModbusGatewayRunner(gateway_mapping(gw_port, {"modbus_response_timeout_s": 3.0}),
                                 logger=lambda _m: None)
    try:
        await runner.start()
        assert await wait_for(lambda: runner._link.is_open), runner.status.as_dict()
        mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
        loop = asyncio.get_running_loop()
        started = loop.time()
        mw.write(modbus.build_tcp_adu(0x77, SILENT_UNIT, READ_PDU))  # never answered
        await mw.drain()
        await asyncio.sleep(0.2)
        assert runner._bus_lock.locked(), "the request should be waiting for its reply"
        port.unplug()
        txn, _unit, rpdu = await read_tcp_adu(mr)
        took = loop.time() - started
        assert (txn, rpdu) == (0x77, UNAVAILABLE_PDU), (hex(txn), rpdu)
        assert took < 1.5, f"answered after {took:.2f}s (the timeout is 3 s)"
        mw.close()
        print(f"port lost during a request: 0x0A after {took * 1000:.0f} ms, not 0x0B after "
              "the timeout  OK")
    finally:
        serial_io.open_serial = real_open
        await asyncio.wait_for(runner.stop(), timeout=10)


async def test_missing_port_at_start(slave_port):
    """A port that can't be opened while the listener comes up is reported at once, not
    shown as running until the next attempt, and masters get 0x0A at once."""
    port = FakePort(slave_port)
    port.present = False
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    gw_port = free_port()
    runner = ModbusGatewayRunner(gateway_mapping(gw_port), logger=lambda _m: None)
    try:
        await runner.start()
        st = runner.status
        assert st.state in ("reconnecting", "device-missing"), st.as_dict()
        assert "could not open" in st.last_error, st.as_dict()
        mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await request(mr, mw, 1, RESPONSIVE_UNIT) == UNAVAILABLE_PDU
        assert loop.time() - started < runner._resp_timeout
        mw.close()
        print(f"port missing at start: state={st.state} right after start(), 0x0A at once  OK")
    finally:
        serial_io.open_serial = real_open
        await asyncio.wait_for(runner.stop(), timeout=10)


async def test_slow_first_open(slave_port):
    """Requests that arrive while the port's first open is still under way wait for it
    (up to the response timeout, all together) and are served, not refused."""
    port = FakePort(slave_port, delay=0.15)
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    gw_port = free_port()
    # a response timeout well above the open's delay: the wait for the port is bounded by it
    runner = ModbusGatewayRunner(gateway_mapping(gw_port, {"modbus_response_timeout_s": 2.0}),
                                 logger=lambda _m: None)
    try:
        await runner.start()
        assert not runner._link.is_open and not runner._link.down, "the first open is under way"
        masters = [await asyncio.open_connection("127.0.0.1", gw_port) for _ in range(3)]
        replies = await asyncio.gather(*(request(r, w, 0x10 + i, RESPONSIVE_UNIT)
                                         for i, (r, w) in enumerate(masters)))
        assert replies == [SLAVE_RESPONSE_PDU] * 3, replies
        for _r, w in masters:
            w.close()
        print("slow first open: requests at start-up wait for the port and are served  OK")
    finally:
        serial_io.open_serial = real_open
        await asyncio.wait_for(runner.stop(), timeout=10)


async def test_read_only():
    """A read-only gateway forwards reads only; everything else gets exception 01 (illegal
    function) and never reaches the bus. Without read-only, writes still pass."""
    seen = []
    srv = await asyncio.start_server(echo_slave(seen), "127.0.0.1", 0)
    slave_port = srv.sockets[0].getsockname()[1]
    reads = [bytes.fromhex("0300000002"),          # FC3 read holding registers
             bytes.fromhex("080000a537"),          # FC8/00 return query data (echo)
             bytes.fromhex("2b0e0100")]            # FC43/14 read device identification
    refused = [bytes.fromhex("0600010003"),       # FC6 write single register
               bytes.fromhex("0500acff00"),       # FC5 write single coil
               bytes.fromhex("0f0013000a02cd01"),  # FC15 write multiple coils
               bytes.fromhex("100001000204000a0102"),  # FC16 write multiple registers
               bytes.fromhex("16000400f20025"),   # FC22 mask write register
               bytes.fromhex("1700030006000e00030600ff00ff00ff"),  # FC23 read/write
               bytes.fromhex("0800040000"),       # FC8/04 force listen only mode
               bytes.fromhex("2b0d0000"),         # FC43/13 CANopen general reference
               bytes.fromhex("4100")]             # user-defined function code
    logs = []
    try:
        async with running_gateway(slave_port, logs, read_only=True) as (runner, gw_port):
            mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
            txn = 0x100
            for pdu in reads:
                txn += 1
                got = await request(mr, mw, txn, RESPONSIVE_UNIT, pdu)
                assert got == echo_answer(pdu), (pdu.hex(" "), got.hex(" "))
            for pdu in refused:
                txn += 1
                got = await request(mr, mw, txn, RESPONSIVE_UNIT, pdu)
                assert got == bytes([pdu[0] | 0x80, modbus.ILLEGAL_FUNCTION]), (pdu.hex(" "), got)
            # a broadcast write (unit 0) is refused the same way
            assert await request(mr, mw, 0x200, 0, bytes.fromhex("0600010003")) == b"\x86\x01"
            assert [p for _u, p in seen] == reads, f"only the reads may reach the bus: {seen}"
            mw.close()
            assert await wait_for(lambda: not runner._clients)
            assert await wait_for(lambda: any("disconnected" in m for m in logs))
        refusals = [m for m in logs if m.startswith("read-only: refused")]
        assert len(refusals) == 1 and "FC6" in refusals[0], refusals  # once per master
        assert any(m.startswith("read-only: requests from Modbus/TCP masters") for m in logs), logs
        bye = next(m for m in logs if m.startswith("modbus master disconnected"))
        assert f"{len(refused) + 1} request(s) refused" in bye, bye
        print(f"read-only: {len(reads)} reads forwarded; {len(refused) + 1} writes/unknown "
              "-> exception 01, nothing on the bus, logged once  OK")

        # the same requests reach the bus on a writable gateway
        seen.clear()
        async with running_gateway(slave_port) as (runner, gw_port):
            mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
            assert await request(mr, mw, 1, RESPONSIVE_UNIT, refused[0]) == refused[0]
            assert [p for _u, p in seen] == [refused[0]]
            mw.close()
        print("not read-only: a write still reaches the slave  OK")
    finally:
        srv.close()
        with suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=2)


async def test_frame_without_function():
    """A Modbus/TCP frame whose MBAP length leaves no function code (1: the unit id alone)
    is a framing error: the master is dropped, as for other bad frames, and nothing reaches
    the bus (it used to go out as a 3-byte RTU frame and get a made-up 80 0b)."""
    seen = []
    srv = await asyncio.start_server(echo_slave(seen), "127.0.0.1", 0)
    logs = []
    try:
        async with running_gateway(srv.sockets[0].getsockname()[1], logs) as (runner, gw_port):
            mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
            read = modbus.read_pdu(3, 0, 2)
            assert await request(mr, mw, 1, RESPONSIVE_UNIT, read) == echo_answer(read)
            mw.write(bytes([0, 2, 0, 0, 0, 1, RESPONSIVE_UNIT]))
            await mw.drain()
            assert await closed_by_gateway(mr), "the master must be dropped"
            assert await wait_for(lambda: any("bad Modbus/TCP framing" in m and
                                              "invalid MBAP length 1" in m for m in logs)), logs
        assert [p for _u, p in seen] == [read], f"nothing more may reach the bus: {seen}"
    finally:
        srv.close()
        with suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=2)
    print("MBAP length 1 (no function code): master dropped, logged, nothing on the bus  OK")


async def test_master_admission(slave_port):
    """Allow-list, max connections, kick_old_user and priority IPs apply to masters."""
    # a full gateway refuses a new master unless kick_old_user or a priority IP says otherwise
    logs = []
    async with running_gateway(slave_port, logs, max_connections=1,
                               priority_client_ips=["10.9.9.9"]) as (runner, gw_port):
        ar, aw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(ar, aw, 1, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        br, _bw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await closed_by_gateway(br), "a master over max_connections must be refused"
        assert await request(ar, aw, 2, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        assert any("max connections reached" in m for m in logs), logs
    print("full: the next master is refused, the connected one keeps working  OK")

    # kick_old_user: the new master takes over, the old one is dropped with the reason logged
    logs = []
    async with running_gateway(slave_port, logs, max_connections=1,
                               kick_old_user=True) as (runner, gw_port):
        ar, aw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(ar, aw, 1, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        br, bw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(br, bw, 2, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        assert await closed_by_gateway(ar), "the old master should have been kicked"
        assert await wait_for(lambda: len(runner.status.clients) == 1)
        assert any("kicking" in m and "for new client" in m for m in logs), logs
        assert await wait_for(lambda: any("kicked for new client" in m for m in logs)), logs
    print("kick_old_user: a reconnecting master replaces the old connection  OK")

    # a priority IP gets in without kick_old_user and is shown as a priority client
    logs = []
    async with running_gateway(slave_port, logs, max_connections=1,
                               priority_client_ips=["127.0.0.1"]) as (runner, gw_port):
        ar, aw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(ar, aw, 1, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        br, bw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await request(br, bw, 2, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        assert await closed_by_gateway(ar), "a priority master must make room"
        assert await wait_for(lambda: len(runner.status.clients) == 1)
        assert runner.status.as_dict()["clients"][0]["priority"] is True, runner.status.clients
        assert any("for priority client" in m for m in logs), logs
    print("priority IP: admitted at capacity, listed as priority  OK")

    # not in the allow-list: dropped before anything reaches the bus
    async with running_gateway(slave_port, max_connections=1,
                               allowed_client_ips=["10.0.0.0/8"]) as (runner, gw_port):
        cr, _cw = await asyncio.open_connection("127.0.0.1", gw_port)
        assert await closed_by_gateway(cr) and not runner._clients
    print("allow-list: a master outside it is dropped  OK")


async def test_unused_options_logged(slave_port):
    """A banner, open string or close-on string would put bytes outside Modbus frames:
    the gateway sends none of them and says so once at start."""
    logs = []
    unused = {"banner": "hi\\r\\n", "closeon": "BYE", "openstr": "ATZ"}
    async with running_gateway(slave_port, logs, options=unused) as (runner, gw_port):
        mr, mw = await asyncio.open_connection("127.0.0.1", gw_port)
        # no banner in front of the reply: the first bytes are the MBAP header
        assert await request(mr, mw, 7, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        mw.close()
    line = next((m for m in logs if m.startswith("not used by a Modbus gateway")), "")
    assert "banner" in line and "openstr" in line and "closeon" in line, logs
    print("banner/openstr/closeon: not sent, logged once at start  OK")


async def main():
    slave_port = free_port()
    slave_srv = await asyncio.start_server(fake_rtu_slave, "127.0.0.1", slave_port)
    try:
        await test_request_path(slave_port)
        await test_reply_framing()
        await test_dropped_bytes()
        await test_cut_and_chattering()
        await test_wire_time()
        await test_split_reply_real_pauses()
        await test_late_reply_dropped()
        await test_reply_must_answer_request()
        await test_broadcast()
        await test_reopen_after_port_loss(slave_port)
        await test_port_lost_during_request(slave_port)
        await test_missing_port_at_start(slave_port)
        await test_slow_first_open(slave_port)
        await test_read_only()
        await test_frame_without_function()
        await test_master_admission(slave_port)
        await test_unused_options_logged(slave_port)
        print("\nPASS: Modbus RTU<->TCP gateway")
    finally:
        slave_srv.close()
        with suppress(Exception):
            await asyncio.wait_for(slave_srv.wait_closed(), timeout=2)


if __name__ == "__main__":
    asyncio.run(main())
