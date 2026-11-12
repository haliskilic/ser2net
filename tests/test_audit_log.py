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
    export with formula-looking cells neutralised;
  * the search finds Turkish names whatever their letter case or accents ('istanbul'
    finds 'İSTANBUL', 'dairesi' 'DAİRESİ'), and the dashboard rows carry the same folded
    text for the browser's filter;
  * the CSV for Excel (?excel=1) is UTF-16 with a BOM and tabs, the default CSV keeps
    its bytes (UTF-8, no BOM, commas);
  * the mappings export (it holds MQTT passwords) and the audit CSV export are
    recorded; a request refused before its handler (no session, CSRF) gets an all.log
    line - one per address and answer a minute, the next says how many more - and the
    security headers.

Run: python3 tests/test_audit_log.py
"""
import csv
import io
import logging
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port

from app.config import AppConfig, ConfigStore, MappingConfig
from app.state import AppState
from app.web.auth import hash_password

PW = "audit-pass-1"


def records(page: str) -> int:
    """The record count the audit page states."""
    m = re.search(r"(\d+) record", page)
    assert m, page[:300]
    return int(m.group(1))


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
        st, _, _ = op.post("/api/mappings/save", {
            "name": "KAZAN DAİRESİ İSTANBUL", "kind": "net", "network_mode": "server",
            "network_protocol": "raw", "network_bind_ip": "127.0.0.1",
            "network_port": str(free_port()), "serial_port": "/dev/ser2net-audit-ışık"}, htmx=True)
        assert st == 200, st
        assert op.get("/settings/audit")[0] == 403 and op.get("/settings/audit.csv")[0] == 403

        admin = Browser(srv.base)
        admin.login("admin", PW)
        st, _, page = admin.get("/settings")
        assert 'href="/settings/audit"' in page
        st, _, page = admin.get("/settings/audit")
        assert st == 200 and "login_failed" in page and "mapping_save" in page, page[:400]
        st, _, page = admin.get("/settings/audit?user=op1&action=mapping_save")
        assert records(page) == 2 and "HYPERLINK" in page, page[page.find("record") - 40:][:200]
        for q in ("istanbul", "dairesi", "kazan daİresİ", "İstanbul"):
            st, _, page = admin.get("/settings/audit?action=mapping_save&q=" + urllib.parse.quote(q))
            assert records(page) == 1 and "KAZAN DAİRESİ İSTANBUL" in page, (q, records(page))
        assert records(admin.get("/settings/audit?q=istasyon")[2]) == 0
        st, _, body = admin.get("/api/status")
        row = re.search(r'<tr data-mid="[^"]*" data-name="KAZAN DAİRESİ İSTANBUL"[^>]*>', body)
        assert row and 'data-search="kazan dairesi istanbul /dev/ser2net-audit-isik ' in row.group(0), \
            (row and row.group(0))
        print("search: Turkish names found in any letter case and without accents, "
              "the dashboard rows carry the folded text  OK")
        st, _, page = admin.get("/settings/audit?q=nothing-matches-this")
        assert records(page) == 0 and "match the filter" in page
        st, h, body = admin.get("/settings/audit.csv?user=op1")
        assert st == 200 and "text/csv" in h.get("Content-Type", "")
        rows = list(csv.reader(io.StringIO(body)))
        assert rows[0] == ["time", "ip", "user", "action", "detail"]
        acts = {r[3]: r for r in rows[1:]}
        assert set(acts) >= {"login", "login_failed", "mapping_save"}, acts.keys()
        assert acts["mapping_save"][4].startswith("'="), "formula cell not neutralised"
        st, h, raw = admin.get("/settings/audit.csv?user=op1", raw=True)
        assert not raw.startswith(b"\xef\xbb\xbf") and raw.decode("utf-8") == body, \
            "the default file stays UTF-8 without a BOM, comma-separated (scripts read it)"
        print("UI: admin-only page with search / filters, CSV export (formula cells defused)  OK")

        # the file for Excel: UTF-16 with a BOM, tab-separated, the same rows; the page
        # links it with the page's filters
        st, _, page = admin.get("/settings/audit?user=op1")
        assert 'href="/settings/audit.csv?user=op1&amp;excel=1"' in page, page[:2000]
        assert 'href="/settings/audit.csv?excel=1"' in admin.get("/settings/audit")[2]
        st, h, raw = admin.get("/settings/audit.csv?user=op1&excel=1", raw=True)
        assert st == 200 and raw.startswith(b"\xff\xfe"), raw[:20]
        assert "charset=utf-16" in h.get("Content-Type", "") and \
            "ser2net-audit-excel.csv" in h.get("Content-Disposition", ""), dict(h)
        text = raw.decode("utf-16")
        assert list(csv.reader(io.StringIO(text), delimiter="\t")) == rows, "the same rows"
        assert "KAZAN DAİRESİ İSTANBUL" in text and "\t" in text and "," not in text.split("\r\n")[0]
        print("Excel file: UTF-16 + BOM, tab-separated, the same rows (Turkish letters kept), "
              "linked with the filters  OK")

        st, h, _ = op.get("/settings/config/export")
        assert st == 200 and "attachment" in h.get("Content-Disposition", "")
        st, _, body = admin.get("/settings/audit.csv")
        rows = {r[3]: r for r in csv.reader(io.StringIO(body))}
        assert rows["config_export"][2] == "op1" and rows["config_export"][4] == "1 mappings", rows
        admin.get("/settings/audit.csv?action=login")
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = fh.read()
        assert "\taudit_export\t" in audit and "records (action=login)\tadmin\n" in audit, audit
        print("exports: the mappings export and the audit CSV export are recorded  OK")

        stranger = Browser(srv.base)
        for _ in range(3):
            st, h, _ = stranger.get("/settings")
            assert st == 303 and h.get("X-Frame-Options") == "DENY", (st, dict(h))
        st, h, _ = stranger.post("/api/mappings/save", {"name": "x"})
        assert st == 401 and h.get("X-Content-Type-Options") == "nosniff", st
        st, _, _ = op.post("/api/mappings/save", {"name": "x"}, htmx=True)
        assert st == 400, st   # op's request reached its handler (logged as before)
        csrf = urllib.request.Request(srv.base + "/api/mappings/save", data=b"name=x",
                                      method="POST", headers={"X-CSRF-Token": "forged"})
        assert op._open(csrf)[0] == 403
        with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
            log = fh.read()
        assert log.count("HTTP 127.0.0.1 GET /settings -> 303") == 1, log[-2000:]
        assert log.count("HTTP 127.0.0.1 POST /api/mappings/save -> 401") == 1, log[-2000:]
        assert log.count("HTTP 127.0.0.1 POST /api/mappings/save -> 403") == 1, log[-2000:]
        assert "HTTP 127.0.0.1 POST /api/mappings/save -> 400" in log
        print("refused before the handler (no session, CSRF): logged once a minute per address "
              "and answer, with the security headers  OK")
    print("\nPASS: audit log (users, injection-safe, viewer, CSV)")


if __name__ == "__main__":
    main()
