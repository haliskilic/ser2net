"""read_only must block RFC2217 control side-effects (H2).

A read-only mapping is supposed to prevent the network client from affecting the
serial device. But RFC2217 SET-BAUDRATE/DATASIZE/PARITY/STOPSIZE/CONTROL commands
are applied as a *side effect* of parsing the network stream (PortManager.filter
writes straight onto the live serial), so suppressing only the data payload left
the control plane wide open. These tests verify the read-only serial proxy drops
those mutations while still allowing reads (so modem-state notifications work).

Run: python3 tests/test_rfc2217_readonly.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.protocols.rfc2217 import _ModemSafeSerial, Rfc2217Session


class FakeSerial:
    """Minimal serial.Serial stand-in exposing the attributes RFC2217 touches."""

    def __init__(self):
        self.baudrate = 115200
        self.bytesize = 8
        self.parity = "N"
        self.stopbits = 1
        self.xonxoff = False
        self.rtscts = False
        self.dsrdtr = False
        self.rts = True
        self.dtr = True
        self.break_condition = False
        self.cts = self.dsr = self.ri = self.cd = False
        self.purges = 0

    def reset_input_buffer(self):
        self.purges += 1

    def reset_output_buffer(self):
        self.purges += 1


# ---- proxy-level: the security primitive ----------------------------------

def test_proxy_readonly_blocks_writes_allows_reads():
    fake = FakeSerial()
    proxy = _ModemSafeSerial(fake, read_only=True)

    proxy.baudrate = 9600           # SET-BAUDRATE
    proxy.parity = "E"              # SET-PARITY
    proxy.stopbits = 2
    proxy.bytesize = 7
    proxy.rtscts = True
    proxy.rts = False               # SET-CONTROL line toggles
    proxy.dtr = False
    proxy.break_condition = True
    proxy.reset_input_buffer()      # PURGE-DATA
    proxy.reset_output_buffer()

    assert fake.baudrate == 115200, "read-only proxy let the client change baudrate"
    assert fake.parity == "N" and fake.stopbits == 1 and fake.bytesize == 8
    assert fake.rtscts is False
    assert fake.rts is True and fake.dtr is True and fake.break_condition is False
    assert fake.purges == 0, "read-only proxy let the client purge buffers"
    # reads still pass through (needed for NOTIFY-MODEMSTATE)
    fake.cts = True
    assert proxy.cts is True, "read-only proxy blocked a modem-line read"
    print("read-only proxy: writes/purges blocked, reads pass through  OK")


def test_proxy_writable_passes_through():
    fake = FakeSerial()
    proxy = _ModemSafeSerial(fake, read_only=False)
    proxy.baudrate = 9600
    proxy.rts = False
    assert fake.baudrate == 9600 and fake.rts is False, "writable proxy should delegate writes"
    print("writable proxy: SET-* delegated to the live serial  OK")


# ---- end-to-end: a real RFC2217 SET_BAUDRATE command through the session ----

COM_PORT_OPTION = 44
SET_BAUDRATE = 1
IAC, SB, SE = 255, 250, 240


def _set_baudrate_cmd(baud: int) -> bytes:
    payload = baud.to_bytes(4, "big")          # 9600 -> 00 00 25 80 (no 0xFF, no escaping needed)
    return bytes([IAC, SB, COM_PORT_OPTION, SET_BAUDRATE]) + payload + bytes([IAC, SE])


def test_session_readonly_ignores_set_baudrate():
    fake = FakeSerial()
    session = Rfc2217Session(fake, read_only=True)
    session.from_network(_set_baudrate_cmd(9600))
    assert fake.baudrate == 115200, "read-only RFC2217 session applied SET-BAUDRATE to the device"
    print("read-only RFC2217 session: SET-BAUDRATE has no effect on the device  OK")


def test_session_writable_applies_set_baudrate():
    fake = FakeSerial()
    session = Rfc2217Session(fake, read_only=False)
    session.from_network(_set_baudrate_cmd(9600))
    assert fake.baudrate == 9600, "writable RFC2217 session failed to apply SET-BAUDRATE"
    print("writable RFC2217 session: SET-BAUDRATE applied to the device  OK")


def main():
    test_proxy_readonly_blocks_writes_allows_reads()
    test_proxy_writable_passes_through()
    test_session_readonly_ignores_set_baudrate()
    test_session_writable_applies_set_baudrate()
    print("\nPASS: read-only RFC2217 control-plane block (H2)")


if __name__ == "__main__":
    main()
