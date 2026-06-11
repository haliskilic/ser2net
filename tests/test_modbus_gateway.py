"""Modbus RTU<->TCP gateway end-to-end (cross-platform, no serial hardware).

The "serial" RTU bus is faked with a TCP loopback slave (the gateway only uses
the StreamReader/StreamWriter interface), so this exercises the real _on_client +
_transaction path: a Modbus/TCP master -> gateway -> RTU slave -> back, including
transaction-id integrity and the gateway-timeout exception.

Run: python3 tests/test_modbus_gateway.py
"""
import asyncio
import os
import socket
import sys
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import modbus
from app.engine.modbus_gateway import ModbusGatewayRunner

RESPONSIVE_UNIT = 1
SILENT_UNIT = 9
WRONG_UNIT = 7   # slave answers but with a different unit id (must be rejected)
SLAVE_RESPONSE_PDU = bytes([0x03, 0x04, 0x00, 0x0A, 0x00, 0x0B])  # read regs: 2 words


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


async def main():
    gw_port, slave_port = free_port(), free_port()
    slave_srv = await asyncio.start_server(fake_rtu_slave, "127.0.0.1", slave_port)

    mapping = MappingConfig.from_dict({
        "name": "mb", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus",
                    "bind_ip": "127.0.0.1", "port": gw_port, "max_connections": 4}})
    runner = ModbusGatewayRunner(mapping, logger=lambda _m: None)
    runner._resp_timeout = 0.4  # keep the timeout test fast

    # wire the fake serial bus + start the gateway's TCP server (bypassing real serial)
    sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
    runner._sreader, runner._swriter, runner.serial_instance = sr, sw, object()
    runner._serial_ready.set()
    runner.status.state = "running"
    runner._server = await asyncio.start_server(runner._on_client, "127.0.0.1", gw_port)

    try:
        mreader, mwriter = await asyncio.open_connection("127.0.0.1", gw_port)

        # 1) normal request -> the slave's PDU comes back with the same txn id
        req_pdu = bytes([0x03, 0x00, 0x00, 0x00, 0x02])
        mwriter.write(modbus.build_tcp_adu(0x1111, RESPONSIVE_UNIT, req_pdu))
        await mwriter.drain()
        txn, unit, pdu = await read_tcp_adu(mreader)
        assert (txn, unit, pdu) == (0x1111, RESPONSIVE_UNIT, SLAVE_RESPONSE_PDU), (hex(txn), unit, pdu)
        print("master->gateway->RTU slave->master returns the reply (txn echoed)  OK")

        # 2) transaction-id integrity across back-to-back requests
        mwriter.write(modbus.build_tcp_adu(0x2222, RESPONSIVE_UNIT, req_pdu))
        mwriter.write(modbus.build_tcp_adu(0x3333, RESPONSIVE_UNIT, req_pdu))
        await mwriter.drain()
        t1, _, _ = await read_tcp_adu(mreader)
        t2, _, _ = await read_tcp_adu(mreader)
        assert {t1, t2} == {0x2222, 0x3333}, (hex(t1), hex(t2))
        print("pipelined requests get responses with matching txn ids  OK")

        # 3) silent slave -> gateway returns a Modbus exception (0x0B) after timeout
        mwriter.write(modbus.build_tcp_adu(0x4444, SILENT_UNIT, req_pdu))
        await mwriter.drain()
        txn, unit, pdu = await read_tcp_adu(mreader)
        assert txn == 0x4444 and pdu == bytes([0x83, modbus.GATEWAY_TARGET_FAILED]), pdu
        print("non-responding slave -> 0x83 0x0B gateway-timeout exception  OK")

        # 4) a reply tagged with the wrong unit id is rejected (also -> 0x0B)
        mwriter.write(modbus.build_tcp_adu(0x5555, WRONG_UNIT, req_pdu))
        await mwriter.drain()
        txn, unit, pdu = await read_tcp_adu(mreader)
        assert txn == 0x5555 and pdu == bytes([0x83, modbus.GATEWAY_TARGET_FAILED]), pdu
        print("mismatched-unit reply rejected -> 0x83 0x0B exception  OK")

        mwriter.close()
        with suppress(Exception):
            await asyncio.wait_for(mwriter.wait_closed(), timeout=2)
        print("\nPASS: Modbus RTU<->TCP gateway")
    finally:
        # Python 3.12+ changed Server.wait_closed() to block until every active
        # connection finishes — bound every wait_closed() so a lingering handler
        # can't hang teardown forever (the assertions above already passed).
        runner._server.close()
        with suppress(Exception):
            await asyncio.wait_for(runner._server.wait_closed(), timeout=2)
        sw.close()
        slave_srv.close()
        with suppress(Exception):
            await asyncio.wait_for(slave_srv.wait_closed(), timeout=2)


if __name__ == "__main__":
    asyncio.run(main())
