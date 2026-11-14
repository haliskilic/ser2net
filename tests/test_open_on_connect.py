"""network.open_on_connect: the serial port is open only while a client is connected.

  - a TCP server mapping starts with the port closed (status: running, waiting); the
    first client opens it and is taken once it is open, so what it sends first reaches
    the device; the port closes port_linger_s after the last client left, and a client
    that comes back within that time finds it still open (one open, not two); one that
    connects while the port is being closed waits for it to open again and is not taken
    on the port that closes;
  - while the mapping waits, the no-data alarm does not count the silence;
  - a device that can't be opened: the client is turned away after the short wait
    (options.rfc2217_net_timeout_s), the status says why, and keeps saying it while it
    waits once nobody wants the port: the notifier reports device_lost once and
    device_back only after an open that works (not for a port closed on purpose);
  - the web console counts as a client; the hex view does not;
  - RFC 2217 works with it (the session begins once the port is open);
  - off (the default): the port is opened at start and kept;
  - not a TCP server mapping: not used, and the mapping says so; the linger is checked
    at edit time, a stored bad one reads as the default; the form has a presence marker.

Run: python3 tests/test_open_on_connect.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _device import FakeDevice, eof_within, port_of, read_for, running, until

from ser2netweb import notify
from ser2netweb.config import ConfigError, MappingConfig, port_linger
from ser2netweb.engine.bridge import MappingRunner
from ser2netweb.web.routes.forms import (
    _build_mapping_dict, _preserve_unmanaged_fields,
)


def mapping(mode="server", protocol="raw", port=0, on=True, linger=0.3, **network):
    return MappingConfig.from_dict({
        "name": "ondemand", "serial": {"port": "/dev/ttyFAKE-od"},
        "network": {"mode": mode, "protocol": protocol, "bind_ip": "127.0.0.1", "port": port,
                    "max_connections": 1 if protocol == "rfc2217" else 4,
                    "open_on_connect": on, "port_linger_s": linger, **network},
        "options": {"rfc2217_net_timeout_s": 0.6, "no_data_alarm_s": 10}})


class _Viewer:
    def feed(self, data):
        pass

    feed_traffic = feed

    def close(self):
        pass


async def _lifecycle():
    device, logs = await FakeDevice().start(), []
    try:
        async with running(mapping(), device, logs, wait_open=False) as runner:
            await asyncio.sleep(0.3)
            st = runner.status
            assert device.opens == 0 and not runner._link.is_open, "not opened at start"
            shown = st.as_dict()
            assert shown["state"] == "running" and shown["waiting"] is True, shown
            assert shown["rx_idle_s"] is None, "no silence is counted while it waits"
            assert not st.devices
            # the first client opens the port, and what it sends first reaches the device
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            w.write(b"first bytes")
            await until(lambda: bytes(device.heard) == b"first bytes")
            assert device.opens == 1 and st.waiting is False and st.bytes_dropped == 0
            assert st.as_dict()["waiting"] is False and st.devices
            device.say(b"reply")
            assert await asyncio.wait_for(r.readexactly(5), 5) == b"reply"
            # back within the linger: the same open
            w.close()
            await until(lambda: st.client_count == 0)
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: st.client_count == 1)
            await asyncio.sleep(0.5)
            assert device.opens == 1 and device.is_open, "kept open for the client that came back"
            # the last client leaves: closed after the linger, so another program can use it
            w.close()
            await until(lambda: not device.is_open)
            await until(lambda: st.waiting)
            assert st.state == "running" and not st.devices and not st.last_error
            assert any("serial port closed: no client is connected" in ln for ln in logs), logs
            # and opened again for the next one
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            w.write(b"x")
            await until(lambda: device.opens == 2 and device.heard.endswith(b"x"))
            w.close()
    finally:
        await device.close()


async def _client_while_closing():
    """A client that connects while the port is being closed after the linger (here a close
    that takes a while) is not taken on that port: it waits for the next open, and what it
    sends first arrives. With disconnect_without_device a client taken on a port that then
    closes would be disconnected."""
    device, logs = await FakeDevice().start(), []
    m = mapping(linger=0.2, disconnect_without_device=True)
    m.options.rfc2217_net_timeout_s = 3.0
    try:
        async with running(m, device, logs, wait_open=False) as runner:
            closing = asyncio.Event()
            session = runner._serial_session

            async def slow_close(reader, writer):
                try:
                    await session(reader, writer)
                except asyncio.CancelledError:
                    closing.set()
                    await asyncio.sleep(0.3)
                    raise

            runner._serial_session = slow_close
            _r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1 and device.is_open)
            w.close()
            await asyncio.wait_for(closing.wait(), 5)  # the linger is over: closing
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            w.write(b"after")
            await until(lambda: device.opens == 2 and bytes(device.heard).endswith(b"after"))
            assert runner.status.client_count == 1
            assert await read_for(r, 0.5) == [], "still connected"
            assert not any("disconnecting" in ln for ln in logs), logs
            w.close()
    finally:
        await device.close()


async def _missing_device():
    device, logs = await FakeDevice().start(), []
    device.present = False
    try:
        async with running(mapping(), device, logs, wait_open=False) as runner:
            st = runner.status
            seen = [notify.snapshot({"m": st.as_dict()})]

            def device_events():  # what the notifier says since its last look
                now = {"m": st.as_dict()}
                events = notify.status_events(seen[-1], now, {"m": "ondemand"})
                seen.append(notify.snapshot(now, seen[-1]))
                return [(e[0], e[3]) for e in events if e[0].startswith("device_")]

            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: st.state == "device-missing")
            assert st.waiting is False and st.last_error
            assert [e for e, _why in device_events()] == ["device_lost"]
            assert await eof_within(r, 5), "turned away once the short wait is over"
            w.close()
            assert st.client_count == 0 and not any("client connected" in ln for ln in logs)
            assert any("the serial port could not be opened" in ln for ln in logs), logs
            error = st.last_error
            await until(lambda: st.waiting)
            assert st.state == "device-missing" and st.last_error == error, \
                ("waiting, and still saying why the port could not be opened", st.as_dict())
            assert device_events() == [], "no device_back: the device never came back"
            # another client while it is still missing: no device_lost again
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: not st.waiting)
            assert await eof_within(r, 5)
            w.close()
            await until(lambda: st.waiting)
            assert device_events() == [] and st.state == "device-missing"
            # the device is back: the next client is served, and that is device_back
            device.present = True
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            w.write(b"ok")
            await until(lambda: bytes(device.heard) == b"ok")
            assert st.state == "running" and st.last_error == ""
            assert [e for e, _why in device_events()] == ["device_back"]
            w.close()
            await until(lambda: st.waiting and not device.is_open)
            assert st.state == "running" and device_events() == [], \
                "a port closed on purpose after it worked is no device event"
    finally:
        await device.close()


async def _console_and_rfc2217():
    device, logs = await FakeDevice().start(), []
    try:
        async with running(mapping(), device, logs, wait_open=False) as runner:
            hexview, console = _Viewer(), _Viewer()
            runner.add_traffic_monitor(hexview)
            await asyncio.sleep(0.2)
            assert device.opens == 0, "the hex view does not open the port"
            runner.add_monitor(console)
            await until(lambda: device.is_open)
            runner.discard_monitor(console)
            await until(lambda: not device.is_open)
            await until(lambda: runner.status.waiting)  # (once the close has ended)
        async with running(mapping(protocol="rfc2217"), device, logs, wait_open=False) as runner:
            assert not device.is_open
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1)
            assert device.is_open and (await asyncio.wait_for(r.read(100), 5))[:1] == b"\xff"
            w.close()
            await until(lambda: not device.is_open)
    finally:
        await device.close()


async def _off():
    device, logs = await FakeDevice().start(), []
    try:
        async with running(mapping(on=False), device, logs) as runner:
            assert device.opens == 1 and runner.status.as_dict()["waiting"] is False
            await asyncio.sleep(0.5)
            assert device.is_open, "kept open without a client, as before"
    finally:
        await device.close()


def test_runner():
    asyncio.run(_lifecycle())
    print("opened for the first client (its first bytes arrive), kept through the linger, "
          "closed after the last one, status waiting in between  OK")
    asyncio.run(_client_while_closing())
    print("a client that connects while the port closes waits for the next open  OK")
    asyncio.run(_missing_device())
    print("a device that can't be opened: the client is turned away, the status keeps why "
          "while it waits; device_lost once, device_back only once an open works  OK")
    asyncio.run(_console_and_rfc2217())
    print("the web console opens the port, the hex view does not; RFC 2217 begins once it "
          "is open  OK")
    asyncio.run(_off())
    print("off: opened at start and kept  OK")


def test_settings():
    assert MappingConfig.from_dict({}).network.open_on_connect is False
    assert not MappingRunner(mapping(on=False)).on_demand
    assert MappingRunner(mapping()).on_demand and MappingRunner(mapping()).needs_port
    for other in (mapping(mode="udp"), mapping(mode="client", remote_host="h", remote_port=1)):
        assert not MappingRunner(other).on_demand
        assert any("only while a client is connected" in t for t in other.ignored_settings())
    assert not mapping().ignored_settings()
    # text is not true (a hand-edited file): off, and an edit refuses it
    assert MappingConfig.from_dict({"network": {"open_on_connect": "yes"}}) \
        .network.open_on_connect is False
    for bad in (-1, 3601, float("inf")):
        m = mapping(port=4001, linger=bad)
        m.validate()
        assert port_linger(m.network) == 2.0, bad
        try:
            m.validate_input()
        except ConfigError:
            continue
        raise AssertionError(f"an edit took a linger of {bad}")
    mapping(port=4001, linger=0).validate_input()
    assert port_linger(mapping(linger=0).network) == 0.0
    print("settings: off by default, TCP server only (said otherwise), the linger's range "
          "at edit time, a stored bad one reads as 2 s  OK")


def test_form():
    base = {"name": "od", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}
    existing = mapping(port=4001, linger=7.5)

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.network.open_on_connect, m.network.port_linger_s

    marker = {"network_open_on_connect_shown": "1"}
    assert parsed(dict(base, id=existing.id)) == (True, 7.5), "absent: kept"
    assert parsed(dict(base, id=existing.id, **marker)) == (False, 2.0), "shown, unticked"
    assert parsed(dict(base, **marker, network_open_on_connect="on",
                       network_port_linger_s="0.5")) == (True, 0.5)
    assert parsed(base) == (False, 2.0)
    print("form: open on connect and its linger kept when the form leaves them out, set "
          "when shown  OK")


def main():
    test_settings()
    test_form()
    test_runner()
    print("\nPASS: the serial port only while a client is connected (open_on_connect)")


if __name__ == "__main__":
    main()
