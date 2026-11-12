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

from app.config import MAX_TIMER_S, AppConfig, ConfigError, MappingConfig, modbus_response_timeout
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


def test_form_apply_retained_set():
    """'Re-apply a retained /set' is a checkbox (absent when unticked) shown only for
    plain-format Modbus points, so its hidden marker says whether the form had it: without
    the marker (Sparkplug format, an older cluster peer's form) the stored value is kept on
    edit and a new mapping gets the default, off."""
    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502"}
    existing = MappingConfig.from_dict({"name": "gw", "serial": {"port": "COM7"},
                                        "network": {"protocol": "modbus", "port": 502},
                                        "mqtt": {"apply_retained_set": True}})
    assert MappingConfig().mqtt.apply_retained_set is False, "off by default"
    assert MappingConfig.from_dict({"mqtt": {}}).mqtt.apply_retained_set is False

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.mqtt.apply_retained_set

    marker = {"mqtt_apply_retained_set_shown": "1"}
    assert parsed(dict(base, id=existing.id)) is True, "absent from the form: kept"
    assert parsed(dict(base, id=existing.id, **marker)) is False, "shown and unticked: off"
    assert parsed(dict(base, id=existing.id, mqtt_apply_retained_set="on", **marker)) is True
    assert parsed(base) is False and parsed(dict(base, **marker)) is False
    assert parsed(dict(base, mqtt_apply_retained_set="on", **marker)) is True
    print("form: apply_retained_set kept when the form leaves it out, set when shown  OK")


def test_form_rfc2217_restore_on_disconnect():
    """'Restore the serial settings when the RFC 2217 client leaves' is a checkbox (absent
    when unticked) shown for RFC 2217 mappings only, so its hidden marker says whether the
    form had it: without the marker (another protocol, an older cluster peer's form) the
    stored value is kept on edit and a new mapping gets the default, off."""
    base = {"name": "vc", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "rfc2217",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}
    existing = MappingConfig.from_dict({"name": "vc", "serial": {"port": "COM7"},
                                        "network": {"protocol": "rfc2217", "port": 4001},
                                        "options": {"rfc2217_restore_on_disconnect": True}})
    assert MappingConfig().options.rfc2217_restore_on_disconnect is False, "off by default"
    assert MappingConfig.from_dict({"options": {}}).options.rfc2217_restore_on_disconnect is False

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.options.rfc2217_restore_on_disconnect

    marker = {"opt_rfc2217_restore_on_disconnect_shown": "1"}
    on = {"opt_rfc2217_restore_on_disconnect": "on"}
    assert parsed(dict(base, id=existing.id)) is True, "absent from the form: kept"
    assert parsed(dict(base, id=existing.id, **marker)) is False, "shown and unticked: off"
    assert parsed(dict(base, id=existing.id, **marker, **on)) is True
    assert parsed(base) is False and parsed(dict(base, **marker)) is False
    assert parsed(dict(base, **marker, **on)) is True
    print("form: rfc2217_restore_on_disconnect kept when the form leaves it out, set when "
          "shown  OK")


def test_form_udp_reply_fields():
    """The reply address and peer timeout are shown (and posted) for UDP only. Without
    them (another mode, an older cluster peer's form) the stored values are kept on edit
    and a new mapping gets the defaults: the last sender, never forgotten."""
    base = {"name": "u", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "udp", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001"}
    existing = MappingConfig.from_dict({
        "name": "u", "serial": {"port": "COM7"},
        "network": {"mode": "udp", "udp_peer_host": "10.0.0.9", "udp_peer_port": 5000,
                    "udp_peer_timeout_s": 30}})
    fresh = MappingConfig()
    assert (fresh.network.udp_peer_host, fresh.network.udp_peer_port,
            fresh.network.udp_peer_timeout_s) == ("", 0, 0)

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        n = m.network
        return n.udp_peer_host, n.udp_peer_port, n.udp_peer_timeout_s

    shown = {"network_udp_peer_host": " 192.168.1.20 ", "network_udp_peer_port": "4002",
             "network_udp_peer_timeout_s": "60"}
    assert parsed(dict(base, **shown)) == ("192.168.1.20", 4002, 60)
    cleared = {"network_udp_peer_host": "", "network_udp_peer_port": "",
               "network_udp_peer_timeout_s": ""}
    assert parsed(dict(base, id=existing.id, **cleared)) == ("", 0, 0), "blank = off"
    assert parsed(dict(base, id=existing.id)) == ("10.0.0.9", 5000, 30), "absent: kept"
    assert parsed(dict(base, id=existing.id, network_mode="server")) == ("10.0.0.9", 5000, 30)
    assert parsed(base) == ("", 0, 0)
    print("form: UDP reply address / peer timeout parsed when shown, kept when absent  OK")


def test_form_mqtt_unit_status():
    """The plain-MQTT unit status option is a checkbox shown for plain-format Modbus points:
    parsed when its block is posted, kept when the form leaves the block out (Sparkplug B
    selected, an older cluster peer's form); off for new mappings and older configs."""
    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502", "mqtt_enabled": "on",
            "mqtt_host": "b", "mqtt_base_topic": "plant/a", "mqtt_payload_format": "plain"}
    existing = MappingConfig.from_dict({"name": "gw", "serial": {"port": "COM7"},
                                        "network": {"protocol": "modbus", "port": 502},
                                        "mqtt": {"unit_status": True}})
    assert MappingConfig().mqtt.unit_status is False
    assert MappingConfig.from_dict({"mqtt": {}}).mqtt.unit_status is False, "older configs: off"

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.mqtt.unit_status

    shown = dict(base, mqtt_unit_status_shown="1")
    assert parsed(dict(shown, id=existing.id)) is False, "unticked"
    assert parsed(dict(shown, id=existing.id, mqtt_unit_status="on")) is True
    assert parsed(dict(base, id=existing.id)) is True, "block not posted: kept"
    assert parsed(shown) is False and parsed(dict(shown, mqtt_unit_status="on")) is True
    assert "unit_status" in MappingConfig.from_dict({"mqtt": {"unit_status": True}}).to_dict()["mqtt"]
    print("form: the MQTT unit status checkbox is parsed when shown, kept when not posted  OK")


def test_form_mqtt_retain_and_value_format():
    """Plain MQTT: the retain checkbox (shown for plain-format mappings, serial lines too)
    and the Modbus value format select (plain-format points) are parsed when posted and
    kept when the form leaves them out (Sparkplug B selected, an older peer's form); off /
    text for new mappings and older configs. An edit refuses a value format this version
    doesn't know; a stored one still loads and saves."""
    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502", "mqtt_enabled": "on",
            "mqtt_host": "b", "mqtt_base_topic": "plant/a", "mqtt_payload_format": "plain"}
    existing = MappingConfig.from_dict({"name": "gw", "serial": {"port": "COM7"},
                                        "network": {"protocol": "modbus", "port": 502},
                                        "mqtt": {"retain": True, "value_format": "json"}})
    for m in (MappingConfig(), MappingConfig.from_dict({"mqtt": {}})):
        assert m.mqtt.retain is False and m.mqtt.value_format == "text"

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.mqtt.retain, m.mqtt.value_format

    shown = dict(base, mqtt_retain_shown="1", mqtt_value_format="text")
    assert parsed(dict(shown, id=existing.id)) == (False, "text"), "unticked, text"
    assert parsed(dict(shown, id=existing.id, mqtt_retain="on",
                       mqtt_value_format="json")) == (True, "json")
    assert parsed(dict(base, id=existing.id)) == (True, "json"), "not posted: kept"
    assert parsed(dict(base)) == (False, "text"), "a new mapping without them: defaults"
    for fmt in ("text", "json"):
        MappingConfig.from_dict(dict(existing.to_dict(), mqtt=dict(
            existing.to_dict()["mqtt"], value_format=fmt))).validate_input()
    later = MappingConfig.from_dict(dict(existing.to_dict(), mqtt=dict(
        existing.to_dict()["mqtt"], value_format="csv")))
    later.validate()
    AppConfig.from_dict({"mappings": [later.to_dict()]}).validate()
    try:
        later.validate_input()
        raise AssertionError("an edit took value format 'csv'")
    except ConfigError as e:
        assert "value format" in str(e), e
    print("form: MQTT retain and value format parsed when shown, kept when not posted; an "
          "unknown value format refused at edit time only  OK")


def test_modbus_point_options():
    """A point's min / max / allowed / poll: off by default (a v2.8 point loads as before);
    an edit refuses ones that can't be used, a stored one still loads and saves. The form's
    points JSON shows them when set, and a form without the options marker (an older
    peer's, whose JSON can't carry them) keeps each point's stored ones, by name."""
    import json
    from app.web.routes.forms import _modbus_points_json
    old = {"name": "sp", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "writable": True}
    p = MappingConfig.from_dict({"modbus_poll": {"points": [old]}}).modbus_poll.points[0]
    assert (p.min, p.max, p.allowed, p.poll) == (None, None, None, True)

    def gateway(*points):
        return MappingConfig.from_dict({"name": "gw", "serial": {"port": "COM7"},
                                        "network": {"protocol": "modbus", "port": 502},
                                        "mqtt": {"enabled": True, "host": "b",
                                                 "base_topic": "plant/a"},
                                        "modbus_poll": {"points": list(points)}})
    coil = {"name": "pump", "unit": 1, "fn": 1, "address": 0, "dtype": "bool", "writable": True}
    good = [dict(old, min=0, max=1500), dict(old, min=-5.5), dict(old, allowed=[0, 1, 2.5]),
            dict(coil, allowed=[True]), dict(old, poll=False), dict(coil, poll=True)]
    for point in good:
        m = gateway(point)
        m.validate()
        m.validate_input()
    bad = [(dict(old, writable=False, min=0), "need \"writable\": true"),
           (dict(coil, min=0), "min and max don't apply"),
           (dict(old, min="0"), "min must be a number"),
           (dict(old, max=float("inf")), "max must be a number"),
           (dict(old, min=True), "min must be a number"),
           (dict(old, min=10, max=5), "min is greater than max"),
           (dict(old, allowed=[]), "allowed must be a list"),
           (dict(old, allowed=5), "allowed must be a list"),
           (dict(old, allowed=[True]), "allowed must be a list"),
           (dict(coil, allowed=[1]), "allowed must be a list"),
           (dict(old, poll="no"), "poll must be true or false"),
           (dict(old, writable=False, poll=False), "is write-only")]
    for point, message in bad:
        m = gateway(point)
        m.validate()                              # a stored one still loads ...
        AppConfig.from_dict({"mappings": [m.to_dict()]}).validate()  # ... and saves
        try:
            m.validate_input()
            raise AssertionError(f"an edit took {point}")
        except ConfigError as e:
            assert message in str(e), (point, str(e))

    shown = json.loads(_modbus_points_json(gateway(dict(old, min=0, max=1500, allowed=[5]),
                                                   dict(coil, poll=False), old)))
    assert shown[0]["min"] == 0 and shown[0]["max"] == 1500 and shown[0]["allowed"] == [5]
    assert shown[1]["poll"] is False and "min" not in shown[1]
    assert not {"min", "max", "allowed", "poll"} & set(shown[2]), "defaults are not shown"

    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502", "mqtt_enabled": "on",
            "mqtt_host": "b", "mqtt_base_topic": "plant/a"}
    existing = gateway(dict(old, min=0, max=1500), dict(coil, poll=False))

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing, form)
        return [(p.name, p.address, p.min, p.max, p.poll) for p in m.modbus_poll.points]
    older = json.dumps([dict(old, address=11), coil])   # an older form: no options in it
    assert parsed(dict(base, id=existing.id, modbus_points=older)) == [
        ("sp", 11, 0, 1500, True), ("pump", 0, None, None, False)], "kept by name"
    newer = json.dumps([dict(old, max=900), coil])
    assert parsed(dict(base, id=existing.id, modbus_points=newer,
                       modbus_point_options_shown="1")) == [
        ("sp", 10, None, 900, True), ("pump", 0, None, None, True)], "this form's own"
    print("point options: off by default; an edit refuses unusable ones (stored ones load); "
          "the form shows them when set and keeps them for an older peer's form  OK")


def test_form_modbus_response_timeout():
    """The Modbus response timeout is in the form for Modbus gateways only (before 2.9 it was
    in config.json only): parsed when posted (blank = the default 1 s), kept when a form
    leaves it out (another protocol, an older cluster peer's form). An edit refuses a value
    outside 0.01-60 s; a stored one is read leniently and never stops a config loading."""
    base = {"name": "gw", "kind": "net", "enabled": "on", "serial_port": "COM7",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "modbus",
            "network_bind_ip": "0.0.0.0", "network_port": "502"}
    existing = MappingConfig.from_dict({"name": "gw", "serial": {"port": "COM7"},
                                        "network": {"protocol": "modbus", "port": 502},
                                        "options": {"modbus_response_timeout_s": 0.8}})
    assert MappingConfig().options.modbus_response_timeout_s == 1.0

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing if form.get("id") else None, form)
        return m.options.modbus_response_timeout_s

    assert parsed(dict(base, id=existing.id, opt_modbus_response_timeout_s="2.5")) == 2.5
    assert parsed(dict(base, id=existing.id, opt_modbus_response_timeout_s=" ")) == 1.0
    assert parsed(dict(base, id=existing.id)) == 0.8, "absent from the form: kept"
    assert parsed(dict(base, id=existing.id, network_protocol="raw")) == 0.8
    assert parsed(base) == 1.0 and parsed(dict(base, opt_modbus_response_timeout_s="3")) == 3.0
    try:
        parsed(dict(base, opt_modbus_response_timeout_s="soon"))
        raise AssertionError("a timeout that is not a number must be refused")
    except ValueError:
        pass

    def gateway(timeout, protocol="modbus"):
        return MappingConfig.from_dict({"name": "gw", "serial": {"port": "COM7"},
                                        "network": {"protocol": protocol, "port": 502},
                                        "options": {"modbus_response_timeout_s": timeout}})
    for ok in (0.01, 0.4, 1, 60):
        gateway(ok).validate_input()
    for bad in (0.005, 0, -1, 61, "1.5", True, None):
        try:
            gateway(bad).validate_input()
            raise AssertionError(f"an edit must refuse a response timeout of {bad!r}")
        except ConfigError as e:
            assert "between 0.01 and 60 seconds" in str(e), e
    gateway(-1, protocol="raw").validate_input()  # not a gateway: not its setting
    # stored values from before 2.9 (config.json only, unchecked) still load and save, and
    # read as their number or the default; nothing past the timers' upper bound
    for stored, reads in ((-1, 1.0), (0, 1.0), ("1.5", 1.5), ("soon", 1.0), (True, 1.0),
                          (None, 1.0), (float("nan"), 1.0), (0.8, 0.8), (1e12, MAX_TIMER_S)):
        m = gateway(stored)
        m.validate()
        AppConfig.from_dict({"mappings": [m.to_dict()]}).validate()
        assert modbus_response_timeout(m.options) == reads, (stored, reads)
    from app.engine.modbus_gateway import ModbusGatewayRunner
    assert ModbusGatewayRunner(gateway("1.5"))._resp_timeout == 1.5
    print("form: Modbus response timeout parsed when shown, kept when absent; edits refuse "
          "values outside 0.01-60 s, stored ones read leniently  OK")


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
    test_form_apply_retained_set()
    test_form_rfc2217_restore_on_disconnect()
    test_form_udp_reply_fields()
    test_form_modbus_response_timeout()
    test_form_mqtt_unit_status()
    test_form_mqtt_retain_and_value_format()
    test_modbus_point_options()
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
