"""Audit log: who did what, readable in the UI, safe against forged lines.

Checks:
  * records carry the acting user (5th column, appended so 2.7 lines still parse),
    sign-ins and failed sign-ins are recorded, REST API / cluster calls say so;
  * control characters in a field (a mapping name with a newline) can't create an
    extra, forged record;
  * Settings -> Audit log (admin only): search, action/user filters, paging, CSV
    export with formula-looking cells neutralised.

Run: python3 tests/test_audit_log.py
"""
import csv
import io
import os
import tempfile

from _server import Browser, Server, free_port

from app.config import AppConfig, ConfigStore
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


def main():
    test_records_and_injection()
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
