"""Config validation + edit-preservation regression tests (M1, M3, M4).

M1: editing a mapping via the form must not silently drop fields the form does
    not expose (stable-id match, RS-485/advanced, openstr/closestr, rfc2217 knobs).
M3: a TCP listener and a UDP listener may share the same port number.
M4: two enabled mappings must not target the same serial device.

Pure stdlib + the app package (needs ./lib on path for app.web.routes).
Run: python3 tests/test_config_validation.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import AppConfig, ConfigError, MappingConfig
from app.web.routes import _preserve_unmanaged_fields


def _expect_error(cfg, needle):
    try:
        cfg.validate()
    except ConfigError as e:
        assert needle in str(e), f"wrong error: {e!r} (wanted {needle!r})"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}, but validate() passed")


# ---- M1: edit preservation ----

def test_preserve_unmanaged_fields_on_edit():
    existing = MappingConfig.from_dict({
        "name": "dev", "serial": {
            "port": "COM3", "match": {"vid": "0403", "pid": "6001"},
            "advanced": {"rs485_enabled": True, "rs485_delay_before_tx_ms": 5.0}},
        "options": {"openstr": "INIT\r\n", "closestr": "BYE\r", "trace_timestamp": False,
                    "rfc2217_net_timeout_s": 9.0}})
    # what the form would submit (no match/advanced/openstr/closestr/rfc2217 fields)
    edited = MappingConfig.from_dict({
        "id": existing.id, "name": "dev", "serial": {"port": "COM3", "baudrate": 19200},
        "options": {"banner": "hello", "closeon": "logout"}})

    _preserve_unmanaged_fields(edited, existing)

    assert edited.serial.match == {"vid": "0403", "pid": "6001"}, "stable-id match dropped on edit"
    assert edited.serial.advanced.rs485_enabled is True, "RS-485 setting dropped on edit"
    assert edited.serial.advanced.rs485_delay_before_tx_ms == 5.0
    assert edited.options.openstr == "INIT\r\n" and edited.options.closestr == "BYE\r"
    assert edited.options.trace_timestamp is False
    assert edited.options.rfc2217_net_timeout_s == 9.0
    # form-managed fields keep the submitted values
    assert edited.options.banner == "hello" and edited.options.closeon == "logout"
    assert edited.serial.baudrate == 19200
    print("edit preserves match / RS-485 / openstr / closestr / rfc2217 knobs  OK")


def test_preserve_is_noop_for_new_mapping():
    fresh = MappingConfig.from_dict({"name": "new", "serial": {"port": "COM1"}})
    _preserve_unmanaged_fields(fresh, None)
    assert fresh.options.openstr == "" and fresh.serial.match == {}
    print("new mapping: preserve is a no-op (defaults intact)  OK")


# ---- M3: TCP and UDP can share a port ----

def test_tcp_and_udp_share_port_ok():
    cfg = AppConfig.from_dict({"mappings": [
        {"name": "tcp", "serial": {"port": "COM1"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5000}},
        {"name": "udp", "serial": {"port": "COM2"},
         "network": {"mode": "udp", "bind_ip": "127.0.0.1", "port": 5000}},
    ]})
    cfg.validate()  # must NOT raise
    print("TCP server + UDP on the same port: accepted  OK")


def test_two_tcp_servers_same_port_rejected():
    cfg = AppConfig.from_dict({"mappings": [
        {"name": "a", "serial": {"port": "COM1"},
         "network": {"mode": "server", "bind_ip": "0.0.0.0", "port": 5000}},
        {"name": "b", "serial": {"port": "COM2"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5000}},
    ]})
    _expect_error(cfg, "TCP port 5000")
    print("two TCP servers on overlapping address+port: rejected  OK")


# ---- M4: serial-device collision ----

def test_two_enabled_mappings_same_serial_rejected():
    cfg = AppConfig.from_dict({"mappings": [
        {"name": "a", "enabled": True, "serial": {"port": "COM7"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5001}},
        {"name": "b", "enabled": True, "serial": {"port": "COM7"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5002}},
    ]})
    _expect_error(cfg, "serial port COM7")
    print("two enabled mappings on the same serial device: rejected  OK")


def test_disabled_mapping_does_not_collide():
    cfg = AppConfig.from_dict({"mappings": [
        {"name": "a", "enabled": True, "serial": {"port": "COM7"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5001}},
        {"name": "b", "enabled": False, "serial": {"port": "COM7"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5002}},
    ]})
    cfg.validate()  # disabled mapping doesn't hold the port -> OK
    print("disabled mapping on the same serial device: allowed  OK")


def test_dynamic_match_devices_not_collided():
    # two mappings resolved by VID/PID match share the literal placeholder port but
    # resolve dynamically -> not treated as a static collision
    cfg = AppConfig.from_dict({"mappings": [
        {"name": "a", "enabled": True,
         "serial": {"port": "auto", "match": {"vid": "0403", "serial_number": "A1"}},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5001}},
        {"name": "b", "enabled": True,
         "serial": {"port": "auto", "match": {"vid": "0403", "serial_number": "B2"}},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 5002}},
    ]})
    cfg.validate()  # match-based devices are skipped by the static check -> OK
    print("dynamic VID/PID match mappings: not flagged as a static collision  OK")


def test_modbus_requires_server_mode():
    bad = AppConfig.from_dict({"mappings": [
        {"name": "mb", "serial": {"port": "COM1"},
         "network": {"mode": "udp", "protocol": "modbus", "bind_ip": "127.0.0.1", "port": 502}},
    ]})
    _expect_error(bad, "Modbus gateway requires TCP server mode")
    ok = AppConfig.from_dict({"mappings": [
        {"name": "mb", "serial": {"port": "COM1"},
         "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1", "port": 502}},
    ]})
    ok.validate()  # server-mode modbus is valid
    print("modbus gateway: server-mode required, udp rejected  OK")


def test_legacy_password_migrates_to_admin_user():
    cfg = AppConfig.from_dict({"password_hash": "scrypt$aa$bb", "pwd_version": 3})
    assert cfg.password_set and len(cfg.users) == 1
    u = cfg.users[0]
    assert (u.username, u.role, u.password_hash, u.pwd_version) == ("admin", "admin", "scrypt$aa$bb", 3)
    # round-trips through to_dict without the legacy top-level fields
    d = cfg.to_dict()
    assert "password_hash" not in d and d["users"][0]["username"] == "admin"
    print("legacy single-password config migrates to one admin user  OK")


def test_mqtt_validation():
    base = {"name": "m", "serial": {"port": "COM1"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4001}}
    bad = AppConfig.from_dict({"mappings": [dict(base, mqtt={"enabled": True})]})  # no host/topic
    _expect_error(bad, "no broker host")
    ok = AppConfig.from_dict({"mappings": [dict(base, mqtt={"enabled": True, "host": "h",
                                                            "base_topic": "ser2net/x"})]})
    ok.validate()  # enabled + host + topic is valid
    print("mqtt: enabled requires host + base_topic  OK")


def test_modbus_poll_requires_gateway():
    pts = [{"name": "t", "unit": 1, "fn": 4, "address": 0, "dtype": "uint16"}]
    bad = AppConfig.from_dict({"mappings": [{"name": "m", "serial": {"port": "COM1"},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": 4001},
        "modbus_poll": {"points": pts}}]})
    _expect_error(bad, "Modbus register polling requires")
    ok = AppConfig.from_dict({"mappings": [{"name": "m", "serial": {"port": "COM1"},
        "network": {"mode": "server", "protocol": "modbus", "bind_ip": "127.0.0.1", "port": 502},
        "modbus_poll": {"interval_s": 2, "points": pts}}]})
    ok.validate()
    print("modbus poll points require a modbus-gateway mapping  OK")


def test_ldap_validation():
    users = [{"username": "a", "password_hash": "x", "role": "admin"}]
    _expect_error(AppConfig.from_dict({"users": users, "ldap": {"enabled": True}}),
                  "no server URI")
    _expect_error(AppConfig.from_dict({"users": users,
                  "ldap": {"enabled": True, "server_uri": "ldap://h"}}), "user DN template")
    ok = AppConfig.from_dict({"users": users, "ldap": {"enabled": True, "server_uri": "ldap://h",
                                                       "user_dn_template": "{username}@corp"}})
    ok.validate()
    print("ldap: enabled requires server URI + a bind mode  OK")


def main():
    test_ldap_validation()
    test_modbus_poll_requires_gateway()
    test_mqtt_validation()
    test_legacy_password_migrates_to_admin_user()
    test_modbus_requires_server_mode()
    test_preserve_unmanaged_fields_on_edit()
    test_preserve_is_noop_for_new_mapping()
    test_tcp_and_udp_share_port_ok()
    test_two_tcp_servers_same_port_rejected()
    test_two_enabled_mappings_same_serial_rejected()
    test_disabled_mapping_does_not_collide()
    test_dynamic_match_devices_not_collided()
    print("\nPASS: config validation + edit preservation (M1, M3, M4)")


if __name__ == "__main__":
    main()
