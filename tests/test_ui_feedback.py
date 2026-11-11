"""What the dashboard's htmx requests answer, so the page can tell the user (no browser).

Checks:
  * the log panel of a mapping deleted meanwhile answers 286 (htmx stops its 5-second
    refresh) with a panel that says so, instead of a 404 every 5 seconds;

Run: python3 tests/test_ui_feedback.py
"""
from _server import Browser, Server, free_port

from app.web.auth import hash_password

PW = "feedback-pass-1"


def mapping(mid, name, **network):
    return {"id": mid, "name": name, "enabled": False, "kind": "net",
            "serial": {"port": f"/dev/ser2net-feedback-{mid}"},
            "network": dict({"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                             "port": free_port()}, **network)}


def test_deleted_mapping_log():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    with Server({"users": users, "mappings": [mapping("fb0000000001", "Alpha")]}) as srv:
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


def main():
    test_deleted_mapping_log()
    print("\nPASS: dashboard request answers")


if __name__ == "__main__":
    main()
