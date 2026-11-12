"""A mapping's MQTT topics are ones a message can be published to, and its points don't
land on topics the mapping uses for something else.

  * an edit (validate_input) refuses '+', '#' or the null character in a plain base topic
    or point name, and a point published where the mapping already publishes or takes
    something else (its status, a unit's status, another point's /set or its result); '/'
    stays legal, Sparkplug B metric names are not topics; validate() takes them all.
Cross-platform, no sockets.

Run: python3 tests/test_mqtt_identity.py
"""
import _server  # noqa: F401  (puts the repository on sys.path)

from app.config import AppConfig, ConfigError, MappingConfig

PUMP = {"name": "pump", "fn": 1, "dtype": "bool", "writable": True}
TEMP = {"name": "temp", "fn": 3, "address": 1}


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


def main():
    test_topic_rules()
    print("\nPASS: MQTT topics a message can be published to, none taken twice")


if __name__ == "__main__":
    main()
