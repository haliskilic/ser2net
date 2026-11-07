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
from app.web.routes import _build_mapping_dict, _preserve_unmanaged_fields


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
            "advanced": {"rs485_enabled": True, "hangup_when_done": True, "nobreak": True}},
        "options": {"openstr": "INIT\r\n", "closestr": "BYE\r", "trace_timestamp": False,
                    "rfc2217_net_timeout_s": 9.0}})
    # what the form would submit: it DOES carry the RS-485 fields now (here: off),
    # but NOT match / hangup_when_done / nobreak / openstr / closestr / rfc2217 knobs.
    edited = MappingConfig.from_dict({
        "id": existing.id, "name": "dev", "serial": {"port": "COM3", "baudrate": 19200},
        "options": {"banner": "hello", "closeon": "logout"}})

    _preserve_unmanaged_fields(edited, existing)

    assert edited.serial.match == {"vid": "0403", "pid": "6001"}, "stable-id match dropped on edit"
    # RS-485 is form-managed now: the (empty) submission wins, not the old value
    assert edited.serial.advanced.rs485_enabled is False, "RS-485 should follow the form, not persist"
    # the two advanced flags the form omits are still preserved
    assert edited.serial.advanced.hangup_when_done is True, "hangup_when_done dropped on edit"
    assert edited.serial.advanced.nobreak is True, "nobreak dropped on edit"
    assert edited.options.openstr == "INIT\r\n" and edited.options.closestr == "BYE\r"
    assert edited.options.trace_timestamp is False
    assert edited.options.rfc2217_net_timeout_s == 9.0
    # form-managed fields keep the submitted values
    assert edited.options.banner == "hello" and edited.options.closeon == "logout"
    assert edited.serial.baudrate == 19200
    print("edit: form-managed RS-485 follows submission; match/hangup/nobreak/strings preserved  OK")


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


def test_config_round_trip():
    # a config exercising every subsystem must survive to_dict -> from_dict unchanged
    full = {
        "admin_ui": {"bind_ip": "0.0.0.0", "port": 8080},
        "api_token_hash": "abc", "api_token_role": "viewer",
        "users": [{"username": "admin", "password_hash": "h", "role": "admin", "source": "local"},
                  {"username": "alice", "password_hash": "", "role": "operator", "source": "oidc"}],
        "ldap": {"enabled": True, "server_uri": "ldaps://dc", "user_dn_template": "{username}@c",
                 "admin_group": "g-admins"},
        "oidc": {"enabled": True, "issuer": "https://idp", "client_id": "cid",
                 "client_secret": "sec", "groups_claim": "roles", "operator_group": "ops"},
        "mappings": [{"name": "gw", "serial": {"port": "COM1"},
                      "network": {"mode": "server", "protocol": "modbus", "bind_ip": "0.0.0.0",
                                  "port": 502},
                      "mqtt": {"enabled": True, "host": "broker", "base_topic": "p/a", "qos": 1},
                      "modbus_poll": {"interval_s": 2, "points": [
                          {"name": "t", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"}]}}],
    }
    cfg = AppConfig.from_dict(full)
    cfg.validate()
    once = cfg.to_dict()
    twice = AppConfig.from_dict(once).to_dict()
    assert once == twice, "config is not stable across to_dict/from_dict"
    # spot-check that key fields actually persisted
    assert once["api_token_role"] == "viewer"
    assert once["ldap"]["enabled"] and once["oidc"]["client_id"] == "cid"
    assert once["users"][1]["source"] == "oidc"
    m = once["mappings"][0]
    assert m["mqtt"]["host"] == "broker" and m["modbus_poll"]["points"][0]["dtype"] == "float32"
    print("config round-trips with every subsystem (auth/ldap/oidc/mqtt/modbus) intact  OK")


def test_invalid_admin_bind_ip_rejected():
    cfg = AppConfig.from_dict({"admin_ui": {"bind_ip": "not-an-ip", "port": 8080}})
    _expect_error(cfg, "bind IP")
    # valid wildcard/loopback/IP must pass
    for ip in ("0.0.0.0", "::", "127.0.0.1", "192.168.1.5"):
        AppConfig.from_dict({"admin_ui": {"bind_ip": ip, "port": 8080}}).validate()
    print("invalid admin UI bind IP rejected; valid ones accepted  OK")


def test_connection_lifecycle_defaults_and_upgrade():
    # a config written before v2.7 has none of the new keys: it must load with
    # dead-peer detection ON (the fix applies on upgrade) and the timers off
    m = MappingConfig.from_dict({"name": "old", "serial": {"port": "COM1"},
                                 "network": {"mode": "server", "port": 4001}})
    n, o = m.network, m.options
    assert n.tcp_keepalive is True
    assert (n.keepalive_idle_s, n.keepalive_interval_s, n.keepalive_count) == (60, 10, 3)
    assert n.connect_timeout_s == 10.0
    assert o.idle_timeout_s == 0 and o.max_connection_time_s == 0
    m.validate()
    d = m.to_dict()
    assert d["network"]["tcp_keepalive"] is True and d["options"]["max_connection_time_s"] == 0
    print("pre-v2.7 mapping: keepalive on (60s/10s/3), connect timeout 10s, timers off  OK")


def test_connection_lifecycle_validation():
    def mapping(network=None, options=None):
        return MappingConfig.from_dict({
            "name": "m", "serial": {"port": "COM1"},
            "network": {"mode": "server", "port": 4001, **(network or {})},
            "options": options or {}})

    bad = [
        ({"keepalive_idle_s": 0}, None, "Keepalive idle time"),
        ({"keepalive_idle_s": 40000}, None, "Keepalive idle time"),
        ({"keepalive_interval_s": "10"}, None, "Keepalive probe interval"),
        ({"keepalive_count": 0}, None, "Keepalive probe count"),
        ({"keepalive_count": 128}, None, "Keepalive probe count"),
        ({"keepalive_count": True}, None, "Keepalive probe count"),
        ({"connect_timeout_s": -1}, None, "Connect timeout"),
        ({"connect_timeout_s": 3601}, None, "Connect timeout"),
        (None, {"max_connection_time_s": -1}, "Max connection time"),
        (None, {"max_connection_time_s": "24h"}, "Max connection time"),
    ]
    for network, options, needle in bad:
        _expect_error(mapping(network, options), needle)
    # a negative idle timeout is rejected when set directly...
    neg = mapping()
    neg.options.idle_timeout_s = -5
    _expect_error(neg, "Idle timeout")
    # ...but values a pre-v2.7 config could hold (unvalidated then; <= 0 meant "off")
    # still load + save — a Docker/headless boot re-saves (= validates) the config
    for stored, loaded in ((-1, 0), ("300", 300), ("abc", 0), (10**12, 31536000)):
        legacy = mapping(options={"idle_timeout_s": stored})
        assert legacy.options.idle_timeout_s == loaded, (stored, legacy.options.idle_timeout_s)
        legacy.validate()
    # boundaries / valid values
    mapping({"keepalive_idle_s": 1, "keepalive_interval_s": 32767, "keepalive_count": 127,
             "connect_timeout_s": 0}, {"idle_timeout_s": 300, "max_connection_time_s": 86400}).validate()
    mapping({"tcp_keepalive": False, "connect_timeout_s": 2.5}).validate()
    print("keepalive/connect-timeout/idle/max-connection-time validation (types + ranges)  OK")


def test_form_conn_options_parse_and_preserve():
    """The mapping form owns the keepalive / connect-timeout / max-connection-time
    fields, but does not always POST them (hidden blocks are disabled; an older
    cluster peer's form lacks them). Absent => keep the stored values on edit."""
    base = {"name": "plc", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}

    # 1) visible block: parsed from the form (keepalive unchecked => off)
    form = dict(base, network_mode="server", network_keepalive_idle_s="30",
                network_keepalive_interval_s="5", network_keepalive_count="4",
                opt_max_connection_time_s="86400", opt_idle_timeout_s="300")
    m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
    assert (m.network.tcp_keepalive, m.network.keepalive_idle_s, m.network.keepalive_interval_s,
            m.network.keepalive_count) == (False, 30, 5, 4)
    assert m.options.max_connection_time_s == 86400 and m.options.idle_timeout_s == 300
    form["network_tcp_keepalive"] = "on"
    assert MappingConfig.from_dict(_build_mapping_dict(form, strict=True)).network.tcp_keepalive

    # 2) a new mapping whose form lacks the blocks gets the safe defaults
    fresh = MappingConfig.from_dict(_build_mapping_dict(dict(base, network_mode="udp"), strict=True))
    assert fresh.network.tcp_keepalive is True and fresh.network.keepalive_idle_s == 60
    assert fresh.network.connect_timeout_s == 10.0 and fresh.options.max_connection_time_s == 0

    # 3) editing with the blocks absent (UDP view / older peer) keeps the stored values
    existing = MappingConfig.from_dict({
        "name": "plc", "serial": {"port": "COM7"},
        "network": {"mode": "client", "remote_host": "10.0.0.9", "remote_port": 4001,
                    "tcp_keepalive": False, "keepalive_idle_s": 15, "keepalive_interval_s": 3,
                    "keepalive_count": 9, "connect_timeout_s": 2.5},
        "options": {"max_connection_time_s": 3600}})
    old_peer_form = dict(base, id=existing.id, network_mode="udp")
    edited = MappingConfig.from_dict(_build_mapping_dict(old_peer_form, strict=True))
    _preserve_unmanaged_fields(edited, existing, old_peer_form)
    n = edited.network
    assert (n.tcp_keepalive, n.keepalive_idle_s, n.keepalive_interval_s, n.keepalive_count,
            n.connect_timeout_s) == (False, 15, 3, 9, 2.5), n
    assert edited.options.max_connection_time_s == 3600

    # 4) ...but a posted field still wins (client view posts the connect timeout)
    client_form = dict(base, id=existing.id, network_mode="client",
                       network_remote_host="10.0.0.9", network_remote_port="4001",
                       network_connect_timeout_s="7.5")
    edited = MappingConfig.from_dict(_build_mapping_dict(client_form, strict=True))
    _preserve_unmanaged_fields(edited, existing, client_form)
    assert edited.network.connect_timeout_s == 7.5
    assert edited.network.keepalive_idle_s == 15  # block absent from this form -> kept
    print("form: keepalive/connect-timeout/max-time parsed when posted, kept when absent  OK")


def test_form_modbus_fields_not_offered():
    """The mapping form leaves the connect banner, close-on string and client buffer out
    for a Modbus gateway (it uses none of them). Absent => the stored values are kept on
    edit and a new mapping gets the defaults; a form that posts them (another protocol,
    an older cluster peer's form) still sets them."""
    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502"}
    existing = MappingConfig.from_dict({
        "name": "gw", "serial": {"port": "COM7"},
        "network": {"protocol": "modbus", "port": 502, "client_queue_max": 8192},
        "options": {"banner": "Welcome \\N", "closeon": "BYE"}})

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.options.banner, m.options.closeon, m.network.client_queue_max

    assert parsed(dict(base, id=existing.id)) == ("Welcome \\N", "BYE", 8192)
    assert parsed(base) == ("", "", 2048)
    posted = dict(base, id=existing.id, network_protocol="raw", opt_banner="",
                  opt_closeon="logout", network_client_queue_max="4096")
    assert parsed(posted) == ("", "logout", 4096)
    print("form: banner/close-on/client buffer kept when a Modbus form leaves them out  OK")


def test_web_access_settings():
    cfg = AppConfig.from_dict({"admin_ui": {"bind_ip": "127.0.0.1"}})
    assert cfg.admin_ui.require_login is True, "login must be required by default"
    assert cfg.admin_ui.anonymous_role == "viewer"
    cfg.validate()
    _expect_error(AppConfig.from_dict({"admin_ui": {"require_login": False,
                                                    "anonymous_role": "root"}}),
                  "Role for visitors without login")
    print("web UI access: login required by default; anonymous role must be a real role  OK")


def main():
    test_config_round_trip()
    test_web_access_settings()
    test_connection_lifecycle_defaults_and_upgrade()
    test_connection_lifecycle_validation()
    test_form_conn_options_parse_and_preserve()
    test_form_modbus_fields_not_offered()
    test_invalid_admin_bind_ip_rejected()
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
