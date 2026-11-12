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
    marks such ports and shows both names;
  * "Other device path…" comes with a text box that has no name until app.js gives it the
    select's ({prefix}port: an older cluster peer saves the typed path as it is); one
    permissive rule for a port set by an edit (config.port_problem: no URL, no control
    characters; device paths, Windows names and "auto" pass), never in validate();
  * a configured link to a listed port (/dev/serial0 -> ttyS0, worked out on the port's own
    machine: portlist.links, a peer's mapping-data "links") is shown as "link → device",
    counts as that port in the Ports page's In use and for the USB-identity box.

Run: python3 tests/test_port_picker.py
"""
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request

from _server import ROOT, Browser, Server, free_port

from app.config import ConfigError, MappingConfig, port_problem
from app.engine import portlist
from app.web.auth import hash_password, hash_token
from app.web.routes.cluster import _peer_links
from app.web.routes.ops import apply_usb_match
from app.web.server import make_templates

PW = "port-picker-pass-1"
TOKEN = "s2n_port-picker-token"
KEY = "port-picker-cluster-key"


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
        assert st == 200 and re.search(r'<select name="serial_port"[^>]* required', page), \
            page[:300]
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
    assert "1 other connected port has the USB identity of /dev/ttyUSB0 (1a86:7523)" in out
    assert "This by-id name is not unique" in out and "By USB socket" in out
    out = _field(TWINS[1]["by_path"])
    assert _selected(_select(out, "serial_port")) == [TWINS[1]["by_path"]]
    assert "(not connected)" not in out and "This by-id name is not unique" not in out
    assert "has the USB identity of /dev/ttyUSB1" in out, "the identity is still shared"
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


def test_typed_path():
    out = _field("")
    assert '<option value="" data-port-other>Other device path…</option>' in out
    typed = re.search(r"<input[^>]*data-port-typed[^>]*>", out).group(0)
    assert "name=" not in typed and "hidden" in typed and "disabled" in typed, typed
    assert 'data-port-field="serial_port"' in out and "data-required" in out
    js = open(os.path.join(ROOT, "app", "web", "static", "app.js"), encoding="utf-8").read()
    assert 'input.setAttribute("name", name)' in js and 'sel.removeAttribute("name")' in js
    for bad in ("socket://10.0.0.1:502", "/dev/ttyUSB0\n", "/dev/tty\tUSB0", "COM3\x00"):
        assert port_problem(bad), bad
        m = MappingConfig.from_dict({"name": "m", "serial": {"port": bad}})
        m.validate()  # a stored one keeps loading
        try:
            m.validate_input()
        except ConfigError:
            pass
        else:
            raise AssertionError(f"an edit took {bad!r}")
    for good in ("/dev/ttymxc1", "/dev/serial0", "/dev/serial/by-path/platform-30860000.serial",
                 "/tmp/ttyV0", "COM5", "\\\\.\\CNCA0", "auto", "ttyUSB0"):
        assert port_problem(good) == "", good
    print("typed path: an unnamed text box until chosen; one permissive edit-time port rule "
          "(no URL, no control characters)  OK")


def test_links_to_listed_ports():
    if sys.platform == "win32":  # (symlinks need a privilege there)
        print("links: Windows, skipped")
        return
    d = tempfile.mkdtemp(prefix="s2n_alias_")
    dev = os.path.join(d, "ttyS0")
    open(dev, "w").close()
    alias = os.path.join(d, "serial0")
    os.symlink(dev, alias)
    ports = [{"device": dev, "vid": "0403", "pid": "6001", "serial_number": "A1",
              "description": "UART"}]
    links = portlist.links(ports, (alias, dev, "", "COM3", os.path.join(d, "nope")))
    assert links == {alias: dev}, links
    out = _field(alias, ports, links=links)
    options = _select(out, "serial_port")
    assert _selected(options) == [alias] and f"{alias} → {dev}</option>" in options, options
    assert "(not connected)" not in out
    assert "(not connected)" in _field(alias, ports), "without links (an older peer): as before"
    assert portlist.used(ports, {alias}) == {dev}, "In use: the alias's device"
    m = MappingConfig.from_dict({"name": "m", "serial": {"port": alias}})
    apply_usb_match(m, {"serial_usb_match_shown": "1", "serial_usb_match": "on"}, ports)
    assert m.serial.match == {"vid": "0403", "pid": "6001", "serial_number": "A1"} \
        and m.serial.port == alias
    assert _peer_links({"links": {alias: dev, 1: "x", "y": None}}) == {alias: dev}
    assert _peer_links({}) == {} and _peer_links({"links": ["x"]}) == {}
    print("links: a configured link to a listed port is shown as 'link → device', is in "
          "use and binds by USB identity; a peer's links are taken as given  OK")


def test_server_paths():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
           "cluster": {"enabled": True, "key": KEY, "discovery_port": free_port()},
           "mappings": [{"id": "aaaaaaaaaaa1", "name": "imx", "enabled": False,
                         "serial": {"port": "/dev/ttymxc1"},
                         "network": {"mode": "server", "bind_ip": "127.0.0.1",
                                     "port": free_port()}}]}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        fields = {"name": "typed", "kind": "net", "serial_port": "/dev/ttyLP2",
                  "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
                  "network_bind_ip": "127.0.0.1", "network_port": str(free_port())}
        st, _h, _page = b.post("/api/mappings/save", fields)
        assert st == 200, st
        assert any(m["serial"]["port"] == "/dev/ttyLP2" for m in srv.saved_config()["mappings"])
        st, _h, page = b.post("/api/mappings/save", dict(fields, name="bad",
                                                         serial_port="/dev/tty\tLP3"))
        assert st == 400 and "control character" in page and "banner error" in page, st
        st, _h, page = b.get("/api/mappings/aaaaaaaaaaa1/form")
        assert st == 200 and "/dev/ttymxc1 (not connected)" in page and "Other device path" in page
        req = urllib.request.Request(
            srv.base + "/api/v1/mappings", method="POST",
            data=json.dumps({"name": "r", "serial": {"port": "/dev/ttyUSB0\r\n"}}).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("REST took a port with a line break")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "control character" in e.read().decode()
        req = urllib.request.Request(srv.base + "/api/cluster/mapping-data?mid=aaaaaaaaaaa1",
                                     headers={"X-Cluster-Key": KEY})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert data.get("links") == {} and data["mapping"]["serial"]["port"] == "/dev/ttymxc1", data
    print("server: a typed path saves as it is, one with a control character is refused "
          "(form, REST); an unlisted stored port can be typed again; mapping-data has links  OK")


def main():
    test_shared_field()
    test_names_and_hints()
    test_typed_path()
    test_links_to_listed_ports()
    test_fragment_endpoint()
    test_server_paths()
    print("\nPASS: the mapping form's serial port picker")


if __name__ == "__main__":
    main()
