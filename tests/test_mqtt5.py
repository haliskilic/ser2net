"""MQTT 5 (mqtt.protocol "5", opt-in per mapping; MQTT 3.1.1 stays the default).

  * settings: protocol 3.1.1, clean start on, no expiry for new mappings and for a config
    of 3.0; an edit refuses another protocol, an expiry that is not 0-4294967295 whole
    seconds and clean start off without a client ID; a stored config with such values still
    loads and validates (the publisher then speaks 3.1.1 / uses no expiry, and says so);
    MQTT 5 settings on a 3.1.1 mapping, and session settings on a Sparkplug B one, are
    named as doing nothing; the form posts them (the MQTT 5 block only for version 5) and
    keeps what a form without them leaves out (an older fleet peer's);
  * paho's callbacks, driven by hand in each shape paho hands them over (paho 2; paho 1.6
    with MQTT 5: reason code objects without is_failure, on_disconnect(rc, properties)):
    a CONNACK's and a DISCONNECT's reason code is named with its number in the log and kept
    in the state (reason_code), a refused subscription names its reason, a publication the
    broker refuses is counted (rejected) and logged once per reason and connection, and
    on_link is told when the connection is there and when it is gone;
  * with paho-mqtt (2.x and 1.6) against the test broker, which speaks MQTT 5 too: a
    3.1.1 mapping connects as before (protocol level 4, no properties); an MQTT 5 one with
    protocol level 5, clean start and no session expiry, or with clean start off and the
    session expiry of its settings (also after a restart of the session); retained data
    carries the message expiry (serial lines and polled values; not the status, not data
    that is not retained, not the Will) and the broker no longer hands it out afterwards;
    a refused connection, a refused subscription, the broker's DISCONNECT and a refused
    publication are logged with their reason codes; a Sparkplug B node always connects
    with clean start and no session expiry, takes a DCMD and publishes its births.

Run: python3 tests/test_mqtt5.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _mqtt_broker import MqttBroker
from _optional import need

from ser2netweb.config import AppConfig, ConfigError, MappingConfig, MqttSettings
from ser2netweb.engine import sparkplug
from ser2netweb.engine.mqtt_pub import MqttPublisher
from ser2netweb.web.routes.forms import (
    FORM_BLOCKS_SINCE, _build_mapping_dict, _preserve_unmanaged_fields, blocks_not_read,
)


def gateway(mqtt=None, fmt="plain"):
    return MappingConfig.from_dict({
        "name": "gw", "kind": "net", "serial": {"port": "/dev/ttyS0"},
        "network": {"mode": "server", "protocol": "modbus", "port": 1502},
        "mqtt": dict({"enabled": True, "host": "b", "base_topic": "plant/a",
                      "payload_format": fmt}, **(mqtt or {})),
        "modbus_poll": {"points": [{"name": "x", "unit": 1, "fn": 3, "address": 0,
                                    "writable": True}]}})


def test_settings():
    for mq in (MqttSettings(), MappingConfig.from_dict({"mqtt": {"enabled": True}}).mqtt):
        assert (mq.protocol, mq.clean_start, mq.session_expiry_s, mq.message_expiry_s) \
            == ("3.1.1", True, 0, 0), mq
    assert gateway().ignored_settings() == []
    for good in ({}, {"protocol": "5"}, {"protocol": "3.1.1"},
                 {"protocol": "5", "session_expiry_s": 4294967295, "message_expiry_s": 60},
                 {"protocol": "5", "clean_start": False, "client_id": "gw1",
                  "session_expiry_s": 3600}):
        m = gateway(good)
        m.validate()
        m.validate_input()
    assert gateway({"protocol": 5}).mqtt.protocol == "5", "a JSON number reads as the text"
    for bad, why in (({"protocol": "4"}, "protocol version must be one of 3.1.1, 5"),
                     ({"protocol": "5.0"}, "protocol version"),
                     ({"protocol": "5", "session_expiry_s": -1}, "MQTT session expiry must"),
                     ({"protocol": "5", "session_expiry_s": 4294967296}, "session expiry"),
                     ({"protocol": "5", "message_expiry_s": -5}, "MQTT message expiry must"),
                     ({"protocol": "5", "clean_start": False}, "needs a client ID")):
        m = gateway(bad)
        m.validate()  # a stored config keeps loading and saving
        AppConfig.from_dict({"mappings": [m.to_dict()]}).validate()
        try:
            m.validate_input()
        except ConfigError as e:
            assert why in str(e), (bad, str(e))
        else:
            raise AssertionError(f"accepted: {bad}")
    # Sparkplug B starts clean whatever is set: clean start off needs no client ID there
    gateway({"protocol": "5", "clean_start": False}, fmt="sparkplug_b").validate_input()
    unused = gateway({"session_expiry_s": 60}).ignored_settings()
    assert len(unused) == 1 and "MQTT 5 settings" in unused[0], unused
    assert "MQTT 5 settings" in gateway({"message_expiry_s": 60}).ignored_settings()[0]
    assert "MQTT 5 settings" in gateway({"clean_start": False}).ignored_settings()[0]
    unused = gateway({"protocol": "5", "session_expiry_s": 60},
                     fmt="sparkplug_b").ignored_settings()
    assert len(unused) == 1 and "Sparkplug B always starts a clean" in unused[0], unused
    assert gateway({"protocol": "5", "session_expiry_s": 60, "client_id": "a",
                    "clean_start": False}).ignored_settings() == []
    stored = gateway({"protocol": "5", "session_expiry_s": 60}).to_dict()["mqtt"]
    assert stored["protocol"] == "5" and stored["session_expiry_s"] == 60
    assert stored["clean_start"] is True and stored["message_expiry_s"] == 0
    print("settings: 3.1.1 by default; an edit refuses another protocol, a bad expiry, "
          "clean start off without a client ID; a stored config still loads  OK")

    # a stored protocol / expiry the edits refuse: 3.1.1, no expiry, said in the log
    logs = []
    pub = MqttPublisher(MqttSettings(enabled=True, host="b", base_topic="t", protocol="6",
                                     session_expiry_s=-1, message_expiry_s="60"),
                        logger=logs.append, client=FakeClient())
    assert pub.connect() is True and pub.state["protocol"] == "3.1.1"
    assert "reason_code" not in pub.state and "rejected" not in pub.state
    assert logs == ["MQTT: connecting to b:1883, topic 't'",
                    "MQTT: protocol '6' is neither 3.1.1 nor 5: MQTT 3.1.1 is used"], logs
    pub = MqttPublisher(MqttSettings(enabled=True, host="b", base_topic="t", protocol="5",
                                     session_expiry_s=-1, message_expiry_s=2 ** 32),
                        client=FakeClient())
    assert (pub._session_expiry, pub._message_expiry) == (0, 0)
    print("a stored protocol or expiry the edits refuse: MQTT 3.1.1 / no expiry, logged  OK")


def test_form():
    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502", "mqtt_enabled": "on",
            "mqtt_host": "b", "mqtt_base_topic": "plant/a", "mqtt_client_id": "gw1"}
    existing = MappingConfig.from_dict({
        "name": "gw", "serial": {"port": "COM7"}, "network": {"protocol": "modbus", "port": 502},
        "mqtt": {"protocol": "5", "clean_start": False, "client_id": "gw1",
                 "session_expiry_s": 600, "message_expiry_s": 90}})

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        mq = m.mqtt
        return mq.protocol, mq.clean_start, mq.session_expiry_s, mq.message_expiry_s

    v5 = dict(base, mqtt_protocol="5", mqtt_v5_shown="1", mqtt_session_expiry_s="30",
              mqtt_message_expiry_s="45")
    assert parsed(dict(v5, mqtt_clean_start="on")) == ("5", True, 30, 45)
    assert parsed(dict(v5, id=existing.id)) == ("5", False, 30, 45), "unticked: off"
    # version 3.1.1 selected: the MQTT 5 block is not posted, and what is stored stays
    assert parsed(dict(base, id=existing.id, mqtt_protocol="3.1.1")) == ("3.1.1", False, 600, 90)
    assert parsed(dict(base, id=existing.id)) == ("5", False, 600, 90), "an older form: kept"
    assert parsed(dict(base)) == ("3.1.1", True, 0, 0), "a new mapping without them"
    try:
        parsed(dict(v5, mqtt_session_expiry_s="soon"))
    except ConfigError as e:
        assert "MQTT session expiry" in str(e), str(e)
    else:
        raise AssertionError("a session expiry that is no number was accepted")
    assert FORM_BLOCKS_SINCE["mqtt5"] == (3, 1, 0) and FORM_BLOCKS_SINCE["point_safety"] == (3, 1, 0)
    assert {"mqtt5", "point_safety"} <= blocks_not_read((3, 0, 1))
    assert not {"mqtt5", "point_safety"} & blocks_not_read((3, 1, 0))
    print("form: the MQTT version and MQTT 5's settings are parsed when posted, kept when "
          "not (3.1.1 selected, an older peer's form)  OK")


# ---- paho's callbacks, driven by hand ----
class Code:
    """A reason code as paho hands it over: paho 2's ReasonCode (is_failure), or paho
    1.6's ReasonCodes for MQTT 5 (only a value and a name)."""

    def __init__(self, name, value, paho2=True):
        self.name, self.value = name, value
        if paho2:
            self.is_failure = value >= 0x80

    def __str__(self):
        return self.name

    def __eq__(self, other):
        return False

    __hash__ = object.__hash__


class Info:
    rc = 0


class FakeClient:
    def __init__(self):
        self.published = []
        self.mids = 0

    def will_set(self, *a, **k):
        pass

    def subscribe(self, topic, qos=0):
        self.mids += 1
        return (0, self.mids)

    def publish(self, topic, payload, qos=0, retain=False, **options):
        self.published.append((topic, payload, qos, retain, options))
        return Info()


async def flush():
    await asyncio.sleep(0.02)


async def callbacks():
    for paho2 in (True, False):
        logs, links = [], []
        fake = FakeClient()
        pub = MqttPublisher(MqttSettings(enabled=True, host="b", base_topic="t", protocol="5",
                                         qos=1), logger=logs.append, client=fake,
                            subscriptions={"t/x/set": lambda t, p: None}, on_link=links.append)
        assert pub.connect() is True
        assert fake.on_publish is not None
        assert pub.state["protocol"] == "5" and pub.state["reason_code"] is None
        assert logs == ["MQTT: connecting to b:1883, topic 't' (MQTT 5: clean start)"], logs
        denied = Code("Not authorized", 0x87, paho2)
        fake.on_connect(fake, None, {}, denied, None)
        if paho2:   # the disconnect paho reports after a refusal
            fake.on_disconnect(fake, None, None, denied, None)
        else:
            fake.on_disconnect(fake, None, denied, None)
        await flush()
        assert pub.state["state"] == "down" and pub.state["reason_code"] == 0x87, pub.state
        assert logs[-1] == ("MQTT: b:1883 refused the connection: Not authorized (reason code "
                            "0x87); trying again"), logs
        assert links == [], "never connected: nothing to tell"

        fake.on_connect(fake, None, {}, Code("Success", 0, paho2), None)
        await flush()
        assert pub.state["state"] == "connected" and pub.state["reason_code"] is None
        assert links == [True]
        fake.on_subscribe(fake, None, 1, [Code("Topic filter invalid", 0x8F, paho2)], None)
        await flush()
        assert logs[-1].startswith("MQTT: b:1883 refused the subscription to 't/x/set': Topic "
                                   "filter invalid (reason code 0x8F); "), logs
        quota = Code("Quota exceeded", 0x97, paho2)
        for _ in range(3):
            fake.on_publish(fake, None, 5, quota, None)
        fake.on_publish(fake, None, 6, Code("No matching subscribers", 0x10, paho2), None)
        fake.on_publish(fake, None, 7)  # (paho 1.6 passes no reason code)
        await flush()
        assert pub.state["rejected"] == 3, pub.state
        assert [m for m in logs if "refused a publication" in m] == [
            "MQTT: b:1883 refused a publication: Quota exceeded (reason code 0x97) (each "
            "reason is logged once per connection; the status counts them)"], logs

        moved = Code("Server moved", 0x9D, paho2)
        if paho2:
            fake.on_disconnect(fake, None, None, moved, None)
        else:
            fake.on_disconnect(fake, None, moved, None)
        await flush()
        assert pub.state["state"] == "down" and pub.state["reason_code"] == 0x9D
        assert logs[-1] == ("MQTT: lost the connection to b:1883: Server moved (reason code "
                            "0x9D); trying again"), logs
        assert links == [True, False]
        fake.on_connect(fake, None, {}, Code("Success", 0, paho2), None)
        await flush()
        fake.on_publish(fake, None, 8, quota, None)  # a new connection: logged again
        if not paho2:  # paho 1.6, the link broke: an error number, and no properties
            fake.on_disconnect(fake, None, 7, None)
            await flush()
            assert pub.state["state"] == "down" and pub.state["reason_code"] is None
            fake.on_connect(fake, None, {}, Code("Success", 0, paho2), None)
        await flush()
        assert sum("refused a publication" in m for m in logs) == 2 and pub.state["rejected"] == 4
        assert links[-1] is True
        pub.close()
        assert links[-1] is False and links.count(False) == (2 if paho2 else 3), links
    print("callbacks (paho 2 and paho 1.6 shapes): reason codes of CONNACK, SUBACK, PUBACK "
          "and DISCONNECT are named, kept and counted; on_link follows the connection  OK")


def test_callbacks():
    asyncio.run(callbacks())


# ---- with paho-mqtt against the test broker ----
async def until(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.05)
    return True


async def paho_checks():
    import paho.mqtt
    paho2 = not paho.mqtt.__version__.startswith("1.")
    logs = []

    def publisher(broker, mqtt=None, **kw):
        logs.clear()
        settings = MqttSettings.from_dict(dict({
            "enabled": True, "host": "127.0.0.1", "port": broker.port,
            "base_topic": "plant/a", "retain": True}, **(mqtt or {})))
        pub = MqttPublisher(settings, logger=logs.append, **kw)
        assert pub.connect() is True
        pub._client.reconnect_delay_set(min_delay=0.2, max_delay=0.2)
        return pub

    with MqttBroker() as broker:  # MQTT 3.1.1, as before
        pub = publisher(broker, {"message_expiry_s": 60, "session_expiry_s": 60})
        assert await until(lambda: pub.state["state"] == "connected"), logs
        pub.publish_value("temp", b"21")
        assert await until(lambda: broker.topics("plant/a/temp"))
        c = broker.connects[0]
        assert (c["protocol"], c["clean"], c["properties"]) == (4, True, {}), c
        assert broker.properties == [{}, {}], broker.properties
        assert logs[0] == f"MQTT: connecting to 127.0.0.1:{broker.port}, topic 'plant/a'"
        await pub.aclose()
    print("paho: an MQTT 3.1.1 mapping connects and publishes as before  OK")

    with MqttBroker() as broker:
        pub = publisher(broker, {"protocol": "5", "message_expiry_s": 60, "qos": 1})
        assert await until(lambda: pub.state["state"] == "connected"), logs
        pub.publish_value("temp", b"21")
        pub.feed(b"a line\n")
        pub.publish_raw("plant/a/unit1/status", b"online", retain=True)
        assert await until(lambda: len(broker.messages) == 4), broker.messages
        c = broker.connects[0]
        assert (c["protocol"], c["clean"], c["properties"]) == (5, True, {}), c
        assert c["will"] == ("plant/a/status", b"offline", 1, True) and c["will_properties"] == {}
        sent = dict(zip([m[0] for m in broker.messages], broker.properties, strict=True))
        assert sent == {"plant/a/status": {}, "plant/a/temp": {"message_expiry": 60},
                        "plant/a": {"message_expiry": 60}, "plant/a/unit1/status": {}}, sent
        assert logs[0].endswith("(MQTT 5: clean start, retained data expires after 60 s)")
        await pub.aclose()
        assert broker.properties[-1] == {} and broker.messages[-1][1] == b"offline"

        pub = publisher(broker, {"protocol": "5", "message_expiry_s": 60, "retain": False})
        assert await until(lambda: pub.state["state"] == "connected")
        pub.publish_value("temp", b"22")
        assert await until(lambda: broker.payloads("plant/a/temp")[-1] == b"22")
        assert broker.properties[-1] == {}, "not retained: no expiry"
        await pub.aclose()
    print("paho: MQTT 5 with clean start; retained lines and values carry the message "
          "expiry, the status, the Will and data that is not retained none  OK")

    with MqttBroker() as broker:
        pub = publisher(broker, {"protocol": "5", "clean_start": False, "client_id": "gw1",
                                 "session_expiry_s": 300})
        assert await until(lambda: pub.state["state"] == "connected"), logs
        c = broker.connects[0]
        assert (c["protocol"], c["clean"], c["client_id"]) == (5, False, "gw1"), c
        assert c["properties"] == {"session_expiry": 300}, c
        assert logs[0].endswith("(MQTT 5: resumes its session, the session kept for 300 s)")
        await pub.restart()
        assert await until(lambda: len(broker.connects) == 2
                           and pub.state["state"] == "connected"), logs
        c = broker.connects[1]
        assert (c["clean"], c["properties"]) == (False, {"session_expiry": 300}), c
        broker.drop()
        assert await until(lambda: len(broker.connects) == 3)
        assert broker.connects[2]["properties"] == {"session_expiry": 300}
        await pub.aclose()
    print("paho: clean start off and the session expiry are in every CONNECT (a reconnect "
          "and a restarted session too)  OK")

    with MqttBroker() as broker:  # the broker stops handing out an expired retained value
        pub = publisher(broker, {"protocol": "5", "message_expiry_s": 1})
        assert await until(lambda: pub.state["state"] == "connected")
        pub.publish_value("temp", b"21")
        assert await until(lambda: broker.topics("plant/a/temp"))
        got = []
        seen = publisher(broker, {"protocol": "5", "base_topic": "other"},
                         subscriptions={"plant/a/temp": lambda t, p: got.append(p)},
                         state_topics=("plant/a/temp",))
        assert await until(lambda: got == [b"21"]), got
        await seen.aclose()
        await asyncio.sleep(1.2)
        late = publisher(broker, {"protocol": "5", "base_topic": "other"},
                         subscriptions={"plant/a/temp": lambda t, p: got.append(p)},
                         state_topics=("plant/a/temp",))
        assert await until(lambda: "plant/a/temp" in broker.subscriptions[1:])
        await asyncio.sleep(0.3)
        assert got == [b"21"], "expired: not handed out again"
        await late.aclose()
        await pub.aclose()
    print("paho: a retained value is not handed out once its message expiry has passed  OK")

    with MqttBroker(connack=0x87) as broker:
        pub = publisher(broker, {"protocol": "5"})
        assert await until(lambda: len(broker.connects) >= 3), broker.connects
        await asyncio.sleep(0.1)
        assert pub.state["state"] == "down" and pub.state["reason_code"] == 0x87, pub.state
        pub.close()
        assert logs == [f"MQTT: connecting to 127.0.0.1:{broker.port}, topic 'plant/a' "
                        "(MQTT 5: clean start)",
                        f"MQTT: 127.0.0.1:{broker.port} refused the connection: Not "
                        "authorized (reason code 0x87); trying again"], logs
    with MqttBroker(refuse={"plant/a/x/set"}) as broker:
        pub = publisher(broker, {"protocol": "5", "qos": 1},
                        subscriptions={"plant/a/x/set": lambda t, p: None})
        assert await until(lambda: any(
            "refused the subscription to 'plant/a/x/set': Not authorized (reason code 0x87)"
            in m for m in logs)), logs
        if paho2:  # (paho 1.6 passes no reason code to on_publish)
            broker.puback = 0x97
            pub.publish_value("temp", b"1")
            pub.publish_value("temp", b"2")
            assert await until(lambda: pub.state["rejected"] == 2), pub.state
            assert sum("refused a publication: Quota exceeded (reason code 0x97)" in m
                       for m in logs) == 1, logs
            broker.puback = 0
        broker.disconnect(0x8B)
        assert await until(lambda: any("lost the connection" in m for m in logs)), logs
        if paho2:  # (paho 1.6 reports the closed socket, not the DISCONNECT)
            assert any("Server shutting down (reason code 0x8B)" in m for m in logs), logs
        assert await until(lambda: pub.state["state"] == "connected"
                           and pub.state["reason_code"] is None)
        await pub.aclose()
    print("paho: a refused connection and subscription, a refused publication and the "
          "broker's DISCONNECT are logged with their reason codes  OK")

    with MqttBroker() as broker:  # Sparkplug B: clean start, no session, whatever is set
        from ser2netweb.engine.sparkplug_node import SparkplugNode
        m = gateway({"protocol": "5", "clean_start": False, "session_expiry_s": 300,
                     "client_id": "node1", "message_expiry_s": 60, "host": "127.0.0.1",
                     "port": broker.port}, fmt="sparkplug_b")
        written = []
        node = SparkplugNode(m, logs.append, lambda point, value, **kw: written.append(
            (point.name, value)))
        pub = node.publisher()
        assert pub.connect() is True
        nbirth = "spBv1.0/ser2net/NBIRTH/gw"
        assert await until(lambda: broker.topics(nbirth)), logs
        c = broker.connects[0]
        assert (c["protocol"], c["clean"], c["properties"]) == (5, True, {}), c
        assert c["will"][0] == "spBv1.0/ser2net/NDEATH/gw" and c["will"][2:] == (1, False)
        assert all(p == {} for p in broker.properties), broker.properties
        cmd = sparkplug.Payload(timestamp=1, seq=None, metrics=[sparkplug.Metric(
            name="x", datatype=sparkplug.DataType.UInt16, value=7)])
        broker.publish("spBv1.0/ser2net/DCMD/gw/unit1", sparkplug.encode_payload(cmd))
        assert await until(lambda: written == [("x", 7)]), written
        await node.aclose()
        assert broker.messages[-1][0] == "spBv1.0/ser2net/NDEATH/gw"
    print("paho: a Sparkplug B node on MQTT 5 always connects with clean start and no "
          "session expiry; births, a DCMD and the NDEATH work  OK")


def test_paho():
    if not need("paho.mqtt", what="MQTT 5 against a test broker"):
        return
    asyncio.run(paho_checks())


def main():
    test_settings()
    test_form()
    test_callbacks()
    test_paho()
    print("\nPASS: MQTT 5 (clean start, session and message expiry, reason codes)")


if __name__ == "__main__":
    main()
