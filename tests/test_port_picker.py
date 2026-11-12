"""The mapping form's Device field (the serial port picker).

Checks:
  * both serial sides render the one shared field (_serial_port_select.html); a local form
    has ↻ next to it, a cluster peer's form (the peer's ports) has not;
  * GET /api/ports/select?prefix=&selected= answers that field again with the ports there
    are now, keeping what the form had chosen (also a port that is not listed), for
    operators; a viewer gets 403, an unknown field 400; the old POST /api/ports/refresh is
    gone;
  * the dashboard's Refresh button keeps the "show all ports" choice (app.js reads the
    request's target, the panel, not its element, the button).

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


def main():
    test_shared_field()
    test_fragment_endpoint()
    print("\nPASS: the mapping form's serial port picker")


if __name__ == "__main__":
    main()
