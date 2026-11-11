"""What the dashboard's htmx requests answer, so the page can tell the user (no browser).

Checks:
  * the log panel of a mapping deleted meanwhile answers 286 (htmx stops its 5-second
    refresh) with a panel that says so, instead of a 404 every 5 seconds;
  * a refused or failed action answers plain text in the page's language (app.js shows
    it as it is), an ended session a 401 (app.js offers to sign in again);

Run: python3 tests/test_ui_feedback.py
"""
import json
import os

from _server import ROOT, Browser, Server, free_port

from app.web.auth import hash_password

PW = "feedback-pass-1"
with open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8") as _fh:
    TR = json.load(_fh)
USERS = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
          "pwd_version": 1},
         {"username": "view", "password_hash": hash_password(PW), "role": "viewer",
          "pwd_version": 1}]


def mapping(mid, name, **network):
    return {"id": mid, "name": name, "enabled": False, "kind": "net",
            "serial": {"port": f"/dev/ser2net-feedback-{mid}"},
            "network": dict({"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                             "port": free_port()}, **network)}


def test_deleted_mapping_log():
    with Server({"users": USERS, "mappings": [mapping("fb0000000001", "Alpha")]}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, body = b.get("/api/mappings/fb0000000001/log")
        assert st == 200 and 'hx-trigger="every 5s"' in body, (st, body[:200])
        st, _, _ = b.post("/api/mappings/fb0000000001", method="DELETE", htmx=True)
        assert st == 200
        st, _, body = b.get("/api/mappings/fb0000000001/log")
        assert st == 286, st
        assert "This mapping no longer exists" in body and "hx-trigger" not in body, body
        assert "data-cancel" in body, "the panel can still be closed"
    print("log panel of a deleted mapping: 286 (polling stops), says it is gone  OK")


def test_error_texts():
    with Server({"users": USERS, "mappings": [mapping("fb0000000002", "Beta")]}) as srv:
        admin, viewer = Browser(srv.base), Browser(srv.base)
        for b in (admin, viewer):
            b.op.addheaders = [("Accept-Language", "tr")]
        admin.login("admin", PW)
        viewer.login("view", PW)
        st, h, body = admin.post("/api/mappings/fb00000000ff/start", htmx=True)
        assert st == 404 and body == TR["Mapping not found"], (st, body)
        assert h.get("Content-Type", "").startswith("text/plain"), h.get("Content-Type")
        st, h, body = viewer.post("/api/mappings/fb0000000002/start", htmx=True)
        assert st == 403 and body == TR["Forbidden: requires the {0} role."].format("operator"), body
        st, h, body = Browser(srv.base).get("/api/status")
        assert st == 401 and json.loads(body) == {"error": "unauthorized"}, (st, body)
    print("refusals: plain text in the page's language; no session: 401  OK")


def main():
    test_deleted_mapping_log()
    test_error_texts()
    print("\nPASS: dashboard request answers")


if __name__ == "__main__":
    main()
