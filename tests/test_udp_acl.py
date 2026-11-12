"""UDP mappings: access control, the serial write queue and where the device's output goes.

  - ``allowed_client_ips`` is checked for every datagram: one from outside the list is
    dropped (no serial write, no peer change), refusals are logged at most once a minute;
  - datagrams wait for the serial port in a queue of at most UDP_BACKLOG_MAX bytes, written
    in one batch per drain() (a flood can't grow memory or the number of buffered writes);
    a datagram that finds the queue full, or the port closed, is dropped and counted;
  - without a reply address the device's output goes to the last allowed sender (as
    before); a change of sender is logged (rate-limited) and restarts its connection time;
    with ``udp_peer_timeout_s`` a quiet sender is forgotten;
  - with a reply address (``udp_peer_host`` / ``udp_peer_port``) the output always goes
    there, whoever sends; edits refuse a malformed one, a stored one fails the start.

Pure stdlib + the engine internals (fake serial port and socket), so it runs identically on
Windows and Linux (no socat / no hardware).

Run: python3 tests/test_udp_acl.py
"""
import asyncio
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import ConfigError, MappingConfig
from app.engine import bridge as bridge_mod
from app.engine.bridge import UDP_BACKLOG_MAX, MappingRunner, _UdpBridge, _UdpPeer


class _FakeWriter:
    """Stand-in for the serial StreamWriter: records each write; drain() waits while
    ``hold`` is set (a port slower than the sender)."""

    def __init__(self):
        self.written = bytearray()
        self.writes = []
        self.hold = None  # an asyncio.Event to wait on in drain(), or None

    def write(self, data):
        self.written.extend(data)
        self.writes.append(bytes(data))

    def is_closing(self):
        return False

    async def drain(self):
        if self.hold is not None:
            await self.hold.wait()


class _FakeSocket:
    def __init__(self, family):
        self.family = family


class _FakeTransport:
    """Stand-in for the UDP socket's transport: records what is sent where."""

    def __init__(self, family=socket.AF_INET):
        self.sent = []
        self._sock = _FakeSocket(family)

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))

    def get_extra_info(self, name, default=None):
        return self._sock if name == "socket" else default


def _make_runner(*, allowed=(), read_only=False, logs=None, **network):
    m = MappingConfig.from_dict({
        "name": "udp-acl", "enabled": True,
        "serial": {"port": "/dev/null", "baudrate": 115200},
        "network": {
            "mode": "udp", "protocol": "raw",
            "bind_ip": "127.0.0.1", "port": 48030,
            "allowed_client_ips": list(allowed), "read_only": read_only, **network,
        },
    })
    runner = MappingRunner(m, logger=logs.append if logs is not None else None)
    runner._link.writer = _FakeWriter()
    return runner


class _Udp:
    """A runner's _UdpBridge with its writer task running (what start() sets up)."""

    def __init__(self, runner, family=socket.AF_INET):
        self.runner = runner
        self.bridge = _UdpBridge(runner)
        self.transport = _FakeTransport(family)
        self.bridge.connection_made(self.transport)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.bridge.run())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    async def send(self, data, addr):
        self.bridge.datagram_received(data, addr)
        await settle()

    def device_says(self, data):
        """Bytes from the device, as the serial read loop hands them to the clients."""
        for c in list(self.runner._clients):
            c.feed_from_serial(data)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _udp_peer(runner):
    peers = [c for c in runner._clients if isinstance(c, _UdpPeer)]
    assert len(peers) <= 1, peers
    return peers[0] if peers else None


async def test_disallowed_source_is_dropped():
    logs = []
    runner = _make_runner(allowed=["10.0.0.0/24"], logs=logs)
    async with _Udp(runner) as udp:
        for _ in range(3):
            await udp.send(b"attack", ("203.0.113.7", 5000))   # outside allow-list
    assert bytes(runner._link.writer.written) == b"", "disallowed datagram reached the serial port"
    assert _udp_peer(runner) is None, "disallowed sender was registered as the UDP peer"
    refused = [m for m in logs if "rejected datagram from 203.0.113.7:5000" in m]
    assert len(refused) == 1, logs  # logged, but not once per datagram
    print("disallowed UDP source dropped (no serial write, no peer), logged once  OK")


async def test_allowed_source_passes():
    runner = _make_runner(allowed=["10.0.0.0/24"])
    async with _Udp(runner) as udp:
        await udp.send(b"hello", ("10.0.0.5", 5000))           # inside allow-list
    assert bytes(runner._link.writer.written) == b"hello", "allowed datagram did not reach serial"
    peer = _udp_peer(runner)
    assert peer is not None and peer.addr == ("10.0.0.5", 5000)
    assert runner.status.bytes_out == 5
    print("allowed UDP source accepted (serial write + peer)  OK")


async def test_spoof_cannot_steal_existing_peer():
    runner = _make_runner(allowed=["10.0.0.0/24"])
    async with _Udp(runner) as udp:
        await udp.send(b"hi", ("10.0.0.5", 5000))              # legit peer
        await udp.send(b"steal", ("203.0.113.7", 6000))        # spoof attempt
        udp.device_says(b"reply")
    peer = _udp_peer(runner)
    assert peer is not None and peer.addr == ("10.0.0.5", 5000), "spoofed source hijacked the peer"
    assert udp.transport.sent == [(b"reply", ("10.0.0.5", 5000))]
    print("source outside the allow-list cannot replace the registered peer  OK")


async def test_read_only_blocks_serial_write_but_keeps_peer():
    runner = _make_runner(allowed=["10.0.0.0/24"], read_only=True)
    async with _Udp(runner) as udp:
        await udp.send(b"data", ("10.0.0.5", 5000))            # allowed, but read-only
    assert bytes(runner._link.writer.written) == b"", "read-only mapping wrote client data to serial"
    assert _udp_peer(runner) is not None, "read-only peer not tracked (would get no serial output)"
    assert (runner.status.bytes_out, runner.status.bytes_dropped) == (0, 0)
    print("read-only UDP: peer tracked for output, but no client->serial write  OK")


async def test_empty_allowlist_allows_any():
    runner = _make_runner(allowed=[])                           # blank = any (matches TCP semantics)
    async with _Udp(runner) as udp:
        await udp.send(b"x", ("203.0.113.7", 5000))
    assert bytes(runner._link.writer.written) == b"x", "empty allow-list should accept any source"
    print("empty allow-list accepts any source (documented 'any' semantics)  OK")


async def test_flood_is_bounded():
    """A sender faster than the port: the queue never holds more than UDP_BACKLOG_MAX, the
    rest is dropped and counted, and the port gets one write per batch, not per datagram."""
    logs = []
    runner = _make_runner(logs=logs)
    port = runner._link.writer
    port.hold = asyncio.Event()  # the port does not drain until the flood is over
    sender = ("10.0.0.5", 5000)
    sent = 0
    async with _Udp(runner) as udp:
        await udp.send(b"\x00" * 1000, sender)  # written at once; its drain() waits
        sent += 1000
        for i in range(10_000):  # 10 MB, as fast as it comes
            udp.bridge.datagram_received(bytes([i % 256]) * 1000, sender)
            sent += 1000
            assert udp.bridge._queued <= UDP_BACKLOG_MAX
        queued = udp.bridge._queued
        assert len(udp.bridge._queue) <= UDP_BACKLOG_MAX // 1000
        st = runner.status
        assert st.bytes_dropped == sent - 1000 - (queued - 1000), (st.bytes_dropped, queued)
        assert len(port.writes) == 1, "nothing more is written while the port is busy"
        port.hold.set()  # the port catches up: what was queued goes in ONE write
        await settle()
    assert len(port.writes) == 2 and len(port.writes[1]) == queued - 1000, \
        [len(w) for w in port.writes]
    assert st.bytes_out == len(port.written) and st.bytes_out + st.bytes_dropped == sent
    assert udp.bridge._queued == 0 and not udp.bridge._queue
    drops = [m for m in logs if "cannot keep up with the datagrams" in m]
    assert len(drops) == 1, logs  # one line, not one per dropped datagram
    print(f"flood: queue capped at {UDP_BACKLOG_MAX} bytes, {st.bytes_dropped} bytes dropped "
          "and counted, one write per batch, one log line  OK")

    # tiny datagrams cost one list append each, and still one write per batch
    runner = _make_runner()
    port = runner._link.writer
    port.hold = asyncio.Event()
    async with _Udp(runner) as udp:
        await udp.send(b"a", sender)
        for _ in range(200_000):
            udp.bridge.datagram_received(b"b", sender)
        assert udp.bridge._queued == UDP_BACKLOG_MAX
        port.hold.set()
        await settle()
    assert [len(w) for w in port.writes] == [1, UDP_BACKLOG_MAX - 1]
    assert runner.status.bytes_dropped == 200_000 - (UDP_BACKLOG_MAX - 1)
    print("flood of 1-byte datagrams: bounded, written as one batch  OK")


async def test_port_closed_drops():
    runner = _make_runner()
    runner._link.writer = None  # unplugged / reconnecting
    async with _Udp(runner) as udp:
        await udp.send(b"lost", ("10.0.0.5", 5000))
        assert not udp.bridge._queue, "nothing waits for a port that is not open"
    assert (runner.status.bytes_out, runner.status.bytes_dropped) == (0, 4)
    assert _udp_peer(runner) is not None, "the sender still gets the output once it is back"
    print("port not open: datagram dropped and counted, not queued  OK")


async def test_last_sender_gets_the_output():
    """Without a reply address the output follows the last allowed sender (unchanged
    default). A change is logged (rate-limited) and the new peer gets its own time."""
    logs = []
    runner = _make_runner(allowed=["10.0.0.0/24"], logs=logs)
    a, b = ("10.0.0.5", 5000), ("10.0.0.6", 5001)
    async with _Udp(runner) as udp:
        await udp.send(b"", a)          # an empty datagram is enough to be the peer
        first = _udp_peer(runner)
        udp.device_says(b"one")
        time.sleep(0.05)                # more than time.time()'s 15.6 ms steps (Windows)
        await udp.send(b"q", b)
        udp.device_says(b"two")
        for _ in range(3):              # a and b take turns
            await udp.send(b"q", a)
            await udp.send(b"q", b)
    peer = _udp_peer(runner)
    assert udp.transport.sent == [(b"one", a), (b"two", b)], udp.transport.sent
    assert peer.addr == b and peer.connected_at > first.connected_at, "connection time restarts"
    assert [c["peer"] for c in runner.status.clients] == ["10.0.0.6:5001"]
    assert sum("udp peer 10.0.0.5:5000" in m for m in logs) == 1, logs
    changes = [m for m in logs if "udp peer is now" in m]
    assert changes == ["udp peer is now 10.0.0.6:5001 (was 10.0.0.5:5000)"], changes
    print("last sender gets the output; a change is logged once a minute, its time restarts  OK")


async def test_reply_address():
    """With a reply address the output always goes there; other allowed senders still
    reach the device but never get the output."""
    logs = []
    runner = _make_runner(allowed=["10.0.0.0/24"], logs=logs,
                          udp_peer_host="10.0.0.9", udp_peer_port=4001,
                          udp_peer_timeout_s=1)  # a timeout does not apply to it
    async with _Udp(runner) as udp:
        assert [c["peer"] for c in runner.status.clients] == ["10.0.0.9:4001"], "listed at start"
        udp.device_says(b"boot")        # before anyone sent anything
        await udp.send(b"cmd1", ("10.0.0.5", 5000))
        await udp.send(b"cmd2", ("10.0.0.6", 5001))
        udp.device_says(b"answer")
    assert bytes(runner._link.writer.written) == b"cmd1cmd2"
    assert udp.transport.sent == [(b"boot", ("10.0.0.9", 4001)), (b"answer", ("10.0.0.9", 4001))]
    assert [c["peer"] for c in runner.status.clients] == ["10.0.0.9:4001"]
    assert not [m for m in logs if "udp peer" in m], logs
    assert udp.bridge.describe() == "device output to 10.0.0.9:4001"

    # an IPv4 reply address on a socket for both families (one IPv6 socket): sent v4-mapped
    runner = _make_runner(bind_ip="::", dual_stack=True, udp_peer_host="10.0.0.9",
                          udp_peer_port=4001)
    async with _Udp(runner, family=socket.AF_INET6) as udp:
        udp.device_says(b"x")
    assert udp.transport.sent == [(b"x", ("::ffff:10.0.0.9", 4001))], udp.transport.sent
    assert [c["peer"] for c in runner.status.clients] == ["10.0.0.9:4001"]
    print("reply address: the output always goes there, whoever sends  OK")


async def test_peer_timeout():
    logs = []
    runner = _make_runner(logs=logs, udp_peer_timeout_s=30)
    clock = [1000.0]
    real = bridge_mod.time.monotonic
    bridge_mod.time.monotonic = lambda: clock[0]
    try:
        async with _Udp(runner) as udp:
            await udp.send(b"hi", ("10.0.0.5", 5000))
            clock[0] += 20
            udp.device_says(b"a")       # 20 s quiet: still the peer
            await udp.send(b"", ("10.0.0.5", 5000))  # an empty keepalive restarts the timer
            clock[0] += 29
            udp.device_says(b"b")
            clock[0] += 2               # 31 s without a datagram
            udp.device_says(b"c")       # not sent: forgotten
            assert _udp_peer(runner) is None and runner.status.clients == []
            await udp.send(b"again", ("10.0.0.6", 5001))
            udp.device_says(b"d")
    finally:
        bridge_mod.time.monotonic = real
    assert [d for d, _ in udp.transport.sent] == [b"a", b"b", b"d"], udp.transport.sent
    assert udp.transport.sent[-1][1] == ("10.0.0.6", 5001)
    assert any("udp peer 10.0.0.5:5000 forgotten: no datagram for 30s" in m for m in logs), logs
    assert "forgotten after 30s" in udp.bridge.describe()

    # a quiet device: the writer task forgets the sender on its own (about once a second)
    runner = _make_runner(udp_peer_timeout_s=1)
    async with _Udp(runner) as udp:
        await udp.send(b"hi", ("10.0.0.5", 5000))
        udp.bridge.peer.last_heard -= 5
        await asyncio.sleep(1.2)
        assert _udp_peer(runner) is None and runner.status.clients == []
    print("peer timeout: a quiet sender is forgotten, a keepalive keeps it  OK")


def test_reply_address_validation():
    def mapping(**network):
        return MappingConfig.from_dict({"name": "u", "serial": {"port": "COM3"},
                                        "network": {"mode": "udp", "bind_ip": "0.0.0.0",
                                                    "port": 4001, **network}})

    def refused(m, text):
        try:
            m.validate_input()
        except ConfigError as e:
            assert text in str(e), e
            return
        raise AssertionError(f"accepted: {m.network}")

    for ok in (mapping(), mapping(udp_peer_host="192.168.1.20", udp_peer_port=4001),
               mapping(udp_peer_timeout_s=60),
               mapping(bind_ip="::", dual_stack=True, udp_peer_host="10.0.0.1", udp_peer_port=9),
               mapping(dual_stack=True, udp_peer_host="fd00::1", udp_peer_port=9),
               mapping(bind_ip="::", dual_stack=False, udp_peer_host="fd00::1", udp_peer_port=9)):
        ok.validate()
        ok.validate_input()
    refused(mapping(udp_peer_host="plc.local", udp_peer_port=4001), "must be a host's IP address")
    refused(mapping(udp_peer_host="0.0.0.0", udp_peer_port=4001), "must be a host's IP address")
    refused(mapping(udp_peer_host="10.0.0.1"), "reply port must be between 1 and 65535")
    refused(mapping(udp_peer_host="10.0.0.1", udp_peer_port=70000), "between 1 and 65535")
    refused(mapping(udp_peer_host="fd00::1", udp_peer_port=9), "is IPv6, but the mapping listens")
    # :: takes IPv6 only (IPV6_V6ONLY): an IPv4 reply address can't be reached from it
    refused(mapping(bind_ip="::", dual_stack=False, udp_peer_host="10.0.0.1", udp_peer_port=9),
            "is IPv4, but the mapping listens on an IPv6 address")
    refused(mapping(bind_ip="2001:db8::5", dual_stack=False, udp_peer_host="10.0.0.1",
                    udp_peer_port=9), "is IPv4, but the mapping listens")
    refused(mapping(udp_peer_timeout_s=-1), "Peer timeout must be between 0")
    # other modes ignore leftovers, and validate() never refuses them (a stored config loads)
    tcp = MappingConfig.from_dict({"name": "t", "serial": {"port": "COM3"},
                                   "network": {"mode": "server", "udp_peer_host": "junk"}})
    tcp.validate()
    tcp.validate_input()
    refused(mapping(udp_peer_timeout_s="soon"), "Peer timeout must be between 0")
    soon = mapping(udp_peer_timeout_s="soon")
    soon.validate()
    assert _UdpBridge(MappingRunner(soon)).timeout == 0, "a stored non-number reads as never"
    # ...but a malformed stored reply address fails the start (no silent last-sender)
    stored = mapping(udp_peer_host="junk")
    stored.validate()
    try:
        _UdpBridge(MappingRunner(stored))
    except ValueError as e:
        assert "not an IP address" in str(e), e
    else:
        raise AssertionError("a malformed reply address was accepted at start")
    # the old connect-out fields are not a reply address
    leftover = mapping(remote_host="10.0.0.1", remote_port=4001)
    assert _UdpBridge(MappingRunner(leftover)).reply_to is None
    print("reply address: edits refuse a malformed one, a stored one fails the start  OK")


async def main():
    await test_disallowed_source_is_dropped()
    await test_allowed_source_passes()
    await test_spoof_cannot_steal_existing_peer()
    await test_read_only_blocks_serial_write_but_keeps_peer()
    await test_empty_allowlist_allows_any()
    await test_flood_is_bounded()
    await test_port_closed_drops()
    await test_last_sender_gets_the_output()
    await test_reply_address()
    await test_peer_timeout()
    test_reply_address_validation()
    print("\nPASS: UDP access control, write queue and reply address")


if __name__ == "__main__":
    asyncio.run(main())
