"""Protocol session interface shared by raw/telnet/rfc2217.

One session is created PER client connection (protocol negotiation state is
per-client). The bridge calls these methods to transform bytes in each direction
and to collect any control bytes the protocol needs to send back to the network
client out-of-band (telnet negotiation replies, RFC2217 modem notifications).
"""
from __future__ import annotations

import re
from typing import Any

#: The longest telnet subnegotiation (the bytes between IAC SB and IAC SE) a client may
#: send. Real ones are a few bytes (an RFC 2217 COM-PORT-OPTION command at most 6, a
#: SIGNATURE a short text); a client that goes past this is disconnected (ProtocolError).
MAX_SUBNEGOTIATION = 1024

#: A run of data bytes and escaped IACs (IAC IAC = one 0xFF byte), up to the next telnet
#: command: the telnet and RFC 2217 parsers pass such a run on in one go instead of
#: looking at every byte in Python.
_DATA_RUN = re.compile(rb"[^\xff]*(?:\xff\xff[^\xff]*)*")


def data_run_end(data: bytes, start: int) -> int:
    """Where the run of data bytes and escaped IACs from ``start`` ends: at the IAC of the
    next command, or at the end of ``data`` (``start`` itself when a command is there)."""
    m = _DATA_RUN.match(data, start)
    return m.end() if m else start


class ProtocolError(ConnectionError):
    """Ends a client session that can't go on (the client broke the protocol, a reopened
    port refused the client's settings); the message is the reason logged."""


def subnegotiation_too_long() -> ProtocolError:
    return ProtocolError(
        f"protocol error: telnet subnegotiation longer than {MAX_SUBNEGOTIATION} bytes")


class ProtocolSession:
    #: If set (seconds), the bridge calls poll() on this interval.
    poll_interval: float | None = None

    def initial_net_bytes(self) -> bytes:
        """Bytes to send to the client immediately on connect (e.g. telnet negotiation)."""
        return b""

    def from_network(self, data: bytes) -> bytes:
        """Transform bytes received from the network into bytes to write to serial.
        Raises ProtocolError when the client broke the protocol beyond repair."""
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

    def serial_changed(self, serial: Any) -> None:
        """The mapping's serial port was reopened (``serial``: the new pyserial object) or
        closed (None). Only a session that drives the port itself (RFC 2217) uses it;
        raises ProtocolError when the session can't go on with the reopened port. Bytes
        for the client are collected with take_net_out()."""
