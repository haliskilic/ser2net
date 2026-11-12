"""Going back to an older release loses nothing for good, and says what it can't keep.

No release keeps the keys it doesn't know: the first save drops them (2.7.x even keeps no
backups, and saves at once in Docker). Checks:
  * every save writes "written_by" (this release) into config.json; start-up does not
    rewrite a file only because an older release wrote it;
  * a config.json written by a newer release, or with keys this one doesn't know, is
    noted at start-up (which release, which keys) and shown on the Settings page;
  * after every save the newest backup is config.json itself, so when an older release
    then overwrites config.json without a backup (2.7.x), backups/ still has the newer
    file to restore; the backups list shows which release wrote each one and which is
    in use.

Run: python3 tests/test_downgrade.py
"""
import json
import os
import re
import shutil
import tempfile

from _server import Browser, Server

from app import __version__
from app.config import AppConfig, ConfigStore
from app.web.auth import hash_password

PW = "downgrade-pass-1"


def test_written_by():
    d = tempfile.mkdtemp(prefix="s2n_down_")
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        cfg = AppConfig()
        store.save(cfg)
        with open(store.path, encoding="utf-8") as fh:
            doc = json.load(fh)
        assert doc["written_by"] == __version__, doc.get("written_by")
        doc.pop("written_by")  # the same settings, written by a release before 2.9.0
        with open(store.path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        assert store.matches(AppConfig.from_dict(doc)), "an older writer alone is no change"
        assert AppConfig.from_dict(doc).load_notes == []
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print('saves write "written_by"; a file without it is not rewritten for that alone  OK')


def test_newer_writer_and_unknown_keys():
    for writer, newer in (("99.0.0", True), ("2.9.0rc1", None), (__version__, False),
                          ("2.7.1", False), ("dev", False), (7, False)):
        cfg = AppConfig.from_dict({"written_by": writer})
        if newer is None:  # depends on this release's number
            continue
        assert (cfg.newer_writer == writer) is newer, (writer, cfg.newer_writer)
        assert any("a newer version than this one" in n for n in cfg.load_notes) is newer
    cfg = AppConfig.from_dict({"written_by": "99.0.0"})
    assert cfg.load_notes == [
        f"config.json was written by ser2net 99.0.0, a newer version than this one "
        f"({__version__}): what only 99.0.0 knows is not used here and is left out when this "
        "version saves config.json. backups/ keeps the file as 99.0.0 left it: restore it "
        "there after upgrading again"], cfg.load_notes
    cfg = AppConfig.from_dict({
        "written_by": __version__, "telemetry": {"on": True},
        "notifications": {"enabled": False, "pager_url": "https://p"},
        "mappings": [{"id": "aaaaaaaaaaa1", "name": "m", "serial": {"port": "/dev/x"},
                      "network": {"mode": "server", "tls_ciphers": "x"}}]})
    assert cfg.unknown_keys == ["telemetry", "notifications.pager_url",
                                "mappings[0].network.tls_ciphers"], cfg.unknown_keys
    notes = "\n".join(cfg.load_notes)
    assert ("config.json: not settings of this version, so they are not used and are left "
            "out when config.json is next saved (backups/ keeps the file as it was): "
            "telemetry, notifications.pager_url") in notes, notes
    assert "mapping 'm': not settings of this version" in notes and "network.tls_ciphers" in notes
    assert "telemetry" not in json.dumps(cfg.to_dict())
    print("a newer writer and unknown keys are noted at load (not for an older writer)  OK")


def test_backups_survive_a_release_without_them():
    d = tempfile.mkdtemp(prefix="s2n_down_")
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        cfg = AppConfig.from_dict({"notifications": {"enabled": False, "smtp_password": "s3cret"}})
        store.save(cfg)
        cfg.session_timeout_s = 3600
        store.save(cfg)
        with open(store.path, encoding="utf-8") as fh:
            current = fh.read()
        # an older release takes over: it drops what it doesn't know and replaces
        # config.json without keeping anything (as 2.7.x does at its first save)
        old = {k: v for k, v in json.loads(current).items()
               if k not in ("notifications", "written_by")}
        with open(store.path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        kept = store.read_backup(store.backup_names()[0])
        assert kept == current and json.loads(kept)["notifications"]["smtp_password"] == "s3cret"
        info = store.backups()
        assert [(b["written_by"], b["current"]) for b in info] == [
            (__version__, False), (__version__, False)], info
        # upgraded again: the file 2.7.x wrote is kept too before it is replaced
        store.save(AppConfig.from_dict(json.loads(kept)))
        assert len(store.backup_names()) == 4 and store.backups()[0]["current"]
        assert json.loads(store.read_backup(store.backup_names()[1])) == old
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("the newest backup is always config.json: an older release that keeps no backups "
          "can't lose the newer file  OK")


def test_settings_page():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    config = {"written_by": "99.0.0", "users": users, "future": {"x": 1},
              "admin_ui": {"theme": "dark"}}
    with Server(config) as srv:
        log = srv.output()
        assert "config.json was written by ser2net 99.0.0, a newer version than this one" in log
        assert "not settings of this version" in log and "future, admin_ui.theme" in log, log
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _h, page = b.get("/settings")
        assert st == 200
        assert ("At start-up, config.json had been written by ser2net 99.0.0, a newer version "
                f"than this one ({__version__})") in page, page[page.find('id="backups"'):][:1500]
        assert ("At start-up, config.json had settings this version doesn’t know: future, "
                "admin_ui.theme.") in page
        table = page[page.find('id="backups"'):]
        rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
        assert len(rows) == 3, rows  # header, the file as 99.0.0 left it, the one written now
        assert '<span class="tag">in use</span>' in rows[1], rows[1]
        assert f"written by ser2net {__version__}" in rows[1], rows[1]
        assert "written by ser2net 99.0.0" in rows[2] and "in use" not in rows[2], rows[2]
        assert srv.saved_config()["written_by"] == __version__
        tr = Browser(srv.base)
        tr.get("/lang/tr?next=/login")
        tr.login("admin", PW)
        st, _h, page = tr.get("/settings")
        assert "yazan: ser2net 99.0.0" in page and "kullanımda" in page, page[-3000:]
    print("Settings: the newer writer and unknown keys pointed out; each backup's writer, "
          "the one in use marked  OK")


def main():
    test_written_by()
    test_newer_writer_and_unknown_keys()
    test_backups_survive_a_release_without_them()
    test_settings_page()
    print("\nPASS: going back to an older release")


if __name__ == "__main__":
    main()
