"""Start-up never refuses a config.json that loads, and rewrites it only when it changed.

A start with SER2NET_BIND_IP set (Docker, headless) used to validate and re-save
config.json every time: one setting the current rules reject (a mapping's TLS file that
is not in the container, a hand edit, a stricter rule in a later release) or a file that
can't be replaced (mounted on its own, read-only, a full disk) ended the start with a
traceback, and `restart: unless-stopped` looped it with the web UI down. Checked here the
way a container starts:
  * a config.json that already holds the settings is left alone (no rewrite, no backup);
  * a new address or an upgrade's normalisation is saved once, the old file backed up;
  * a rejected setting is logged (which mapping, what), the file is left as it is, and
    the real server starts: the UI answers, the broken mappings fail on their own, the
    others run, and the cluster and a mapping that can't even be built don't stop it;
  * a write that fails is a warning, not a crash;
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
import urllib.request

from _server import ROOT, Browser, Server, free_port

from app import runtime
from app.config import AdminUI, AppConfig, ConfigStore, MappingConfig
from app.web.auth import hash_password

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

    def save(self, config):
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


def broken_config(ports: dict) -> dict:
    """Every mapping here breaks a rule validate() enforces; none may stop the start."""
    missing = os.path.join(tempfile.gettempdir(), "s2n-no-such-dir", "cert.pem")
    net = {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1"}
    return {
        "users": [{"username": "admin", "password_hash": hash_password("admin-pass-1"),
                   "role": "admin", "pwd_version": 1, "source": "local"}],
        "cluster": {"enabled": True, "key": "cluster-key", "discovery_port": "not-a-port"},
        "mappings": [
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


def test_problems_listed():
    cfg = AppConfig.from_dict(broken_config({"acl": 1, "tls_off": 2, "tls_on": 3, "modbus": 4}))
    found = cfg.problems()
    assert found[0] == "Cluster discovery port must be a number.", found
    assert [p.split(":")[0] for p in found[1:]] == [
        "mapping 'acl-typo'", "mapping 'tls-off'", "mapping 'tls-on'", "mapping 'baud-typo'"], found
    assert "TLS file not found" in found[2] and "Baud rate" in found[4], found
    clash = AppConfig(mappings=[MappingConfig(name="a"), MappingConfig(name="b")])
    for m in clash.mappings:  # both on the default 0.0.0.0:4001, different serial ports
        m.serial.port = f"/dev/s2n-{m.name}"
    assert clash.problems() == ["Mappings 'a' and 'b' both use TCP port 4001 on overlapping "
                                "addresses (0.0.0.0 / 0.0.0.0)."], clash.problems()
    assert AppConfig().problems() == []
    print("problems(): every rejected setting, by mapping name, cross-mapping clashes too  OK")


def test_invalid_settings_still_start():
    ports = {k: free_port() for k in ("acl", "tls_off", "tls_on", "modbus")}
    srv = Server(broken_config(ports))
    with open(srv.config_path, "rb") as fh:
        written = fh.read()
    with srv:
        log = srv.output()
        for line in ("WARNING: config.json: Cluster discovery port must be a number.",
                     "WARNING: config.json: mapping 'acl-typo': Invalid allowed client IP/CIDR",
                     "WARNING: config.json: mapping 'tls-off': TLS file not found",
                     "WARNING: config.json: mapping 'baud-typo': Baud rate must be",
                     "config.json was not updated at start-up",
                     "ser2net starts with the setting(s) above anyway",
                     "cluster: discovery port 'not-a-port' is not a port number",
                     "[tls-on] start failed", "[baud-typo] start failed",
                     f"listening on 127.0.0.1:{ports['acl']}"):
            assert line in log, f"{line!r} not logged:\n{log[-3000:]}"
        assert "Traceback" not in log, log[-3000:]
        with open(srv.config_path, "rb") as fh:
            assert fh.read() == written, "config.json must be left as it is"
        assert backups(srv.data) == []
        with urllib.request.urlopen(srv.base + "/healthz", timeout=10) as r:
            assert r.read().strip() == b"ok"
        b = Browser(srv.base)
        b.login("admin", "admin-pass-1")  # a local sign-in saves nothing: works
        st, _h, _body = b.get("/")
        assert st == 200, st
        st, _h, table = b.get("/api/status")  # the dashboard's mapping table
        assert st == 200 and "baud-typo" in table and "state-error" in table, (st, table[-1500:])
        with socket.create_connection(("127.0.0.1", ports["acl"]), timeout=5):
            pass
    print("rejected settings: logged per mapping, config.json untouched, UI up, the bad "
          "mappings fail alone, the others run  OK")


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
        msg = exits_with(runtime._require_admin_tls, AdminUI(tls_cert=missing, tls_key=missing))
        assert "cannot read the admin UI TLS certificate" in msg and missing in msg, msg
        runtime._require_admin_tls(AdminUI())  # no TLS: nothing to check
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
    test_invalid_settings_still_start()
    test_write_failure_is_a_warning()
    test_fatal_with_one_line()
    test_real_process_has_no_traceback()
    print("\nPASS: start-up keeps going on a config.json that loads, rewrites it only on change")


if __name__ == "__main__":
    main()
