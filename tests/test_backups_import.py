"""Configuration backups and the import preview (merge / replace).

Before v2.8 "Import" replaced every mapping at once and config.json was never backed
up, so a wrong import or edit could not be undone. Checks:
  * every save keeps the config.json it writes in data/backups, and the one it
    replaces when that isn't kept yet (no duplicates, newest 50 kept; names that aren't
    ours are rejected);
  * the import form first previews (nothing changes), then merges — same name =
    update keeping the id, new = add, others untouched — or replaces;
  * a 2.7-style one-step POST still replaces everything (scripts keep working);
  * a classic ser2net.yaml goes through the same preview (named as such) and merge; a
    file with nothing to import is refused instead of emptying the mappings;
  * starting and stopping mappings takes no backup of its own (the newest one is
    updated), a save that changes nothing writes nothing, a new backup sorts first also
    when the clock went back, and a backup that can't be written is logged and said so;
  * an admin can download (without the session key) and restore a backup; the restore
    is undoable, keeps the session working, and a backup without users is refused;
  * a restore first shows what changes for sign-in and access. By default it brings back
    mappings and settings but keeps the user accounts, passwords, REST API token and
    cluster key (a deleted user, an old password or a revoked token stay gone, and no
    session ends); restoring the accounts too is a choice, which then also signs
    everyone else out; "Only its mappings" goes through the import preview;
  * the cluster key still accepted during a key change (previous_key/previous_until) is
    kept or restored with the key; the legacy authentication and auto-discovery switches
    come from the backup like any other setting;
  * viewers/operators can't reach backups.

Run: python3 tests/test_backups_import.py
"""
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app.config import BACKUPS_KEPT, AppConfig, ConfigError, ConfigStore, MappingConfig
from app.importer import apply_import, parse_mappings_file, plan_rows
from app.web.auth import hash_password, verify_password

PW = "backup-pass-1"


def mapping(mid, name, port, **net):
    return {"id": mid, "name": name, "enabled": False, "kind": "net",
            "serial": {"port": f"/dev/ser2net-test-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": port, **net}}


def test_store_backups():
    d = tempfile.mkdtemp(prefix="s2n_bk_")
    store = ConfigStore(os.path.join(d, "config.json"))
    cfg = AppConfig()

    def newest_is_current():
        with open(store.path, encoding="utf-8") as fh:
            return store.read_backup(store.backup_names()[0]) == fh.read()

    store.save(cfg)
    assert len(store.backup_names()) == 1 and newest_is_current(), \
        "the first save keeps what it wrote"
    cfg.session_timeout_s = 3600
    store.save(cfg)
    store.save(cfg)  # same content again: no new file
    names = store.backup_names()
    assert len(names) == 2 and newest_is_current(), names
    assert json.loads(store.read_backup(names[1]))["session_timeout_s"] == 28800
    for i in range(BACKUPS_KEPT + 5):
        cfg.session_timeout_s = 600 + i
        store.save(cfg)
    names = store.backup_names()
    assert len(names) == BACKUPS_KEPT, len(names)
    assert newest_is_current(), "the newest backup is what config.json holds"
    before = json.loads(store.read_backup(names[1]))["session_timeout_s"]
    assert before == 600 + BACKUPS_KEPT + 3, before  # the version before the last save
    # config.json edited by hand (or by an older release): kept before it is replaced
    with open(store.path, "r+", encoding="utf-8") as fh:
        doc = json.load(fh)
        doc["session_timeout_s"] = 7200
        fh.seek(0)
        fh.truncate()
        json.dump(doc, fh)
    cfg.session_timeout_s = 900
    store.save(cfg)
    names = store.backup_names()
    assert json.loads(store.read_backup(names[1]))["session_timeout_s"] == 7200, names
    assert newest_is_current()
    for bad in ("../config.json", "config.json", "config-20260101T000000Z.json.x", ""):
        try:
            store.read_backup(bad)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"read_backup accepted {bad!r}")
    info = store.backups()[0]
    assert info["mappings"] == 0 and info["users"] == 0 and len(info["time"]) == 19, info
    assert info["current"] is True and store.backups()[1]["current"] is False
    print(f"store: each save keeps what it wrote (and the file it replaces if not kept yet), "
          f"no duplicates, newest {BACKUPS_KEPT} kept  OK")


def test_store_start_stop_and_clock():
    """A start / stop takes no backup of its own; a save that changes nothing writes
    nothing; a new backup is the newest also when the clock went back; a backup that can't
    be written is reported."""
    import app.config as config_module

    d = tempfile.mkdtemp(prefix="s2n_bk2_")
    real_now = config_module.now
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        logs = []
        store.log = logs.append
        cfg = AppConfig.from_dict({"mappings": [mapping("aaaaaaaaaaa1", "A", 5001)]})

        def text(name):
            return store.read_backup(name)

        def newest_is_current():
            with open(store.path, encoding="utf-8") as fh:
                return text(store.backup_names()[0]) == fh.read()

        store.save(cfg)
        cfg.session_timeout_s = 3600
        store.save(cfg)  # a real change
        before_change = store.backup_names()[1]
        m = cfg.mappings[0]
        for _ in range(BACKUPS_KEPT + 5):  # many starts and stops
            m.enabled = not m.enabled
            assert store.save(cfg, backup=False) is True
        names = store.backup_names()
        assert len(names) == 2 and names[1] == before_change and newest_is_current(), names
        assert json.loads(text(names[0]))["mappings"][0]["enabled"] is m.enabled
        written = os.stat(store.path).st_mtime_ns
        assert store.save(cfg) is True and os.stat(store.path).st_mtime_ns == written, \
            "a save that changes nothing must not write config.json"
        with open(store.path, "r+", encoding="utf-8") as fh:  # edited by hand meanwhile
            doc = json.load(fh)
            doc["session_timeout_s"] = 7200
            fh.seek(0)
            fh.truncate()
            json.dump(doc, fh)
        m.enabled = not m.enabled
        store.save(cfg, backup=False)  # ... is kept, start / stop or not
        names = store.backup_names()
        assert len(names) == 4 and json.loads(text(names[1]))["session_timeout_s"] == 7200
        assert newest_is_current()
        print("store: starts and stops take no backup of their own (a hand edit is still "
              "kept); a save that changes nothing writes nothing  OK")

        for i in range(BACKUPS_KEPT):  # the newest 50 backups, from the clock's time now
            cfg.session_timeout_s = 600 + i
            store.save(cfg)
        prev = top = store.backup_names()[0]
        config_module.now = lambda: 1230768000.0  # 2009-01-01: a CMOS battery gone flat
        for i in range(3):
            cfg.session_timeout_s = 1200 + i
            store.save(cfg)
            names = store.backup_names()
            assert len(names) == BACKUPS_KEPT and newest_is_current(), names[:3]
            assert names[1] == prev and names[0].startswith(top[:len("config-20260101T000000Z")])
            prev = names[0]
        assert json.loads(text(names[1]))["session_timeout_s"] == 1201
        assert not any(n.startswith("config-2009") for n in names)
        clock = [line for line in logs if "the system clock" in line]
        assert len(clock) == 1 and "(2009-01-01 00:00:00 UTC) is behind" in clock[0], logs
        print("store: with the clock behind the backups, a new one is still the newest (named "
              "after it), not pruned at once; said once in the log  OK")
        config_module.now = real_now

        future = os.path.join(store.backup_dir, "config-20990101T000000Z.json")
        with open(store.path, encoding="utf-8") as fh, open(future, "w", encoding="utf-8") as out:
            out.write(fh.read())
        info = store.backups()
        assert info[0]["name"] == "config-20990101T000000Z.json" and info[0]["file_time"], info[0]
        assert not info[1]["file_time"], info[1]
        print("store: a backup named after a later time than it was saved shows its file time  OK")

        with open(store.path, "a", encoding="utf-8") as fh:
            fh.write("\n")  # not the newest backup any more (a hand edit)
        real_mkstemp = config_module.tempfile.mkstemp

        def mkstemp(*a, **k):  # backups/ is full, config.json's folder is not
            if k.get("prefix") == ".bak.":
                raise OSError(28, "No space left on device")
            return real_mkstemp(*a, **k)

        config_module.tempfile.mkstemp = mkstemp
        try:
            cfg.session_timeout_s = 4000
            assert store.save(cfg) is False, "a backup that failed was reported as kept"
        finally:
            config_module.tempfile.mkstemp = real_mkstemp
        assert json.loads(open(store.path, encoding="utf-8").read())["session_timeout_s"] == 4000
        assert any(line.startswith("WARNING: could not keep a backup of config.json")
                   for line in logs), logs
        print("store: a backup that can't be written is logged and reported; the save is "
              "done  OK")
    finally:
        config_module.now = real_now
        shutil.rmtree(d, ignore_errors=True)


def test_planner():
    cur = [MappingConfig.from_dict(mapping("aaaaaaaaaaa1", "A", 5001)),
           MappingConfig.from_dict(mapping("aaaaaaaaaaa2", "B", 5002))]
    incoming = [MappingConfig.from_dict(mapping("ffffffffff01", "A", 6001)),   # same name
                MappingConfig.from_dict(mapping("aaaaaaaaaaa2", "B", 5002)),   # identical
                MappingConfig.from_dict(mapping("ffffffffff03", "C", 5003))]   # new
    rows = {r["name"]: r["action"] for r in plan_rows(cur, incoming, "merge")}
    assert rows == {"A": "update", "B": "same", "C": "add"}, rows
    merged, touched = apply_import(cur, incoming, "merge")
    by = {m.name: m for m in merged}
    assert [m.name for m in merged] == ["A", "B", "C"]
    assert by["A"].id == "aaaaaaaaaaa1" and by["A"].network.port == 6001, "update keeps the id"
    assert sorted(m.name for m in touched) == ["A", "C"], "unchanged B is not restarted"
    assert cur[0].network.port == 5001, "apply_import must not modify its input"
    replaced, _ = apply_import(cur, incoming, "replace")
    assert [m.name for m in replaced] == ["A", "B", "C"]
    rows = [r["action"] for r in plan_rows(cur, incoming, "replace")]
    assert rows.count("remove") == 2 and rows.count("add") == 3, rows
    plan = parse_mappings_file(json.dumps({"users": [], "mappings": [mapping("x" * 12, "X", 1)]}).encode())
    assert plan.source == "ser2net config.json" and plan.warnings
    for raw in (b"\xff\xfe", b"not json", b'{"no": "mappings"}', b'{"mappings": [1]}'):
        try:
            parse_mappings_file(raw)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"accepted {raw!r}")
    print("planner: merge by id/name keeps ids, skips identical; replace lists removals  OK")


def test_planner_keeps_absent_settings():
    """A merge import of an export written by an older version (2.7.1's, which has no
    "writable", trace rotation, Sparkplug ...) keeps the current values of the settings
    the file doesn't have, a Modbus point's by its name; replace takes the defaults."""
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "config-2.7.1.json"),
              encoding="utf-8") as fh:
        old = next(m for m in json.load(fh)["mappings"] if m["name"] == "Modbus gateway")
    now = MappingConfig.from_dict(old)
    now.options.trace_max_mb, now.mqtt.payload_format = 2.5, "sparkplug_b"
    for p in now.modbus_poll.points:
        p.writable = p.fn in (1, 3)
    assert any(p.writable for p in now.modbus_poll.points)
    plan = parse_mappings_file(json.dumps({"mappings": [old]}).encode())
    assert {"options.trace_max_mb", "modbus_poll.points[0].writable"} <= set(plan.absent[0])
    note = next(w for w in plan.warnings if "the file has no" in w)
    assert "options.trace_max_mb" in note and "mqtt.payload_format" in note, note
    assert [r["action"] for r in plan_rows([now], plan.mappings, "merge", plan.absent)] == ["same"]
    merged, touched = apply_import([now], plan.mappings, "merge", plan.absent)
    assert merged[0].to_dict() == now.to_dict() and not touched, "nothing the file has changed"
    # what the file has still wins
    old2 = json.loads(json.dumps(old))
    old2["modbus_poll"]["points"][0]["address"] += 1
    old2["modbus_poll"]["points"][1]["fn"] = 4   # an input register now: can't be written
    plan = parse_mappings_file(json.dumps({"mappings": [old2]}).encode())
    merged, touched = apply_import([now], plan.mappings, "merge", plan.absent)
    got = merged[0].modbus_poll.points
    assert got[0].address == now.modbus_poll.points[0].address + 1 and touched
    assert got[0].writable == now.modbus_poll.points[0].writable and not got[1].writable
    assert merged[0].options.trace_max_mb == 2.5 and merged[0].mqtt.payload_format == "sparkplug_b"
    replaced, _ = apply_import([now], plan.mappings, "replace", plan.absent)
    assert replaced[0].options.trace_max_mb == 10.0 and not any(
        p.writable for p in replaced[0].modbus_poll.points), "replace: the defaults"
    print("planner: a merge keeps the settings an older export doesn't have (points by name); "
          "the preview names them  OK")


def main():
    test_store_backups()
    test_store_start_stop_and_clock()
    test_planner()
    test_planner_keeps_absent_settings()
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "op", "password_hash": hash_password(PW), "role": "operator", "pwd_version": 1}]
    p1, p2, p3 = free_port(), free_port(), free_port()
    config = {"users": users, "mappings": [mapping("aaaaaaaaaaa1", "A", p1),
                                           mapping("aaaaaaaaaaa2", "B", p2)]}
    with Server(config) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        export = json.dumps({"version": 1, "mappings": [
            mapping("ffffffffff01", "A", p3), mapping("ffffffffff03", "C", free_port())]}).encode()

        st, _, page = admin.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                                 files={"file": ("m.json", export)})
        assert st == 200 and "Import preview" in page, page[:300]
        assert [m["name"] for m in srv.saved_config()["mappings"]] == ["A", "B"], "preview changed config"
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        op = Browser(srv.base)
        op.login("op", PW)
        st, _, page = op.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "expired" in page, "another user must not apply someone else's preview"
        st, _, page = admin.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "Imported: 1 added, 1 updated, 0 unchanged" in page, page[page.find("banner"):][:300]
        maps = {m["name"]: m for m in srv.saved_config()["mappings"]}
        assert sorted(maps) == ["A", "B", "C"] and maps["A"]["id"] == "aaaaaaaaaaa1"
        assert maps["A"]["network"]["port"] == p3
        st, _, page = admin.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "expired" in page, "a preview applies once"
        print("UI import: preview changes nothing; merge updates A (same id), adds C, keeps B  OK")

        st, _, page = admin.post("/settings/config/import", {}, files={"file": ("m.json", export)})
        assert "Imported 2 mappings (replaced the existing ones)" in page, page[:300]
        assert sorted(m["name"] for m in srv.saved_config()["mappings"]) == ["A", "C"]
        print("legacy one-step import (no preview step) still replaces everything  OK")

        port = free_port()
        classic = ("connection: &plc\n"
                   f"  accepter: telnet(rfc2217),tcp,127.0.0.1,{port}\n"
                   "  connector: serialdev,/dev/ser2net-test-missing,19200n81,local\n").encode()
        st, _, page = admin.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                                 files={"file": ("ser2net.yaml", classic)})
        assert st == 200 and "from a ser2net.yaml (ser2net 4.x)" in page, page[page.find("banner"):][:300]
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        st, _, page = admin.post("/settings/config/import/apply", {"token": token, "mode": "merge"})
        assert "Imported: 1 added" in page, page[page.find("banner"):][:300]
        plc = next(m for m in srv.saved_config()["mappings"] if m["name"] == "plc")
        assert plc["serial"]["port"] == "/dev/ser2net-test-missing" and plc["serial"]["baudrate"] == 19200
        assert plc["network"]["protocol"] == "rfc2217" and plc["network"]["port"] == port
        st, _, page = admin.post("/settings/config/import", {"step": "preview", "mode": "replace"},
                                 files={"file": ("ser2net.conf", b"# only comments\n")})
        assert "is not a ser2net configuration" in page and "Import preview" not in page
        assert "plc" in [m["name"] for m in srv.saved_config()["mappings"]], "a refused import changes nothing"
        print("UI import of a classic ser2net.yaml: the preview names the format, merge adds it; "
              "a file with nothing to import is refused  OK")

        st, _, page = admin.get("/settings")
        backups = ConfigStore(srv.config_path).backups()
        assert len(backups) >= 3 and backups[0]["name"] in page
        target = next(b for b in backups if b["mappings"] == 2 and "B" in
                      json.dumps(json.loads(ConfigStore(srv.config_path).read_backup(b["name"]))["mappings"]))
        st, h, body = admin.get(f"/settings/backups/{target['name']}")
        assert st == 200 and "attachment" in h.get("Content-Disposition", "") and '"mappings"' in body
        downloaded = json.loads(body)
        assert "secret_key" not in downloaded and downloaded["users"], "a download leaves out the session key"
        assert op.get(f"/settings/backups/{target['name']}")[0] == 403
        assert admin.get("/settings/backups/..%2Fconfig.json")[0] == 404
        assert op.post("/settings/backups/restore", {"name": target["name"]})[0] == 403
        st, _, page = admin.post("/settings/backups/restore", {"name": target["name"]})
        assert "Restored" in page, page[page.find("banner"):][:300]
        assert sorted(m["name"] for m in srv.saved_config()["mappings"]) == ["A", "B"]
        assert admin.get("/settings")[0] == 200, "the admin's session must survive the restore"
        assert any(b["mappings"] == 2 and b["name"] != target["name"]
                   for b in ConfigStore(srv.config_path).backups()), "restore must be undoable"
        print("backups: listed, download/restore admin-only, restore undoable, session kept  OK")

        store = ConfigStore(srv.config_path)
        empty = os.path.join(store.backup_dir, "config-20000101T000000Z.json")
        with open(empty, "w") as fh:
            json.dump(AppConfig().to_dict(), fh)
        st, _, page = admin.post("/settings/backups/restore", {"name": "config-20000101T000000Z.json",
                                                               "accounts": "on"})
        assert "has no user accounts" in page and srv.saved_config()["users"], "pre-setup backup restored"
        print("restoring the accounts of a backup without any is refused (would reopen setup)  OK")
    test_restore_accounts()
    test_start_stop_backups()
    test_cluster_setting_changes()
    test_restore_cluster_keys()
    print("\nPASS: configuration backups + import preview / merge / replace")


def test_start_stop_backups():
    """Starting and stopping mappings (REST API, bulk) takes no backup of its own, so the
    backups of real changes stay; an import whose backup can't be written says so."""
    from app.web.auth import hash_token

    token = "s2n_backup-start-stop"
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    a, b = "aaaaaaaaaaa1", "aaaaaaaaaaa2"
    config = {"users": users, "api_token_hash": hash_token(token), "api_token_role": "admin",
              "mappings": [mapping(a, "A", free_port()), mapping(b, "B", free_port())]}

    def call(method, path, body=None):
        req = urllib.request.Request(
            srv.base + "/api/v1" + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    with Server(config) as srv:
        store = ConfigStore(srv.config_path)
        view = call("GET", f"/mappings/{b}")
        call("PUT", f"/mappings/{b}", dict(view, name="B2"))  # a real change
        names = store.backup_names()
        before_change = names[1]
        for _ in range(25):
            call("POST", f"/mappings/{a}/start")
            call("POST", f"/mappings/{a}/stop")
        admin = Browser(srv.base)
        admin.login("admin", PW)
        for action in ("start", "stop", "start", "stop"):
            st, _, _ = admin.post("/api/mappings/bulk", {"action": action, "ids": [a, b]},
                                  htmx=True)
            assert st == 200, st
        after = store.backup_names()
        assert len(after) == len(names) and before_change in after, (names, after)
        with open(srv.config_path, encoding="utf-8") as fh:
            assert store.read_backup(after[0]) == fh.read(), "the newest backup is config.json"
        print("UI/REST: 25 start/stop cycles and bulk actions take no backup of their own; the "
              "backup from before a real change stays  OK")

        with open(srv.config_path, "a", encoding="utf-8") as fh:
            fh.write("\n")  # edited by hand: not the newest backup any more
        away = store.backup_dir + ".away"
        os.rename(store.backup_dir, away)
        open(store.backup_dir, "w").close()  # backups/ can't be written
        try:
            export = json.dumps({"version": 1, "mappings": [mapping("ffffffffff07", "C",
                                                                    free_port())]}).encode()
            st, _, page = admin.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                                     files={"file": ("m.json", export)})
            token_ = page.split('name="token" value="', 1)[1].split('"', 1)[0]
            st, _, page = admin.post("/settings/config/import/apply", {"token": token_,
                                                                       "mode": "merge"})
            assert "Imported: 1 added" in page and \
                "The previous configuration could not be kept as a backup (see all.log)." in page, \
                page[page.find("banner"):][:400]
        finally:
            os.remove(store.backup_dir)
            os.rename(away, store.backup_dir)
        with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
            assert "WARNING: could not keep a backup of config.json" in fh.read()
        print("UI import: a backup that can't be written is said so (and logged)  OK")


def api_status(base, token):
    req = urllib.request.Request(base + "/api/v1/mappings", headers={"Authorization": f"Bearer {token}"})
    try:
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


def account(cfg, name):
    return next((u for u in cfg["users"] if u["username"] == name), None)


def test_restore_accounts():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "op", "password_hash": hash_password(PW), "role": "operator", "pwd_version": 1}]
    config = {"users": users, "mappings": [mapping("aaaaaaaaaaa1", "A", free_port()),
                                           mapping("aaaaaaaaaaa2", "B", free_port())]}
    with Server(config) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        disc = str(free_port())
        assert admin.post("/settings/users", {"username": "gone", "role": "viewer",
                                              "password": PW, "password2": PW})[0] == 200
        st, _, page = admin.post("/settings/api-token", {"api_token_role": "operator"})
        token = page.split('token-box">', 1)[1].split("<", 1)[0].strip()
        assert admin.post("/settings/cluster", {"enabled": "on", "key": "old-cluster-key",
                                                "discovery_port": disc})[0] == 200
        # what config.json holds now is backed up by the next change: then revoke it all
        assert admin.post("/settings/users/gone/delete")[0] == 200
        assert admin.post("/settings/api-token/revoke")[0] == 200
        new_op = "op-new-pass-2"
        assert admin.post("/settings/users/password", {"username": "op", "password": new_op,
                                                       "password2": new_op})[0] == 200
        assert admin.post("/settings/cluster", {"enabled": "on", "key": "new-cluster-key",
                                                "discovery_port": disc})[0] == 200
        assert admin.post("/settings/access", {"anonymous_role": "viewer"})[0] == 200  # login off
        store = ConfigStore(srv.config_path)
        name = next(b["name"] for b in store.backups()
                    if account(json.loads(store.read_backup(b["name"])), "gone")
                    and json.loads(store.read_backup(b["name"]))["cluster"]["key"] == "old-cluster-key")

        st, _, page = admin.get(f"/settings?restore={name}")
        assert st == 200 and "Restore the configuration saved at" in page, page[:300]
        for line in ("Login required: disabled → enabled", "Accounts it brings back: gone",
                     "Accounts with another password or role: op",
                     "The REST API token as it was then", "The cluster key as it was then"):
            assert line in page, line
        assert 'formaction="/settings/backups/restore-mappings"' in page
        assert srv.saved_config()["users"][1]["username"] == "op", "a preview changes nothing"
        print("restore preview: access setting changes, and what restoring the accounts would do  OK")

        op = Browser(srv.base)
        op.login("op", new_op)
        st, _, page = admin.post("/settings/backups/restore", {"name": name})
        assert "User accounts, the REST API token and the cluster key were kept" in page, \
            page[page.find("banner"):][:400]
        assert "Login required: disabled → enabled" in page
        cfg = srv.saved_config()
        assert [u["username"] for u in cfg["users"]] == ["admin", "op"], "a deleted user came back"
        assert verify_password(new_op, account(cfg, "op")["password_hash"]), "an old password came back"
        assert cfg["api_token_hash"] == "" and api_status(srv.base, token) == 401, "a revoked token came back"
        assert cfg["cluster"]["key"] == "new-cluster-key" and cfg["admin_ui"]["require_login"] is True
        assert op.get("/settings")[0] == 200, "no session ends when the accounts stay"
        audit = open(os.path.join(srv.data, "audit.log"), encoding="utf-8").read()
        assert f"{name}; accounts kept; Login required: disabled → enabled" in audit, audit[-500:]
        print("restore: settings back; deleted user, old password, revoked token, key stay gone  OK")

        st, _, page = admin.post("/settings/backups/restore", {"name": name, "accounts": "on"})
        assert "Everyone else was signed out" in page, page[page.find("banner"):][:400]
        cfg = srv.saved_config()
        assert account(cfg, "gone") and verify_password(PW, account(cfg, "op")["password_hash"])
        assert cfg["cluster"]["key"] == "old-cluster-key" and api_status(srv.base, token) == 200
        assert op.get("/settings")[0] == 303, "restoring the accounts must end the other sessions"
        assert admin.get("/settings")[0] == 200, "the admin who restored stays signed in"
        print("restore with its accounts (chosen): they come back, everyone else is signed out  OK")

        empty = json.loads(store.read_backup(name))
        empty["users"] = [dict(u, role="viewer") for u in empty["users"]]
        with open(os.path.join(store.backup_dir, "config-20000102T000000Z.json"), "w") as fh:
            json.dump(empty, fh)
        st, _, page = admin.post("/settings/backups/restore", {"name": "config-20000102T000000Z.json",
                                                               "accounts": "on"})
        assert "has no admin account" in page and account(srv.saved_config(), "admin")["role"] == "admin"
        print("restoring accounts from a backup without an admin is refused  OK")

        export = json.dumps({"version": 1, "mappings": [mapping("aaaaaaaaaaa1", "A", free_port())]})
        st, _, page = admin.post("/settings/config/import", {}, files={"file": ("m.json", export.encode())})
        assert [m["name"] for m in srv.saved_config()["mappings"]] == ["A"]
        st, _, page = admin.post("/settings/backups/restore-mappings", {"name": name})
        assert st == 200 and "Import preview" in page and "configuration backup (" in page, page[:300]
        token = page.split('name="token" value="', 1)[1].split('"', 1)[0]
        st, _, page = admin.post("/settings/config/import/apply", {"token": token, "mode": "replace"})
        assert "Imported 2 mappings" in page, page[page.find("banner"):][:300]
        cfg = srv.saved_config()
        assert [m["name"] for m in cfg["mappings"]] == ["A", "B"] and account(cfg, "gone")
        print("Only its mappings: through the import preview; accounts and settings untouched  OK")



def audit_detail(srv, action):
    """The detail of the newest audit record with this action."""
    with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
        rows = [ln.split("\t") for ln in fh.read().split("\n") if ln]
    return next(r[3] for r in reversed(rows) if r[2] == action)


def test_cluster_setting_changes():
    """What a restore without the accounts takes from the backup's cluster settings is
    listed (preview, banner, audit log) like the sign-in settings."""
    from app.web.routes.settings import _setting_changes

    old = AppConfig()
    new = AppConfig.from_dict({"cluster": {
        "enabled": True, "legacy_auth": False, "discovery": False, "discovery_port": 41751,
        "advertise_ip": "10.0.0.5", "peers": ["10.0.0.6:8080", "10.0.0.7:8080"], "key": "k"}})
    assert _setting_changes(old, new) == [
        "LAN cluster: disabled → enabled",
        "LAN cluster · Legacy authentication: enabled → disabled",
        "LAN cluster · Automatic discovery: enabled → disabled",
        "LAN cluster · Discovery UDP port: 41750 → 41751",
        "LAN cluster · Advertise IP: — → 10.0.0.5",
        "LAN cluster · Manual peers: — → 10.0.0.6:8080, 10.0.0.7:8080"], _setting_changes(old, new)
    same_but_key = AppConfig.from_dict({"cluster": {"key": "another"}})
    assert _setting_changes(old, same_but_key) == [], "the key is an account change"
    print("restore changes: the cluster's switch, legacy authentication, discovery, port, "
          "advertised IP and peers are listed  OK")


def test_restore_cluster_keys():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1}]
    with Server({"users": users, "mappings": []}) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)
        disc = str(free_port())

        def cluster(key, **switches):
            form = {"enabled": "on", "key": key, "discovery_port": disc, "keep_old_key": "on",
                    "cluster_form": "2", **switches}
            assert admin.post("/settings/cluster", form)[0] == 200
            return srv.saved_config()["cluster"]

        cluster("cluster-key-one-0123456789", legacy_auth="on", discovery="on")
        then = cluster("cluster-key-two-0123456789", discovery="on")  # legacy off
        assert then["previous_key"] == "cluster-key-one-0123456789" and then["previous_until"] > 0
        now = cluster("cluster-key-three-0123456789", legacy_auth="on")  # discovery off
        assert now["previous_key"] == "cluster-key-two-0123456789"
        store = ConfigStore(srv.config_path)
        name = next(b["name"] for b in store.backups()
                    if json.loads(store.read_backup(b["name"]))["cluster"] == then)

        st, _, page = admin.post("/settings/backups/restore", {"name": name})
        assert "the cluster key were kept" in page, page[page.find("banner"):][:400]
        c = srv.saved_config()["cluster"]
        assert (c["key"], c["previous_key"], c["previous_until"]) == \
            (now["key"], now["previous_key"], now["previous_until"]), c
        assert (c["legacy_auth"], c["discovery"]) == (False, True), c
        for line in ("LAN cluster · Legacy authentication: enabled → disabled",
                     "LAN cluster · Automatic discovery: disabled → enabled"):
            assert line in page, page[page.find("banner"):][:600]
            assert line in audit_detail(srv, "config_backup_restore"), line
        print("restore: the current and the still-accepted cluster key stay; switches restored "
              "and listed (banner, audit log)  OK")

        st, _, page = admin.post("/settings/backups/restore", {"name": name, "accounts": "on"})
        assert "Everyone else was signed out" in page, page[page.find("banner"):][:400]
        c = srv.saved_config()["cluster"]
        assert (c["key"], c["previous_key"], c["previous_until"]) == \
            (then["key"], then["previous_key"], then["previous_until"]), c
        print("restore with its accounts: both cluster keys come from the backup  OK")

        # same key, but the backup still accepts the previous one: the preview says so
        assert cluster("", clear_previous_key="on", legacy_auth="on")["previous_key"] == ""
        st, _, page = admin.get(f"/settings?restore={name}")
        assert st == 200 and "The cluster key as it was then" in page, page[:300]
        print("restore preview: a previous cluster key the backup accepts counts as a key change  OK")


if __name__ == "__main__":
    main()
