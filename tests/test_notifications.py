"""Event notifications (webhook + e-mail).

Checks:
  * status changes become the right events (device lost / back, start failed,
    clients connected / disconnected), none at start-up;
  * only enabled events are sent, repeats are suppressed for repeat_s;
  * payload shape per webhook format (JSON, Slack, Teams card, Discord, Telegram);
  * real delivery to a local webhook and a local SMTP server; a failing endpoint is
    reported, not raised;
  * Settings -> Notifications saves (secrets kept when left blank), validates, and
    "Save and send a test" delivers; a failed sign-in is notified;
  * with Turkish as the UI's default language the messages are Turkish, the test message
    wholly (title and text), and an e-mail with non-ASCII text stays 7-bit
    (quoted-printable, encoded subject);
  * a channel that failed is logged in English and shown in the page's language;
  * an SMTP user name or password with Turkish letters signs in (UTF-8 AUTH PLAIN, LOGIN
    or CRAM-MD5; smtplib alone fails before sending anything), ASCII ones as before; a
    webhook URL with Turkish letters is sent IDNA / percent-encoded.

Run: python3 tests/test_notifications.py
"""
import asyncio
import base64
import email
import email.policy
import hmac
import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from _server import ROOT, Browser, Server

from app import i18n, notify
from app.config import AdminUI, ConfigError, NotificationSettings
from app.web.auth import hash_password

RECEIVED: list = []
MAILS: list = []
LOGINS: list = []   # (mechanism, user, password) the SMTP stub accepted
TR = json.load(open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8"))


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append((self.path, json.loads(body)))
        self.send_response(500 if self.path == "/fail" else 204)
        self.end_headers()

    def log_message(self, *a):
        pass


class Smtp(socketserver.StreamRequestHandler):
    """Just enough SMTP (no TLS) to receive one message; AUTH with the mechanisms the
    server's `auth` attribute names (none: no AUTH), credentials decoded as UTF-8."""

    def handle(self):
        w = lambda s: self.wfile.write(s.encode() + b"\r\n")  # noqa: E731
        r = lambda: self.rfile.readline().decode().strip()  # noqa: E731
        b64 = lambda s: base64.b64decode(s).decode("utf-8")  # noqa: E731
        w("220 test ESMTP")
        rcpt, data = [], None
        mechs = getattr(self.server, "auth", "")
        while True:
            line = self.rfile.readline().decode(errors="replace").strip()
            cmd = line.upper()
            if cmd.startswith(("EHLO", "HELO")):
                w(f"250-test\r\n250 AUTH {mechs}" if mechs else "250 test")
            elif cmd.startswith("AUTH ") and line.split()[1].upper() in mechs.split():
                mech, arg = line.split()[1].upper(), (line.split() + [""])[2]
                if mech == "PLAIN":
                    _authz, user, pw = b64(arg).split("\0")
                elif mech == "LOGIN":
                    user = b64(arg) if arg else (w("334 VXNlcm5hbWU6") or b64(r()))
                    w("334 UGFzc3dvcmQ6")
                    pw = b64(r())
                else:   # CRAM-MD5: the stub knows one password
                    challenge = b"<1.2@test>"
                    w("334 " + base64.b64encode(challenge).decode())
                    user, digest = b64(r()).rsplit(" ", 1)
                    pw = self.server.password
                    if digest != hmac.HMAC(pw.encode(), challenge, "md5").hexdigest():
                        w("535 bad digest")
                        continue
                LOGINS.append((mech, user, pw))
                w("235 ok")
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
    assert ("client_connected", "m1:10.0.0.5:5000", "PLC", "10.0.0.5:5000") in ev, ev
    ev = notify.status_events(notify.snapshot(lost), running, names)
    assert [e[0] for e in ev] == ["device_back", "client_disconnected"], ev
    err = {"m1": {"state": "error", "last_error": "address in use", "clients": []}}
    assert notify.status_events(prev, err, names)[0][:1] == ("start_failed",)
    print("status changes -> device_lost / device_back / start_failed / client events  OK")


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
    # the test message wholly in the notification language: its title and its text
    errors = await n.deliver("test", "", "")
    text = RECEIVED[-1][1]["text"]
    assert errors == [] and TR["test notification"] in text and \
        TR["If you can read this, ser2net can reach you."] in text, text
    st.config.admin_ui.language = "auto"
    errors = await n.deliver("test", "", "")
    assert "test notification" in RECEIVED[-1][1]["text"] and "If you can read this" in RECEIVED[-1][1]["text"]
    s.webhook_url = f"http://127.0.0.1:{hook_port}/fail"
    errors = await n.deliver("device_lost", "PLC", "gone")
    assert errors and str(errors[0]) == "webhook (json) failed: HTTP 500 Internal Server Error", \
        "the log line stays English"
    tok = i18n.set_language("tr")
    try:
        shown = errors[0].shown()
    finally:
        i18n.reset(tok)
    assert shown == TR["Webhook ({0}) failed: {1}"].format("json", "HTTP 500 Internal Server Error"), shown
    print("delivery: webhook + SMTP received (Turkish, 7-bit e-mail; the test message in the "
          "notification language); a failing webhook is reported (log English, page Turkish), "
          "not raised  OK")


async def test_non_ascii(hook_port, smtp):
    """Turkish letters in the SMTP user name / password and in the webhook URL."""
    class State:
        def log(self, m):
            pass
    st = State()
    s = NotificationSettings.from_dict({
        "enabled": True, "events": ["device_lost"],
        "webhook_url": f"http://127.0.0.1:{hook_port}/kanal/bakım-ğüşıöç?ad=İstanbul&x=%C3%A7",
        "email_to": "ops@example.com", "smtp_host": "127.0.0.1",
        "smtp_port": smtp.server_address[1], "smtp_security": "none",
        "smtp_username": "operatör", "smtp_password": "Güvenli1!ş"})
    st.config = type("C", (), {"notifications": s, "mappings": [], "admin_ui": AdminUI()})()
    n = notify.Notifier(st)
    for mechs in ("PLAIN LOGIN", "LOGIN", "CRAM-MD5"):
        smtp.auth, smtp.password = mechs, s.smtp_password
        errors = await n.deliver("device_lost", "PLC", "gone")
        assert errors == [], [str(e) for e in errors]
        assert LOGINS[-1] == (mechs.split()[0], "operatör", "Güvenli1!ş"), LOGINS[-1:]
    path = RECEIVED[-1][0]
    assert path == "/kanal/bak%C4%B1m-%C4%9F%C3%BC%C5%9F%C4%B1%C3%B6%C3%A7?ad=%C4%B0stanbul&x=%C3%A7", path
    smtp.auth = "PLAIN"
    s.smtp_username, s.smtp_password = "ops", "ascii-pass"   # smtplib's own login, as before
    assert await n.deliver("device_lost", "PLC", "gone") == [] and LOGINS[-1] == ("PLAIN", "ops", "ascii-pass")
    smtp.auth, smtp.password = "CRAM-MD5", "another"   # refused: said so, in the page's language
    s.smtp_username, s.smtp_password = "operatör", "Güvenli1!ş"
    errors = await n.deliver("device_lost", "PLC", "gone")
    tok = i18n.set_language("tr")
    try:
        shown = errors[-1].shown()
    finally:
        i18n.reset(tok)
    assert str(errors[-1]).startswith("e-mail via 127.0.0.1:") and "535" in str(errors[-1])
    assert TR["the server did not accept the user name or password (SMTP {0})"].format(535) in shown \
        and TR["— they have letters outside ASCII, which some servers don't take"] in shown, shown

    u = notify.ascii_url
    assert u("https://hooks.example.com/a%20b?x=1") == "https://hooks.example.com/a%20b?x=1", "ASCII as it is"
    assert u("https://Bücher.example:8443/şş?q=İ#frag") == "https://xn--bcher-kva.example:8443/%C5%9F%C5%9F?q=%C4%B0"
    assert u("http://[::1]:8080/ş") == "http://[::1]:8080/%C5%9F"
    assert u(u("https://ışık.example/ğ?a=b&c=%41")) == u("https://ışık.example/ğ?a=b&c=%41"), "idempotent"
    print("non-ASCII: SMTP sign-in with Turkish letters (PLAIN, LOGIN, CRAM-MD5; ASCII via "
          "smtplib), refused sign-in explained; webhook URL IDNA / percent-encoded  OK")


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
        # a failed test, on a Turkish page: the channel's failure in Turkish
        b.op.addheaders = [("Accept-Language", "tr")]
        st, _, page = b.post("/settings/notifications", {
            **form, "notify_webhook_url": f"http://127.0.0.1:{hook_port}/fail", "then": "test"})
        b.op.addheaders = []
        want = TR["Webhook ({0}) failed: {1}"].format("slack", "HTTP 500 Internal Server Error")
        assert want in page, page[page.find("banner"):][:300]
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
    test_formats_and_validation()
    hook = serve(HTTPServer, Hook)
    smtp = serve(socketserver.ThreadingTCPServer, Smtp)
    try:
        asyncio.run(test_delivery(hook.server_address[1], smtp.server_address[1]))
        asyncio.run(test_non_ascii(hook.server_address[1], smtp))
        test_settings_ui(hook.server_address[1])
    finally:
        hook.shutdown()
        smtp.shutdown()
    print("\nPASS: event notifications")


if __name__ == "__main__":
    main()
