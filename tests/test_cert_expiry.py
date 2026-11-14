"""The cert_expiring notification: TLS certificates about to end (ser2netweb/certwatch.py).

Checks:
  * the certificates looked at are the admin UI's (while its TLS is on) and each enabled
    TLS mapping's, read with web/certinfo.py;
  * a certificate with fewer than 30, 7 or 1 days left, or that has ended, is logged and
    sent once per threshold and channel, also across restarts (cert-notices.json, written
    once a channel took the notice, with the channels it did not reach, which get it at
    the next check, and only they), a renewed one (a new end date) starts afresh, one
    that can't be read at a check, whose mapping is disabled or the admin UI's TLS off
    keeps what was sent for it (a deleted mapping's is forgotten), and one with more days
    left says nothing; with the event off
    nothing is sent or remembered, so switching it on later still tells;
  * the event is opt-in (not in the defaults), has a title, a Settings checkbox and a
    Turkish label; a config that names it loads, and one without it is unchanged;
  * a running server: /metrics has ser2net_tls_certificate_expiry_seconds, GET
    /api/v1/status lists the certificates, the notifier looks soon after the start and
    sends the event to the webhook once - not again after a restart.

Needs the optional cryptography package (to make certificates); skipped without it.

Run: python3 tests/test_cert_expiry.py
"""
import asyncio
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from _server import Browser, Server, free_port

from ser2netweb import certwatch, notify
from ser2netweb.config import (NOTIFY_DEFAULT_EVENTS, NOTIFY_EVENTS, OWN_NAMES, AppConfig,
                               MappingConfig, NotificationSettings, foreign_entries)
from ser2netweb.web import certinfo, selfsigned
from ser2netweb.web.auth import hash_password, hash_token

DAY = 86400.0
TOKEN = "s2n_cert-expiry-token"


def pair(folder, name, days):
    """A self-signed pair that ends `days` days after yesterday (selfsigned.generate: valid
    from a day ago), so with a little less than days - 1 days left."""
    cert, key = os.path.join(folder, f"{name}.pem"), os.path.join(folder, f"{name}.key")
    selfsigned.generate(cert, key, common_name=name, days=days)
    return cert, key


def tls_mapping(mid, name, cert, key, enabled=True, **network):
    return {"id": mid, "name": name, "enabled": enabled, "kind": "net",
            "serial": {"port": f"/dev/ser2net-cert-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": free_port(), "tls": True, "tls_cert": cert, "tls_key": key,
                        **network}}


class State:
    def __init__(self, folder, config, events=("cert_expiring",)):
        self.data_dir = folder
        self.config = AppConfig.from_dict(config)
        self.config.notifications = NotificationSettings.from_dict({
            "enabled": True, "events": list(events), "repeat_s": 300,
            "webhook_url": "https://x/y"})
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


def test_event():
    assert "cert_expiring" in NOTIFY_EVENTS and "cert_expiring" not in NOTIFY_DEFAULT_EVENTS
    assert notify.title("cert_expiring") != "cert_expiring"
    assert certwatch.NOTICES_FILE in OWN_NAMES
    with tempfile.TemporaryDirectory() as d:
        for name in ("config.json", certwatch.NOTICES_FILE, certwatch.NOTICES_FILE + ".tmp"):
            open(os.path.join(d, name), "w").close()
        assert foreign_entries(d) == []
    kept = NotificationSettings.from_dict({"events": ["device_lost", "cert_expiring"]})
    assert kept.events == ["device_lost", "cert_expiring"]
    assert "cert_expiring" not in NotificationSettings.from_dict({}).events
    print("event: opt-in, titled; its notices file is one of the data folder's own  OK")


def test_thresholds(folder):
    long_cert, long_key = pair(folder, "long", 400)
    near_cert, near_key = pair(folder, "near", 20)
    admin_cert, admin_key = pair(folder, "admin", 400)
    config = {"admin_ui": {"tls_cert": admin_cert, "tls_key": admin_key}, "mappings": [
        tls_mapping("m1", "long-lived", long_cert, long_key),
        tls_mapping("m2", "near", near_cert, near_key),
        tls_mapping("m3", "off", near_cert, near_key, enabled=False),
        tls_mapping("m4", "udp", near_cert, near_key, mode="udp"),
        {"id": "m5", "name": "plain", "serial": {"port": "/dev/x"}}]}
    st = State(folder, config)
    used = certwatch.in_use(st.config)
    assert [(c.key, c.mapping, c.path) for c in used] == [
        ("admin", "", admin_cert), ("m1", "long-lived", long_cert), ("m2", "near", near_cert)], used
    sent, pending = [], []

    def emit(event, key, mapping, detail, **after):
        sent.append((event, key, mapping, detail))
        pending.append(after["sent"])  # (what the notifier awaits once it has tried it)
        return True

    async def check(watch, at, delivered=True):
        queued = await watch.check(emit, at)
        while pending:
            # (sent(tried, failed): the one channel took it, or it did not)
            await pending.pop(0)(["webhook"], [] if delivered else ["webhook"])
        return queued

    def remembered():
        with open(os.path.join(folder, certwatch.NOTICES_FILE), encoding="utf-8") as fh:
            return json.load(fh)

    async def run():
        watch = certwatch.CertWatch(st)
        end = certinfo.read(near_cert).not_after  # (a little less than 20 days from now)
        now = end - 19.5 * DAY
        assert await check(watch, now) == 1
        assert [(e, m) for e, _k, m, _d in sent] == [("cert_expiring", "near")], sent
        assert "mapping 'near' certificate" in sent[0][3] and "ends in 19 days" in sent[0][3], sent
        assert sum("WARNING: TLS certificate: mapping 'near'" in ln for ln in st.logs) == 1
        assert await check(watch, now + 3600) == 0, "once per threshold"
        assert await check(watch, now + 5 * DAY) == 0, "still the 30-day threshold"
        assert len(st.logs) == 1, st.logs
        # a restart (another CertWatch) remembers what was sent, in cert-notices.json
        again = certwatch.CertWatch(st)
        assert await check(again, now + 6 * DAY) == 0 and len(sent) == 1, sent
        assert sum("WARNING: TLS certificate" in ln for ln in st.logs) == 2, "logged per run"
        with open(os.path.join(folder, certwatch.NOTICES_FILE), encoding="utf-8") as fh:
            notices = json.load(fh)
        assert list(notices) == [f"m2|{near_cert}"] and notices[f"m2|{near_cert}"][1] == 30
        # 7 days, 1 day, ended: one each
        assert await check(again, end - 6.5 * DAY) == 1 and "ends in 6 days" in sent[-1][3]
        assert await check(again, end - 6 * DAY) == 0
        assert await check(again, end - 0.5 * DAY) == 1 and "ends in 0 days" in sent[-1][3]
        assert await check(again, end - 0.4 * DAY) == 0
        assert await check(again, end + DAY) == 1 and "EXPIRED" in sent[-1][3]
        assert await check(again, end + 10 * DAY) == 0 and len(sent) == 4, sent
        assert len({k for _e, k, _m, _d in sent}) == 4, "a repeat key per threshold"
        # renewed (the same path, a new end date): it starts afresh
        selfsigned.generate(near_cert, near_key, common_name="near", days=5)
        now = time.time()
        assert await check(again, now) == 1 and "ends in 3 days" in sent[-1][3], sent[-1]
        with open(os.path.join(folder, certwatch.NOTICES_FILE), encoding="utf-8") as fh:
            assert json.load(fh)[f"m2|{near_cert}"][1] == 7
        # the admin UI's certificate, and a certificate that can't be read
        assert await check(again, now + 380 * DAY) == 3
        assert {m for _e, _k, m, _d in sent[-3:]} == {"", "long-lived", "near"}, sent[-3:]
        assert any("admin UI certificate" in d for _e, _k, _m, d in sent[-3:])
        with open(long_cert, "rb") as fh:
            good = fh.read()
        with open(long_cert, "w", encoding="utf-8") as fh:
            fh.write("not a certificate\n")
        before = len(sent)
        await check(again, now)
        views = {c.key: c.view(now) for c in await again.current()}
        assert views["m1"]["state"] == "unreadable" and views["m1"]["detail"], views["m1"]
        assert views["admin"] == {"use": "admin", "path": admin_cert, "subject": "admin",
                                  "not_after": views["admin"]["not_after"], "days_left": 398,
                                  "state": "valid"}, views["admin"]
        assert views["m2"]["id"] == "m2" and views["m2"]["state"] == "expiring", views["m2"]
        assert len(sent) == before, "an unreadable file is not an expiry"
        # ... and keeps what was sent for it: readable again (certbot was replacing it),
        # nothing is sent twice
        assert f"m1|{long_cert}" in remembered(), remembered()
        with open(long_cert, "wb") as fh:
            fh.write(good)
        assert await check(again, now + 380 * DAY) == 0 and len(sent) == before, sent[before:]
        # a notice that did not get through (the webhook was down) is not kept as sent:
        # the next check sends it again, and once it got through, no more
        flaky_cert, flaky_key = pair(folder, "flaky", 20)
        st.config.mappings.append(MappingConfig.from_dict(
            tls_mapping("m6", "flaky", flaky_cert, flaky_key)))
        at = time.time()
        assert await check(again, at, delivered=False) == 1 and sent[-1][2] == "flaky"
        assert f"m6|{flaky_cert}" not in remembered(), "not delivered: not remembered"
        assert await check(again, at + DAY) == 1 and sent[-1][2] == "flaky", "sent again"
        assert remembered()[f"m6|{flaky_cert}"][1] == 30
        assert await check(again, at + 2 * DAY) == 0

    asyncio.run(run())
    print("thresholds: 30 / 7 / 1 days and ended, once each, remembered across restarts once "
          "delivered (one that did not get through goes again); a renewed certificate starts "
          "afresh; one unreadable at a check keeps its record; the admin UI's is watched too  OK")


def test_event_off(folder):
    cert, key = pair(folder, "quiet", 3)
    st = State(folder, {"mappings": [tls_mapping("q1", "quiet", cert, key)]}, events=())
    os.remove(os.path.join(folder, certwatch.NOTICES_FILE))

    async def run():
        n = notify.Notifier(st)
        st.certwatch = certwatch.CertWatch(st)
        await n.check_certificates()
        assert n._queue.empty(), "the event is off"
        assert any("WARNING: TLS certificate: mapping 'quiet'" in ln for ln in st.logs), st.logs
        assert not os.path.exists(os.path.join(folder, certwatch.NOTICES_FILE)), \
            "nothing sent, nothing remembered"
        st.config.notifications.events = ["cert_expiring"]
        await n.check_certificates()
        event, mapping, detail, *_after = n._queue.get_nowait()
        assert (event, mapping) == ("cert_expiring", "quiet") and "ends in 1 day" in detail
        await n.check_certificates()
        assert n._queue.empty(), "once"

    asyncio.run(run())
    print("event off: logged, not sent, not remembered - switched on later, it is sent  OK")


def test_per_channel(folder):
    # a webhook that works and e-mail that fails: the webhook gets the notice once, e-mail
    # again at each check until it takes it (not the webhook)
    cert, key = pair(folder, "two", 3)
    st = State(folder, {"mappings": [tls_mapping("t1", "two", cert, key)]})
    st.config.notifications.email_to = "ops@example.com"
    path = os.path.join(folder, certwatch.NOTICES_FILE)
    if os.path.exists(path):
        os.remove(path)

    async def run():
        n = notify.Notifier(st)
        st.certwatch = certwatch.CertWatch(st)
        reached, mail_up = [], [False]

        async def deliver(event, mapping, detail, settings=None, warnings=None, channels=None):
            use = [c for c in ("webhook", "e-mail") if channels is None or c in channels]
            reached.append(use)
            if "e-mail" in use and not mail_up[0]:
                return [notify.Failure("e-mail", "mail.example:587", OSError("refused"))]
            return []

        n.deliver = deliver
        worker = asyncio.create_task(n._worker())

        async def check():
            n._last.clear()  # (a check a day later: the repeat limit has run out)
            await n.check_certificates()
            await asyncio.wait_for(n._queue.join(), 5)

        try:
            await check()
            assert reached == [["webhook", "e-mail"]], reached
            assert certwatch.load_notices(path)[f"t1|{cert}"] == [
                certinfo.read(cert).not_after, 7, ["e-mail"]], certwatch.load_notices(path)
            assert any("e-mail via mail.example:587 failed" in ln for ln in st.logs), st.logs
            await check()
            await check()
            assert reached[1:] == [["e-mail"], ["e-mail"]], "only the channel that failed"
            mail_up[0] = True
            await check()
            assert reached[3:] == [["e-mail"]]
            assert certwatch.load_notices(path)[f"t1|{cert}"][2:] == []
            await check()
            assert len(reached) == 4, "every channel has it: no more"
        finally:
            worker.cancel()

    asyncio.run(run())
    print("notifier: a notice one channel took is kept, and only the channel that failed "
          "gets it again at the next checks  OK")


def test_kept_while_unused(folder):
    # what was sent for a certificate is kept while its mapping is disabled or the admin
    # UI's TLS is off (also when the mapping names another file meanwhile); forgotten when
    # the certificate at its path is renewed or its mapping is deleted
    cert, key = pair(folder, "off-on", 20)
    ui_cert, ui_key = pair(folder, "ui-off-on", 20)
    other_cert, other_key = pair(folder, "other", 20)
    st = State(folder, {"admin_ui": {"tls_cert": ui_cert, "tls_key": ui_key},
                        "mappings": [tls_mapping("d1", "off-on", cert, key)]})
    path = os.path.join(folder, certwatch.NOTICES_FILE)
    if os.path.exists(path):
        os.remove(path)
    sent, pending = [], []

    def emit(event, key, mapping, detail, **after):
        sent.append((mapping, detail))
        pending.append(after["sent"])
        return True

    async def check(watch):
        queued = await watch.check(emit)
        while pending:
            await pending.pop(0)(["webhook"], [])
        return queued

    def records():
        return set(certwatch.load_notices(path))

    async def run():
        watch = certwatch.CertWatch(st)
        assert await check(watch) == 2 and sorted(m for m, _d in sent) == ["", "off-on"], sent
        m, admin = st.config.mappings[0], st.config.admin_ui
        m.enabled = False
        admin.tls_cert, admin.tls_key = "", ""  # (TLS off: its paths cleared)
        assert await check(watch) == 0
        assert records() == {f"d1|{cert}", f"admin|{ui_cert}"}, "kept while not in use"
        m.enabled, admin.tls_cert, admin.tls_key = True, ui_cert, ui_key
        assert await check(watch) == 0 and len(sent) == 2, ("sent again", sent)
        m.network.tls_cert, m.network.tls_key = other_cert, other_key  # another file ...
        assert await check(watch) == 1 and other_cert in sent[-1][1]
        m.network.tls_cert, m.network.tls_key = cert, key  # ... and back: said already
        assert await check(watch) == 0 and len(sent) == 3, sent
        # renewed while its mapping is off: forgotten, so it starts afresh
        m.enabled = False
        selfsigned.generate(cert, key, common_name="off-on", days=10)
        await check(watch)
        assert f"d1|{cert}" not in records(), records()
        m.enabled = True
        assert await check(watch) == 1 and cert in sent[-1][1]
        st.config.mappings.clear()  # deleted: its records go
        await check(watch)
        assert records() == {f"admin|{ui_cert}"}, records()

    asyncio.run(run())
    print("records: kept while a mapping is disabled or the admin UI's TLS is off (not sent "
          "again once back); forgotten once the certificate is renewed or its mapping "
          "deleted  OK")


def test_kept_once_delivered(folder):
    cert, key = pair(folder, "relay", 3)
    st = State(folder, {"mappings": [tls_mapping("r1", "relay", cert, key)]})
    path = os.path.join(folder, certwatch.NOTICES_FILE)
    if os.path.exists(path):
        os.remove(path)

    async def run():
        n = notify.Notifier(st)
        st.certwatch = certwatch.CertWatch(st)
        tried = []
        answers = [[notify.Failure("webhook", "json", OSError("unreachable"))], []]

        async def deliver(event, mapping, detail, settings=None, warnings=None, channels=None):
            tried.append((event, mapping))
            return answers.pop(0)

        n.deliver = deliver
        worker = asyncio.create_task(n._worker())
        try:
            await n.check_certificates()
            await asyncio.wait_for(n._queue.join(), 5)
            assert tried == [("cert_expiring", "relay")], tried
            assert not os.path.exists(path) or f"r1|{cert}" not in certwatch.load_notices(path)
            assert any("webhook (json) failed" in ln for ln in st.logs), st.logs
            n._last.clear()  # (the next daily check: the repeat limit has run out)
            await n.check_certificates()
            await asyncio.wait_for(n._queue.join(), 5)
            assert len(tried) == 2, "the notice that did not get through is sent again"
            assert certwatch.load_notices(path)[f"r1|{cert}"][1] == 7
            n._last.clear()
            await n.check_certificates()
            await asyncio.wait_for(n._queue.join(), 5)
            assert len(tried) == 2, "once it got through: not again"
        finally:
            worker.cancel()

    asyncio.run(run())
    print("notifier: a threshold is kept as sent once delivered; one that did not get "
          "through goes out again at the next check  OK")


class Hook(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):
        Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_live(folder):
    cert, key = pair(folder, "live", 5)
    hook = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    users = [{"username": "admin", "password_hash": hash_password("cert-pass-1"),
              "role": "admin", "pwd_version": 1}]
    srv = Server({"users": users, "api_token_hash": hash_token(TOKEN), "api_token_role": "viewer",
                  "admin_ui": {"language": "en"},
                  "notifications": {"enabled": True, "events": ["cert_expiring"],
                                    "webhook_url": f"http://127.0.0.1:{hook.server_port}/hook"},
                  "mappings": [tls_mapping("c1", "live", cert, key)]})

    def wait(cond, what, timeout=20.0):
        end = time.time() + timeout
        while not cond():
            assert time.time() < end, f"timed out: {what}"
            time.sleep(0.1)

    try:
        with srv:
            wait(lambda: Hook.got, "the webhook got cert_expiring")
            assert len(Hook.got) == 1 and Hook.got[0]["event"] == "cert_expiring", Hook.got
            assert Hook.got[0]["mapping"] == "live" and "ends in 3 days" in Hook.got[0]["detail"]
            assert "WARNING: TLS certificate: mapping 'live' certificate" in srv.output()
            b = Browser(srv.base)
            b.login("admin", "cert-pass-1")
            _st, _h, body = b.get("/metrics")
            m = re.search(r'^ser2net_tls_certificate_expiry_seconds\{use="mapping",'
                          r'mapping="live",id="c1"\} (\d+)$', body, re.M)
            assert m and 3 * DAY < int(m.group(1)) - time.time() < 4 * DAY + 60, body[-600:]
            assert "# TYPE ser2net_tls_certificate_expiry_seconds gauge" in body
            req = urllib.request.Request(srv.base + "/api/v1/status",
                                         headers={"Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                status = json.load(r)
            assert status["certificates"] == [{
                "use": "mapping", "id": "c1", "mapping": "live", "path": cert, "subject": "live",
                "not_after": status["certificates"][0]["not_after"], "days_left": 3,
                "state": "expiring"}], status["certificates"]
            _st, _h, page = b.get("/settings")
            assert re.search(r'name="notify_event_cert_expiring" checked>\s*TLS certificate '
                             r'about to expire', page), "its checkbox, ticked"
            b.get("/lang/tr")
            _st, _h, page = b.get("/settings")
            assert "TLS sertifikasının süresi dolmak üzere" in page
            assert os.path.isfile(os.path.join(srv.data, certwatch.NOTICES_FILE))
            srv.stop()
            srv.start()
            wait(lambda: "WARNING: TLS certificate: mapping 'live'" in srv.output(),
                 "the certificate is looked at again after the restart")
            time.sleep(2.0)
            assert len(Hook.got) == 1, f"sent again after a restart: {Hook.got}"
    finally:
        hook.shutdown()
        hook.server_close()
    print("live: the notifier looks after the start and sends cert_expiring once (not again "
          "after a restart); /metrics and /api/v1/status have the certificate  OK")


def main():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        print("SKIP: needs the cryptography package to make certificates")
        return
    test_event()
    with tempfile.TemporaryDirectory() as folder:
        test_thresholds(folder)
        test_event_off(folder)
        test_kept_once_delivered(folder)
        test_per_channel(folder)
        test_kept_while_unused(folder)
        test_live(folder)
    print("\nPASS: cert_expiring notification")


if __name__ == "__main__":
    main()
