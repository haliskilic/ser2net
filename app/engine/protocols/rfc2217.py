"""RFC 2217 mode: telnet + COM-PORT-CONTROL, via pyserial's PortManager.

We reuse ``serial.rfc2217.PortManager`` as the protocol engine — it parses the
telnet/RFC2217 subnegotiation stream and applies SET-BAUDRATE/DATASIZE/PARITY/
STOPSIZE/CONTROL directly onto the live serial.Serial, and emits NOTIFY-MODEMSTATE
deltas. We supply one PortManager PER client (it carries protocol state) and own
the networking ourselves.

PortManager writes its telnet replies/notifications by calling ``connection.write``;
we capture those into a buffer that the bridge drains to the socket.

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

from .base import ProtocolSession

_MODEM_READS = ("cts", "dsr", "ri", "cd")
_LINE_WRITES = ("rts", "dtr", "break_condition")


class _ModemSafeSerial:
    """Transparent proxy that prevents unsupported modem-line ioctls from raising.

    Reads of CTS/DSR/RI/CD fall back to False; writes of RTS/DTR/BREAK are ignored
    if the backend doesn't support them. All other attribute access (including
    baudrate/parity/bytesize/stopbits changes that RFC2217 needs) is delegated
    verbatim to the real serial instance.
    """

    def __init__(self, ser) -> None:
        object.__setattr__(self, "_ser", ser)

    def __getattr__(self, name):
        if name in _MODEM_READS:
            try:
                return getattr(self._ser, name)
            except Exception:
                return False
        return getattr(self._ser, name)

    def __setattr__(self, name, value):
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


class Rfc2217Session(ProtocolSession):
    def __init__(self, serial_instance, poll_interval: float = 1.0) -> None:
        from serial.rfc2217 import PortManager

        self._conn = _NetConnection()
        safe_serial = _ModemSafeSerial(serial_instance) if serial_instance is not None else serial_instance
        # PortManager.__init__ emits initial telnet negotiation into _conn.
        self._pm = PortManager(safe_serial, self._conn)
        self.poll_interval = poll_interval if poll_interval and poll_interval > 0 else 1.0

    def initial_net_bytes(self) -> bytes:
        return self.take_net_out()

    def from_network(self, data: bytes) -> bytes:
        # filter() yields the serial-bound bytes; telnet replies go to _conn.write
        return b"".join(self._pm.filter(data))

    def from_serial(self, data: bytes) -> bytes:
        return b"".join(self._pm.escape(data))

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
