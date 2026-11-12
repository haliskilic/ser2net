"""MQTT publisher: its connection to the broker is logged and shown.

  * paho's callbacks, driven by hand (no paho needed): connecting is logged at start; a
    failed attempt (no answer, DNS, TLS: paho's on_connect_fail, inside its except block),
    a refused CONNACK (paho 2 reason code, paho 1.x number) and a connection closed before
    any CONNACK are each logged once until the problem changes or a connect works; the
    disconnect paho reports after a refusal is not a second problem; a lost connection is
    logged, a disconnect that was asked for (restart) is not; a refused subscription is
    logged; MqttPublisher.state follows (connecting / connected / down, error, since);
  * a publish paho refuses (a wildcard in a point's topic) is logged once per topic and
    counted as dropped, as is a QoS 0 message published while the broker is away; a base
    topic with '+' or '#' keeps MQTT off (state off, logged), and so does a missing paho;
  * with paho-mqtt, against the test broker: refused (not authorised) and can't connect
    (nothing listening) are logged once over several attempts, connected and lost are
    logged, a refused SUBACK is logged;
  * a mapping's status carries the publisher's state while it runs (REST API, dashboard),
    {} once stopped, and /metrics has ser2net_mqtt_connected and
    ser2net_mqtt_messages_dropped_total for it.

Run: python3 tests/test_mqtt_link.py
"""
import asyncio
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _mqtt_broker import MqttBroker
from _optional import need

from app.config import MappingConfig, MqttSettings
from app.engine.mqtt_pub import MqttPublisher


class Code:
    """paho 2's ReasonCode, as far as the publisher looks at it."""

    def __init__(self, name, failure=True):
        self.name, self.is_failure = name, failure

    def __str__(self):
        return self.name

    def __eq__(self, other):
        return False

    __hash__ = object.__hash__


class Info:
    def __init__(self, rc):
        self.rc = rc


class FakeClient:
    def __init__(self):
        self.published = []
        self.mids = 0
        self.rc = 0

    def will_set(self, *a, **k):
        pass

    def subscribe(self, topic, qos=0):
        self.mids += 1
        return (0, self.mids)

    def publish(self, topic, payload, qos=0, retain=False):
        if "#" in topic or "+" in topic:
            raise ValueError("Publish topic cannot contain wildcards.")
        self.published.append((topic, payload, qos, retain))
        return Info(self.rc)


def failed_attempt(pub, exc):
    """What paho does when an attempt raises: on_connect_fail inside its except block."""
    try:
        raise exc
    except OSError:
        pub._paho_on_connect_fail(None, None)


async def flush():
    await asyncio.sleep(0.02)


async def callbacks():
    logs = []
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="broker.lan", port=8883, tls=True,
                                     base_topic="plant/a"), logger=logs.append, client=fake,
                        subscriptions={"plant/a/x/set": lambda t, p: None,
                                       "plant/a/y/set": lambda t, p: None})
    assert pub.connect() is True
    assert logs == ["MQTT: connecting to broker.lan:8883 (TLS), topic 'plant/a'"], logs
    assert pub.state["state"] == "connecting" and pub.state["broker"] == "broker.lan:8883"
    logs.clear()

    for _ in range(3):  # paho retries; the same problem is logged once
        failed_attempt(pub, ConnectionRefusedError(111, "Connection refused"))
    await flush()
    assert logs == ["MQTT: can't connect to broker.lan:8883: [Errno 111] Connection refused; "
                    "trying again"], logs
    assert pub.state["state"] == "down" and "Connection refused" in pub.state["error"]
    since = pub.state["since"]
    failed_attempt(pub, TimeoutError("timed out"))  # another problem: logged
    await flush()
    assert logs[-1] == "MQTT: can't connect to broker.lan:8883: timed out; trying again"
    assert pub.state["since"] == since, "still down: since is the time it went down"

    logs.clear()  # a refused CONNACK; paho then reports the disconnect too
    for _ in range(2):
        pub._paho_on_connect(fake, None, {}, Code("Not authorized"))
        pub._paho_on_disconnect(fake, None, {}, Code("Unspecified error"), None)
    await flush()
    assert logs == ["MQTT: broker.lan:8883 refused the connection: Not authorized; "
                    "trying again"], logs
    assert pub.state["error"] == "broker.lan:8883 refused the connection: Not authorized"
    pub._paho_on_connect(fake, None, {}, 5)  # paho 1.x: a number (texts come from paho)
    await flush()
    assert logs[-1].startswith("MQTT: broker.lan:8883 refused the connection: "), logs
    assert len(logs) == 2, logs

    logs.clear()  # accepted
    pub._paho_on_connect(fake, None, {}, Code("Success", failure=False))
    await flush()
    assert logs == ["MQTT: connected to broker.lan:8883"], logs
    assert pub.state["state"] == "connected" and pub.state["error"] == ""
    assert ("plant/a/status", b"online", 0, True) in fake.published

    # a refused subscription (paho 2: a failure code; paho 1.x: granted QoS 128)
    pub._paho_on_subscribe(fake, None, 1, [Code("Not authorized")], None)
    pub._paho_on_subscribe(fake, None, 2, (1,))          # granted: nothing to say
    await flush()
    assert logs[-1] == ("MQTT: broker.lan:8883 refused the subscription to 'plant/a/x/set'; "
                        "messages on it won't arrive (the broker's access rules?)"), logs
    pub._paho_on_subscribe(fake, None, 2, (128,))
    await flush()
    assert "'plant/a/y/set'" in logs[-1] and len(logs) == 3, logs

    logs.clear()  # lost
    pub._paho_on_disconnect(fake, None, {}, Code("Keep alive timeout"), None)
    await flush()
    assert logs == ["MQTT: lost the connection to broker.lan:8883: Keep alive timeout; "
                    "trying again"], logs
    assert pub.state["state"] == "down"
    failed_attempt(pub, ConnectionRefusedError(111, "Connection refused"))
    await flush()
    assert len(logs) == 2, "the problem changed: logged again"

    logs.clear()  # connected again, then a disconnect that was asked for (a restart)
    pub._paho_on_connect(fake, None, {}, 0)
    pub._paho_on_disconnect(fake, None, {}, Code("Normal disconnection", failure=False), None)
    await flush()
    assert logs == ["MQTT: connected to broker.lan:8883"], logs
    assert pub.state["state"] == "connecting" and pub.state["error"] == ""
    pub._paho_on_disconnect(fake, None, 7)  # paho 1.x: closed before any CONNACK
    await flush()
    assert logs[-1].startswith("MQTT: broker.lan:8883 closed the connection before accepting "
                               "it: "), logs
    pub._paho_on_disconnect(fake, None, 7)
    await flush()
    assert len(logs) == 2, logs

    pub.close()  # late callbacks after close change nothing
    logs.clear()
    failed_attempt(pub, ConnectionRefusedError(111, "Connection refused"))
    pub._paho_on_disconnect(fake, None, {}, Code("Keep alive timeout"), None)
    await flush()
    assert logs == [], logs


def test_callbacks():
    asyncio.run(callbacks())
    print("connecting / can't connect / refused / closed before CONNACK / connected / lost / "
          "refused subscription: logged once per problem, state follows  OK")


def test_publish_problems():
    logs = []
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="plant/a"),
                        logger=logs.append, client=fake)
    pub.connect()
    pub.handle_connect()
    logs.clear()
    for _ in range(3):
        pub.publish_value("tank/#", b"1")
    pub.publish_value("tank/+", b"1")
    assert logs == ["MQTT: can't publish to 'plant/a/tank/#': Publish topic cannot contain "
                    "wildcards.",
                    "MQTT: can't publish to 'plant/a/tank/+': Publish topic cannot contain "
                    "wildcards."], logs
    assert pub.state["dropped"] == 4, pub.state
    fake.rc = 4  # MQTT_ERR_NO_CONN: the broker is away; at QoS 0 the message is lost
    pub.publish_value("level", b"7")
    assert pub.state["dropped"] == 5 and len(logs) == 2, (pub.state, logs)
    print("a publish paho refuses: logged once per topic, counted; QoS 0 while away "
          "counted  OK")

    for base in ("plant/#", "plant/+/a"):
        logs.clear()
        fake = FakeClient()
        pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic=base),
                            logger=logs.append, client=fake)
        assert pub.connect() is False
        assert logs == [f"MQTT publishing disabled: the base topic {base!r} contains '+' or "
                        "'#', which a message can't be published to"], logs
        assert pub.state["state"] == "off" and "contains '+' or '#'" in pub.state["error"]
        pub.feed(b"line\n")
        assert fake.published == []
    # Sparkplug B ignores the base topic
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="a/#"),
                        logger=logs.append, client=FakeClient(), birth=False)
    assert pub.connect() is True
    print("a base topic with '+' or '#': MQTT off, said once  OK")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            return False
        await asyncio.sleep(0.05)
    return True


async def paho_checks():
    logs = []

    def publisher(port, **kw):
        logs.clear()
        pub = MqttPublisher(MqttSettings(enabled=True, host="127.0.0.1", port=port,
                                         base_topic="plant/a"), logger=logs.append, **kw)
        assert pub.connect() is True
        pub._client.reconnect_delay_set(min_delay=0.2, max_delay=0.2)
        return pub

    with MqttBroker(connack=5) as broker:  # not authorised
        pub = publisher(broker.port)
        assert await until(lambda: len(broker.connects) >= 3), broker.connects
        await asyncio.sleep(0.1)
        pub.close()
        assert logs == [f"MQTT: connecting to 127.0.0.1:{broker.port}, topic 'plant/a'",
                        f"MQTT: 127.0.0.1:{broker.port} refused the connection: Not authorized;"
                        " trying again"], logs
    print("paho: a refused connection is logged once over several attempts  OK")

    port = free_port()  # nothing listens there
    pub = publisher(port)
    assert await until(lambda: pub.state["state"] == "down")
    await asyncio.sleep(1)  # more attempts
    pub.close()
    assert len(logs) == 2 and logs[1].startswith(f"MQTT: can't connect to 127.0.0.1:{port}: "), \
        logs
    print("paho: can't connect (nothing listening) is logged once  OK")

    with MqttBroker(refuse={"plant/a/x/set"}) as broker:
        pub = publisher(broker.port, subscriptions={"plant/a/x/set": lambda t, p: None})
        assert await until(lambda: any("refused the subscription" in m for m in logs)), logs
        assert f"MQTT: connected to 127.0.0.1:{broker.port}" in logs, logs
        assert pub.state["state"] == "connected"
        broker.drop()
        assert await until(lambda: any("lost the connection" in m for m in logs)), logs
        assert await until(lambda: logs.count(f"MQTT: connected to 127.0.0.1:{broker.port}") == 2)
        assert pub.state["state"] == "connected" and pub.state["error"] == ""
        pub.close()
    print("paho: connected, a refused subscription, lost and connected again are logged  OK")


def test_paho():
    if not need("paho.mqtt", what="the MQTT connection log against a test broker"):
        return
    asyncio.run(paho_checks())


async def metrics_of(mapping, status):
    from app.config import AppConfig
    from app.web.routes import system

    class State:
        config = AppConfig(mappings=[mapping])

        class supervisor:
            @staticmethod
            def all_status():
                return {mapping.id: status}

    class Ctx:
        state = State
        render = None
    endpoint = next(r.endpoint for r in system.routes(Ctx) if r.path == "/metrics")
    return (await endpoint(None)).body.decode()


async def runner_checks():
    from app.engine.bridge import MappingRunner

    with MqttBroker() as broker:
        m = MappingConfig.from_dict({
            "id": "m1", "name": "meter", "serial": {"port": "/dev/ser2net-test-missing"},
            "network": {"mode": "client", "remote_host": "127.0.0.1",
                        "remote_port": free_port()},
            "mqtt": {"enabled": True, "host": "127.0.0.1", "port": broker.port,
                     "base_topic": "plant/meter"}})
        r = MappingRunner(m, logger=lambda _m: None)
        await r.start()
        try:
            assert await until(lambda: r.status.as_dict()["mqtt"].get("state") == "connected")
            st = r.status.as_dict()["mqtt"]
            assert st["broker"] == f"127.0.0.1:{broker.port}" and st["dropped"] == 0, st
            body = await metrics_of(m, r.status.as_dict())
            assert 'ser2net_mqtt_connected{mapping="meter",id="m1"} 1' in body, body
            assert 'ser2net_mqtt_messages_dropped_total{mapping="meter",id="m1"} 0' in body
        finally:
            await r.stop()
        assert r.status.as_dict()["mqtt"] == {}
        assert "ser2net_mqtt" not in await metrics_of(m, r.status.as_dict())
    body = await metrics_of(m, {"state": "running"})  # an older status: no mqtt key
    assert "ser2net_mqtt" not in body
    down = {"state": "down", "broker": "b:1883", "since": 1, "error": "x", "dropped": 3}
    body = await metrics_of(m, {"state": "running", "mqtt": down})
    assert ('ser2net_mqtt_connected{mapping="meter",id="m1"} 0\n'
            'ser2net_mqtt_messages_dropped_total{mapping="meter",id="m1"} 3') in body, body


def test_runner_status_and_metrics():
    if not need("paho.mqtt", what="a mapping's MQTT status and metrics"):
        return
    asyncio.run(runner_checks())
    print("status mqtt while the mapping runs ({} once stopped), /metrics series  OK")


def main():
    test_callbacks()
    test_publish_problems()
    test_paho()
    test_runner_status_and_metrics()
    print("\nPASS: MQTT connection log, status and metrics")


if __name__ == "__main__":
    main()
