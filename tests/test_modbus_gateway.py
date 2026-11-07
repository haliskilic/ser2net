"""Modbus RTU<->TCP gateway end-to-end (cross-platform, no serial hardware).

The "serial" RTU bus is faked with a TCP loopback slave (the gateway only uses
the StreamReader/StreamWriter interface), so this exercises the real _on_client +
_transaction path: a Modbus/TCP master -> gateway -> RTU slave -> back, including
transaction-id integrity and the gateway-timeout exception. With serial_io.open_serial
replaced by that fake bus, the real start() / serial supervision runs too: the port
goes away (its transport is closed, as the serial library does on an I/O error) and
comes back, and the gateway must reopen it and report the outage. Masters are admitted
like bridge clients (allow-list, max connections, kick_old_user, priority IPs), and a
read-only gateway answers anything that is not a read with exception 01 without
touching the bus.

Run: python3 tests/test_modbus_gateway.py
"""
import asyncio
import contextlib
import os
import socket
import sys
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import notify
from app.config import MappingConfig
from app.engine import modbus, serial_io
from app.engine.modbus_gateway import ModbusGatewayRunner

RESPONSIVE_UNIT = 1
SILENT_UNIT = 9
WRONG_UNIT = 7   # slave answers but with a different unit id (must be rejected)
SLAVE_RESPONSE_PDU = bytes([0x03, 0x04, 0x00, 0x0A, 0x00, 0x0B])  # read regs: 2 words
READ_PDU = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
TIMEOUT_PDU = bytes([0x83, modbus.GATEWAY_TARGET_FAILED])


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

    def __init__(self, slave_port):
        self.slave_port = slave_port
        self.present = True
        self.opens = []  # the writer of each opened port

    async def open(self, settings):
        if not self.present:
            raise OSError(2, f"could not open port {settings.port}: No such file or directory")
        r, w = await asyncio.open_connection("127.0.0.1", self.slave_port)
        self.opens.append(w)
        return r, w, None, settings.port

    def unplug(self):
        self.present = False
        self.opens[-1].transport.abort()


def gateway_mapping(gw_port, options=None, **network):
    return MappingConfig.from_dict({
        "name": "mb", "kind": "net", "serial": {"port": "/dev/ttyUSB-fake", "baudrate": 9600},
        "network": dict({"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                         "port": gw_port, "max_connections": 4}, **network),
        "options": dict({"modbus_response_timeout_s": 0.4}, **(options or {}))})


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


def echo_slave(seen):
    """An RTU slave that records each request and answers with the request PDU itself
    (what a slave sends back for an accepted single write; good enough for any FC here)."""
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
                    writer.write(modbus.rtu_wrap(unit, pdu))
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
    return handle


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
    the gateway reports the outage, answers 0x0B meanwhile, reopens the port and the
    same master connection works again. A silent slave is not a lost port."""
    port = FakePort(slave_port)
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    gw_port = free_port()
    mapping = gateway_mapping(gw_port)
    logs = []
    runner = ModbusGatewayRunner(mapping, logger=logs.append)
    names = {mapping.id: mapping.name}

    def snap():
        return {mapping.id: runner.status.as_dict()}

    try:
        await runner.start()
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

        # requests during the outage get the gateway-timeout exception
        assert await request(mr, mw, 0x0103, RESPONSIVE_UNIT) == TIMEOUT_PDU

        # it comes back: a second open, running again, the same master connection works
        port.present = True
        assert await wait_for(lambda: runner.status.state == "running" and len(port.opens) == 2,
                              timeout=20), runner.status.as_dict()
        assert await request(mr, mw, 0x0104, RESPONSIVE_UNIT) == SLAVE_RESPONSE_PDU
        events = notify.status_events(notify.snapshot(lost), snap(), names)
        assert [e[0] for e in events] == ["device_back"], events
        assert sum("serial open:" in line for line in logs) == 2, logs
        assert any("serial error:" in line for line in logs), logs
        print("port back: reopened (2nd open), running, device_back event, master served again  OK")
        mw.close()
    finally:
        serial_io.open_serial = real_open
        await asyncio.wait_for(runner.stop(), timeout=10)
    assert runner.status.state == "stopped" and not runner._link.is_open


async def test_missing_port_at_start(slave_port):
    """A port that can't be opened while the listener comes up is reported at once, not
    shown as running until the next attempt."""
    port = FakePort(slave_port)
    port.present = False
    real_open, serial_io.open_serial = serial_io.open_serial, port.open
    runner = ModbusGatewayRunner(gateway_mapping(free_port()), logger=lambda _m: None)
    try:
        await runner.start()
        st = runner.status
        assert st.state in ("reconnecting", "device-missing"), st.as_dict()
        assert "could not open" in st.last_error, st.as_dict()
        print(f"port missing at start: state={st.state} right after start()  OK")
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
                assert await request(mr, mw, txn, RESPONSIVE_UNIT, pdu) == pdu, pdu.hex(" ")
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
        await test_reopen_after_port_loss(slave_port)
        await test_missing_port_at_start(slave_port)
        await test_read_only()
        await test_master_admission(slave_port)
        await test_unused_options_logged(slave_port)
        print("\nPASS: Modbus RTU<->TCP gateway")
    finally:
        slave_srv.close()
        with suppress(Exception):
            await asyncio.wait_for(slave_srv.wait_closed(), timeout=2)


if __name__ == "__main__":
    asyncio.run(main())
