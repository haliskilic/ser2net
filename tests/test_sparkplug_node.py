"""The Sparkplug B edge node's session state (app/engine/sparkplug_node.py), without a broker.

A SparkplugNode on a MqttPublisher whose paho client is a stand-in that records what is
published and the Will; the broker's CONNACK is handle_connect(), a lost connection
_paho_on_disconnect(). Checks:
  * no DDATA before the session's NBIRTH and DBIRTHs (at start and after a reconnect): a
    poll that ends meanwhile only updates the values the births then carry;
  * report by exception: a NaN that stays NaN is not sent again on every poll;
  * bdSeq is an Int64 metric in NBIRTH and NDEATH (the Will);
  * a metric is stamped with the time its value was read, in DDATA and in every DBIRTH
    (a rebirth doesn't restamp old values as current); DBIRTH lists metrics oldest first;
  * bdSeq across runs: with a data folder each session's bdSeq is stored per edge node
    (group/node) and the next run goes on from it (255 wraps to 0); a file that can't be
    read or written gives a random start, logged once, and never an error; the data folder
    reaches the node from the Supervisor (AppState passes its data_dir);
  * primary host (opt-in): the node subscribes to its STATE (3.0 and 2.2 topics; the
    retained STATE is taken, a retained command still is not) and births only while it is
    online; offline -> NDEATH, a clean disconnect and a new session (bdSeq + 1, once),
    which waits again; an older 3.0 STATE, junk and a rebirth request while the host is
    offline are ignored; a bad stored id is logged and ignored; edits refuse '/', '+', '#';
    the form keeps the field when it is not posted; stopping during the restart leaves no
    connection behind.

Run: python3 tests/test_sparkplug_node.py
"""
import asyncio
import json
import logging
import math
import os
import shutil
import tempfile
import time
from unittest import mock

from _server import ROOT  # noqa: F401  (sys.path setup)

from app.config import AppConfig, ConfigError, MappingConfig
from app.engine import sparkplug, sparkplug_node
from app.engine.sparkplug_node import SparkplugNode
from app.engine.supervisor import make_runner
from app.state import AppState, ConfigStore
from app.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields

POINTS = [
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
    {"name": "level", "unit": 2, "fn": 3, "address": 30, "dtype": "uint16"},
]


class FakeClient:
    """paho's client, as far as MqttPublisher uses it. ``connected``: a disconnect() then
    calls on_disconnect, as paho does once the DISCONNECT is out."""

    def __init__(self):
        self.published, self.wills, self.subscribed, self.calls = [], [], [], []
        self.on_message = self.on_connect = self.on_disconnect = None
        self.connected = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, bytes(payload), qos, retain))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload), qos, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")
        if self.connected:
            self.connected = False
            self.on_disconnect(self, None, None, None, None)

    def connect_async(self, host, port, keepalive=60):
        self.calls.append("connect_async")

    def loop_start(self):
        self.calls.append("loop_start")


def mapping(points=None, **mqtt):
    m = MappingConfig.from_dict({
        "name": "Line 3 PLC", "kind": "net", "serial": {"port": "fake"},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                    "port": 1502},
        "mqtt": dict({"enabled": True, "host": "broker", "base_topic": "plant/a",
                      "payload_format": "sparkplug_b"}, **mqtt),
        "modbus_poll": {"interval_s": 1, "points": points or POINTS}})
    m.validate()
    return m


def node_with(m=None, logs=None, **kw):
    """A node whose publisher has 'connected' (the Will is set), not yet CONNACKed."""
    client = FakeClient()
    node = SparkplugNode(m or mapping(), (logs.append if logs is not None else (lambda _m: None)),
                         lambda point, value: None, **kw)
    pub = node.publisher()
    pub._client = client
    pub.connect()
    return node, pub, client


def topic(kind, dev=None):
    return sparkplug.topic("ser2net", kind, "Line_3_PLC", dev)


def sent(client, kind, dev=None):
    return [sparkplug.decode_payload(p) for t, p, _q, _r in client.published
            if t == topic(kind, dev)]


def values(payload):
    return {m.name: (None if m.is_null else m.value) for m in payload.metrics}


def test_no_data_before_births():
    node, pub, client = node_with()
    node.update({"temp": 21.5, "level": 7})          # a poll ends before the CONNACK
    assert client.published == [], client.published
    pub.handle_connect()                              # CONNACK: births with those values
    assert values(sent(client, "DBIRTH", "unit1")[-1]) == {"temp": 21.5, "setpoint": None}
    assert values(sent(client, "DBIRTH", "unit2")[-1]) == {"level": 7}
    assert sent(client, "DDATA", "unit1") == [] and sent(client, "DDATA", "unit2") == []
    node.update({"temp": 22.0})
    assert [values(p) for p in sent(client, "DDATA", "unit1")] == [{"temp": 22.0}]
    assert sent(client, "DDATA", "unit1")[-1].seq == 3, "seq counts on from the births"

    pub._paho_on_disconnect(client, None)             # lost: nothing until the next births
    node.update({"temp": 23.0, "level": 8})
    assert len(sent(client, "DDATA", "unit1")) == 1 and sent(client, "DDATA", "unit2") == []
    pub.handle_connect()
    birth = sent(client, "NBIRTH")[-1]
    assert birth.seq == 0 and values(birth)["bdSeq"] == 1
    bdseq = {m.name: m for m in birth.metrics}["bdSeq"]
    will = sparkplug.decode_payload(client.wills[-1][1]).metrics[0]
    assert bdseq.datatype == will.datatype == sparkplug.DataType.Int64, (bdseq, will)
    assert values(sent(client, "DBIRTH", "unit1")[-1])["temp"] == 23.0
    assert values(sent(client, "DBIRTH", "unit2")[-1]) == {"level": 8}
    node.update({"temp": 24.0})
    assert sent(client, "DDATA", "unit1")[-1].seq == 3
    print("no DDATA before the session's births; values read meanwhile go into the births  OK")


def test_nan_sent_once():
    node, pub, client = node_with()
    pub.handle_connect()
    nan = float("nan")
    for _ in range(3):
        node.update({"temp": nan, "level": 7})
    ddata = sent(client, "DDATA", "unit1")
    assert len(ddata) == 1 and math.isnan(values(ddata[0])["temp"]), ddata
    assert len(sent(client, "DDATA", "unit2")) == 1
    node.update({"temp": 1.5})
    node.update({"temp": nan})
    assert [values(p)["temp"] for p in sent(client, "DDATA", "unit1")][1] == 1.5
    assert len(sent(client, "DDATA", "unit1")) == 3
    print("a NaN point is reported once, not on every poll  OK")


def test_read_timestamps():
    node, pub, client = node_with()
    t0 = int(time.time() * 1000) - 60_000            # read a minute ago
    node.update({"setpoint": 5, "temp": 20.0}, {"setpoint": t0 + 10, "temp": t0})
    before = int(time.time() * 1000)
    pub.handle_connect()
    birth = sent(client, "DBIRTH", "unit1")[-1]
    assert [(m.name, m.timestamp) for m in birth.metrics] == [("temp", t0), ("setpoint", t0 + 10)]
    level = sent(client, "DBIRTH", "unit2")[-1].metrics[0]
    assert level.is_null and level.timestamp >= before, "a null is stamped with the birth"
    node.update({"temp": 20.5, "setpoint": 5}, {"temp": t0 + 500, "setpoint": t0 + 600})
    ddata = sent(client, "DDATA", "unit1")[-1]
    assert [(m.name, m.timestamp) for m in ddata.metrics] == [("temp", t0 + 500)], ddata
    assert ddata.timestamp >= before, "the payload is stamped when it is sent"
    node.birth()                                      # a rebirth: last read times, not now
    birth = sent(client, "DBIRTH", "unit1")[-1]
    assert [(m.name, m.timestamp) for m in birth.metrics] == [("temp", t0 + 500),
                                                              ("setpoint", t0 + 600)], birth
    node.update({"level": 3})                         # no stamp given: now
    assert sent(client, "DDATA", "unit2")[-1].metrics[0].timestamp >= before
    print("metrics carry their read time (DDATA, DBIRTH, rebirth); DBIRTH oldest first  OK")


def test_bdseq_across_runs():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, sparkplug_node.BDSEQ_FILE)

        def will_bdseq(client):
            return sparkplug.decode_payload(client.wills[-1][1]).metrics[0].value

        node, pub, client = node_with(state_dir=d)
        assert will_bdseq(client) == 0 == node.bdseq, "no stored bdSeq: the first run starts at 0"
        pub._paho_on_disconnect(client, None)
        pub._paho_on_disconnect(client, None)
        assert will_bdseq(client) == 2
        assert json.load(open(path)) == {"ser2net/Line_3_PLC": 2}
        node.close()

        # the next run (a restart, a reboot after a power loss) goes on from there
        node, pub, client = node_with(state_dir=d)
        assert will_bdseq(client) == 3, "the next run continues after the stored bdSeq"
        pub.handle_connect()
        assert values(sent(client, "NBIRTH")[-1])["bdSeq"] == 3
        # another edge node has its own; a node renamed starts over
        other, _pub, other_client = node_with(mapping(sparkplug_node_id="line-4"), state_dir=d)
        assert will_bdseq(other_client) == 0
        assert json.load(open(path)) == {"ser2net/Line_3_PLC": 3, "ser2net/line-4": 0}
        assert sorted(os.listdir(d)) == [sparkplug_node.BDSEQ_FILE], os.listdir(d)
        # 255 wraps to 0
        sparkplug_node.save_bdseq(path, "ser2net/Line_3_PLC", 255)
        assert node_with(state_dir=d)[0].bdseq == 0
        # no data folder: in memory only, every run at 0, nothing written
        assert node_with()[0].bdseq == 0
        print("bdSeq stored per edge node in the data folder; the next run goes on from it  OK")

        # a file that makes no sense: a random start, logged, and the file set right
        for junk in ("{not json", "[1, 2]", '{"ser2net/Line_3_PLC": "7"}',
                     '{"ser2net/Line_3_PLC": 300}', '{"ser2net/Line_3_PLC": true}'):
            with open(path, "w") as fh:
                fh.write(junk)
            logs = []
            node = node_with(state_dir=d, logs=logs)[0]
            assert 0 <= node.bdseq <= 255
            assert sum("this run starts at a random bdSeq" in m for m in logs) == 1, logs
            assert json.load(open(path))["ser2net/Line_3_PLC"] == node.bdseq
        print("an unreadable bdSeq file gives a random start (logged) and is rewritten  OK")

        # a data folder that can't be written: a random start, logged once, never an error
        if os.name == "posix" and os.geteuid() != 0:
            os.remove(path)
            os.chmod(d, 0o500)
            try:
                logs = []
                node, pub, client = node_with(state_dir=d, logs=logs)
                for _ in range(3):
                    pub._paho_on_disconnect(client, None)
                pub.handle_connect()
                assert sum("bdSeq" in m for m in logs) == 1, logs
                assert "can't be stored" in logs[0], logs
                assert will_bdseq(client) == (node.bdseq) and sent(client, "NBIRTH")
            finally:
                os.chmod(d, 0o700)
            print("a data folder that can't be written: random start, logged once  OK")
        else:
            print("SKIP: read-only data folder (needs POSIX, not as root)")

        # a save that starts failing later is logged once; the sessions go on
        logs = []
        node, pub, client = node_with(state_dir=d, logs=logs)
        with mock.patch.object(sparkplug_node, "save_bdseq", side_effect=OSError("disk full")):
            for _ in range(3):
                pub._paho_on_disconnect(client, None)
        assert sum("can't be stored" in m and "disk full" in m for m in logs) == 1, logs
        assert will_bdseq(client) == (json.load(open(path))["ser2net/Line_3_PLC"] + 3) % 256

    # the data folder reaches the node: AppState -> Supervisor -> make_runner -> gateway
    d = tempfile.mkdtemp(prefix="s2n_bdseq_")
    try:
        state = AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig())
        assert state.supervisor.state_dir == state.data_dir == os.path.abspath(d)
        runner = make_runner(mapping(), lambda _m: None, state.supervisor.trace_dir,
                             state.supervisor.state_dir)
        assert runner.state_dir == state.data_dir
    finally:
        # AppState's log handlers keep all.log open (Windows can't remove it then)
        logger = logging.getLogger("ser2net")
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)
        shutil.rmtree(d, ignore_errors=True)
    print("the data folder is passed from AppState through the Supervisor to the gateway  OK")


def state(online, stamp):
    return json.dumps({"online": online, "timestamp": stamp}).encode()


async def test_primary_host():
    host_topic, legacy = "spBv1.0/STATE/scada-1", "STATE/scada-1"
    logs = []
    node, pub, client = node_with(mapping(sparkplug_primary_host_id=" scada-1 "), logs=logs)
    client.connected = True
    pub.handle_connect()                              # CONNACK: subscribed, but no births yet
    assert host_topic in client.subscribed and legacy in client.subscribed, client.subscribed
    assert sent(client, "NBIRTH") == [] and node._born is False
    assert sum("waiting for primary host 'scada-1'" in m for m in logs) == 1, logs
    node.update({"temp": 20.0})
    assert sent(client, "DDATA", "unit1") == []
    # the host's STATE is retained: the replay on subscribe is taken (a command's is not)
    pub.handle_message(host_topic, state(True, 1000), retained=True)
    assert len(sent(client, "NBIRTH")) == 1 and values(sent(client, "DBIRTH", "unit1")[-1])["temp"] == 20.0
    assert any("primary host 'scada-1' is online" in m for m in logs), logs
    node.update({"temp": 21.0})
    assert len(sent(client, "DDATA", "unit1")) == 1
    pub.handle_message(host_topic, state(True, 1001))  # online again: no second birth
    assert len(sent(client, "NBIRTH")) == 1
    print("primary host: births wait for its STATE online (retained replay taken)  OK")

    # an older STATE (a stale retained one, another host's clock) and junk are ignored
    for payload in (state(False, 999), b"{not json", b'{"online": "no"}', b"[true]", b"\xff"):
        pub.handle_message(host_topic, payload)
    assert sent(client, "NDEATH") == [] and node._born
    assert sum("Sparkplug STATE of 'scada-1' ignored" in m for m in logs) == 5, logs

    # offline: NDEATH (this session's bdSeq), a clean disconnect, a new session; bdSeq + 1
    bdseq = node.bdseq
    pub.handle_message(host_topic, state(False, 2000))
    assert node._born is False and node._restart is not None
    await asyncio.wait_for(node._restart, 2)
    death = sent(client, "NDEATH")
    assert len(death) == 1 and values(death[0])["bdSeq"] == bdseq, death
    assert client.calls[-4:] == ["disconnect", "loop_stop", "connect_async", "loop_start"]
    assert node.bdseq == (bdseq + 1) % 256, "bdSeq + 1, once"
    assert values(sparkplug.decode_payload(client.wills[-1][1]))["bdSeq"] == node.bdseq
    node.update({"temp": 22.0})
    assert len(sent(client, "DDATA", "unit1")) == 1, "no data while the host is offline"
    pub.handle_connect()                              # the new session: still waiting
    pub.handle_message(host_topic, state(False, 2000), retained=True)
    for _ in range(2):  # a rebirth request while the host is offline is not a birth
        node.on_node_command(topic("NCMD"), sparkplug.encode_payload(sparkplug.Payload(
            0, [sparkplug.Metric(sparkplug.REBIRTH, sparkplug.DataType.Boolean, True)], None)))
    assert len(sent(client, "NBIRTH")) == 1
    pub.handle_message(host_topic, state(True, 3000))
    births = sent(client, "NBIRTH")
    assert len(births) == 2 and values(births[-1])["bdSeq"] == node.bdseq
    assert values(sent(client, "DBIRTH", "unit1")[-1])["temp"] == 22.0
    print("primary host offline: NDEATH, clean disconnect, new session (bdSeq + 1) that waits; "
          "back online: births  OK")

    # the older Sparkplug 2.2 STATE/<id> ONLINE / OFFLINE; not connected: the Will is renewed
    # by the restart itself (no disconnect callback), still once
    client.connected = False
    bdseq = node.bdseq
    pub.handle_message(legacy, b"OFFLINE")
    await asyncio.wait_for(node._restart, 2)
    assert node.bdseq == (bdseq + 1) % 256 and len(sent(client, "NDEATH")) == 2
    pub.handle_connect()
    pub.handle_message(legacy, b" online\n", retained=True)
    assert len(sent(client, "NBIRTH")) == 3
    pub.handle_message(legacy, b"maybe")
    assert any("'maybe'" in m and "neither ONLINE nor OFFLINE" in m for m in logs), logs
    print("primary host, Sparkplug 2.2 STATE/<id> ONLINE / OFFLINE  OK")

    # the mapping stops while the new session is being made: no connection is left behind,
    # whenever the stop comes (close() runs on the loop, the restart on a worker thread)
    client.calls.clear()
    pub.handle_message(legacy, b"OFFLINE")
    node.close()                                      # before the restart began
    await asyncio.sleep(0.1)
    assert client.calls == ["loop_stop", "disconnect"], client.calls
    for during, expected in (
            ("loop_stop", ["disconnect", "loop_stop"]),       # no new connection
            ("connect_async", ["disconnect", "loop_stop", "connect_async", "loop_start",
                               "loop_stop", "disconnect"])):  # the new one is undone
        node, pub, client = node_with(mapping(sparkplug_primary_host_id="scada-1"))
        real = getattr(client, during)

        def closing(*a, real=real, pub=pub, **k):
            real(*a, **k)
            pub._closing = True                       # what close() sets first
        setattr(client, during, closing)
        await pub.restart()
        assert client.calls == expected, (during, client.calls)
    print("stopped while a new session is made: no connection left behind  OK")

    # no primary host: births at once, no STATE subscription; a bad stored id: logged, ignored
    for host, said in (("", False), ("a/b", True), ("#", True)):
        logs = []
        node, pub, client = node_with(mapping(sparkplug_primary_host_id=host), logs=logs)
        pub.handle_connect()
        assert len(sent(client, "NBIRTH")) == 1 and not any("STATE" in t for t in client.subscribed)
        assert any("primary host ID ignored" in m for m in logs) == said, logs
    print("no primary host (or a bad stored one, logged): births at once  OK")

    # edits refuse an id with '/', '+' or '#'; a stored one still loads and saves
    for bad in ("a/b", "x+", "#", 5):
        m = MappingConfig.from_dict(mapping(sparkplug_primary_host_id=bad).to_dict())
        m.validate()
        AppConfig.from_dict({"mappings": [m.to_dict()]}).validate()
        try:
            m.validate_input()
            raise AssertionError(f"an edit took primary host ID {bad!r}")
        except ConfigError as e:
            assert "primary host ID" in str(e), e
    mapping(sparkplug_primary_host_id="scada-1").validate_input()
    mapping(sparkplug_primary_host_id="a/b", payload_format="plain").validate_input()
    assert MappingConfig().mqtt.sparkplug_primary_host_id == ""
    base = {"name": "gw", "kind": "net", "serial_port": "COM7", "network_protocol": "modbus",
            "network_port": "502", "mqtt_enabled": "on", "mqtt_host": "b",
            "mqtt_base_topic": "p", "mqtt_payload_format": "sparkplug_b"}
    existing = mapping(sparkplug_primary_host_id="scada-1")

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.mqtt.sparkplug_primary_host_id
    assert parsed(dict(base, id=existing.id, mqtt_sparkplug_primary_host_id=" scada-2 ")) == "scada-2"
    assert parsed(dict(base, id=existing.id, mqtt_sparkplug_primary_host_id="")) == ""
    assert parsed(dict(base, id=existing.id)) == "scada-1", "not posted (plain, older peer): kept"
    assert parsed(base) == ""
    print("primary host ID: edits refuse '/', '+', '#' (stored ones load); the form keeps it "
          "when not posted  OK")


def main():
    test_no_data_before_births()
    test_nan_sent_once()
    test_read_timestamps()
    test_bdseq_across_runs()
    asyncio.run(test_primary_host())
    print("\nPASS: Sparkplug B edge node session state")


if __name__ == "__main__":
    main()
