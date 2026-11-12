"""Values of the wrong JSON type in a mapping or in config.json.

Mappings come in as JSON from config.json, the REST API, imports and the mapping form's
Modbus-points field. Checked here:
  * a value sent as text is read as the setting's type when that needs no guess
    ("false" -> false, "1" -> 1, "0.1" -> 0.1), so "false" no longer starts a mapping or
    makes a point writable, and a text scale no longer breaks the published values — but
    a protective switch (login, TLS, read-only) keeps reading text in config.json as on,
    as before, and edits refuse text for it;
  * a value that can't be read so is refused by an edit (validate_input, with the
    setting's path) and, in config.json, takes the default with a load note — never a
    refusal of the whole file (validate() gains no rule);
  * keys that are no setting are named (an edit's warnings, a load note);
  * a section or list that is not shaped like one still fails to load, as before;
  * validate() raises ConfigError, not TypeError, for a model built in code;
  * the Modbus poll loop survives a value the publisher can't take, and stop() a poll
    task that died; the REST API answers an unexpected error with JSON.

Run: python3 tests/test_config_types.py
"""
import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import (AppConfig, ClusterSettings, ConfigError, MappingConfig, ModbusPoint,
                        ModbusPoll)
from app.engine.modbus_gateway import ModbusGatewayRunner
from app.importer import parse_mappings_file

POINT = {"name": "coil", "unit": 1, "fn": 1, "address": 0, "dtype": "bool"}


def gateway(**over):
    d = {"name": "gw", "serial": {"port": "/dev/ser2net-test"},
         "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1",
                     "port": 5020},
         "mqtt": {"enabled": True, "host": "broker", "base_topic": "plant"},
         "modbus_poll": {"interval_s": 1, "points": [dict(POINT)]}}
    for key, value in over.items():
        d[key] = value
    return MappingConfig.from_dict(d)


def refused(m: MappingConfig, needle: str) -> None:
    try:
        m.validate_input()
    except ConfigError as e:
        assert needle in str(e), f"wrong error {e!r}, wanted {needle!r}"
        return
    raise AssertionError(f"an edit accepted it ({needle})")


def test_text_values_read_as_meant():
    m = MappingConfig.from_dict({
        "name": "m", "enabled": "false",
        "serial": {"port": "COM1", "baudrate": "19200", "stopbits": "1.5",
                   "advanced": {"rs485_enabled": "on"}},
        "network": {"port": "4002", "kick_old_user": "0", "allowed_client_ips": "10.0.0.0/8"},
        "mqtt": {"enabled": "FALSE", "qos": "1"}})
    assert m.enabled is False, "'false' must not start the mapping"
    assert (m.serial.baudrate, m.serial.stopbits, m.serial.advanced.rs485_enabled) == (19200, 1.5, True)
    assert (m.network.port, m.network.kick_old_user, m.mqtt.enabled, m.mqtt.qos) == (4002, False, False, 1)
    assert m.network.allowed_client_ips == ["10.0.0.0/8"], "a bare text is a one-entry list"
    m.validate()
    m.validate_input()  # an edit takes them as meant, without a word
    assert m.input_warnings() == [], m.input_warnings()

    gw = gateway(modbus_poll={"interval_s": "2", "points": [
        {"name": "coil", "unit": "1", "fn": "1", "address": "0", "dtype": "bool",
         "writable": "false"},
        {"name": "temp", "unit": 1, "fn": 4, "address": 2, "dtype": "uint16", "scale": "0.1"}]})
    p0, p1 = gw.modbus_poll.points
    assert (p0.unit, p0.fn, p0.address, p0.writable) == (1, 1, 0, False)
    assert p1.scale == 0.1 and gw.modbus_poll.interval_s == 2
    gw.validate()
    assert not gw.accepts_mqtt_writes, "'writable': 'false' must leave the coil read-only"
    runner = ModbusGatewayRunner(gw)
    assert runner._set_points == {}, "no MQTT /set subscription for a point that is not writable"
    from app.engine import modbus
    assert modbus.decode_value(b"\x00\x05", p1.dtype, p1.scale) == 0.5, "scale is a number now"
    print("text values: 'false' / '0' / '1' / '19200' / '0.1' read as meant; writable "
          "'false' is read-only, scale '0.1' multiplies  OK")


def test_protective_switches_need_false():
    """A switch that protects something (login, TLS, certificate checks, read-only) took any
    text as true before: "false" in quotes kept it on. Loading config.json keeps reading
    text that way (an upgrade never turns one off), with a note; an edit (REST API, import,
    form, fleet edit) refuses text for one, so a client sending "false" is told instead of
    finding the switch still on. JSON true / false work as always."""
    def edit(**sections):
        return MappingConfig.from_dict({"name": "m", "serial": {"port": "COM1"}, **sections})

    for sections, path in (({"network": {"read_only": "false"}}, "network.read_only"),
                           ({"network": {"tls": "0"}}, "network.tls"),
                           ({"mqtt": {"tls": "off"}}, "mqtt.tls"),
                           ({"network": {"read_only": "true"}}, "network.read_only"),
                           ({"network": {"read_only": ""}}, "network.read_only")):
        m = edit(**sections)
        try:
            m.validate_input()
        except ConfigError as e:
            assert str(e) == f"{path}: use true or false (JSON booleans), not a string.", e
        else:
            raise AssertionError(f"an edit took the text in {sections}")
    m = edit(network={"read_only": "false", "tls": "0"}, mqtt={"tls": "off"})
    assert (m.network.read_only, m.network.tls, m.mqtt.tls) == (True, True, True), \
        "read as before 2.9.0 (what a stored config.json gets)"
    edit(network={"read_only": "false"}).validate()  # loading never refuses it
    ok = edit(network={"read_only": False, "tls": False}, mqtt={"tls": True})
    ok.validate_input()
    assert (ok.network.read_only, ok.network.tls, ok.mqtt.tls) == (False, False, True)
    cfg = AppConfig.from_dict({"admin_ui": {"require_login": "false"},
                               "ldap": {"tls_verify": "no", "start_tls": "false"},
                               "mappings": [{"id": "aaaaaaaaaaa1", "name": "m",
                                             "serial": {"port": "COM1"},
                                             "network": {"read_only": "false"}}]})
    assert cfg.admin_ui.require_login is True
    assert (cfg.ldap.tls_verify, cfg.ldap.start_tls) == (True, True)
    assert cfg.mappings[0].network.read_only is True
    for note in ('config.json: admin_ui.require_login "false" is text, so it is read as true, '
                 "as before: this protective switch takes true or false (without quotes)",
                 'config.json: ldap.tls_verify "no" is text, so it is read as true',
                 "mapping 'm': network.read_only \"false\" is text, so it is read as true"):
        assert any(note in n for n in cfg.load_notes), (note, cfg.load_notes)
    cfg.validate()
    # an import is an edit: the file is refused, naming the mapping and the setting
    doc = {"mappings": [{"name": "m", "serial": {"port": "COM1"},
                         "network": {"read_only": "false"}}]}
    try:
        parse_mappings_file(json.dumps(doc).encode())
    except ConfigError as e:
        assert str(e) == ("Mapping #1 (m): network.read_only: use true or false (JSON "
                          "booleans), not a string."), e
    else:
        raise AssertionError("an import took a protective switch as text")
    print("protective switches (login, TLS, certificate check, read-only): config.json text "
          "keeps them on, as before; edits and imports refuse text  OK")


def test_nulls():
    m = MappingConfig.from_dict({"name": None, "serial": {"port": "COM1"},
                                 "network": {"remote_host": None, "port": None}})
    assert (m.name, m.network.remote_host, m.network.port) == ("", "", 4001)
    try:
        m.validate()
    except ConfigError as e:
        assert "name is required" in str(e), e
    else:
        raise AssertionError("a mapping without a name validated")
    gw = gateway(modbus_poll={"points": [dict(POINT, name=None)]})
    try:
        gw.validate()
    except ConfigError as e:
        assert "needs a name" in str(e), e
    else:
        raise AssertionError("a point without a name validated")
    print("null name / point name / port: ConfigError (no TypeError), null port = default  OK")


def test_edits_refuse_what_cannot_be_read():
    cases = [
        (gateway(modbus_poll={"points": [dict(POINT, unit="one")]}),
         'modbus_poll.points[0].unit: "one" is not a whole number'),
        (gateway(modbus_poll={"points": [dict(POINT, fn=3, dtype="uint16", scale="x")]}),
         'modbus_poll.points[0].scale: "x" is not a finite number'),
        (gateway(modbus_poll={"interval_s": "inf", "points": [dict(POINT)]}),
         'modbus_poll.interval_s: "inf" is not a finite number'),
        (gateway(enabled="maybe"), 'enabled: "maybe" is not true or false'),
        (gateway(mqtt={"enabled": True, "host": "b", "base_topic": "t", "port": [1883]}),
         "mqtt.port: [1883] is not a whole number"),
        (MappingConfig.from_dict({"name": "m", "serial": {"port": ["COM1"]}}),
         'serial.port: ["COM1"] is not text'),
        (MappingConfig.from_dict({"name": "m", "serial": {"port": "COM1", "match": "x"}}),
         'serial.match: "x" is not a JSON object'),
    ]
    for m, needle in cases:
        refused(m, needle)
    # what each one is instead (a stored config with them loads, see below)
    assert cases[0][0].modbus_poll.points[0].unit == 1
    assert cases[1][0].modbus_poll.points[0].scale == 1.0
    assert cases[2][0].modbus_poll.interval_s == 5.0
    assert cases[3][0].enabled is True, "a value that is no true/false keeps its old meaning"
    assert cases[4][0].mqtt.port == 1883 and cases[5][0].serial.port == ""
    print("edits refuse values that can't be read, naming the setting's path  OK")


def test_poll_interval():
    gw = gateway(modbus_poll={"interval_s": 0, "points": [dict(POINT)]})
    assert gw.modbus_poll.interval_s == 5.0, "0 has always meant the default"
    gw.validate_input()  # a warning, not a refusal
    assert any("interval_s 0 is not a poll interval" in w for w in gw.input_warnings())
    quiet = gateway(modbus_poll={"interval_s": 0, "points": []})
    assert quiet.input_warnings() == [], "no points: the interval doesn't matter"
    print("poll interval: 0 -> 5 s with a warning, inf refused  OK")


def test_unknown_keys():
    m = MappingConfig.from_dict({"name": "m", "baud": 115200, "status": {"state": "running"},
                                 "warnings": ["x"],
                                 "serial": {"port": "COM1", "baud": 115200, "advanced": {"x": 1}}})
    assert m.serial.baudrate == 9600
    warnings = m.input_warnings()
    assert len(warnings) == 1 and "baud, serial.baud, serial.advanced.x" in warnings[0], warnings
    assert "status" not in warnings[0], "what the REST API adds to a mapping is not reported"
    m.validate_input()  # warned about, never refused
    assert "input_notes" not in m.to_dict() and "baud" not in m.to_dict()
    print("unknown keys: named in the edit's warnings, never refused or stored  OK")


def test_structure_still_fails():
    for d, needle in (({"name": "m", "serial": "COM1"}, "serial must be a JSON object"),
                      ({"name": "m", "modbus_poll": {"points": [1]}},
                       "modbus_poll.points[0] must be a JSON object"),
                      ({"name": "m", "modbus_poll": {"points": "temp"}},
                       "modbus_poll.points must be a JSON list")):
        try:
            MappingConfig.from_dict(d)
        except ConfigError as e:
            assert needle in str(e), e
        else:
            raise AssertionError(f"read {d}")
    for d, needle in (({"users": "admin"}, "users must be a JSON list"),
                      ({"users": None}, "users must be a JSON list"),
                      ({"ldap": [1]}, "ldap must be a JSON object"),
                      ({"mappings": [None]}, "mappings[0] must be a JSON object")):
        try:
            AppConfig.from_dict(d)
        except ConfigError as e:  # a ValueError: start-up says the file is not valid
            assert needle in str(e), e
        else:
            raise AssertionError(f"loaded {d}")
    # empty ones are empty, as before
    cfg = AppConfig.from_dict({"mappings": None, "ldap": "", "admin_ui": {}})
    assert cfg.mappings == [] and not cfg.ldap.enabled
    print("a section / list of the wrong shape still fails to load (a broken users list "
          "never reads as 'no accounts yet')  OK")


def test_validate_is_type_safe():
    for point, needle in ((ModbusPoint(name="a", unit="1"), "unit must be 0-255"),  # type: ignore[arg-type]
                          (ModbusPoint(name="a", address=None), "address must be"),  # type: ignore[arg-type]
                          (ModbusPoint(name="a", scale="0.1"), "scale must be a number"),  # type: ignore[arg-type]
                          (ModbusPoint(name=None), "needs a name")):  # type: ignore[arg-type]
        try:
            ModbusPoll(points=[point]).validate()
        except ConfigError as e:
            assert needle in str(e), e
        else:
            raise AssertionError(f"validated {point}")
    print("ModbusPoll.validate: ConfigError, not TypeError, for a model built in code  OK")


def test_load_normalizes_and_notes():
    stored = {"version": "1", "admin_ui": {"port": "8081", "require_login": "true"},
              "cluster": {"legacy_auth": None, "discovery": "maybe", "peers": "10.0.0.2:8080"},
              "notifications": {"telegram_chat_id": -100123, "repeat_s": "600"},
              "mappings": [{
                  "id": "aaaaaaaaaaa1", "name": "gw", "enabled": "false", "kind": "net",
                  "serial": {"port": "/dev/ttyUSB0", "baud": 115200},
                  "network": {"mode": "server", "protocol": "modbus", "port": "5020",
                              "keepalive_count": "lots"},
                  "mqtt": {"enabled": "true", "host": "b", "base_topic": "t"},
                  "modbus_poll": {"interval_s": None, "points": [
                      dict(POINT, writable="false"),
                      {"name": "t", "unit": "2", "fn": 3, "address": 1, "dtype": "uint16",
                       "scale": "0.5"}]}}]}
    cfg = AppConfig.from_dict(json.loads(json.dumps(stored)))
    m = cfg.mappings[0]
    assert (cfg.version, cfg.admin_ui.port, cfg.admin_ui.require_login) == (1, 8081, True)
    assert (cfg.cluster.legacy_auth, cfg.cluster.discovery) == (True, True), \
        "these two keep their default for a value that is no true/false, as in 2.8.1"
    assert cfg.cluster.peers == ["10.0.0.2:8080"]
    assert (cfg.notifications.telegram_chat_id, cfg.notifications.repeat_s) == ("-100123", 600)
    assert (m.enabled, m.network.port, m.network.keepalive_count, m.mqtt.enabled) == (False, 5020, 3, True)
    assert m.modbus_poll.interval_s == 5.0
    assert m.modbus_poll.points[0].writable is False and m.modbus_poll.points[1].scale == 0.5
    cfg.validate()  # loads and saves: nothing is refused
    notes = "\n".join(cfg.load_notes)
    for needle in ('config.json: admin_ui.port "8081" is read as 8081',
                   'config.json: cluster.discovery "maybe" is not true or false: true is used',
                   "mapping 'gw': enabled \"false\" is read as false (versions before 2.9.0 "
                   "took any text but \"\" as true)",
                   "mapping 'gw': modbus_poll.points[0].writable \"false\" is read as false",
                   "mapping 'gw': modbus_poll.points[1].scale \"0.5\" is read as 0.5",
                   "mapping 'gw': network.keepalive_count \"lots\" is not a whole number: 3 is used",
                   "mapping 'gw': not settings of this version, so they are not used and are "
                   "left out when config.json is next saved (backups/ keeps the file as it "
                   "was): serial.baud"):
        assert needle in notes, f"missing note {needle!r} in:\n{notes}"
    assert "interval_s" not in notes, "null for a number is that number left unset"
    # saved once, the file has the right types and loads without a note
    again = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert again.load_notes == [], again.load_notes
    print("config.json: wrong types normalized with a load note each; saved once, "
          "nothing to note  OK")


def test_cluster_flags():
    # as in 2.8.1: anything but true/false is the default (true), so no text cuts off older
    # nodes or discovery; a previous key that is no text is none
    for raw in (None, "false", "maybe", 0):
        c = ClusterSettings.from_dict({"legacy_auth": raw, "discovery": raw})
        assert (c.legacy_auth, c.discovery) == (True, True), (raw, c)
    assert ClusterSettings.from_dict({"legacy_auth": False}).legacy_auth is False
    assert ClusterSettings.from_dict({"previous_key": 5}).previous_key == ""
    assert ClusterSettings.from_dict({"key": 1234}).key == "1234"
    print("cluster legacy_auth / discovery / previous_key: a value of another type is the "
          "default, as in 2.8.1  OK")


async def test_poll_loop_survives():
    gw = gateway(modbus_poll={"interval_s": 0.05, "points": [dict(POINT)]})
    log = []
    runner = ModbusGatewayRunner(gw, logger=log.append)

    async def read_point(p):
        return True

    def publish(values):
        raise TypeError("can't publish this")

    runner._read_point = read_point
    runner._publish_values = publish
    runner._link.down = False
    task = asyncio.create_task(runner._poll_loop())
    await asyncio.sleep(0.3)
    assert not task.done(), "a publishing error ended the poll loop"
    failed = [m for m in log if "publishing failed" in m]
    assert len(failed) == 1, log  # once, not every interval
    runner._stop.set()
    await asyncio.wait_for(task, 2)

    # a poll task that died anyway: stop() still goes through (and says why)
    log.clear()
    runner = ModbusGatewayRunner(gw, logger=log.append)

    async def dies():
        raise ValueError("boom")

    runner._poll_task = asyncio.create_task(dies())
    await asyncio.sleep(0)
    closed = []
    runner._mqtt = types.SimpleNamespace(close=lambda: closed.append(True))
    await runner.stop()
    assert closed == [True], "the MQTT side was not closed"
    assert any("polling had stopped: ValueError('boom')" in m for m in log), log
    assert log[-1] == "modbus gateway stopped", log
    print("Modbus poll: a publishing error is logged once and polling goes on; stop() "
          "survives a dead poll task  OK")


def test_api_server_error_is_json():
    from starlette.requests import Request

    from app.web.server import server_error

    def request(path):
        return Request({"type": "http", "method": "GET", "path": path, "headers": [],
                        "query_string": b"", "server": ("127.0.0.1", 80), "scheme": "http"})

    api = asyncio.run(server_error(request("/api/v1/mappings"), RuntimeError("x")))
    assert api.status_code == 500 and api.media_type == "application/json"
    assert json.loads(api.body) == {"error": "internal", "detail": "internal server error"}
    ui = asyncio.run(server_error(request("/settings"), RuntimeError("x")))
    assert ui.status_code == 500 and ui.body == b"Internal Server Error"
    print("unexpected error: JSON 500 under /api/v1, plain text elsewhere  OK")


def test_other_settings_of_this_release():
    """The settings other parts of this release added read through the same reader: named
    API tokens, a one-line trusted-proxy list, a two-factor secret that is no text (it
    stays on), a port-locking switch as true/false, an unknown console encoding as UTF-8;
    the switches that are on only for JSON true (a listener for both families, MQTT writes
    to the device, HSTS through a proxy, the masking of notifications ...) stay off for
    text, with a note, and an edit refuses text for them; a relative TLS path of a mapping
    or of the admin UI is a file of the data directory."""
    import tempfile

    from app.config import AdminUI, console_encoding, set_data_dir, tls_files_missing
    tokens = [{"id": "0123456789abcdef", "name": "grafana", "sha256": "a" * 64,
               "role": "viewer", "created": 1.0, "expires": 0.0}]
    cfg = AppConfig.from_dict({
        "api_tokens": tokens,
        "admin_ui": {"trusted_proxies": "10.0.0.1, 10.0.0.0/24 ::1"},
        "users": [{"username": "a", "password_hash": "x", "role": "admin", "totp_secret": True},
                  {"username": "b", "password_hash": "x", "totp_secret": None}],
        "notifications": {"mask_personal_data": True, "email_separately": "true"},
        "mappings": [{"name": "m", "serial": {"port": "/dev/ser2net-test",
                                              "advanced": {"tiocexcl": "1"}},
                      "network": {"mode": "udp", "bind_ip": "::", "dual_stack": "true",
                                  "port": 4000},
                      "options": {"console_encoding": "klingon"}}]})
    assert [t.name for t in cfg.api_tokens] == ["grafana"], cfg.api_tokens
    assert cfg.admin_ui.trusted_proxies == ["10.0.0.1", "10.0.0.0/24", "::1"], cfg.admin_ui
    a, b = cfg.users
    assert a.totp_on and a.totp_secret == "TRUE" and not b.totp_on, (a, b)
    assert any("totp_secret is not a base32 secret" in n for n in cfg.load_notes), cfg.load_notes
    assert cfg.notifications.mask_personal_data is True
    assert cfg.notifications.email_separately is False
    m = cfg.mappings[0]
    assert m.network.dual_stack is False and m.serial.advanced.tiocexcl is True
    assert any("network.dual_stack \"true\" is not true or false: false is used" in n
               for n in cfg.load_notes), cfg.load_notes
    assert console_encoding(m.options) == "utf-8"
    for d, path in (({"network": {"mode": "udp", "port": 4000, "udp_broadcast": "yes"}},
                     "network.udp_broadcast"),
                    ({"mqtt": {"enabled": True, "host": "b", "tx_enabled": 1}}, "mqtt.tx_enabled"),
                    ({"options": {"poll_with_clients": "on"}}, "options.poll_with_clients")):
        refused(MappingConfig.from_dict({"name": "x", "serial": {"port": "/dev/ser2net-test"},
                                         **d}), path)
    assert AppConfig.from_dict({"admin_ui": {"proxy_hsts": "yes"}}).admin_ui.proxy_hsts is False
    data = tempfile.mkdtemp(prefix="s2n_types_")
    try:
        os.makedirs(os.path.join(data, "tls"))
        for name in ("cert.pem", "key.pem"):
            with open(os.path.join(data, "tls", name), "w") as fh:
                fh.write("x")
        set_data_dir(data)
        tls = MappingConfig.from_dict({"name": "t", "serial": {"port": "/dev/ser2net-test"},
                                       "network": {"mode": "server", "port": 4001, "tls": True,
                                                   "tls_cert": "tls/cert.pem",
                                                   "tls_key": "tls/key.pem"}})
        assert tls_files_missing(tls.network) == [], "a relative path: in the data directory"
        AdminUI(tls_cert="tls/cert.pem", tls_key="tls/key.pem").check_tls_files()
        tls.network.tls_key = "tls/gone.pem"
        assert tls_files_missing(tls.network) == ["TLS file not found: tls/gone.pem"]
    finally:
        set_data_dir(None)
        import shutil
        shutil.rmtree(data, ignore_errors=True)
    print("this release's other settings: named tokens, one-line trusted proxies, a two-factor "
          "secret that is no text stays on, true/false switches, relative TLS paths in the "
          "data directory  OK")


def main():
    test_text_values_read_as_meant()
    test_protective_switches_need_false()
    test_nulls()
    test_edits_refuse_what_cannot_be_read()
    test_poll_interval()
    test_unknown_keys()
    test_structure_still_fails()
    test_validate_is_type_safe()
    test_load_normalizes_and_notes()
    test_cluster_flags()
    asyncio.run(test_poll_loop_survives())
    test_api_server_error_is_json()
    test_other_settings_of_this_release()
    print("\nPASS: config value types")


if __name__ == "__main__":
    main()
