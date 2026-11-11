"""Event notifications (webhook + e-mail).

Checks:
  * status changes become the right events (device lost / back, start failed,
    clients connected / disconnected), none at start-up;
  * only enabled events are sent, repeats are suppressed for repeat_s; a client that
    reconnects (another port) is the same client for that; what is remembered for it
    expires after repeat_s and is bounded (MAX_KEYS);
  * payload shape per webhook format (JSON, Slack, Teams card, Discord, Telegram);
  * real delivery to a local webhook and a local SMTP server; a failing endpoint is
    reported, not raised;
  * Settings -> Notifications saves (secrets kept when left blank), validates, and
    "Save and send a test" delivers; a failed sign-in is notified;
  * with Turkish as the UI's default language the messages are Turkish, and an e-mail
    with non-ASCII text stays 7-bit (quoted-printable, encoded subject).

Run: python3 tests/test_notifications.py
"""
import asyncio
import email
import email.policy
import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from _server import ROOT, Browser, Server

from app import notify
from app.config import AdminUI, ConfigError, NotificationSettings
from app.web.auth import hash_password

RECEIVED: list = []
MAILS: list = []


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append((self.path, json.loads(body)))
        self.send_response(500 if self.path == "/fail" else 204)
        self.end_headers()

    def log_message(self, *a):
        pass


class Smtp(socketserver.StreamRequestHandler):
    """Just enough SMTP (no TLS, no auth) to receive one message."""

    def handle(self):
        w = lambda s: self.wfile.write(s.encode() + b"\r\n")  # noqa: E731
        w("220 test ESMTP")
        rcpt, data = [], None
        while True:
            line = self.rfile.readline().decode(errors="replace").strip()
            cmd = line.upper()
            if cmd.startswith(("EHLO", "HELO")):
                w("250 test")
            elif cmd.startswith("MAIL FROM"):
                w("250 ok")
            elif cmd.startswith("RCPT TO"):
                rcpt.append(line.split(":", 1)[1].strip(" <>"))
                w("250 ok")
            elif cmd == "DATA":
                w("354 go")
                lines = []
                while (ln := self.rfile.readline()) not in (b".\r\n", b""):
                    lines.append(ln.decode(errors="replace"))
                data = "".join(lines)
                MAILS.append((rcpt, data))
                w("250 queued")
            elif cmd == "QUIT":
                w("221 bye")
                return
            elif not line:
                return
            else:
                w("250 ok")


def serve(server_cls, handler):
    srv = server_cls(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_events():
    running = {"m1": {"state": "running", "device": "/dev/ttyUSB0", "clients": []}}
    names = {"m1": "PLC"}
    assert notify.status_events({}, running, names) == [], "no events at start-up"
    prev = notify.snapshot(running)
    lost = {"m1": {"state": "reconnecting", "last_error": "device removed",
                   "clients": [{"peer": "10.0.0.5:5000"}]}}
    ev = notify.status_events(prev, lost, names)
    assert ("device_lost", "m1", "PLC", "device removed") in ev, ev
    assert ("client_connected", "m1:10.0.0.5", "PLC", "10.0.0.5:5000") in ev, ev
    ev = notify.status_events(notify.snapshot(lost), running, names)
    assert [e[0] for e in ev] == ["device_back", "client_disconnected"], ev
    err = {"m1": {"state": "error", "last_error": "address in use", "clients": []}}
    assert notify.status_events(prev, err, names)[0][:1] == ("start_failed",)
    print("status changes -> device_lost / device_back / start_failed / client events  OK")


def test_repeat_keys():
    import time
    from types import SimpleNamespace

    s = NotificationSettings.from_dict({
        "enabled": True, "events": ["client_connected", "client_disconnected", "login_failed"],
        "repeat_s": 300, "webhook_url": "http://127.0.0.1:9/hook"})
    st = SimpleNamespace(config=SimpleNamespace(notifications=s, mappings=[], admin_ui=AdminUI()),
                         log=lambda m: None)
    n = notify.Notifier(st)
    names = {"m1": "PLC"}

    def clients(*peers):
        return {"m1": {"state": "running", "clients": [{"peer": p} for p in peers]}}

    prev, sent = notify.snapshot(clients()), []
    for port in range(5000, 5020):  # a client reconnecting from a new port each poll
        now = clients(f"10.0.0.5:{port}", "2001:db8::7:6000")
        sent += [ev[:2] for ev in notify.status_events(prev, now, names) if n.emit(*ev)]
        prev = notify.snapshot(now)
    assert sent == [("client_connected", "m1:10.0.0.5"), ("client_connected", "m1:2001:db8::7"),
                    ("client_disconnected", "m1:10.0.0.5")], sent
    assert len(n._last) == 3
    # what can't hold a repeat back any more is forgotten (the watcher does it every poll)
    s.repeat_s = 0
    n._forget_expired()
    assert not n._last
    s.repeat_s = 0.3
    assert n.emit("login_failed", "10.9.0.1", "", "x")
    time.sleep(0.2)
    assert n.emit("login_failed", "10.9.0.2", "", "x")
    assert not n.emit("login_failed", "10.9.0.1", "", "x"), "a repeat doesn't renew it"
    time.sleep(0.15)
    n._forget_expired()
    assert list(n._last) == [("login_failed", "10.9.0.2")], n._last
    s.repeat_s = 300
    for i in range(notify.MAX_KEYS + 50):
        n.emit("login_failed", f"10.{i // 65536}.{i // 256 % 256}.{i % 256}", "", "x")
    assert len(n._last) == notify.MAX_KEYS and ("login_failed", "10.9.0.2") not in n._last
    print("repeat interval: a reconnecting client (new port) is one client; what is "
          f"remembered expires after repeat_s, at most {notify.MAX_KEYS} keys  OK")


def test_formats_and_validation():
    for fmt, key in (("slack", "text"), ("discord", "content"), ("telegram", "chat_id"),
                     ("json", "event"), ("teams", "attachments")):
        body = notify.webhook_body(fmt, "device_lost", "PLC", "gone", "42")
        assert key in body, (fmt, body)
    card = notify.webhook_body("teams", "device_lost", "PLC", "gone")["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    for bad in ({"enabled": True}, {"enabled": True, "webhook_url": "ftp://x"},
                {"enabled": True, "webhook_url": "https://x", "webhook_format": "telegram"},
                {"enabled": True, "email_to": "ops@example.com"},
                {"enabled": True, "email_to": "nobody", "smtp_host": "h"}):
        try:
            NotificationSettings.from_dict(bad).validate()
        except ConfigError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    NotificationSettings.from_dict({"enabled": False, "webhook_url": "nonsense"}).validate()
    assert NotificationSettings.from_dict({"events": ["device_lost", "bogus"]}).events == ["device_lost"]
    print("webhook payloads (JSON/Slack/Teams/Discord/Telegram) + settings validation  OK")


async def test_delivery(hook_port, smtp_port):
    class State:
        def __init__(self):
            self.logs = []

        def log(self, m):
            self.logs.append(m)

    st = State()
    s = NotificationSettings.from_dict({
        "enabled": True, "events": ["device_lost"], "repeat_s": 300,
        "webhook_url": f"http://127.0.0.1:{hook_port}/hook", "webhook_format": "json",
        "email_to": "ops@example.com, lead@example.com", "smtp_host": "127.0.0.1",
        "smtp_port": smtp_port, "smtp_security": "none"})
    st.config = type("C", (), {"notifications": s, "mappings": [], "admin_ui": AdminUI()})()
    n = notify.Notifier(st)
    assert n.emit("device_lost", "m1", "PLC", "gone") is True
    assert n.emit("device_lost", "m1", "PLC", "gone") is False, "repeat within repeat_s"
    assert n.emit("device_back", "m1", "PLC", "") is False, "event not enabled"
    errors = await n.deliver("device_lost", "PLC", "gone")
    assert errors == [], errors
    path, body = RECEIVED[-1]
    assert path == "/hook" and body["event"] == "device_lost" and body["mapping"] == "PLC"
    rcpt, data = MAILS[-1]
    assert rcpt == ["ops@example.com", "lead@example.com"] and "serial device lost" in data
    st.config.admin_ui.language = "tr"
    errors = await n.deliver("device_lost", "Kazan dairesi ğüşıöç", "gone")
    lost = json.load(open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8"))["serial device lost"]
    assert errors == [] and lost in RECEIVED[-1][1]["text"], RECEIVED[-1:]
    raw = MAILS[-1][1]
    mail = email.message_from_string(raw, policy=email.policy.default)
    assert raw.isascii(), "the e-mail must stay 7-bit"
    assert "Kazan dairesi ğüşıöç" in mail["Subject"] and lost in mail["Subject"], mail["Subject"]
    assert "Kazan dairesi ğüşıöç" in mail.get_content()
    st.config.admin_ui.language = "auto"
    s.webhook_url = f"http://127.0.0.1:{hook_port}/fail"
    errors = await n.deliver("device_lost", "PLC", "gone")
    assert errors and "HTTP 500" in errors[0], errors
    print("delivery: webhook + SMTP received (Turkish, 7-bit e-mail); a failing webhook is "
          "reported, not raised  OK")


def test_settings_ui(hook_port):
    users = [{"username": "admin", "password_hash": hash_password("notify-pass-1"),
              "role": "admin", "pwd_version": 1}]
    with Server({"users": users}) as srv:
        b = Browser(srv.base)
        b.login("admin", "notify-pass-1")
        st, _, page = b.get("/settings")
        assert 'id="notifications"' in page
        form = {"notify_enabled": "on", "notify_event_device_lost": "on",
                "notify_event_login_failed": "on", "notify_repeat_min": "0",
                "notify_webhook_url": f"http://127.0.0.1:{hook_port}/ui",
                "notify_webhook_format": "slack", "notify_smtp_port": "587",
                "notify_smtp_security": "starttls", "then": "test"}
        st, _, page = b.post("/settings/notifications", form)
        assert "the test notification was sent" in page, page[page.find("banner"):][:300]
        assert RECEIVED[-1][0] == "/ui" and "test notification" in RECEIVED[-1][1]["text"]
        saved = srv.saved_config()["notifications"]
        assert saved["events"] == ["device_lost", "login_failed"] and saved["webhook_format"] == "slack"
        form.pop("notify_webhook_url")
        form.pop("then")
        st, _, page = b.post("/settings/notifications", form)
        assert srv.saved_config()["notifications"]["webhook_url"].endswith("/ui"), "blank URL must keep the stored one"
        st, _, page = b.post("/settings/notifications", {**form, "notify_webhook_url": "ftp://x"})
        assert "must start with https://" in page
        before = len(RECEIVED)
        Browser(srv.base).post("/login", {"username": "admin", "password": "wrong"})
        for _ in range(50):
            if len(RECEIVED) > before:
                break
            import time
            time.sleep(0.1)
        assert len(RECEIVED) > before and "failed sign-in" in RECEIVED[-1][1]["text"], RECEIVED[-1:]
        print("Settings: save (secret kept when blank), validation, test message, failed sign-in  OK")


def main():
    test_events()
    test_repeat_keys()
    test_formats_and_validation()
    hook = serve(HTTPServer, Hook)
    smtp = serve(socketserver.ThreadingTCPServer, Smtp)
    try:
        asyncio.run(test_delivery(hook.server_address[1], smtp.server_address[1]))
        test_settings_ui(hook.server_address[1])
    finally:
        hook.shutdown()
        smtp.shutdown()
    print("\nPASS: event notifications")


if __name__ == "__main__":
    main()
