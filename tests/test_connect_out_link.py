"""A connect-out mapping reports its link to the remote server, apart from the serial side.

  - status: link "connecting" at start, "down" (link_error, connect_failures counted) while
    the remote server can't be reached, "up" once it is (link_error and the link's
    last_error cleared), "connecting" again when the link closes, "" once stopped; state
    keeps reporting the serial side ("running");
  - notifications: link_lost when an attempt fails while the link was up or connecting
    (also for a link that is down at start-up, once), link_back when it is up again; a
    link that closes and connects again at once (a max connection time) reports nothing;
  - the dashboard (and the fleet view) counts a link that is down as a problem ("link
    down" and the reason); /metrics has ser2net_mapping_link_up and
    ser2net_mapping_connect_failures_total for connect-out mappings only.
The remote server is a socket on 127.0.0.1 with an OS-assigned port: bound but not
listening (connections are refused), then listening.

Run: python3 tests/test_connect_out_link.py
"""
import asyncio
import os
import re
import socket
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import Browser, Server, free_port

from app import notify
from app.config import NOTIFY_DEFAULT_EVENTS, MappingConfig, NotificationSettings
from app.engine import serial_io
from app.engine.bridge import MappingRunner
from app.web.auth import hash_password


def remote():
    """A port nothing listens on yet (a connection to it is refused): (socket, port)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


def client_mapping(port, **options):
    return MappingConfig.from_dict({
        "name": "uplink", "enabled": True, "serial": {"port": "/dev/ttyNOPE-uplink"},
        "network": {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
                    "remote_port": port, "connect_timeout_s": 2},
        "options": options})


async def _port_open(settings):
    await asyncio.Event().wait()  # the serial port "opens" and stays open


async def _until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.05)


async def _link_states():
    server, port = remote()
    runner = MappingRunner(client_mapping(port), logger=lambda m: None)
    st = runner.status
    try:
        with mock.patch.object(serial_io, "open_serial", _port_open):
            await runner.start()
            assert st.link == "connecting" and st.state == "running", st.as_dict()
            await _until(lambda: st.link == "down")
            d = st.as_dict()
            assert d["link"] == "down" and d["link_error"] and d["connect_failures"] >= 1, d
            assert d["state"] == "running" and d["last_error"] == d["link_error"], d
            assert d["link_since"] > 0
            server.listen()  # the remote server is up now
            await _until(lambda: st.link == "up")
            d = st.as_dict()
            assert d["link_error"] == "" and d["last_error"] == "", d
            failures = d["connect_failures"]
            conn, _addr = server.accept()
            conn.close()  # the remote end closes the link: connecting, then up again
            await _until(lambda: st.link != "up")
            assert st.link in ("connecting", "up"), st.as_dict()
            await _until(lambda: st.link == "up")
            assert st.connect_failures == failures, "a reconnect that works is no failure"
            server.close()  # gone for good: down again
            await _until(lambda: st.link == "down")
            await runner.stop()
            assert st.link == "" and st.link_error == "" and st.state == "stopped", st.as_dict()
    finally:
        server.close()
        if st.state != "stopped":
            await runner.stop()
    print("status: link connecting -> down (error, failures counted) -> up (error cleared) "
          "-> connecting -> up; '' when stopped; state stays the serial side's  OK")


def test_link_states():
    asyncio.run(_link_states())


async def _serial_error_kept():
    server, port = remote()
    runner = MappingRunner(client_mapping(port), logger=lambda m: None)
    st = runner.status
    try:
        with mock.patch.object(serial_io, "open_serial", _port_open):
            await runner.start()
            await _until(lambda: st.link == "down")
            assert st.last_error == st.link_error != ""
            st.last_error = "serial error"  # the serial side reports after the link did
            server.listen()
            await _until(lambda: st.link == "up")
            assert st.last_error == "serial error" and st.link_error == "", st.as_dict()
    finally:
        server.close()
        await runner.stop()
    print("status: a link that comes up clears only its own error, not the serial side's  OK")


def test_serial_error_kept():
    asyncio.run(_serial_error_kept())


def test_notifications():
    names = {"c": "Uplink"}

    def status(link, error="", peers=()):
        return {"c": {"state": "running", "link": link, "link_error": error,
                      "clients": [{"peer": p} for p in peers]}}

    def events(before, after):
        return [e[0] for e in notify.status_events(notify.snapshot(before), after, names)]

    up, down = status("up", peers=["10.0.0.9:502"]), status("down", "connection refused")
    connecting = status("connecting")
    assert notify.status_events(notify.snapshot(up), down, names)[0] == \
        ("link_lost", "c", "Uplink", "connection refused")
    assert events(connecting, down) == ["link_lost"], "a link that never came up"
    back = notify.status_events(notify.snapshot(down), up, names)
    assert ("link_back", "c", "Uplink", "10.0.0.9:502") in back, back
    assert events(up, connecting) == ["client_disconnected"], "a recycle is no outage"
    assert "link_lost" not in events(connecting, up) and events(down, down) == []
    # down at start-up: reported once, like a later loss
    prev = notify.baseline(down)
    assert prev == {}, prev
    assert [e[0] for e in notify.status_events(prev, down, names)] == ["link_lost"]
    assert events(down, down) == []
    # the server mappings and the serial side are unchanged
    srv = {"c": {"state": "running", "clients": []}}
    assert events(srv, srv) == [] and "c" in notify.baseline(srv)
    assert "link_lost" in NOTIFY_DEFAULT_EVENTS and "link_back" in NOTIFY_DEFAULT_EVENTS
    assert NotificationSettings.from_dict({}).events == list(NOTIFY_DEFAULT_EVENTS)
    kept = NotificationSettings.from_dict({"events": ["device_lost"]}).events
    assert kept == ["device_lost"], "a saved events list stays as it is"
    assert notify.title("link_lost") and notify.title("link_lost") != "link_lost"
    print("notifications: link_lost (also when down at start-up, once), link_back; a "
          "recycle reports nothing; on by default only without a saved events list  OK")


def test_dashboard_markup():
    from app.web.server import make_templates

    env = make_templates().env
    m = client_mapping(5000)
    base = {"state": "running", "device": "/dev/ttyUSB0", "client_count": 0, "clients": [],
            "bytes_in": 0, "bytes_out": 0, "bytes_dropped": 0, "dropped_clients": 0,
            "last_error": "", "retrying": False, "next_retry_s": 0}

    def row(**st):
        out = env.get_template("_mappings_body.html").render(
            mappings=[m], statuses={m.id: dict(base, **st)}, can_edit=True)
        return out, re.search(r'<tr data-mid="[^"]+"[^>]*>', out).group(0)

    out, tr = row(link="down", link_error="Connect call failed ('127.0.0.1', 5000)",
                  last_error="Connect call failed ('127.0.0.1', 5000)")
    assert 'data-state="problem"' in tr and "link down" in out, tr
    assert out.count("Connect call failed") == 2, "the reason once (cell text + its title)"
    out, tr = row(link="up")
    assert 'data-state="running"' in tr and "link down" not in out, tr
    out, tr = row(link="connecting")
    assert 'data-state="running"' in tr and ">connecting<" in out, out
    # a serial problem and a link that is down: both reasons
    out, tr = row(state="reconnecting", last_error="serial EOF", link="down",
                  link_error="refused")
    assert "serial EOF" in out and "refused" in out and 'data-state="problem"' in tr
    nodes = [{"self": True, "online": True, "name": "n", "addr": "a", "id": "x", "uptime": "",
              "version": "", "mapping_count": 1, "running_count": 1, "source": "self",
              "mappings": [{"id": "m", "name": "M", "enabled": True, "kind": "client/raw",
                            "endpoint": "→ h:1", "serial": "/dev/x", "state": "running",
                            "link": "down", "client_count": 0, "bytes_in": 0,
                            "bytes_out": 0}]}]
    out = env.get_template("_cluster_body.html").render(nodes=nodes, can_edit=False,
                                                        discovery_error="")
    assert "link down" in out, out
    del nodes[0]["mappings"][0]["link"]  # a peer older than this release
    out = env.get_template("_cluster_body.html").render(nodes=nodes, can_edit=False,
                                                        discovery_error="")
    assert "link down" not in out
    print("dashboard / fleet view: a link that is down is a problem, tagged 'link down' "
          "with its reason; a peer without the field shows as before  OK")


def test_metrics():
    holder, port = remote()
    pw = "link-pass-1"
    users = [{"username": "admin", "password_hash": hash_password(pw), "role": "admin",
              "pwd_version": 1}]
    maps = [{"id": "uplink000001", "name": "Uplink", "enabled": True,
             "serial": {"port": "/dev/ttyNOPE-uplink"},
             "network": {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
                         "remote_port": port, "connect_timeout_s": 2}},
            {"id": "listen000001", "name": "Listener", "enabled": True,
             "serial": {"port": "/dev/ttyNOPE-listen"},
             "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                         "port": free_port()}}]
    try:
        with Server({"users": users, "mappings": maps}) as srv:
            b = Browser(srv.base)
            b.login("admin", pw)
            failures = re.compile(r'ser2net_mapping_connect_failures_total\{mapping="Uplink",'
                                  r'id="uplink000001"\} [1-9]')
            end = time.monotonic() + 15
            while True:
                st, _h, metrics = b.get("/metrics")
                if failures.search(metrics):
                    break
                assert st == 200 and time.monotonic() < end, metrics
                time.sleep(0.3)
            assert 'ser2net_mapping_link_up{mapping="Uplink",id="uplink000001"} 0' in metrics
            assert 'ser2net_mapping_link_up{mapping="Listener"' not in metrics, metrics
            assert 'ser2net_mapping_connect_failures_total{mapping="Listener"' not in metrics
            assert 'ser2net_mapping_running{mapping="Uplink"' in metrics  # still there
            holder.listen()
            end = time.monotonic() + 20
            while True:
                st, _h, metrics = b.get("/metrics")
                if 'ser2net_mapping_link_up{mapping="Uplink",id="uplink000001"} 1' in metrics:
                    break
                assert time.monotonic() < end, metrics
                time.sleep(0.3)
    finally:
        holder.close()
    print("/metrics: link_up / connect_failures_total for connect-out mappings only, "
          "link_up 0 -> 1 once the remote server listens  OK")


if __name__ == "__main__":
    test_link_states()
    test_serial_error_kept()
    test_notifications()
    test_dashboard_markup()
    test_metrics()
    print("\nPASS: a connect-out mapping reports its link to the remote server")
