"""network.writer_client_ips: only clients from these addresses send to the device; the
others only listen.

  - TCP server (raw): a writer's bytes reach the device; a listener's are dropped and
    counted (bytes_dropped), and the log says so once per connection; both get the
    device's output; the status lists the listener as listen_only;
  - telnet: a listener's negotiation is still answered, its data and its BREAK are not
    passed on;
  - UDP: a datagram from an address that is no writer is not written and does not make
    it the peer (the device's output stays with the writer that sent last); the log names
    the address once;
  - off (the default, and every config written without the key): everyone writes;
  - with read_only nobody writes; a connect-out mapping has one peer, which writes;
  - with the allow-list and priority clients: they decide who gets a connection, the
    writer list who writes; at max_connections a listener (also a priority one) never
    kicks a writer out, only another listener;
  - entries are read as the allow-list's: a list with no usable entry lets nobody write;
    edits refuse a bad entry, validate() does not;
  - the form field is its own presence marker.

Run: python3 tests/test_writer_clients.py
"""
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _device import FakeDevice, eof_within, port_of, read_for, running, until

from ser2netweb.config import ConfigError, MappingConfig
from ser2netweb.engine.access import AccessPolicy
from ser2netweb.engine.bridge import MappingRunner, _UdpBridge
from ser2netweb.web.routes.forms import (
    _build_mapping_dict, _preserve_unmanaged_fields,
)


def mapping(mode="server", protocol="raw", port=0, **network):
    return MappingConfig.from_dict({
        "name": "writers", "serial": {"port": "/dev/ttyFAKE-w"},
        "network": {"mode": mode, "protocol": protocol, "bind_ip": "127.0.0.1", "port": port,
                    "max_connections": 4, **network}})


async def connect_from(runner, source):
    """A client of the mapping whose source address is ``source`` (127.0.0.x)."""
    r, w = await asyncio.open_connection("127.0.0.1", port_of(runner),
                                         local_addr=(source, 0))
    return r, w


def test_policy():
    p = AccessPolicy(mapping().network)
    assert not p.writers_only and p.may_write("203.0.113.9"), "off: everyone writes"
    p = AccessPolicy(mapping(writer_client_ips=["10.0.0.7", "10.1.0.0/16",
                                                "::ffff:192.168.1.0/120"]).network)
    assert p.writers_only
    assert p.may_write("10.0.0.7") and p.may_write("10.1.2.3") and p.may_write("192.168.1.9")
    assert p.may_write("::ffff:10.0.0.7"), "an IPv4-mapped client is its IPv4 address"
    assert not p.may_write("10.0.0.8") and not p.may_write("fd00::1")
    # a hand-edited list with no usable entry: nobody writes (never everyone)
    stored = mapping(port=4001, writer_client_ips=["not-an-ip"])
    stored.validate()
    assert not AccessPolicy(stored.network).may_write("10.0.0.7")
    assert any("No entry is usable, so no client may send" in t for t in stored.ignored_settings())
    try:
        stored.validate_input()
    except ConfigError as e:
        assert "not-an-ip" in str(e)
    else:
        raise AssertionError("an edit took a writer entry that is no address")
    mapping(port=4001, writer_client_ips=["10.0.0.0/8", "fd00::/8"]).validate_input()
    # a connect-out link has one peer: it writes, and the mapping says the list is unused
    out = mapping(mode="client", remote_host="192.0.2.1", remote_port=4001,
                  writer_client_ips=["10.0.0.7"])
    assert AccessPolicy(out.network).may_write("192.0.2.1")
    assert any("Writer client IPs are set" in t for t in out.ignored_settings())
    print("policy: off = everyone, entries as the allow-list's, none usable = nobody, "
          "edit-time check, connect-out unaffected  OK")


async def _tcp():
    device, logs = await FakeDevice().start(), []
    try:
        m = mapping(writer_client_ips=["127.0.0.2"])
        async with running(m, device, logs) as runner:
            assert any("only the writer client IPs may send" in ln for ln in logs), logs
            wr, ww = await connect_from(runner, "127.0.0.2")
            lr, lw = await connect_from(runner, "127.0.0.3")
            await until(lambda: runner.status.client_count == 2)
            shown = {c["peer"].split(":")[0]: c["listen_only"]
                     for c in runner.status.as_dict()["clients"]}
            assert shown == {"127.0.0.2": False, "127.0.0.3": True}, shown
            assert any("127.0.0.3" in ln and "[listen-only]" in ln for ln in logs), logs
            lw.write(b"IGNORED")
            await lw.drain()
            await until(lambda: runner.status.bytes_dropped == 7)
            lw.write(b"again")
            await lw.drain()
            await until(lambda: runner.status.bytes_dropped == 12)
            ww.write(b"SET 1\r")
            await until(lambda: bytes(device.heard) == b"SET 1\r")
            assert runner.status.bytes_out == 6
            said = [ln for ln in logs if "only listens" in ln]
            assert len(said) == 1 and "127.0.0.3" in said[0], logs
            device.say(b"OK\r")  # both get the device's output
            assert await asyncio.wait_for(wr.readexactly(3), 5) == b"OK\r"
            assert await asyncio.wait_for(lr.readexactly(3), 5) == b"OK\r"
            ww.close()
            lw.close()
    finally:
        await device.close()


async def _telnet():
    device, logs = await FakeDevice().start(), []
    try:
        m = mapping(protocol="telnet", writer_client_ips=["127.0.0.2"])
        async with running(m, device, logs) as runner:
            lr, lw = await connect_from(runner, "127.0.0.3")
            await until(lambda: runner.status.client_count == 1)
            assert b"\xff" in b"".join(await read_for(lr, 0.3)), "the negotiation"
            lw.write(b"hello\xff\xf3")  # data, then IAC BRK
            await lw.drain()
            await until(lambda: runner.status.bytes_dropped == 5)
            await asyncio.sleep(0.1)
            assert not device.heard and runner.status.bytes_out == 0
            assert not any("serial BREAK sent" in ln for ln in logs), logs
            lw.close()
    finally:
        await device.close()


async def _read_only_and_off():
    device, logs = await FakeDevice().start(), []
    try:
        async with running(mapping(), device, logs) as runner:  # off: everyone writes
            _r, w = await connect_from(runner, "127.0.0.3")
            w.write(b"abc")
            await until(lambda: bytes(device.heard) == b"abc")
            assert runner.status.as_dict()["clients"][0]["listen_only"] is False
            w.close()
        device.heard.clear()
        m = mapping(writer_client_ips=["127.0.0.2"], read_only=True)
        async with running(m, device, logs) as runner:
            _r, w = await connect_from(runner, "127.0.0.2")
            await until(lambda: runner.status.client_count == 1)
            w.write(b"abc")
            await w.drain()
            await asyncio.sleep(0.2)
            assert not device.heard, "read-only: a writer does not write either"
            w.close()
    finally:
        await device.close()


async def _priority_and_allow_list():
    device, logs = await FakeDevice().start(), []
    try:
        m = mapping(writer_client_ips=["127.0.0.2"], priority_client_ips=["127.0.0.3"],
                    allowed_client_ips=["127.0.0.2", "127.0.0.3", "127.0.0.5"],
                    max_connections=2, kick_old_user=True)
        async with running(m, device, logs) as runner:
            wr, ww = await connect_from(runner, "127.0.0.2")
            await until(lambda: runner.status.client_count == 1)
            _ar, aw = await connect_from(runner, "127.0.0.5")  # a listener: the 2nd slot
            await until(lambda: runner.status.client_count == 2)
            lr, lw = await connect_from(runner, "127.0.0.3")  # priority, not a writer
            assert await asyncio.wait_for(_ar.read(100), 5) == b"", "the listener made room"
            await until(lambda: sorted(c["listen_only"] for c in
                                       runner.status.as_dict()["clients"]) == [False, True])
            lw.write(b"nope")
            await until(lambda: runner.status.bytes_dropped == 4)
            assert not device.heard, "a priority client that is no writer only listens"
            # the slots are the writer's and a priority listener's: another listener
            # (kick_old_user) takes neither, and is refused
            _r2, w2 = await connect_from(runner, "127.0.0.5")
            assert await eof_within(_r2, 5), "refused"
            assert any("127.0.0.5" in ln and "takes no writer's place" in ln for ln in logs), logs
            ww.write(b"w")
            await until(lambda: bytes(device.heard) == b"w")
            w2.close()
            aw.close()
            # the allow-list still comes first: a writer address outside it is not let in
            _r, w = await connect_from(runner, "127.0.0.4")
            assert await asyncio.wait_for(_r.read(100), 5) == b""
            assert any("127.0.0.4" in ln and "not in allowed list" in ln for ln in logs), logs
            for writer in (ww, lw, w):
                writer.close()
    finally:
        await device.close()


class _FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def is_closing(self):
        return False


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def get_extra_info(self, name, default=None):
        return default

    def sendto(self, data, addr):
        self.sent.append((data, addr))


async def _udp():
    logs = []
    m = mapping(mode="udp", writer_client_ips=["10.0.0.7"])
    runner = MappingRunner(m, logger=logs.append)
    runner._link.writer = _FakeWriter()
    bridge = _UdpBridge(runner)
    transport = _FakeTransport()
    bridge.connection_made(transport)
    task = asyncio.create_task(bridge.run())
    try:
        def output():
            transport.sent.clear()
            for c in list(runner._clients):
                c.feed_from_serial(b"out")
            return transport.sent

        bridge.datagram_received(b"from-a-listener", ("10.0.0.8", 5000))
        bridge.datagram_received(b"more", ("10.0.0.8", 5000))
        await asyncio.sleep(0.05)
        assert not runner._link.writer.data and runner.status.bytes_dropped == 19
        said = [ln for ln in logs if "not written to the device" in ln]
        assert len(said) == 1 and "10.0.0.8" in said[0], logs
        assert output() == [] and bridge.peer is None, "a listener is not made the peer"
        bridge.datagram_received(b"from-the-writer", ("10.0.0.7", 5001))
        await asyncio.sleep(0.05)
        assert bytes(runner._link.writer.data) == b"from-the-writer"
        assert runner.status.bytes_dropped == 19
        assert output() == [(b"out", ("10.0.0.7", 5001))]
        # a listener that sends now does not take the output away from the writer
        bridge.datagram_received(b"mine", ("10.0.0.8", 5000))
        await asyncio.sleep(0.05)
        assert output() == [(b"out", ("10.0.0.7", 5001))], "the writer keeps the output"
        assert runner.status.bytes_dropped == 23
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _loopback_aliases() -> bool:
    """127.0.0.2 and .3 can be a connection's source (not on macOS, whose loopback
    interface has 127.0.0.1 alone)."""
    for ip in ("127.0.0.2", "127.0.0.3"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((ip, 0))
            except OSError:
                return False
    return True


def test_runner():
    asyncio.run(_udp())
    print("UDP: a non-writer becomes the peer but its datagrams are not written (address "
          "named once)  OK")
    if not _loopback_aliases():
        print("SKIP the TCP parts: 127.0.0.2 / 127.0.0.3 are not addresses of this host")
        return
    asyncio.run(_tcp())
    print("TCP raw: the writer writes, the listener's bytes are dropped and counted (said "
          "once), both get the output, the status marks the listener  OK")
    asyncio.run(_telnet())
    print("telnet: a listener's data and BREAK are not passed on  OK")
    asyncio.run(_read_only_and_off())
    print("off: everyone writes; with read-only nobody does  OK")
    asyncio.run(_priority_and_allow_list())
    print("the allow-list and priority decide who connects, the writer list who writes  OK")


def test_form():
    base = {"name": "w", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}
    existing = mapping(port=4001, writer_client_ips=["10.0.0.7"])

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.network.writer_client_ips

    assert parsed(dict(base, id=existing.id)) == ["10.0.0.7"], "absent: kept"
    assert parsed(dict(base, id=existing.id, network_writer_client_ips="")) == []
    assert parsed(dict(base, network_writer_client_ips="10.0.0.1, 10.2.0.0/16")) == \
        ["10.0.0.1", "10.2.0.0/16"]
    assert parsed(base) == []
    print("form: writer client IPs kept when the form leaves them out, set when shown  OK")


def main():
    test_policy()
    test_form()
    test_runner()
    print("\nPASS: one writer and read-only listeners (writer_client_ips)")


if __name__ == "__main__":
    main()
