"""Names that stay as they were when ser2net became ser2netweb (3.0), because other programs,
nodes of 2.9 and older, and stored data rely on them. A rename done by search and replace
must not reach them, so this file writes the old name as OLD ("ser" "2net"), which such a
replace leaves alone, and fails when one of them changes:

  * the cluster protocol: the labels the peer requests, their replies and the request key
    are signed with, the header names, the auth scheme, the beacon's envelope and the UDP
    port, so 2.9.x and 3.x nodes keep talking to each other;
  * the Prometheus metric names (ser2net_*), which dashboards and alerts query;
  * defaults a configuration may rely on: the Sparkplug B group ID, the RFC 2217
    signature, the e-mail sender address;
  * names stored or sent elsewhere: the cookies (a new name signs everybody out), the
    browser's localStorage keys, the data directory's lock file, the ./lib stamp, the
    labels of the HMACs behind mapping revisions and signed payloads, the logger name.

Run: python3 tests/test_kept_names.py
"""
import hashlib
import hmac
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

import bootstrap

from ser2netweb import datalock, i18n
from ser2netweb.config import (
    OWN_NAMES, AppConfig, ClusterSettings, MappingConfig, MqttSettings, NotificationSettings,
    mapping_revision, sparkplug_identity,
)
from ser2netweb.engine import cluster
from ser2netweb.engine.protocols import rfc2217
from ser2netweb.notify import sender
from ser2netweb.web import auth
from ser2netweb.web.routes import login, system, totp

OLD = "ser" "2net"   # (written in two parts: a search and replace of the name misses it)
KEY = "a-cluster-key"


def _hmac(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def test_cluster_protocol():
    assert (cluster.AUTH_HEADER, cluster.KEY_HEADER) == ("X-Cluster-Auth", "X-Cluster-Key")
    assert (cluster.AUTH_SCHEME, cluster.PROTOCOL) == ("v2", 2)
    assert ClusterSettings().discovery_port == 41750
    # the request key and the two signatures, with the labels 2.9 signs with
    raw = cluster.key_bytes(KEY)
    auth_key = hmac.new(raw, f"{OLD} cluster auth v2".encode(), hashlib.sha256).digest()
    assert cluster.auth_key(KEY) == auth_key
    body_hash = hashlib.sha256(b"a=1").hexdigest()
    want = _hmac(auth_key, "\n".join([f"{OLD}-cluster-request", "POST", "/api/cluster/x",
                                      body_hash, "aaaa", "bbbb", "1.000", "00" * 16]).encode())
    assert cluster.request_mac(KEY, "post", "/api/cluster/x", b"a=1", "aaaa", "bbbb", "1.000",
                               "00" * 16) == want
    want = _hmac(auth_key, "\n".join([f"{OLD}-cluster-reply", "00" * 16, "200", body_hash,
                                      "bbbb", "2.000"]).encode())
    assert cluster.reply_mac(KEY, "00" * 16, 200, b"a=1", "bbbb", "2.000") == want
    assert cluster.format_auth({"from": "aaaa"}) == "v2 from=aaaa"
    # a beacon as 2.9 sends it: {"d": <payload>, "s": <HMAC of it with the key>}
    payload = json.dumps({"v": 2, "id": "aaaa1111", "name": "old-node", "ip": "10.0.0.5",
                          "port": 8080, "scheme": "http", "t": 1.0, "b": "0" * 16,
                          "ips": ["10.0.0.5"]}, separators=(",", ":"), sort_keys=True)
    packet = json.dumps({"d": payload, "s": _hmac(raw, payload.encode())}).encode()
    cfg = AppConfig()
    cfg.instance_id = "bbbb2222"
    cfg.cluster = ClusterSettings(enabled=True, key=KEY)
    node = cluster.ClusterDiscovery(cfg, lambda m: None)
    node.handle_datagram(packet, ("10.0.0.5", 41750))
    assert [p["id"] for p in node.peers()] == ["aaaa1111"], node.peers()
    print("cluster: signing labels, headers, auth scheme, beacon envelope, UDP 41750  OK")


# every series /metrics answers, as 2.9 named them (the last line: added in 3.1):
# OLD + "_" + one of these
METRICS = [f"{OLD}_{name}" for name in """
up build_info start_time_seconds event_loop_lag_seconds event_loop_stalls_total
mappings_total open_fds fd_limit data_free_bytes data_size_bytes traces_bytes logs_bytes
admin_connections tls_handshakes_in_progress tls_handshakes_waiting connections_refused_total
mapping_enabled mapping_state mapping_running mapping_last_rx_timestamp_seconds
mapping_no_data mapping_clients mapping_bytes_in_total mapping_bytes_out_total
mapping_bytes_dropped_total mapping_reconnects_total mapping_open_failures_total
mapping_device_lost_total mapping_dropped_clients_total mapping_link_up
mapping_connect_failures_total modbus_unit_up modbus_unit_poll_errors_total mqtt_connected
mqtt_messages_dropped_total
tls_certificate_expiry_seconds clock_steps_total
""".split()]


def test_metric_names():
    assert sorted(system.METRIC_INFO) == sorted(METRICS), \
        sorted(set(system.METRIC_INFO) ^ set(METRICS))
    with open(os.path.join(ROOT, "ser2netweb", "web", "routes", "system.py"),
              encoding="utf-8") as fh:
        written = set(re.findall(r"\b(" + OLD + r"\w*?)(?:\{|\s|\")", fh.read()))
    series = {n for n in written if n.startswith(f"{OLD}_")}
    assert len(series) >= 30, series
    assert series <= set(METRICS), series - set(METRICS)
    print(f"/metrics: the {len(METRICS)} {OLD}_* names of 2.9  OK")


def test_defaults_a_config_relies_on():
    assert MqttSettings().sparkplug_group_id == OLD
    blank = MappingConfig(name="m1")
    blank.mqtt = MqttSettings(sparkplug_group_id=" ")
    assert sparkplug_identity(blank)[0] == OLD
    assert rfc2217.DEFAULT_SIGNATURE == OLD
    assert MappingConfig().options.rfc2217_signature == ""
    assert sender(NotificationSettings()).startswith(f"{OLD}@"), sender(NotificationSettings())
    print("defaults: Sparkplug group ID, RFC 2217 signature, the e-mail sender address  OK")


def test_stored_and_sent_names():
    assert (auth.SESSION_COOKIE, auth.CSRF_COOKIE) == (f"{OLD}_session", f"{OLD}_csrf")
    assert (i18n.COOKIE, login.OIDC_COOKIE, totp.PENDING_COOKIE) == \
        (f"{OLD}_lang", f"{OLD}_oidc", f"{OLD}_2fa")
    assert datalock.LOCK_NAME == f"{OLD}.lock" and f"{OLD}.lock" in OWN_NAMES
    assert bootstrap.STAMP == f".{OLD}-deps.json"
    secret = "s" * 32
    want = _hmac(secret.encode(), f"{OLD} signed payload".encode())
    assert auth._payload_secret(secret) == want
    m = MappingConfig(name="m1")
    key = hmac.new(secret.encode(), f"{OLD} mapping revision".encode(), hashlib.sha256).digest()
    content = m.to_dict()
    del content["options"]["trace_until"]  # (3.1: a timed capture's end is no setting)
    body = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    assert mapping_revision(secret, m) == _hmac(key, body.encode())[:24]
    static = os.path.join(ROOT, "ser2netweb", "web", "static")
    with open(os.path.join(static, "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    with open(os.path.join(static, "theme.js"), encoding="utf-8") as fh:
        theme = fh.read()
    assert f'"{OLD}-theme"' in js and f'"{OLD}-theme"' in theme and f'"{OLD}-dashboard"' in js
    with open(os.path.join(ROOT, "ser2netweb", "state.py"), encoding="utf-8") as fh:
        assert f'logging.getLogger("{OLD}")' in fh.read(), "the logger's name"
    print("cookies, localStorage keys, the lock file, the ./lib stamp, HMAC labels, the "
          "logger  OK")


def main():
    test_cluster_protocol()
    test_metric_names()
    test_defaults_a_config_relies_on()
    test_stored_and_sent_names()
    print(f"\nPASS: the names that stay {OLD}")


if __name__ == "__main__":
    main()
