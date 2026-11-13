"""MQTT topics, client IDs and Sparkplug B edge nodes stay apart where they must, and a
copied mapping gets an identity of its own.

  * an edit (validate_input) refuses '+', '#' or the null character in a plain base topic
    or point name, and a point published where the mapping already publishes or takes
    something else (its status, a unit's status, another point's /set or its result); '/'
    stays legal, Sparkplug B metric names are not topics; validate() takes them all;
  * between enabled mappings on one broker, a shared explicit client ID, Sparkplug edge
    node or command (/set) topic is refused (check_mqtt_clashes), shared publish topics
    only warned about; other brokers, disabled mappings and mappings that start no MQTT
    client don't count;
  * a stored clash loads with a load note (in English) and is logged in the mapping's log
    at each start, and validate() / save keep passing;
  * Copy: "(copy)", "(copy 2)", disabled, no client ID, "<topic>-copy", a Sparkplug node
    ID of its own, the next free port for its transport and address (down from 65535),
    and a clear error when none is free;
  * end to end: the form, a cluster node's save, the REST API and an import refuse a
    clash, a stored one is logged at boot, and Copy makes a mapping that can be enabled
    next to its original.
Cross-platform (the server part binds 127.0.0.1 only).

Run: python3 tests/test_mqtt_identity.py
"""
import asyncio
import json
import os
import shutil
import tempfile
import types
import urllib.error
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port

from ser2netweb import i18n
from ser2netweb.config import (AppConfig, ConfigError, ConfigStore, MappingConfig, check_mqtt_clashes,
                        mqtt_clashes, mqtt_warnings, sparkplug_identity)
from ser2netweb.web.auth import hash_password, hash_token
from ser2netweb.web.routes.ops import copy_of

PUMP = {"name": "pump", "fn": 1, "dtype": "bool", "writable": True}
TEMP = {"name": "temp", "fn": 3, "address": 1}
PW = "mqtt-identity-pass-1"
TOKEN = "s2n_mqtt-identity-token"
KEY = "mqtt-identity-cluster-key"


# fixed port: the default for configs that are only checked; boot_config() passes free ports
def gw(name, base="plant/line3", points=None, enabled=True, port=5020, mid=None, **mqtt):
    """A Modbus gateway publishing its points over MQTT."""
    d = {"name": name, "enabled": enabled, "serial": {"port": f"/dev/ser2net-test-{name}"},
         "network": {"protocol": "modbus", "bind_ip": "127.0.0.1", "port": port},
         "mqtt": {"enabled": True, "host": "broker.lan", "base_topic": base, **mqtt},
         "modbus_poll": {"points": [PUMP, TEMP] if points is None else points}}
    if mid:
        d["id"] = mid
    return MappingConfig.from_dict(d)


def lines(name, base, port=5030, **mqtt):
    """A raw mapping publishing its serial lines over MQTT."""
    return MappingConfig.from_dict({
        "name": name, "serial": {"port": f"/dev/ser2net-test-{name}"},
        "network": {"bind_ip": "127.0.0.1", "port": port},
        "mqtt": {"enabled": True, "host": "broker.lan", "base_topic": base, **mqtt}})


def refused(m, *words):
    try:
        m.validate_input()
    except ConfigError as e:
        assert all(w in str(e) for w in words), (str(e), words)
        return str(e)
    raise AssertionError(f"accepted: {m.mqtt.base_topic!r} {m.modbus_poll.points}")


def kinds(m, others):
    return [(c.kind, c.other) for c in mqtt_clashes(m, others)]


def test_topic_rules():
    for base in ("plant/#", "plant/+/x", "a\x00b"):
        refused(gw("g", base), "base topic", "'+', '#' or the null character")
        refused(lines("l", base), "base topic")
    for name in ("a+b", "#", "x\x00y"):
        refused(gw("g", points=[dict(TEMP, name=name)]), repr(name), "'+', '#'")
    # Sparkplug B metric names are not topics; '/' is legal (hierarchical topics)
    gw("g", points=[dict(TEMP, name="a+b"), dict(PUMP, name="aux/pump"), PUMP],
       payload_format="sparkplug_b").validate_input()
    gw("g", points=[dict(TEMP, name="line/a"), dict(PUMP, name="aux/pump"), PUMP]).validate_input()
    print("edit: '+', '#' or NUL in a plain base topic or point name refused; '/' and "
          "Sparkplug metric names fine  OK")

    refused(gw("g", points=[dict(TEMP, name="status")]), "Point 'status'", "'plant/line3/status'",
            "online/offline status topic")
    unit_topic = [TEMP, dict(TEMP, name="unit1/status", address=2)]
    refused(gw("g", points=unit_topic, unit_status=True), "'unit1/status'", "unit 1")
    gw("g", points=unit_topic).validate_input()  # unit status off: the topic is free
    gw("g", points=[TEMP, dict(TEMP, name="unit2/status", address=2)],
       unit_status=True).validate_input()        # no unit 2 is polled
    refused(gw("g", points=[PUMP, dict(TEMP, name="pump/set")]), "Point 'pump/set'",
            "command topic of point 'pump'")
    refused(gw("g", points=[dict(TEMP, name="pump/set/result"), PUMP]),
            "answers to point 'pump'")
    gw("g", points=[dict(PUMP, name="status", poll=False)]).validate_input()  # write-only
    print("edit: a point on the status, a unit status, another point's /set or /set/result "
          "topic refused (a write-only point publishes nothing)  OK")

    off = gw("g", "plant/#", points=[dict(TEMP, name="status")])
    off.mqtt.enabled = False
    off.validate_input()
    for m in (gw("g", "plant/#"), gw("g", points=[dict(TEMP, name="status")]),
              gw("g", points=[dict(TEMP, name="x+y")])):
        m.validate()
        AppConfig(mappings=[m]).validate()
    print("MQTT off: not checked; validate() still takes stored ones  OK")


def test_clashes():
    a = gw("A", client_id="gw-1")
    b = lines("B", "other", client_id="gw-1", host=" BROKER.lan ")
    assert kinds(b, [a]) == [("client_id", "A")], kinds(b, [a])
    clash = mqtt_clashes(b, [a])[0]
    assert clash.refused and clash.broker == "broker.lan:1883", clash
    try:
        check_mqtt_clashes(b, [a])
        raise AssertionError("a shared client ID was accepted")
    except ConfigError as e:
        assert "'A'" in str(e) and "'gw-1'" in str(e) and "client ID" in str(e), e
    other_port = lines("B", "other", client_id="gw-1")
    other_port.mqtt.port = 1884
    assert kinds(other_port, [a]) == []
    assert kinds(lines("B", "other", client_id="gw-1", host="broker2.lan"), [a]) == []
    assert kinds(lines("B", "other"), [gw("A2")]) == [], "blank client IDs never clash"
    assert kinds(b, [gw("A", client_id="gw-1", enabled=False)]) == []
    b.enabled = False
    assert kinds(b, [a]) == [], "a disabled mapping clashes with nothing"
    assert kinds(a, [a]) == [], "an edit is not compared with its own stored version"
    print("client ID: the same one on the same broker (host in any case) refused; another "
          "broker or port, blank IDs, disabled mappings, the mapping itself fine  OK")

    sp1 = gw("plc 1", "x", payload_format="sparkplug_b")
    sp2 = gw("plc/1", "y", payload_format="sparkplug_b")
    assert sparkplug_identity(sp1) == sparkplug_identity(sp2) == ("ser2net", "plc_1")
    assert kinds(sp2, [sp1]) == [("sparkplug", "plc 1")]
    assert "'ser2net/plc_1'" in mqtt_clashes(sp2, [sp1])[0].text()
    assert kinds(gw("n", "z", payload_format="sparkplug_b", sparkplug_node_id="plc_1"),
                 [sp1]) == [("sparkplug", "plc 1")]
    assert kinds(gw("plc/1", "y", payload_format="sparkplug_b", sparkplug_group_id="g2"),
                 [sp1]) == []
    assert kinds(gw("S", "plant/line3", payload_format="sparkplug_b"), [a]) == [], \
        "Sparkplug B uses no base topic"
    print("Sparkplug B: the same group/edge node (also made from names) refused; another "
          "group fine; no topic clash with plain mappings  OK")

    same_base = gw("B2", "plant/line3/")
    assert kinds(same_base, [a]) == [("command", "A")]
    assert mqtt_clashes(same_base, [a])[0].what == "plant/line3/pump/set"
    on_its_set = gw("D", "plant", [dict(TEMP, name="line3/pump/set")])  # data onto A's /set
    assert kinds(on_its_set, [a]) == [("command", "A")]
    a4 = gw("A4", "plant/line4", [dict(TEMP, name="mode/set")])
    e = gw("E", "plant/line4", [dict(PUMP, name="mode")])  # subscribes to A4's data
    assert kinds(e, [a4]) == [("command", "A4")] and kinds(a4, [e]) == [("command", "E")]
    try:
        check_mqtt_clashes(same_base, [a])
        raise AssertionError("a shared /set topic was accepted")
    except ConfigError as err:
        assert "'plant/line3/pump/set'" in str(err) and "takes writes" in str(err), err
    print("command topics: a /set shared, or used by the other for its data, refused  OK")

    reader = gw("C", "plant/line3", [TEMP])
    shared = mqtt_clashes(reader, [a])
    assert [(c.kind, c.what, c.refused) for c in shared] == [
        ("shared", "plant/line3/status", False)], shared
    check_mqtt_clashes(reader, [a])
    assert kinds(lines("L2", "scan/"), [lines("L1", "scan")]) == [("shared", "L1")]
    assert kinds(gw("N", points=[], client_id="gw-1"), [a]) == [], "no points: no client"
    assert kinds(lines("W", "plant/#", client_id="gw-1"), [a]) == [], "wildcard: MQTT off"
    both = gw("B3", client_id="gw-1")
    assert kinds(both, [a]) == [("client_id", "A")], "one clash per pair, the worst"
    print("shared publish topics only warned about; mappings without an MQTT client "
          "don't count  OK")


def test_serial_writes_topic():
    """A serial mapping's MQTT writes (<base_topic>/tx) are a command topic like a point's
    /set: another enabled mapping on the broker that takes writes from it too, or publishes
    its data there, is refused; one that only publishes next to it is warned about."""
    tx = lines("T", "lab/scale", tx_enabled=True)
    twin = lines("T2", "lab/scale/", port=5031, tx_enabled=True)
    assert kinds(twin, [tx]) == [("command", "T")], kinds(twin, [tx])
    text = mqtt_clashes(twin, [tx])[0].text()
    assert "'lab/scale/tx'" in text and "/tx" in text, text
    onto = gw("G", "lab/scale", [dict(TEMP, name="tx")])  # a point's value on T's /tx
    assert kinds(onto, [tx]) == [("command", "T")] and kinds(tx, [onto]) == [("command", "G")]
    assert kinds(lines("R", "lab/scale", port=5032), [tx]) == [("shared", "T")], \
        "a mapping that only publishes there"
    assert kinds(lines("T4", "lab/scale", port=5033), [lines("T", "lab/scale")]) == [
        ("shared", "T")], "writes off: no command topic"
    try:
        check_mqtt_clashes(twin, [tx])
        raise AssertionError("a shared /tx topic was accepted")
    except ConfigError as e:
        assert "takes writes from it" in str(e), e
    print("serial writes: a /tx topic shared with another enabled mapping refused  OK")


def stored_config():
    # fixed port: checked in-process only (test_stored_clashes), never started
    return [gw("A", mid="aaaaaaaaaaa1", port=5020),
            gw("B", "plant/line3/", mid="aaaaaaaaaaa2", port=5021),
            gw("C", "plant/line3", [TEMP], mid="aaaaaaaaaaa3", port=5022),
            gw("S", "plant/s", [dict(TEMP, name="status")], mid="aaaaaaaaaaa4", port=5023),
            gw("Off", mid="aaaaaaaaaaa5", port=5024, enabled=False)]


def test_stored_clashes():
    raw = {"mappings": [m.to_dict() for m in stored_config()]}
    token = i18n.set_language("tr")  # a backup restored by a Turkish page: still English
    try:
        cfg = AppConfig.from_dict(json.loads(json.dumps(raw)))
        warnings = mqtt_warnings(cfg.mappings[1], cfg.mappings)
    finally:
        i18n.reset(token)
    notes = [n for n in cfg.load_notes if "MQTT" in n]
    assert len(notes) == 3, notes
    assert notes[0].startswith("mapping 'B': MQTT: Mapping 'A' also uses the MQTT topic "
                               "'plant/line3/pump/set' on the broker broker.lan:1883"), notes
    assert notes[1].startswith("mapping 'C': MQTT: mapping 'A' publishes to the MQTT topic "
                               "'plant/line3/status'"), notes
    assert notes[2].startswith("mapping 'S': MQTT: Point 'status'"), notes
    assert warnings == [notes[0].split(": ", 1)[1]], warnings
    cfg.validate()
    tmp = tempfile.mkdtemp(prefix="s2n_mqttid_")
    store = ConfigStore(os.path.join(tmp, "config.json"))
    store.save(cfg)
    again = store.load()
    assert [n for n in again.load_notes if "MQTT" in n] == notes
    assert [m.mqtt.base_topic for m in again.mappings] == [m.mqtt.base_topic
                                                           for m in cfg.mappings]
    shutil.rmtree(tmp, ignore_errors=True)
    junk = gw("J")
    junk.modbus_poll = None
    assert mqtt_warnings(junk, cfg.mappings) == []
    print("stored clashes: kept, one load note per pair in English, validate()/save pass, "
          "nothing raises on junk  OK")

    from ser2netweb.engine import supervisor as sup
    from ser2netweb.engine.bridge import RunnerStatus

    class Stub:
        def __init__(self, mapping, logger, *_args):
            self.mapping, self.status = mapping, RunnerStatus()

        async def start(self):
            self.status.state = "running"

        async def stop(self):
            self.status.state = "stopped"

    log = []
    real = sup._build_runner
    sup._build_runner = Stub
    try:
        s = sup.Supervisor(logger=log.append)
        cfg = AppConfig(mappings=stored_config())

        async def go():
            await s.start_all(cfg)
            warned = [line for line in log if "warning: MQTT" in line]
            assert [w.split(":", 1)[0] for w in warned] == ["[B] warning", "[C] warning",
                                                            "[S] warning"], warned
            assert warned[0].startswith("[B] warning: MQTT: Mapping 'A' also uses the MQTT "
                                        "topic 'plant/line3/pump/set'"), warned
            log.clear()
            off = cfg.mappings[4]
            off.enabled = True
            await s.apply_mapping(off)  # Start on the dashboard: A, B (and C) run
            assert [line.split(": MQTT: ")[1][:13] for line in log] == ["Mapping 'A' a",
                                                                         "Mapping 'B' a"], log
            log.clear()
            cfg.mappings[0].enabled = cfg.mappings[1].enabled = off.enabled = False
            await s.stop_mapping(cfg.mappings[0].id)
            await s.restart_mapping(cfg.mappings[2])  # C alone now: nothing to say
            assert not [line for line in log if "warning" in line], log
        asyncio.run(go())
    finally:
        sup._build_runner = real
    print("start: each start of a mapping that clashes with a running one logs a warning "
          "in its log  OK")


def test_copy():
    a = gw("A", "plant/line3/", client_id="gw-1", mid="aaaaaaaaaaa1")
    cfg = AppConfig(mappings=[a])
    c1 = copy_of(cfg, a)
    assert (c1.name, c1.enabled, c1.mqtt.client_id, c1.mqtt.base_topic, c1.network.port) == (
        "A (copy)", False, "", "plant/line3-copy", 5021), c1
    assert c1.id != a.id and c1.mqtt.enabled, "MQTT stays as it was (the copy is disabled)"
    cfg.mappings.append(c1)
    c2 = copy_of(cfg, a)
    assert (c2.name, c2.mqtt.base_topic, c2.network.port) == ("A (copy 2)",
                                                             "plant/line3-copy-2", 5022), c2
    cfg.mappings.append(c2)
    rest = a.to_dict()
    for c in (c1, c2):
        d = c.to_dict()
        for key in ("id", "name", "enabled"):
            d.pop(key)
            rest.pop(key, None)
        for part, key in (("mqtt", "client_id"), ("mqtt", "base_topic"), ("network", "port")):
            d[part].pop(key)
            rest[part].pop(key, None)
        assert d == rest, "nothing else differs"
    for i, c in enumerate((c1, c2)):
        c.enabled, c.serial.port = True, f"/dev/ser2net-test-other{i}"
        c.validate()
        c.validate_input()
        check_mqtt_clashes(c, cfg.mappings)
    cfg.validate()
    print("copy: '(copy)', '(copy 2)', disabled, no client ID, '<topic>-copy(-2)', next "
          "port; enabled next to the original nothing clashes  OK")

    sp = gw("plc 1", "x", payload_format="sparkplug_b", sparkplug_node_id="line3")
    copy = copy_of(AppConfig(mappings=[sp]), sp)
    assert copy.mqtt.sparkplug_node_id == "" and sparkplug_identity(copy) == (
        "ser2net", "plc_1_(copy)"), copy.mqtt
    taken = gw("t", "y", payload_format="sparkplug_b", sparkplug_node_id="plc_1_(copy)", port=6000)
    copy = copy_of(AppConfig(mappings=[sp, taken]), sp)
    assert copy.mqtt.sparkplug_node_id == "plc_1_(copy)_2", copy.mqtt
    print("copy: Sparkplug B edge node made from the new name, numbered when taken  OK")

    def listener(port, bind="127.0.0.1", mode="server"):
        return MappingConfig.from_dict({"name": f"n{port}{bind}{mode}", "serial": {"port": "x"},
                                        "network": {"mode": mode, "bind_ip": bind,
                                                    "port": port, "remote_host": "h",
                                                    "remote_port": 1}})
    a = gw("A", port=5020)
    others = [listener(5021, mode="udp"), listener(5021, "127.0.0.2"),
              listener(5021, mode="client")]
    assert copy_of(AppConfig(mappings=[a, *others]), a).network.port == 5021, \
        "UDP, another address, a connect-out mapping: the port is free for TCP here"
    assert copy_of(AppConfig(mappings=[a, listener(5021, "0.0.0.0")]), a).network.port == 5022
    beside_ui = AppConfig(mappings=[a])
    beside_ui.admin_ui.bind_ip, beside_ui.admin_ui.port = "127.0.0.1", 5021
    assert copy_of(beside_ui, a).network.port == 5022, "not the admin UI's port"
    both = gw("B", port=5030)
    both.network.bind_ip, both.network.dual_stack = "::", True  # IPv4 and IPv6 clients
    assert copy_of(AppConfig(mappings=[a, both]), gw("A4", port=5030)).network.port == 5031, \
        "a listener that takes both families has the IPv4 port too"
    top = gw("A", port=65535)
    assert copy_of(AppConfig(mappings=[top, listener(65534)]), top).network.port == 65533
    out = gw("O")
    out.network.mode = "client"
    assert copy_of(AppConfig(mappings=[out]), out).network.port == out.network.port
    full = types.SimpleNamespace(mappings=[a] + [
        types.SimpleNamespace(name=str(p), kind="net", mqtt=types.SimpleNamespace(base_topic=""),
                              network=types.SimpleNamespace(listens=True, mode="server", port=p,
                                                            bind_ip="0.0.0.0",
                                                            both_families=False))
        for p in range(1, 65536)])
    try:
        copy_of(full, a)
        raise AssertionError("a copy without a free port was made")
    except ConfigError as e:
        assert str(e) == "No free TCP port is left on 127.0.0.1 for the copy.", e
    print("copy: port per transport and address, down from 65535, an error when none is "
          "free  OK")


def boot_config(ports, broker):
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    mq = {"enabled": True, "host": "127.0.0.1", "port": broker}
    a = gw("A", mid="aaaaaaaaaaa1", port=ports[0], client_id="gw-a").to_dict()
    b = gw("B", "plant/line3/", mid="aaaaaaaaaaa2", port=ports[1]).to_dict()  # a stored clash
    for m in (a, b):
        m["mqtt"].update(mq)
    return {"users": users, "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
            "cluster": {"enabled": True, "key": KEY, "discovery_port": free_port()},
            "mappings": [a, b]}


def test_server():
    broker = free_port()  # nothing listens there: the clients keep trying, that's all

    def form(name, base, points, port, **extra):
        return {"name": name, "kind": "net", "enabled": "on",
                "serial_port": f"/dev/ser2net-test-{name}", "serial_baudrate": "9600",
                "network_mode": "server", "network_protocol": "modbus",
                "network_bind_ip": "127.0.0.1", "network_port": str(port),
                "mqtt_enabled": "on", "mqtt_host": "127.0.0.1", "mqtt_port": str(broker),
                "mqtt_base_topic": base, "mqtt_payload_format": "plain",
                "modbus_points": json.dumps(points), **extra}

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

    def names():
        return [m["name"] for m in srv.saved_config()["mappings"]]

    with Server(boot_config([free_port(), free_port()], broker)) as srv:
        log = srv.output()
        assert ("mapping 'B': MQTT: Mapping 'A' also uses the MQTT topic 'plant/line3/pump/set'"
                in log), log[-3000:]
        assert "[B] warning: MQTT: Mapping 'A' also uses the MQTT topic" in log, log[-3000:]
        assert names() == ["A", "B"]
        print("boot: a stored clash loads, with a load note and a warning in the mapping's "
              "log  OK")

        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, body = b.post("/api/mappings/save", form("C", "plant/line3", [PUMP], free_port()))
        assert st == 400 and "takes writes from it" in body, (st, body[:500])
        st, _, body = b.post("/api/mappings/save", form("C", "plant/c", [dict(TEMP, name="status")],
                                                        free_port()))
        assert st == 400 and "Rename the point" in body, (st, body[:500])
        assert names() == ["A", "B"]
        st, _, body = b.post("/api/mappings/save", form("C", "plant/line5", [PUMP], free_port()))
        assert st == 200 and names() == ["A", "B", "C"], (st, body[:300])
        stored_b = srv.saved_config()["mappings"][1]
        edit = form("B renamed", "plant/line3/", [PUMP, TEMP], stored_b["network"]["port"],
                    id=stored_b["id"])
        st, _, body = b.post("/api/mappings/save", edit)
        assert st == 400 and "takes writes from it" in body, "an edit must fix the clash"
        edit.pop("enabled")
        st, _, _ = b.post("/api/mappings/save", edit)
        assert st == 200 and names()[1] == "B renamed", "disabling it is an edit that fixes it"
        # ...and starting it again is refused like a shared serial port (REST, the UI's
        # Start and bulk start): it stays off
        st, out = api("POST", f"/api/v1/mappings/{stored_b['id']}/start", {})
        assert st == 400 and "takes writes from it" in json.dumps(out), (st, out)
        st, _, body = b.post(f"/api/mappings/{stored_b['id']}/start", htmx=True)
        assert st == 400 and "takes writes from it" in body, (st, body[:300])
        st, _, _ = b.post("/api/mappings/bulk", {"action": "start", "ids": [stored_b["id"]]},
                          htmx=True)
        assert st == 400, st
        assert srv.saved_config()["mappings"][1]["enabled"] is False
        print("form: a /set topic shared with an enabled mapping and a reserved point name "
              "refused; another base topic, or the clashing one disabled, saved  OK")

        req = urllib.request.Request(
            srv.base + "/api/cluster/mapping-save", method="POST",
            data=urllib.parse.urlencode(form("P", "plant/line3", [PUMP], free_port())).encode(),
            headers={"X-Cluster-Key": KEY, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("a cluster node saved a clashing mapping")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "takes writes from it" in e.read().decode(), e.code
        print("cluster: the node a mapping is saved on refuses a clash  OK")

        raw = {"name": "R", "serial": {"port": "/dev/ser2net-test-R"},
               "network": {"bind_ip": "127.0.0.1", "port": free_port()},
               "mqtt": {"enabled": True, "host": "127.0.0.1", "port": broker,
                        "base_topic": "r", "client_id": "gw-a"}}
        st, out = api("POST", "/api/v1/mappings", raw)
        assert st == 400 and "client ID" in out.get("detail", ""), (st, out)
        raw["mqtt"]["client_id"] = "gw-r"
        st, out = api("POST", "/api/v1/mappings", raw)
        assert st == 201, (st, out)
        rid = out["id"]
        raw["mqtt"]["client_id"] = "gw-a"
        st, out = api("PUT", f"/api/v1/mappings/{rid}", raw)
        assert st == 400 and "'gw-a'" in out.get("detail", ""), (st, out)
        bad = dict(raw, name="R2", mqtt=dict(raw["mqtt"], base_topic="r/#", client_id=""))
        st, out = api("POST", "/api/v1/mappings", bad)
        assert st == 400 and "base topic" in out.get("detail", ""), (st, out)
        # a PATCH is checked like a PUT, and so is the REST import (dry run and apply)
        req = urllib.request.Request(
            srv.base + f"/api/v1/mappings/{rid}", method="PATCH",
            data=json.dumps({"mqtt": {"client_id": "gw-a"}}).encode(),
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/merge-patch+json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("a PATCH to a shared client ID was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "client ID" in e.read().decode(), e.code
        clash = {"mappings": [dict(raw, name="R3", serial={"port": "/dev/ser2net-test-R3"},
                                   network=dict(raw["network"], port=free_port()))]}
        st, out = api("POST", "/api/v1/import?dry_run=true", clash)
        assert st == 200 and out["valid"] is False and "client ID" in out["problem"], (st, out)
        before = names()
        st, out = api("POST", "/api/v1/import", clash)
        assert st == 400 and "client ID" in out.get("detail", "") and names() == before, (st, out)
        print("REST API: a shared client ID and a wildcard base topic refused (POST, PUT, "
              "PATCH, import)  OK")

        export = json.dumps({"mappings": [gw("D", "plant/line3", [PUMP], port=free_port())
                                          .to_dict() | {"mqtt": {"enabled": True,
                                                                 "host": "127.0.0.1",
                                                                 "port": broker,
                                                                 "base_topic": "plant/line3"}}]})
        st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                             files={"file": ("m.json", export.encode())})
        assert st == 200 and "be applied" in page and "takes writes from it" in page, page[:500]
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        before = names()
        st, _, page = b.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "Import rejected" in page and "takes writes from it" in page, page[:500]
        assert names() == before
        print("import: the preview shows the clash and applying it is refused  OK")

        st, _, _ = b.post("/api/mappings/aaaaaaaaaaa1/duplicate", {})
        assert st == 200
        copy = next(m for m in srv.saved_config()["mappings"] if m["name"] == "A (copy)")
        orig = srv.saved_config()["mappings"][0]
        assert (copy["enabled"], copy["mqtt"]["client_id"], copy["mqtt"]["base_topic"]) == (
            False, "", "plant/line3-copy"), copy["mqtt"]
        assert copy["network"]["port"] != orig["network"]["port"]
        enable = form("A2", "plant/line3-copy", [PUMP, TEMP], copy["network"]["port"],
                      id=copy["id"])
        st, _, body = b.post("/api/mappings/save", enable)
        assert st == 200, (st, body[:400])
        assert next(m for m in srv.saved_config()["mappings"] if m["id"] == copy["id"])["enabled"]
        print("copy: disabled, no client ID, '-copy' base topic, its own port; enabled next to "
              "the original after its serial port changed  OK")


def main():
    test_topic_rules()
    test_clashes()
    test_serial_writes_topic()
    test_stored_clashes()
    test_copy()
    test_server()
    print("\nPASS: MQTT topics, client IDs and Sparkplug B edge nodes stay apart")


if __name__ == "__main__":
    main()
