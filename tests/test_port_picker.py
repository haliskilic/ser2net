"""The mapping form's Device field (the serial port picker).

Checks:
  * both serial sides render the one shared field (_serial_port_select.html); a local form
    has ↻ next to it, a cluster peer's form (the peer's ports) has not;
  * GET /api/ports/select?prefix=&selected= answers that field again with the ports there
    are now, keeping what the form had chosen (also a port that is not listed), for
    operators; a viewer gets 403, an unknown field 400; the old POST /api/ports/refresh is
    gone;
  * the dashboard's Refresh button keeps the "show all ports" choice (app.js reads the
    request's target, the panel, not its element, the button);
  * by serial number (by-id) and by USB socket (by-path) names are offered in their own
    groups, a by-id name that is not unique is marked, and a port whose USB identity other
    ports share has a hint (pick its by-path name); a stored port in another spelling or
    under one of these names is shown as chosen, not as "(not connected)"; the Ports page
    marks such ports and shows both names.

Run: python3 tests/test_port_picker.py
"""
import os
import re

from _server import ROOT, Browser, Server

from app.config import MappingConfig
from app.web.auth import hash_password
from app.web.server import make_templates

PW = "port-picker-pass-1"


class _Req:
    class state:
        csrf_token = "t"
        user = None


def _form(mapping, ports, **extra):
    tpl = make_templates().env.get_template("_mapping_form.html")
    return tpl.render(request=_Req, mapping=mapping, is_new=False, modbus_points_json="",
                      ports=ports, ips=[], error=None, **extra)


def _select(html, name):
    m = re.search(r'<select name="%s"[^>]*>(.*?)</select>' % re.escape(name), html, re.S)
    assert m, (name, html[:500])
    return m.group(1)


def _selected(options):
    return re.findall(r'<option value="([^"]*)"[^>]*\bselected\b', options)


def test_shared_field():
    ports = [{"device": "/dev/ttyUSB0", "by_id": "/dev/serial/by-id/usb-FTDI_A1-if00-port0",
              "description": "FT232R"}]
    bridge = MappingConfig.from_dict({"id": "sb1", "name": "sb", "kind": "serialbridge",
                                      "serial": {"port": "/dev/ttyUSB0"},
                                      "serial_b": {"port": "/dev/ttyUSB9"}})
    out = _form(bridge, ports)
    assert out.count('class="port-pick"') == 2, "one shared field per serial side"
    assert _selected(_select(out, "serial_port")) == ["/dev/ttyUSB0"]
    assert _selected(_select(out, "serial_b_port")) == ["/dev/ttyUSB9"]
    assert "/dev/ttyUSB9 (not connected)" in out
    for prefix in ("serial_", "serial_b_"):
        assert f'data-port-rescan="{prefix}"' in out and f'id="{prefix}port-pick"' in out
    remote = _form(bridge, ports, remote={"host": "10.0.0.2", "port": 8080, "id": "sb1",
                                          "addr": "10.0.0.2:8080"})
    assert "data-port-rescan" not in remote, "a peer's form lists the peer's ports: no ↻"
    assert remote.count('class="port-pick"') == 2
    js = open(os.path.join(ROOT, "app", "web", "static", "app.js"), encoding="utf-8").read()
    assert "var el = e.detail.target || e.detail.elt;" in js, \
        "show-all must follow the request's target (the Refresh button is its element)"
    print("form: both sides use the shared Device field; ↻ on local forms only  OK")


def test_fragment_endpoint():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1},
                     {"username": "view", "password_hash": hash_password(PW), "role": "viewer",
                      "pwd_version": 1}],
           "mappings": []}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _h, page = b.get("/api/ports/select?prefix=serial_b_&selected=/dev/ttyNOPE7")
        assert st == 200 and 'id="serial_b_port-pick"' in page, (st, page[:300])
        options = _select(page, "serial_b_port")
        assert _selected(options) == ["/dev/ttyNOPE7"] and "(not connected)" in options
        assert 'data-port-rescan="serial_b_"' in page
        st, _h, page = b.get("/api/ports/select?prefix=serial_&selected=")
        assert st == 200 and '<select name="serial_port" required>' in page, page[:300]
        st, _h, page = b.get("/api/ports/select?prefix=network_&selected=x")
        assert st == 400, st
        st, _h, _page = b.post("/api/ports/refresh", {}, htmx=True)
        assert st in (404, 405), st
        v = Browser(srv.base)
        v.login("view", PW)
        st, _h, page = v.get("/api/ports/select?prefix=serial_&selected=")
        assert st == 403 and "operator" in page, (st, page)
    print("GET /api/ports/select: the field again, choice kept (operators); 403 viewer, 400 "
          "unknown field; POST /api/ports/refresh gone  OK")


TWINS = [
    {"device": "/dev/ttyUSB0", "by_id": "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
     "by_path": "/dev/serial/by-path/pci-0000:00:14.0-usb-0:1:1.0-port0", "vid": "1a86",
     "pid": "7523", "serial_number": None, "description": "USB Serial",
     "same_identity": ["/dev/ttyUSB1"], "ambiguous": True},
    {"device": "/dev/ttyUSB1", "by_id": None,
     "by_path": "/dev/serial/by-path/pci-0000:00:14.0-usb-0:2:1.0-port0", "vid": "1a86",
     "pid": "7523", "serial_number": None, "description": "USB Serial",
     "same_identity": ["/dev/ttyUSB0"], "ambiguous": True},
    {"device": "COM3", "by_id": None, "by_path": None, "vid": None, "pid": None,
     "description": "Communications Port"},
]


def _field(selected, ports=TWINS, **kw):
    tpl = make_templates().env.get_template("_serial_port_select.html")
    return tpl.render(fragment=True, prefix="serial_", selected=selected, ports=ports, **kw)


def test_names_and_hints():
    out = _field("/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    assert "By serial number (/dev/serial/by-id): survives re-plugging; not unique" in out
    assert "By USB socket (/dev/serial/by-path)" in out
    assert "usb-1a86_USB_Serial-if00-port0 (not unique)</option>" in out
    assert "pci-0000:00:14.0-usb-0:2:1.0-port0 (ttyUSB1)</option>" in out
    assert _selected(_select(out, "serial_port")) == [TWINS[0]["by_id"]]
    assert "1 other connected port(s) have the USB identity of /dev/ttyUSB0 (1a86:7523)" in out
    assert "This by-id name is not unique" in out and "By USB socket" in out
    out = _field(TWINS[1]["by_path"])
    assert _selected(_select(out, "serial_port")) == [TWINS[1]["by_path"]]
    assert "(not connected)" not in out and "This by-id name is not unique" not in out
    assert "have the USB identity of /dev/ttyUSB1" in out, "the identity is still shared"
    out = _field("com3")  # another spelling of a listed port: chosen as it is written
    options = _select(out, "serial_port")
    assert _selected(options) == ["com3"] and "com3 → COM3</option>" in options, options
    assert "(not connected)" not in out and "port-hint" not in out
    plain = [{"device": "/dev/ttyUSB0", "vid": "0403", "pid": "6001"}]  # an older peer's list
    out = _field("/dev/ttyUSB0", plain)
    assert "optgroup" not in out and "port-hint" not in out
    assert _selected(_select(out, "serial_port")) == ["/dev/ttyUSB0"]
    table = make_templates().env.get_template("_ports_table.html").render(
        ports=TWINS, show_all=False, hidden=0, in_use={"/dev/ttyUSB1"})
    assert table.count(">same USB identity</span>") == 2 and table.count(">by-id not unique</span>") == 2
    assert "/dev/ttyUSB0 have the same USB identity" in table
    assert table.count('title="by USB socket: right while the adapter stays in this socket"') == 2
    print("picker: by-id and by-path groups, 'not unique' marks and hints; a stored "
          "spelling or link is chosen; the Ports page marks shared identities  OK")


def main():
    test_shared_field()
    test_names_and_hints()
    test_fragment_endpoint()
    print("\nPASS: the mapping form's serial port picker")


if __name__ == "__main__":
    main()
