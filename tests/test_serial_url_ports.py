r"""A serial port must be a device, never a pyserial URL handler.

pyserial's serial_for_url dispatches on "://": socket:// opens a TCP connection,
rfc2217:// a remote serial, spy://...?file= a local file, loop:// / alt:// / hwgrep://
other handlers. A mapping only ever opens a real serial device, so a port containing
"://" is refused everywhere a port is set — the web form, the REST API, JSON and classic
import, the cluster mapping-save — and is never opened by the engine. Real device paths,
Windows COM names and \\.\COM10 keep working. A stored URL port (hand-edited, restored,
older config) still loads and saves (Docker re-saves on every boot); it just does not
open, with a load note.

Run: python3 tests/test_serial_url_ports.py
"""
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port

from ser2netweb.config import AppConfig, ConfigError, MappingConfig, url_port
from ser2netweb.engine import serial_io
from ser2netweb.importer import parse_mappings_file
from ser2netweb.web.auth import hash_password, hash_token

PW = "serial-url-pass-1"
TOKEN = "s2n_serial-url-token"
KEY = "serial-url-cluster-key"
URL_PORTS = ["socket://10.0.0.1:502", "rfc2217://10.0.0.1:4001", "spy:///dev/ttyS0?file=/tmp/x",
             "loop://", "alt:///dev/ttyS0", "hwgrep://0403:6001", "SPY://x", "a://b"]
DEVICE_PORTS = ["/dev/ttyUSB0", "/dev/ttyS1", "/dev/serial/by-id/usb-FTDI_A1-if00-port0",
                "COM3", "COM250", "\\\\.\\COM10", "auto", "fake", ""]


def test_url_port_predicate():
    for p in URL_PORTS:
        assert url_port(p), p
    for p in DEVICE_PORTS + [None, 5, ["x"]]:
        assert not url_port(p), p
    print("url_port: '://' values are URLs; device paths, COM names, \\\\.\\COM10 are not  OK")


def test_validate_input():
    def mapping(port, port_b=None, kind="net"):
        d = {"name": "m", "kind": kind, "serial": {"port": port},
             "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4001}}
        if port_b is not None:
            d["serial_b"] = {"port": port_b}
        return MappingConfig.from_dict(d)

    for p in URL_PORTS:
        m = mapping(p)
        m.validate()  # validate() must NOT reject it (a stored config keeps loading/saving)
        try:
            m.validate_input()
        except ConfigError as e:
            assert "URL" in str(e)
        else:
            raise AssertionError(f"validate_input accepted {p!r}")
    # the B side of a serial bridge is checked too
    try:
        mapping("/dev/ttyS0", "socket://x:1", kind="serialbridge").validate_input()
    except ConfigError:
        pass
    else:
        raise AssertionError("validate_input accepted a URL on serial_b")
    for p in DEVICE_PORTS:
        if p:
            mapping(p).validate_input()  # device ports pass
    print("validate_input: refuses a URL on either serial side, accepts device ports  OK")


def test_load_keeps_but_notes():
    cfg = AppConfig.from_dict({"mappings": [
        {"id": "urlmap123456", "name": "relay", "serial": {"port": "socket://10.0.0.1:502"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4001}}]})
    assert cfg.mappings[0].serial.port == "socket://10.0.0.1:502", "the stored value is kept"
    cfg.validate()  # loads + validates (so a boot re-save does not crash-loop)
    notes = [n for n in cfg.load_notes if "is a URL" in n]
    assert len(notes) == 1 and "does not start" in notes[0], cfg.load_notes
    d = tempfile.mkdtemp(prefix="s2n_url_")
    from ser2netweb.config import ConfigStore
    store = ConfigStore(os.path.join(d, "config.json"))
    store.save(cfg)  # must not raise
    assert store.load().mappings[0].serial.port == "socket://10.0.0.1:502"
    print("load: a stored URL port is kept and saved, with a load note (it just won't open)  OK")


def test_refuse_url_at_open():
    import asyncio

    async def opens(port):
        try:
            await serial_io.open_serial(MappingConfig.from_dict(
                {"name": "m", "serial": {"port": port}}).serial)
        except ValueError as e:
            assert "URL" in str(e), e
            return "refused"
        except Exception:
            return "tried"   # got past the URL check to a real open attempt
        return "opened"

    for p in URL_PORTS:
        assert asyncio.run(opens(p)) == "refused", p
    # a device path is NOT refused by this check (it fails later, at the real open)
    assert asyncio.run(opens("/dev/ser2net-does-not-exist")) == "tried"
    print("open_serial: refuses every URL port before any connection/open; devices pass  OK")


def test_classic_import_skips_urls():
    yaml = ("connection: &a\n  accepter: tcp,127.0.0.1,4500\n"
            "  connector: serialdev,spy:///dev/ttyS0?file=/tmp/x,9600n81\n"
            "connection: &b\n  accepter: tcp,127.0.0.1,4501\n"
            "  connector: serialdev,/dev/ttyS1,9600n81\n")
    plan = parse_mappings_file(yaml.encode(), "ser2net.yaml")
    assert [m.serial.port for m in plan.mappings] == ["/dev/ttyS1"], \
        [m.serial.port for m in plan.mappings]
    conf = ("DEVICE:gw:socket://10.0.0.1:502\n"
            "4502:raw:0:gw:9600 8DATABITS\n4503:raw:0:/dev/ttyS2:9600\n")
    plan = parse_mappings_file(conf.encode(), "ser2net.conf")
    assert [m.serial.port for m in plan.mappings] == ["/dev/ttyS2"], \
        [m.serial.port for m in plan.mappings]
    print("classic import: a URL device is skipped (warning), real devices import  OK")


def form_fields(name, port, **extra):
    return {"name": name, "kind": "net", "serial_port": port, "serial_baudrate": "9600",
            "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(free_port()), **extra}


def test_server():
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
           "cluster": {"enabled": True, "key": KEY, "discovery_port": free_port()},
           "mappings": []}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)

        # web form: a URL port is refused with a visible error, nothing is stored
        st, _, page = b.post("/api/mappings/save", form_fields("relay", "socket://10.0.0.1:502"))
        assert st == 400 and "URL" in page and "banner error" in page, (st, page[:300])
        assert srv.saved_config()["mappings"] == [], "a URL port must not be stored"
        # a real device saves
        st, _, _ = b.post("/api/mappings/save", form_fields("good", "/dev/ser2net-test"))
        assert st == 200, st
        assert srv.saved_config()["mappings"][0]["serial"]["port"] == "/dev/ser2net-test"
        print("form: a URL serial port is refused; a device path saves  OK")

        # REST create + update
        def api(method, path, body):
            req = urllib.request.Request(srv.base + path, method=method,
                                         data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {TOKEN}",
                                                  "Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req, timeout=10)
                return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        body = {"name": "r", "serial": {"port": "socket://1.2.3.4:5"},
                "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port()}}
        st, data = api("POST", "/api/v1/mappings", body)
        assert st == 400 and "URL" in data.get("detail", ""), (st, data)
        mid = srv.saved_config()["mappings"][0]["id"]
        st, data = api("PUT", f"/api/v1/mappings/{mid}", body)
        assert st == 400 and "URL" in data.get("detail", ""), (st, data)
        print("REST: create and update reject a URL serial port  OK")

        # cluster mapping-save (key-guarded, peer-facing) refuses it too
        req = urllib.request.Request(
            srv.base + "/api/cluster/mapping-save", method="POST",
            data=urllib.parse.urlencode(form_fields("peer", "rfc2217://x:1")).encode(),
            headers={"X-Cluster-Key": KEY, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("cluster mapping-save accepted a URL port")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
        print("cluster mapping-save: a URL serial port is refused  OK")

    # a stored URL port: the mapping loads but does not start (fails fast, logged once)
    port = free_port()
    boot = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                       "pwd_version": 1}],
            "mappings": [{"id": "urlmap123456", "name": "relay", "enabled": True, "kind": "net",
                          "serial": {"port": "socket://127.0.0.1:1"},
                          "network": {"mode": "server", "protocol": "raw",
                                      "bind_ip": "127.0.0.1", "port": port}}]}
    with Server(boot) as srv:
        log = srv.output()
        assert "is a URL" in log, log[-2000:]
        assert srv.saved_config()["mappings"][0]["serial"]["port"] == "socket://127.0.0.1:1", \
            "the stored URL port must survive the boot re-save"
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, table = b.get("/api/status")
        assert st == 200 and ("error" in table or "device-missing" in table or "URL" in table), \
            "the mapping should show a problem, not be running"
        print("boot: a stored URL port loads, re-saves, is logged, and does not open  OK")


def main():
    test_url_port_predicate()
    test_validate_input()
    test_load_keeps_but_notes()
    test_refuse_url_at_open()
    test_classic_import_skips_urls()
    test_server()
    print("\nPASS: serial ports are devices, never pyserial URL handlers")


if __name__ == "__main__":
    main()
