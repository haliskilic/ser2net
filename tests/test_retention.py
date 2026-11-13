"""Retention by age (Settings -> Retention, log_max_age_days; 0 by default):
  * off, nothing more is deleted than before, however old (all.log.N, audit.log.N,
    backups, the mapping logs' 15 days with their last 1000 lines);
  * with N days, all.log and audit.log begin a new file once their first record is N/5
    days old (also on a quiet node, at the hourly pass) and a rotated file goes once its
    first record is older than N days; audit.log then keeps up to 5 older files, which
    the audit page reads; backups named for an older time go, the newest 5 never; a
    mapping's log keeps at most N of its 15 days, with no floor of lines;
  * a clock set ahead while ser2netweb runs deletes nothing early (storage.now());
  * a hand-edited value out of range is read as 0 or 3650, with a load note.
No server: AppState on a temporary data folder.

Run: python3 tests/test_retention.py
"""
import asyncio
import calendar
import logging
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ser2netweb.state as state_mod
from ser2netweb.config import AppConfig, ConfigStore, MappingConfig
from ser2netweb.state import AppState, RetainingFileHandler, write_audit_record

DAY = 86400


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def write(path, *lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(f"{ln}\n" for ln in lines))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def backup_name(t):
    return time.strftime("config-%Y%m%dT%H%M%SZ.json", time.gmtime(t))


def make_data(d, now):
    """all.log (+ .1 .2 .3), audit.log (+ .1 .2), 8 backups, a mapping log: their first
    records 3 .. 21 days old."""
    write(os.path.join(d, "all.log"), f"{stamp(now - 3 * DAY)} live all.log")
    for i, days in ((1, 5), (2, 9), (3, 11)):
        write(os.path.join(d, f"all.log.{i}"), f"{stamp(now - days * DAY)} all.log.{i}",
              f"{stamp(now - (days - 1) * DAY)} newer line")
    for name, days in (("audit.log", 3), ("audit.log.1", 5), ("audit.log.2", 12)):
        write(os.path.join(d, name), f"{stamp(now - days * DAY)}\tlocal\taction\t{name}\tadmin")
    os.makedirs(os.path.join(d, "backups"), exist_ok=True)
    for days in (1, 3, 6, 9, 12, 15, 18, 21):
        write(os.path.join(d, "backups", backup_name(now - days * DAY)), "{}")


async def test_off_and_on():
    d = tempfile.mkdtemp()
    try:
        now = time.time()
        make_data(d, now)
        cfg = AppConfig()
        m = MappingConfig(name="M1")
        cfg.mappings.append(m)
        st = AppState(ConfigStore(os.path.join(d, "config.json")), cfg)
        logs = []
        st._logger.info = logs.append  # (the messages, without writing all.log)
        before = sorted(os.listdir(d)), sorted(os.listdir(os.path.join(d, "backups")))
        assert await st.run_retention() == 0
        assert (sorted(os.listdir(d)), sorted(os.listdir(os.path.join(d, "backups")))) == before
        log_path = st.mapping_log_path(m.id)
        write(log_path, *(f"{stamp(now - 5 * DAY + i)} line {i}" for i in range(1500)))
        await st.run_log_maintenance()
        assert len(read(log_path).splitlines()) == 1500, "15 days by default"

        st.config.log_max_age_days = 30  # nothing that old, but a year ahead everything
        with mock.patch("time.time", lambda: now + 365 * DAY):  # stepped ahead meanwhile
            assert await st.run_retention() == 0
        assert (sorted(os.listdir(d)), sorted(os.listdir(os.path.join(d, "backups")))) == before
        st.config.log_max_age_days = 10
        assert await st.run_retention() == 5
        # all.log: the live file (3 days, > 10/5) begun anew; .3 (11 days) shifted to .4, gone
        names = sorted(n for n in os.listdir(d) if n.startswith("all.log"))
        assert names == ["all.log", "all.log.1", "all.log.2", "all.log.3"], names
        assert read(os.path.join(d, "all.log.1")).endswith(" live all.log\n")
        assert "all.log.2" in read(os.path.join(d, "all.log.3"))
        # audit.log: begun anew, .2 (12 days) shifted to .3 and gone; the page reads .2 .1
        names = sorted(n for n in os.listdir(d) if n.startswith("audit.log"))
        assert names == ["audit.log.1", "audit.log.2"], names
        assert [r["detail"] for r in st.read_audit()] == ["audit.log", "audit.log.1"]
        # backups: 15, 18 and 21 days old go; 12 days stays (one of the newest 5)
        kept = sorted(os.listdir(os.path.join(d, "backups")), reverse=True)
        assert kept == [backup_name(now - days * DAY) for days in (1, 3, 6, 9, 12)], kept
        assert logs[-1] == ("retention: deleted 2 log file(s) (all.log.4, audit.log.3) and 3 "
                            "configuration backup(s) older than 10 days"), logs[-1:]
        assert await st.run_retention() == 0, "nothing more"
        # a mapping's log: at most 10 of its 15 days... and 3 of them, with no line floor
        st.config.log_max_age_days = 3
        await st.run_log_maintenance()
        assert read(log_path) == "", "lines older than the set period, however many"
        print("retention: off deletes nothing; N days rolls and deletes all.log / audit.log "
              "files, backups (newest 5 kept), mapping log lines; a clock ahead deletes "
              "nothing  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def test_handler_rolls_by_age():
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "all.log")
        write(path, f"{stamp(time.time() - 3 * DAY)} first")
        h = RetainingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg = logging.getLogger("ser2net.test.retention")
        lg.handlers[:] = [h]
        lg.propagate, lg.level = False, logging.INFO
        assert abs(h.first - (time.time() - 3 * DAY)) < 2, "read from the file"
        lg.info("not rolled: no period set")
        assert not os.path.exists(path + ".1")
        h.max_age_s = 10 * DAY  # a file spans 2 days at most
        lg.info("rolled")
        assert read(path + ".1").endswith("not rolled: no period set\n")
        assert read(path).endswith(" rolled\n") and abs(h.first - time.time()) < 2
        lg.info("same file")
        assert read(path).count("\n") == 2 and not os.path.exists(path + ".2")
        h.close()
        print("all.log: begins a new file once its first record is a fifth of the period old  OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_audit_chain():
    """With a period set the audit log keeps 5 older files and is begun anew by age; a
    rotation without one keeps the chain's length (its oldest file goes), never cutting
    it at .1."""
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "audit.log")
        write(path, f"{stamp(time.time() - 3 * DAY)}\tlocal\ta\tlive\tu")
        write_audit_record(path, "1.2.3.4", "b", "next", "u", max_age_s=10 * DAY)
        assert "\tlive\t" in read(path + ".1") and "\tnext\t" in read(path), "rolled by age"
        for i in range(6):
            write_audit_record(path, "1.2.3.4", "b", f"more {i}", "u", max_age_s=10 * DAY)
        assert not os.path.exists(path + ".2"), "a young file isn't rolled"
        with mock.patch.object(state_mod, "AUDIT_LOG_MAX_BYTES", 10):
            for i in range(6):  # by size, with the period: 5 older files at most
                write_audit_record(path, "1.2.3.4", "c", f"big {i}", "u", max_age_s=10 * DAY)
            assert sorted(os.listdir(d)) == ["audit.log"] + [f"audit.log.{i}" for i in
                                                             range(1, 6)], os.listdir(d)
            os.remove(path + ".5")
            os.remove(path + ".4")
            oldest = read(path + ".3")
            write_audit_record(path, "1.2.3.4", "c", "no period", "u")
        assert sorted(os.listdir(d)) == ["audit.log", "audit.log.1", "audit.log.2",
                                         "audit.log.3"], os.listdir(d)
        assert oldest not in [read(path + f".{i}") for i in (1, 2, 3)], "its oldest went"
        assert "\tbig 5\t" in read(path + ".1") and "\tno period\t" in read(path)
        print("audit.log: by age with a period, 5 older files at most; without one the "
              "chain keeps its length  OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_setting_load():
    assert AppConfig().log_max_age_days == 0 and AppConfig().to_dict()["log_max_age_days"] == 0
    for raw, got in ((-5, 0), (99999, 3650), (30, 30)):
        cfg = AppConfig.from_dict({"log_max_age_days": raw})
        assert cfg.log_max_age_days == got, (raw, cfg.log_max_age_days)
        assert any("log_max_age_days" in n for n in cfg.load_notes) == (raw != got), cfg.load_notes
    cfg.validate()
    print("log_max_age_days: 0 by default; out of range read as 0 or 3650 with a note  OK")


def test_backup_floor():
    d = tempfile.mkdtemp()
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        os.makedirs(store.backup_dir)
        now = time.time()
        for days in range(10, 0, -1):
            write(os.path.join(store.backup_dir, backup_name(now - days * 400 * DAY)), "{}")
        assert len(store.prune_backups(30 * DAY, now)) == 5
        assert len(store.backup_names()) == 5, "the newest 5 stay, however old"
        named = calendar.timegm(time.strptime(store.backup_names()[0][7:22], "%Y%m%dT%H%M%S"))
        assert abs(named - (now - 400 * DAY)) < 2
        print("backups: the newest 5 are never deleted for their age  OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    asyncio.run(test_off_and_on())
    test_handler_rolls_by_age()
    test_audit_chain()
    test_setting_load()
    test_backup_floor()
    print("\nPASS: retention of all.log, audit.log, backups and mapping logs by age")


if __name__ == "__main__":
    main()
