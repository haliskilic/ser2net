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

Run: python3 tests/test_sparkplug_node.py
"""
import math
import time

from _server import ROOT  # noqa: F401  (sys.path setup)

from app.config import MappingConfig
from app.engine import sparkplug
from app.engine.sparkplug_node import SparkplugNode

POINTS = [
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True},
    {"name": "level", "unit": 2, "fn": 3, "address": 30, "dtype": "uint16"},
]


class FakeClient:
    """paho's client, as far as MqttPublisher uses it."""

    def __init__(self):
        self.published, self.wills, self.subscribed = [], [], []
        self.on_message = self.on_connect = self.on_disconnect = None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.wills.append((topic, bytes(payload), qos, retain))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload), qos, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


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


def main():
    test_no_data_before_births()
    test_nan_sent_once()
    test_read_timestamps()
    print("\nPASS: Sparkplug B edge node session state")


if __name__ == "__main__":
    main()
