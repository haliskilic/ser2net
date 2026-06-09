"""MQTT publisher: line buffering + birth/death (Phase 2).

Uses a fake MQTT client injected into MqttPublisher, so this runs cross-platform
with no broker and without paho-mqtt installed. Verifies that complete
newline-delimited serial lines are published to the base topic, partial lines are
buffered, CR is stripped, and online/offline status is published with retain.

Run: python3 tests/test_mqtt_pub.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MqttSettings
from app.engine.mqtt_pub import MqttPublisher


class FakeClient:
    def __init__(self):
        self.published = []   # (topic, payload, qos, retain)
        self.will = None
        self.loop_started = False
        self.disconnected = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, host, port, keepalive=30):
        pass

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload), qos, retain))


def _pub(fake, topic):
    return [p for (t, p, _q, _r) in fake.published if t == topic]


def test_lines_published_and_buffered():
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="ser2net/dev", qos=1),
                        logger=lambda _m: None, client=fake)
    assert pub.connect() is True
    # birth message: online, retained, on <base>/status
    assert ("ser2net/dev/status", b"online", 1, True) in fake.published

    pub.feed(b"line-1\r\nline-2\n")          # two complete lines (CRLF + LF)
    pub.feed(b"partial")                      # buffered, not yet published
    assert _pub(fake, "ser2net/dev") == [b"line-1", b"line-2"], fake.published
    pub.feed(b"-rest\nlast")                  # completes 'partial-rest', 'last' buffered
    assert _pub(fake, "ser2net/dev") == [b"line-1", b"line-2", b"partial-rest"]
    print("complete lines published (CR stripped), partial line buffered  OK")

    # QoS is carried through
    assert all(q == 1 for (t, _p, q, _r) in fake.published if t == "ser2net/dev")
    print("data published with the configured QoS  OK")

    pub.close()
    assert ("ser2net/dev/status", b"offline", 1, True) in fake.published
    assert fake.disconnected and not fake.loop_started
    print("close() publishes retained 'offline' and tears down the client  OK")


def test_disabled_until_connected():
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t"),
                        logger=lambda _m: None, client=fake)
    pub.feed(b"before-connect\n")             # not connected yet -> dropped
    assert _pub(fake, "t") == []
    print("feed() before connect() publishes nothing  OK")


def test_will_is_set_for_lwt():
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="x", qos=2),
                        logger=lambda _m: None, client=fake)
    pub.connect()
    assert fake.will == ("x/status", b"offline", 2, True), fake.will
    print("Last-Will set to retained 'offline' on the status topic  OK")


def main():
    test_lines_published_and_buffered()
    test_disabled_until_connected()
    test_will_is_set_for_lwt()
    print("\nPASS: MQTT publisher (line buffering + birth/death)")


if __name__ == "__main__":
    main()
