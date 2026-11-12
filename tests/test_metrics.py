"""/metrics for Prometheus.

Checks:
  * every series has its # HELP and # TYPE lines, and the samples of a series stand
    together (the text format asks for both); comment lines stay first;
  * ser2net_mapping_enabled tells a mapping stopped on purpose from one that failed, and
    ser2net_mapping_state has exactly one state set per mapping; ser2net_build_info and
    ser2net_start_time_seconds describe the process; the existing names are kept;
  * without a session a scraper (no HTML asked for, or credentials sent) gets 401 with
    WWW-Authenticate instead of the redirect to the sign-in page; a browser still gets it;
  * a successful scrape is not written to all.log (other requests still are).

Run: python3 tests/test_metrics.py
"""
import os
import time
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app import __version__
from app.web.auth import hash_password
from app.web.routes import system


def test_exposition():
    text = system.exposition([
        "# ser2net metrics",
        "ser2net_up 1",
        'ser2net_mapping_running{id="a"} 1',
        'ser2net_mapping_clients{id="a"} 0',
        'ser2net_mapping_running{id="b"} 0',
        "ser2net_something_new 5",
        'ser2net_mapping_clients{id="b"} 2',
    ])
    assert text.endswith("\n")
    assert text.splitlines() == [
        "# ser2net metrics",
        "# HELP ser2net_up 1 while ser2net answers",
        "# TYPE ser2net_up gauge",
        "ser2net_up 1",
        "# HELP ser2net_mapping_running 1 while the mapping runs",
        "# TYPE ser2net_mapping_running gauge",
        'ser2net_mapping_running{id="a"} 1',
        'ser2net_mapping_running{id="b"} 0',
        "# HELP ser2net_mapping_clients Clients connected to the mapping",
        "# TYPE ser2net_mapping_clients gauge",
        'ser2net_mapping_clients{id="a"} 0',
        'ser2net_mapping_clients{id="b"} 2',
        "ser2net_something_new 5",
    ], text
    for name, (kind, help_text) in system.METRIC_INFO.items():
        assert kind in ("gauge", "counter") and help_text and "\n" not in help_text, name
        assert (kind == "counter") == name.endswith("_total") or name == "ser2net_mappings_total", name

    class M:
        enabled = False
    lines = system.mapping_state_metrics(M, {"state": "stopped"}, 'id="x"')
    assert lines[0] == 'ser2net_mapping_enabled{id="x"} 0'
    assert [ln for ln in lines if ln.endswith(" 1")] == ['ser2net_mapping_state{id="x",state="stopped"} 1']
    assert len(lines) == 1 + len(system.MAPPING_STATES)
    lines = system.mapping_state_metrics(M, {"state": "odd"}, 'id="x"')
    assert lines[-1] == 'ser2net_mapping_state{id="x",state="odd"} 1', lines
    print("exposition: HELP/TYPE before each series, its samples together; one-hot state  OK")


def families(text: str) -> dict:
    """name -> {"help", "type", "samples"}; fails when a series is split up or its samples
    come before its HELP/TYPE lines."""
    out: dict = {}
    order: list = []
    for ln in text.splitlines():
        if ln.startswith("# HELP ") or ln.startswith("# TYPE "):
            _hash, what, name, rest = ln.split(" ", 3)
            fam = out.setdefault(name, {"samples": []})
            assert not fam["samples"], f"{what} after the samples of {name}"
            fam[what.lower()] = rest
            continue
        if ln.startswith("#") or not ln:
            continue
        name = system._series(ln)
        if order and order[-1] != name:
            assert name not in order, f"the samples of {name} are split up:\n{text}"
        if not order or order[-1] != name:
            order.append(name)
        out.setdefault(name, {"samples": []})["samples"].append(ln)
    return out


def test_server():
    users = [{"username": "admin", "password_hash": hash_password("metrics-pass-1"),
              "role": "admin", "pwd_version": 1}]
    mappings = [
        {"id": "off1", "name": "stopped one", "enabled": False,
         "serial": {"port": "/dev/ser2net-test-none-a"},
         "network": {"bind_ip": "127.0.0.1", "port": free_port()}},
        {"id": "on1", "name": 'needs "a" device', "enabled": True,
         "serial": {"port": "/dev/ser2net-test-none-b"},
         "network": {"bind_ip": "127.0.0.1", "port": free_port()}},
    ]
    with Server({"users": users, "mappings": mappings}) as srv:
        t0 = time.time()
        b = Browser(srv.base)
        b.login("admin", "metrics-pass-1")
        st, headers, body = b.get("/metrics")
        assert st == 200 and headers["Content-Type"].startswith("text/plain; version=0.0.4"), st
        fams = families(body)
        for name, fam in fams.items():
            if name.startswith("ser2net_"):
                assert fam.get("help") and fam.get("type"), f"{name} has no HELP/TYPE:\n{body}"
        for kept in ("ser2net_up", "ser2net_mappings_total", "ser2net_mapping_running",
                     "ser2net_mapping_clients", "ser2net_mapping_bytes_in_total",
                     "ser2net_mapping_reconnects_total", "ser2net_mapping_dropped_clients_total"):
            assert kept in fams, kept
        assert fams["ser2net_mappings_total"]["type"] == "gauge"
        build = fams["ser2net_build_info"]["samples"]
        assert len(build) == 1 and f'version="{__version__}"' in build[0] and build[0].endswith(" 1")
        started = float(fams["ser2net_start_time_seconds"]["samples"][0].split()[-1])
        assert t0 - 120 < started <= time.time(), started
        enabled = {ln.split('id="')[1].split('"')[0]: ln.split()[-1]
                   for ln in fams["ser2net_mapping_enabled"]["samples"]}
        assert enabled == {"off1": "0", "on1": "1"}, enabled
        assert 'mapping="needs \\"a\\" device"' in body, "label values are escaped"
        for mid in ("off1", "on1"):
            set_ = [ln for ln in fams["ser2net_mapping_state"]["samples"]
                    if f'id="{mid}"' in ln and ln.endswith(" 1")]
            assert len(set_) == 1, (mid, set_)
        off_state = next(ln for ln in fams["ser2net_mapping_state"]["samples"]
                         if 'id="off1"' in ln and ln.endswith(" 1"))
        assert 'state="stopped"' in off_state, off_state
        on_state = next(ln for ln in fams["ser2net_mapping_state"]["samples"]
                        if 'id="on1"' in ln and ln.endswith(" 1"))
        assert 'state="stopped"' not in on_state, "an enabled mapping that can't run is not 'stopped'"
        print("server: HELP/TYPE for every series, enabled 0/1, one state each, build info, "
              "start time  OK")

        # without a session: a scraper is refused with 401, a browser is sent to sign in
        def anonymous(headers):
            req = urllib.request.Request(srv.base + "/metrics", headers=headers)
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                with opener.open(req, timeout=10) as r:
                    return r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.headers, e.read()
        st, h, text = anonymous({"Accept": "text/plain;version=0.0.4;q=0.3,*/*;q=0.2"})
        assert st == 401 and h["WWW-Authenticate"].startswith("Bearer"), (st, dict(h))
        assert b"/api/v1/metrics" in text, text
        st, h, _ = anonymous({"Accept": "text/html,application/xhtml+xml",
                              "Authorization": "Bearer s2n_not_a_session"})
        assert st == 401, "credentials sent: 401, not the sign-in page"
        st, h, _ = anonymous({"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        # (to the sign-in page, which leads back to the page asked for)
        assert st == 303 and h["Location"] == "/login?next=%2Fmetrics", (st, dict(h))
        st, h, _ = anonymous({"Accept": "*/*"})
        assert st == 401, st
        print("no session: scraper 401 + WWW-Authenticate, browser 303 to /login  OK")

        for _ in range(3):
            assert b.get("/metrics")[0] == 200
        assert b.get("/settings")[0] == 200
        log = ""
        for _ in range(50):
            with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
                log = fh.read()
            if "GET /settings" in log:
                break
            time.sleep(0.1)
        assert "GET /settings -> 200" in log, log[-1500:]
        assert "GET /metrics -> 200" not in log, "a successful scrape is not logged"
        # (a refused one is, as every request refused before a handler: once a minute for
        # an address and status)
        assert log.count("GET /metrics -> 401") == 1, log[-1500:]
        print("all.log: successful scrapes left out, refused ones and other requests logged  OK")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    test_exposition()
    test_server()
    print("\nPASS: /metrics")


if __name__ == "__main__":
    main()
