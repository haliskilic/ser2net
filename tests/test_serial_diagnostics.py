"""What ser2net tells about a mapping's serial port: why it can't be opened, and settings the
port did not take.

  - a hint next to the raw error (status `hint`; `last_error` is kept as it is), when the
    error says more than pyserial's text: no access (EACCES) names the group the device
    belongs to (read from the device: dialout, uucp, a bare gid when the group has no name
    here) and what is missing: the user is not in it (usermod / SupplementaryGroups), was
    added after the process started (restart), the container lacks it (--group-add), the
    group may not open it (its mode), or only root may; a port in use (EBUSY, flock's
    EAGAIN, Windows' "Access is denied") names the mapping of this process that has it open
    under any of its names (by device number: a symlink counts), else the kind of lock
    another program holds. Two exclusive mappings on one PTY: the second says which has it;

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
import contextlib
import errno
import json
import os
import shutil
import struct
import sys
import tempfile
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import serial
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


# ------------------------------------------------------------------ why an open failed
def _stat(gid, mode=0o20660, uid=0):
    return os.stat_result((mode, 1, 1, 1, uid, gid, 0, 0, 0, 0))


@contextlib.contextmanager
def _account(groups=(), listed=(), egid=1000, uid=1000, container=False, names=None):
    """This process: user 'svc' (uid), its groups; `listed`: the groups the group database
    lists it in (a usermod after the start); `names`: gid -> group name (else unknown)."""
    import grp
    import pwd
    names = {20: "dialout", 14: "uucp", 0: "root"} if names is None else names

    def getgrgid(gid):
        if gid not in names:
            raise KeyError(gid)
        return types.SimpleNamespace(gr_name=names[gid])

    exists = os.path.exists
    with mock.patch.object(portdiag.os, "geteuid", lambda: uid), \
            mock.patch.object(portdiag.os, "getegid", lambda: egid), \
            mock.patch.object(portdiag.os, "getgroups", lambda: list(groups)), \
            mock.patch.object(portdiag.os, "getgrouplist",
                              lambda user, base: [base, *groups, *listed]), \
            mock.patch.object(pwd, "getpwuid",
                              lambda u: types.SimpleNamespace(pw_name="svc", pw_gid=egid)), \
            mock.patch.object(grp, "getgrgid", getgrgid), \
            mock.patch.object(portdiag.os.path, "exists",
                              lambda p: container if p in ("/.dockerenv", "/run/.containerenv")
                              else exists(p)):
        yield


def _denied(device):
    return serial.SerialException(errno.EACCES, f"could not open port {device}: [Errno 13] "
                                                f"Permission denied: '{device}'")


def test_no_access_hints():
    if os.name != "posix":
        print("SKIP (part): the no-access hints are POSIX only")
        return
    dev = "/dev/ttyUSB0"
    cases = [  # (stat, account, kind, text)
        (_stat(20), {}, "group", "/dev/ttyUSB0 belongs to group dialout (gid 20), and user svc "
         "is not in it: add the user to the group (usermod -aG dialout svc; the systemd unit: "
         "SupplementaryGroups=dialout), then restart ser2net"),
        (_stat(14), {}, "group", "group uucp (gid 14)"),
        (_stat(986), {}, "group", "group 986 (gid 986)"),  # no name for it here
        (_stat(20), {"listed": (20,)}, "restart", "user svc was added to group dialout after "
         "ser2net started"),
        (_stat(18), {"container": True, "names": {}}, "container_group",
         "group 18 (gid 18), which this container doesn't have: start it with --group-add 18"),
        (_stat(20, mode=0o20600), {"groups": (20,)}, "mode", "/dev/ttyUSB0 is crw------- "
         "(group dialout): this process is in that group"),
        (_stat(0), {}, "root_group", "only root may open /dev/ttyUSB0"),
    ]
    for st, account, kind, text in cases:
        with _account(**account), mock.patch.object(portdiag.os, "stat", lambda p, _s=st: _s):
            note = portdiag.explain(_denied(dev), dev)
        assert note is not None and note.kind == kind, (account, note)
        assert text in note.text(), (text, note.text())
        tok = i18n.set_language("tr")
        try:
            assert note.text(i18n.gettext) != note.text(), "translated"
        finally:
            i18n.reset(tok)
    with mock.patch.object(portdiag.os, "stat", side_effect=FileNotFoundError(2, "gone")):
        assert portdiag.explain(_denied(dev), dev) is None
    assert portdiag.explain(serial.SerialException(errno.EIO, "x"), dev) is None
    print("no access: the device's group (dialout / uucp / a bare gid) and what is missing: "
          "the user's membership, a restart, --group-add, the device's mode, root only  OK")


async def _two_mappings_one_port(path, alias):
    from app.engine.bridge import MappingRunner
    logs = []
    first = MappingRunner(MappingConfig.from_dict(
        {"name": "first", "serial": {"port": path},
         "network": {"mode": "client", "remote_host": "127.0.0.1", "remote_port": 9}}),
        logger=logs.append)
    second = MappingRunner(MappingConfig.from_dict(
        {"name": "second", "serial": {"port": alias},
         "network": {"mode": "client", "remote_host": "127.0.0.1", "remote_port": 9}}),
        logger=logs.append)
    tasks = []
    try:
        tasks.append(asyncio.ensure_future(first._link.run(first._serial_session)))
        await _until(lambda: first.status.state == "running")
        tasks.append(asyncio.ensure_future(second._link.run(second._serial_session)))
        await _until(lambda: second.status.hint is not None)
        st = second.status.as_dict()
        assert st["state"] == "reconnecting", st
        assert f"Could not exclusively lock port {alias}" in st["last_error"], st
        assert st["hint"] == "mapping 'first' has this port open", st
        assert first.status.as_dict()["hint"] == ""
        return st
    finally:
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await first._link.close()
        await second._link.close()


async def _until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def test_port_in_use_hints():
    if os.name != "posix":
        print("SKIP (part): the PTY checks of a port in use are POSIX only")
        return
    pty = Pty()
    folder = tempfile.mkdtemp(prefix="s2n_diag_")
    alias = os.path.join(folder, "usb-FTDI-if00-port0")  # a by-id style name of the same port
    os.symlink(pty.path, alias)
    try:
        st = asyncio.run(_two_mappings_one_port(pty.path, alias))
        held = serial_compat.PosixSerial(pty.path, timeout=0, write_timeout=0, exclusive=True)
        try:
            try:
                serial_compat.PosixSerial(alias, timeout=0, write_timeout=0, exclusive=True)
            except serial.SerialException as e:
                locked = e
            else:
                raise AssertionError("a second exclusive open must fail")
            note = portdiag.explain(locked, alias)  # nobody here registered `held`
            assert note is not None and note.kind == "flock", note
            assert "another program has locked this port (flock)" in note.text()
            portdiag.opened(held, "plc", pty.path)
            assert portdiag.explain(locked, alias) == portdiag.Note("in_use_by", ("plc",))
        finally:
            held.close()
        assert portdiag.holder(alias) == "", "a closed port has no holder"
        busy = serial.SerialException(errno.EBUSY, "could not open port x: [Errno 16] busy")
        assert portdiag.explain(busy, alias).kind == "exclusive"
    finally:
        pty.close()
        shutil.rmtree(folder, ignore_errors=True)

    m = MappingConfig.from_dict({"name": "second", "serial": {"port": alias}})
    from app.web.server import make_templates
    html = make_templates().env.get_template("_mappings_body.html").render(
        mappings=[m], statuses={m.id: st}, can_edit=False)
    assert "Could not exclusively lock port" in html and \
        "mapping &#39;first&#39; has this port open" in html, html
    print("port in use: the mapping that has it open (also under another name of the port), "
          "else the kind of lock (flock / TIOCEXCL); the dashboard shows the hint under the "
          "raw error  OK")


def test_windows_in_use_hint():
    text = ("could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5)")
    held = OrderedPort()  # (any pyserial object; registered by weak reference)
    held.is_open = True
    with mock.patch.object(portdiag.os, "name", "nt"):
        note = portdiag.explain(serial.SerialException(text), "COM9")
        assert note == portdiag.Note("in_use_windows"), note
        portdiag.opened(held, "plc", "\\\\.\\com9")
        assert portdiag.explain(serial.SerialException(text), "COM9") == \
            portdiag.Note("in_use_by", ("plc",))
        missing = serial.SerialException("could not open port 'COM7': FileNotFoundError(2, "
                                         "'The system cannot find the file specified.', None, 2)")
        assert portdiag.explain(missing, "COM7") is None and portdiag.missing(missing)
    print("Windows: 'Access is denied' is a port another program (or a mapping here) has "
          "open; FileNotFoundError is a missing device  OK")


if __name__ == "__main__":
    test_lines_set_before_open()
    test_refused_settings_stand_in()
    test_refused_rs485_on_a_pty()
    test_status_and_log()
    test_dashboard_shows_warnings()
    test_no_access_hints()
    test_port_in_use_hints()
    test_windows_in_use_hint()
    print("\nPASS: what ser2net tells about a mapping's serial port")
