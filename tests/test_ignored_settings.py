"""Settings a mapping accepts but can't use are said, not silently ignored.

  * telnet over UDP has always run raw (there is no connection to negotiate on): it is
    read as raw, with a load note / an edit warning — never refused, so an old config
    with it keeps loading and saving;
  * MQTT on a serial-to-serial bridge, MQTT on a Modbus gateway without poll points and a
    close-on string on UDP do nothing: the runner logs each one when it starts (in
    English, also when started from a Turkish web UI), the edit form, the REST API and
    the import preview show them; UDP no longer logs "closing N client(s)" for close-on.

Run: python3 tests/test_ignored_settings.py
"""
import asyncio
import json
import os
import socket
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb import i18n
from ser2netweb.config import AppConfig, MappingConfig
from ser2netweb.engine.bridge import MappingRunner, log_ignored_settings
from ser2netweb.importer import parse_mappings_file

MQTT = {"enabled": True, "host": "broker", "base_topic": "plant"}


def mapping(**over):
    # fixed port: built, never started (the UDP runner below gets its own free port)
    d = {"id": "aaaaaaaaaaa1", "name": "m", "serial": {"port": "/dev/ser2net-test-ign"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4001}}
    d.update(over)
    return MappingConfig.from_dict(d)


def test_telnet_over_udp():
    m = mapping(network={"mode": "udp", "protocol": "telnet", "bind_ip": "127.0.0.1",
                         "port": 4001})
    assert m.network.protocol == "raw"
    m.validate()
    m.validate_input()  # a warning, not a refusal
    assert m.input_warnings() == ["Telnet is not supported over UDP, so the protocol is raw "
                                  "(as it always was in effect)."], m.input_warnings()
    assert mapping(network={"mode": "server", "protocol": "telnet"}).network.protocol == "telnet"
    cfg = AppConfig.from_dict({"mappings": [m.to_dict() | {"network": dict(
        m.to_dict()["network"], protocol="telnet")}]})
    assert cfg.mappings[0].network.protocol == "raw"
    assert ("mapping 'm': network.protocol telnet is not supported over UDP: raw is used (as "
            "it always was: no telnet negotiation or escaping over UDP)") in cfg.load_notes, \
        cfg.load_notes
    cfg.validate()
    print("telnet over UDP: read as raw (what always ran), with a note / warning  OK")


def test_ignored_settings():
    assert mapping().ignored_settings() == []
    assert mapping(mqtt=MQTT).ignored_settings() == [], "a raw mapping publishes its lines"
    bridge = mapping(kind="serialbridge", serial_b={"port": "/dev/ser2net-test-ign-b"}, mqtt=MQTT)
    assert bridge.ignored_settings() == [
        "MQTT is on, but a serial-to-serial bridge does not publish to MQTT: its MQTT "
        "settings are not used."]
    gw = mapping(network={"mode": "server", "protocol": "modbus", "port": 502}, mqtt=MQTT)
    assert "nothing is published" in gw.ignored_settings()[0]
    polled = mapping(network={"mode": "server", "protocol": "modbus", "port": 502}, mqtt=MQTT,
                     modbus_poll={"points": [{"name": "t", "unit": 1, "fn": 3, "address": 0,
                                              "dtype": "uint16"}]})
    assert polled.ignored_settings() == []
    udp = mapping(network={"mode": "udp", "port": 4001}, options={"closeon": "BYE"})
    assert "no connections to close" in udp.ignored_settings()[0]
    assert mapping(options={"closeon": "BYE"}).ignored_settings() == []
    for m in (bridge, gw, udp):
        m.validate()  # accepted, as always
    # in the language of the request (the form), but in English for the log
    token = i18n.set_language("tr")
    try:
        assert "MQTT açık" in bridge.ignored_settings()[0], bridge.ignored_settings()
        logged: list[str] = []
        log_ignored_settings(bridge, logged.append)
        assert logged == ["warning: MQTT is on, but a serial-to-serial bridge does not publish "
                          "to MQTT: its MQTT settings are not used."], logged
        assert i18n.current() == "tr", "the request's language is back afterwards"
    finally:
        i18n.reset(token)
    print("ignored settings: MQTT on a serial bridge / a gateway without points, close-on "
          "over UDP; English in the log  OK")


def test_import_preview():
    doc = {"mappings": [
        {"name": "bridge", "kind": "serialbridge", "serial": {"port": "/dev/a"},
         "serial_b": {"port": "/dev/b"}, "mqtt": MQTT},
        {"name": "udp", "serial": {"port": "/dev/c"},
         "network": {"mode": "udp", "protocol": "telnet", "port": 4001}}]}
    plan = parse_mappings_file(json.dumps(doc).encode())
    assert plan.mappings[1].network.protocol == "raw"
    text = "\n".join(plan.warnings)
    assert "Mapping #1 (bridge): MQTT is on, but a serial-to-serial bridge" in text, text
    assert "Mapping #2 (udp): Telnet is not supported over UDP" in text, text
    print("import preview: names the ignored settings and telnet-over-UDP  OK")


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_udp_closeon_runner():
    m = mapping(network={"mode": "udp", "bind_ip": "127.0.0.1", "port": free_udp_port()},
                options={"closeon": "BYE"})
    log: list[str] = []
    runner = MappingRunner(m, logger=log.append)
    assert runner._closeon == b"", "UDP has nothing to close (the read loop checks only a set one)"
    await runner.start()
    try:
        assert any("warning: A close-on string is set, but a UDP mapping has no connections"
                   in x for x in log), log
    finally:
        await runner.stop()
    tcp = MappingRunner(mapping(options={"closeon": "BYE"}))
    assert tcp._closeon == b"BYE", "TCP mappings keep close-on"
    print("UDP close-on: not acted on (no 'closing N client(s)'), the start says why  OK")


def main():
    test_telnet_over_udp()
    test_ignored_settings()
    test_import_preview()
    asyncio.run(test_udp_closeon_runner())
    print("\nPASS: settings that do nothing are said")


if __name__ == "__main__":
    main()
