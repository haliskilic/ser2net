"""The cert_expiring notification: TLS certificates about to end (ser2netweb/certwatch.py).

Checks:
  * the certificates looked at are the admin UI's (while its TLS is on) and each enabled
    TLS mapping's, read with web/certinfo.py;
  * a certificate with fewer than 30, 7 or 1 days left, or that has ended, is logged and
    sent once per threshold, also across restarts (cert-notices.json), a renewed one (a
    new end date) starts afresh, and one with more days left says nothing; with the event
    off nothing is sent or remembered, so switching it on later still tells;
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
                               NotificationSettings, foreign_entries)
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
    sent = []

    def emit(event, key, mapping, detail):
        sent.append((event, key, mapping, detail))
        return True

    async def run():
        watch = certwatch.CertWatch(st)
        end = certinfo.read(near_cert).not_after  # (a little less than 20 days from now)
        now = end - 19.5 * DAY
        assert await watch.check(emit, now) == 1
        assert [(e, m) for e, _k, m, _d in sent] == [("cert_expiring", "near")], sent
        assert "mapping 'near' certificate" in sent[0][3] and "ends in 19 days" in sent[0][3], sent
        assert sum("WARNING: TLS certificate: mapping 'near'" in ln for ln in st.logs) == 1
        assert await watch.check(emit, now + 3600) == 0, "once per threshold"
        assert await watch.check(emit, now + 5 * DAY) == 0, "still the 30-day threshold"
        assert len(st.logs) == 1, st.logs
        # a restart (another CertWatch) remembers what was sent, in cert-notices.json
        again = certwatch.CertWatch(st)
        assert await again.check(emit, now + 6 * DAY) == 0 and len(sent) == 1, sent
        assert sum("WARNING: TLS certificate" in ln for ln in st.logs) == 2, "logged per run"
        with open(os.path.join(folder, certwatch.NOTICES_FILE), encoding="utf-8") as fh:
            notices = json.load(fh)
        assert list(notices) == [f"m2|{near_cert}"] and notices[f"m2|{near_cert}"][1] == 30
        # 7 days, 1 day, ended: one each
        assert await again.check(emit, end - 6.5 * DAY) == 1 and "ends in 6 days" in sent[-1][3]
        assert await again.check(emit, end - 6 * DAY) == 0
        assert await again.check(emit, end - 0.5 * DAY) == 1 and "ends in 0 days" in sent[-1][3]
        assert await again.check(emit, end - 0.4 * DAY) == 0
        assert await again.check(emit, end + DAY) == 1 and "EXPIRED" in sent[-1][3]
        assert await again.check(emit, end + 10 * DAY) == 0 and len(sent) == 4, sent
        assert len({k for _e, k, _m, _d in sent}) == 4, "a repeat key per threshold"
        # renewed (the same path, a new end date): it starts afresh
        selfsigned.generate(near_cert, near_key, common_name="near", days=5)
        now = time.time()
        assert await again.check(emit, now) == 1 and "ends in 3 days" in sent[-1][3], sent[-1]
        with open(os.path.join(folder, certwatch.NOTICES_FILE), encoding="utf-8") as fh:
            assert json.load(fh)[f"m2|{near_cert}"][1] == 7
        # the admin UI's certificate, and a certificate that can't be read
        assert await again.check(emit, now + 380 * DAY) == 3
        assert {m for _e, _k, m, _d in sent[-3:]} == {"", "long-lived", "near"}, sent[-3:]
        assert any("admin UI certificate" in d for _e, _k, _m, d in sent[-3:])
        with open(long_cert, "w", encoding="utf-8") as fh:
            fh.write("not a certificate\n")
        before = len(sent)
        await again.check(emit, now)
        views = {c.key: c.view(now) for c in await again.current()}
        assert views["m1"]["state"] == "unreadable" and views["m1"]["detail"], views["m1"]
        assert views["admin"] == {"use": "admin", "path": admin_cert, "subject": "admin",
                                  "not_after": views["admin"]["not_after"], "days_left": 398,
                                  "state": "valid"}, views["admin"]
        assert views["m2"]["id"] == "m2" and views["m2"]["state"] == "expiring", views["m2"]
        assert len(sent) == before, "an unreadable file is not an expiry"

    asyncio.run(run())
    print("thresholds: 30 / 7 / 1 days and ended, once each, remembered across restarts; a "
          "renewed certificate starts afresh; the admin UI's is watched too  OK")


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
        event, mapping, detail = n._queue.get_nowait()
        assert (event, mapping) == ("cert_expiring", "quiet") and "ends in 1 day" in detail
        await n.check_certificates()
        assert n._queue.empty(), "once"

    asyncio.run(run())
    print("event off: logged, not sent, not remembered - switched on later, it is sent  OK")


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
        test_live(folder)
    print("\nPASS: cert_expiring notification")


if __name__ == "__main__":
    main()
