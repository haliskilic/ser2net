"""Start-up never refuses a config.json that loads, and rewrites it only when it changed.

A start with SER2NET_BIND_IP set (Docker, headless) used to validate and re-save
config.json every time: one setting the current rules reject (a mapping's TLS file that
is not in the container, a hand edit, a stricter rule in a later release) or a file that
can't be replaced (mounted on its own, read-only, a full disk) ended the start with a
traceback, and `restart: unless-stopped` looped it with the web UI down. Checked here the
way a container starts:
  * a config.json that already holds the settings is left alone (no rewrite, no backup);
  * a new address or an upgrade's normalisation is saved once, the old file backed up;
  * a rejected setting is logged (which part, what) and accepted, and the real server
    starts: the UI answers, the broken mappings fail on their own, the others run, and
    the cluster and a mapping that can't even be built don't stop it;
  * later saves refuse only what they change or add: stopping or deleting a mapping,
    fixing one broken part while others stay broken work; editing a broken part without
    fixing it, or adding a new clash, is refused with its error;
  * a write that fails is a warning, not a crash, and so is a file a check looks for
    that goes away while the start saves;
  * what start-up adds for the accounts — the first admin from
    SER2NET_ADMIN_PASSWORD_FILE, the session key and cluster id of a config.json that
    had none — is saved once, also while a setting is accepted broken, and the next
    start leaves the file alone;
  * fatal with one line and no traceback: an unusable SER2NET_BIND_IP / SER2NET_PORT, a
    config.json that can't be read or parsed, a first run that can't create it, an admin
    UI certificate that can't be read.
Cross-platform (the unreadable-file case is POSIX only).

Run: python3 tests/test_boot_config.py
"""
import contextlib
import errno
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from _server import ROOT, Browser, Server, free_port

from app import runtime
from app.config import AdminUI, AppConfig, ConfigError, ConfigStore, MappingConfig
from app.web.auth import hash_password, hash_token

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "config-2.7.1.json")
CONTAINER = {"SER2NET_BIND_IP": "0.0.0.0", "SER2NET_PORT": "8080"}


@contextlib.contextmanager
def env(**values):
    """Set (str) or unset (None) environment variables for the block."""
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def failing_save(exc):
    """ConfigStore.save raises `exc` (a read-only data dir, a config.json mounted alone)."""
    real = ConfigStore.save

    def save(self, config, **_kw):
        raise exc

    ConfigStore.save = save
    try:
        yield
    finally:
        ConfigStore.save = real


def new_dir() -> str:
    return tempfile.mkdtemp(prefix="s2n_boot_")


def file_state(path: str):
    st = os.stat(path)
    with open(path, "rb") as fh:
        return st.st_ino, st.st_mtime_ns, fh.read()


def backups(data: str) -> list[str]:
    d = os.path.join(data, "backups")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def warnings(config) -> list[str]:
    return [n for n in config.load_notes if n.startswith("WARNING")]


def exits_with(fn, *args) -> str:
    try:
        fn(*args)
    except SystemExit as e:
        assert isinstance(e.code, str) and e.code.startswith("ser2net: "), e.code
        assert "\n" not in e.code, e.code
        return e.code
    raise AssertionError(f"{fn.__name__} did not stop the start")


def test_left_alone_unless_changed():
    data = new_dir()
    path = os.path.join(data, "config.json")
    try:
        with env(**CONTAINER):
            runtime.prepare_config(path)  # first run: created (no console picker: env set)
            assert os.path.isfile(path) and backups(data) == []
            before = file_state(path)
            _store, cfg = runtime.prepare_config(path)
            assert file_state(path) == before, "an unchanged config.json was rewritten"
            assert backups(data) == [] and warnings(cfg) == [], (backups(data), cfg.load_notes)
        print("same settings on the next start: config.json not rewritten, no backup  OK")

        with env(SER2NET_BIND_IP="0.0.0.0", SER2NET_PORT="9090"):
            runtime.prepare_config(path)
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["admin_ui"]["port"] == 9090
        assert len(backups(data)) == 1, backups(data)
        print("a new SER2NET_PORT: saved, the previous file backed up  OK")

        shutil.copy(FIXTURE, path)  # an upgrade: 2.8 adds keys and moves the trace paths
        with env(**CONTAINER):
            runtime.prepare_config(path)
            with open(path, encoding="utf-8") as fh:
                assert "session_timeout_s" in json.load(fh)
            newest = ConfigStore(path).backup_names()[0]
            with open(os.path.join(data, "backups", newest), encoding="utf-8") as fh, \
                    open(FIXTURE, encoding="utf-8") as orig:
                assert json.load(fh) == json.load(orig), "the 2.7.1 file must be backed up"
            before, kept = file_state(path), backups(data)
            runtime.prepare_config(path)
            assert file_state(path) == before and backups(data) == kept
        print("upgraded 2.7.1 config: normalised + backed up on the first start, then left alone  OK")
    finally:
        shutil.rmtree(data, ignore_errors=True)


TOKEN = "s2n_boot-config-test"
MISSING = os.path.join(tempfile.gettempdir(), "s2n-no-such-dir", "cert.pem")


def broken_config(ports: dict) -> dict:
    """Every mapping but "ok" breaks a rule validate() enforces; none may stop the start."""
    missing = MISSING
    net = {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1"}
    return {
        "users": [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
                   "role": "admin", "pwd_version": 1, "source": "local"}],
        "api_token_hash": hash_token(TOKEN),
        "cluster": {"enabled": True, "key": "cluster-key", "discovery_port": "not-a-port"},
        "mappings": [
            {"id": "b00000000000", "name": "ok", "serial": {"port": "/dev/s2n-missing-0"},
             "network": dict(net, port=ports["ok"])},
            # rejected, but runs (the bad entry is skipped): must keep listening
            {"id": "b00000000001", "name": "acl-typo", "serial": {"port": "/dev/s2n-missing-1"},
             "network": dict(net, port=ports["acl"], allowed_client_ips=["not-a-cidr",
                                                                         "127.0.0.1"])},
            # the classic trigger: TLS files missing on this machine, mapping disabled
            {"id": "b00000000002", "name": "tls-off", "enabled": False,
             "serial": {"port": "/dev/s2n-missing-2"},
             "network": dict(net, port=ports["tls_off"], tls=True, tls_cert=missing,
                             tls_key=missing)},
            # the same, enabled: fails on its own
            {"id": "b00000000003", "name": "tls-on", "serial": {"port": "/dev/s2n-missing-3"},
             "network": dict(net, port=ports["tls_on"], tls=True, tls_cert=missing,
                             tls_key=missing)},
            # a hand-edited value the runner can't even be built from
            {"id": "b00000000004", "name": "baud-typo",
             "serial": {"port": "/dev/s2n-missing-4", "baudrate": "9600"},
             "network": dict(net, protocol="modbus", port=ports["modbus"])},
        ],
    }


PORTS = {"ok": 1, "acl": 2, "tls_off": 3, "tls_on": 4, "modbus": 5}


def test_problems_listed():
    cfg = AppConfig.from_dict(broken_config(PORTS))
    cfg.ldap.enabled = True  # a second broken section: each one is listed, by name
    found = cfg.problems()
    assert found[0].startswith("LDAP: LDAP is enabled but no server URI"), found
    assert found[1] == "LAN cluster: Cluster discovery port must be a number.", found
    assert [p.split(":")[0] for p in found[2:]] == [
        "mapping 'acl-typo'", "mapping 'tls-off'", "mapping 'tls-on'", "mapping 'baud-typo'"], found
    assert "TLS file not found" in found[3] and "Baud rate" in found[5], found
    clash = AppConfig(mappings=[MappingConfig(name=n) for n in ("a", "b", "c")])
    for m in clash.mappings:  # all on the default 0.0.0.0:4001, a and c on one serial port
        m.serial.port = "/dev/s2n-a" if m.name in "ac" else "/dev/s2n-b"
    clash.mappings[1].network.port = 4002
    assert clash.problems() == [
        "Mappings 'a' and 'c' both use serial port /dev/s2n-a — a serial port can only be "
        "opened by one mapping.",
        "Mappings 'a' and 'c' both use TCP port 4001 on overlapping addresses (0.0.0.0 / 0.0.0.0)."
    ], clash.problems()
    try:
        clash.validate()
    except ConfigError as e:  # validate() still raises the first one
        assert str(e) == clash.problems()[0], str(e)
    else:
        raise AssertionError("validate() accepted a clash")
    assert AppConfig().problems() == []
    for key in (None, 1234):  # a hand-edited key that is not text must not break the start
        odd = AppConfig.from_dict({"cluster": {"enabled": True, "key": key}})
        assert odd.cluster.key == ("" if key is None else "1234") and \
            odd.cluster.active is (key is not None), odd.cluster
    assert AppConfig.from_dict({"cluster": {"enabled": True, "key": None}}).problems() == [
        "LAN cluster: Cluster is enabled but no shared key is set."]
    print("problems(): every rejected part (section or mapping) by name, every clash  OK")


def test_saves_refuse_only_new_problems():
    """What start-up accepted may stay as it is; what a save changes or adds must validate."""
    data = new_dir()
    try:
        store = ConfigStore(os.path.join(data, "config.json"))
        cfg = AppConfig.from_dict(broken_config(PORTS))
        try:
            store.save(cfg)
        except ConfigError:
            pass
        else:
            raise AssertionError("an invalid config was saved without being accepted first")
        accepted = cfg.tolerate_problems()
        assert len(accepted) == 5 and not os.path.exists(store.path)
        store.save(cfg)  # nothing changed: the accepted problems don't block the save
        saved = read_json(store.path)
        assert "tolerated" not in saved and saved["cluster"]["discovery_port"] == "not-a-port"

        by = {m.name: m for m in cfg.mappings}
        by["ok"].enabled = by["tls-on"].enabled = False  # stopping: fine, broken or not
        cfg.users[0].pwd_version += 1                    # a password change, a sign-in ...
        store.save(cfg)
        by["tls-on"].enabled = True
        store.save(cfg)

        def refused(change, undo, expect):
            change()
            try:
                store.save(cfg)
            except ConfigError as e:
                assert expect in str(e), str(e)
            else:
                raise AssertionError(f"saved although {expect!r}")
            finally:
                undo()

        acl = by["acl-typo"].network
        # a broken part edited without fixing it
        refused(lambda: setattr(acl, "max_connections", 2),
                lambda: setattr(acl, "max_connections", 1), "Invalid allowed client IP/CIDR")
        refused(lambda: setattr(cfg.cluster, "advertise_ip", "x"),
                lambda: setattr(cfg.cluster, "advertise_ip", ""), "Cluster discovery port")
        # a new problem in a part that was fine
        refused(lambda: setattr(by["ok"].network, "max_connections", 0),
                lambda: setattr(by["ok"].network, "max_connections", 1), "Max connections")
        newcomer = MappingConfig(name="clash")
        newcomer.serial.port = "/dev/s2n-new"
        newcomer.network.bind_ip, newcomer.network.port = "127.0.0.1", PORTS["ok"]
        refused(lambda: cfg.mappings.append(newcomer), lambda: cfg.mappings.remove(newcomer),
                "both use TCP port")
        # fixing one broken part while the others stay broken
        acl.allowed_client_ips = ["127.0.0.1"]
        store.save(cfg)
        assert len(cfg.problems()) == 4 and "mapping b00000000001" not in cfg.tolerated
        # ... and once fixed, it is held to the rules again
        refused(lambda: setattr(acl, "allowed_client_ips", ["not-a-cidr"]),
                lambda: setattr(acl, "allowed_client_ips", ["127.0.0.1"]),
                "Invalid allowed client IP/CIDR")
        cfg.mappings.remove(by["baud-typo"])  # deleting a broken mapping
        store.save(cfg)
        assert [m["name"] for m in read_json(store.path)["mappings"]] == [
            "ok", "acl-typo", "tls-off", "tls-on"]
        fresh = AppConfig.from_dict(cfg.to_dict())  # a restore or import starts strict
        assert fresh.tolerated == {}
        try:
            fresh.validate_for_save()
        except ConfigError:
            pass
        else:
            raise AssertionError("an unaccepted config passed")
    finally:
        shutil.rmtree(data, ignore_errors=True)
    print("saves: accepted problems may stay; a changed or new part must validate; fixed "
          "parts are held to the rules again  OK")


def api(srv, method, path, body=None):
    req = urllib.request.Request(srv.base + "/api/v1" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_invalid_settings_still_start():
    ports = {k: free_port() for k in PORTS}
    srv = Server(broken_config(ports))
    with open(srv.config_path, encoding="utf-8") as fh:
        written = json.load(fh)
    with srv:
        log = srv.output()
        for line in ("WARNING: config.json: LAN cluster: Cluster discovery port must be a number.",
                     "WARNING: config.json: mapping 'acl-typo': Invalid allowed client IP/CIDR",
                     "WARNING: config.json: mapping 'tls-off': TLS file not found",
                     "WARNING: config.json: mapping 'baud-typo': Baud rate must be",
                     "ser2net starts with the setting(s) above anyway",
                     "cluster: discovery port 'not-a-port' is not a port number",
                     "[tls-on] start failed", "[baud-typo] start failed",
                     f"listening on 127.0.0.1:{ports['ok']}",
                     f"listening on 127.0.0.1:{ports['acl']}"):
            assert line in log, f"{line!r} not logged:\n{log[-3000:]}"
        assert "Traceback" not in log, log[-3000:]
        # saved as usual (here: the minimal file filled in), the rejected values kept as they were
        saved = srv.saved_config()
        by = {m["name"]: m for m in saved["mappings"]}
        assert by["tls-on"]["network"]["tls_cert"] == MISSING
        assert by["baud-typo"]["serial"]["baudrate"] == "9600"
        assert saved["cluster"]["discovery_port"] == "not-a-port" and "tolerated" not in saved
        assert len(backups(srv.data)) == 1
        with open(os.path.join(srv.data, "backups", backups(srv.data)[0]), encoding="utf-8") as fh:
            assert json.load(fh) == written, "the original file must be backed up"
        with urllib.request.urlopen(srv.base + "/healthz", timeout=10) as r:
            assert r.read().strip() == b"ok"
        b = Browser(srv.base)
        b.login("admin", "admin-pass-1")
        st, _h, _body = b.get("/")
        assert st == 200, st
        st, _h, table = b.get("/api/status")  # the dashboard's mapping table
        assert st == 200, st
        row = table[table.index('data-mid="b00000000004"'):]
        row = row[:row.index("</tr>")]  # the mapping its runner can't even be built for
        assert "state-error" in row and "its settings can&#39;t be used" in row, row
        with socket.create_connection(("127.0.0.1", ports["acl"]), timeout=5):
            pass
        print("rejected settings: logged per part, accepted, saved as they were; UI up, the "
              "bad mappings fail alone, the others run  OK")

        mid = {m["name"]: m["id"] for m in saved["mappings"]}
        for name in ("ok", "tls-on"):  # stop a valid and a broken mapping
            st, body = api(srv, "POST", f"/mappings/{mid[name]}/stop")
            assert st == 200, (st, body)
        st, _h, page = b.post("/settings/password", {"current": "admin-pass-1",
                                                     "password": "admin-pass-2",
                                                     "password2": "admin-pass-2"})
        assert st == 200 and "Password updated" in page, (st, page[-800:])
        saved = srv.saved_config()
        assert [m["enabled"] for m in saved["mappings"] if m["name"] in ("ok", "tls-on")] \
            == [False, False]
        assert saved["users"][0]["pwd_version"] == 2
        st, baud = api(srv, "GET", f"/mappings/{mid['baud-typo']}")
        baud["name"] = "baud-typo-renamed"  # still "9600": refused, with its own error
        st, body = api(srv, "PUT", f"/mappings/{mid['baud-typo']}", baud)
        assert st == 400 and "Baud rate must be a positive integer" in body["detail"], (st, body)
        baud["serial"]["baudrate"] = 9600
        st, body = api(srv, "PUT", f"/mappings/{mid['baud-typo']}", baud)
        assert st == 200, (st, body)
        st, body = api(srv, "POST", "/mappings", {
            "name": "clash", "serial": {"port": "/dev/s2n-missing-9"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": ports["ok"]}})
        assert st == 400 and "both use TCP port" in body["detail"], (st, body)
        st, body = api(srv, "DELETE", f"/mappings/{mid['acl-typo']}")
        assert st == 200, (st, body)
        saved = srv.saved_config()
        assert [m["name"] for m in saved["mappings"]] == ["ok", "tls-off", "tls-on",
                                                          "baud-typo-renamed"]
        assert "Traceback" not in srv.output(), srv.output()[-3000:]
    print("with problems accepted: stopping (valid or broken), a password change, fixing one "
          "part, deleting another are saved; an unfixed edit and a new clash are refused  OK")


def test_accounts_saved_once():
    data = new_dir()
    path = os.path.join(data, "config.json")
    pwfile = os.path.join(data, "admin-password.txt")
    no_env = {"SER2NET_BIND_IP": None, "SER2NET_PORT": None}  # only the accounts change it
    try:
        with open(pwfile, "w", encoding="utf-8") as fh:
            fh.write("boot-admin-pass-1\n")
        doc = broken_config(PORTS)
        doc["users"] = []
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with env(SER2NET_ADMIN_PASSWORD_FILE=pwfile, SER2NET_ADMIN_USER="boss", **no_env):
            _store, cfg = runtime.prepare_config(path)
            saved = read_json(path)
            assert [(u["username"], u["role"]) for u in saved["users"]] == [("boss", "admin")]
            assert saved["secret_key"] == cfg.secret_key and saved["instance_id"] == cfg.instance_id
            assert (saved["cluster"]["discovery_port"], saved["mappings"][3]["network"]["tls_cert"],
                    saved["mappings"][4]["serial"]["baudrate"]) == ("not-a-port", MISSING, "9600"), \
                "the broken parts must stay as they were"
            assert len(backups(data)) == 1, backups(data)
            assert "admin account 'boss' created from SER2NET_ADMIN_PASSWORD_FILE" in cfg.load_notes
            assert "first_run_setup\tSER2NET_ADMIN_PASSWORD_FILE\tboss" in \
                read_text(os.path.join(data, "audit.log"))
            before, kept = file_state(path), backups(data)
            _store, cfg = runtime.prepare_config(path)
            assert file_state(path) == before and backups(data) == kept, "rewritten on a restart"
            assert any("SER2NET_ADMIN_PASSWORD_FILE is ignored" in n for n in cfg.load_notes)
        print("SER2NET_ADMIN_PASSWORD_FILE with a setting accepted broken: the admin, session "
              "key and cluster id saved once, the next start leaves config.json alone  OK")

        doc = read_json(path)
        key = doc.pop("secret_key")
        doc.pop("instance_id")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        with env(**no_env):
            _store, cfg = runtime.prepare_config(path)
            saved = read_json(path)
            assert saved["secret_key"] == cfg.secret_key != key and saved["instance_id"]
            before = file_state(path)
            _store, again = runtime.prepare_config(path)
            assert file_state(path) == before and again.secret_key == cfg.secret_key, \
                "the session key must survive a restart"
        print("a config.json without secret_key / instance_id: made once and kept  OK")
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_write_failure_is_a_warning():
    data = new_dir()
    path = os.path.join(data, "config.json")
    try:
        with env(**CONTAINER):
            runtime.prepare_config(path)
        before = file_state(path)
        busy = OSError(errno.EBUSY, "Device or resource busy")
        busy.filename, busy.filename2 = os.path.join(data, ".cfg.x.tmp"), path
        with env(SER2NET_BIND_IP="0.0.0.0", SER2NET_PORT="9191"), failing_save(busy):
            _store, cfg = runtime.prepare_config(path)
        assert cfg.admin_ui.port == 9191, "the env address applies to this run"
        assert file_state(path) == before
        note = " ".join(warnings(cfg))
        assert "could not update config.json at start-up" in note, cfg.load_notes
        assert "Device or resource busy" in note and path in note, note
        print("a config.json that can't be replaced: a warning, the start goes on  OK")
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_file_gone_while_saving():
    """Start-up accepts what config.json breaks, then saves what it adds (here the session
    key and cluster id of a hand-made file). A TLS file that a check found a moment ago
    and that is gone by the save must not stop the start with a ConfigError."""
    data = new_dir()
    path = os.path.join(data, "config.json")
    cert = os.path.join(data, "cert.pem")
    real = AppConfig.tolerate_problems

    def tolerate_then_lose_the_file(self):
        found = real(self)
        os.remove(cert)  # e.g. replaced by a certificate renewal just now
        return found

    try:
        with open(cert, "w") as fh:
            fh.write("not checked here")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"mappings": [{
                "id": "d00000000001", "name": "tls", "serial": {"port": "/dev/s2n-missing"},
                "network": {"mode": "server", "port": 4200, "tls": True, "tls_cert": cert,
                            "tls_key": cert}}]}, fh)
        AppConfig.tolerate_problems = tolerate_then_lose_the_file
        with env(SER2NET_BIND_IP=None, SER2NET_PORT=None):
            _store, cfg = runtime.prepare_config(path)
        AppConfig.tolerate_problems = real
        saved = read_json(path)
        assert saved["secret_key"] == cfg.secret_key and saved["instance_id"], saved
        assert saved["mappings"][0]["network"]["tls_cert"] == cert
        with env(SER2NET_BIND_IP=None, SER2NET_PORT=None):
            _store, cfg = runtime.prepare_config(path)
        assert any("TLS file not found" in w for w in warnings(cfg)), cfg.load_notes
        print("a file a check looks for, gone while start-up saves: saved anyway, the next "
              "start warns about it  OK")
    finally:
        AppConfig.tolerate_problems = real
        shutil.rmtree(data, ignore_errors=True)


@contextlib.contextmanager
def full_disk():
    """config.json's temp file can't be created (a full disk, a read-only data dir)."""
    import app.config as config_module

    real = config_module.tempfile.mkstemp

    def mkstemp(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    config_module.tempfile.mkstemp = mkstemp
    try:
        yield
    finally:
        config_module.tempfile.mkstemp = real


def test_failed_write_keeps_what_was_accepted():
    """A save that validates but then can't be written is rolled back by its caller
    (stopping or deleting a mapping); what start-up accepted while broken must stay
    accepted too, or every later, unrelated save is refused until a restart."""
    data = new_dir()
    try:
        store = ConfigStore(os.path.join(data, "config.json"))
        cfg = AppConfig.from_dict({
            "users": [{"username": "admin", "password_hash": hash_password("pw-12345678"),
                       "role": "admin", "pwd_version": 1}],
            "mappings": [{"id": f"c0000000000{i}", "name": n, "serial": {"port": "/dev/ttyX"},
                          "network": {"mode": "server", "port": 4100 + i}}
                         for i, n in ((1, "A"), (2, "B"))]})
        assert len(cfg.tolerate_problems()) == 1  # both use /dev/ttyX: accepted at start-up
        store.save(cfg)
        b = cfg.get_mapping("c00000000002")
        b.enabled = False  # stopping B would resolve the clash ...
        with full_disk():
            try:
                store.save(cfg)
            except OSError:
                b.enabled = True  # ... but the write failed, so ops rolls the stop back
            else:
                raise AssertionError("the save was expected to fail")
        cfg.users[0].pwd_version += 1  # an unrelated save afterwards (password change, sign-in)
        store.save(cfg)
        assert read_json(store.path)["users"][0]["pwd_version"] == 2
        # a save that really resolves it is written and then holds the rule again
        b.enabled = False
        store.save(cfg)
        assert not cfg.tolerated, cfg.tolerated
        b.enabled = True
        try:
            store.save(cfg)
        except ConfigError as e:
            assert "both use serial port" in str(e), str(e)
        else:
            raise AssertionError("a clash resolved and re-introduced was accepted again")
        print("a save that fails to write keeps what start-up accepted; a written fix ends it  OK")
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_fatal_with_one_line():
    data = new_dir()
    path = os.path.join(data, "config.json")
    try:
        with env(SER2NET_BIND_IP="eth0", SER2NET_PORT=None):
            msg = exits_with(runtime.prepare_config, path)
            assert "SER2NET_BIND_IP must be an IP address" in msg and "'eth0'" in msg, msg
        with env(SER2NET_BIND_IP="0.0.0.0", SER2NET_PORT="70000"):
            msg = exits_with(runtime.prepare_config, path)
            assert "SER2NET_PORT must be a port number" in msg, msg
        assert not os.path.exists(path), "nothing may be written before the env is checked"
        with env(SER2NET_BIND_IP="0.0.0.0", SER2NET_PORT="80a"):
            _store, cfg = runtime.prepare_config(path)
            assert cfg.admin_ui.port == 8080
            assert any("SER2NET_PORT '80a' is not a number" in n for n in cfg.load_notes)
        print("bad SER2NET_BIND_IP / out-of-range SER2NET_PORT: stop with one line; a "
              "non-numeric port is ignored with a warning  OK")

        with env(**CONTAINER):
            runtime.prepare_config(path)
            with env(SER2NET_PORT="8081"):
                runtime.prepare_config(path)  # makes a backup of the previous file
            newest = ConfigStore(path).backup_names()[0]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"version": 1,, }')
            msg = exits_with(runtime.prepare_config, path)
            assert f"{path} is not a valid configuration" in msg and newest in msg, msg
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"mappings": "not-a-list"}, fh)
            msg = exits_with(runtime.prepare_config, path)
            assert "is not a valid configuration" in msg, msg
        print("config.json that is not JSON / not a config: stop with one line naming the "
              "newest backup  OK")
        with env(**CONTAINER):  # the Windows service has no console: the line goes to all.log
            msg = exits_with(runtime.service_main, path)
        assert msg in read_text(os.path.join(data, "all.log")), "not in all.log"
        print("as a service: the one line is also written to all.log  OK")

        if os.name == "posix" and os.geteuid() != 0:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            os.chmod(path, 0)
            try:
                with env(**CONTAINER):
                    msg = exits_with(runtime.prepare_config, path)
            finally:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            me = f"{os.getuid()}:{os.getgid()}"
            assert "cannot read the configuration" in msg and path in msg, msg
            assert f"it belongs to {me} (mode 0000) and ser2net runs as {me}" in msg, msg
            assert f"give the data directory to that account: sudo chown -R {me} {data}" in msg
            print("unreadable config.json: stop with one line naming its owner, mode, the "
                  "account ser2net runs as and the chown that fixes it  OK")

        fresh = os.path.join(new_dir(), "config.json")
        denied = PermissionError(errno.EACCES, "Permission denied")
        denied.filename = os.path.join(os.path.dirname(fresh), ".cfg.x.tmp")
        with env(**CONTAINER), failing_save(denied):
            msg = exits_with(runtime.prepare_config, fresh)
        assert "cannot create the configuration" in msg and "Permission denied" in msg, msg
        if os.name == "posix":
            assert "its folder belongs to" in msg and "sudo chown -R" in msg, msg
        shutil.rmtree(os.path.dirname(fresh), ignore_errors=True)
        print("first run that can't create config.json: stop (no first-run setup in memory)  OK")

        missing = os.path.join(data, "no-such-cert.pem")
        msg = exits_with(runtime.require_admin_tls, AdminUI(tls_cert=missing, tls_key=missing))
        assert "cannot read the admin UI TLS certificate" in msg and missing in msg, msg
        assert "fix admin_ui.tls_cert / tls_key in config.json" in msg, msg
        if os.name == "posix" and os.geteuid() != 0:
            key = os.path.join(data, "key.pem")
            with open(key, "w") as fh:
                fh.write("key")
            os.chmod(key, 0)
            msg = exits_with(runtime.require_admin_tls, AdminUI(tls_cert=key, tls_key=key))
            os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
            assert "(mode 0000)" in msg and "make it readable for that account" in msg, msg
        msg = exits_with(runtime.require_admin_tls, AdminUI(tls_cert=True, tls_key=True))
        assert "admin_ui.tls_cert in config.json must be a file name, not True" in msg, msg
        os.fstat(1)  # stdout is still open (open(True) would have closed it)
        runtime.require_admin_tls(AdminUI())  # no TLS: nothing to check
        print("unreadable admin UI certificate: stop with one line (no fallback to HTTP)  OK")
    finally:
        shutil.rmtree(data, ignore_errors=True)


def test_real_process_has_no_traceback():
    data = new_dir()
    try:
        missing = os.path.join(data, "no-such-cert.pem")
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"admin_ui": {"tls_cert": missing, "tls_key": missing}}, fh)
        for extra, expect in (({"SER2NET_BIND_IP": "not-an-ip"}, "SER2NET_BIND_IP must be"),
                              ({}, "cannot read the admin UI TLS certificate")):
            run_env = dict(os.environ, SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(free_port()))
            run_env.update(extra)
            p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", data],
                               cwd=ROOT, env=run_env, stdin=subprocess.DEVNULL,
                               capture_output=True, encoding="utf-8", errors="replace",
                               timeout=60)
            out = p.stdout + p.stderr
            assert p.returncode == 1 and expect in out, (p.returncode, out[-2000:])
            assert "Traceback" not in out and "starting" not in out, out[-2000:]
        print("ser2net.py itself: exit 1 with the one-line message, no traceback, no bridge "
              "started  OK")
    finally:
        shutil.rmtree(data, ignore_errors=True)


def main():
    test_left_alone_unless_changed()
    test_problems_listed()
    test_saves_refuse_only_new_problems()
    test_invalid_settings_still_start()
    test_accounts_saved_once()
    test_write_failure_is_a_warning()
    test_file_gone_while_saving()
    test_failed_write_keeps_what_was_accepted()
    test_fatal_with_one_line()
    test_real_process_has_no_traceback()
    print("\nPASS: start-up keeps going on a config.json that loads, rewrites it only on change")


if __name__ == "__main__":
    main()
