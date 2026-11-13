"""Mapping ids are checked at every entry point and stay inside the data directory.

A mapping id names the mapping's files (logs/<id>.log, traces/<id>-<name>) and is part
of URLs, so only 1-64 letters, digits or "_" are accepted. Checks:
  * the validator: generated ids pass; separators, ".", "-", NUL, non-strings, overlong
    ids and Windows device names don't;
  * loading (config file, backup, import) replaces an invalid id with a stable derived
    one and says so (a load note / an import warning); validate() and save() still pass;
  * the log / trace path helpers refuse invalid ids, so nothing is written outside
    data/logs and data/traces even if one got through;
  * the web form and the cluster mapping-save only edit mappings that exist (a new
    mapping gets a server-made id); REST PUT with an invalid id is 404;
  * a config with such ids boots, logs what changed, re-saves valid ids, and the
    dashboard, /metrics and deleting the mappings work.
Cross-platform (on Windows the backslash cases exercise the native separator).

Run: python3 tests/test_mapping_ids.py
"""
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port

from ser2netweb.config import AppConfig, ConfigStore, MappingConfig, mapping_file, valid_mapping_id
from ser2netweb.importer import parse_mappings_file
from ser2netweb.state import AppState
from ser2netweb.web.auth import hash_password, hash_token

PW = "mapping-ids-pass-1"
TOKEN = "s2n_mapping-ids-token"
KEY = "mapping-ids-cluster-key"
BAD_IDS = ["../audit", "..\\audit", "/tmp/s2n-abs", "C:\\s2n", "C:s2n", "a\x00b", "plc-1",
           "plc.1", "a b", "", "x" * 65, "CON", "nul", "com1", "lpt9", "é"]


def test_validator():
    for good in ("a1b2c3d4e5f6", "plc_1", "A", "x" * 64, "0"):
        assert valid_mapping_id(good), good
    for bad in BAD_IDS + [None, 1, ["a"], {"a": 1}, True]:
        assert not valid_mapping_id(bad), bad
    for _ in range(50):
        assert valid_mapping_id(MappingConfig().id), "generated ids must be valid"
    print("validator: generated / letter-digit-underscore ids pass; paths, '-', '.', NUL, "
          "device names and non-strings don't  OK")


def test_load_normalizes():
    raw = [{"id": bad, "name": f"m{i}", "serial": {"port": f"COM{i + 1}"},
            "network": {"bind_ip": "127.0.0.1", "port": 5001 + i}}
           for i, bad in enumerate(["../audit", "a\x00b", ["x"], {"y": 1}, "plc-1"])]
    raw.append({"id": "keepme_1", "name": "good", "serial": {"port": "COM9"},
                "network": {"bind_ip": "127.0.0.1", "port": 5009}})
    cfg = AppConfig.from_dict({"mappings": raw})
    ids = [m.id for m in cfg.mappings]
    assert all(valid_mapping_id(i) for i in ids), ids
    assert ids[-1] == "keepme_1", "a valid id is kept as it is"
    assert len(set(ids)) == len(ids), ids
    again = [m.id for m in AppConfig.from_dict(json.loads(json.dumps({"mappings": raw}))).mappings]
    assert again == ids, "a replaced id must be the same on every load"
    notes = [n for n in cfg.load_notes if "is not a valid mapping id" in n]
    assert len(notes) == 5, cfg.load_notes
    assert all("\n" not in n and "\x00" not in n for n in notes)
    cfg.validate()
    d = tempfile.mkdtemp(prefix="s2n_ids_")
    store = ConfigStore(os.path.join(d, "config.json"))
    store.save(cfg)
    assert [m["id"] for m in store.load().to_dict()["mappings"]] == ids
    fresh = MappingConfig.from_dict({"name": "no id"})
    assert valid_mapping_id(fresh.id)
    print("load: invalid ids get a stable valid id + a load note; valid ids kept; "
          "validate/save pass  OK")


def test_path_helpers():
    d = tempfile.mkdtemp(prefix="s2n_ids_paths_")
    for bad in BAD_IDS:
        try:
            mapping_file(d, bad, ".log")
        except ValueError:
            pass
        else:
            raise AssertionError(f"mapping_file accepted {bad!r}")
    for suffix in ("-../x", "-a/b", "-a\\b" if os.name == "nt" else "/x"):
        try:
            mapping_file(d, "abc", suffix)
        except ValueError:
            pass
        else:
            raise AssertionError(f"mapping_file accepted the suffix {suffix!r}")
    assert mapping_file(d, "abc", ".log") == os.path.join(d, "abc.log")

    data = os.path.join(d, "data")
    st = AppState(ConfigStore(os.path.join(data, "config.json")), AppConfig())
    audit = os.path.join(data, "audit.log")
    with open(audit, "w", encoding="utf-8") as fh:
        fh.write("keep\n")
    before = sorted(os.listdir(data))
    for bad in BAD_IDS:
        lg = st._mapping_logger(bad)          # must not raise (e.g. NUL -> ValueError)
        lg.info("hello")
        assert st.read_mapping_log(bad) == []
        assert st.trace_files(bad) == []
        st.delete_mapping_log(bad)
        st.delete_mapping_traces(bad)
    assert sorted(os.listdir(data)) == before, os.listdir(data)
    assert os.listdir(st.logs_dir) == [], os.listdir(st.logs_dir)
    assert open(audit, encoding="utf-8").read() == "keep\n", "audit.log was touched"
    for name in ("abc-x.log", "abc_2-y.log"):
        with open(os.path.join(st.traces_dir, name), "w") as fh:
            fh.write(name)
    assert [os.path.basename(p) for p in st.trace_files("abc")] == ["abc-x.log"], \
        "a mapping's prefix must not match another mapping's traces"
    print("paths: invalid ids never name a file; logs/traces of a bad id are refused "
          "without errors; prefixes don't overlap  OK")


def test_import_warns():
    doc = {"mappings": [
        {"id": "../evil", "name": "A", "serial": {"port": "COM1"}},
        {"id": ["l"], "name": "B", "serial": {"port": "COM2"}},
        {"id": "okid", "name": "C", "serial": {"port": "COM3"}}]}
    plan = parse_mappings_file(json.dumps(doc).encode())
    assert [valid_mapping_id(m.id) for m in plan.mappings] == [True, True, True]
    assert plan.mappings[2].id == "okid"
    warned = [w for w in plan.warnings if "is not a valid mapping id" in w]
    assert len(warned) == 2 and "'../evil'" in warned[0], plan.warnings
    print("import: invalid ids are replaced and listed as warnings in the preview  OK")


def boot_config(map_ports):
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]

    def mapping(mid, name, port):
        return {"id": mid, "name": name, "enabled": True, "kind": "net",
                "serial": {"port": f"/dev/ser2net-test-{name}"},
                "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                            "port": port},
                "options": {"trace_both": "t.log"}}

    return {"users": users, "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
            "cluster": {"enabled": True, "key": KEY, "discovery_port": free_port()},
            "mappings": [mapping("../audit", "up", map_ports[0]),
                         mapping("a\u0000b", "nul", map_ports[1]),
                         mapping(["x"], "listid", map_ports[2]),
                         mapping("plc-1", "dash", map_ports[3]),
                         mapping("goodid12", "good", map_ports[4])]}


def api(srv, method, path, body=None):
    req = urllib.request.Request(srv.base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def form_fields(name, port, **extra):
    return {"name": name, "kind": "net", "serial_port": f"/dev/ser2net-test-{name}",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(port), **extra}


def test_server():
    ports = [free_port() for _ in range(5)]
    with Server(boot_config(ports)) as srv:
        log = srv.output()
        assert log.count("is not a valid mapping id") == 4, log[-3000:]
        saved = srv.saved_config()   # SER2NETWEB_BIND_IP set: the boot re-saved the config
        ids = [m["id"] for m in saved["mappings"]]
        assert all(valid_mapping_id(i) for i in ids) and ids[-1] == "goodid12", ids
        data = srv.data
        assert sorted(os.listdir(os.path.join(data, "logs"))) == sorted(f"{i}.log" for i in ids)
        audit = os.path.join(data, "audit.log")
        if os.path.exists(audit):
            assert "listening on" not in open(audit, encoding="utf-8").read(), "mapping log in audit.log"
        traces = os.listdir(os.path.join(data, "traces"))
        assert sorted(traces) == sorted(f"{i}-t.log" for i in ids), traces
        print("boot: invalid stored ids replaced (logged), config re-saved with valid ids, "
              "logs/traces only under data/  OK")

        b = Browser(srv.base)
        b.login("admin", PW)
        assert b.get("/")[0] == 200
        st, _, table = b.get("/api/status")
        assert st == 200 and all(f'data-mid="{i}"' in table for i in ids), table[:500]
        st, _, metrics = b.get("/metrics")
        assert st == 200 and sorted(re.findall(r'ser2net_mapping_running\{mapping="[^"]*",id="(\w+)"\}',
                                               metrics)) == sorted(ids), metrics
        print("dashboard and /metrics render every mapping by its valid id  OK")

        count = len(srv.saved_config()["mappings"])
        for bad in ("../audit", "nosuchid1", "a\x00b"):
            st, _, body = b.post("/api/mappings/save", form_fields("x", free_port(), id=bad))
            assert st == 400 and "no longer exists" in body, (bad, st, body[:300])
        assert len(srv.saved_config()["mappings"]) == count, "an unknown id must not add a mapping"
        st, _, _ = b.post("/api/mappings/save", form_fields("fresh", free_port()))
        assert st == 200
        fresh = next(m for m in srv.saved_config()["mappings"] if m["name"] == "fresh")
        assert re.fullmatch(r"[0-9a-f]{12}", fresh["id"]), fresh["id"]
        st, _, _ = b.post("/api/mappings/save", form_fields("fresh2", free_port(), id=fresh["id"]))
        assert st == 200 and fresh["id"] in [m["id"] for m in srv.saved_config()["mappings"]
                                             if m["name"] == "fresh2"]
        print("form: an unknown id is refused, a new mapping gets a server id, an edit keeps it  OK")

        req = urllib.request.Request(
            srv.base + "/api/cluster/mapping-save", method="POST",
            data=urllib.parse.urlencode(form_fields("peer", free_port(), id="../x")).encode(),
            headers={"X-Cluster-Key": KEY, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("cluster mapping-save accepted an unknown id")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
        assert len(srv.saved_config()["mappings"]) == count + 1
        print("cluster mapping-save: an unknown id is refused  OK")

        assert api(srv, "PUT", "/api/v1/mappings/..%5Caudit", {"name": "x"})[0] == 404
        assert api(srv, "GET", f"/api/v1/mappings/{ids[0]}")[0] == 200
        for mid in ids:
            st, _, _ = b.post(f"/api/mappings/{mid}", {}, method="DELETE")
            assert st == 200
        left = [m["id"] for m in srv.saved_config()["mappings"]]
        assert not set(ids) & set(left), left
        assert sorted(os.listdir(os.path.join(data, "traces"))) == [], "traces left behind"
        print("REST: PUT with an invalid id is 404; deleting the mappings removes their "
              "files  OK")


def main():
    test_validator()
    test_load_normalizes()
    test_path_helpers()
    test_import_warns()
    test_server()
    print("\nPASS: mapping ids are checked at every entry point")


if __name__ == "__main__":
    main()
