"""Telnet (RFC 854) mode: IAC escaping + minimal option negotiation.

We proactively negotiate 8-bit-clean BINARY (RFC 856) in both directions plus
SGA (RFC 858) so the link carries arbitrary bytes. Unknown options are refused
with WONT/DONT. A simplified, loop-safe negotiation state machine (we pre-mark
our requested states so the peer's acknowledgements don't trigger replies) keeps
this from oscillating, per RFC 854's loop-avoidance guidance.

This is the shared IAC handling that distinguishes 'telnet' from 'raw'. RFC2217
goes further and is handled by pyserial's PortManager (see rfc2217.py).
"""
from __future__ import annotations

from .base import ProtocolSession

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240

OPT_BINARY = 0
OPT_SGA = 3
SUPPORTED = frozenset({OPT_BINARY, OPT_SGA})


class TelnetSession(ProtocolSession):
    def __init__(self) -> None:
        self._net_out = bytearray()
        self._state = "data"
        self._cmd = 0
        self._local: dict[int, bool] = {}   # options WE will perform
        self._remote: dict[int, bool] = {}  # options the PEER performs (we DO)
        # Proactively request an 8-bit clean session, pre-marking desired state.
        for opt in (OPT_BINARY, OPT_SGA):
            self._local[opt] = True
            self._send(IAC, WILL, opt)
            self._remote[opt] = True
            self._send(IAC, DO, opt)

    # ----- helpers -----
    def _send(self, *bytes_: int) -> None:
        self._net_out.extend(bytes_)

    def initial_net_bytes(self) -> bytes:
        return self.take_net_out()

    def take_net_out(self) -> bytes:
        out = bytes(self._net_out)
        self._net_out.clear()
        return out

    # ----- serial -> network: escape IAC -----
    def from_serial(self, data: bytes) -> bytes:
        if 0xFF in data:
            return data.replace(b"\xff", b"\xff\xff")
        return data

    # ----- network -> serial: strip IAC, handle negotiation -----
    def from_network(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            st = self._state
            if st == "data":
                if byte == IAC:
                    self._state = "iac"
                else:
                    out.append(byte)
            elif st == "iac":
                if byte == IAC:
                    out.append(IAC)  # escaped literal 0xFF
                    self._state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self._cmd = byte
                    self._state = "opt"
                elif byte == SB:
                    self._state = "sb"
                else:
                    self._state = "data"  # NOP and other 2-byte commands: ignore
            elif st == "opt":
                self._negotiate(self._cmd, byte)
                self._state = "data"
            elif st == "sb":
                if byte == IAC:
                    self._state = "sb_iac"
            elif st == "sb_iac":
                self._state = "data" if byte == SE else "sb"
        return bytes(out)

    def _negotiate(self, cmd: int, opt: int) -> None:
        if cmd == DO:
            if opt in SUPPORTED:
                if not self._local.get(opt):
                    self._local[opt] = True
                    self._send(IAC, WILL, opt)
            elif self._local.get(opt) is not False:
                self._local[opt] = False
                self._send(IAC, WONT, opt)
        elif cmd == DONT:
            if self._local.get(opt) is not False:
                self._local[opt] = False
                self._send(IAC, WONT, opt)
        elif cmd == WILL:
            if opt in SUPPORTED:
                if not self._remote.get(opt):
                    self._remote[opt] = True
                    self._send(IAC, DO, opt)
            elif self._remote.get(opt) is not False:
                self._remote[opt] = False
                self._send(IAC, DONT, opt)
        elif cmd == WONT:
            if self._remote.get(opt) is not False:
                self._remote[opt] = False
                self._send(IAC, DONT, opt)
