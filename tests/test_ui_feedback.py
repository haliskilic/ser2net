"""What the dashboard's htmx requests answer, so the page can tell the user (no browser).

Checks:
  * the log panel of a mapping deleted meanwhile answers 286 (htmx stops its 5-second
    refresh) with a panel that says so, instead of a 404 every 5 seconds;
  * a refused or failed action answers plain text in the page's language (app.js shows
    it as it is), an ended session a 401 (app.js offers to sign in again);
  * a successful action says what it did in its X-Notice header (percent-encoded JSON,
    app.js shows it): save, start / stop / restart, bulk, copy, delete — as a warning
    when a mapping did not start, with the reason, which also goes to its own log; a
    save names the row button that gets the focus when the form closes (a new
    mapping's too); the answer itself stays as it was (status, empty body, HX-Trigger);
  * every self-refreshing panel — the ports, the fleet view, a mapping's log, like the
    mappings table — answers 204 while the browser shows its content already;
  * Settings → Users: each role selector and row button names its user;
  * a refused sign-in (401, 429) and a refused first-run setup fill in the user name
    typed, not "admin" again; a plain sign-in page still offers "admin";
  * a page request without a session goes to /login?next=<that page> (not for the
    dashboard, a form post or /api); the sign-in leads back there, also after a failed
    try and across the language switch; a next that is not a page here leads to "/";

Run: python3 tests/test_ui_feedback.py
"""
import json
import os
import re
import socket
import time
import urllib.parse

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


def note(headers):
    """The X-Notice of an answer: (kind, text), or None."""
    raw = headers.get("X-Notice")
    if raw is None:
        return None
    d = json.loads(urllib.parse.unquote(raw))
    return d["kind"], d["text"]


def detail(headers):
    """The X-Notice's tooltip (the raw error behind a translated reason), or ""."""
    return json.loads(urllib.parse.unquote(headers.get("X-Notice"))).get("detail", "")


def focus(headers):
    """The element the X-Notice gives the focus to when the form panel closes."""
    return json.loads(urllib.parse.unquote(headers.get("X-Notice"))).get("focus")


def test_notices():
    busy = socket.socket()   # a port in use: a mapping that listens on it can't start
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    maps = [mapping("fb0000000003", "Gamma"), mapping("fb0000000004", "Delta", port=port)]
    try:
        with Server({"users": USERS, "mappings": maps}) as srv:
            b = Browser(srv.base)
            b.login("admin", PW)
            form = {"id": "fb0000000003", "name": "Gamma", "kind": "net",
                    "serial_port": "/dev/ser2net-feedback-g", "serial_baudrate": "9600",
                    "network_mode": "server", "network_protocol": "raw",
                    "network_bind_ip": "127.0.0.1", "network_port": str(free_port())}
            st, h, body = b.post("/api/mappings/save", form, htmx=True)
            assert st == 200 and body == "" and h.get("HX-Trigger") == "refreshMappings"
            assert note(h) == ("ok", "“Gamma” saved."), note(h)
            assert focus(h) == "m-fb0000000003-edit", "the focus goes back to the row"
            st, h, _ = b.post("/api/mappings/save", {**form, "id": "", "name": "Zeta",
                                                     "serial_port": "/dev/ser2net-feedback-z",
                                                     "network_port": str(free_port())}, htmx=True)
            new = next(m["id"] for m in srv.saved_config()["mappings"] if m["name"] == "Zeta")
            assert note(h) == ("ok", "“Zeta” saved.") and focus(h) == f"m-{new}-edit", \
                "a new mapping: the focus to its row (no new form: that would add it twice)"

            st, h, _ = b.post("/api/mappings/fb0000000003/start", htmx=True)
            assert st == 200 and note(h) == ("ok", "“Gamma” started."), note(h)
            st, h, _ = b.post("/api/mappings/fb0000000003/restart", htmx=True)
            assert note(h) == ("ok", "“Gamma” restarted."), note(h)
            st, h, _ = b.post("/api/mappings/fb0000000003/stop", htmx=True)
            assert note(h) == ("ok", "“Gamma” stopped."), note(h)

            st, h, _ = b.post("/api/mappings/fb0000000004/start", htmx=True)
            kind, text = note(h)
            assert st == 200 and kind == "warn" and text.startswith("“Delta” could not start: "), text
            # what the error means, in the page's language; the error itself as its tooltip
            assert text.endswith("The port is already in use by another program (or another "
                                 "mapping)."), text
            assert "already in use" in detail(h).lower() and "Errno" in detail(h), detail(h)
            st, _, log = b.get("/api/mappings/fb0000000004/log")
            assert "start failed: " in log, "the reason belongs in the mapping's own log too"
            st, h, _ = b.post("/api/mappings/save", {**form, "id": "fb0000000004", "name": "Delta",
                                                     "serial_port": "/dev/ser2net-feedback-d",
                                                     "enabled": "on", "network_port": str(port)},
                              htmx=True)
            kind, text = note(h)
            assert st == 200 and kind == "warn" and \
                text.startswith("“Delta” saved, but it could not start: "), (kind, text)

            st, h, body = b.post("/api/mappings/bulk", {"action": "restart",
                                                        "ids": ["fb0000000003", "fb0000000004"]},
                                 htmx=True)
            assert st == 200 and h.get("HX-Trigger") == "refreshMappings", (st, body)
            assert note(h) == ("warn", "Restarted 1 of 2 selected mappings. Could not start: "
                                       "Delta."), note(h)
            st, h, _ = b.post("/api/mappings/bulk", {"action": "stop",
                                                     "ids": ["fb0000000003", "fb0000000004"]},
                              htmx=True)
            assert note(h) == ("ok", "Stopped 2 of 2 selected mappings."), note(h)

            st, h, _ = b.post("/api/mappings/fb0000000003/duplicate", htmx=True)
            assert st == 200 and note(h) == (
                "ok", "Copied as “Gamma (copy)” (disabled: it uses the same serial port)."), note(h)
            st, h, _ = b.post("/api/mappings/fb0000000003", method="DELETE", htmx=True)
            assert st == 200 and note(h) == ("ok", "“Gamma” deleted."), note(h)

            # in Turkish, with the mapping's name as it is
            b.op.addheaders = [("Accept-Language", "tr")]
            st, h, _ = b.post("/api/mappings/fb0000000004/stop", htmx=True)
            assert note(h) == ("ok", TR["“{0}” stopped."].format("Delta")), note(h)
    finally:
        busy.close()
    print("action results: X-Notice says what was done, warns with the reason when a "
          "mapping did not start (also in its log)  OK")


def test_unchanged_panels():
    cfg = {"users": USERS, "mappings": [mapping("fb0000000005", "Epsilon")],
           "cluster": {"enabled": True, "key": "feedback-cluster-key-1", "discovery_port": free_port()}}
    with Server(cfg) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        for path in ("/api/ports/table", "/api/cluster/status"):
            st, h, body = b.get(path)
            digest = h.get("X-Content-Hash")
            assert st == 200 and digest and body, (path, st)
            b.op.addheaders = [("X-Content-Hash", digest)]
            st, h, body = b.get(path)
            b.op.addheaders = []
            assert st == 204 and body == "", (path, st, body[:80])
            assert b.get(path)[0] == 200, "without the hash: the panel again"
        # the log panel carries its hash in the panel (each refresh replaces it)
        st, _, body = b.get("/api/mappings/fb0000000005/log")
        digest = re.search(r'data-hash="([0-9a-f]{16})"', body).group(1)
        b.op.addheaders = [("X-Content-Hash", digest)]
        assert b.get("/api/mappings/fb0000000005/log")[0] == 204
        b.post("/api/mappings/fb0000000005/start", htmx=True)   # a new log line
        for _ in range(50):
            st, _, body = b.get("/api/mappings/fb0000000005/log")
            if st == 200:
                break
            time.sleep(0.1)
        assert st == 200 and f'data-hash="{digest}"' not in body, "a new line: the new log"
    print("ports, fleet view and log panels: 204 while unchanged  OK")


def test_users_table_names():
    with Server({"users": USERS}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, page = b.get("/settings")
        assert st == 200
        i = page.index('<span id="user-2-name">view</span>')
        row = page[i:page.index("</tr>", i)]
        assert 'aria-label="Role of view"' in row, row
        assert row.count('aria-describedby="user-2-name"') == 3, "Set, Sign out, Delete"
    print("users table: the role selector and the row buttons name their user  OK")


def username_field(page):
    m = re.search(r'<input type="text" name="username" value="([^"]*)"', page)
    return m and m.group(1)


def test_typed_username_kept():
    with Server({"users": USERS}) as srv:
        b = Browser(srv.base)
        assert username_field(b.get("/login")[2]) == "admin"
        st, _, page = b.post("/login", {"username": "view", "password": "wrong"})
        assert st == 401 and username_field(page) == "view", (st, username_field(page))
        st, _, page = b.post("/login", {"username": 'a"><b>x', "password": "wrong"})
        assert username_field(page) == "a&#34;&gt;&lt;b&gt;x", "escaped"
        for _ in range(8):
            st, _, page = b.post("/login", {"username": "view", "password": "wrong"})
        assert st == 429 and username_field(page) == "view", (st, username_field(page))
    with Server() as srv:   # first-run setup
        b = Browser(srv.base)
        assert username_field(b.get("/setup")[2]) == "admin"
        st, _, page = b.post("/setup", {"username": "boss", "password": "long-enough-1",
                                        "password2": "something-else"})
        assert st == 400 and username_field(page) == "boss", (st, username_field(page))
    print("sign-in and setup: a refused form keeps the user name typed  OK")


def test_back_after_sign_in():
    with Server({"users": USERS, "mappings": [mapping("fb0000000006", "Eta")]}) as srv:
        b = Browser(srv.base)
        st, h, _ = b.get("/settings/audit?q=x&action=login")
        assert st == 303 and h.get("Location") == \
            "/login?next=%2Fsettings%2Faudit%3Fq%3Dx%26action%3Dlogin", h.get("Location")
        assert b.get("/")[1].get("Location") == "/login", "the dashboard: a plain sign-in page"
        st, h, _ = b.post("/settings/access", {"require_login": "on"})
        assert st == 303 and h.get("Location") == "/login", "a form post: no way back to it"
        assert b.get("/api/mappings/fb0000000006/log")[0] == 401

        nxt = "/settings/audit?q=x&action=login"
        st, _, page = b.get("/login?" + urllib.parse.urlencode({"next": nxt}))
        hidden = '<input type="hidden" name="next" value="/settings/audit?q=x&amp;action=login">'
        assert st == 200 and hidden in page, page[page.find("<form"):][:400]
        lang = re.search(r'href="(/lang/tr\?next=[^"]+)"', page).group(1)
        st, h, _ = b.get(lang.replace("&amp;", "&"))
        assert h.get("Location") == "/login?" + urllib.parse.urlencode({"next": nxt}), \
            ("the language switch keeps the way back", h.get("Location"))
        st, _, page = b.post("/login", {"username": "admin", "password": "wrong", "next": nxt})
        assert st == 401 and hidden in page, "a failed try keeps it"
        st, h, _ = b.post("/login", {"username": "admin", "password": PW, "next": nxt})
        assert st == 303 and h.get("Location") == nxt, h.get("Location")
        for bad in ("//evil.example/", "/\\evil.example/", "https://evil.example/",
                    "/\t/evil.example/", "javascript:alert(1)", "settings"):
            c = Browser(srv.base)
            st, _, page = c.get("/login?" + urllib.parse.urlencode({"next": bad}))
            assert 'name="next"' not in page, bad
            st, h, _ = c.post("/login", {"username": "admin", "password": PW, "next": bad})
            assert st == 303 and h.get("Location") == "/", (bad, h.get("Location"))
    print("sign-in: back to the page whose session ended (a local one only)  OK")


def main():
    test_deleted_mapping_log()
    test_error_texts()
    test_notices()
    test_unchanged_panels()
    test_users_table_names()
    test_typed_username_kept()
    test_back_after_sign_in()
    print("\nPASS: dashboard request answers")


if __name__ == "__main__":
    main()
