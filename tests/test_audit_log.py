"""Audit log: who did what, readable in the UI, safe against forged lines.

Checks:
  * records carry the acting user (5th column, appended so 2.7 lines still parse),
    sign-ins and failed sign-ins are recorded, REST API / cluster calls say so;
  * control characters in a field (a mapping name with a newline) can't create an
    extra, forged record;
  * the same holds for all.log / stdout and a mapping's own log: a message is one line
    (control characters become spaces, terminal escapes included), a traceback after it
    stays as it is;
  * Settings -> Audit log (admin only): search, action/user filters, paging, CSV
    export with formula-looking cells neutralised.

Run: python3 tests/test_audit_log.py
"""
import csv
import io
import logging
import os
import shutil
import tempfile

from _server import Browser, Server, free_port

from app.config import AppConfig, ConfigStore, MappingConfig
from app.state import AppState
from app.web.auth import hash_password

PW = "audit-pass-1"


def test_records_and_injection():
    d = tempfile.mkdtemp(prefix="s2n_audit_")
    st = AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig())
    with open(st.audit_path, "w", encoding="utf-8") as fh:  # a line as 2.7 wrote it
        fh.write("2026-11-01 10:00:00\t10.0.0.1\tmapping_save\tPLC-1\n")
    st.audit("10.0.0.2", "mapping_save", "evil\n2026-11-04 00:00:00\t6.6.6.6\tuser_delete\tadmin",
             user="op1")
    st.audit("10.0.0.3", "login", "admin, local", user="admin")
    with open(st.audit_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 3, lines
    recs = st.read_audit()
    assert [r["action"] for r in recs] == ["login", "mapping_save", "mapping_save"]
    assert recs[0]["user"] == "admin" and recs[2]["user"] == "", recs
    assert "6.6.6.6" in recs[1]["detail"] and recs[1]["user"] == "op1", recs[1]
    assert not any(r["ip"] == "6.6.6.6" for r in recs), "a forged record got in"
    print("records: user column appended (2.7 lines still parse), newline can't forge a record  OK")


def _close_loggers():
    # AppState's FileHandlers keep the logs open (Windows can't remove the folder then)
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def test_log_lines():
    d = tempfile.mkdtemp(prefix="s2n_logline_")
    try:
        st = AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig())
        forged = "2026-11-04 00:00:00 login: admin (admin, local) from 6.6.6.6"
        st.log(f"saved\r\n{forged}\x1b[2J\u2028\x85\x9b31m\tend")
        log = st.make_mapping_logger(MappingConfig(id="m1", name="PLC\n1"))
        log(f"point 'x\u2029y' failed\n{forged}")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("ser2net").exception("fatal\nerror")
        with open(st.log_path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        assert not any(ln.startswith(forged[:19]) for ln in lines), "a forged line got in"
        assert any(ln.endswith(f"saved  {forged} [2J   31m end") for ln in lines), lines
        assert any(ln.endswith(f"[PLC 1] point 'x y' failed {forged}") for ln in lines), lines
        assert not any("\x1b" in ln or "\x9b" in ln or "\u2028" in ln for ln in lines)
        assert any(ln.endswith(" fatal error") for ln in lines), lines
        assert "Traceback (most recent call last):" in lines and "RuntimeError: boom" in lines
        with open(st.mapping_log_path("m1"), encoding="utf-8") as fh:
            own = fh.read().split("\n")
        assert len(own) == 2 and own[0].endswith(f" point 'x y' failed {forged}"), own
        assert st.read_mapping_log("m1") == own[:1]
        print("all.log / stdout / mapping log: one line per message (no forged line, no "
              "terminal escape); a traceback stays below its line  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_records_and_injection()
    test_log_lines()
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "op1", "password_hash": hash_password(PW), "role": "operator", "pwd_version": 1}]
    with Server({"users": users}) as srv:
        op = Browser(srv.base)
        st, _, _ = op.post("/login", {"username": "op1", "password": "wrong-password"})
        assert st == 401
        op.login("op1", PW)
        st, _, _ = op.post("/api/mappings/save", {
            "name": "=HYPERLINK(\"x\")", "kind": "net", "network_mode": "server",
            "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
            "network_port": str(free_port()), "serial_port": "/dev/ser2net-audit-test"}, htmx=True)
        assert st == 200, st
        assert op.get("/settings/audit")[0] == 403 and op.get("/settings/audit.csv")[0] == 403

        admin = Browser(srv.base)
        admin.login("admin", PW)
        st, _, page = admin.get("/settings")
        assert 'href="/settings/audit"' in page
        st, _, page = admin.get("/settings/audit")
        assert st == 200 and "login_failed" in page and "mapping_save" in page, page[:400]
        st, _, page = admin.get("/settings/audit?user=op1&action=mapping_save")
        assert "1 record(s)" in page and "HYPERLINK" in page, page[page.find("record(s)") - 40:][:200]
        st, _, page = admin.get("/settings/audit?q=nothing-matches-this")
        assert "0 record(s)" in page and "match the filter" in page
        st, h, body = admin.get("/settings/audit.csv?user=op1")
        assert st == 200 and "text/csv" in h.get("Content-Type", "")
        rows = list(csv.reader(io.StringIO(body)))
        assert rows[0] == ["time", "ip", "user", "action", "detail"]
        acts = {r[3]: r for r in rows[1:]}
        assert set(acts) >= {"login", "login_failed", "mapping_save"}, acts.keys()
        assert acts["mapping_save"][4].startswith("'="), "formula cell not neutralised"
        print("UI: admin-only page with search / filters, CSV export (formula cells defused)  OK")
    print("\nPASS: audit log (users, injection-safe, viewer, CSV)")


if __name__ == "__main__":
    main()
