"""Upgrading a v2.8.0 install: its config.json and data folder keep working, nothing is lost.

tests/fixtures/config-2.8.0.json was written by the real v2.8.0 code
(tests/make_config_fixture.py): the 2.7.1 fixture as 2.8.0 upgraded it, with every setting
2.8.0 added switched on (notifications by webhook and e-mail with dummy secrets, the UI
language, Sparkplug B, coil / discrete-input points, writable points, trace rotation) and an
account made by 2.8.0. This test:
  * loads it with the current code: it validates, nothing is noted, and every key/value
    2.8.0 wrote is still there after a save (EXPECTED_CHANGES: none);
  * boots the real server on it the Docker/headless way (SER2NET_BIND_IP re-saves the
    config), in a data folder laid out as 2.8.0 leaves one (built here: audit.log and
    audit.log.1 with 4-column 2.7 and 5-column 2.8 records, backups named
    config-<UTC>.json and config-<UTC>-2.json, a mapping's log, traces with .1 / .2
    rotations): the untouched 2.8.0 file is kept in data/backups/ next to the old backups,
    the old passwords and the API token work, the UI speaks the configured language, and
    the audit log, the traces and the mapping log are read as 2.8.0 wrote them;
  * restores a backup 2.8.0 wrote (with its accounts), then the untouched 2.8.0 file: the
    mappings, the notifications and the language come back exactly as 2.8.0 wrote them.

Run: python3 tests/test_upgrade_from_2_8.py
"""
import copy
import csv
import io
import json
import os
import shutil
import tempfile
import time
import urllib.request

from _server import Browser, Server, free_port
from test_upgrade_from_2_7 import walk

from ser2netweb.config import AppConfig, ConfigStore

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "config-2.8.0.json")
FIXTURE_2_7 = os.path.join(HERE, "fixtures", "config-2.7.1.json")
# what the current version changes in a 2.8.0 config on purpose (path -> new value)
EXPECTED_CHANGES: dict = {}
ACCOUNTS = (("admin", "old-admin-pass-1"), ("op1", "old-op-pass-1"),
            ("viewer1", "old-viewer-pass-1"), ("op28", "new-in-2.8.0-pass-1"))
TOKEN = "s2n_upgrade-from-2.7.1"
PLC, GATEWAY = "aaaa00000001", "aaaa00000006"
# data/ as 2.8.0 leaves it (names and formats of that release)
OLD_BACKUP = "config-20261015T080000Z.json"     # written by 2.7.1, before the upgrade
BACKUP = "config-20261104T093000Z.json"         # 2.8.0: before the UDP sensor was added
BACKUP_2 = "config-20261104T093000Z-2.json"     # 2.8.0: a second save in the same second
AUDIT_1 = [  # audit.log.1, rotated: 2.7 records (time, ip, action, detail)
    "2026-10-15 08:00:00\t10.0.0.6\tmapping_save\tPLC-1 raw (rotated, 2.7)",
]
AUDIT = [    # audit.log: a 2.7 record, then 2.8.0's (time, ip, action, detail, user)
    "2026-10-20 07:59:00\t10.0.0.7\tlogin\tadmin (2.7)",
    "2026-11-04 09:30:00\t10.0.0.8\tmapping_save\tUDP sensor (2.8.0)\top1",
    "2026-11-04 09:31:00\t10.0.0.9\tnotifications_save\twebhook + e-mail (2.8.0)\tadmin",
]
TRACES = {   # traces/<id>-<name>[.N]: the current file and two rotations; age in hours
    f"{PLC}-plc1.trace": (b"net>ser hello (current)\n", 0),
    f"{PLC}-plc1.trace.1": (b"net>ser hello (rotated once)\n", 2),
    f"{PLC}-plc1.trace.2": (b"net>ser hello (rotated twice)\n", 4),
    f"{GATEWAY}-modbus.log.1": (b"ser>net 01 03 02 00 2a (rotated)\n", 1),
}
MAPPING_LOG = "2026-11-04 09:30:05 serial error: [Errno 2] could not open port (2.8.0 log)"


def test_roundtrip(original):
    cfg = AppConfig.from_dict(copy.deepcopy(original))
    cfg.validate()
    assert cfg.load_notes == [], f"loading a 2.8.0 config noted: {cfg.load_notes}"
    saved = json.loads(json.dumps(cfg.to_dict()))
    diffs = {p: n for p, _o, n in walk(original, saved)}
    assert diffs == EXPECTED_CHANGES, f"unexpected changes: {diffs}"
    print("load + validate + save: every 2.8.0 value kept, nothing noted  OK")


def dump(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False)  # as 2.8.0's ConfigStore writes


def make_data_folder(data: str, boot: dict) -> None:
    for sub in ("backups", "logs", "traces"):
        os.makedirs(os.path.join(data, sub))
    with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as fh:
        fh.write(dump(boot))
    with open(FIXTURE_2_7, encoding="utf-8") as fh:
        old = fh.read()
    without_udp = copy.deepcopy(boot)
    without_udp["mappings"] = [m for m in boot["mappings"] if m["name"] != "UDP sensor"]
    bridge_off = copy.deepcopy(boot)
    next(m for m in bridge_off["mappings"] if m["kind"] == "serialbridge")["enabled"] = False
    for name, text in ((OLD_BACKUP, old), (BACKUP, dump(without_udp)),
                       (BACKUP_2, dump(bridge_off))):
        with open(os.path.join(data, "backups", name), "w", encoding="utf-8") as fh:
            fh.write(text)
    for name, lines in (("audit.log.1", AUDIT_1), ("audit.log", AUDIT)):
        with open(os.path.join(data, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(f"{ln}\n" for ln in lines))
    with open(os.path.join(data, "logs", f"{PLC}.log"), "w", encoding="utf-8") as fh:
        fh.write(MAPPING_LOG + "\n")
    with open(os.path.join(data, "all.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-11-04 09:30:00 ser2net 2.8.0 starting\n")
    now = time.time()
    for name, (content, hours) in TRACES.items():
        path = os.path.join(data, "traces", name)
        with open(path, "wb") as fh:
            fh.write(content)
        os.utime(path, (now - hours * 3600, now - hours * 3600))


def audit_rows(admin) -> list[dict]:
    st, h, body = admin.get("/settings/audit.csv")
    assert st == 200 and "text/csv" in h.get("Content-Type", ""), st
    return list(csv.DictReader(io.StringIO(body)))


def check_audit(admin) -> None:
    rows = audit_rows(admin)
    by_detail = {r["detail"]: r for r in rows}
    for line in AUDIT_1 + AUDIT:
        fields = line.split("\t") + [""]
        want = {"time": fields[0], "ip": fields[1], "action": fields[2], "detail": fields[3],
                "user": fields[4]}
        got = by_detail.get(want["detail"])
        assert got is not None, f"audit record missing: {line!r}"
        assert {k: got[k] for k in want} == want, (got, want)
    planted = [ln.split("\t")[3] for ln in AUDIT_1 + AUDIT][::-1]  # newest first
    assert [r["detail"] for r in rows][-len(planted):] == planted, \
        "the records of audit.log, then audit.log.1, newest first, after the new ones"
    assert any(r["action"] == "login" and r["user"] == "admin" for r in rows), \
        "no new record (a sign-in) was appended after the 2.8.0 ones"
    st, _, page = admin.get("/settings/audit?user=op1")
    # (the page breaks long details at dots, so the records are told apart by their IPs)
    assert st == 200 and "10.0.0.8" in page, page
    assert not any(ip in page for ip in ("10.0.0.6", "10.0.0.7", "10.0.0.9")), page
    print("audit log: 4-column (2.7) and 5-column (2.8.0) records of audit.log and "
          "audit.log.1 read, new ones appended  OK")


def check_traces_and_log(op) -> None:
    st, _, page = op.get(f"/api/mappings/{PLC}/traces")
    assert st == 200, st
    for name in TRACES:
        if name.startswith(PLC):
            assert name in page, f"{name} not listed"
    for name, (content, _hours) in TRACES.items():
        mid = name.split("-", 1)[0]
        st, _, body = op.get(f"/api/mappings/{mid}/traces/{name}", raw=True)
        assert st == 200 and body == content, (name, st, body[:80])
    st, _, page = op.get(f"/api/mappings/{PLC}/log")
    assert st == 200 and "could not open port (2.8.0 log)" in page, page[:500]
    print("traces: the 2.8.0 files and their .1 / .2 rotations listed and downloaded; the "
          "mapping's 2.8.0 log shown  OK")


def restore(admin, srv, name: str) -> dict:
    st, _, page = admin.post("/settings/backups/restore", {"name": name, "accounts": "on"})
    assert st == 200, (name, st, page[:500])
    return srv.saved_config()


def main():
    with open(FIXTURE, encoding="utf-8") as fh:
        original = json.load(fh)
    test_roundtrip(original)

    boot = copy.deepcopy(original)
    for m in boot["mappings"]:  # free ports, so the listeners can't clash on this machine
        if m["network"]["mode"] in ("server", "udp"):
            m["network"]["port"] = free_port()
    root = tempfile.mkdtemp(prefix="s2n_up28_")
    try:
        srv = Server(data_dir=os.path.join(root, "data"))
        # the address the server is started with (SER2NET_PORT) is saved at start
        boot["admin_ui"]["port"] = srv.port
        make_data_folder(srv.data, boot)
        with srv:
            log = srv.output()
            assert "Traceback" not in log and "is not a valid mapping id" not in log, log[-2000:]
            saved = srv.saved_config()
            diffs = {p: n for p, _o, n in walk(boot, saved)}
            assert diffs == EXPECTED_CHANGES, f"the re-save at start changed: {diffs}"
            store = ConfigStore(srv.config_path)
            names = store.backup_names()
            kept = [n for n in names if n in (OLD_BACKUP, BACKUP, BACKUP_2)]
            assert kept == [BACKUP_2, BACKUP, OLD_BACKUP], names  # newest first, -2 after
            new = [n for n in names if n not in (OLD_BACKUP, BACKUP, BACKUP_2)]
            assert dump(boot) in [store.read_backup(n) for n in new], \
                f"the untouched 2.8.0 config.json is not among the new backups {new}"
            print("boot (re-save): every 2.8.0 value kept, the 2.8.0 file backed up next to "
                  "the old backups (incl. a -2 one)  OK")

            for user, pw in ACCOUNTS:
                Browser(srv.base).login(user, pw)
            req = urllib.request.Request(srv.base + "/api/v1/mappings",
                                         headers={"Authorization": f"Bearer {TOKEN}"})
            body = json.loads(urllib.request.urlopen(req, timeout=10).read())
            assert len(body["mappings"]) == 7, body
            st, _, page = Browser(srv.base).get("/login")
            assert st == 200 and '<html lang="tr">' in page, page[:300]
            print("old passwords (admin/operator/viewer, an account made by 2.8.0) and the API "
                  "token work; the UI's default language (tr) applies  OK")

            admin = Browser(srv.base)
            admin.login("admin", "old-admin-pass-1")
            check_audit(admin)
            op = Browser(srv.base)
            op.login("op1", "old-op-pass-1")
            check_traces_and_log(op)
            st, _, page = admin.get("/settings")
            assert st == 200 and all(n in page for n in (BACKUP, BACKUP_2, OLD_BACKUP))

            restored = restore(admin, srv, BACKUP_2)
            bridge = next(m for m in restored["mappings"] if m["kind"] == "serialbridge")
            assert bridge["enabled"] is False and len(restored["mappings"]) == 7, restored
            untouched = next(n for n in new if store.read_backup(n) == dump(boot))
            restored = restore(admin, srv, untouched)
            for key in ("mappings", "notifications", "admin_ui", "ldap", "oidc", "cluster"):
                diffs = {p: n for p, _o, n in walk(boot[key], restored[key])}
                assert diffs == {}, f"restoring the 2.8.0 file changed {key}: {diffs}"
            Browser(srv.base).login("op28", "new-in-2.8.0-pass-1")
            print("restore: a backup 2.8.0 wrote (-2), then the untouched 2.8.0 file, with "
                  "their accounts: mappings, notifications, language, LDAP/OIDC/cluster "
                  "back as 2.8.0 wrote them  OK")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\nPASS: upgrade from a v2.8.0 config and data folder")


if __name__ == "__main__":
    main()
