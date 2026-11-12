r"""Names of one serial port in other spellings are one port.

Windows opens 'com3', 'COM3', '\\.\COM3' and '//./COM3' alike, so:
  * canonical_device makes them all 'COM3' (by the name's shape, on any OS); other names
    are left as they are ('\\.\CNCA0' of com0com, POSIX paths);
  * the port list finds a listed port by any of them (the USB-identity box works for a
    stored 'com3'); stored values are never rewritten;
  * two enabled mappings on one port in different spellings (or bound to the same USB
    identity on one port) are refused where a mapping is created, changed or started — the
    form, the REST API (create, update, start), the UI's start and bulk start, a cluster
    peer's start, an import — but not by validate(): a stored pair keeps loading and
    saving, with a load note. A serial-to-serial bridge needs two different ports in any
    spelling; the classic ser2net importer takes 'com3' after 'COM3' as the same port.

Run: python3 tests/test_serial_port_names.py
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port

from app.config import AppConfig, ConfigError, MappingConfig, canonical_device, serial_conflict
from app.engine import portlist
from app.importer import parse_mappings_file
from app.web.auth import hash_password, hash_token
from app.web.routes.ops import apply_usb_match

PW = "port-names-pass-1"
TOKEN = "s2n_port-names-token"
KEY = "port-names-cluster-key"


def test_canonical_device():
    for name in ("COM3", "com3", "Com3", "\\\\.\\COM3", "\\\\.\\com3", "//./COM3", "//./com3"):
        assert canonical_device(name) == "COM3", name
    for name in ("\\\\.\\CNCA0", "CNCA0", "/dev/ttyUSB0", "/dev/serial/by-id/usb-X", "COM",
                 "COM3x", "xCOM3", "\\\\?\\COM3", "", "auto"):
        assert canonical_device(name) == name, name
    assert canonical_device(None) is None
    ports = [{"device": "COM3", "vid": "0403", "pid": "6001", "serial_number": "A1"},
             {"device": "COM10"}, {"device": "\\\\.\\CNCA0"}]
    assert portlist.find(ports, "com3") is ports[0]
    assert portlist.find(ports, "\\\\.\\COM10") is ports[1]
    assert portlist.find(ports, "//./cnca0") is None, "only COMn is case-folded"
    assert portlist.find(ports, "\\\\.\\CNCA0") is ports[2]
    assert portlist.used(ports, {"com3", "//./COM10", "/dev/ttyS0"}) == {"COM3", "COM10"}, \
        "the ports table's In use"
    m = MappingConfig.from_dict({"name": "m", "serial": {"port": "com3"}})
    apply_usb_match(m, {"serial_usb_match_shown": "1", "serial_usb_match": "on"}, ports)
    assert m.serial.match == {"vid": "0403", "pid": "6001", "serial_number": "A1"}
    assert m.serial.port == "com3", "what the user set is kept"
    print("canonical_device: COMn in any case and prefix is one name; others unchanged; "
          "the port list finds them  OK")


def _m(mid, port, enabled=True, match=None, port_b=None):
    d = {"id": mid, "name": mid, "enabled": enabled, "serial": {"port": port, "match": match or {}},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4000}}
    if port_b is not None:
        d.update(kind="serialbridge", serial_b={"port": port_b})
    return MappingConfig.from_dict(d)


def test_conflict_rules():
    a = _m("aaaaaaaaaaa1", "COM3")
    for spelling in ("com3", "\\\\.\\COM3", "//./com3"):
        msg = serial_conflict(_m("bbbbbbbbbbb1", spelling), [a])
        assert "both use serial port COM3" in msg and f"written {spelling}" in msg, msg
    assert "both use serial port COM3 —" in serial_conflict(_m("bbbbbbbbbbb1", "COM3"), [a])
    assert serial_conflict(_m("bbbbbbbbbbb1", "com3", enabled=False), [a]) == ""
    assert serial_conflict(_m("bbbbbbbbbbb1", "com3"), [_m("aaaaaaaaaaa1", "COM3", False)]) == ""
    assert serial_conflict(_m("aaaaaaaaaaa1", "com3"), [a]) == "", "itself (an edit)"
    assert serial_conflict(_m("bbbbbbbbbbb1", "COM4"), [a]) == ""
    # bound to a USB identity: only the same identity on the same port is one device
    ch340 = {"vid": "1a86", "pid": "7523"}
    bound = _m("aaaaaaaaaaa2", "/dev/ttyUSB0", match=ch340)
    msg = serial_conflict(_m("bbbbbbbbbbb2", "/dev/ttyUSB0", match=dict(ch340, x=1)), [bound])
    assert "both bound to the USB identity 1a86:7523 on serial port /dev/ttyUSB0" in msg, msg
    assert serial_conflict(_m("bbbbbbbbbbb2", "/dev/ttyUSB1", match=ch340), [bound]) == ""
    assert serial_conflict(_m("bbbbbbbbbbb2", "/dev/ttyUSB0"), [bound]) == ""
    assert serial_conflict(_m("bbbbbbbbbbb2", "/dev/ttyUSB0", match={"vid": "0403"}), [bound]) == ""
    # a bridge's B side counts too, and a bridge needs two ports in any spelling
    bridge = _m("ccccccccccc1", "/dev/ttyS0", port_b="com3")
    assert "written com3" in serial_conflict(bridge, [a])
    bridge = _m("ccccccccccc1", "COM3", port_b="\\\\.\\com3")
    bridge.validate()  # stored: still loads
    try:
        bridge.validate_input()
    except ConfigError as e:
        assert "two different ports" in str(e)
    else:
        raise AssertionError("a bridge from COM3 to \\\\.\\com3 must be refused on edit")
    print("conflicts: other spellings of one port, the same USB identity on one port; not a "
          "disabled one, itself, another identity; bridges need two ports  OK")


def test_stored_pairs_load():
    cfg = AppConfig.from_dict({"mappings": [
        {"id": "aaaaaaaaaaa1", "name": "a", "serial": {"port": "COM3"}},
        {"id": "aaaaaaaaaaa2", "name": "b", "serial": {"port": "com3"},
         "network": {"port": 4002}},
        {"id": "aaaaaaaaaaa3", "name": "c", "serial": {"port": "/dev/ttyUSB0",
                                                        "match": {"vid": "1a86"}},
         "network": {"port": 4003}},
        {"id": "aaaaaaaaaaa4", "name": "d", "serial": {"port": "/dev/ttyUSB0",
                                                        "match": {"vid": "1a86"}},
         "network": {"port": 4004}},
        {"id": "aaaaaaaaaaa5", "name": "e", "enabled": False, "serial": {"port": "\\\\.\\COM3"},
         "network": {"port": 4005}}]})
    cfg.validate()  # no new rule in validate(): such a config keeps loading and saving
    notes = [n for n in cfg.load_notes if "only one of them can open it" in n]
    assert len(notes) == 2, cfg.load_notes
    assert "'a' and 'b' both use serial port 'COM3' (written 'com3' in 'b')" in notes[0], notes
    assert "'c' and 'd' are both bound to the USB identity 1a86:* on serial port" in notes[1]
    print("load: a stored pair loads and validates, with a load note (none for a disabled "
          "one)  OK")


def test_classic_import_owner():
    port = free_port()
    yaml = (f"connection: &a\n  accepter: tcp,127.0.0.1,{port}\n  connector: serialdev,COM3,9600n81\n"
            f"connection: &b\n  accepter: tcp,127.0.0.1,{port + 1}\n  connector: serialdev,com3,9600n81\n")
    plan = parse_mappings_file(yaml.encode(), "ser2net.yaml")
    assert [(m.serial.port, m.enabled) for m in plan.mappings] == [("COM3", True), ("com3", False)]
    assert any("imported disabled: com3 is also used by 'a'" in w for w in plan.warnings), \
        plan.warnings
    print("classic import: 'com3' after 'COM3' is the same port (imported disabled)  OK")


def _form(name, port, **extra):
    return {"name": name, "kind": "net", "enabled": "on", "serial_port": port,
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(free_port()), **extra}


def _body(name, port, enabled=True):
    return {"name": name, "enabled": enabled, "serial": {"port": port},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port()}}


def test_server():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
           "cluster": {"enabled": True, "key": KEY, "discovery_port": free_port()},
           "mappings": [dict(_body("first", "COM7"), id="aaaaaaaaaaa1")]}
    with Server(cfg) as srv:
        def api(method, path, body=None):
            req = urllib.request.Request(srv.base + path, method=method,
                                         data=json.dumps(body or {}).encode(),
                                         headers={"Authorization": f"Bearer {TOKEN}",
                                                  "Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req, timeout=10)
                return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        def names():
            return {m["name"]: m for m in srv.saved_config()["mappings"]}

        # REST create / update
        st, data = api("POST", "/api/v1/mappings", _body("second", "com7"))
        assert st == 400 and "written com7" in data.get("detail", ""), (st, data)
        st, data = api("POST", "/api/v1/mappings", _body("second", "\\\\.\\com7", enabled=False))
        assert st == 201, (st, data)
        mid = data["id"]
        st, data = api("PUT", f"/api/v1/mappings/{mid}", _body("second", "//./COM7"))
        assert st == 400 and "both use serial port COM7" in data.get("detail", ""), (st, data)
        print("REST: create / update refuse another spelling of a port in use; disabled is fine  OK")

        # starting it: REST, the UI, the UI's bulk start, a cluster peer
        st, data = api("POST", f"/api/v1/mappings/{mid}/start")
        assert st == 400 and data.get("error") == "validation" and "COM7" in data["detail"], data
        assert names()["second"]["enabled"] is False, "a refused start changes nothing"
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, text = b.post(f"/api/mappings/{mid}/start", htmx=True)
        assert st == 400 and "both use serial port COM7" in text, (st, text)
        st, _, _ = b.post("/api/mappings/bulk", {"action": "start", "ids": [mid]}, htmx=True)
        assert st == 400 and names()["second"]["enabled"] is False
        assert "bulk start: Mappings 'first' and 'second'" in srv.output()
        req = urllib.request.Request(
            srv.base + "/api/cluster/control", method="POST",
            data=urllib.parse.urlencode({"mapping_id": mid, "action": "start"}).encode(),
            headers={"X-Cluster-Key": KEY, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("a peer's start of a clashing mapping was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "COM7" in e.read().decode(), e.code
        assert names()["second"]["enabled"] is False
        # stopping the first lets it start
        st, _ = api("POST", "/api/v1/mappings/aaaaaaaaaaa1/stop")
        assert st == 200
        st, data = api("POST", f"/api/v1/mappings/{mid}/start")
        assert st == 200 and names()["second"]["enabled"] is True, (st, data)
        print("start: refused (400, the reason) by REST, the UI, bulk start and a peer while "
              "another spelling runs; nothing changes; allowed once that one stops  OK")

        # the form
        st, _, page = b.post("/api/mappings/save", _form("third", "COM7"))
        assert st == 400 and "banner error" in page and "(written COM7 in" in page, \
            (st, page[:300])
        assert "third" not in names()
        st, _, _ = b.post("/api/mappings/save", _form("third", "COM8"))
        assert st == 200 and names()["third"]["serial"]["port"] == "COM8"
        print("form: another spelling of a port in use is refused with a visible error  OK")

        # an import
        export = json.dumps({"mappings": [_body("fourth", "com8")]}).encode()
        st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                             files={"file": ("m.json", export)})
        assert st == 200 and "written com8" in page, page[page.find("banner"):][:400]
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        st, _, page = b.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "Import rejected" in page and "written com8" in page, page[page.find("banner"):][:300]
        assert "fourth" not in names()
        print("import: preview and apply refuse a mapping on another spelling of a port in use  OK")


def main():
    test_canonical_device()
    test_conflict_rules()
    test_stored_pairs_load()
    test_classic_import_owner()
    test_server()
    print("\nPASS: one serial port, any spelling")


if __name__ == "__main__":
    main()
