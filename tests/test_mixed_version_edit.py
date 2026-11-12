"""Fleet-view edits between nodes of different versions keep every setting.

tests/fixtures/forms-<version>.json were made once with the code of 2.7.1, 2.8.0 and
2.8.1 (tests/make_form_fixtures.py) for the kitchen-sink mappings of
tests/_form_cases.py. With them, without running the old code:

  * an older node edits a mapping on this one: what its fleet view forwards for an
    unchanged save ("forms"), parsed by this version's save, changes nothing (2.7.1
    has no "writable" for Modbus points, no USB identity, no MQTT client id ...);
  * this node edits a mapping on an older one: the form it renders from that node's
    mapping-data ("mapping_data") and the fields it forwards post, for every field the
    older node's own form posts ("own_forms"), the same value — so the older node keeps
    what it would keep after an edit on itself (2.7.1's trace path included) — and none
    of the blocks that version doesn't read (forms.FORM_BLOCKS_SINCE).

Pure in-process test (no sockets).

Run: python3 tests/test_mixed_version_edit.py
"""
from __future__ import annotations

import glob
import json
import os

import _form_cases as cases
from _formsubmit import browser_post
from _server import ROOT  # noqa: F401  (sys.path setup)
from starlette.datastructures import FormData
from test_mapping_form_roundtrip import PEER, assert_kept, render

from app import __version__
from app.config import MappingConfig
from app.web.routes import forms
from app.web.routes.cluster import _peer_form, _peer_save_fields, _peer_version

FIXTURES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "forms-*.json")))


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.loads(fh.read().replace("@TLS_FILE@", json.dumps(cases.TLS_FILE)[1:-1]))


# a form field of each block a node reads only since some version
BLOCK_FIELDS = {"usb_match": {"serial_usb_match_shown"}, "trace_rotation": {"opt_trace_max_mb"},
                "payload_format": {"mqtt_payload_format"},
                "retained_set": {"mqtt_apply_retained_set_shown"},
                "rfc2217_restore": {"opt_rfc2217_restore_on_disconnect_shown"},
                "udp_reply": {"network_udp_peer_timeout_s"}}


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
        page = render(shown, ports=fx["ports"], **_peer_form(PEER, shown, version, own["id"]))
        # a note when it runs another version than this node
        assert (f"runs version {version}, this node" in page) == (
            _peer_version(version) != _peer_version(__version__)), (version, __version__)
        fields, err = _peer_save_fields(FormData(browser_post(page)), own, version)
        assert err is None, (own["name"], err)
        sent = dict(fields)
        # a one-line field that can't show its stored value: the stored one is sent (that
        # node's own form loses its line breaks)
        raw = {f: own[section][attr] for f, (section, attr) in forms._TEXT_INPUTS.items()}
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


def main():
    assert FIXTURES, "tests/fixtures/forms-*.json missing"
    for path in FIXTURES:
        fx = load(path)
        test_older_node_edits_this_one(fx)
        test_this_node_edits_an_older_one(fx)
    print(f"\nPASS: fleet-view edits with {', '.join(load(p)['version'] for p in FIXTURES)} "
          "nodes keep every setting")


if __name__ == "__main__":
    main()
