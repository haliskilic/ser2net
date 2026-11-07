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

A request is refused as RFC 2217 says, by answering with the current setting, and the
client stays connected: a malformed or reserved value (a short SET-BAUDRATE, parity 6,
stop size 4, ...) is handled as a request for the current setting, and a value the serial
port refuses (Windows SetCommState, a baud rate the OS can't take) is first taken back
off the serial object, where pyserial stores it before the driver sees it. What a client
sends that is refused or ignored is logged (rate-limited by the bridge).

Known upstream gaps (documented, not fatal): server-side NOTIFY-LINESTATE is
stubbed, and "query current" for BREAK/DTR/RTS is stubbed. Mainstream flow control
and baud/parity/databits/stopbits changes work.
"""
from __future__ import annotations

import contextlib
from typing import Callable, Optional

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

    A port setting the serial port refuses is put back to what it was and raised as
    ValueError, which PortManager answers with the current setting (_set_config).
    """

    def __init__(self, ser, read_only: bool = False,
                 log: Optional[Callable[[str], None]] = None) -> None:
        object.__setattr__(self, "_ser", ser)
        object.__setattr__(self, "_read_only", read_only)
        object.__setattr__(self, "_log", log or _noop)

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
        if name in _CONFIG_WRITES:
            self._set_config(name, value)
            return
        setattr(self._ser, name, value)

    def _set_config(self, name: str, value) -> None:
        """Set a port setting; raises ValueError, with the old value back in place, when
        the port refuses it. pyserial stores a new value before it reconfigures the port,
        so a value the driver or the OS refused (SerialException, termios.error,
        OverflowError, ...) would otherwise stay set: reported by every later query and
        applied again with the next change."""
        ser = self._ser
        old = getattr(ser, name)
        try:
            setattr(ser, name, value)
        except Exception as e:
            with contextlib.suppress(Exception):
                setattr(ser, name, old)
            if value != old:
                self._log(f"RFC 2217: the serial port refused {name} {value!r} "
                          f"({type(e).__name__}: {e}); it stays {old!r}")
            raise ValueError(f"{name} {value!r} refused: {e}") from e


class _NetConnection:
    """Minimal sink implementing the .write(bytes) that PortManager expects."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)


_IAC, _SB, _SE = _rfc2217.IAC, _rfc2217.SB, _rfc2217.SE
_NEGOTIATION = (_rfc2217.DO, _rfc2217.DONT, _rfc2217.WILL, _rfc2217.WONT)
M_NORMAL, M_IAC_SEEN, M_NEGOTIATE = _rfc2217.M_NORMAL, _rfc2217.M_IAC_SEEN, _rfc2217.M_NEGOTIATE

# COM-PORT-OPTION commands a client sends (RFC 2217 section 3)
_COMMANDS = {
    0: "SIGNATURE", 1: "SET-BAUDRATE", 2: "SET-DATASIZE", 3: "SET-PARITY", 4: "SET-STOPSIZE",
    5: "SET-CONTROL", 6: "NOTIFY-LINESTATE", 7: "NOTIFY-MODEMSTATE", 8: "FLOWCONTROL-SUSPEND",
    9: "FLOWCONTROL-RESUME", 10: "SET-LINESTATE-MASK", 11: "SET-MODEMSTATE-MASK",
    12: "PURGE-DATA",
}
# The ones that set a port setting: the size of the value and the values RFC 2217 defines
# (None: any); 0 asks for the current setting, the rest are reserved.
_SETTINGS = {
    1: (4, None),
    2: (1, frozenset({0, 5, 6, 7, 8})),
    3: (1, frozenset(range(6))),        # none, odd, even, mark, space
    4: (1, frozenset(range(4))),        # 1, 2, 1.5 stop bits
}
_SET_CONTROL, _PURGE_DATA = 5, 12
_FLOW_CONTROL = frozenset({1, 2, 3})    # SET-CONTROL: no / XON-XOFF / hardware flow control
_NEEDS_VALUE = frozenset({5, 10, 11, 12})
_PURGE_VALUES = frozenset({1, 2, 3})    # receive / transmit / both buffers


def _show(value: bytes) -> str:
    """A request's value for the log (the first bytes of a long one)."""
    if not value:
        return "without a value"
    return value[:8].hex(" ") + (" ..." if len(value) > 8 else "")


class _PortManager(_rfc2217.PortManager):
    """pyserial's RFC 2217 server engine with a parser of our own in front of it (feed).
    It keeps PortManager's parser state (mode, suboption, telnet_command), so the command
    handlers and anything that looks at that state work as before. Commands go to
    PortManager's handlers after a check of their value (_telnet_process_subnegotiation)."""

    # PortManager's parser state (set in its __init__)
    mode: int
    suboption: Optional[bytearray]  # the subnegotiation under way (None: none)
    telnet_command: bytes

    def __init__(self, serial_port, connection, log: Optional[Callable[[str], None]] = None):
        self._note = log or _noop  # (PortManager's own `logger` is a logging.Logger)
        super().__init__(serial_port, connection)

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
                if self.suboption is None:
                    self._note("telnet IAC SE without IAC SB ignored")
                else:
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

    def _telnet_process_subnegotiation(self, suboption: bytes) -> None:
        """PortManager's handler, after a check of the value. RFC 2217 refuses a request by
        answering with the current setting, and PortManager's handlers raise on a value
        they can't read (struct.error, KeyError, TypeError):
          - a SET-BAUDRATE/DATASIZE/PARITY/STOPSIZE value that is too short or reserved is
            handled as a request for the current setting (value 0), which is the answer;
          - a SET-CONTROL, mask or PURGE-DATA without a (defined) value is ignored;
          - a value the serial port refuses comes back from _ModemSafeSerial as ValueError,
            which PortManager answers with the current setting itself;
          - anything else that fails is answered with the current setting where RFC 2217
            has an answer, a failed purge is acknowledged.
        Each of these is logged; none ends the session."""
        if suboption[:1] != _rfc2217.COM_PORT_OPTION or len(suboption) < 2:
            super()._telnet_process_subnegotiation(suboption)  # not RFC 2217: ignored
            return
        command, value = suboption[1], suboption[2:]
        name = _COMMANDS.get(command, f"command {command}")
        setting = _SETTINGS.get(command)
        if setting is not None:
            size, defined = setting
            if len(value) < size or (defined is not None and value[0] not in defined):
                self._note(f"RFC 2217 {name} {_show(value)} refused (malformed or reserved "
                           "value); answered with the current setting")
                suboption = suboption[:2] + bytes(size)  # a request for the current setting
        elif command in _NEEDS_VALUE and (
                not value or (command == _PURGE_DATA and value[0] not in _PURGE_VALUES)):
            self._note(f"RFC 2217 {name} {_show(value)} ignored (malformed or reserved value)")
            return
        flow = None
        try:
            if command == _SET_CONTROL and value[0] in _FLOW_CONTROL:
                flow = {"xonxoff": self.serial.xonxoff, "rtscts": self.serial.rtscts}
            super()._telnet_process_subnegotiation(suboption)
        except Exception as e:
            self._note(f"RFC 2217 {name} {_show(value)} failed ({type(e).__name__}: {e}); "
                       "answered with the current setting")
            self._answer_current(command, value, flow)

    def _answer_current(self, command: int, value: bytes, flow: Optional[dict]) -> None:
        """After a request that failed: undo what a flow control change did halfway, then
        answer with the current setting (a purge: acknowledge it)."""
        for attr, old in (flow or {}).items():
            with contextlib.suppress(Exception):
                setattr(self.serial, attr, old)
        with contextlib.suppress(Exception):
            if command in _SETTINGS:
                super()._telnet_process_subnegotiation(
                    _rfc2217.COM_PORT_OPTION + bytes([command]) + bytes(_SETTINGS[command][0]))
            elif flow is not None:
                super()._telnet_process_subnegotiation(
                    _rfc2217.COM_PORT_OPTION + _rfc2217.SET_CONTROL
                    + _rfc2217.SET_CONTROL_REQ_FLOW_SETTING)
            elif command == _PURGE_DATA:
                self.rfc2217_send_subnegotiation(_rfc2217.SERVER_PURGE_DATA, value[:1])


class Rfc2217Session(ProtocolSession):
    def __init__(self, serial_instance, poll_interval: float = 1.0, read_only: bool = False,
                 log: Optional[Callable[[str], None]] = None) -> None:
        """``log``: where what the client sends that is refused or ignored is reported."""
        self._conn = _NetConnection()
        safe_serial = (_ModemSafeSerial(serial_instance, read_only=read_only, log=log)
                       if serial_instance is not None else serial_instance)
        # PortManager.__init__ emits initial telnet negotiation into _conn.
        self._pm = _PortManager(safe_serial, self._conn, log=log)
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
