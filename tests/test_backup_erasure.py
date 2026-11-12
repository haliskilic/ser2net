"""Deleted accounts and the configuration backups (Settings -> Configuration backups).

Deleting a user leaves the account, with its password hash, in the backups made before
(and a restore with accounts would bring it back). Checks:
  * the backup list names each backup's accounts and roles (a config from before
    accounts: 'admin', its top-level password_hash);
  * an admin can delete one backup or all of them, and take an account that no longer
    exists out of every backup that has it: each is rewritten under its name with the
    rest unchanged, so a restore can't bring the account back; a legacy backup loses its
    password_hash; an account that exists now is refused; operators can't do any of it;
  * deleting a user says how many backups still hold it; each step is audited.

Run: python3 tests/test_backup_erasure.py
"""
import json
import os
import shutil
import tempfile

from _server import Browser, Server

from app.config import AppConfig, ConfigError, ConfigStore, User
from app.web.auth import hash_password

PW = "erasure-pass-1"


def _user(name, role="viewer", source="local"):
    return User(username=name, password_hash="" if source != "local" else "scrypt$x$y",
                role=role, pwd_version=1, source=source)


def test_store():
    d = tempfile.mkdtemp(prefix="s2n_erase_")
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        cfg = AppConfig(users=[_user("admin", "admin"), _user("bob")])
        store.save(cfg, validate=False)
        cfg.users.append(_user("carol@example.com", "operator", "oidc"))
        store.save(cfg, validate=False)       # backup 1: admin, bob
        cfg.users = [u for u in cfg.users if u.username != "bob"]
        store.save(cfg, validate=False)       # backup 2: admin, bob, carol
        # a config.json from before accounts existed, kept as a backup by the first save
        legacy = {"password_hash": "scrypt$legacy$hash", "pwd_version": 3,
                  "mappings": [{"id": "m1", "name": "PLC"}]}
        with open(os.path.join(store.backup_dir, "config-20200101T000000Z.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(legacy, fh)
        listed = store.backups()
        assert [b["users"] for b in listed] == [3, 2, 1], listed
        assert listed[0]["accounts"] == [
            {"username": "admin", "role": "admin", "source": "local"},
            {"username": "bob", "role": "viewer", "source": "local"},
            {"username": "carol@example.com", "role": "operator", "source": "oidc"}], listed[0]
        assert listed[2]["accounts"] == [{"username": "admin", "role": "admin", "source": "local"}]

        names = store.backup_names()
        before = {n: open(os.path.join(store.backup_dir, n), encoding="utf-8").read() for n in names}
        assert store.remove_account("bob") == 2
        assert store.backup_names() == names, "backups keep their names"
        for n in names:
            doc = json.load(open(os.path.join(store.backup_dir, n), encoding="utf-8"))
            assert "bob" not in [u["username"] for u in doc.get("users", [])], n
            old = json.loads(before[n])
            if "users" in old:
                old["users"] = [u for u in old["users"] if u["username"] != "bob"]
            assert doc == old, ("only the account changed", n)
            restored = AppConfig.from_dict(doc)
            assert restored.get_user("bob") is None
        assert store.remove_account("bob") == 0, "nothing left to remove"
        assert store.remove_account("Bob") == 0, "the exact name only"
        assert not [f for f in os.listdir(store.backup_dir) if f.endswith(".tmp")]
        # the legacy one: its password_hash is the admin account
        assert store.remove_account("admin") == 3
        doc = json.load(open(os.path.join(store.backup_dir, names[-1]), encoding="utf-8"))
        assert "password_hash" not in doc and "pwd_version" not in doc and doc["mappings"], doc
        assert AppConfig.from_dict(doc).users == []

        for bad in ("../config.json", "config.json", "config-20990101T000000Z.json", ""):
            try:
                store.delete_backup(bad)
            except ConfigError:
                pass
            else:
                raise AssertionError(f"deleted {bad!r}")
        store.delete_backup(names[0])
        assert store.backup_names() == names[1:]
        assert store.delete_backups() == 2 and store.backup_names() == []
        assert os.path.isfile(store.path), "config.json itself stays"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("store: accounts per backup (legacy password_hash = admin); an account taken out "
          "of every backup, the rest and the names unchanged; one / all backups deleted  OK")


def _audit(srv) -> str:
    with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
        return fh.read()


def _backup_users(srv) -> list:
    folder = os.path.join(srv.data, "backups")
    out = []
    for n in sorted(os.listdir(folder)):
        with open(os.path.join(folder, n), encoding="utf-8") as fh:
            out.append([u["username"] for u in json.load(fh).get("users", [])])
    return out


def test_settings():
    users = [{"username": u, "password_hash": hash_password(PW), "role": r, "pwd_version": 1}
             for u, r in (("admin", "admin"), ("op", "operator"), ("bob", "viewer"))]
    with Server({"users": users}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        b.post("/settings/node-name", {"node_name": "line-1"})   # a backup with bob in it
        b.post("/settings/node-name", {"node_name": "line-2"})
        _, _, page = b.get("/settings")
        assert "<summary>Accounts</summary>" in page and "bob (viewer)" in page, "accounts"
        assert 'data-confirm="Delete the account bob?"' in page
        assert 'name="username"><option value="bob">' not in page, "bob exists: not offered"
        st, _, page = b.post("/settings/users/delete", {"username": "bob"})
        held = sum("bob" in u for u in _backup_users(srv))
        assert held >= 2, _backup_users(srv)
        assert f"{held} configuration backups still hold it" in page, page[:600]
        _, _, page = b.get("/settings")
        assert '<option value="bob">bob</option>' in page, "bob offered for removal"
        st, _, page = b.post("/settings/backups/remove-account", {"username": "admin"})
        assert "&#39;admin&#39; is an account now: delete it under Users first." in page
        op = Browser(srv.base)
        op.login("op", PW)
        for path, form in (("/settings/backups/remove-account", {"username": "bob"}),
                           ("/settings/backups/delete", {"all": "on"})):
            st, _, _ = op.post(path, form)
            assert st == 403, (path, st)
        st, _, page = b.post("/settings/backups/remove-account", {"username": "bob"})
        changed = int(page.split("&#39;bob&#39; was removed from ")[1].split(" ")[0])
        assert changed >= held and not any("bob" in u for u in _backup_users(srv)), page[:600]
        # a restore with accounts can't bring bob back now
        name = sorted(os.listdir(os.path.join(srv.data, "backups")))[0]
        b.post("/settings/backups/restore", {"name": name, "accounts": "on"})
        assert "bob" not in [u["username"] for u in srv.saved_config()["users"]]
        st, _, page = b.post("/settings/backups/remove-account", {"username": "bob"})
        assert "No backup holds an account named &#39;bob&#39;." in page
        # delete one, then all
        names = sorted(os.listdir(os.path.join(srv.data, "backups")))
        st, _, page = b.post("/settings/backups/delete", {"name": names[-1]})
        assert "deleted." in page and names[-1] not in os.listdir(os.path.join(srv.data, "backups"))
        st, _, page = b.post("/settings/backups/delete", {"name": "../config.json"})
        assert "No such backup." in page and os.path.isfile(srv.config_path)
        st, _, page = b.post("/settings/backups/delete", {"all": "on"})
        assert "configuration backups deleted." in page, page[:600]
        assert os.listdir(os.path.join(srv.data, "backups")) == []
        audit = _audit(srv)
        assert f"\tconfig_backup_account_remove\tbob: {changed} backups\tadmin" in audit, audit
        assert f"\tconfig_backup_delete\t{names[-1]}\tadmin" in audit, audit
        assert "\tconfig_backups_delete_all\t" in audit, audit
    print("Settings: accounts per backup; deleting a user says how many backups hold it; "
          "removed from every backup (a restore can't bring it back); one / all backups "
          "deleted; admin only, audited  OK")


def main():
    test_store()
    test_settings()
    print("\nPASS: deleted accounts and the configuration backups")


if __name__ == "__main__":
    main()
