"""Health notifications: a silent device, an offline cluster node, the start-up summary.

Checks (no sockets except the last part's server, in a netns):
  * RunnerStatus notes what the device sends at each look: how long it has sent nothing
    since the port opened or its last byte, how long what was sent to it has had no
    answer (reply-only), and when it last sent something;
  * a mapping's no-data alarm (off by default) is past its limit only while the mapping
    runs; no_data is logged (all.log and the mapping's log) and sent once, data_back when
    the device talks again; the dashboard, /metrics and the REST status show it;
  * cluster nodes heard from in this run that are not heard from for PEER_TTL, and
    configured peers that miss PEER_PROBE_MISSES answers in a row, are logged and sent
    (peer_offline / peer_back); probes only run while peer events are chosen;
  * SERVICE_STARTED_S after the start, service_started sums up which mappings run (and
    whether the watchdog ended the previous run), once;
  * a stop lets what is queued go out (at most DRAIN_S);
  * none of the new events is in the default events; the settings page and the mapping
    form offer them; a mapping form from an older node keeps the stored alarm.

Run: python3 tests/test_health_events.py
"""
import asyncio
import types

from _server import Browser, Server, free_port

from app import notify
from app.config import (
    NOTIFY_DEFAULT_EVENTS, NOTIFY_EVENTS, AppConfig, MappingConfig, NotificationSettings,
)
from app.engine import bridge
from app.engine.cluster import PEER_TTL
from app.web.auth import hash_password
from app.web.routes import forms


class Clock:
    def __init__(self):
        self.mono, self.wall = 100.0, 1_800_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def tick(self, s):
        self.mono += s
        self.wall += s

    def __getattr__(self, name):
        return getattr(__import__("time"), name)


def test_status_watches_the_device():
    clock, real = Clock(), bridge.time
    bridge.time = clock
    try:
        st = bridge.RunnerStatus(state="device-missing")
        d = st.as_dict()
        assert d["rx_idle_s"] is None and d["reply_wait_s"] is None and d["last_rx"] is None
        st.state = "running"  # the port opened
        st.as_dict()
        clock.tick(30)
        d = st.as_dict()
        assert d["rx_idle_s"] == 30 and d["reply_wait_s"] is None, d
        st.bytes_in += 5
        clock.tick(2)
        d = st.as_dict()
        assert d["rx_idle_s"] == 0 and d["last_rx"] == clock.wall, d
        st.bytes_out += 8  # a request ...
        clock.tick(2)
        st.as_dict()
        clock.tick(40)
        d = st.as_dict()
        assert d["reply_wait_s"] == 40 and d["rx_idle_s"] == 42, d  # ... with no answer
        st.bytes_in += 3  # the answer
        clock.tick(2)
        d = st.as_dict()
        assert d["reply_wait_s"] is None and d["rx_idle_s"] == 0, d
        st.bytes_out += 8
        st.bytes_in += 3  # asked and answered between two looks
        clock.tick(2)
        assert st.as_dict()["reply_wait_s"] is None
        st.state = "reconnecting"
        assert st.as_dict()["rx_idle_s"] is None
    finally:
        bridge.time = real
    print("RunnerStatus: silence since the port opened / the last byte, unanswered requests, "
          "last byte's time  OK")


def mapping(mid, alarm=0, reply_only=False, enabled=True):
    return MappingConfig.from_dict({"id": mid, "name": f"map {mid}", "enabled": enabled,
                                    "serial": {"port": f"/dev/tty{mid}"},
                                    "options": {"no_data_alarm_s": alarm,
                                                "no_data_reply_only": reply_only}})


class State:
    def __init__(self, mappings, events):
        self.config = AppConfig.from_dict({})
        self.config.mappings = mappings
        self.config.notifications = NotificationSettings.from_dict({
            "enabled": True, "events": events, "repeat_s": 0, "webhook_url": "https://x/y"})
        self.logs, self.mapping_logs = [], []
        self.previous_hang = ""
        self.cluster = None

    def log(self, msg):
        self.logs.append(msg)

    def make_mapping_logger(self, m):
        return lambda msg: (self.mapping_logs.append((m.id, msg)), self.log(f"[{m.name}] {msg}"))


def test_no_data_alarm():
    assert not set(NOTIFY_DEFAULT_EVENTS) & {"no_data", "data_back", "peer_offline", "peer_back",
                                             "service_started"}, "new events are opt-in"
    for e in ("no_data", "data_back", "peer_offline", "peer_back", "service_started"):
        assert e in NOTIFY_EVENTS and notify.title(e) != e
    plain, asks, off = mapping("a", 30), mapping("b", 60, reply_only=True), mapping("c")
    run = {"state": "running", "device": "/dev/ttyA"}
    assert notify.no_data_for(plain, {**run, "rx_idle_s": 29.5}) == 0
    assert notify.no_data_for(plain, {**run, "rx_idle_s": 31.2}) == 31
    assert notify.no_data_for(plain, {"state": "reconnecting", "rx_idle_s": 99}) == 0
    assert notify.no_data_for(off, {**run, "rx_idle_s": 10_000}) == 0, "off by default"
    assert notify.no_data_for(asks, {**run, "rx_idle_s": 500, "reply_wait_s": None}) == 0
    assert notify.no_data_for(asks, {**run, "rx_idle_s": 500, "reply_wait_s": 61}) == 61
    # out-of-range values are brought into range with a note
    m = MappingConfig.from_dict({"name": "x", "serial": {"port": "/dev/x"},
                                 "options": {"no_data_alarm_s": -4}})
    assert m.options.no_data_alarm_s == 0 and any("no-data alarm must be" in w for w in m.input_warnings()), m.input_notes
    assert mapping("x", 5).options.no_data_alarm_s == 10
    assert mapping("x", 10**9).options.no_data_alarm_s == 7 * 86400

    state = State([plain, asks, off], ["no_data", "data_back"])
    n = notify.Notifier(state)
    queued = []
    n._queue = types.SimpleNamespace(put_nowait=queued.append)
    statuses = {"a": {**run, "rx_idle_s": 45}, "b": {**run, "rx_idle_s": 45, "reply_wait_s": 3},
                "c": {**run, "rx_idle_s": 45}}
    n.check_silence(statuses)
    assert queued == [("no_data", "map a", "no data from /dev/ttyA for 45 s")], queued
    assert state.mapping_logs == [("a", "no data from /dev/ttyA for 45 s (no-data alarm: 30 s)")]
    n.check_silence(statuses)
    assert len(queued) == 1, "sent once while it stays silent"
    statuses["a"]["rx_idle_s"] = 1
    statuses["b"]["reply_wait_s"] = 70
    n.check_silence(statuses)
    assert queued[1:] == [("data_back", "map a", "/dev/ttyA"),
                          ("no_data", "map b", "no answer from /dev/ttyA for 70 s")], queued
    assert ("a", "data from /dev/ttyA again") in state.mapping_logs
    statuses["b"] = {"state": "stopped"}  # stopped while silent: no "back"
    n.check_silence(statuses)
    assert len(queued) == 3 and n.silent == set(), (queued, n.silent)
    print("no-data alarm: off by default, past its limit only while running, reply-only, "
          "logged + no_data once, data_back; range normalized  OK")


class Cluster:
    def __init__(self):
        self.live, self.manual, self.answers, self.asked = [], [], {}, []
        self.cluster = types.SimpleNamespace(active=True)

    def peers(self):
        return list(self.live)

    def manual_targets(self):
        return list(self.manual)

    async def fetch_peer(self, t):
        self.asked.append(t["ip"])
        return self.answers.get(t["ip"])


async def _peers():
    state = State([], ["peer_offline", "peer_back"])
    c = state.cluster = Cluster()
    n = notify.Notifier(state)
    queued = []
    n._queue = types.SimpleNamespace(put_nowait=queued.append)
    clock, real = Clock(), notify.time
    notify.time = clock
    try:
        node = {"id": "abc123456789", "name": "plant-2", "ip": "10.0.0.5", "port": 8080}
        c.live = [node]
        await n.check_peers()
        assert queued == [] and state.logs == []
        c.live = []           # its beacons stop (cluster.peers() drops it after PEER_TTL)
        clock.tick(PEER_TTL - 1)
        await n.check_peers()
        assert queued == [], "not before PEER_TTL"
        clock.tick(2)
        await n.check_peers()
        assert queued == [("peer_offline", "plant-2", f"10.0.0.5:8080: not heard from for {PEER_TTL:g} s")], queued
        assert state.logs[-1].startswith("cluster: node 'plant-2' offline"), state.logs
        clock.tick(30)
        await n.check_peers()
        assert len(queued) == 1, "once"
        c.live = [node]
        await n.check_peers()
        assert queued[-1] == ("peer_back", "plant-2", "10.0.0.5:8080"), queued
        # a configured peer (no beacons): asked every PEER_PROBE_S, offline after 2 misses
        c.manual = [{"ip": "192.168.7.9", "port": 8080, "scheme": "http", "source": "manual"},
                    {"ip": "10.0.0.5", "port": 8080, "scheme": "http", "source": "manual"}]
        queued.clear()
        clock.tick(notify.PEER_PROBE_S)
        await n.check_peers()
        assert c.asked == ["192.168.7.9"], "a peer heard by its beacons is not asked"
        clock.tick(10)
        await n.check_peers()
        assert c.asked == ["192.168.7.9"], "not before PEER_PROBE_S"
        clock.tick(notify.PEER_PROBE_S)
        await n.check_peers()
        assert queued == [("peer_offline", "192.168.7.9:8080",
                           "192.168.7.9:8080: no answer to 2 requests 60 s apart")], queued
        clock.tick(notify.PEER_PROBE_S)
        await n.check_peers()
        assert len(queued) == 1
        c.answers["192.168.7.9"] = {"node": "far"}
        clock.tick(notify.PEER_PROBE_S)
        await n.check_peers()
        assert queued[-1] == ("peer_back", "192.168.7.9:8080", "192.168.7.9:8080"), queued
        # peer events not chosen: nothing is asked
        state.config.notifications.events = ["device_lost"]
        c.asked.clear()
        clock.tick(notify.PEER_PROBE_S * 2)
        await n.check_peers()
        assert c.asked == []
    finally:
        notify.time = real
    print("cluster nodes: offline after PEER_TTL without beacons, back; configured peers "
          "probed (only with peer events chosen), offline after 2 misses, back  OK")


async def _started_and_drain():
    ms = [mapping("a"), mapping("b"), mapping("c", enabled=False)]
    state = State(ms, ["service_started"])
    state.previous_hang = ("WARNING: the previous run was ended by the event loop watchdog "
                           "(2026-11-12 10:00:00 ...)")
    n = notify.Notifier(state)
    sent = []

    async def deliver(event, mapping_name, detail, settings=None, warnings=None):
        await asyncio.sleep(0.2)
        sent.append((event, detail))
        return []
    n.deliver = deliver
    clock, real = Clock(), notify.time
    notify.time = clock
    try:
        state.supervisor = types.SimpleNamespace(all_status=lambda: {
            "a": {"state": "running"},
            "b": {"state": "device-missing", "last_error": "no such device /dev/ttyb"}})
        await n.start()
        statuses = state.supervisor.all_status()
        n.check_started(statuses)
        assert n._queue.qsize() == 0, "not before SERVICE_STARTED_S"
        clock.tick(notify.SERVICE_STARTED_S)
        n.check_started(statuses)
        n.check_started(statuses)
        assert n._queue.qsize() == 1, "once"
        line = next(ln for ln in state.logs if ln.startswith("start-up:"))
        assert "1 of 2 enabled mapping(s) running; not running: map b (device-missing: no such " \
               "device /dev/ttyb)" in line, line
        n.emit("service_started", "other", "", "second")  # (another key: also queued)
        await n.stop()  # both go out before the worker stops
        assert [e for e, _d in sent] == ["service_started", "service_started"], sent
        assert "ended by the event loop watchdog" in sent[0][1], sent
    finally:
        notify.time = real
    print("service_started: once, after SERVICE_STARTED_S, names what doesn't run and a "
          "watchdog restart; a stop sends what is queued  OK")


def test_form_keeps_the_alarm_for_older_nodes():
    stored = mapping("a", 120, reply_only=True)
    form = {"id": "a", "name": "map a", "kind": "net", "enabled": "on", "serial_port": "/dev/ttya",
            "network_mode": "server", "network_protocol": "raw", "network_port": "4001"}
    new = forms.mapping_from_form(form, stored, [])
    assert (new.options.no_data_alarm_s, new.options.no_data_reply_only) == (120, True), \
        "a form without the field (an older node's) keeps the stored alarm"
    form.update(opt_no_data_alarm_s="0")
    new = forms.mapping_from_form(form, stored, [])
    assert (new.options.no_data_alarm_s, new.options.no_data_reply_only) == (0, False)
    form.update(opt_no_data_alarm_s="45", opt_no_data_reply_only="on")
    new = forms.mapping_from_form(form, stored, [])
    assert (new.options.no_data_alarm_s, new.options.no_data_reply_only) == (45, True)
    print("mapping form: the alarm is set and cleared; a form without it keeps it  OK")


def test_ui_and_metrics():
    users = [{"username": "admin", "password_hash": hash_password("health-pass-1"),
              "role": "admin", "pwd_version": 1}]
    m = {"id": "q1", "name": "quiet", "serial": {"port": "/dev/ser2net-test-none"},
         "network": {"bind_ip": "127.0.0.1", "port": free_port()},
         "options": {"no_data_alarm_s": 30}}
    with Server({"users": users, "mappings": [m]}) as srv:
        b = Browser(srv.base)
        b.login("admin", "health-pass-1")
        _st, _h, page = b.get("/settings")
        for e in ("no_data", "data_back", "peer_offline", "peer_back", "service_started"):
            assert f'name="notify_event_{e}"' in page, e
        _st, _h, form = b.get("/api/mappings/q1/form")
        assert 'name="opt_no_data_alarm_s"' in form and 'value="30"' in form
        assert 'name="opt_no_data_reply_only"' in form
        _st, _h, body = b.get("/metrics")
        assert 'ser2net_mapping_last_rx_timestamp_seconds{mapping="quiet",id="q1"} 0.000' in body
        assert 'ser2net_mapping_no_data{mapping="quiet",id="q1"} 0' in body, body[-800:]
    print("Settings offers the new events; the mapping form has the alarm; /metrics: last "
          "byte's time and the alarm  OK")


def main():
    test_status_watches_the_device()
    test_no_data_alarm()
    asyncio.run(_peers())
    asyncio.run(_started_and_drain())
    test_form_keeps_the_alarm_for_older_nodes()
    test_ui_and_metrics()
    print("\nPASS: health notifications")


if __name__ == "__main__":
    main()
