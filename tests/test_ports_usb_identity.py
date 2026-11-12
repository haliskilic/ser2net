"""Serial port list and binding a mapping to a USB device's identity.

Checks:
  * Linux placeholder ttyS ports (sysfs type 0 = no UART) are marked hardware=False
    and hidden from the UI by default (shown on request); /dev/serial/by-id names are
    attached to their real device;
  * the mapping form's "find this device by its USB identity" sets serial.match from
    the selected port (VID/PID + serial number), clears it when unticked, keeps it when
    the device is unplugged, refuses a port without a USB identity, and a form without
    the field (an older cluster peer) keeps the stored binding;
  * a form shown again after an error (local or a cluster peer's) keeps the box and the
    binding as a save would set them, so saving it does not drop the binding;
  * the engine then opens whatever device currently has that identity.

Run: python3 tests/test_ports_usb_identity.py
"""
import os
import tempfile
from types import SimpleNamespace

from _server import ROOT  # noqa: F401  (sys.path setup)

from app.config import MappingConfig
from app.engine import portlist, serial_io
from app.web.routes.forms import _preserve_unmanaged_fields, mapping_for_rerender
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


def test_error_rerender_keeps_binding():
    # the form posted back with an error elsewhere (a bad bind IP): the mapping shown
    # again has the identity the ticked box stands for, so its next save keeps it
    stored = mapping("/dev/ttyUSB3", {"vid": "0403", "pid": "6001", "serial_number": "A12345"})
    form = {"id": stored.id, "name": "m", "kind": "net", "serial_port": "/dev/ttyUSB3",
            "serial_usb_match_shown": "1", "serial_usb_match": "on",
            "serial_b_usb_match_shown": "1", "network_mode": "server",
            "network_bind_ip": "custom", "network_bind_ip_custom": "10.0.0.300",
            "network_port": "4000"}
    for ports in ([FTDI], [CH340]):  # plugged in / not plugged in now
        again = mapping_for_rerender(form, stored, ports)
        assert again.serial.match == stored.serial.match, (ports, again.serial.match)
    # a new mapping: the identity of the port picked
    new = mapping_for_rerender({**form, "id": ""}, None, [FTDI])
    assert new.serial.match == {"vid": "0403", "pid": "6001", "serial_number": "A12345"}
    # the error is the box itself (a port without a USB identity): the stored binding
    # is shown, not dropped
    on_board = {**form, "serial_port": "/dev/ttyS0"}
    assert mapping_for_rerender(on_board, stored, [ONBOARD]).serial.match == stored.serial.match
    assert mapping_for_rerender({**on_board, "id": ""}, None, [ONBOARD]).serial.match == {}
    # unticked stays unticked
    off = {k: v for k, v in form.items() if k != "serial_usb_match"}
    assert mapping_for_rerender(off, stored, [FTDI]).serial.match == {}
    print("form shown again after an error: the binding is kept as a save would set it  OK")


def test_engine_resolves(monkeypatch_ports):
    m = mapping("/dev/ttyUSB0", {"vid": "0403", "pid": "6001", "serial_number": "A12345"})
    assert serial_io.resolve_device(m.serial) == "/dev/ttyUSB7", "engine must follow the identity"
    m = mapping("/dev/ttyUSB0", {"vid": "0403", "pid": "6001", "serial_number": "OTHER"})
    assert serial_io.resolve_device(m.serial) == "/dev/ttyUSB0", "no match -> configured path"
    print("engine: opens the device that has the identity now (renumbered ttyUSB0 -> ttyUSB7)  OK")


def main():
    test_sysfs_and_by_id()
    test_form_binding()
    test_error_rerender_keeps_binding()
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
