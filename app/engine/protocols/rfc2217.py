"""RFC 2217 mode: telnet + COM-PORT-CONTROL, via pyserial's PortManager.

We reuse ``serial.rfc2217.PortManager`` as the protocol engine — it parses the
telnet/RFC2217 subnegotiation stream and applies SET-BAUDRATE/DATASIZE/PARITY/
STOPSIZE/CONTROL directly onto the live serial.Serial, and emits NOTIFY-MODEMSTATE
deltas. We supply one PortManager PER client (it carries protocol state) and own
the networking ourselves.

PortManager writes its telnet replies/notifications by calling ``connection.write``;
we capture those into a buffer that the bridge drains to the socket.

The network stream is parsed by our own subclass (_PortManager.feed): the same state
machine as PortManager.filter, which looks at every byte in Python and keeps whatever
follows IAC SB without a limit, but data between commands goes through in bulk (a regex
scan, bytes.replace) and a subnegotiation longer than MAX_SUBNEGOTIATION ends the session.
Commands still go to PortManager's own handlers. Serial data is escaped with
bytes.replace, which gives exactly what PortManager.escape yields byte by byte.

Robustness: PortManager reads modem-status lines (CTS/DSR/RI/CD) and may toggle
RTS/DTR/BREAK. Virtual ports (PTYs) and some USB adapters don't support those
ioctls and raise OSError(ENOTTY), which would otherwise tear down the client. We
wrap the serial object so those specific operations degrade gracefully while every
other attribute (baudrate/parity/databits/stopbits/...) passes straight through.

Known upstream gaps (documented, not fatal): server-side NOTIFY-LINESTATE is
stubbed, and "query current" for BREAK/DTR/RTS is stubbed. Mainstream flow control
and baud/parity/databits/stopbits changes work.
"""
from __future__ import annotations

from typing import Optional

from serial import rfc2217 as _rfc2217

from .base import MAX_SUBNEGOTIATION, ProtocolSession, data_run_end, subnegotiation_too_long

_MODEM_READS = ("cts", "dsr", "ri", "cd")
_LINE_WRITES = ("rts", "dtr", "break_condition")
# Port-configuration writes RFC2217 SET-* commands perform on the live serial.
# Blocked on read-only mappings so a "read-only" client cannot reconfigure the
# physical port (baud/framing/flow-control) or toggle control lines.
_CONFIG_WRITES = ("baudrate", "bytesize", "parity", "stopbits", "xonxoff", "rtscts", "dsrdtr")
_RO_BLOCKED_WRITES = frozenset(_CONFIG_WRITES + _LINE_WRITES)
# Mutating method calls (buffer purge, writes) blocked on read-only mappings.
_RO_BLOCKED_CALLS = frozenset({
    "write", "flush", "reset_input_buffer", "reset_output_buffer",
    "flushInput", "flushOutput", "send_break", "setRTS", "setDTR", "setBreak",
})


def _noop(*_args, **_kwargs):
    return None


class _ModemSafeSerial:
    """Transparent proxy that prevents unsupported modem-line ioctls from raising.

    Reads of CTS/DSR/RI/CD fall back to False; writes of RTS/DTR/BREAK are ignored
    if the backend doesn't support them. All other attribute access (including
    baudrate/parity/bytesize/stopbits changes that RFC2217 needs) is delegated
    verbatim to the real serial instance.

    When ``read_only`` is set the proxy additionally drops every port-mutating
    operation (SET-BAUDRATE/DATASIZE/PARITY/STOPSIZE/CONTROL, RTS/DTR/BREAK,
    buffer purges). Reads still pass through, so the client keeps getting
    NOTIFY-MODEMSTATE; PortManager then echoes the *actual* (unchanged) settings
    back, which is the correct RFC2217 response for a request that took no effect.
    """

    def __init__(self, ser, read_only: bool = False) -> None:
        object.__setattr__(self, "_ser", ser)
        object.__setattr__(self, "_read_only", read_only)

    def __getattr__(self, name):
        if name in _MODEM_READS:
            try:
                return getattr(self._ser, name)
            except Exception:
                return False
        if self._read_only and name in _RO_BLOCKED_CALLS:
            return _noop
        return getattr(self._ser, name)

    def __setattr__(self, name, value):
        if self._read_only and name in _RO_BLOCKED_WRITES:
            return  # read-only: ignore baud/framing/flow-control/RTS/DTR/BREAK changes
        if name in _LINE_WRITES:
            try:
                setattr(self._ser, name, value)
            except Exception:
                pass
            return
        setattr(self._ser, name, value)


class _NetConnection:
    """Minimal sink implementing the .write(bytes) that PortManager expects."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)


_IAC, _SB, _SE = _rfc2217.IAC, _rfc2217.SB, _rfc2217.SE
_NEGOTIATION = (_rfc2217.DO, _rfc2217.DONT, _rfc2217.WILL, _rfc2217.WONT)
M_NORMAL, M_IAC_SEEN, M_NEGOTIATE = _rfc2217.M_NORMAL, _rfc2217.M_IAC_SEEN, _rfc2217.M_NEGOTIATE


class _PortManager(_rfc2217.PortManager):
    """pyserial's RFC 2217 server engine with a parser of our own in front of it (feed).
    It keeps PortManager's parser state (mode, suboption, telnet_command), so the command
    handlers and anything that looks at that state work as before."""

    # PortManager's parser state (set in its __init__)
    mode: int
    suboption: Optional[bytearray]  # the subnegotiation under way (None: none)
    telnet_command: bytes

    def feed(self, data: bytes) -> bytes:
        """Parse bytes from the network; returns those for the serial port. What filter()
        does, but a run of data bytes and escaped IACs is passed on, or added to the
        subnegotiation under way, in one go; only the bytes of a command go through the
        state machine one at a time. Raises ProtocolError once a subnegotiation grows past
        MAX_SUBNEGOTIATION."""
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            mode = self.mode
            if mode == M_NORMAL:
                end = data_run_end(data, i)
                if end > i:
                    run = data[i:end]
                    if 0xFF in run:
                        run = run.replace(b"\xff\xff", b"\xff")
                    if self.suboption is None:
                        out += run
                    else:
                        self._add_to_suboption(run)
                    i = end
                    if i == n:
                        break
                self.mode = M_IAC_SEEN  # data[i] is the IAC of a command
                i += 1
                continue
            byte = data[i:i + 1]
            i += 1
            self.mode = M_NORMAL
            if mode == M_NEGOTIATE:  # the option of a DO/DONT/WILL/WONT
                self._telnet_negotiate_option(self.telnet_command, byte)
            elif byte == _IAC:  # an escaped 0xFF split across two reads
                if self.suboption is None:
                    out += byte
                else:
                    self._add_to_suboption(byte)
            elif byte == _SB:
                self.suboption = bytearray()
            elif byte == _SE:
                if self.suboption is not None:  # an IAC SE without IAC SB is ignored
                    suboption, self.suboption = bytes(self.suboption), None
                    self._telnet_process_subnegotiation(suboption)
            elif byte in _NEGOTIATION:
                self.telnet_command = byte
                self.mode = M_NEGOTIATE
            else:
                self._telnet_process_command(byte)
        return bytes(out)

    def filter(self, data):
        """PortManager's generator interface, on top of feed()."""
        out = self.feed(data)
        if out:
            yield out

    def _add_to_suboption(self, data: bytes) -> None:
        sub = self.suboption
        assert sub is not None
        if len(sub) + len(data) > MAX_SUBNEGOTIATION:
            raise subnegotiation_too_long()
        sub += data


class Rfc2217Session(ProtocolSession):
    def __init__(self, serial_instance, poll_interval: float = 1.0, read_only: bool = False) -> None:
        self._conn = _NetConnection()
        safe_serial = (_ModemSafeSerial(serial_instance, read_only=read_only)
                       if serial_instance is not None else serial_instance)
        # PortManager.__init__ emits initial telnet negotiation into _conn.
        self._pm = _PortManager(safe_serial, self._conn)
        self.poll_interval = poll_interval if poll_interval and poll_interval > 0 else 1.0

    def initial_net_bytes(self) -> bytes:
        return self.take_net_out()

    def from_network(self, data: bytes) -> bytes:
        # the serial-bound bytes; telnet replies go to _conn.write
        pm = self._pm
        if pm.mode == M_NORMAL and pm.suboption is None and 0xFF not in data:
            return data  # no command in this chunk: it all goes to the port as it is
        return pm.feed(data)

    def from_serial(self, data: bytes) -> bytes:
        return data.replace(b"\xff", b"\xff\xff") if 0xFF in data else data

    def take_net_out(self) -> bytes:
        out = bytes(self._conn.buf)
        self._conn.buf.clear()
        return out

    def poll(self) -> bytes:
        # push modem-line (CTS/DSR/RI/CD) change notifications to the client
        try:
            self._pm.check_modem_lines()
        except Exception:
            pass
        return self.take_net_out()
