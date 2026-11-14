"""Packing (options.pack_gap_ms / pack_delimiter / pack_max_bytes): the device's output is
forwarded to raw TCP / UDP clients as frames instead of as it is read.

  - Packer: a frame ends with the delimiter (kept in it, found across reads), or at the
    frame size, whichever comes first; flush() gives what is waiting;
  - a TCP raw mapping with a gap: bytes that arrive in pieces within the gap reach the
    client in one piece once the gap has passed, nothing before;
  - a delimiter: each line goes out when its end arrives; an unfinished one after
    PACK_IDLE_FLUSH_S without a byte (no gap set), so nothing is held back for good;
  - UDP: one datagram per frame;
  - stop with a frame waiting: its timer is cancelled and nothing is left for a timer to
    send after the stop;
  - off (the default, every config written without the keys): no packer at all; telnet /
    RFC 2217: not packed, and the mapping says so (ignored_settings);
  - edits refuse values out of range (validate_input); validate() does not; a stored one
    turns packing off with a log line instead of failing the start;
  - the form: its fields have a presence marker (absent: the stored values are kept); a
    delimiter of real CR / LF is shown escaped (the same bytes) and kept as it is stored by
    an unchanged save; a blank box turns it off.

Run: python3 tests/test_packing.py
"""
import asyncio
import os
import socket
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from _device import FakeDevice, port_of, read_for, running, until

from ser2netweb.config import (
    PACK_MAX_BYTES_DEFAULT, ConfigError, MappingConfig, pack_settings, poll_bytes,
)
from ser2netweb.engine import bridge
from ser2netweb.engine.bridge import MappingRunner, Packer
from ser2netweb.web.routes.forms import (
    _build_mapping_dict, _preserve_unmanaged_fields, escaped_text,
)


def mapping(mode="server", protocol="raw", port=0, **options):
    return MappingConfig.from_dict({
        "name": "pack", "serial": {"port": "/dev/ttyFAKE-pack"},
        "network": {"mode": mode, "protocol": protocol, "bind_ip": "127.0.0.1", "port": port,
                    "max_connections": 1 if protocol == "rfc2217" else 4},
        "options": options})


def test_packer():
    p = Packer(b"\r\n", 8)
    assert p.feed(b"ab\r") == [] and p.pending == 3
    assert p.feed(b"\ncd\r\nef") == [b"ab\r\n", b"cd\r\n"], "a delimiter split over two reads"
    assert p.feed(b"ghijklmn") == [b"efghijkl"], "the frame size, without a delimiter"
    assert p.flush() == b"mn" and p.pending == 0 and p.flush() == b""
    # the delimiter ends a frame only when all of it fits in the frame
    assert p.feed(b"1234567\r\n") == [b"1234567\r"] and p.flush() == b"\n"
    p = Packer(b"", 4)  # size only
    assert p.feed(b"abcdefghij") == [b"abcd", b"efgh"] and p.flush() == b"ij"
    p = Packer(b"\n", 4096)  # delimiter only: one byte at a time
    out = [f for c in b"one\ntwo\n3" for f in p.feed(bytes([c]))]
    assert out == [b"one\n", b"two\n"] and p.flush() == b"3"
    print("Packer: delimiter (across reads), frame size, both, flush  OK")


def test_settings():
    assert pack_settings(mapping().options) is None, "off by default"
    assert MappingRunner(mapping())._packer is None
    assert pack_settings(mapping(pack_gap_ms=20).options) == (0.02, b"", PACK_MAX_BYTES_DEFAULT)
    assert pack_settings(mapping(pack_delimiter=r"\r\x0a", pack_max_bytes=100).options) == \
        (0.0, b"\r\n", 100)
    for bad in ({"pack_gap_ms": -1}, {"pack_gap_ms": 60001}, {"pack_max_bytes": 65508},
                {"pack_max_bytes": -5}, {"pack_delimiter": r"\q"},
                {"pack_delimiter": "x" * 17}):
        m = mapping(port=4001, **bad)
        m.validate()  # a stored one keeps loading and saving
        try:
            m.validate_input()
        except ConfigError:
            pass
        else:
            raise AssertionError(f"an edit took {bad}")
        logs = []
        runner = MappingRunner(m, logger=logs.append)
        assert runner._packer is None and "packing is off" in runner._pack_note, bad
    mapping(port=4001, pack_gap_ms=60000, pack_max_bytes=65507,
            pack_delimiter="x" * 16).validate_input()
    # raw only
    for protocol in ("telnet", "rfc2217"):
        m = mapping(protocol=protocol, pack_gap_ms=20)
        assert MappingRunner(m)._packer is None
        assert any("raw protocol only" in t for t in m.ignored_settings()), protocol
    assert not mapping(pack_gap_ms=20).ignored_settings()
    assert MappingRunner(mapping(mode="udp", pack_gap_ms=20))._packer is not None
    print("settings: off by default, edit-time ranges, a stored bad value turns it off "
          "(logged), raw only  OK")


async def _tcp_gap():
    device, logs = await FakeDevice().start(), []
    try:
        async with running(mapping(pack_gap_ms=300), device, logs) as runner:
            assert any("packing the device's output" in ln and "300 ms" in ln for ln in logs), logs
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1)
            for piece in (b"ab", b"cd", b"ef"):
                device.say(piece)
                await asyncio.sleep(0.05)
            assert runner.status.bytes_in == 6, "read already, held for the gap"
            assert await read_for(r, 0.1) == [], "nothing before the gap has passed"
            assert await asyncio.wait_for(r.read(100), 5) == b"abcdef", "one piece"
            w.close()
    finally:
        await device.close()


async def _tcp_delimiter():
    device, logs = await FakeDevice().start(), []
    try:
        with mock.patch.object(bridge, "PACK_IDLE_FLUSH_S", 0.4):
            async with running(mapping(pack_delimiter=r"\n", closeon="bye\n"), device,
                               logs) as runner:
                r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
                await until(lambda: runner.status.client_count == 1)
                device.say(b"one\ntw")
                assert await asyncio.wait_for(r.read(100), 5) == b"one\n"
                assert await read_for(r, 0.15) == [], "the unfinished line waits"
                device.say(b"o\nthr")
                assert await asyncio.wait_for(r.read(100), 5) == b"two\n"
                # no gap set: an unfinished frame goes out after PACK_IDLE_FLUSH_S
                assert await asyncio.wait_for(r.read(100), 5) == b"thr"
                # the close-on string is looked for in what the clients get, so the frame
                # that has it is delivered before the client is closed
                device.say(b"bye\n")
                got = b"".join(await read_for(r, 5))
                assert got == b"bye\n", got
                w.close()
    finally:
        await device.close()


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self):
        self.got = []

    def datagram_received(self, data, addr):
        self.got.append(data)


async def _udp():
    device, logs = await FakeDevice().start(), []
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    transport, proto = await loop.create_datagram_endpoint(_Datagrams, sock=sock)
    try:
        with mock.patch.object(bridge, "PACK_IDLE_FLUSH_S", 0.3):
            async with running(mapping(mode="udp", pack_delimiter=r"\r\n", pack_max_bytes=10),
                               device, logs) as runner:
                assert any("one datagram per frame" in ln for ln in logs), logs
                port = runner._udp_transport.get_extra_info("sockname")[1]
                transport.sendto(b"hi", ("127.0.0.1", port))  # becomes the peer
                await until(lambda: bytes(device.heard) == b"hi")
                device.say(b"$GPGGA\r\n$GPRMC\r\n0123456789ABC")
                await until(lambda: len(proto.got) >= 4)
                assert proto.got == [b"$GPGGA\r\n", b"$GPRMC\r\n", b"0123456789", b"ABC"], \
                    proto.got
    finally:
        transport.close()
        await device.close()


async def _stop_mid_frame():
    device, logs = await FakeDevice().start(), []
    try:
        async with running(mapping(pack_gap_ms=5000), device, logs) as runner:
            r, w = await asyncio.open_connection("127.0.0.1", port_of(runner))
            await until(lambda: runner.status.client_count == 1)
            device.say(b"half a frame")
            await until(lambda: runner.status.bytes_in == 12)
            assert runner._pack_timer is not None and runner._packer.pending == 12
        assert runner._pack_timer is None and runner._packer.pending == 0
        loop = asyncio.get_running_loop()
        flushes = [h for h in getattr(loop, "_scheduled", [])
                   if not h.cancelled() and "_pack_flush" in repr(h)]
        assert not flushes, flushes
        w.close()
    finally:
        await device.close()


def test_runner():
    asyncio.run(_tcp_gap())
    print("TCP raw, gap: pieces within the gap arrive as one frame, nothing before it  OK")
    asyncio.run(_tcp_delimiter())
    print("TCP raw, delimiter: a line per frame, an unfinished one after the idle flush, "
          "close-on after its frame  OK")
    asyncio.run(_udp())
    print("UDP: one datagram per frame (delimiter, frame size, the rest)  OK")
    asyncio.run(_stop_mid_frame())
    print("stop with a frame waiting: no packing timer is left  OK")


def test_form():
    base = {"name": "pack", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}
    existing = mapping(pack_gap_ms=20, pack_delimiter=r"\n", pack_max_bytes=512)

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        o = m.options
        return o.pack_gap_ms, o.pack_delimiter, o.pack_max_bytes

    assert parsed(dict(base, id=existing.id)) == (20, r"\n", 512), "absent: kept"
    assert parsed(dict(base, id=existing.id, opt_pack_gap_ms="")) == (0, "", 0), "blank: off"
    assert parsed(dict(base, opt_pack_gap_ms="50", opt_pack_delimiter=r"\r\n",
                       opt_pack_max_bytes="1400")) == (50, r"\r\n", 1400)
    assert parsed(base) == (0, "", 0)
    # a delimiter stored as real CR LF (REST API, an import, by hand): its box shows it
    # escaped (\r\n, the same bytes), and an unchanged save keeps the stored value exactly
    existing = mapping(pack_gap_ms=0, pack_delimiter="\r\n", pack_max_bytes=0)
    assert escaped_text(existing.options.pack_delimiter) == r"\r\n"
    shown = dict(base, id=existing.id, opt_pack_gap_ms="", opt_pack_delimiter=r"\r\n",
                 opt_pack_max_bytes="")
    assert parsed(shown) == (0, "\r\n", 0), "an unchanged save keeps it as it is stored"
    assert parsed(dict(shown, opt_pack_delimiter="")) == (0, "", 0), "a blank box: off"
    assert parsed(dict(shown, opt_pack_delimiter=";")) == (0, ";", 0), "a changed one is taken"
    for stored in ("\r\n", r"\r\n", "a\0b\x7f\tc", "ç\x01;", ""):
        assert poll_bytes(escaped_text(stored)) == poll_bytes(stored), stored
    print("form: the packing fields are kept when the form leaves them out, set when "
          "shown; a delimiter of real CR LF is shown escaped and kept as it is by a save  OK")


def main():
    test_packer()
    test_settings()
    test_form()
    test_runner()
    print("\nPASS: packing (pack_gap_ms, pack_delimiter, pack_max_bytes)")


if __name__ == "__main__":
    main()
