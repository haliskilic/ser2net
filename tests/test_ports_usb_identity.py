"""Serial port list and binding a mapping to a USB device's identity.

Checks:
  * Linux placeholder ttyS ports (sysfs type 0 = no UART) are marked hardware=False
    and hidden from the UI by default (shown on request); /dev/serial/by-id names are
    attached to their real device;
  * the mapping form's "find this device by its USB identity" sets serial.match from
    the selected port (VID/PID + serial number), clears it when unticked, keeps it when
    the device is unplugged, refuses a port without a USB identity, and a form without
    the field (an older cluster peer) keeps the stored binding;
  * the engine then opens whatever device currently has that identity: only the known
    match keys count (a typo binds nothing), of several ports with the identity
    (identical adapters) the configured one is opened (also through a by-id style link),
    else one no other mapping has open, with a warning; a configured port that is another
    USB adapter now is not opened (device missing); serial numbers in any letter case.

Run: python3 tests/test_ports_usb_identity.py
"""
import asyncio
import errno
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

from _server import ROOT  # noqa: F401  (sys.path setup)

from app.config import AppConfig, MappingConfig, SerialSettings
from app.engine import portdiag, portlist, serial_io
from app.web.routes.forms import _preserve_unmanaged_fields
from app.web.routes.ops import apply_usb_match

FTDI = {"device": "/dev/ttyUSB3", "by_id": "/dev/serial/by-id/usb-FTDI_FT232R_A12345-if00-port0",
        "vid": "0403", "pid": "6001", "serial_number": "A12345", "hardware": None}
CH340 = {"device": "/dev/ttyUSB4", "by_id": None, "vid": "1a86", "pid": "7523",
         "serial_number": None, "hardware": None}
ONBOARD = {"device": "/dev/ttyS0", "by_id": None, "vid": None, "pid": None,
           "serial_number": None, "hardware": True}
PHANTOM = {"device": "/dev/ttyS7", "by_id": None, "vid": None, "pid": None,
           "serial_number": None, "hardware": False}


def test_sysfs_and_by_id():
    d = tempfile.mkdtemp(prefix="s2n_ports_")
    for name, typ in (("ttyS0", "4"), ("ttyS1", "0")):
        os.makedirs(os.path.join(d, "sys", name))
        with open(os.path.join(d, "sys", name, "type"), "w") as fh:
            fh.write(typ + "\n")
    import sys
    if sys.platform.startswith("linux"):
        assert portlist.has_hardware("/dev/ttyS0", os.path.join(d, "sys")) is True
        assert portlist.has_hardware("/dev/ttyS1", os.path.join(d, "sys")) is False
    assert portlist.has_hardware("/dev/ttyUSB0", os.path.join(d, "sys")) is None
    os.makedirs(os.path.join(d, "by-id"))
    target = os.path.join(d, "ttyUSB9")
    open(target, "w").close()
    os.symlink(target, os.path.join(d, "by-id", "usb-Prolific_PL2303-if00-port0"))
    links = portlist._by_id_links(os.path.join(d, "by-id"))
    assert links == {os.path.realpath(target): os.path.join(d, "by-id", "usb-Prolific_PL2303-if00-port0")}
    ports = [FTDI, ONBOARD, PHANTOM]
    assert portlist.visible(ports) == [FTDI, ONBOARD]
    assert portlist.visible(ports, show_all=True) == ports
    assert portlist.find(ports, FTDI["by_id"]) is FTDI and portlist.find(ports, "/dev/ttyUSB3") is FTDI
    assert portlist.find(ports, "/dev/nope") is None
    print("ports: placeholder ttyS hidden (sysfs type 0), by-id names attached  OK")


def mapping(port, match=None):
    return MappingConfig.from_dict({"id": "abcabcabcabc", "name": "m", "serial": {"port": port, "match": match or {}},
                                    "network": {"port": 4000, "bind_ip": "127.0.0.1"}})


def test_form_binding():
    form = {"serial_usb_match_shown": "1", "serial_usb_match": "on", "serial_b_usb_match_shown": "1"}
    m = mapping("/dev/serial/by-id/usb-FTDI_FT232R_A12345-if00-port0")
    apply_usb_match(m, form, [FTDI, CH340])
    assert m.serial.match == {"vid": "0403", "pid": "6001", "serial_number": "A12345"}, m.serial.match
    m = mapping("/dev/ttyUSB4")
    apply_usb_match(m, form, [FTDI, CH340])
    assert m.serial.match == {"vid": "1a86", "pid": "7523"}, "no serial number: VID/PID only"
    stored = mapping("/dev/ttyUSB3", {"vid": "0403", "pid": "6001", "serial_number": "A12345"})
    m = mapping("/dev/ttyUSB3")
    apply_usb_match(m, form, [CH340], existing=stored)  # unplugged now
    assert m.serial.match == stored.serial.match, "unplugged device keeps its binding"
    m = mapping("/dev/ttyS0")
    try:
        apply_usb_match(m, form, [ONBOARD])
    except ValueError as e:
        assert "no USB identity" in str(e)
    else:
        raise AssertionError("a non-USB port must be refused")
    m = mapping("/dev/ttyUSB3", {"vid": "0403", "pid": "6001"})
    apply_usb_match(m, {"serial_usb_match_shown": "1"}, [FTDI])
    assert m.serial.match == {}, "unticked = unbound"
    old_peer_form = {"name": "m"}  # no *_usb_match_shown: an older node's form
    edited = mapping("/dev/ttyUSB3")
    _preserve_unmanaged_fields(edited, stored, old_peer_form)
    apply_usb_match(edited, old_peer_form, [FTDI], stored)
    assert edited.serial.match == stored.serial.match, "older form must keep the binding"
    print("form: bind (VID/PID+serial), VID/PID only, unplugged keeps, non-USB refused, untick clears, old form keeps  OK")


def test_engine_resolves(monkeypatch_ports):
    m = mapping("/dev/ttyUSB0", {"vid": "0403", "pid": "6001", "serial_number": "A12345"})
    assert serial_io.resolve_device(m.serial) == "/dev/ttyUSB7", "engine must follow the identity"
    m = mapping("/dev/ttyUSB0", {"vid": "0403", "pid": "6001", "serial_number": "OTHER"})
    assert serial_io.resolve_device(m.serial) == "/dev/ttyUSB0", "no match -> configured path"
    print("engine: opens the device that has the identity now (renumbered ttyUSB0 -> ttyUSB7)  OK")


def _twin(device, serial=None):
    return {"device": device, "by_id": None, "vid": "1a86", "pid": "7523",
            "serial_number": serial, "location": None, "hardware": None}


def test_identity_resolution():
    ch340 = {"vid": "1a86", "pid": "7523"}
    twins = [_twin("/dev/ttyUSB0"), _twin("/dev/ttyUSB1"), _twin("/dev/ttyUSB2")]

    def side(port, match):
        return SerialSettings.from_dict({"port": port, "match": match})

    # only known keys count: a typo or another program's key name binds nothing
    for odd in ({"VID": "0403"}, {"serial": "A1"}, {"vid": ""}, "0403:6001"):
        assert serial_io.resolve(side("/dev/ttyACM0", odd), twins) == ("/dev/ttyACM0", []), odd
    # identical adapters: the configured one is opened when it is one of them
    for port in ("/dev/ttyUSB1", "/dev/ttyUSB2"):
        got = serial_io.resolve(side(port, ch340), twins)
        assert got == (port, ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]), got
    # ... also when it is set through a link that leads to it (by-id / by-path)
    if sys.platform != "win32":  # (symlinks need a privilege there)
        link = os.path.join(tempfile.mkdtemp(prefix="s2n_twins_"), "pci-0:2:1.0-port0")
        os.symlink("/dev/ttyUSB2", link)
        assert serial_io.resolve_device(side(link, ch340), twins) == "/dev/ttyUSB2"
    # the configured one is not there: the first one no other mapping has open
    with mock.patch.object(portdiag, "holder", lambda dev: "a" if dev == "/dev/ttyUSB0" else ""):
        assert serial_io.resolve_device(side("/dev/ttyUSB7", ch340), twins) == "/dev/ttyUSB1"
    with mock.patch.object(portdiag, "holder", lambda dev: "a"):  # all taken: the first
        assert serial_io.resolve_device(side("/dev/ttyUSB7", ch340), twins) == "/dev/ttyUSB0"
    # Windows names in another spelling are the configured port too
    com = [dict(_twin("COM3"), device="COM3"), dict(_twin("COM4"), device="COM4")]
    assert serial_io.resolve_device(side("com4", ch340), com) == "COM4"
    # serial numbers compare in any letter case
    ftdi = dict(FTDI, serial_number="a12345")
    assert serial_io.resolve_device(side("/dev/ttyUSB0", {"vid": "0403", "pid": "6001",
                                                          "serial_number": "A12345"}),
                                    [ftdi]) == "/dev/ttyUSB3"
    # the bound adapter is gone and its port is another USB adapter now: not opened
    bound = side("/dev/ttyUSB4", {"vid": "0403", "pid": "6001", "serial_number": "A12345"})
    try:
        serial_io.resolve(bound, [CH340])
    except serial_io.ForeignDevice as e:
        assert e.errno == errno.ENODEV and portdiag.missing(e), e
        assert "1a86:7523" in str(e) and "0403:6001 [A12345]" in str(e), e
    else:
        raise AssertionError("another adapter on the configured port must not be opened")
    # ... but a port that is not there, or has no USB identity, is opened by its name
    assert serial_io.resolve_device(bound, [FTDI]) == "/dev/ttyUSB3"
    assert serial_io.resolve_device(bound, []) == "/dev/ttyUSB4"
    onboard = side("/dev/ttyS0", {"vid": "0403", "pid": "6001"})
    assert serial_io.resolve_device(onboard, [ONBOARD]) == "/dev/ttyS0"
    print("engine: known match keys only, configured port first among identical adapters "
          "(also via a link), else a free one, another adapter on the port refused  OK")


def test_shared_identity_warning():
    """open_serial says when it had to guess among several ports with the identity."""
    if not hasattr(os, "openpty"):
        print("shared-identity warning: no PTYs here, skipped")
        return
    pairs = [os.openpty() for _ in range(2)]
    try:
        ptys = sorted(os.ttyname(slave) for _master, slave in pairs)
        listed = [_twin(ptys[0]), _twin(ptys[1])]
        settings = SerialSettings.from_dict({"port": "/dev/ttyUSB9", "exclusive": False,
                                             "match": {"vid": "1a86", "pid": "7523"}})

        async def run(port):
            settings.port = port
            with mock.patch.object(portlist, "recent", lambda max_age=5.0: listed):
                _r, writer, ser, device = await serial_io.open_serial(settings)
            try:
                return device, [n for n in serial_io.open_notes(ser) if n.kind == "identity_shared"]
            finally:
                writer.close()

        device, notes = asyncio.run(run("/dev/ttyUSB9"))
        assert device == ptys[0] and len(notes) == 1, (device, notes)
        assert notes[0].text().startswith("2 connected ports have the USB identity this port "
                                           "is bound to (1a86:7523)"), notes[0].text()
        assert "By USB socket" in notes[0].text()
        device, notes = asyncio.run(run(ptys[1]))  # the configured one: no guess, no warning
        assert device == ptys[1] and notes == [], (device, notes)
    finally:
        for master, slave in pairs:
            os.close(master)
            os.close(slave)
    print("open: a guess among identical adapters is reported once the port is open  OK")


def test_unknown_match_keys_noted():
    cfg = AppConfig.from_dict({"mappings": [
        {"id": "abcabcabcab1", "name": "typo", "serial": {"port": "/dev/ttyUSB0",
                                                           "match": {"VID": "0403"}}},
        {"id": "abcabcabcab2", "name": "extra", "serial": {
            "port": "/dev/ttyUSB1", "match": {"vid": "0403", "pid": "6001", "x": 1}}}]})
    notes = [n for n in cfg.load_notes if "USB identity" in n]
    assert len(notes) == 2 and "(VID)" in notes[0] and "opened by its name" in notes[0], notes
    assert "(x)" in notes[1] and "opened by its name" not in notes[1], notes
    assert cfg.mappings[0].serial.match == {"VID": "0403"}, "the stored value is kept"
    assert cfg.mappings[0].serial.identity == {} and cfg.mappings[1].serial.identity == {
        "vid": "0403", "pid": "6001"}
    print("load: unknown match keys are noted and not used; the stored value is kept  OK")


def test_identity_flags():
    """same_identity / ambiguous come from comparing the listed ports, not from a missing
    serial number."""
    def usb(dev, vid="1a86", pid="7523", serial=None, by_id=None):
        return {"device": dev, "vid": vid, "pid": pid, "serial_number": serial, "by_id": by_id}

    # two CH340s: udev gave the one by-id name to one of them
    a = usb("/dev/ttyUSB0", by_id="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    b = usb("/dev/ttyUSB1")
    lone = usb("/dev/ttyUSB2", vid="067b", pid="2303",
               by_id="/dev/serial/by-id/usb-Prolific_PL2303-if00-port0")
    onboard = {"device": "/dev/ttyS0", "vid": None, "pid": None, "by_id": None}
    ports = [a, b, lone, onboard]
    portlist._identities(ports, udev=True)
    assert a["same_identity"] == ["/dev/ttyUSB1"] and b["same_identity"] == ["/dev/ttyUSB0"]
    assert a["ambiguous"] and b["ambiguous"], "the link may go to either"
    assert lone["same_identity"] == [] and not lone["ambiguous"], "a lone adapter without a serial"
    assert onboard["same_identity"] == [] and not onboard["ambiguous"]
    # an FT4232H: one identity, but every channel has its own by-id name (-if00..-if03)
    chans = [usb(f"/dev/ttyUSB{i}", "0403", "6011", "FT4X", f"/dev/serial/by-id/usb-FTDI-if0{i}")
             for i in range(4)]
    portlist._identities(chans, udev=True)
    assert all(len(c["same_identity"]) == 3 and not c["ambiguous"] for c in chans), chans
    # clones that all carry serial number 0001 (in any letter case)
    clones = [usb("/dev/ttyUSB0", "10c4", "ea60", "0001", "/dev/serial/by-id/usb-Silicon_0001"),
              usb("/dev/ttyUSB1", "10c4", "ea60", "0001")]
    portlist._identities(clones, udev=True)
    assert clones[0]["same_identity"] == ["/dev/ttyUSB1"] and clones[0]["ambiguous"]
    cased = [usb("COM3", serial="ab12"), usb("COM4", serial="AB12")]
    portlist._identities(cased, udev=False)  # Windows (no udev): no by-id names at all
    assert cased[0]["same_identity"] == ["COM4"] and not cased[0]["ambiguous"]
    # in a container without udev's links nothing is taken as ambiguous
    a2, b2 = usb("/dev/ttyUSB0"), usb("/dev/ttyUSB1")
    portlist._identities([a2, b2], udev=False)
    assert a2["same_identity"] == ["/dev/ttyUSB1"] and not a2["ambiguous"]
    print("ports: identical identities found by comparing the ports (CH340 pair, FT4232H "
          "channels, 0001 clones, any case); by-id ambiguous only where udev names ports  OK")


def test_by_path_names():
    import sys as _sys
    if not _sys.platform.startswith("linux"):
        print("by-path names: not Linux, skipped")
        return
    d = tempfile.mkdtemp(prefix="s2n_bypath_")
    by_id, by_path = os.path.join(d, "by-id"), os.path.join(d, "by-path")
    os.makedirs(by_id)
    os.makedirs(by_path)
    os.symlink("/dev/ttyUSB0", os.path.join(by_id, "usb-1a86_USB_Serial-if00-port0"))
    for name, dev in (("pci-0000:00:14.0-usb-0:1:1.0-port0", "/dev/ttyUSB0"),
                      ("pci-0000:00:14.0-usbv2-0:1:1.0-port0", "/dev/ttyUSB0"),
                      ("pci-0000:00:14.0-usb-0:2:1.0-port0", "/dev/ttyUSB1")):
        os.symlink(dev, os.path.join(by_path, name))

    def port(dev):
        return SimpleNamespace(device=dev, name=os.path.basename(dev), description="USB Serial",
                               hwid="", vid=0x1a86, pid=0x7523, serial_number=None,
                               manufacturer=None, product=None, location="1-1")

    from serial.tools import list_ports
    with mock.patch.object(list_ports, "comports", lambda: [port("/dev/ttyUSB1"),
                                                            port("/dev/ttyUSB0")]), \
            mock.patch.object(portlist, "BY_ID_DIR", by_id), \
            mock.patch.object(portlist, "BY_PATH_DIR", by_path), \
            mock.patch.object(portdiag, "port_warnings", lambda dev: []), \
            mock.patch.object(portdiag, "access", lambda dev: True):
        ports = portlist.snapshot()
    usb0, usb1 = ports
    assert usb0["by_id"] == os.path.join(by_id, "usb-1a86_USB_Serial-if00-port0")
    assert usb0["by_path"] == os.path.join(by_path, "pci-0000:00:14.0-usb-0:1:1.0-port0"), \
        "of the usb- and usbv2- forms the first by name"
    assert usb1["by_id"] is None and usb1["by_path"].endswith("usb-0:2:1.0-port0")
    assert usb0["ambiguous"] and usb1["ambiguous"] and usb0["same_identity"] == ["/dev/ttyUSB1"]
    assert portlist.find(ports, usb1["by_path"]) is usb1
    assert portlist.names(usb0) == ["/dev/ttyUSB0", usb0["by_id"], usb0["by_path"]]
    # a mapping set to a by-path name: the supervisor wakes it when that name appears
    from app.engine.supervisor import Supervisor
    sup = Supervisor(logger=lambda *a: None)
    woken = []
    sup._runners["m"] = SimpleNamespace(status=SimpleNamespace(state="device-missing"),
                                        wake_serial=lambda: woken.append(1))
    sup.ports_changed([usb0])
    woken.clear()
    sup.ports_changed([usb0, usb1])
    assert woken, "a new by-path name counts as a port that appeared"
    print("ports: /dev/serial/by-path names attached (one per socket), found by find(), "
          "and they wake a waiting mapping  OK")


def test_rebinding_keeps_a_socket():
    form = {"serial_usb_match_shown": "1", "serial_usb_match": "on"}
    stored = mapping("/dev/ttyUSB3", {"vid": "0403", "pid": "6001", "location": "1-1.2"})
    m = mapping("/dev/ttyUSB3")
    apply_usb_match(m, form, [dict(FTDI, location="1-1.4")], existing=stored)
    assert m.serial.match == {"vid": "0403", "pid": "6001", "serial_number": "A12345",
                              "location": "1-1.4"}, m.serial.match
    m = mapping("/dev/ttyUSB3")
    apply_usb_match(m, form, [dict(FTDI, location="1-1.4")])
    assert "location" not in m.serial.match, "the form never binds to a socket by itself"
    print("form: a binding that has a USB socket (location) keeps one when bound again  OK")


def main():
    test_sysfs_and_by_id()
    test_form_binding()
    test_identity_flags()
    test_by_path_names()
    test_rebinding_keeps_a_socket()
    test_identity_resolution()
    test_shared_identity_warning()
    test_unknown_match_keys_noted()
    from serial.tools import list_ports
    real = list_ports.comports
    list_ports.comports = lambda: [SimpleNamespace(device="/dev/ttyUSB7", vid=0x0403, pid=0x6001,
                                                   serial_number="A12345", location="1-1")]
    try:
        test_engine_resolves(None)
    finally:
        list_ports.comports = real
    print("\nPASS: port list + USB identity binding")


if __name__ == "__main__":
    main()
