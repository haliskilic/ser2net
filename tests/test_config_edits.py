"""Configuration changes are all or nothing (AppState.edit_config).

A change that could not be saved used to stay in memory at about half the places that
change the configuration, and a config.json that could not be written (a full or
read-only disk) gave a 500 everywhere. Starting a copy that shares a serial port with a
running mapping (what Copy makes) left it enabled in memory, so the dashboard showed it
running, and every later save — a password change, an LDAP/OIDC sign-in — failed on that
clash. Checks:
  * edit_config: a block that raises or a save that fails puts every field back, in
    place (the same objects); a block that changes nothing saves nothing; a save runs to
    its end when the request is cancelled meanwhile, and keeps the lock until then;
    keep_on_failure keeps a change that can't be saved;
  * a start that would share a serial port is refused with the reason (UI 400, REST 400
    validation, bulk 400 naming how many) and nothing stays enabled; later saves work;
  * a TLS file that goes missing while ser2net runs no longer blocks other saves; an
    edit of that mapping is still refused;
  * config.json can't be written: the password, the API token, a deletion stay as they
    were, with "Could not save config.json" (REST: 500 save_failed); a stop still stops
    (with a warning), and the next save that works keeps it.

Run: python3 tests/test_config_edits.py
"""
import asyncio
import errno
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _optional import need
from _pki import TestCA
from _server import Browser, Server, free_port

import app.config as config_module
from app.config import (
    AppConfig, ClusterSettings, ConfigError, ConfigStore, ConfigWriteError, MappingConfig,
)
from app.state import AppState
from app.web.auth import hash_password, hash_token

PW = "edits-pass-1"
NEW_PW = "edits-pass-2"
TOKEN = "s2n_config-edits-test"


class full_disk:
    """config.json's temp file can't be created (a full disk)."""

    def __enter__(self):
        self.real = config_module.tempfile.mkstemp

        def mkstemp(*_a, **_k):
            raise OSError(errno.ENOSPC, "No space left on device")

        config_module.tempfile.mkstemp = mkstemp

    def __exit__(self, *exc):
        config_module.tempfile.mkstemp = self.real


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


async def test_edit_config():
    d = tempfile.mkdtemp(prefix="s2n_edit_")
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        cfg = AppConfig.from_dict({
            "users": [{"username": "admin", "password_hash": "x", "role": "admin",
                       "pwd_version": 1}],
            "mappings": [{"id": "e00000000001", "name": "A", "serial": {"port": "/dev/s2n-e1"},
                          "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4301}}]})
        store.save(cfg)
        state = AppState(store, cfg)
        cluster, mappings, m, user = cfg.cluster, cfg.mappings, cfg.mappings[0], cfg.users[0]

        def changes():
            cfg.cluster = ClusterSettings(enabled=True, key="k")
            m.name = "renamed"
            m.serial.match["vid"] = "0403"
            mappings.append(MappingConfig.from_dict({
                "name": "new", "serial": {"port": "/dev/s2n-e2"},
                "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": 4302}}))
            user.role = "viewer"
            cfg.users = []

        def as_before():
            assert cfg.cluster is cluster and cfg.mappings is mappings and mappings == [m]
            assert m.name == "A" and m.serial.match == {} and cfg.users == [user]
            assert user.role == "admin"

        try:
            async with state.edit_config():
                changes()
                raise ConfigError("refused")
        except ConfigError:
            pass
        as_before()
        on_disk = read(store.path)
        with full_disk():
            try:
                async with state.edit_config():
                    changes()
            except ConfigWriteError as e:
                assert "Could not save config.json: No space left on device" in str(e), str(e)
            else:
                raise AssertionError("a failed write was not raised")
        as_before()
        assert read(store.path) == on_disk
        print("edit_config: a refused change and a failed write put every field back, in place  OK")

        calls = []
        real_save = store.save
        store.save = lambda *a, **k: (calls.append(1), real_save(*a, **k))[1]
        async with state.edit_config() as edit:
            m.name = "A"  # the same as before: nothing to save
        assert not calls and not edit.saved
        async with state.edit_config() as edit:
            m.name = "B"
        assert calls == [1] and edit.saved and json.loads(read(store.path))["mappings"][0]["name"] == "B"
        print("edit_config: a change that changes nothing is not saved  OK")

        with full_disk():
            try:
                async with state.edit_config(keep_on_failure=True):
                    user.pwd_version += 1
            except ConfigWriteError:
                pass
        assert user.pwd_version == 2, "keep_on_failure keeps the change"
        print("edit_config: keep_on_failure keeps a change that can't be saved  OK")

        # cancelled while saving: the save ends, the change stays, the lock is held until then
        entered, release = threading.Event(), threading.Event()

        def slow_save(*a, **k):
            entered.set()
            release.wait(10)
            return real_save(*a, **k)

        store.save = slow_save

        async def edit_c():
            async with state.edit_config():
                m.name = "C"

        task = asyncio.ensure_future(edit_c())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "the edit must wait for its save"
        waited = asyncio.ensure_future(state.config_lock.acquire())
        await asyncio.sleep(0.05)
        assert not waited.done(), "the lock was released before the save ended"
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancellation was not passed on")
        await waited
        state.config_lock.release()
        assert m.name == "C" and json.loads(read(store.path))["mappings"][0]["name"] == "C"
        print("edit_config: a cancelled request's save ends, and its change is kept  OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


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


def mapping_of(srv, name):
    _, listing = api(srv, "GET", "/mappings")
    return next(m for m in listing["mappings"] if m["name"] == name)


ALIVE = ("running", "reconnecting", "device-missing")  # (its serial port doesn't exist)


def wait_state(srv, name, want, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st = (mapping_of(srv, name)["status"] or {}).get("state")
        if st in want:
            return
        time.sleep(0.1)
    raise AssertionError(f"{name}: state {st!r}, not {want!r}")


def test_server():
    port = free_port()
    config = {"users": [{"username": "admin", "password_hash": hash_password(PW),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
              "mappings": [{"id": "e10000000001", "name": "A", "serial": {"port": "/dev/s2n-shared"},
                            "network": {"mode": "server", "bind_ip": "127.0.0.1",
                                        "port": port}}]}
    with Server(config) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        a_id = "e10000000001"
        st, _, _ = b.post(f"/api/mappings/{a_id}/duplicate", htmx=True)
        assert st == 200, st
        dup = mapping_of(srv, "A (copy)")
        assert dup["enabled"] is False and dup["serial"]["port"] == "/dev/s2n-shared"

        # starting the copy would share A's serial port: refused, nothing stays enabled
        st, _, text = b.post(f"/api/mappings/{dup['id']}/start", htmx=True)
        assert st == 400 and "both use serial port /dev/s2n-shared" in text, (st, text)
        code, err = api(srv, "POST", f"/mappings/{dup['id']}/start")
        assert code == 400 and err["error"] == "validation" and "serial port" in err["detail"], err
        st, _, text = b.post("/api/mappings/bulk", {"action": "start", "ids": [a_id, dup["id"]]},
                             htmx=True)
        assert st == 400 and text.startswith("1 of 2 selected mappings not changed: "), text
        assert mapping_of(srv, "A (copy)")["enabled"] is False
        assert srv.saved_config()["mappings"][1]["enabled"] is False
        # ... and a later, unrelated change is saved
        st, _, page = b.post("/settings/password", {"current": PW, "password": NEW_PW,
                                                    "password2": NEW_PW})
        assert "Password updated." in page, page[-800:]
        b = Browser(srv.base)
        b.login("admin", NEW_PW)
        print("a start that would share a serial port: 400 with the reason (UI, REST, bulk), "
              "nothing left enabled, later saves work  OK")

        # a TLS file that goes missing while running blocks no other save (the edit that
        # adds it loads the files, even of a disabled mapping)
        if not need("cryptography", what="a TLS file gone while running"):
            return
        tls_dir = tempfile.mkdtemp(prefix="s2n_tls_")
        cert, key = TestCA(tls_dir).issue(tls_dir, "t", ["127.0.0.1"])
        tls = {"name": "T", "enabled": False, "serial": {"port": "/dev/s2n-tls"},
               "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port(),
                           "tls": True, "tls_cert": cert, "tls_key": key}}
        code, made = api(srv, "POST", "/mappings", tls)
        assert code == 201, made
        shutil.rmtree(tls_dir)
        code, other = api(srv, "POST", "/mappings", {
            "name": "U", "enabled": False, "serial": {"port": "/dev/s2n-u"},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port()}})
        assert code == 201, other
        code, err = api(srv, "PUT", f"/mappings/{made['id']}", dict(tls, name="T2"))
        assert code == 400 and "TLS file not found" in err["detail"], err
        # the admin UI's own TLS files are checked when they are set
        st, _, page = b.post("/settings/tls", {"tls_cert": cert, "tls_key": cert})
        assert f"TLS file not found or not readable: {cert}" in page, page[-800:]
        assert srv.saved_config()["admin_ui"]["tls_cert"] == ""
        print("a TLS file gone while running: other saves work, an edit of its mapping (or "
              "of the admin UI's TLS) is refused  OK")

        if os.name != "posix" or os.geteuid() == 0:
            print("SKIP: the unwritable config.json part needs a POSIX non-root run")
            return
        saved = srv.saved_config()
        os.chmod(srv.data, 0o500)  # config.json can't be replaced
        try:
            st, _, page = b.post("/settings/password", {"current": NEW_PW, "password": PW,
                                                        "password2": PW})
            assert "Could not save config.json" in page and "Password updated." not in page
            Browser(srv.base).login("admin", NEW_PW)  # the password stays as it was
            st, _, page = b.post("/settings/api-token", {"api_token_role": "viewer"})
            assert "Could not save config.json" in page and "Copy it now" not in page
            assert api(srv, "GET", "/mappings")[0] == 200, "the token in use must keep working"
            st, _, text = b.post(f"/api/mappings/{dup['id']}", htmx=True, method="DELETE")
            assert st == 400 and text.startswith("Not deleted: Could not save config.json"), text
            assert mapping_of(srv, "A (copy)")
            code, err = api(srv, "DELETE", f"/mappings/{other['id']}")
            assert code == 500 and err["error"] == "save_failed", err
            # a stop stops anyway, and says that it isn't kept
            wait_state(srv, "A", ALIVE)
            st, h, _text = b.post(f"/api/mappings/{a_id}/stop", htmx=True)
            notice = json.loads(urllib.parse.unquote(h.get("X-Notice") or "{}"))
            assert st == 200 and notice.get("kind") == "warn" and notice.get("text", "").startswith(
                "Stopped, but config.json could not be saved"), (st, notice)
            wait_state(srv, "A", ("stopped",))
            assert mapping_of(srv, "A")["enabled"] is False
            code, err = api(srv, "POST", f"/mappings/{other['id']}/start")
            assert code == 500 and err["error"] == "save_failed", err
            assert mapping_of(srv, "U")["enabled"] is False
            assert srv.saved_config() == saved, "nothing may have been written"
        finally:
            os.chmod(srv.data, 0o700)
        code, _ = api(srv, "POST", f"/mappings/{other['id']}/start")  # a save that works ...
        assert code == 200
        assert srv.saved_config()["mappings"][0]["enabled"] is False  # ... keeps A's stop
        assert "WARNING: stopped 'A', but config.json could not be saved" in srv.output()
        print("config.json unwritable: password, token, deletions unchanged with the reason "
              "(REST 500 save_failed); a stop still stops, kept by the next save  OK")


def main():
    asyncio.run(test_edit_config())
    test_server()
    print("\nPASS: configuration changes are all or nothing")


if __name__ == "__main__":
    main()
