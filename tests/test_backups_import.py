"""Configuration backups and the import preview (merge / replace).

Before v2.8 "Import" replaced every mapping at once and config.json was never backed
up, so a wrong import or edit could not be undone. Checks:
  * every change keeps the replaced config.json in data/backups (no duplicates,
    newest 50 kept; names that aren't ours are rejected);
  * the import form first previews (nothing changes), then merges — same name =
    update keeping the id, new = add, others untouched — or replaces;
  * a 2.7-style one-step POST still replaces everything (scripts keep working);
  * a classic ser2net.yaml goes through the same preview (named as such) and merge; a
    file with nothing to import is refused instead of emptying the mappings;
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
    store.save(cfg)
    assert store.backup_names() == [], "the first save has nothing to back up"
    cfg.session_timeout_s = 3600
    store.save(cfg)
    store.save(cfg)  # same content again: no new file
    names = store.backup_names()
    assert len(names) == 1, names
    assert json.loads(store.read_backup(names[0]))["session_timeout_s"] == 28800
    for i in range(BACKUPS_KEPT + 5):
        cfg.session_timeout_s = 600 + i
        store.save(cfg)
    names = store.backup_names()
    assert len(names) == BACKUPS_KEPT, len(names)
    newest = json.loads(store.read_backup(names[0]))["session_timeout_s"]
    assert newest == 600 + BACKUPS_KEPT + 3, newest  # the version before the last save
    for bad in ("../config.json", "config.json", "config-20260101T000000Z.json.x", ""):
        try:
            store.read_backup(bad)
        except ConfigError:
            pass
        else:
            raise AssertionError(f"read_backup accepted {bad!r}")
    info = store.backups()[0]
    assert info["mappings"] == 0 and info["users"] == 0 and len(info["time"]) == 19, info
    print(f"store: each change backs up the old file, no duplicates, newest {BACKUPS_KEPT} kept  OK")


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


def main():
    test_store_backups()
    test_planner()
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
    test_restore_cluster_keys()
    print("\nPASS: configuration backups + import preview / merge / replace")


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
        print("restore: the current and the still-accepted cluster key stay; switches restored  OK")

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
