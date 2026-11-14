"""Kitchen-sink mappings for the mapping-form round-trip tests: between them, every
setting has a value other than its default (tests/test_mapping_form_roundtrip.py checks
that, and fills in a setting added later). Plain data, no app imports: also used by
tests/make_form_fixtures.py under the code of older releases, and by test_ui_layout.

Each mapping is valid (validate() and an edit's validate_input() pass), as a stored one
is; settings a mode or kind doesn't use have values too, as after switching it.
"""
from __future__ import annotations

import atexit
import copy
import os
import shutil
import subprocess
import tempfile


def _tls_file() -> str:
    """A PEM file with a self-signed certificate and its key (and so a CA file too): an edit
    loads a TLS mapping's files (engine/tls.py). Made once per process, with cryptography,
    else the openssl command, in a folder removed at exit; with neither, an existing file
    (an edit that loads it then refuses it)."""
    folder = tempfile.mkdtemp(prefix="s2n_form_cases_")
    atexit.register(shutil.rmtree, folder, True)
    path = os.path.join(folder, "kitchen-sink.pem")
    try:
        import datetime

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ser2net-form-cases")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        with open(path, "wb") as fh:
            fh.write(cert.public_bytes(serialization.Encoding.PEM))
            fh.write(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        return path
    except ImportError:
        pass
    try:
        keyfile, certfile = path + ".key", path + ".crt"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:P-256", "-nodes", "-days", "3650",
                        "-subj", "/CN=ser2net-form-cases", "-keyout", keyfile, "-out", certfile],
                       check=True, capture_output=True, timeout=60)
        with open(path, "wb") as out:
            for part in (certfile, keyfile):
                with open(part, "rb") as fh:
                    out.write(fh.read())
        return path
    except (OSError, subprocess.SubprocessError):
        return os.path.abspath(__file__)


# a certificate with its key, in one file: tls_cert, tls_key and the CA files name it
TLS_FILE = _tls_file()

# the serial ports and bind addresses the form offers (what state.ports / netinfo give)
PORTS = [
    {"device": "/dev/ttyUSB7", "by_id": "/dev/serial/by-id/usb-FTDI_FT232R_A1-if00-port0",
     "vid": "0403", "pid": "6001", "serial_number": "A1", "description": "FT232R USB UART",
     "hardware": None},
    {"device": "/dev/ttyS0", "by_id": None, "vid": None, "pid": None, "serial_number": None,
     "description": "ttyS0", "hardware": True},
]
IPS = [
    {"value": "0.0.0.0", "label": "All interfaces (0.0.0.0)", "kind": "all"},
    {"value": "*", "label": "All interfaces (IPv4 + IPv6)", "kind": "all"},   # (dual_stack)
    {"value": "127.0.0.1", "label": "Localhost (127.0.0.1)", "kind": "loopback"},
    {"value": "192.168.1.10", "label": "eth0 — 192.168.1.10", "kind": "lan"},
]

_SERIAL_A = {
    "port": "/dev/ttyUSB7", "match": {"vid": "0403", "pid": "6001", "serial_number": "A1"},
    "baudrate": 250000, "bytesize": 7, "parity": "E", "stopbits": 2, "flowcontrol": "xonxoff",
    "rts_on_open": "on", "dtr_on_open": "off", "exclusive": False,
    "advanced": {"rs485_enabled": True, "rs485_delay_before_tx_ms": 0.5,
                 "rs485_delay_after_tx_ms": 1.5, "rs485_rts_level_for_tx": False,
                 "hangup_when_done": True, "nobreak": True, "tiocexcl": True,
                 "respect_uucp_locks": True}}
_SERIAL_B = {
    "port": "/dev/ttyACM5", "match": {"vid": "1a86", "pid": "7523"}, "baudrate": 19200,
    "bytesize": 6, "parity": "O", "stopbits": 1.5, "flowcontrol": "dsrdtr",
    "rts_on_open": "off", "dtr_on_open": "on", "exclusive": False,
    "advanced": {"rs485_enabled": True, "rs485_delay_before_tx_ms": 0.25,
                 "rs485_delay_after_tx_ms": 0.75, "rs485_rts_level_for_tx": False,
                 "hangup_when_done": True, "nobreak": True, "tiocexcl": True,
                 "respect_uucp_locks": True}}
_NETWORK = {
    "bind_ip": "10.0.0.5", "port": 4555, "remote_host": "plc.example", "remote_port": 2001,
    "udp_peer_host": "10.0.0.8", "udp_peer_port": 5001, "udp_peer_timeout_s": 30,
    "max_connections": 3, "kick_old_user": True,
    "allowed_client_ips": ["10.0.0.0/24", "192.168.1.7"], "priority_client_ips": ["10.0.0.7"],
    "read_only": True, "client_queue_max": 8192, "tcp_keepalive": False,
    "keepalive_idle_s": 30, "keepalive_interval_s": 5, "keepalive_count": 4,
    "connect_timeout_s": 2.5, "tls": True, "tls_cert": TLS_FILE, "tls_key": TLS_FILE,
    "tls_key_password": "kitchen-sink-key-pw", "tls_ca": TLS_FILE,
    "tls_fingerprint": "AB:" * 31 + "AB", "tls_client_ca": TLS_FILE,
    "disconnect_without_device": True}
_OPTIONS = {
    "banner": "Hello \\N on \\p", "openstr": "INIT\r\n", "closestr": "BYE\r",
    "closeon": "logout\r\n", "idle_timeout_s": 300, "max_connection_time_s": 86400,
    "trace_both": "plc1.trace", "trace_hexdump": True, "trace_timestamp": False,
    "trace_until": 4102444800.0,
    "trace_max_mb": 2.5, "trace_keep": 7, "rfc2217_poll_modem_interval_s": 2.0,
    "rfc2217_net_timeout_s": 9.0, "rfc2217_restore_on_disconnect": True,
    "modbus_response_timeout_s": 2.5, "no_data_alarm_s": 300, "no_data_reply_only": True,
    "console_encoding": "iso-8859-9", "rfc2217_signature": "ser2net kitchen sink",
    "poll_string": "P\\r\\n", "poll_interval_s": 0.5, "poll_with_clients": True,
    "modbus_unit_map": {"255": 1, "0": 2}}
_MQTT = {
    "enabled": True, "host": "broker.example", "port": 8883, "base_topic": "ser2net/plc1",
    "qos": 2, "tls": True, "username": "plc", "password": "p@ss w'rd\"<&>",
    "client_id": "s2n-plc1", "payload_format": "plain", "sparkplug_group_id": "line3",
    "sparkplug_node_id": "plc-1", "apply_retained_set": True, "line_end": "cr",
    "tx_enabled": True, "sparkplug_primary_host_id": "scada-1", "unit_status": True,
    "ca_file": TLS_FILE, "client_cert": TLS_FILE, "client_key": TLS_FILE,
    "client_key_password": "kitchen-sink-mqtt-key-pw", "tls_check_hostname": False,
    "retain": True, "value_format": "json"}
_POINTS = [
    {"name": "run", "unit": 2, "fn": 1, "address": 5, "dtype": "bool", "scale": 1.0,
     "writable": True},
    {"name": "setpoint", "unit": 1, "fn": 3, "address": 10, "dtype": "int16", "scale": 0.1,
     "writable": True, "min": -50, "max": 150.5, "word_order": "BADC"},
    {"name": "temp", "unit": 1, "fn": 4, "address": 0, "dtype": "float32", "scale": 0.5},
    {"name": "door", "unit": 3, "fn": 2, "address": 1, "dtype": "bool", "scale": 1.0},
    {"name": "label", "unit": 1, "fn": 3, "address": 20, "dtype": "string", "length": 4},
    {"name": "alarm", "unit": 1, "fn": 4, "address": 30, "dtype": "bool", "bit": 3},
    {"name": "command", "unit": 1, "fn": 3, "address": 40, "dtype": "uint32", "writable": True,
     "poll": False, "allowed": [0, 1, 2], "word_order": "CDAB"},
]


def _with(base: dict, **changes) -> dict:
    return {**base, **changes}


def mappings() -> list[dict]:
    """The kitchen-sink mappings (fresh copies), as config.json holds them."""
    return copy.deepcopy([
        {"id": "sinkserver01", "name": "TCP server", "enabled": True, "kind": "net",
         "serial": _SERIAL_A, "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="server", protocol="raw"),
         "options": _OPTIONS, "mqtt": _MQTT, "modbus_poll": {"interval_s": 2.5, "points": []}},
        {"id": "sinkclient01", "name": "TCP client", "enabled": False, "kind": "net",
         "serial": _with(_SERIAL_A, port="/dev/ttyS0", match={}, baudrate=115200,
                         advanced=_with(_SERIAL_A["advanced"], rs485_enabled=False)),
         "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="client", protocol="telnet", bind_ip="127.0.0.1",
                          connect_timeout_s=7.5, tcp_keepalive=True, client_queue_max=4096),
         "options": _with(_OPTIONS, closeon="\x00END\r"),
         "mqtt": _with(_MQTT, tls=False, qos=1, client_id=""),
         "modbus_poll": {"interval_s": 0.5, "points": []}},
        {"id": "sinkudp00001", "name": "UDP", "enabled": True, "kind": "net",
         "serial": _with(_SERIAL_A, port="/dev/ttyACM3", match={}, flowcontrol="none"),
         "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="udp", protocol="raw", bind_ip="192.168.1.10",
                          tls=False, udp_peer_host="192.168.1.20", udp_peer_port=4002,
                          udp_peer_timeout_s=60, allowed_client_ips=["192.168.1.0/24"],
                          udp_targets=["192.168.1.255:10110", "10.0.0.9:5000"],
                          udp_broadcast=True),
         "options": _with(_OPTIONS, closeon=""), "mqtt": _with(_MQTT, enabled=False),
         "modbus_poll": {"interval_s": 2.5, "points": []}},
        {"id": "sinkrfc22171", "name": "RFC 2217", "enabled": True, "kind": "net",
         "serial": _with(_SERIAL_A, match={}, port="COM7"), "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="server", protocol="rfc2217", max_connections=1,
                          bind_ip="0.0.0.0", port=4556, dual_stack=True),
         "options": _OPTIONS, "mqtt": _MQTT, "modbus_poll": {"interval_s": 2.5, "points": []}},
        {"id": "sinkmodbus01", "name": "Modbus Sparkplug", "enabled": True, "kind": "net",
         "serial": _with(_SERIAL_A, port="/dev/ttyUSB9", match={}), "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="server", protocol="modbus", port=5502,
                          read_only=False, tls=False),
         "options": _OPTIONS,
         "mqtt": _with(_MQTT, payload_format="sparkplug_b", sparkplug_group_id="plant",
                       sparkplug_node_id="gw-1"),
         "modbus_poll": {"interval_s": 0.5, "points": _POINTS, "block_reads": True,
                         "block_gap": 4}},
        {"id": "sinkmodbus02", "name": "Modbus plain", "enabled": False, "kind": "net",
         "serial": _with(_SERIAL_A, port="/dev/ttyUSB8", match={}), "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="server", protocol="modbus", port=5503, tls=False),
         "options": _OPTIONS, "mqtt": _with(_MQTT, apply_retained_set=False),
         "modbus_poll": {"interval_s": 1.5, "points": _POINTS[1:]}},
        {"id": "sinkbridge01", "name": "Serial bridge", "enabled": True, "kind": "serialbridge",
         "serial": _SERIAL_A, "serial_b": _SERIAL_B,
         "network": _with(_NETWORK, mode="client", protocol="telnet", tls=False),
         "options": _OPTIONS, "mqtt": _MQTT, "modbus_poll": {"interval_s": 2.5, "points": []}},
    ])


# What an edit through the form may reset because it doesn't apply to the mapping
# (its block is hidden, and keeping it would not be valid or useful):
#   kind -> paths (a prefix ends with ".")
IRRELEVANT = {
    # a serial↔serial bridge has no network side: the network fieldset is not posted;
    # the settings that are posted in every Serial ↔ Network form take their defaults
    "serialbridge": ("network.mode", "network.protocol", "network.read_only",
                     "mqtt.enabled", "mqtt.host", "mqtt.port", "mqtt.base_topic", "mqtt.qos",
                     "mqtt.tls", "mqtt.username", "mqtt.password"),
}


def relevant(mappings: list[dict]) -> list[dict]:
    """Stored mappings (dicts) without the settings an edit may reset (IRRELEVANT)."""
    out = []
    for m in copy.deepcopy(mappings):
        for path in IRRELEVANT.get(m.get("kind", "net"), ()):
            section, _dot, key = path.partition(".")
            m.get(section, {}).pop(key, None)
        out.append(m)
    return out
