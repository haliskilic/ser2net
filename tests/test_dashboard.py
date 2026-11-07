"""Dashboard: unchanged table answers 204, bulk start/stop/restart, trace files.

Checks:
  * GET /api/status returns X-Content-Hash; asking again with that hash gets 204 (the
    browser keeps its table — no flicker), a change gets the new table;
  * POST /api/mappings/bulk applies start/stop/restart to every selected mapping
    (operator+; viewers 403; bad input 400) and audits each;
  * the trace panel lists a mapping's files and downloads only those (no other
    mapping's file, no path tricks); the table offers Trace only when tracing is on;
    traces record what is sent to the device, so viewers neither get the button nor
    the files (403);
  * the dashboard has the search / filter / sort / bulk toolbar.

Run: python3 tests/test_dashboard.py
"""
import os

from _server import Browser, Server, free_port

from app.web.auth import hash_password

PW = "dash-pass-1"


def m(mid, name, **opts):
    return {"id": mid, "name": name, "enabled": True, "kind": "net",
            "serial": {"port": f"/dev/ser2net-dash-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": free_port()}, "options": opts}


def main():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin", "pwd_version": 1},
             {"username": "view", "password_hash": hash_password(PW), "role": "viewer", "pwd_version": 1}]
    maps = [m("aaaaaaaaaaa1", "Alpha", trace_both="alpha.log"), m("aaaaaaaaaaa2", "Beta"),
            m("aaaaaaaaaaa3", "Gamma")]
    with Server({"users": users, "mappings": maps}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, page = b.get("/")
        assert "data-mapping-tools" in page and "data-filter-text" in page and "data-bulk-action" in page
        st, h, body = b.get("/api/status")
        digest = h.get("X-Content-Hash")
        assert st == 200 and digest and 'data-mid="aaaaaaaaaaa1"' in body and 'class="row-select"' in body
        assert body.count(">Trace</button>") == 1, "Trace only for the mapping that traces"
        b.op.addheaders = [("X-Content-Hash", digest)]
        st, h, body = b.get("/api/status")
        assert st == 204 and body == "", (st, body[:80])
        print("table refresh: same content -> 204 (no swap), hash in X-Content-Hash  OK")

        st, _, _ = b.post("/api/mappings/bulk", {"action": "stop", "ids": ["aaaaaaaaaaa1", "aaaaaaaaaaa3"]}, htmx=True)
        assert st == 200
        en = {x["name"]: x["enabled"] for x in srv.saved_config()["mappings"]}
        assert en == {"Alpha": False, "Beta": True, "Gamma": False}, en
        st, _, body = b.get("/api/status")
        assert st == 200, "a change must send the new table even with the old hash"
        b.op.addheaders = []
        st, _, _ = b.post("/api/mappings/bulk", {"action": "start", "ids": ["aaaaaaaaaaa1", "aaaaaaaaaaa3"]}, htmx=True)
        assert all(x["enabled"] for x in srv.saved_config()["mappings"])
        assert b.post("/api/mappings/bulk", {"action": "explode", "ids": ["aaaaaaaaaaa1"]}, htmx=True)[0] == 400
        assert b.post("/api/mappings/bulk", {"action": "stop"}, htmx=True)[0] == 400
        v = Browser(srv.base)
        v.login("view", PW)
        assert v.post("/api/mappings/bulk", {"action": "stop", "ids": ["aaaaaaaaaaa1"]}, htmx=True)[0] == 403
        st, _, page = b.get("/settings/audit?action=mapping_stop")
        assert "2 record(s)" in page, page[page.find("record(s)") - 30:][:80]
        print("bulk: stop / start on the selection, audited per mapping; viewer 403, bad input 400  OK")

        tdir = os.path.join(srv.data, "traces")
        for name, text in (("aaaaaaaaaaa1-alpha.log", "line now\n"), ("aaaaaaaaaaa1-alpha.log.1", "older\n"),
                           ("aaaaaaaaaaa2-secret.log", "other mapping\n")):
            with open(os.path.join(tdir, name), "w", encoding="utf-8", newline="") as fh:
                fh.write(text)  # byte-exact (no \r\n on Windows): the download must match
        st, _, page = b.get("/api/mappings/aaaaaaaaaaa1/traces")
        assert st == 200 and "aaaaaaaaaaa1-alpha.log.1" in page and "secret" not in page, page[:400]
        st, h, body = b.get("/api/mappings/aaaaaaaaaaa1/traces/aaaaaaaaaaa1-alpha.log")
        assert st == 200 and body == "line now\n" and "attachment" in h.get("Content-Disposition", "")
        for bad in ("aaaaaaaaaaa2-secret.log", "..%2Fconfig.json", "%2E%2E%2F%2E%2E%2Fall.log"):
            assert b.get(f"/api/mappings/aaaaaaaaaaa1/traces/{bad}")[0] == 404, bad
        print("traces: listed per mapping, downloadable, other files / traversal 404  OK")
        st, _, body = v.get("/api/status")
        assert st == 200 and ">Trace</button>" not in body and ">Monitor</button>" in body
        for path in ("/api/mappings/aaaaaaaaaaa1/traces",
                     "/api/mappings/aaaaaaaaaaa1/traces/aaaaaaaaaaa1-alpha.log",
                     "/api/mappings/aaaaaaaaaaa1/traces/no-such-file"):
            st, _, body = v.get(path)
            assert st == 403 and "requires the operator role" in body, (path, st, body[:80])
        print("traces: viewers get no Trace button, and the list and files answer 403  OK")
    print("\nPASS: dashboard (204 refresh, bulk actions, trace files)")


if __name__ == "__main__":
    main()
