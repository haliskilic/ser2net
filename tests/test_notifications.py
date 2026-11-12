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
  * with Turkish as the UI's default language the messages are Turkish, the test message
    wholly (title and text), and an e-mail with non-ASCII text stays 7-bit
    (quoted-printable, encoded subject);
  * a channel that failed is logged in English and shown in the page's language;
  * an SMTP user name or password with Turkish letters signs in (UTF-8 AUTH PLAIN, LOGIN
    or CRAM-MD5; smtplib alone fails before sending anything), ASCII ones as before; a
    webhook URL with Turkish letters is sent IDNA / percent-encoded;
  * personal data: with mask_personal_data a client's address goes out as its network
    (IPv4 /24, IPv6 /48; the repeat key stays the address) and a failed sign-in without
    the account name; with email_separately each recipient gets a message of their own;
    Discord pings no one; the Settings boxes are kept by a form that doesn't have them;
  * a mapping name with a line break still gets its e-mail (the subject is one line, no
    header can be added through it), and a failed delivery's log line names the event
    and the mapping;
  * a webhook that redirects is followed but reported (log, test result): the message
    may not have arrived; Settings warns that SMTP security "none" with a user name
    sends the password unencrypted.

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
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from _server import ROOT, Browser, Server

from ser2netweb import i18n, notify
from ser2netweb.config import (
    NOTIFY_DEFAULT_EVENTS, NOTIFY_EVENTS, AdminUI, ConfigError, NotificationSettings,
)
from ser2netweb.web.auth import hash_password

RECEIVED: list = []
MAILS: list = []
LOGINS: list = []   # (mechanism, user, password) the SMTP stub accepted
TR = json.load(open(os.path.join(ROOT, "ser2netweb", "locale", "tr.json"), encoding="utf-8"))


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):  # http.server API
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append((self.path, json.loads(body)))
        if self.path == "/moved":  # as a receiver that moved (or answers a POST so) does
            self.send_response(302)
            self.send_header("Location", "/moved-here?token=secret")
            self.send_header("Content-Length", "0")
        else:
            self.send_response(500 if self.path == "/fail" else 204)
        self.end_headers()

    def do_GET(self):  # http.server API; where a redirect leads
        RECEIVED.append((self.path, None))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class Smtp(socketserver.StreamRequestHandler):
    """Just enough SMTP (no TLS) to receive messages; AUTH with the mechanisms the
    server's `auth` attribute names (none: no AUTH), credentials decoded as UTF-8."""

    def handle(self):
        w = lambda s: self.wfile.write(s.encode() + b"\r\n")
        r = lambda: self.rfile.readline().decode().strip()
        b64 = lambda s: base64.b64decode(s).decode("utf-8")
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
            elif cmd.startswith("MAIL FROM"):  # a new message (also in the same session)
                rcpt = []
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


def test_unit_events():
    """A polled Modbus unit: unit_offline when it stops answering, unit_back when it answers
    again; nothing while the serial port is down (device_lost / device_back say that), and its
    return is still reported after a restart of the mapping (units unknown again)."""
    names = {"gw": "Line 3"}

    def status(state="running", **units):
        return {"gw": {"state": state, "clients": [], "units": [
            {"unit": int(n[1:]), "state": st, "cause": cause, "since": None, "errors": 0}
            for n, (st, cause) in units.items()]}}
    on, silent, port = ("online", ""), ("offline", "no reply"), ("offline", "port down")
    unknown = ("unknown", "")
    steps = [  # (status, events expected against the previous one)
        (status(u1=unknown, u2=unknown), []),
        (status(u1=on, u2=on), []),
        (status(u1=on, u2=silent), [("unit_offline", "gw:unit2", "Line 3", "unit 2")]),
        (status(u1=on, u2=silent), []),
        (status(u1=on, u2=on), [("unit_back", "gw:unit2", "Line 3", "unit 2")]),
        # the port goes away: device_lost, the units go offline because of it, come back
        (status("reconnecting", u1=on, u2=on), ["device_lost"]),
        (status("reconnecting", u1=port, u2=port), []),
        (status(u1=port, u2=port), ["device_back"]),
        (status(u1=on, u2=port), []),
        # the port came back but unit 2 is silent: that is reported now
        (status(u1=on, u2=silent), [("unit_offline", "gw:unit2", "Line 3", "unit 2")]),
        # a save restarts the mapping (units unknown), then unit 2 answers
        (status(u1=unknown, u2=unknown), []),
        (status(u1=on, u2=on), [("unit_back", "gw:unit2", "Line 3", "unit 2")]),
        # a unit that is silent from the start (or after a restart) is reported
        (status(u1=unknown, u2=unknown, u5=unknown), []),
        (status(u1=on, u2=on, u5=silent), [("unit_offline", "gw:unit5", "Line 3", "unit 5")]),
    ]
    prev = notify.snapshot(steps[0][0])
    for st, expected in steps[1:]:
        events = notify.status_events(prev, st, names)
        whole = bool(expected) and isinstance(expected[0], tuple)  # else: the event names
        assert [e if whole else e[0] for e in events] == expected, (st, events)
        prev = notify.snapshot(st, prev)
    # a status without units (an older runner) and an older 2-tuple snapshot still work
    assert notify.status_events({"gw": ("running", set())}, status(u2=silent), names)[0][0] \
        == "unit_offline"
    assert notify.status_events(notify.snapshot(status(u2=silent)),
                                {"gw": {"state": "running", "clients": []}}, names) == []
    assert "unit_offline" in NOTIFY_EVENTS and "unit_offline" not in NOTIFY_DEFAULT_EVENTS
    assert "unit_back" not in NotificationSettings().events, "new events are opt-in"
    assert NotificationSettings.from_dict({"events": ["unit_back"]}).events == ["unit_back"]
    print("Modbus units: unit_offline / unit_back (not for a port outage; across a restart); "
          "opt-in  OK")


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


def test_personal_data():
    from types import SimpleNamespace

    for peer, want in (("10.0.5.77:5000", "10.0.5.x"), ("10.0.5.77", "10.0.5.x"),
                       ("2001:db8:5:1::7:6000", "2001:db8:5::/48"),   # "ip:port", no brackets
                       ("2001:db8:5:1:2:3:4:5:6000", "2001:db8:5::/48"),
                       ("[2001:db8:7::1]:5000", "2001:db8:7::/48"),
                       ("::ffff:10.1.2.3", "10.1.2.x"), ("::1:4001", "::/48"),
                       ("plc-hall3:9000", "plc-hall3:9000"), ("", "")):
        assert notify.mask_address(peer) == want, (peer, notify.mask_address(peer), want)
    off, on = NotificationSettings(), NotificationSettings(mask_personal_data=True)
    assert notify.sign_in_detail(off, "alice", "10.0.0.9") == "user 'alice' from 10.0.0.9"
    assert notify.sign_in_detail(on, "alice", "10.0.0.9") == "user (name left out) from 10.0.0.x"
    assert notify.sign_in_detail(off, "bob", "10.0.0.9", "LDAP", "the name of a local account") \
        == "LDAP user 'bob' from 10.0.0.9: the name of a local account"
    assert notify.sign_in_detail(on, "bob", "2001:db8::1", "OIDC", "it is empty") \
        == "OIDC user (name left out) from 2001:db8::/48: it is empty"
    assert notify.sign_in_detail(off, "", "10.0.0.9", "OIDC", "state mismatch") \
        == "OIDC sign-in from 10.0.0.9: state mismatch", "an SSO answer without a name"
    s = NotificationSettings.from_dict({
        "enabled": True, "events": ["client_connected", "login_failed"], "repeat_s": 300,
        "webhook_url": "http://127.0.0.1:9/hook", "mask_personal_data": True})
    st = SimpleNamespace(config=SimpleNamespace(notifications=s, mappings=[], admin_ui=AdminUI()),
                         log=lambda m: None)
    n = notify.Notifier(st)
    assert n.emit("client_connected", "m1:10.0.0.5", "PLC", "10.0.0.5:5000")
    assert not n.emit("client_connected", "m1:10.0.0.5", "PLC", "10.0.0.5:5001"), "a repeat"
    assert n.emit("client_connected", "m1:10.0.0.6", "PLC", "10.0.0.6:5000"), \
        "another client of the same network is another client"
    assert n.emit("login_failed", "10.0.0.5", "", "user (name left out) from 10.0.0.x")
    queued = [n._queue.get_nowait() for _ in range(3)]
    assert queued == [("client_connected", "PLC", "10.0.0.x"),
                      ("client_connected", "PLC", "10.0.0.x"),
                      ("login_failed", "", "user (name left out) from 10.0.0.x")], queued
    assert ("client_connected", "m1:10.0.0.5") in n._last, n._last
    s.mask_personal_data = False
    assert n.emit("client_connected", "m1:10.0.0.7", "PLC", "10.0.0.7:5000")
    assert n._queue.get_nowait()[2] == "10.0.0.7:5000", "off: the address as it is"
    # older configs keep today's payloads; text in a hand-edited privacy switch keeps it on
    # (a protective switch: on unless empty, with a load note; an edit refuses it), the
    # e-mail switch takes only JSON true
    loaded = NotificationSettings.from_dict({})
    assert loaded.mask_personal_data is False and loaded.email_separately is False
    for raw, masked in (("yes", True), ("false", True), ("", False)):
        notes = []
        loaded = NotificationSettings.from_dict(
            {"mask_personal_data": raw, "email_separately": "yes"}, notes, "notifications.")
        assert loaded.mask_personal_data is masked and loaded.email_separately is False, raw
        kinds = {n.path: n.kind for n in notes}
        assert kinds == {"notifications.mask_personal_data": "switch_text",
                         "notifications.email_separately": "invalid"}, notes
        assert "use true or false" in notes[0].error(), notes[0].error()
    assert NotificationSettings.from_dict({"email_separately": 1}).email_separately is False
    body = notify.webhook_body("discord", "start_failed", "@everyone", "x")
    assert body["allowed_mentions"] == {"parse": []} and "@everyone" in body["content"], body
    print("personal data: addresses as their network (IPv4 /24, IPv6 /48, repeat key "
          "unchanged), no account name; off by default; Discord pings no one  OK")


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
    st.config = type("C", (), {"notifications": s, "mappings": [], "admin_ui": AdminUI(),
                               "instance_id": "0123456789abcdef"})()
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
    assert "To: ops@example.com, lead@example.com" in data, data
    s.email_separately = True   # one message per recipient, each with only its own To:
    before = len(MAILS)
    assert await n.deliver("device_lost", "PLC", "gone") == []
    got = MAILS[before:]
    assert [r for r, _d in got] == [["ops@example.com"], ["lead@example.com"]], got
    for (r, d) in got:
        mail = email.message_from_string(d, policy=email.policy.default)
        assert mail["To"] == r[0] and "serial device lost" in mail["Subject"], d
    s.email_separately = False
    st.config.admin_ui.language = "tr"
    errors = await n.deliver("device_lost", "Kazan dairesi ğüşıöç", "gone")
    lost = json.load(open(os.path.join(ROOT, "ser2netweb", "locale", "tr.json"), encoding="utf-8"))["serial device lost"]
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
    # a line break in a mapping name: the e-mail still goes, its subject on one line
    errors = await n.deliver("device_lost", "PLC\r\nBcc: x@example.net\u2028\x85", "gone")
    assert errors == [], errors
    mail = email.message_from_string(MAILS[-1][1], policy=email.policy.default)
    assert mail["Subject"].startswith("ser2net ") and "PLC  Bcc: x@example.net" in mail["Subject"], \
        mail["Subject"]
    assert mail["Bcc"] is None and "\n" not in mail["Subject"], mail.items()
    assert RECEIVED[-1][1]["mapping"] == "PLC\r\nBcc: x@example.net\u2028\x85", "JSON keeps it"
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
    # a failed delivery from the queue: its log line names the event and the mapping
    worker = asyncio.create_task(n._worker())  # (the first emit above is still queued)
    assert n.emit("device_lost", "m2", "Pompa-2", "gone") is True
    for _ in range(100):
        if len(st.logs) >= 2:
            break
        await asyncio.sleep(0.05)
    worker.cancel()
    assert st.logs[-1] == ("notifications: webhook (json) failed: HTTP 500 Internal Server "
                           "Error (device_lost, mapping 'Pompa-2')"), st.logs
    # a redirect is followed (as a GET), and reported: the message may not have arrived
    s.webhook_url = f"http://127.0.0.1:{hook_port}/moved"
    warnings: list = []
    errors = await n.deliver("device_lost", "PLC", "gone", warnings=warnings)
    assert errors == [] and RECEIVED[-1] == ("/moved-here?token=secret", None), RECEIVED[-2:]
    assert warnings == [f"webhook (json) was redirected to http://127.0.0.1:{hook_port}: if the "
                        "message didn't arrive, set the address it redirects to"], warnings
    assert await n.deliver("device_lost", "PLC", "gone") == [], "a warning is no error"
    # a URL that isn't http(s) (a config.json tolerated on load) is never opened
    sent = len(RECEIVED)
    for url in (f"file://{os.path.abspath(__file__)}", "ftp://127.0.0.1/x"):
        s.webhook_url = url
        errors = await n.deliver("device_lost", "PLC", "gone")
        assert errors and "https://" in str(errors[0]), [str(e) for e in errors]
    assert len(RECEIVED) == sent
    print("delivery: webhook + SMTP received (Turkish, 7-bit e-mail; the test message in the "
          "notification language); a line break in a mapping name can't stop its e-mail or add "
          "a header; a failing webhook is reported (log English with event and mapping, page "
          "Turkish), not raised; a redirect is reported; a URL that isn't http(s) is never "
          "opened  OK")


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
    st.config = type("C", (), {"notifications": s, "mappings": [], "admin_ui": AdminUI(),
                               "instance_id": "0123456789abcdef"})()
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
        assert 'name="notify_event_unit_offline"' in page and "Modbus unit answering again" in page
        form = {"notify_enabled": "on", "notify_event_device_lost": "on",
                "notify_event_login_failed": "on", "notify_event_unit_back": "on",
                "notify_repeat_min": "0",
                "notify_webhook_url": f"http://127.0.0.1:{hook_port}/ui",
                "notify_webhook_format": "slack", "notify_smtp_port": "587",
                "notify_smtp_security": "starttls", "then": "test"}
        st, _, page = b.post("/settings/notifications", form)
        assert "the test notification was sent" in page, page[page.find("banner"):][:300]
        assert RECEIVED[-1][0] == "/ui" and "test notification" in RECEIVED[-1][1]["text"]
        saved = srv.saved_config()["notifications"]
        assert saved["events"] == ["device_lost", "login_failed", "unit_back"], saved["events"]
        assert saved["webhook_format"] == "slack"
        form.pop("notify_webhook_url")
        form.pop("then")
        st, _, page = b.post("/settings/notifications", form)
        assert srv.saved_config()["notifications"]["webhook_url"].endswith("/ui"), "blank URL must keep the stored one"
        st, _, page = b.post("/settings/notifications", {**form, "notify_webhook_url": "ftp://x"})
        assert "must start with https://" in page
        # a redirecting webhook: sent, with a warning in the test result
        st, _, page = b.post("/settings/notifications", {
            **form, "notify_webhook_url": f"http://127.0.0.1:{hook_port}/moved", "then": "test"})
        assert "the test notification was sent, but: webhook (slack) was redirected to " \
            f"http://127.0.0.1:{hook_port}:" in page, page[page.find("banner"):][:400]
        assert "moved-here" not in page, "only the redirect's scheme and host are shown"
        # SMTP security "none" with a user name: the password goes unencrypted
        hint = "the user name and password go to the mail server unencrypted"
        assert hint not in page
        st, _, page = b.post("/settings/notifications", {
            **form, "notify_smtp_security": "none", "notify_smtp_username": "mailer",
            "notify_smtp_host": "127.0.0.1", "notify_email_to": "ops@example.com"})
        assert hint in page, page[page.find("E-mail"):][:2000]
        st, _, page = b.post("/settings/notifications", {
            **form, "notify_smtp_security": "none",
            "notify_webhook_url": f"http://127.0.0.1:{hook_port}/ui"})
        assert hint not in page, "no user name, no password sent"
        # a failed test, on a Turkish page: the channel's failure in Turkish
        b.op.addheaders = [("Accept-Language", "tr")]
        st, _, page = b.post("/settings/notifications", {
            **form, "notify_webhook_url": f"http://127.0.0.1:{hook_port}/fail", "then": "test"})
        b.op.addheaders = []
        want = TR["Webhook ({0}) failed: {1}"].format("slack", "HTTP 500 Internal Server Error")
        assert want in page, page[page.find("banner"):][:300]
        st, _, page = b.post("/settings/notifications", {**form, "notify_repeat_min": "inf"})
        assert st == 400 and "repeat interval must be numbers" in page, (st, page[:300])
        before = len(RECEIVED)
        Browser(srv.base).post("/login", {"username": "admin", "password": "wrong"})
        for _ in range(50):
            if len(RECEIVED) > before:
                break
            time.sleep(0.1)
        assert len(RECEIVED) > before and "failed sign-in" in RECEIVED[-1][1]["text"], RECEIVED[-1:]
        assert "user 'admin' from 127.0.0.1" in RECEIVED[-1][1]["text"], RECEIVED[-1]
        print("Settings: save (secret kept when blank), validation, test message (a redirect "
              "warned about), SMTP 'none' + user name hint, failed sign-in  OK")

        # the personal-data boxes: hints next to the events that carry personal data
        _, _, page = b.get("/settings")
        assert "sends the client’s IP address" in page and 'name="notify_privacy_fields"' in page
        st, _, page = b.post("/settings/notifications", {
            **form, "notify_privacy_fields": "1", "notify_mask_personal_data": "on",
            "notify_email_separately": "on"})
        saved = srv.saved_config()["notifications"]
        assert saved["mask_personal_data"] is True and saved["email_separately"] is True, saved
        b.post("/settings/notifications", form)   # a page from before: no boxes, both kept
        saved = srv.saved_config()["notifications"]
        assert saved["mask_personal_data"] is True and saved["email_separately"] is True, saved
        _, _, page = b.get("/settings")
        assert 'name="notify_mask_personal_data" checked' in page, "box not shown ticked"
        before = len(RECEIVED)
        Browser(srv.base).post("/login", {"username": "admin", "password": "wrong-again"})
        for _ in range(50):
            if len(RECEIVED) > before:
                break
            time.sleep(0.1)
        assert "user (name left out) from 127.0.0.x" in RECEIVED[-1][1]["text"], RECEIVED[-1]
        assert "admin" not in RECEIVED[-1][1]["text"].split("\n", 1)[1], RECEIVED[-1]
        b.post("/settings/notifications", {**form, "notify_privacy_fields": "1"})
        saved = srv.saved_config()["notifications"]
        assert saved["mask_personal_data"] is False and saved["email_separately"] is False, saved
        print("Settings: personal data left out of a failed sign-in's message when asked; "
              "a form without the boxes keeps them  OK")


def main():
    test_events()
    test_repeat_keys()
    test_unit_events()
    test_formats_and_validation()
    test_personal_data()
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
