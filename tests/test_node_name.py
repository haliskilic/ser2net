"""A name for each instance: Settings -> Admin interface -> Name of this instance
(admin_ui.node_name, or SER2NETWEB_NODE_NAME for one run).

Instances on one host share its host name, which was all notifications, the cluster's
fleet view and the browser tab had to tell them apart by. Checks:
  * config.json: optional ('' = the host name, as before); a hand-edited value that is too
    long, has control characters / line breaks or isn't text is dropped with a load note
    (no new validate() rule); SER2NETWEB_NODE_NAME wins while it is usable, a start-up warning
    says why not otherwise;
  * notifications: "ser2netweb <name>: …" in the title and text, the JSON webhook's "node"
    (plus "hostname" and "instance_id" for programs), e-mail From display name
    ("ser2netweb <name>", never in the address) and signature; without a name exactly as
    before; a name doesn't break e-mail (EmailMessage refuses CR/LF in a Subject);
  * the cluster: the beacon's and the node payload's "name" (no new field);
  * the web UI: the signed-in pages' title and top bar (not the sign-in page); Settings
    saves / refuses / logs / audits it, shows the environment's read-only, a form without
    the field keeps it, the restore preview lists it;
  * a real start logs "ser2netweb starting as '<name>'".

Calls the web app in this process; the last part starts one server.
Run: python3 tests/test_node_name.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
from email.message import EmailMessage
from types import SimpleNamespace

from _asgi import App, cookies, header
from _server import Server

from ser2netweb import notify, runtime
from ser2netweb.config import AdminUI, AppConfig, node_label, node_name
from ser2netweb.engine.cluster import ClusterDiscovery
from ser2netweb.web import auth

PW = "node-name-pass-1"
CSRF = "node-name-csrf"
HOST = socket.gethostname()
USERS = [{"username": "admin", "password_hash": auth.hash_password(PW), "role": "admin",
          "pwd_version": 1}]


def test_config() -> None:
    cfg = AppConfig.from_dict({})
    assert cfg.admin_ui.node_name == "" and cfg.to_dict()["admin_ui"]["node_name"] == ""
    assert node_name(cfg.admin_ui, {}) == "" and node_label(cfg.admin_ui) in (HOST, node_name(
        cfg.admin_ui))
    cfg = AppConfig.from_dict({"admin_ui": {"node_name": "  Line 1 — Hat B  "}})
    assert cfg.admin_ui.node_name == "Line 1 — Hat B" and not cfg.load_notes
    # (a number reads as its text, as any text setting: the typed reader)
    cfg = AppConfig.from_dict({"admin_ui": {"node_name": 42}})
    assert cfg.admin_ui.node_name == "42" and any("admin_ui.node_name" in n for n in cfg.load_notes)
    for bad in ("x" * 65, "line\r\nBcc: someone@example.com", "tab\there", "a b",
                "rtl‮evil", ["x"]):
        cfg = AppConfig.from_dict({"admin_ui": {"node_name": bad}})
        assert cfg.admin_ui.node_name == "", bad
        assert any("admin_ui.node_name" in n for n in cfg.load_notes), (bad, cfg.load_notes)
        cfg.validate()
    assert AppConfig.from_dict({"admin_ui": {"node_name": "x" * 64}}).admin_ui.node_name == "x" * 64
    admin = AdminUI(node_name="stored")
    assert node_name(admin, {"SER2NETWEB_NODE_NAME": " from-env "}) == "from-env"
    assert node_name(admin, {"SER2NETWEB_NODE_NAME": "x" * 65}) == "stored"
    assert node_name(admin, {"SER2NETWEB_NODE_NAME": ""}) == "stored"
    assert runtime.node_name_notes({"SER2NETWEB_NODE_NAME": "x" * 65}) == [
        f"WARNING: SER2NETWEB_NODE_NAME {'x' * 65!r} is not used: The instance name can have at "
        "most 64 characters."]
    assert runtime.node_name_notes({"SER2NETWEB_NODE_NAME": "fine"}) == []
    print("config: optional, unusable hand-edited values dropped with a note, the environment "
          "first while usable  OK")


def test_notifications() -> None:
    # without a name: as before
    head, text = notify.message("login_failed", "", "from 10.0.0.9")
    assert head == f"ser2netweb {HOST}: failed sign-in" and text == head + "\nfrom 10.0.0.9"
    body = notify.webhook_body("generic", "device_lost", "PLC", "gone")
    assert body["node"] == HOST and body["hostname"] == HOST and "instance_id" not in body
    settings = SimpleNamespace(email_from="")
    assert notify.sender(settings) == f"ser2net@{HOST}"
    # with one
    head, text = notify.message("login_failed", "", "from 10.0.0.9", name="Line 1")
    assert head == "ser2netweb Line 1: failed sign-in", head
    body = notify.webhook_body("generic", "device_lost", "PLC", "gone", name="Line 1",
                               instance_id="0123456789abcdef")
    assert body["node"] == "Line 1" and body["hostname"] == HOST, body
    assert body["instance_id"] == "0123456789abcdef" and body["text"].startswith("ser2netweb Line 1:")
    card = notify.webhook_body("teams", "device_lost", "PLC", "gone", name="Line 1")
    assert card["attachments"][0]["content"]["body"][0]["text"].startswith("ser2netweb Line 1: PLC")
    for fmt in ("slack", "discord", "telegram"):
        assert "ser2netweb Line 1: PLC" in json.dumps(notify.webhook_body(fmt, "device_lost", "PLC",
                                                                       "", name="Line 1"))
    assert notify.sender(settings, "Line 1") == f"ser2netweb Line 1 <ser2net@{HOST}>"
    assert notify.sender(SimpleNamespace(email_from="alerts@example.com"), "Hat, B") == \
        '"ser2netweb Hat, B" <alerts@example.com>'
    assert notify.sender(SimpleNamespace(email_from="Ops <ops@example.com>"), "Hat B") == \
        "Ops <ops@example.com>", "a display name of its own is kept"
    # a non-ASCII name goes into the headers encoded; the message can be sent
    msg = EmailMessage()
    msg["Subject"] = notify.message("test", "", "", name="Hat Ç-2")[0]
    msg["From"] = notify.sender(settings, "Hat Ç-2")
    assert str(msg["From"]) == f"ser2netweb Hat Ç-2 <ser2net@{HOST}>", str(msg["From"])
    assert f"<ser2net@{HOST}>" in msg.as_string()
    # the e-mail as sent: Subject, From and the signature
    class FakeSMTP:
        sent: list = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

        def send_message(self, m) -> None:
            FakeSMTP.sent.append(m)

    real_smtp = notify.smtplib.SMTP
    notify.smtplib.SMTP = FakeSMTP   # type: ignore[misc]
    try:
        mail = SimpleNamespace(email_from="", recipients=["ops@example.com"], smtp_host="h",
                               smtp_port=25, smtp_security="none", smtp_username="")
        notify.send_email(mail, "test", "", "hello", name="Line 1")
        notify.send_email(mail, "test", "", "hello")
    finally:
        notify.smtplib.SMTP = real_smtp   # type: ignore[misc]
    named, plain = FakeSMTP.sent
    assert named["Subject"] == "ser2netweb Line 1: test notification", named["Subject"]
    assert str(named["From"]) == f"ser2netweb Line 1 <ser2net@{HOST}>"
    assert f" on Line 1 ({HOST})" in named.get_content(), named.get_content()
    assert plain["Subject"] == f"ser2netweb {HOST}: test notification"
    assert str(plain["From"]) == f"ser2net@{HOST}" and f" on {HOST}\n" in plain.get_content()
    # what the notifier sends: this instance's name and id
    sent = []
    real = notify.send_webhook
    notify.send_webhook = lambda s, *args, **kw: sent.append(kw)
    try:
        with App({"admin_ui": {"node_name": "Line 1"}}) as a:
            a.state.config.notifications.webhook_url = "https://hooks.example/x"
            assert asyncio.run(a.state.notifier.deliver("test", "", "hello")) == []
            assert sent == [{"name": "Line 1", "instance_id": a.state.config.instance_id}], sent
            a.state.config.admin_ui.node_name = ""
            asyncio.run(a.state.notifier.deliver("test", "", "hello"))
            assert sent[-1]["name"] == "", sent
    finally:
        notify.send_webhook = real
    print("notifications: the name in titles, text, JSON 'node' (+ hostname, instance_id), "
          "the e-mail From display name and signature; without one as before  OK")


def test_cluster() -> None:
    cfg = AppConfig.from_dict({"cluster": {"enabled": True, "key": "k" * 32},
                               "admin_ui": {"node_name": "Line 1"}})
    node = ClusterDiscovery(cfg, lambda m: None)
    body = json.loads(node._beacon_body())
    assert body["name"] == "Line 1" and set(body) == {"v", "id", "name", "ip", "port", "scheme",
                                                      "t", "b", "ips"}, body
    cfg.admin_ui.node_name = ""
    assert json.loads(node._beacon_body())["name"] == HOST
    print("cluster: the beacon's name is the instance's (no new field), else the host name  OK")


def _sign_in(a: App) -> str:
    st, h, _ = a.call("POST", "/login", fields={"username": "admin", "password": PW,
                                                "_csrf": CSRF},
                      headers={"Cookie": f"ser2net_csrf={CSRF}"})
    assert st == 303, st
    return f"ser2net_csrf={CSRF}; ser2net_session={cookies(h)['ser2net_session'][0]}"


def test_web() -> None:
    os.environ.pop("SER2NETWEB_NODE_NAME", None)
    os.environ.pop("SER2NET_NODE_NAME", None)
    with App({"users": USERS, "cluster": {"enabled": True, "key": "k" * 32}}) as a:
        jar = _sign_in(a)
        st, _, page = a.call("GET", "/", headers={"Cookie": jar})
        assert st == 200 and "<title>Mappings — ser2netweb</title>" in page
        assert 'class="node-name"' not in page
        st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
        assert 'name="node_name" value=""' in page and f'placeholder="{HOST}"' in page
        save = {"_csrf": CSRF, "node_name": "  Line 1 <b>  "}
        st, h, _ = a.call("POST", "/settings/node-name", fields=save, headers={"Cookie": jar})
        assert st == 303 and header(h, "location") == "/settings#admin-interface", (st, h)
        st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
        assert st == 200 and "Instance name saved." in page, st
        assert a.state.config.admin_ui.node_name == "Line 1 <b>"
        assert a.state.store.load().admin_ui.node_name == "Line 1 <b>"
        assert "instance name set to 'Line 1 <b>'" in a.lines
        assert any(r["action"] == "node_name_set" and r["detail"] == "Line 1 <b>"
                   for r in a.state.read_audit())
        assert "<title>Line 1 &lt;b&gt; · Settings — ser2netweb</title>" in page, page[:400]
        assert 'class="node-name" title="the name of this instance">Line 1 &lt;b&gt;</span>' in page
        for path, title in (("/", "Mappings"), ("/settings/audit", "Audit log")):
            st, _, page = a.call("GET", path, headers={"Cookie": jar})
            assert f"<title>Line 1 &lt;b&gt; · {title} — ser2netweb</title>" in page, path
        st, _, page = a.call("GET", "/login")
        assert "Line 1" not in page, "not on the sign-in page"
        # the fleet view's own row
        st, _, page = a.call("GET", "/api/cluster/status", headers={"Cookie": jar})
        assert st == 200 and "Line 1 &lt;b&gt;" in page, (st, page[:300])
        for bad, why in (("x" * 65, "at most 64 characters"),
                         ("a\r\nb", "control characters or line breaks")):
            st, _, page = a.call("POST", "/settings/node-name", fields={"_csrf": CSRF,
                                                                        "node_name": bad},
                                 headers={"Cookie": jar})
            assert st == 400 and why in page and a.state.config.admin_ui.node_name == "Line 1 <b>"
        # a form without the field (the environment sets it there) keeps the name
        st, _, _ = a.call("POST", "/settings/node-name", fields={"_csrf": CSRF},
                          headers={"Cookie": jar})
        assert st == 303 and a.state.config.admin_ui.node_name == "Line 1 <b>"
        os.environ["SER2NETWEB_NODE_NAME"] = "from-env"
        try:
            st, _, page = a.call("GET", "/settings", headers={"Cookie": jar})
            assert '<input type="text" value="from-env" disabled>' in page
            assert "Set by <span class=\"mono\">SER2NETWEB_NODE_NAME</span>" in page
            assert "<title>from-env · Settings — ser2netweb</title>" in page
        finally:
            del os.environ["SER2NETWEB_NODE_NAME"]
        # restore preview: an earlier backup has no name
        st, _, page = a.call("POST", "/settings/node-name", fields={"_csrf": CSRF,
                                                                    "node_name": ""},
                             headers={"Cookie": jar})
        assert "instance name cleared: the host name is used" in a.lines
        # (each save keeps a backup of what it wrote: the newest is the cleared name, the one
        # before it has the name)
        before = a.state.store.backup_names()[1]
        st, _, page = a.call("GET", f"/settings?restore={before}", headers={"Cookie": jar})
        assert st == 200 and "Instance name: — → Line 1 &lt;b&gt;" in page, page[-800:]
    print("web UI: the name in the signed-in pages' title and top bar and the fleet view; "
          "Settings saves / refuses / logs / audits it, the environment's read-only  OK")


def test_start() -> None:
    with Server({"users": USERS, "admin_ui": {"node_name": "Line 1"}},
                env={"SER2NETWEB_NODE_NAME": "y" * 65}) as srv:
        out = srv.output()
    assert "ser2netweb starting as 'Line 1' — admin UI on http://127.0.0.1:" in out, out[:800]
    assert "WARNING: SER2NETWEB_NODE_NAME 'yyyy" in out and "at most 64 characters" in out
    print("start-up: 'ser2netweb starting as <name>', an unusable SER2NETWEB_NODE_NAME warned  OK")


def main() -> None:
    test_config()
    test_notifications()
    test_cluster()
    test_web()
    test_start()
    print("\nPASS: instance name")


if __name__ == "__main__":
    main()
