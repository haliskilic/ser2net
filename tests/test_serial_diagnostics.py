"""What ser2net tells about a mapping's serial port: settings the port did not take.

  - RTS / DTR set to on or off are given to pyserial before the port opens (so pyserial
    opens the port with them, instead of switching both on first), and set again once it is
    open; a line the port can't switch (a PTY has no modem lines) doesn't stop the port from
    opening and is reported (portdiag.Note);
  - an RS-485 mode the port refuses doesn't stop it from opening either: it is reported and
    taken back, so a later change of the port's settings works (an RFC 2217 client's
    SET-BAUDRATE, which pyserial would otherwise refuse for as long as the refused mode is
    kept): on a Linux PTY (TIOCSRS485 -> ENOTTY, as on most USB adapters) and with a
    stand-in port (any OS: the Windows path);
  - the runner's status has them as `warnings` while the port is open (English for the REST
    API, the web UI's language on the dashboard), the mapping's log says them once (not at
    every reopen), and the dashboard shows them under the state.
Ports are PTYs (POSIX) and stand-ins; nothing needs serial hardware.

Run: python3 tests/test_serial_diagnostics.py
"""
import asyncio
import json
import os
import struct
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serial.serialutil import SerialBase

from app import i18n
from app.config import MappingConfig, SerialSettings
from app.engine import portdiag, serial_compat, serial_io
from app.engine.bridge import RunnerStatus
from app.engine.protocols.rfc2217 import Rfc2217Session
from app.engine.serial_link import SerialLink

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TR = json.load(open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8"))
IAC, SB, SE = 255, 250, 240


def set_baudrate(baud: int) -> bytes:
    """RFC 2217 SET-BAUDRATE, as pyserial's rfc2217:// client sends it when it opens."""
    body = bytes([44, 1]) + struct.pack("!I", baud)
    return bytes([IAC, SB]) + body.replace(b"\xff", b"\xff\xff") + bytes([IAC, SE])


class Pty:
    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)

    def close(self):
        os.close(self.master)


class OrderedPort(SerialBase):
    """A pyserial port that records when the modem lines are set: before or after open()."""
    events: list = []

    def open(self):
        self.events.append(("open", self._rts_state, self._dtr_state))
        self.is_open = True

    def close(self):
        self.is_open = False

    def _reconfigure_port(self, force_update=False):
        pass

    def _update_rts_state(self):
        self.events.append(("rts", self._rts_state))

    def _update_dtr_state(self):
        self.events.append(("dtr", self._dtr_state))


def test_lines_set_before_open():
    OrderedPort.events = []
    with mock.patch.object(serial_compat, "serial_class", lambda: OrderedPort):
        port = serial_compat._open_port("COM9", rts=False, dtr=True, baudrate=9600)
    assert port.is_open and OrderedPort.events == [("open", False, True)], OrderedPort.events
    OrderedPort.events = []
    with mock.patch.object(serial_compat, "serial_class", lambda: OrderedPort):
        serial_compat._open_port("COM9", baudrate=9600)  # keep: pyserial's default (on)
    assert OrderedPort.events == [("open", True, True)], OrderedPort.events
    print("RTS / DTR on or off: given to pyserial before the port opens; keep: its default  OK")


class RefusingPort(SerialBase):
    """An open port whose driver takes no RS-485 mode (pyserial stores the mode first, then
    every reconfiguration fails with it: Windows with delays, Linux without TIOCSRS485) and
    has no modem lines."""

    def __init__(self):
        super().__init__()
        self.is_open = True

    def _reconfigure_port(self, force_update=False):
        if self._rs485_mode is not None:
            raise ValueError("Failed to set RS485 mode: [Errno 25] Inappropriate ioctl")

    def _update_rts_state(self):
        raise OSError(25, "Inappropriate ioctl for device")

    def _update_dtr_state(self):
        pass


def _stand_in_stream(ser):
    async def open_stream(device, limit, **kwargs):
        transport = types.SimpleNamespace(serial=ser)
        return asyncio.StreamReader(), types.SimpleNamespace(transport=transport)
    return open_stream


def test_refused_settings_stand_in():
    ser = RefusingPort()
    settings = SerialSettings.from_dict({"port": "COM9", "rts_on_open": "off",
                                         "dtr_on_open": "on",
                                         "advanced": {"rs485_enabled": True}})

    async def run():
        with mock.patch.object(serial_compat, "open_stream", _stand_in_stream(ser)):
            return await serial_io.open_serial(settings)

    _r, _w, got, device = asyncio.run(run())
    assert got is ser and device == "COM9"
    notes = serial_io.open_notes(ser)
    assert [n.kind for n in notes] == ["line", "rs485"], notes
    assert notes[0].text() == "RTS could not be switched off: [Errno 25] Inappropriate ioctl " \
                              "for device", notes[0].text()
    assert notes[1].text().startswith("RS-485 mode is off: the port did not take it (Failed "
                                      "to set RS485 mode"), notes[1].text()
    assert ser.rs485_mode is None, "the refused mode is taken back"
    ser.baudrate = 19200  # pyserial would refuse it while the refused mode is kept
    assert ser.baudrate == 19200
    assert serial_io.open_notes(object()) == []
    print("a port refusing RS-485 and RTS (stand-in): it opens, both are noted, RS-485 is "
          "taken back and the next setting change works  OK")


async def _pty_rs485_then_rfc2217(path):
    settings = SerialSettings.from_dict({"port": path, "baudrate": 9600, "rts_on_open": "on",
                                         "advanced": {"rs485_enabled": True}})
    _reader, writer, ser, _dev = await serial_io.open_serial(settings)
    try:
        kinds = [n.kind for n in serial_io.open_notes(ser)]
        assert kinds == ["line", "rs485"], serial_io.open_notes(ser)
        assert ser.rs485_mode is None
        logs = []
        session = Rfc2217Session(ser, log=logs.append)
        session.take_net_out()  # the opening negotiation
        session.from_network(set_baudrate(19200))
        assert ser.baudrate == 19200 and not logs, (ser.baudrate, logs)
    finally:
        writer.transport.abort()
        await asyncio.sleep(0.05)


def test_refused_rs485_on_a_pty():
    if not sys.platform.startswith("linux"):
        print("SKIP (part): the PTY check of RS-485 is Linux only")
        return
    pty = Pty()
    try:
        asyncio.run(_pty_rs485_then_rfc2217(pty.path))
    finally:
        pty.close()
    print("Linux PTY: RS-485 refused (ENOTTY) and RTS without modem lines are noted, the port "
          "opens, and an RFC 2217 SET-BAUDRATE then changes the baud rate  OK")


async def _link_reports_notes():
    logs = []
    status = RunnerStatus()
    stop = asyncio.Event()
    link = SerialLink(SerialSettings(port="COM9"), status, stop, logs.append)
    notes = [portdiag.Note("rs485", ("ENOTTY",))]
    for _ in range(2):  # opened twice with the same refusal: logged once
        ser = RefusingPort()
        serial_io._NOTES[ser] = notes
        link.attach(asyncio.StreamReader(), types.SimpleNamespace(), ser, "COM9")
        assert status.warnings == tuple(notes) and status.as_dict()["warnings"] == [
            "RS-485 mode is off: the port did not take it (ENOTTY)"], status.as_dict()
        link.reader = link.writer = None  # (nothing to close)
        await link.close()
        assert status.warnings == () and status.as_dict()["warnings"] == []
    said = [m for m in logs if m.startswith("serial warning:")]
    assert said == ["serial warning: RS-485 mode is off: the port did not take it (ENOTTY)"], logs
    status.warnings = tuple(notes)
    token = i18n.set_language("tr")
    try:
        tr = status.as_dict()["warnings"]
    finally:
        i18n.reset(token)
    assert tr == [TR["RS-485 mode is off: the port did not take it ({0})"].format("ENOTTY")], tr
    side = portdiag.Note("side", ("B", notes[0]))
    assert side.text() == "port B: RS-485 mode is off: the port did not take it (ENOTTY)"


def test_status_and_log():
    asyncio.run(_link_reports_notes())
    print("status: warnings while the port is open (English; Turkish in a Turkish web "
          "request), cleared when it closes; the log says them once  OK")


def test_dashboard_shows_warnings():
    from app.web.server import make_templates
    env = make_templates().env
    m = MappingConfig.from_dict({"name": "rs", "serial": {"port": "/dev/ttyUSB0"}})
    st = RunnerStatus(state="running", warnings=(portdiag.Note("rs485", ("ENOTTY",)),))
    html = env.get_template("_mappings_body.html").render(
        mappings=[m], statuses={m.id: st.as_dict()}, can_edit=False)
    assert "RS-485 mode is off: the port did not take it (ENOTTY)" in html, html
    assert 'class="warn-small"' in html
    st.state = "reconnecting"
    html = env.get_template("_mappings_body.html").render(
        mappings=[m], statuses={m.id: st.as_dict()}, can_edit=False)
    assert "RS-485 mode is off" not in html, "shown for a port that is open only"
    print("dashboard: a running mapping's warnings are shown under its state  OK")


if __name__ == "__main__":
    test_lines_set_before_open()
    test_refused_settings_stand_in()
    test_refused_rs485_on_a_pty()
    test_status_and_log()
    test_dashboard_shows_warnings()
    print("\nPASS: what ser2net tells about a mapping's serial port")
