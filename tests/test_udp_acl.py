"""UDP access-control regression test (H1).

Verifies that a UDP mapping enforces ``allowed_client_ips``: a datagram from a
source outside the allow-list is dropped (no serial write, no peer registration),
so an off-path / spoofed sender cannot hijack the serial<->net stream — including
on read-only mappings. Pure stdlib + the engine internals, so it runs identically
on Windows and Linux (no socat / no hardware).

Run: python3 tests/test_udp_acl.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.bridge import MappingRunner, _UdpBridge, _UdpPeer


class _FakeWriter:
    """Stand-in for the serial StreamWriter: just records what was written."""

    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)


def _make_runner(*, allowed, read_only=False):
    m = MappingConfig.from_dict({
        "name": "udp-acl", "enabled": True,
        "serial": {"port": "/dev/null", "baudrate": 115200},
        "network": {
            "mode": "udp", "protocol": "raw",
            "bind_ip": "127.0.0.1", "port": 48030,
            "allowed_client_ips": allowed, "read_only": read_only,
        },
    })
    runner = MappingRunner(m)
    runner._swriter = _FakeWriter()
    return runner


def _udp_peer(runner):
    return next((c for c in runner._clients if isinstance(c, _UdpPeer)), None)


def test_disallowed_source_is_dropped():
    runner = _make_runner(allowed=["10.0.0.0/24"])
    bridge = _UdpBridge(runner)

    bridge.datagram_received(b"attack", ("203.0.113.7", 5000))  # outside allow-list
    assert bytes(runner._swriter.written) == b"", "disallowed datagram reached the serial port"
    assert _udp_peer(runner) is None, "disallowed sender was registered as the UDP peer"
    print("disallowed UDP source dropped (no serial write, no peer)  OK")


def test_allowed_source_passes():
    runner = _make_runner(allowed=["10.0.0.0/24"])
    bridge = _UdpBridge(runner)

    bridge.datagram_received(b"hello", ("10.0.0.5", 5000))      # inside allow-list
    assert bytes(runner._swriter.written) == b"hello", "allowed datagram did not reach serial"
    peer = _udp_peer(runner)
    assert peer is not None and peer.addr == ("10.0.0.5", 5000)
    print("allowed UDP source accepted (serial write + peer)  OK")


def test_spoof_cannot_steal_existing_peer():
    runner = _make_runner(allowed=["10.0.0.0/24"])
    bridge = _UdpBridge(runner)

    bridge.datagram_received(b"hi", ("10.0.0.5", 5000))         # legit peer
    bridge.datagram_received(b"steal", ("203.0.113.7", 6000))   # spoof attempt
    peer = _udp_peer(runner)
    assert peer is not None and peer.addr == ("10.0.0.5", 5000), "spoofed source hijacked the peer"
    print("spoofed source cannot replace the registered peer  OK")


def test_read_only_blocks_serial_write_but_keeps_peer():
    runner = _make_runner(allowed=["10.0.0.0/24"], read_only=True)
    bridge = _UdpBridge(runner)

    bridge.datagram_received(b"data", ("10.0.0.5", 5000))       # allowed, but read-only
    assert bytes(runner._swriter.written) == b"", "read-only mapping wrote client data to serial"
    assert _udp_peer(runner) is not None, "read-only peer not tracked (would get no serial output)"
    print("read-only UDP: peer tracked for output, but no client->serial write  OK")


def test_empty_allowlist_allows_any():
    runner = _make_runner(allowed=[])                            # blank = any (matches TCP semantics)
    bridge = _UdpBridge(runner)

    bridge.datagram_received(b"x", ("203.0.113.7", 5000))
    assert bytes(runner._swriter.written) == b"x", "empty allow-list should accept any source"
    print("empty allow-list accepts any source (documented 'any' semantics)  OK")


def main():
    test_disallowed_source_is_dropped()
    test_allowed_source_passes()
    test_spoof_cannot_steal_existing_peer()
    test_read_only_blocks_serial_write_but_keeps_peer()
    test_empty_allowlist_allows_any()
    print("\nPASS: UDP access-control (H1)")


if __name__ == "__main__":
    main()
