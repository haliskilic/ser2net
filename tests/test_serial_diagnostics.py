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
  - what else may use a port (Linux, from stand-in /sys, /run/systemd and /run/udev
    folders): the kernel console, a login prompt (serial-getty / getty unit running), and
    ModemManager for a ttyACM port unless udev tells it to ignore the port; /dev/serial0
    style links resolve to their tty. The ports page shows them as badges (plus "no
    access" for a port this process may not open), and a mapping that has such a port open
    has them in its warnings (logged once);
  - a port that went away and opened again on the very next try with the same device
    number was hung up (a login prompt's vhangup), not unplugged: counted (`hangups`),
    logged and shown; a real unplug (the next open fails) is not;
  - keeping other programs off the port (opt-in, POSIX): with tiocexcl another program's
    open of the PTY fails with EBUSY while the mapping has it open, and works again once
    it is closed; with respect_uucp_locks a port a running program holds a UUCP lock file
    on (HDB or binary, under any of its names) is not opened, and the error and the hint
    name the program; a lock whose process is gone is ignored and left in place; both off
    (the default, and on Windows) change nothing;

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


# ------------------------------------------------------------------ what else uses a port
@contextlib.contextmanager
def _system(folder, consoles="tty1", units=(), udev=None):
    """Stand-in /sys/class/tty/console/active, /run/systemd/units and /run/udev/data."""
    run = os.path.join(folder, "units")
    data = os.path.join(folder, "udev")
    os.makedirs(run, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    active = os.path.join(folder, "console-active")
    with open(active, "w") as fh:
        fh.write(consoles + "\n")
    for unit in units:
        os.symlink("/dev/null", os.path.join(run, f"invocation:{unit}"))
    for name, text in (udev or {}).items():
        with open(os.path.join(data, name), "w") as fh:
            fh.write(text)
    with mock.patch.object(portdiag, "CONSOLE_ACTIVE", active), \
            mock.patch.object(portdiag, "SYSTEMD_UNITS", run), \
            mock.patch.object(portdiag, "UDEV_DATA", data):
        yield


def test_port_warnings():
    if not sys.platform.startswith("linux"):
        assert portdiag.port_warnings("/dev/ttyS0") == []
        print("SKIP (part): console / login prompt / ModemManager are looked for on Linux only")
        return
    folder = tempfile.mkdtemp(prefix="s2n_diag_")
    try:
        dev = os.path.join(folder, "dev")
        os.makedirs(dev)
        for name in ("ttyS0", "ttyAMA0", "ttyACM0", "ttyACM1", "ttyUSB0"):
            open(os.path.join(dev, name), "w").close()
        os.symlink(os.path.join(dev, "ttyAMA0"), os.path.join(dev, "serial0"))
        sys_ = os.path.join(folder, "sys")
        with _system(sys_, consoles="tty1 ttyAMA0",
                     units=("serial-getty@ttyAMA0.service", "getty@ttyS0.service",
                            "ModemManager.service"),
                     udev={"c0:0": "E:ID_VENDOR=x\n"}):
            w = portdiag.port_warnings
            assert w(os.path.join(dev, "serial0")) == ["console", "getty"]  # its ttyAMA0
            assert w(os.path.join(dev, "ttyS0")) == ["getty"]
            assert w(os.path.join(dev, "ttyACM0")) == ["modemmanager"]
            assert w(os.path.join(dev, "ttyUSB0")) == [], "ModemManager: ttyACM only"
            notes = portdiag.port_notes(os.path.join(dev, "serial0"))
            assert [n.text() for n in notes] == [
                "ttyAMA0 is also the kernel console: the kernel writes its messages to it "
                "(remove console=ttyAMA0 from the kernel command line; Raspberry Pi: "
                "raspi-config, Interface Options, Serial Port)",
                "a login prompt runs on this port (serial-getty@ttyAMA0.service): it reads from "
                "the port and hangs it up when it restarts (systemctl disable --now "
                "serial-getty@ttyAMA0.service, or systemctl mask serial-getty@ttyAMA0.service)"]
        with _system(os.path.join(folder, "sys2"), units=("ModemManager.service",),
                     udev={"c0:0": "E:ID_MM_DEVICE_IGNORE=1\n"}):
            assert portdiag.port_warnings(os.path.join(dev, "ttyACM1")) == [], "ignored"
        with _system(os.path.join(folder, "sys3")):  # nothing runs: nothing to say
            assert portdiag.port_warnings(os.path.join(dev, "ttyAMA0")) == []
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("Linux: the kernel console, a login prompt, ModemManager on ttyACM (unless udev "
          "says to ignore it) are found, /dev/serial0 resolved to its tty  OK")


def test_ports_page():
    from serial.tools import list_ports

    from app.engine import portlist
    from app.web.server import make_templates
    port = types.SimpleNamespace(device="/dev/ttyAMA0", name="ttyAMA0", description="UART",
                                 hwid="", vid=None, pid=None, serial_number=None,
                                 manufacturer=None, product=None, location=None)
    with mock.patch.object(list_ports, "comports", lambda: [port]), \
            mock.patch.object(portdiag, "port_warnings", lambda d: ["console", "getty"]), \
            mock.patch.object(portdiag, "access", lambda d: False):
        ports = portlist.snapshot()
    assert ports[0]["warnings"] == ["console", "getty"] and ports[0]["access"] is False, ports
    tpl = make_templates().env.get_template("_ports_table.html")
    html = tpl.render(ports=ports, show_all=False, hidden=0, in_use=set())
    for label in ("kernel console", "login prompt", "no access"):
        assert f">{label}</span>" in html, (label, html)
    assert "ModemManager</span>" not in html
    tok = i18n.set_language("tr")
    try:
        html = tpl.render(ports=ports, show_all=False, hidden=0, in_use=set())
    finally:
        i18n.reset(tok)
    assert f">{TR['kernel console']}</span>" in html and f">{TR['no access']}</span>" in html
    ports[0].pop("warnings")  # a port list without them (an older peer's): no badges
    ports[0].pop("access")
    html = tpl.render(ports=ports, show_all=False, hidden=0, in_use=set())
    assert "tag-warn" not in html
    print("ports page: what else uses a port and a port ser2net may not open are shown as "
          "badges (TR too)  OK")


async def _open_port_notes(path, folder):
    logs = []
    status = RunnerStatus()
    stop = asyncio.Event()
    link = SerialLink(SerialSettings(port=path), status, stop, logs.append, owner="console")
    name = os.path.basename(os.path.realpath(path))
    with _system(folder, consoles=f"tty0 {name}"):
        task = asyncio.ensure_future(link.run(link.hold_open))
        try:
            await _until(lambda: status.state == "running")
            assert [n.kind for n in status.warnings] == ["console"], status.warnings
            assert status.as_dict()["warnings"][0].startswith(f"{name} is also the kernel "
                                                              "console"), status.as_dict()
            assert sum(m.startswith(f"serial warning: {name} is also the kernel console")
                       for m in logs) == 1, logs
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await link.close()


def test_open_port_says_what_else_uses_it():
    if not sys.platform.startswith("linux"):
        print("SKIP (part): the open port's console check is Linux only")
        return
    pty = Pty()
    folder = tempfile.mkdtemp(prefix="s2n_diag_")
    try:
        asyncio.run(_open_port_notes(pty.path, folder))
    finally:
        pty.close()
        shutil.rmtree(folder, ignore_errors=True)
    print("a mapping that has the kernel console open: in its warnings, logged once  OK")


async def _hangup(pty):
    from app.engine import serial_link
    logs = []
    status = RunnerStatus()
    stop = asyncio.Event()
    link = SerialLink(SerialSettings(port=pty.path), status, stop, logs.append)
    sessions = []

    async def session(reader, writer):  # hung up once: the port reads as gone
        sessions.append(1)
        if len(sessions) == 1:
            raise serial.SerialException("device reports readiness to read but returned no "
                                         "data (device disconnected or multiple access on "
                                         "port?)")
        await link.hold_open(reader, writer)

    def fast(self):
        self.attempt += 1
        return 0.05

    with mock.patch.object(serial_link.Backoff, "pause", fast):
        task = asyncio.ensure_future(link.run(session))
        try:
            await _until(lambda: len(sessions) == 2 and status.state == "running")
            assert (status.device_lost, status.hangups) == (1, 1), status
            assert any("was hung up while open and could be reopened at once" in m
                       for m in logs), logs
            assert status.as_dict()["hangups"] == 1
            assert status.as_dict()["warnings"] == [
                "hung up 1 time(s) while open, and could be reopened at once: another program "
                "may be using this port (a login prompt on it?)"], status.as_dict()
            pty.close()  # now unplugged for real: the next open fails, no hang-up
            await _until(lambda: status.device_lost == 2 and status.open_failures >= 1)
            assert status.hangups == 1, status
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await link.close()


def test_hangup_told_from_an_unplug():
    if os.name != "posix":
        print("SKIP (part): the hang-up check is POSIX only (device numbers)")
        return
    pty = Pty()
    try:
        asyncio.run(_hangup(pty))
    finally:
        with contextlib.suppress(OSError):
            pty.close()
    print("a port lost and opened again at once with the same device number: a hang-up "
          "(counted, logged, shown); a real unplug is not  OK")


# ------------------------------------------------------------------ keeping programs off
async def _tiocexcl(path):
    settings = SerialSettings.from_dict({"port": path, "advanced": {"tiocexcl": True}})
    _r, writer, ser, _d = await serial_io.open_serial(settings)
    try:
        assert ser._excl
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            assert e.errno == errno.EBUSY, e
        else:
            os.close(fd)
            raise AssertionError("another program's open must fail while it is open")
    finally:
        writer.transport.abort()
        await asyncio.sleep(0.05)
    os.close(os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))  # closed: free again
    plain = SerialSettings.from_dict({"port": path})
    _r, writer, ser, _d = await serial_io.open_serial(plain)
    try:
        assert not ser._excl
        os.close(os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))  # off: shared
    finally:
        writer.transport.abort()
        await asyncio.sleep(0.05)


def test_tiocexcl():
    if os.name != "posix":
        print("SKIP (part): TIOCEXCL is POSIX only (Windows ports are exclusive anyway)")
        return
    if os.geteuid() == 0:
        print("SKIP (part): root opens a TIOCEXCL port anyway")
        return
    pty = Pty()
    try:
        asyncio.run(_tiocexcl(pty.path))
    finally:
        pty.close()
    print("TIOCEXCL: another program's open is refused (EBUSY) while the mapping has the "
          "port, and works once it is closed; off by default  OK")


async def _uucp(pty, folder):
    import subprocess
    locks = os.path.join(folder, "lock")
    os.makedirs(locks)
    alias = os.path.join(folder, "by-id-name")
    os.symlink(pty.path, alias)
    name = os.path.basename(pty.path)  # /dev/pts/N: LCK..N
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = os.path.join(locks, f"LCK..{name}")
        with open(lock, "w") as fh:
            fh.write(f"{holder.pid:10d}\n")  # HDB, as minicom writes it
        settings = SerialSettings.from_dict({"port": alias,
                                             "advanced": {"respect_uucp_locks": True}})
        with mock.patch.object(portdiag, "LOCK_DIRS", (locks, locks)):
            try:
                await serial_io.open_serial(settings)
            except serial.SerialException as e:
                refused = e
            else:
                raise AssertionError("a port locked by a running program must not open")
            assert refused.errno == errno.EBUSY, refused
            assert f"PID {holder.pid}" in str(refused) and lock in str(refused), str(refused)
            if sys.platform.startswith("linux"):  # named from /proc/<pid>/comm
                assert f"python3.{sys.version_info.minor} (PID" in str(refused) or \
                    "python3 (PID" in str(refused) or "python (PID" in str(refused), refused
            note = portdiag.explain(refused, alias)
            assert note is not None and note.kind == "uucp" and note.args[1] == holder.pid, note
            assert f"(PID {holder.pid}) is using this port" in note.text(), note.text()
            # the same lock read through the default: off, the port opens
            _r, writer, _s, _d = await serial_io.open_serial(SerialSettings(port=alias))
            writer.transport.abort()
            await asyncio.sleep(0.05)
            # binary (old style) lock file
            with open(lock, "wb") as fh:
                fh.write(holder.pid.to_bytes(4, sys.byteorder))
            assert portdiag.uucp_lock(alias).pid == holder.pid
            holder.kill()
            holder.wait()
            # its process is gone: the port opens, the lock stays where it is
            _r, writer, _s, _d = await serial_io.open_serial(settings)
            writer.transport.abort()
            await asyncio.sleep(0.05)
            assert os.path.exists(lock), "never removed"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_uucp_locks():
    if os.name != "posix":
        print("SKIP (part): UUCP lock files are POSIX only")
        return
    pty = Pty()
    folder = tempfile.mkdtemp(prefix="s2n_diag_")
    try:
        asyncio.run(_uucp(pty, folder))
    finally:
        pty.close()
        shutil.rmtree(folder, ignore_errors=True)
    print("UUCP lock files: a port a running program has locked (HDB or binary, under any of "
          "its names) is not opened and the program is named; a stale lock is ignored and "
          "left alone; off by default  OK")


if __name__ == "__main__":
    test_lines_set_before_open()
    test_refused_settings_stand_in()
    test_refused_rs485_on_a_pty()
    test_status_and_log()
    test_dashboard_shows_warnings()
    test_no_access_hints()
    test_port_in_use_hints()
    test_windows_in_use_hint()
    test_port_warnings()
    test_ports_page()
    test_open_port_says_what_else_uses_it()
    test_hangup_told_from_an_unplug()
    test_tiocexcl()
    test_uucp_locks()
    print("\nPASS: what ser2net tells about a mapping's serial port")
