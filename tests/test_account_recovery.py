"""Account recovery and the first admin without the web UI (app/accounts.py).

  * `ser2net --set-password NAME` on a stopped install changes config.json (backed up,
    audited): the new password works, the old sessions end; NAME is created as an admin
    when missing — on a fresh data directory, too (headless set-up); LDAP/OIDC accounts,
    bad names and short passwords are refused; nothing secret is printed;
  * on a running install the same command is handed to the running instance (no
    restart): the new password works at once and the old session is refused at once;
    `--sign-out-all` ends every session (new session key); the first admin of a fresh
    running install closes the setup page;
  * the request files: taken by one side only, withdrawn when nobody takes them, an
    old one is not applied;
  * SER2NET_ADMIN_PASSWORD_FILE (+ SER2NET_ADMIN_USER) creates the first admin at start
    and is ignored once accounts exist; problems are logged, the password never;
  * as root on a data directory owned by someone else, the command first becomes that
    user (checked with stand-ins for the os calls).

Run: python3 tests/test_account_recovery.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

from _server import ROOT, Browser, Server

from app import accounts
from app.config import AppConfig
from app.web.auth import hash_password, verify_password

PW = "recovery-pass-1"
NEW = "brand-new-pass-2"


def cli(data_dir, *args, password=None, env=None):
    p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", data_dir,
                        *args], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       input="" if password is None else password + "\n",
                       env=dict(os.environ, **(env or {})))
    out = p.stdout + p.stderr
    for secret in (PW, NEW):
        assert secret not in out, "a password was printed"
    return p.returncode, out.strip()


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def config_of(data_dir):
    return json.loads(read(os.path.join(data_dir, "config.json")))


def account(cfg, name):
    return next(u for u in cfg["users"] if u["username"] == name)


def test_apply():
    cfg = AppConfig.from_dict({"users": [
        {"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 7},
        {"username": "jdoe", "password_hash": "", "role": "admin", "pwd_version": 1, "source": "ldap"}]})
    h = hash_password(NEW)
    ch = accounts.apply(cfg, {"action": "set_password", "username": "admin", "password_hash": h})
    assert cfg.get_user("admin").pwd_version == 8 and cfg.get_user("admin").password_hash == h
    ch.undo()
    assert cfg.get_user("admin").pwd_version == 7 and verify_password(PW, cfg.get_user("admin").password_hash)
    ch = accounts.apply(cfg, {"action": "set_password", "username": "rescue", "password_hash": h})
    rescue = cfg.get_user("rescue")
    assert rescue.role == "admin" and rescue.source == "local" and ch.action == "user_create"
    ch.undo()
    assert cfg.get_user("rescue") is None
    for bad in ({"action": "set_password", "username": "jdoe", "password_hash": h},
                {"action": "set_password", "username": "JDoe", "password_hash": h},
                {"action": "set_password", "username": "ADMIN", "password_hash": h},
                {"action": "set_password", "username": "a/b", "password_hash": h},
                {"action": "set_password", "username": "admin", "password_hash": "plain text"},
                {"action": "drop_tables"}):
        try:
            accounts.apply(cfg, bad)
        except accounts.AccountError:
            pass
        else:
            raise AssertionError(f"applied {bad}")
    key = cfg.secret_key
    accounts.apply(cfg, {"action": "sign_out_all"})
    assert cfg.secret_key != key
    first = accounts.apply(AppConfig(), {"action": "set_password", "username": "boss", "password_hash": h})
    assert first.first_admin and first.action == "first_run_setup"
    try:
        accounts.apply(cfg, {"action": "set_password", "username": "Admin", "password_hash": h})
    except accounts.AccountError as e:
        assert "there is a local account 'admin' (another letter case)" in str(e), e
    assert [u.username for u in cfg.users] == ["admin", "jdoe"]
    print("apply: set (+1 version) / create admin / undo; LDAP account (any letter case), a "
          "name differing only in case, bad name or hash refused; new session key  OK")


def test_request_files():
    d = tempfile.mkdtemp(prefix="s2n_req_")
    try:
        rid = accounts.post_request(d, {"action": "sign_out_all"})
        assert accounts.wait_answer(d, rid, 0.3) is None, "nobody took it: withdrawn"
        assert accounts.take_requests(d) == [], "a withdrawn request must not be applied"
        rid = accounts.post_request(d, {"action": "sign_out_all"})
        (got, req), = accounts.take_requests(d)
        assert got == rid and req["action"] == "sign_out_all"
        assert accounts.take_requests(d) == [], "taken once"
        accounts.answer(d, rid, True, "done")
        assert accounts.wait_answer(d, rid, 1) == (True, "done")
        old = accounts.post_request(d, {"action": "sign_out_all"})
        path = os.path.join(d, accounts.REQUEST_DIR, old + ".req")
        doc = json.loads(read(path))
        with open(path, "w") as fh:
            json.dump(dict(doc, time=doc["time"] - accounts.REQUEST_MAX_AGE_S - 5), fh)
        assert accounts.take_requests(d) == [(old, None)], "an old request is answered, not applied"
        assert sorted(os.listdir(os.path.join(d, accounts.REQUEST_DIR))) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("request files: withdrawn when nobody takes them, taken once, answered; old ones not applied  OK")


def test_provision():
    env = {"SER2NET_ADMIN_PASSWORD_FILE": "", "SER2NET_ADMIN_USER": ""}
    assert accounts.provision_from_env(AppConfig(), env) == (None, None)
    d = tempfile.mkdtemp(prefix="s2n_env_")
    try:
        pwfile = os.path.join(d, "pw")
        with open(pwfile, "w") as fh:
            fh.write(PW + "\n")
        cfg = AppConfig()
        name, note = accounts.provision_from_env(cfg, {"SER2NET_ADMIN_PASSWORD_FILE": pwfile})
        assert name == "admin" and "created" in note and PW not in note
        assert verify_password(PW, cfg.get_user("admin").password_hash), "trailing newline is not part of it"
        assert accounts.provision_from_env(cfg, {"SER2NET_ADMIN_PASSWORD_FILE": pwfile})[0] is None
        for env, problem in (({"SER2NET_ADMIN_PASSWORD_FILE": pwfile + ".missing"}, "cannot read"),
                             ({"SER2NET_ADMIN_USER": "boss"}, "SER2NET_ADMIN_PASSWORD_FILE is not"),
                             ({"SER2NET_ADMIN_PASSWORD_FILE": pwfile, "SER2NET_ADMIN_USER": "a b"},
                              "SER2NET_ADMIN_USER")):
            cfg = AppConfig()
            name, note = accounts.provision_from_env(cfg, env)
            assert name is None and not cfg.users and note.startswith("ERROR") and problem in note, note
        with open(pwfile, "w") as fh:
            fh.write("short\n")
        name, note = accounts.provision_from_env(AppConfig(), {"SER2NET_ADMIN_PASSWORD_FILE": pwfile})
        assert name is None and "at least 8" in note, note
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("SER2NET_ADMIN_PASSWORD_FILE: first admin only, newline stripped, problems reported  OK")


def test_become_owner():
    calls = []
    fake = types.SimpleNamespace(
        geteuid=lambda: 0, stat=lambda p: types.SimpleNamespace(st_uid=10001, st_gid=10002),
        setgroups=lambda g: calls.append(("groups", g)), setgid=lambda g: calls.append(("gid", g)),
        setuid=lambda u: calls.append(("uid", u)))
    note = accounts.become_owner("/data", "linux", fake)
    assert calls == [("groups", []), ("gid", 10002), ("uid", 10001)], calls
    assert "uid 10001" in note
    calls.clear()
    fake.stat = lambda p: types.SimpleNamespace(st_uid=0, st_gid=0)
    assert accounts.become_owner("/data", "linux", fake) == "" and not calls, "root-owned: stays root"
    fake.geteuid = lambda: 1000
    assert accounts.become_owner("/data", "linux", fake) == "" and not calls, "not root: nothing to do"
    assert accounts.become_owner("C:\\data", "win32", None) == ""
    print("as root on another user's data directory: setgroups, setgid, setuid to its owner  OK")


def test_offline():
    work = tempfile.mkdtemp(prefix="s2n_cli_")
    try:
        fresh = os.path.join(work, "fresh")
        code, out = cli(fresh, "--set-password", "admin", password=PW)
        assert code == 0 and "local admin account 'admin' created" in out and "was created" in out, out
        cfg = config_of(fresh)
        assert verify_password(PW, account(cfg, "admin")["password_hash"])
        assert "first_run_setup\tadmin\t(command line)" in read(os.path.join(fresh, "audit.log"))
        print("fresh data directory: --set-password creates config.json with the admin  OK")

        d = os.path.join(work, "data")
        os.makedirs(d)
        with open(os.path.join(d, "config.json"), "w") as fh:
            json.dump({"users": [
                {"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 3},
                {"username": "jdoe", "password_hash": "", "role": "viewer", "pwd_version": 1,
                 "source": "ldap"}],
                # an unrelated invalid setting must not block a recovery
                "admin_ui": {"tls_cert": "/nonexistent/cert.pem", "tls_key": "/nonexistent/key.pem"},
                "secret_key": "k" * 64, "instance_id": "abcdef0123456789"}, fh)
        code, out = cli(d, "--set-password", "admin", password=NEW)
        assert code == 0 and "new password set for 'admin'" in out, out
        cfg = config_of(d)
        admin = account(cfg, "admin")
        assert verify_password(NEW, admin["password_hash"]) and admin["pwd_version"] == 4
        assert os.listdir(os.path.join(d, "backups")), "the previous config.json must be kept"
        assert "user_password_set\tadmin\t(command line)" in read(os.path.join(d, "audit.log"))
        for args, password, problem in ((("--set-password", "jdoe"), NEW, "LDAP account"),
                                        (("--set-password", "admin"), "short", "at least 8"),
                                        (("--set-password", "no/slash"), NEW, "Username must be")):
            code, out = cli(d, *args, password=password)
            assert code == 1 and problem in out, (args, out)
        code, out = cli(d, "--sign-out-all")
        assert code == 0 and config_of(d)["secret_key"] != "k" * 64, out
        print("stopped install: new password (+1 version, backup, audit), refusals, --sign-out-all  OK")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_running():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1}]
    with Server({"users": users}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        code, out = cli(srv.data, "--set-password", "admin", password=NEW)
        assert code == 0 and "done by the running ser2net" in out, out
        st, h, _ = b.get("/settings")
        assert st == 303 and h.get("Location") == "/login", "the old session must end at once"
        Browser(srv.base).login("admin", NEW)
        assert verify_password(NEW, account(srv.saved_config(), "admin")["password_hash"])
        assert "command line: new password set for 'admin'" in srv.output()
        b2 = Browser(srv.base)
        b2.login("admin", NEW)
        key = srv.saved_config()["secret_key"]
        code, out = cli(srv.data, "--sign-out-all")
        assert code == 0 and "every web UI session was signed out" in out, out
        assert b2.get("/settings")[0] == 303 and srv.saved_config()["secret_key"] != key
        audit = read(os.path.join(srv.data, "audit.log"))
        assert "user_password_set\tadmin\t(command line)" in audit and "sessions_revoke_all" in audit
        assert not [n for n in os.listdir(os.path.join(srv.data, accounts.REQUEST_DIR))
                    if not n.startswith(".")], "request files left behind"
    print("running install: the instance applies it at once (old session refused, new password "
          "works, audited), --sign-out-all too  OK")

    with Server({"users": []}) as srv:
        b = Browser(srv.base)
        assert b.get("/")[1].get("Location") == "/setup"
        code, out = cli(srv.data, "--set-password", "boss", password=PW)
        assert code == 0 and "local admin account 'boss' created" in out, out
        st, h, _ = b.get("/setup")
        assert st == 303, "setup must close once the first admin exists"
        Browser(srv.base).login("boss", PW)
    print("fresh running install: the first admin from the command line closes the setup page  OK")


def test_env_admin():
    d = tempfile.mkdtemp(prefix="s2n_envsrv_")
    try:
        pwfile = os.path.join(d, "secret")
        with open(pwfile, "w") as fh:
            fh.write(PW + "\n")
        env = {"SER2NET_ADMIN_PASSWORD_FILE": pwfile, "SER2NET_ADMIN_USER": "ops"}
        with Server(None, env=env) as srv:
            Browser(srv.base).login("ops", PW)
            assert Browser(srv.base).get("/setup")[0] == 303
            out = srv.output()
            assert "admin account 'ops' created from SER2NET_ADMIN_PASSWORD_FILE" in out
            assert PW not in out and PW not in read(os.path.join(srv.data, "all.log"))
            assert "first_run_setup\tSER2NET_ADMIN_PASSWORD_FILE\tops" in read(os.path.join(srv.data, "audit.log"))
            data = srv.data
            srv.stop()
            with open(pwfile, "w") as fh:
                fh.write("another-pass-9\n")
            srv.start()  # accounts exist now: the variable is ignored, the password stays
            Browser(srv.base).login("ops", PW)
            assert "SER2NET_ADMIN_PASSWORD_FILE is ignored" in srv.output()
            assert len(config_of(data)["users"]) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("SER2NET_ADMIN_PASSWORD_FILE: first admin at start (no password in the logs), "
          "ignored once accounts exist  OK")


def main():
    test_apply()
    test_request_files()
    test_provision()
    test_become_owner()
    test_offline()
    test_running()
    test_env_admin()
    print("\nPASS: account recovery and headless first admin")


if __name__ == "__main__":
    main()
