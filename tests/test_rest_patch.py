"""REST API: PATCH /api/v1/mappings/{id} changes only the fields it names.

  * merge_patch follows RFC 7396 (its appendix A examples): objects merge key by key,
    null removes a key, a list or a value replaces what was there;
  * PATCH {"name": ...} keeps everything else of the stored mapping — bind address,
    allow-list, read-only, enabled, the USB match, the MQTT password — where PUT with the
    same body would reset those to their defaults (PUT stays a full replace);
  * a nested object merges (network.port alone), null resets a field to its default;
  * a patch that makes the mapping invalid, a body that is no JSON object and an unknown
    mapping are refused and change nothing; the change is audited;
  * a patch is checked as a PUT is: a serial port another enabled mapping uses or the
    admin UI's own port is refused, the MQTT / TLS key password mask sent back keeps the
    stored secret;
  * a created mapping's URL is in Location; a client's connection time is also a number;
  * openapi.json describes every route build_api_routes registers, with the token errors
    (401, 503; 403 for a change) and the Error schema; the mapping schemas are made from
    the config dataclasses (every field, its default, open to fields added later).

Run: python3 tests/test_rest_patch.py
"""
import dataclasses
import json
import time
import types
import typing
import urllib.error
import urllib.request

from _server import Server, free_port

from app import config
from app.web.api import API_PREFIX, build_api_routes, mapping_schemas, merge_patch, openapi_spec
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


def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def test_spec():
    spec = json.loads(json.dumps(openapi_spec(None)))  # JSON as served
    assert spec["openapi"].startswith("3.0"), spec["openapi"]
    paths = spec["paths"]
    routes = build_api_routes(types.SimpleNamespace())  # building them reads no state
    public = {"/health", "/openapi.json"}
    for route in routes:
        rel = route.path[len(API_PREFIX):]
        assert rel in paths, f"{route.path} is not in openapi.json"
        for method in route.methods - {"HEAD"}:
            o = paths[rel].get(method.lower())
            assert o is not None, f"{method} {route.path} is not in openapi.json"
            codes = set(o["responses"])
            if rel in public:
                assert o["security"] == [], rel
                continue
            assert {"401", "503"} <= codes, (method, rel, codes)
            assert ("403" in codes) == (method in ("POST", "PUT", "PATCH", "DELETE")), (method, rel)
            for code, r in o["responses"].items():
                if code[0] in "45" and code != "413":
                    assert r["$ref"].startswith("#/components/responses/"), (method, rel, code)
    described = {(p, m) for p, item in paths.items() for m in item if m != "parameters"}
    registered = {(r.path[len(API_PREFIX):], m.lower()) for r in routes for m in r.methods - {"HEAD"}}
    assert described == registered, described ^ registered
    for target in set(_refs(spec)):
        section, name = target.split("/")[2:]
        assert name in spec["components"][section], f"{target} does not resolve"
    for name in ("BadRequest", "Unauthorized", "Forbidden", "NotFound", "SetupIncomplete"):
        schema = spec["components"]["responses"][name]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/Error"}, name
    print(f"openapi.json: all {len(registered)} routes described, with 401/503 (403 for "
          "changes) through the Error schema; every $ref resolves  OK")

    schemas = mapping_schemas()
    classes = {"Mapping": config.MappingConfig, "SerialSettings": config.SerialSettings,
               "SerialAdvanced": config.SerialAdvanced, "NetworkSettings": config.NetworkSettings,
               "MappingOptions": config.MappingOptions, "MqttSettings": config.MqttSettings,
               "ModbusPoll": config.ModbusPoll, "ModbusPoint": config.ModbusPoint}
    names = {cls: name for name, cls in classes.items()}
    for name, cls in classes.items():
        sch = schemas[name]
        assert sch["additionalProperties"] is True and "required" not in sch, name
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            if cls is config.MappingConfig and f.name == "input_notes":
                assert f.name not in sch["properties"], "what reading noted is no JSON field"
                continue
            prop = sch["properties"][f.name]
            if dataclasses.is_dataclass(hints[f.name]):
                assert prop == {"$ref": f"#/components/schemas/{names[hints[f.name]]}"}, prop
            elif f.name != "id":
                default = (f.default if f.default is not dataclasses.MISSING
                           else f.default_factory())
                assert prop["default"] == default, (name, f.name, prop)
    m = schemas["Mapping"]["properties"]
    assert m["id"]["readOnly"] and m["status"]["readOnly"], m["id"]
    net = schemas["NetworkSettings"]["properties"]
    assert net["bind_ip"]["default"] == "0.0.0.0" and net["port"]["default"] == 4001
    assert net["mode"]["enum"] == list(config.NET_MODES)
    assert schemas["SerialSettings"]["properties"]["stopbits"]["enum"] == [1, 1.5, 2]
    print("openapi.json: the mapping schemas name every config field with its type and "
          "default; id and status read-only; open to new fields  OK")


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

        # a created mapping's URL is in Location
        req = urllib.request.Request(
            base + "/api/v1/mappings", method="POST", headers={
                "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            data=json.dumps({"name": "made", "enabled": False,
                             "serial": {"port": "/dev/ser2net-rest-patch-2"},
                             "network": {"bind_ip": "127.0.0.1", "port": free_port()}}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            made = json.loads(r.read())
            assert r.status == 201 and r.headers["Location"] == f"/api/v1/mappings/{made['id']}"
        assert call(base, "GET", r.headers["Location"])[1]["name"] == "made"
        print("POST: the new mapping's URL in Location  OK")

        with open(srv.data + "/audit.log", encoding="utf-8") as fh:
            audit = fh.read()
        assert "\tapi_mapping_patch\tplc-renamed\tapi-token" in audit, audit[-500:]
        print("PATCH is audited  OK")


def test_patch_checks():
    other = {"id": "patchother001", "name": "other", "enabled": True,
             "serial": {"port": "/dev/ser2net-rest-patch-other"},
             "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port()}}
    mine = {"id": MID, "name": "plc", "enabled": False,
            "serial": {"port": "/dev/ser2net-rest-patch"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port(),
                        "tls_key_password": "key-secret"},
            "mqtt": {"enabled": False, "host": "broker", "password": "broker-secret"}}
    config = {"users": [{"username": "admin", "password_hash": hash_password("patchpass1"),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "operator",
              "mappings": [other, mine]}
    with Server(config) as srv:
        def saved():
            return next(m for m in srv.saved_config()["mappings"] if m["id"] == MID)

        good = saved()
        st, doc = call(srv.base, "PATCH", f"/api/v1/mappings/{MID}",
                       {"enabled": True, "serial": {"port": "/dev/ser2net-rest-patch-other"}})
        assert st == 400 and doc["error"] == "validation", (st, doc)
        assert "both use serial port /dev/ser2net-rest-patch-other" in doc["detail"], doc
        st, doc = call(srv.base, "PATCH", f"/api/v1/mappings/{MID}",
                       {"network": {"port": srv.port}})
        assert st == 400 and "the admin UI's port" in doc["detail"], (st, doc)
        assert saved() == good, "a refused patch changed the mapping"
        st, doc = call(srv.base, "PATCH", f"/api/v1/mappings/{MID}",
                       {"name": "plc-2", "mqtt": {"password": "********"},
                        "network": {"tls_key_password": "********"}})
        assert st == 200, (st, doc)
        m = saved()
        assert m["name"] == "plc-2" and m["mqtt"]["password"] == "broker-secret", m
        assert m["network"]["tls_key_password"] == "key-secret", m
    print("PATCH: a shared serial port and the admin UI's port are refused; the password "
          "masks sent back keep the stored secrets  OK")


def test_connected_seconds():
    from app.engine.bridge import RunnerStatus

    st = RunnerStatus()
    st.set_clients([types.SimpleNamespace(peer="10.0.0.5:5000", seq=1, priority=False,
                                          connected_at=time.monotonic() - 125)])
    client = st.as_dict()["clients"][0]
    assert client["duration"] and 124 <= client["connected_s"] <= 127, client
    print("status: a client's connection time also as a number (connected_s)  OK")


def main():
    test_merge_patch()
    test_connected_seconds()
    test_spec()
    test_patch()
    test_patch_checks()
    print("\nPASS: REST API PATCH (JSON merge patch)")


if __name__ == "__main__":
    main()
