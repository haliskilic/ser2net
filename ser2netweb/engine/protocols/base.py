"""Protocol session interface shared by raw/telnet/rfc2217.

One session is created PER client connection (protocol negotiation state is
per-client). The bridge calls these methods to transform bytes in each direction
and to collect any control bytes the protocol needs to send back to the network
client out-of-band (telnet negotiation replies, RFC2217 modem notifications).
"""
from __future__ import annotations

import re
from typing import Any, Callable

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


class SerialBreak:
    """An item of from_network_parts(): the client asked for a serial BREAK here (telnet
    IAC BRK). The bridge sends it once the data before it has left the port."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "BREAK"


BREAK = SerialBreak()


class PortCommand:
    """An item of from_network_parts(): a command of the client's, at its place in the data.
    The bridge calls apply() once the bytes before it are written to the port and, with
    ``settle`` (a command that changes the port: an RFC 2217 SET-BAUDRATE, SET-CONTROL,
    ...), have left it, so they go out at the setting they were sent with. The commands
    after one of these in the same read are PortCommands too, so the replies keep their
    order."""

    __slots__ = ("apply", "settle")

    def __init__(self, apply: Callable[[], None], settle: bool = False) -> None:
        self.apply = apply
        self.settle = settle

    def __repr__(self) -> str:
        return f"PortCommand(settle={self.settle})"


class ProtocolSession:
    #: If set (seconds), the bridge calls poll() on this interval.
    poll_interval: float | None = None

    def initial_net_bytes(self) -> bytes:
        """Bytes to send to the client immediately on connect (e.g. telnet negotiation)."""
        return b""

    def from_network_parts(self, data: bytes) -> list:
        """What the client sent, in order: the bytes for the serial port (bytes), with BREAK
        where it asked for a serial break and a PortCommand where a command of its acts on
        the port. Raises ProtocolError when the client broke the protocol beyond repair."""
        return [data] if data else []

    def from_network(self, data: bytes) -> bytes:
        """The bytes for the serial port in ``data``, its commands carried out at once (a
        BREAK is left out): from_network_parts() for a caller that has no port to keep them
        in order with. Raises ProtocolError like it."""
        parts = self.from_network_parts(data)
        if len(parts) == 1 and isinstance(parts[0], bytes):
            return parts[0]
        out = bytearray()
        for part in parts:
            if isinstance(part, bytes):
                out += part
            elif isinstance(part, PortCommand):
                part.apply()
        return bytes(out)

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
