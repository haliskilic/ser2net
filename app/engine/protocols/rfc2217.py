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

The serial port can go away and be reopened while a client stays connected (the bridge
reopens it with a new pyserial object and calls serial_changed): the session then drives
the reopened port, which is given the settings the client has, so it goes on as before.
While the port is closed, the client's settings can still be read and changed (they take
effect when it is back), the modem lines read low and a purge has nothing to do.

Known upstream gaps (documented, not fatal): server-side NOTIFY-LINESTATE is
stubbed, and "query current" for BREAK/DTR/RTS is stubbed. Mainstream flow control
and baud/parity/databits/stopbits changes work.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Callable, Optional

from serial import rfc2217 as _rfc2217

from .base import (MAX_SUBNEGOTIATION, ProtocolError, ProtocolSession, data_run_end,
                   subnegotiation_too_long)

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


def _settings_of(ser) -> dict:
    """A port's settings, the ones an RFC 2217 client can change ({} without a port)."""
    settings = {}
    if ser is not None:
        for name in _CONFIG_WRITES:
            with contextlib.suppress(Exception):
                settings[name] = getattr(ser, name)
    return settings


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

    The proxy keeps the port settings as the client knows them, and the RTS/DTR/BREAK
    state it set: rebind() moves it to a reopened port, which is given them, or to None
    while the port is closed. Without a port, settings are read from and changed in what
    the proxy keeps (the reopened port gets the changes), line changes wait for the port
    too, modem lines read low and a purge does nothing.
    """

    def __init__(self, ser, read_only: bool = False,
                 log: Optional[Callable[[str], None]] = None) -> None:
        object.__setattr__(self, "_ser", ser)
        object.__setattr__(self, "_read_only", read_only)
        object.__setattr__(self, "_log", log or _noop)
        object.__setattr__(self, "_settings", _settings_of(ser))  # as the client knows them
        object.__setattr__(self, "_lines", {})  # RTS/DTR/BREAK as the client set them

    def __getattr__(self, name):
        ser = self._ser
        if name in _MODEM_READS:
            if ser is None:
                return False  # no port: the lines are low
            try:
                return getattr(ser, name)
            except Exception:
                return False
        if ser is None:
            if name in self._settings:
                return self._settings[name]
            if name in _RO_BLOCKED_CALLS:
                return _noop  # no port: nothing to purge
        elif self._read_only and name in _RO_BLOCKED_CALLS:
            return _noop
        return getattr(ser, name)

    def __setattr__(self, name, value):
        if self._read_only and name in _RO_BLOCKED_WRITES:
            return  # read-only: ignore baud/framing/flow-control/RTS/DTR/BREAK changes
        ser = self._ser
        if name in _LINE_WRITES:
            if ser is not None:
                try:
                    setattr(ser, name, value)
                except Exception:
                    return  # not supported by this port (PTYs, some USB adapters)
            self._lines[name] = value
            return
        if name in _CONFIG_WRITES:
            if ser is not None:
                self._set_config(name, value)
            self._settings[name] = value
            return
        setattr(ser, name, value)

    def rebind(self, ser) -> list[str]:
        """Drive ``ser`` from now on: the reopened port, or None while it is closed. A
        reopened port (of a writable mapping) is given the client's settings and line
        state; returns the settings it refused (it keeps its own for those)."""
        object.__setattr__(self, "_ser", ser)
        if ser is None:
            return []
        refused = []
        if not self._read_only:
            for name, value in self._settings.items():  # pyserial's order: baud rate first
                try:
                    if getattr(ser, name) != value:
                        self._set_config(name, value)
                except Exception:
                    refused.append(f"{name} {value!r}")
            for name, value in self._lines.items():
                with contextlib.suppress(Exception):
                    setattr(ser, name, value)
        object.__setattr__(self, "_settings", _settings_of(ser))
        return refused

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


if TYPE_CHECKING:
    class _PortManagerBase(_rfc2217.PortManager):
        # PortManager's private telnet handlers, which _PortManager calls and overrides;
        # pyserial's type stubs (types-pyserial) leave them out
        def _telnet_negotiate_option(self, command: bytes, option: bytes) -> None: ...
        def _telnet_process_command(self, command: bytes) -> None: ...
        def _telnet_process_subnegotiation(self, suboption: bytes) -> None: ...
else:
    _PortManagerBase = _rfc2217.PortManager


class _PortManager(_PortManagerBase):
    """pyserial's RFC 2217 server engine with a parser of our own in front of it (feed).
    It keeps PortManager's parser state (mode, suboption, telnet_command), so the command
    handlers and anything that looks at that state work as before. Commands go to
    PortManager's handlers after a check of their value (_telnet_process_subnegotiation)."""

    # PortManager's parser state (set in its __init__)
    mode: int
    # the subnegotiation under way (None: none); pyserial keeps a bytearray here, its
    # stubs say bytes (the ignore is unused where they aren't installed)
    suboption: Optional[bytearray]  # type: ignore[assignment, unused-ignore]
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
        assert sub is not None  # noqa: S101 - called only during a subnegotiation
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
        self._serial = _ModemSafeSerial(serial_instance, read_only=read_only, log=log)
        # PortManager.__init__ emits initial telnet negotiation into _conn.
        self._pm = _PortManager(self._serial, self._conn, log=log)
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

    def serial_changed(self, serial) -> None:
        """Drive the bridge's port from now on: the reopened one (given the client's
        settings, so the client goes on as before) or None while it is closed. The modem
        lines are reported to the client as they are now. Raises ProtocolError when the
        reopened port refuses a setting the client has (the client would be told one
        thing while the port runs at another)."""
        refused = self._serial.rebind(serial)
        with contextlib.suppress(Exception):
            self._pm.check_modem_lines()
        if refused:
            raise ProtocolError("the reopened serial port refused the client's settings ("
                                + ", ".join(refused) + ")")
