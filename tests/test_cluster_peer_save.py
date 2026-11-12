"""The fleet-view edit proxy forwards only known, locally-validated mapping fields.

cluster_peer_save proxies an operator's mapping edit to a peer with the shared key. It
must not relay raw form values: a free trace path would reach an older peer that stores
it and opens it in append mode, and a pyserial URL serial port would reach any peer. So
_peer_save_fields forwards only the mapping form's own fields, validates the submission
locally, reduces the trace field to a sanitised file name (kept as the peer stored it
when unchanged, refused as a new name for a pre-2.8 peer), and requires the mapping to
exist on the peer. Pure unit test (no sockets); the two-node proxy path is in
tests/test_cluster.py.

Run: python3 tests/test_cluster_peer_save.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from starlette.datastructures import FormData

from app.web.routes.cluster import _peer_below_2_8, _peer_save_fields


def form(**kw):
    base = {"id": "abc", "name": "m", "kind": "net", "serial_port": "/dev/ttyS0",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": "4001"}
    base.update(kw)
    return FormData(list(base.items()))


def test_version_gate():
    for v in ("2.8.0", "2.8.1", "2.9.0", "3.0.0", "v2.8", "2.10.0"):
        assert not _peer_below_2_8(v), v
    for v in ("2.7.1", "2.6.1", "2.0.0", "1.9.9", "", "unknown", "2"):
        assert _peer_below_2_8(v), v
    print("version gate: 2.8+ passes; older or unparsable is treated as old  OK")


def test_requires_existing_mapping():
    fields, err = _peer_save_fields(form(), None, "2.8.0")
    assert fields is None and "no longer exists" in err, err
    print("a mapping the peer doesn't have is refused (no create via the proxy)  OK")


def test_rejects_url_serial_port():
    for port in ("socket://10.0.0.1:502", "rfc2217://h:1", "spy:///dev/ttyS0?file=/x"):
        fields, err = _peer_save_fields(form(serial_port=port), {"options": {}}, "2.8.0")
        assert fields is None and "URL" in err, (port, err)
    print("a URL serial port is rejected locally, never forwarded  OK")


def test_forwards_only_known_fields():
    # fields that are not part of the mapping form (the peer-addressing ones, the CSRF
    # token, and anything extra the request carries) must not be relayed to the peer
    fields, err = _peer_save_fields(
        form(scheme="http", host="h", port="1", _csrf="t", extra_one="a", extra_two="b"),
        {"options": {"trace_both": ""}}, "2.8.0")
    assert err is None, err
    keys = {k for k, _v in fields}
    for forbidden in ("scheme", "host", "port", "_csrf", "extra_one", "extra_two"):
        assert forbidden not in keys, forbidden
    assert {"id", "name", "serial_port", "network_port"} <= keys, keys
    print("only the mapping form's own fields are forwarded; other fields are dropped  OK")


def test_trace_sanitised():
    # a new trace path on a 2.8 peer is reduced to a bare file name
    fields, err = _peer_save_fields(form(opt_trace_both="/data/config.json"),
                                    {"options": {"trace_both": ""}}, "2.8.0")
    assert err is None and dict(fields)["opt_trace_both"] == "config.json", fields
    fields, err = _peer_save_fields(form(opt_trace_both="../../etc/cron.d/x"),
                                    {"options": {"trace_both": ""}}, "2.9.0")
    assert dict(fields)["opt_trace_both"] == "x", fields
    print("a new trace path is reduced to a file name before it is forwarded  OK")


def test_trace_new_name_refused_pre_2_8():
    fields, err = _peer_save_fields(form(opt_trace_both="/data/config.json"),
                                    {"options": {"trace_both": ""}}, "2.7.1")
    assert fields is None and "older than 2.8" in err, err
    # clearing a trace (empty) is always allowed, even on an old peer
    fields, err = _peer_save_fields(form(opt_trace_both=""),
                                    {"options": {"trace_both": "data/old.log"}}, "2.7.1")
    assert err is None and dict(fields)["opt_trace_both"] == "", (err, fields)
    print("a NEW trace name needs a 2.8+ peer; clearing a trace is always allowed  OK")


def test_unchanged_trace_kept_as_stored():
    # the form shows trace_file_name(peer's stored path); submitting it unchanged keeps
    # the peer's exact stored value, so a legacy path survives an edit (not rewritten)
    fields, err = _peer_save_fields(form(opt_trace_both="trace-plc1.log"),
                                    {"options": {"trace_both": "data/trace-plc1.log"}}, "2.7.1")
    assert err is None and dict(fields)["opt_trace_both"] == "data/trace-plc1.log", fields
    print("an unchanged trace field keeps the peer's stored value verbatim  OK")


def test_older_peer_keeps_what_the_form_leaves_out():
    # this node's form doesn't offer the banner, close-on string or client buffer for a
    # Modbus gateway; a peer before 2.8.1 sets them from every form, so it gets its own
    peer = {"options": {"trace_both": "", "banner": "Hi", "closeon": "BYE\r\n"},
            "network": {"client_queue_max": 8192}}
    modbus = form(network_protocol="modbus", network_port="502")
    for version, sent in (("2.8.0", {"opt_banner": "Hi", "opt_closeon": "BYE\r\n",
                                     "network_client_queue_max": "8192"}),
                          ("2.8.1", {}), ("2.9.0", {})):
        fields, err = _peer_save_fields(modbus, peer, version)
        assert err is None, err
        got = {k: v for k, v in fields if k in ("opt_banner", "opt_closeon",
                                                "network_client_queue_max")}
        assert got == sent, (version, got)
    # a close-on string posted back as a one-line field shows it: the peer's own value
    fields, _err = _peer_save_fields(form(opt_closeon="BYE"), peer, "2.9.0")
    assert dict(fields)["opt_closeon"] == "BYE\r\n", fields
    fields, _err = _peer_save_fields(form(opt_closeon="QUIT"), peer, "2.9.0")
    assert dict(fields)["opt_closeon"] == "QUIT", fields
    print("an older peer gets its stored banner/close-on/client buffer the form leaves out; "
          "an unchanged close-on keeps its line breaks  OK")


def main():
    test_version_gate()
    test_requires_existing_mapping()
    test_rejects_url_serial_port()
    test_forwards_only_known_fields()
    test_trace_sanitised()
    test_trace_new_name_refused_pre_2_8()
    test_unchanged_trace_kept_as_stored()
    test_older_peer_keeps_what_the_form_leaves_out()
    print("\nPASS: fleet-view edit proxy forwards only known, validated fields")


if __name__ == "__main__":
    main()
