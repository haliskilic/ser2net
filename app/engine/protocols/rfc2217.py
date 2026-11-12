"""RFC 2217 mode: telnet + COM-PORT-CONTROL, via pyserial's PortManager.

We reuse ``serial.rfc2217.PortManager`` as the protocol engine — it parses the
telnet/RFC2217 subnegotiation stream and applies SET-BAUDRATE/DATASIZE/PARITY/
STOPSIZE/CONTROL directly onto the live serial.Serial, and emits NOTIFY-MODEMSTATE
deltas. We supply one PortManager PER client (it carries protocol state) and own
the networking ourselves.

PortManager writes its telnet replies/notifications by calling ``connection.write``;
we capture those into a buffer that the bridge drains to the socket.

The network stream is parsed by our own subclass (_PortManager.feed_parts): the same state
machine as PortManager.filter, which looks at every byte in Python and keeps whatever
follows IAC SB without a limit, but data between commands goes through in bulk (a regex
scan, bytes.replace) and a subnegotiation longer than MAX_SUBNEGOTIATION ends the session.
Serial data is escaped with bytes.replace, which gives exactly what PortManager.escape
yields byte by byte.

Commands and data keep their order: a command that acts on the port (SET-BAUDRATE,
SET-DATASIZE, SET-PARITY, SET-STOPSIZE, SET-CONTROL, PURGE-DATA) is not carried out while
the read is parsed, but returned at its place in the data (PortCommand), and so is every
command after it in that read; the bridge writes the data before it first and, for a
setting, waits until that data has left the port. IAC BRK becomes a serial BREAK there too
(BREAK), unless the mapping is read-only.

What pyserial's PortManager leaves out is answered here: SIGNATURE (an empty one asks for
ours: "ser2net", or the mapping's rfc2217_signature), the SET-CONTROL queries (BREAK, DTR
and RTS state, the inbound flow control) from the port's state, and SET-LINESTATE-MASK /
SET-MODEMSTATE-MASK with an acknowledgement. Flow control is one mode at a time (XON/XOFF
turns RTS/CTS off and the other way round, either one DSR/DTR) and a DSR/DTR port says so
when asked (DSR flow control outbound, DTR inbound); "no flow control" leaves DSR/DTR flow
control on, as before, since pyserial's rfc2217:// clients ask for it at every open. The
flow controls RFC 2217 has and pyserial can't set (inbound only, DCD, DTR, DSR) are refused
with the current setting.

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

Not done (documented, not fatal): server-side NOTIFY-LINESTATE is stubbed (always 0),
FLOWCONTROL-SUSPEND/RESUME are taken but don't hold the client's data, and PURGE-DATA
purges the operating system's buffers, not the data still waiting in the bridge.
"""
from __future__ import annotations

import contextlib
from functools import partial
from typing import Callable, Optional

from serial import rfc2217 as _rfc2217

from .base import (BREAK, MAX_SUBNEGOTIATION, PortCommand, ProtocolError, ProtocolSession,
                   data_run_end, subnegotiation_too_long)

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
    NOTIFY-MODEMSTATE and the queries answer the port's real state. A refused
    SET-BAUDRATE/DATASIZE/PARITY/STOPSIZE is answered with the actual (unchanged)
    setting, the correct RFC 2217 response for a request that took no effect; a
    SET-CONTROL that would change flow control, RTS, DTR or BREAK is acknowledged
    with the value asked for although nothing changed, since pyserial's rfc2217://
    client fails its open() on any other answer.

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
            if name in _LINE_WRITES:  # no port: as the client set them (applied at reopen)
                return self._lines.get(name, False)
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


_IAC, _SB, _SE, _BRK = _rfc2217.IAC, _rfc2217.SB, _rfc2217.SE, _rfc2217.BRK
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
_SIGNATURE, _SET_CONTROL, _PURGE_DATA = 0, 5, 12
_SERVER_SIGNATURE = bytes([100])        # (pyserial has no constant for it)
_FLOW_CONTROL = frozenset({1, 2, 3})    # SET-CONTROL: no / XON-XOFF / hardware flow control
# SET-CONTROL values that change the port: flow control, BREAK, DTR and RTS on / off
_CONTROL_CHANGES = frozenset({1, 2, 3, 5, 6, 8, 9, 11, 12})
# what a SET-CONTROL value belongs to: its query (flow control out, BREAK, DTR, RTS, flow
# control in); 17 (DCD) and 19 (DSR flow control) are outbound / both, 18 (DTR) inbound
_CONTROL_QUERY = {**dict.fromkeys((0, 1, 2, 3, 17, 19), 0), **dict.fromkeys((4, 5, 6), 4),
                  **dict.fromkeys((7, 8, 9), 7), **dict.fromkeys((10, 11, 12), 10),
                  **dict.fromkeys((13, 14, 15, 16, 18), 13)}
# the masks' acknowledgements (RFC 2217: the server echoes the mask it now uses)
_MASK_ACKS = {10: _rfc2217.SERVER_SET_LINESTATE_MASK, 11: _rfc2217.SERVER_SET_MODEMSTATE_MASK}
_NEEDS_VALUE = frozenset({5, 10, 11, 12})
_PURGE_VALUES = frozenset({1, 2, 3})    # receive / transmit / both buffers
#: what an empty SIGNATURE (a request for ours) is answered with by default
DEFAULT_SIGNATURE = "ser2net"


def _acts_on_port(suboption: bytes) -> Optional[bool]:
    """Does a subnegotiation act on the serial port? None: no (a query, a mask, ...);
    True: it changes the port's settings (the data before it must have left first); False:
    it purges the port's buffers (the data before it must have been written first)."""
    if suboption[:1] != _rfc2217.COM_PORT_OPTION or len(suboption) < 3:
        return None
    command, value = suboption[1], suboption[2:]
    if command in _SETTINGS:
        return True if any(value) else None  # (0 asks for the current setting)
    if command == _SET_CONTROL:
        return True if value[0] in _CONTROL_CHANGES else None
    if command == _PURGE_DATA:
        return False
    return None


def _show(value: bytes) -> str:
    """A request's value for the log (the first bytes of a long one)."""
    if not value:
        return "without a value"
    return value[:8].hex(" ") + (" ..." if len(value) > 8 else "")


class _PortManager(_rfc2217.PortManager):
    """pyserial's RFC 2217 server engine with a parser of our own in front of it
    (feed_parts). It keeps PortManager's parser state (mode, suboption, telnet_command), so
    the command handlers and anything that looks at that state work as before. Commands go
    to PortManager's handlers after a check of their value (_telnet_process_subnegotiation);
    SIGNATURE and SET-CONTROL are answered here."""

    # PortManager's parser state (set in its __init__)
    mode: int
    suboption: Optional[bytearray]  # the subnegotiation under way (None: none)
    telnet_command: bytes

    def __init__(self, serial_port, connection, log: Optional[Callable[[str], None]] = None,
                 signature: str = ""):
        self._note = log or _noop  # (PortManager's own `logger` is a logging.Logger)
        text = signature if isinstance(signature, str) and signature else DEFAULT_SIGNATURE
        self._signature = text.encode("utf-8", "replace")
        super().__init__(serial_port, connection)

    def feed(self, data: bytes) -> bytes:
        """The bytes for the serial port in ``data``, its commands carried out at once
        (feed_parts for a caller that has no port to keep them in order with; a BREAK is
        left out)."""
        out = bytearray()
        for part in self.feed_parts(data):
            if isinstance(part, bytes):
                out += part
            elif isinstance(part, PortCommand):
                part.apply()
        return bytes(out)

    def feed_parts(self, data: bytes) -> list:
        """Parse bytes from the network; returns what they hold, in order: the bytes for
        the serial port, BREAK for an IAC BRK, and a PortCommand for a command that acts on
        the port and for every command after it or after a BREAK in ``data`` (so the
        replies keep their order); the commands before those are carried out at once. What
        filter() does, but a run of data bytes and escaped IACs is passed on, or added to
        the subnegotiation under way, in one go; only the bytes of a command go through the
        state machine one at a time. Raises ProtocolError once a subnegotiation grows past
        MAX_SUBNEGOTIATION."""
        parts: list = []
        out = bytearray()

        def flush() -> None:
            if out:
                parts.append(bytes(out))
                out.clear()

        def later(apply: Callable[[], None], settle: bool = False) -> None:
            flush()
            parts.append(PortCommand(apply, settle))

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
                if parts:
                    later(partial(self._telnet_negotiate_option, self.telnet_command, byte))
                else:
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
                    acts = _acts_on_port(suboption)
                    if parts or acts is not None:
                        later(partial(self._telnet_process_subnegotiation, suboption),
                              settle=acts is True)
                    else:
                        self._telnet_process_subnegotiation(suboption)
            elif byte in _NEGOTIATION:
                self.telnet_command = byte
                self.mode = M_NEGOTIATE
            elif byte == _BRK:  # a serial BREAK, after the data before it
                flush()
                parts.append(BREAK)
            elif parts:
                later(partial(self._telnet_process_command, byte))
            else:
                self._telnet_process_command(byte)
        flush()
        return parts

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
        Each of these is logged; none ends the session. SIGNATURE and SET-CONTROL are
        handled here (_set_control), a mask is acknowledged once PortManager stored it."""
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
        if command == _SIGNATURE:
            if not value:  # (with a text: the client's own signature, nothing to answer)
                self.rfc2217_send_subnegotiation(_SERVER_SIGNATURE, self._signature)
            return
        flow = None
        try:
            if command == _SET_CONTROL:
                if value[0] in _FLOW_CONTROL:
                    flow = {attr: getattr(self.serial, attr, False)
                            for attr in ("xonxoff", "rtscts", "dsrdtr")}
                self._set_control(value[0])
                return
            super()._telnet_process_subnegotiation(suboption)
            if command in _MASK_ACKS:
                self.rfc2217_send_subnegotiation(_MASK_ACKS[command], value[:1])
        except Exception as e:
            self._note(f"RFC 2217 {name} {_show(value)} failed ({type(e).__name__}: {e}); "
                       "answered with the current setting")
            self._answer_current(command, value, flow)

    def _set_control(self, value: int) -> None:
        """SET-CONTROL: change flow control (one mode at a time), BREAK, DTR or RTS and
        acknowledge it, or answer a query with the port's state. A flow control pyserial
        can't set (inbound only, DCD, DTR, DSR) or a reserved value is refused with the
        current setting of what it belongs to (outbound flow control for a reserved one)."""
        ser = self.serial
        if value in _FLOW_CONTROL:
            for attr, on in (("xonxoff", value == 2), ("rtscts", value == 3)):
                if getattr(ser, attr) != on:
                    setattr(ser, attr, on)
            if value != 1 and getattr(ser, "dsrdtr", False):  # (no flow control: see above)
                ser.dsrdtr = False
            reply = value
        elif value in (5, 6):
            ser.break_condition = value == 5
            reply = value
        elif value in (8, 9):
            ser.dtr = value == 8
            reply = value
        elif value in (11, 12):
            ser.rts = value == 11
            reply = value
        else:
            query = _CONTROL_QUERY.get(value, 0)
            if query != value:
                self._note(f"RFC 2217 SET-CONTROL {value} refused (a flow control this port "
                           "can't set, or a reserved value); answered with the current "
                           "setting")
            reply = self._control_state(query)
        self.rfc2217_send_subnegotiation(_rfc2217.SERVER_SET_CONTROL, bytes([reply]))

    def _control_state(self, query: int) -> int:
        """The answer to a SET-CONTROL query (0, 4, 7, 10 or 13): the port's state."""
        ser = self.serial
        if query == 4:
            return 5 if ser.break_condition else 6
        if query == 7:
            return 8 if ser.dtr else 9
        if query == 10:
            return 11 if ser.rts else 12
        dsrdtr = getattr(ser, "dsrdtr", False)
        if query == 13:  # inbound: XON/XOFF, hardware (RTS), DTR
            return 15 if ser.xonxoff else 16 if ser.rtscts else 18 if dsrdtr else 14
        # outbound / both: XON/XOFF, hardware (CTS), DSR (pyserial's order)
        return 2 if ser.xonxoff else 3 if ser.rtscts else 19 if dsrdtr else 1

    def _answer_current(self, command: int, value: bytes, flow: Optional[dict]) -> None:
        """After a request that failed: undo what a flow control change did halfway, then
        answer with the current setting (a purge: acknowledge it)."""
        for attr, old in (flow or {}).items():
            with contextlib.suppress(Exception):
                if getattr(self.serial, attr, False) != old:
                    setattr(self.serial, attr, old)
        with contextlib.suppress(Exception):
            if command in _SETTINGS:
                super()._telnet_process_subnegotiation(
                    _rfc2217.COM_PORT_OPTION + bytes([command]) + bytes(_SETTINGS[command][0]))
            elif command == _SET_CONTROL:
                self.rfc2217_send_subnegotiation(
                    _rfc2217.SERVER_SET_CONTROL,
                    bytes([self._control_state(_CONTROL_QUERY.get(value[0], 0))]))
            elif command == _PURGE_DATA:
                self.rfc2217_send_subnegotiation(_rfc2217.SERVER_PURGE_DATA, value[:1])


class Rfc2217Session(ProtocolSession):
    def __init__(self, serial_instance, poll_interval: float = 1.0, read_only: bool = False,
                 log: Optional[Callable[[str], None]] = None, signature: str = "") -> None:
        """``log``: where what the client sends that is refused or ignored is reported;
        ``signature``: what a SIGNATURE request is answered with (blank: "ser2net")."""
        self._conn = _NetConnection()
        self._serial = _ModemSafeSerial(serial_instance, read_only=read_only, log=log)
        # PortManager.__init__ emits initial telnet negotiation into _conn.
        self._pm = _PortManager(self._serial, self._conn, log=log, signature=signature)
        self.poll_interval = poll_interval if poll_interval and poll_interval > 0 else 1.0

    def initial_net_bytes(self) -> bytes:
        return self.take_net_out()

    def from_network_parts(self, data: bytes) -> list:
        # the serial-bound bytes and the commands between them; replies go to _conn.write
        pm = self._pm
        if pm.mode == M_NORMAL and pm.suboption is None and 0xFF not in data:
            # no command in this chunk: it all goes to the port as it is
            return [data] if data else []
        return pm.feed_parts(data)

    def break_left_on(self) -> bool:
        """The client set BREAK on (SET-CONTROL) and did not turn it off again."""
        return bool(self._serial._lines.get("break_condition"))

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
