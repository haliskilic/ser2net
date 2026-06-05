"""Protocol session interface shared by raw/telnet/rfc2217.

One session is created PER client connection (protocol negotiation state is
per-client). The bridge calls these methods to transform bytes in each direction
and to collect any control bytes the protocol needs to send back to the network
client out-of-band (telnet negotiation replies, RFC2217 modem notifications).
"""
from __future__ import annotations


class ProtocolSession:
    #: If set (seconds), the bridge calls poll() on this interval.
    poll_interval: float | None = None

    def initial_net_bytes(self) -> bytes:
        """Bytes to send to the client immediately on connect (e.g. telnet negotiation)."""
        return b""

    def from_network(self, data: bytes) -> bytes:
        """Transform bytes received from the network into bytes to write to serial."""
        return data

    def from_serial(self, data: bytes) -> bytes:
        """Transform bytes read from serial into bytes to send to the network client."""
        return data

    def take_net_out(self) -> bytes:
        """Return + clear any pending control bytes destined for the network client."""
        return b""

    def poll(self) -> bytes:
        """Periodic hook (only called if poll_interval is set). Returns net-bound bytes."""
        return b""
