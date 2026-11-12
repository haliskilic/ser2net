"""Traffic counters count what reached the device, once (no socat, no hardware).

RunnerStatus.bytes_in / bytes_out are what the dashboard, /api/v1/status, /metrics and the
cluster view show. They are counted at the serial device:
  - bytes_in once per read from the device, however many clients get it (or none: MQTT,
    the console and a trace only), before any telnet IAC doubling;
  - bytes_out when the open port takes the data (serial_write, shared by TCP clients, UDP
    datagrams and the browser console); data that finds the port closed is counted in
    bytes_dropped instead, and logged (rate-limited);
  - a serial<->serial bridge counts port A -> B as in and B -> A as out (as the live view
    shows them) and reports both of its ports as in use.

Run: python3 tests/test_traffic_counters.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import AppConfig, MappingConfig
from ser2netweb.engine import bridge
from ser2netweb.engine.bridge import FROM_DEVICE, TO_DEVICE, MappingRunner, _LogEvery
from ser2netweb.engine.supervisor import Supervisor


class FakePort:
    """Stand-in for the serial StreamWriter."""

    def __init__(self, closing=False):
        self.written = bytearray()
        self.closing = closing

    def write(self, data):
        self.written += data

    def is_closing(self):
        return self.closing

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


class Sink:
    """A client as the serial read loop sees it."""

    def __init__(self):
        self.got = bytearray()

    def feed_from_serial(self, data):
        self.got += data


class Traffic:
    def __init__(self):
        self.items = []

    def feed_traffic(self, direction, data):
        self.items.append((direction, bytes(data)))


def net_mapping(**network):
    return MappingConfig.from_dict({
        "name": "cnt", "serial": {"port": "/dev/ttyX"},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": 48500, **network}})


def device_reader(*chunks):
    """A StreamReader that returns each chunk once, then EOF (the device went away)."""
    reader = asyncio.StreamReader()
    for c in chunks:
        reader.feed_data(c)
    reader.feed_eof()
    return reader


async def read_all(runner, reader):
    try:
        await runner._serial_read_loop(reader)
    except ConnectionError:
        pass  # EOF: what the loop raises when the device goes away


async def test_bytes_in_once_at_the_device():
    runner = MappingRunner(net_mapping(max_connections=3))
    sinks = [Sink() for _ in range(3)]
    runner._clients.update(sinks)
    await read_all(runner, device_reader(b"0123456789"))
    assert all(bytes(s.got) == b"0123456789" for s in sinks)
    assert runner.status.bytes_in == 10, runner.status.bytes_in  # not 3 x 10

    # nobody connected (the data only goes to MQTT / the console / a trace): still counted
    alone = MappingRunner(net_mapping())
    await read_all(alone, device_reader(b"abc", b"de"))
    assert alone.status.bytes_in == 5, alone.status.bytes_in

    # telnet: what the device sent, not the doubled IAC a client gets
    telnet = MappingRunner(net_mapping(protocol="telnet"))
    await read_all(telnet, device_reader(b"x\xffy"))
    assert telnet.status.bytes_in == 3, telnet.status.bytes_in
    print("bytes_in: counted once per device read (3 clients, none, telnet)  OK")


async def test_bytes_out_only_when_written():
    logs = []
    runner = MappingRunner(net_mapping(), logger=logs.append)
    traffic = Traffic()
    runner.add_traffic_monitor(traffic)

    # the port is not open (unplugged / reconnecting): dropped, counted, logged once
    assert await runner.serial_write(b"lost") is False
    assert await runner.serial_write(b"gone") is False
    st = runner.status
    assert (st.bytes_out, st.bytes_dropped) == (0, 8), (st.bytes_out, st.bytes_dropped)
    assert traffic.items == [], "dropped data must not show as sent in the live view"
    dropped = [m for m in logs if "dropped" in m]
    assert len(dropped) == 1 and "serial port is not open" in dropped[0], logs

    # the port is closing (it failed, its session has not ended yet): dropped too
    runner._link.writer = FakePort(closing=True)
    assert await runner.serial_write(b"xy") is False
    assert (st.bytes_out, st.bytes_dropped) == (0, 10)

    # open: handed to the port and counted once
    port = runner._link.writer = FakePort()
    assert await runner.serial_write(b"hello") is True
    assert bytes(port.written) == b"hello"
    assert (st.bytes_out, st.bytes_dropped) == (5, 10)
    assert traffic.items == [(TO_DEVICE, b"hello")]
    assert st.as_dict()["bytes_dropped"] == 10
    print("bytes_out: only what the open port took; the rest is bytes_dropped (logged)  OK")


async def test_serial_bridge_directions():
    m = MappingConfig.from_dict({"name": "sb", "kind": "serialbridge",
                                 "serial": {"port": "/dev/ttyA"},
                                 "serial_b": {"port": "/dev/ttyB"}})
    runner = MappingRunner(m)
    traffic = Traffic()
    runner.add_traffic_monitor(traffic)
    to_b, to_a = FakePort(), FakePort()
    for reader, writer, tag in ((device_reader(b"AAAA"), to_b, "A>B"),
                                (device_reader(b"BB"), to_a, "B>A")):
        try:
            await runner._pump_serial_pair(reader, writer, tag)
        except ConnectionError:
            pass
    assert (bytes(to_b.written), bytes(to_a.written)) == (b"AAAA", b"BB")
    assert (runner.status.bytes_in, runner.status.bytes_out) == (4, 2), runner.status
    assert traffic.items == [(FROM_DEVICE, b"AAAA"), (TO_DEVICE, b"BB")]
    print("serial<->serial: A -> B counts as in, B -> A as out (as the live view)  OK")


def test_devices_in_use():
    sup = Supervisor()
    single = MappingRunner(net_mapping())
    single._link.attach(None, FakePort(), None, "/dev/ttyUSB0")  # what SerialLink.run does
    pair = MappingRunner(MappingConfig.from_dict({
        "name": "sb", "kind": "serialbridge",
        "serial": {"port": "/dev/ttyA"}, "serial_b": {"port": "/dev/ttyB"}}))
    pair.status.state, pair.status.device = "running", "/dev/ttyA <-> /dev/ttyB"
    pair.status.devices = ("/dev/ttyA", "/dev/ttyB")
    stopped = MappingRunner(net_mapping())
    stopped.status.devices, stopped.status.state = ("/dev/ttyS9",), "reconnecting"
    sup._runners = {"a": single, "b": pair, "c": stopped}
    assert sup.devices_in_use() == {"/dev/ttyUSB0", "/dev/ttyA", "/dev/ttyB"}
    assert pair.status.as_dict()["device"] == "/dev/ttyA <-> /dev/ttyB", "unchanged for the UI"
    print("devices in use: both ports of a serial<->serial bridge, not a stopped one  OK")


def test_log_every():
    lines = []
    clock = [100.0]
    real = bridge.time.monotonic
    bridge.time.monotonic = lambda: clock[0]
    try:
        say = _LogEvery(lines.append, interval=60)
        say("one")
        say("two")
        say("three")
        clock[0] += 61
        say("four")
    finally:
        bridge.time.monotonic = real
    assert lines == ["one", "four (2 more since the last such line)"], lines
    print("rate-limited log line: one per interval, says how many were held back  OK")


async def test_metrics_line():
    from ser2netweb.web.routes import system

    class State:
        config = AppConfig.from_dict({"mappings": [{"id": "m1", "name": "plc", "serial":
                                                    {"port": "/dev/ttyX"}}]})

        class supervisor:
            @staticmethod
            def all_status():
                return {"m1": {"state": "running", "bytes_in": 7, "bytes_out": 3,
                               "bytes_dropped": 12}}

    class Ctx:
        state = State
        render = None

    metrics = next(r.endpoint for r in system.routes(Ctx) if r.path == "/metrics")
    body = (await metrics(None)).body.decode()
    lbl = 'mapping="plc",id="m1"'
    for line in (f"ser2net_mapping_bytes_in_total{{{lbl}}} 7",
                 f"ser2net_mapping_bytes_out_total{{{lbl}}} 3",
                 f"ser2net_mapping_bytes_dropped_total{{{lbl}}} 12"):
        assert line in body.splitlines(), body
    print("/metrics: bytes_in/out keep their names, bytes_dropped added  OK")


async def main():
    await test_bytes_in_once_at_the_device()
    await test_bytes_out_only_when_written()
    await test_serial_bridge_directions()
    test_devices_in_use()
    test_log_every()
    await test_metrics_line()
    print("\nPASS: traffic counters")


if __name__ == "__main__":
    asyncio.run(main())
