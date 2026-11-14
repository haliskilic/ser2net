"""Fleet-view edits between nodes of different versions keep every setting.

tests/fixtures/forms-<version>.json were made once with the code of 2.7.1, 2.8.0, 2.8.1
and 3.0.1 (tests/make_form_fixtures.py) for the kitchen-sink mappings of
tests/_form_cases.py. With them, without running the old code:

  * an older node edits a mapping on this one: what its fleet view forwards for an
    unchanged save ("forms"), parsed by this version's save, changes nothing (2.7.1
    has no "writable" for Modbus points, no USB identity, no MQTT client id ...);
  * this node edits a mapping on an older one: the form it renders from that node's
    mapping-data ("mapping_data") and the fields it forwards post, for every field the
    older node's own form posts ("own_forms"), the same value — so the older node keeps
    what it would keep after an edit on itself (2.7.1's trace path included) — and none
    of the blocks that version doesn't read (forms.FORM_BLOCKS_SINCE);
  * the form this node shows for a mapping on an older node has only fields that node's
    own form has: a field added since is in a FORM_BLOCKS_SINCE block, so the older node
    isn't offered a setting it would ignore.

Pure in-process test (no sockets).

Run: python3 tests/test_mixed_version_edit.py
"""
from __future__ import annotations

import glob
import json
import os

import _form_cases as cases
from _formsubmit import Form, browser_post
from _server import ROOT  # noqa: F401  (sys.path setup)
from starlette.datastructures import FormData
from test_mapping_form_roundtrip import PEER, assert_kept, render

from ser2netweb import __version__
from ser2netweb.config import MappingConfig
from ser2netweb.web.routes import forms
from ser2netweb.web.routes.cluster import (
    _PEER_FORM_FIELDS,
    _PEER_FORM_PREFIXES,
    _PEER_PASSWORD_FIELDS,
    _peer_form,
    _peer_save_fields,
    _peer_version,
)

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "forms-*.json")))


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.loads(fh.read().replace("@TLS_FILE@", json.dumps(cases.TLS_FILE)[1:-1]))


# a form field of each block a node reads only since some version
BLOCK_FIELDS = {"usb_match": {"serial_usb_match_shown"}, "trace_rotation": {"opt_trace_max_mb"},
                "payload_format": {"mqtt_payload_format"},
                "retained_set": {"mqtt_apply_retained_set_shown"},
                "rfc2217_restore": {"opt_rfc2217_restore_on_disconnect_shown"},
                "udp_reply": {"network_udp_peer_timeout_s"},
                "port_lock": {"serial_port_lock_shown", "serial_tiocexcl",
                              "serial_respect_uucp_locks", "serial_b_port_lock_shown"},
                "dual_stack": {"network_dual_stack_shown"},
                "udp_targets": {"network_udp_targets_shown", "network_udp_targets",
                                "network_udp_broadcast"},
                "tls_checks": {"network_tls_ca", "network_tls_fingerprint",
                               "network_tls_client_ca"},
                "tls_key_password": {"network_tls_key_password"},
                "disconnect_without_device": {"network_disconnect_without_device_shown"},
                "rfc2217_signature": {"opt_rfc2217_signature"},
                "poll_string": {"opt_poll_string", "opt_poll_interval_s"},
                "no_data_alarm": {"opt_no_data_alarm_s", "opt_no_data_reply_only"},
                "console_encoding": {"opt_console_encoding"},
                "serial_mqtt": {"mqtt_line_end", "mqtt_tx_shown", "mqtt_tx_enabled"},
                "mqtt_retain": {"mqtt_retain_shown", "mqtt_retain"},
                "mqtt_tls_files": {"mqtt_ca_file", "mqtt_client_cert", "mqtt_client_key",
                                   "mqtt_client_key_password", "mqtt_tls_check_hostname"},
                "primary_host": {"mqtt_sparkplug_primary_host_id"},
                "modbus_gateway": {"opt_modbus_response_timeout_s", "opt_modbus_unit_map"},
                "block_reads": {"modbus_block_reads_shown", "modbus_block_gap"},
                "point_options": {"modbus_point_options_shown"},
                "value_format": {"mqtt_value_format"},
                "unit_status": {"mqtt_unit_status_shown"},
                "mqtt5": {"mqtt_protocol", "mqtt_v5_shown", "mqtt_clean_start",
                          "mqtt_session_expiry_s", "mqtt_message_expiry_s"},
                "point_safety": {"modbus_point_safety_shown"},
                "packing": {"opt_pack_gap_ms", "opt_pack_delimiter", "opt_pack_max_bytes"},
                "writer_ips": {"network_writer_client_ips"},
                "open_on_connect": {"network_open_on_connect_shown", "network_open_on_connect",
                                    "network_port_linger_s"},
                "pool": {"network_pool"},
                "failover": {"network_remote_targets", "network_failover_return_s"}}
# fields of every older version's form that its own posts in the fixtures leave out
# (boxes no kitchen-sink mapping ticks, a custom baud box not in use), and the MQTT
# client ID, which every version's save reads although only 2.9.0's form shows it
OLDER_FIELDS_NOT_POSTED = {"serial_exclusive", "serial_b_exclusive",
                           "serial_rs485_rts_level_for_tx", "serial_b_rs485_rts_level_for_tx",
                           "serial_b_custom_baud", "mqtt_client_id",
                           # (2.9.0+: boxes of the TLS blocks a mapping without that TLS
                           # leaves unticked)
                           "mqtt_tls_check_hostname", "mqtt_client_key_password_clear",
                           "network_tls_key_password_clear"}


def same_value(a: str, b: str) -> bool:
    if a == b:
        return True
    try:  # a number written another way ("10" / "10.0")
        return float(a) == float(b)
    except ValueError:
        return False


def test_older_node_edits_this_one(fx: dict) -> None:
    for d in cases.mappings():
        stored = MappingConfig.from_dict(d)
        edited = forms.mapping_from_form(FormData(fx["forms"][d["id"]]), stored, cases.PORTS)
        assert_kept(stored, edited, f"edited from a {fx['version']} node")
    print(f"{fx['version']} node edits a mapping here: an unchanged save keeps every "
          "setting  OK")


def test_this_node_edits_an_older_one(fx: dict) -> None:
    version = fx["version"].lstrip("v")
    for mid, own in fx["mapping_data"].items():
        shown = MappingConfig.from_dict(own)
        page = render(shown, ports=fx["ports"], **_peer_form(PEER, shown, version, own["id"], {}))
        # a note when it runs another version than this node
        assert (f"runs version {version}, this node" in page) == (
            _peer_version(version) != _peer_version(__version__)), (version, __version__)
        fields, err = _peer_save_fields(FormData(browser_post(page)), own, version)
        assert err is None, (own["name"], err)
        sent = dict(fields)
        # a one-line field that can't show its stored value: the stored one is sent (that
        # node's own form loses its line breaks)
        raw = {f: own[section].get(attr) for f, (section, attr) in forms._TEXT_INPUTS.items()}
        problems = [f"{k}: {v!r} -> {sent.get(k, '<not sent>')!r}"
                    for k, v in fx["own_forms"][mid]
                    if not same_value(v, sent.get(k, "\0")) and sent.get(k) != raw.get(k)]
        assert not problems, (f"{own['name']} on a {version} node: this node posts other "
                              "values than that node's own form:\n  " + "\n  ".join(problems))
        unread = sorted(BLOCK_FIELDS[b] & sent.keys()
                        for b in forms.blocks_not_read(_peer_version(version)))
        assert not any(unread), (own["name"], version, unread)
    print(f"this node edits a mapping on a {version} node: it posts what that node's own "
          "form posts (trace path included)  OK")


def test_older_node_shown_only_its_fields(fx: dict) -> None:
    version = fx["version"].lstrip("v")
    known = set(OLDER_FIELDS_NOT_POSTED)
    for own in fx["own_forms"].values():
        known |= {k for k, _v in own}
    for own in fx["mapping_data"].values():
        shown = MappingConfig.from_dict(own)
        page = render(shown, ports=fx["ports"], **_peer_form(PEER, shown, version, own["id"], {}))
        # every field the fleet view would forward, ticked or not
        offered = {k for k in Form(page).names() if k not in _PEER_PASSWORD_FIELDS
                   and (k in _PEER_FORM_FIELDS or k.startswith(_PEER_FORM_PREFIXES))}
        new = sorted(offered - known)
        assert not new, (f"{own['name']} on a {version} node: the form offers fields that "
                         f"{version} ignores (put them in a forms.FORM_BLOCKS_SINCE block): {new}")
    print(f"this node's form for a mapping on a {version} node offers only the fields "
          f"{version} reads  OK")


def test_blocks_listed() -> None:
    assert set(forms.FORM_BLOCKS_SINCE) == set(BLOCK_FIELDS), (
        "a FORM_BLOCKS_SINCE block without a field in BLOCK_FIELDS here, or the reverse")
    newest = max(forms.FORM_BLOCKS_SINCE.values())
    assert not forms.blocks_not_read(newest) and forms.blocks_not_read(None) == set(BLOCK_FIELDS)
    print(f"{len(BLOCK_FIELDS)} blocks; a node of {'.'.join(map(str, newest))} is shown every "
          "one, a node that names no version none  OK")


def main():
    assert FIXTURES, "tests/fixtures/forms-*.json missing"
    test_blocks_listed()
    for path in FIXTURES:
        fx = load(path)
        test_older_node_edits_this_one(fx)
        test_this_node_edits_an_older_one(fx)
        test_older_node_shown_only_its_fields(fx)
    print(f"\nPASS: fleet-view edits with {', '.join(load(p)['version'] for p in FIXTURES)} "
          "nodes keep every setting")


if __name__ == "__main__":
    main()
