"""REST API: PATCH /api/v1/mappings/{id} changes only the fields it names.

  * merge_patch follows RFC 7396 (its appendix A examples): objects merge key by key,
    null removes a key, a list or a value replaces what was there;
  * PATCH {"name": ...} keeps everything else of the stored mapping — bind address,
    allow-list, read-only, enabled, the USB match, the MQTT password — where PUT with the
    same body would reset those to their defaults (PUT stays a full replace);
  * a nested object merges (network.port alone), null resets a field to its default;
  * a patch that makes the mapping invalid, a body that is no JSON object and an unknown
    mapping are refused and change nothing; the change is audited.

Run: python3 tests/test_rest_patch.py
"""
import json
import urllib.error
import urllib.request

from _server import Server, free_port

from app.web.api import merge_patch
from app.web.auth import hash_password, hash_token

TOKEN = "s2n_rest-patch-operator-token"
MID = "patchme000001"


def test_merge_patch():
    # RFC 7396, appendix A
    cases = [
        ({"a": "b"}, {"a": "c"}, {"a": "c"}),
        ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
        ({"a": "b"}, {"a": None}, {}),
        ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
        ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
        ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
        ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
        ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
        (["a", "b"], ["c", "d"], ["c", "d"]),
        ({"a": "b"}, ["c"], ["c"]),
        ({"a": "foo"}, None, None),
        ({"a": "foo"}, "bar", "bar"),
        ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
        ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
        ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
    ]
    for target, patch, want in cases:
        before = json.dumps(target)
        assert merge_patch(target, patch) == want, (target, patch, merge_patch(target, patch))
        assert json.dumps(target) == before, "merge_patch must not change its input"
    print("merge_patch: the RFC 7396 appendix A examples  OK")


def call(base, method, path, body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/merge-patch+json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        r = e
    return (r.status if hasattr(r, "status") else r.code), json.loads(r.read() or b"null")


def test_patch():
    port = free_port()
    stored = {"id": MID, "name": "plc", "enabled": False, "kind": "net",
              "serial": {"port": "/dev/ser2net-rest-patch", "baudrate": 19200,
                         "match": {"vid": "0403", "pid": "6001", "serial_number": "A1"}},
              "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port,
                          "allowed_client_ips": ["10.0.0.0/8"], "read_only": True},
              "mqtt": {"enabled": False, "host": "broker", "password": "broker-secret"},
              "options": {"trace_both": "t.log"}}
    config = {"users": [{"username": "admin", "password_hash": hash_password("patchpass1"),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "operator",
              "mappings": [stored]}
    with Server(config) as srv:
        base = srv.base

        def saved():
            return next(m for m in srv.saved_config()["mappings"] if m["id"] == MID)

        before = saved()
        st, doc = call(base, "PATCH", f"/api/v1/mappings/{MID}", {"name": "plc-renamed"})
        assert st == 200 and doc["name"] == "plc-renamed" and doc["id"] == MID, (st, doc)
        after = saved()
        assert after == dict(before, name="plc-renamed"), "PATCH changed more than the name"
        assert after["enabled"] is False and after["network"]["bind_ip"] == "127.0.0.1"
        assert after["network"]["allowed_client_ips"] == ["10.0.0.0/8"]
        assert after["network"]["read_only"] is True and after["serial"]["match"]["vid"] == "0403"
        assert after["mqtt"]["password"] == "broker-secret" and after["kind"] == "net"
        print("PATCH {name}: bind address, allow-list, read-only, enabled, USB match and MQTT "
              "password kept  OK")

        new_port = free_port()
        st, doc = call(base, "PATCH", f"/api/v1/mappings/{MID}",
                       {"network": {"port": new_port}, "id": "ignored", "status": {"x": 1}})
        assert st == 200 and doc["network"]["port"] == new_port, (st, doc)
        net = saved()["network"]
        assert net["port"] == new_port and net["allowed_client_ips"] == ["10.0.0.0/8"]
        assert net["bind_ip"] == "127.0.0.1" and net["read_only"] is True
        st, doc = call(base, "PATCH", f"/api/v1/mappings/{MID}",
                       {"network": {"allowed_client_ips": None}, "options": None})
        assert st == 200, (st, doc)
        m = saved()
        assert m["network"]["allowed_client_ips"] == [] and m["network"]["read_only"] is True
        assert m["options"]["trace_both"] == "", "null resets a section to its defaults"
        assert [x["id"] for x in srv.saved_config()["mappings"]] == [MID]
        print("PATCH: a nested object merges, null resets a field or section to its default; "
              "the id in the body is ignored  OK")

        good = saved()
        for body, want in (({"network": {"port": 0}}, "validation"),
                           ({"name": "  "}, "validation"),
                           ({"kind": "teleport"}, "validation"),
                           ({"serial": "not an object"}, "validation")):
            st, doc = call(base, "PATCH", f"/api/v1/mappings/{MID}", body)
            assert st == 400 and doc["error"] == want, (body, st, doc)
        for raw in (b"not json", b"[1, 2]", b'"name"'):
            st, doc = call(base, "PATCH", f"/api/v1/mappings/{MID}", raw=raw)
            assert st == 400 and doc["error"] == "invalid_json", (raw, st, doc)
        assert saved() == good, "a refused patch changed the mapping"
        st, doc = call(base, "PATCH", "/api/v1/mappings/nosuchmapping", {"name": "x"})
        assert st == 404 and doc == {"error": "not_found"}, (st, doc)
        st, doc = call(base, "PATCH", "/api/v1/mappings/bad-id!", {"name": "x"})
        assert st == 404, (st, doc)
        print("PATCH: an invalid result, a body that is no JSON object and an unknown mapping "
              "are refused; nothing changes  OK")

        # PUT is still a full replace (documented): what the body leaves out is reset
        st, doc = call(base, "PUT", f"/api/v1/mappings/{MID}",
                       {"name": "plc", "serial": {"port": "/dev/ser2net-rest-patch"},
                        "enabled": False, "network": {"bind_ip": "127.0.0.1", "port": new_port}})
        assert st == 200 and doc["network"]["read_only"] is False and doc["serial"]["match"] == {}
        print("PUT: still replaces the whole mapping (left-out fields get their defaults)  OK")

        with open(srv.data + "/audit.log", encoding="utf-8") as fh:
            audit = fh.read()
        assert "\tapi_mapping_patch\tplc-renamed\tapi-token" in audit, audit[-500:]
        print("PATCH is audited  OK")


def main():
    test_merge_patch()
    test_patch()
    print("\nPASS: REST API PATCH (JSON merge patch)")


if __name__ == "__main__":
    main()
