"""Modbus register polling -> MQTT (edge mode) end-to-end (Phase 2).

A Modbus-gateway mapping with MQTT + poll points reads registers off the RTU bus
and publishes decoded values to MQTT. The RTU bus is a TCP-loopback fake slave and
the MQTT client is a fake, so this runs cross-platform with no broker/hardware.

Run: python3 tests/test_modbus_poll.py
"""
import asyncio
import os
import socket
import struct
import sys
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import modbus
from app.engine.modbus_gateway import ModbusGatewayRunner
from app.engine.mqtt_pub import MqttPublisher


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeMqtt:
    def __init__(self):
        self.published = []

    def will_set(self, *a, **k):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload)))


async def fake_slave(reader, writer):
    """Answer read requests: 2-register reads -> float32 23.5, 1-register -> uint16 1234."""
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
                fn = pdu[0]
                count = (pdu[3] << 8) | pdu[4]
                payload = struct.pack(">f", 23.5) if count == 2 else struct.pack(">H", 1234)
                resp = bytes([fn, len(payload)]) + payload
                writer.write(modbus.rtu_wrap(unit, resp))
                await writer.drain()
    except (ConnectionError, OSError):
        pass


async def main():
    gw_port, slave_port = free_port(), free_port()
    slave_srv = await asyncio.start_server(fake_slave, "127.0.0.1", slave_port)

    mapping = MappingConfig.from_dict({
        "name": "edge", "kind": "net", "serial": {"port": "fake", "baudrate": 9600},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1", "port": gw_port},
        "mqtt": {"enabled": True, "host": "broker", "base_topic": "plant/a", "qos": 0},
        "modbus_poll": {"interval_s": 0.1, "points": [
            {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
            {"name": "count", "unit": 1, "fn": 3, "address": 10, "dtype": "uint16"},
        ]},
    })
    mapping.validate()  # also proves config accepts the poll points
    runner = ModbusGatewayRunner(mapping, logger=lambda _m: None)
    runner._resp_timeout = 0.4

    sr, sw = await asyncio.open_connection("127.0.0.1", slave_port)
    runner._sreader, runner._swriter, runner.serial_instance = sr, sw, object()
    runner._serial_ready.set()
    runner.status.state = "running"
    fake = FakeMqtt()
    runner._mqtt = MqttPublisher(mapping.mqtt, logger=lambda _m: None, client=fake)
    runner._mqtt.connect()

    poll = asyncio.create_task(runner._poll_loop())
    try:
        for _ in range(60):  # wait for the first poll cycle to publish both points
            topics = {t for (t, _p) in fake.published}
            if "plant/a/temp" in topics and "plant/a/count" in topics:
                break
            await asyncio.sleep(0.05)
        runner._stop.set()
        await asyncio.wait_for(poll, timeout=2)

        temps = [p for (t, p) in fake.published if t == "plant/a/temp"]
        counts = [p for (t, p) in fake.published if t == "plant/a/count"]
        assert temps and temps[0] == b"23.5", temps
        assert counts and counts[0] == b"1234", counts
        print("poll loop read registers and published decoded values to MQTT  OK")
        print(f"  plant/a/temp={temps[0].decode()}  plant/a/count={counts[0].decode()}")
        print("\nPASS: Modbus register polling -> MQTT")
    finally:
        runner._stop.set()
        poll.cancel()
        with suppress(Exception):
            await poll
        sw.close()
        slave_srv.close()
        with suppress(Exception):
            await slave_srv.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
