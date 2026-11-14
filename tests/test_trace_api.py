"""Trace files in the REST API, and timed captures (ser2netweb/capture.py).

Checks:
  * GET /api/v1/mappings/{id}/traces lists a mapping's trace files (name, size, time) and
    GET .../traces/{name} downloads one for an operator or admin token only (a viewer
    token: 403, as a viewer gets no Trace panel), only a name of the mapping's own list,
    the download audited as api_trace_download; DELETE .../traces/{name} and DELETE .../traces delete one or all
    (viewer token 403), audited as api_trace_delete; the OpenAPI document has them all;
  * a timed capture, by the REST API and in the Trace panel: the mapping is traced to
    traces/<id>-capture.log without a restart of its runner, the end time is stored in
    config.json (options.trace_until), a mapping that is traced already is refused, and
    stopping it closes the file; viewers may not start one; all of it audited;
  * a capture whose end time is stored goes on after a restart and ends by itself (the
    file is closed, trace_until cleared, audited as trace_capture_end); one whose time
    passed while the server was down is ended before the mapping starts (no file); the
    end of a capture never turns off a trace that is on by its trace file name; a capture
    extended just as its end comes (the watcher waiting for the configuration) goes on;
  * a config.json without trace_until loads as before; it is no setting: an edit form
    saved while a capture runs, a REST PUT (of what a GET answered, without it or with
    another value) and a PATCH that names it keep the stored one (and the runner), a REST
    create, a Copy and a new mapping of an import have none, an import keeps a mapping's
    own.

Run: python3 tests/test_trace_api.py
"""
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

from _formsubmit import browser_post
from _server import Browser, Server, free_port

from ser2netweb import capture, importer
from ser2netweb.config import CAPTURE_MAX_S, MappingConfig, mapping_revision
from ser2netweb.engine import bridge
from ser2netweb.engine.supervisor import Supervisor
from ser2netweb.web.auth import hash_password, hash_token

PW = "trace-api-pass-1"
A, B, C = "bbbbbbbbbbb1", "bbbbbbbbbbb2", "bbbbbbbbbbb3"
VIEW, OP, ADMIN = "s2n_trace-api-viewer", "s2n_trace-api-operator", "s2n_trace-api-admin"
USERS = [{"username": u, "password_hash": hash_password(PW), "role": r, "pwd_version": 1}
         for u, r in (("admin", "admin"), ("op", "operator"), ("view", "viewer"))]
TOKENS = [{"id": "t1", "name": "ci", "sha256": hash_token(OP), "role": "operator"},
          {"id": "t2", "name": "dash", "sha256": hash_token(VIEW), "role": "viewer"},
          {"id": "t3", "name": "root", "sha256": hash_token(ADMIN), "role": "admin"}]


def mapping(mid, name, **options):
    return {"id": mid, "name": name, "enabled": True, "kind": "net",
            "serial": {"port": f"/dev/ser2net-trace-api-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": free_port()},
            "options": options}


def wait(cond, what, timeout=15.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, f"timed out: {what}"
        time.sleep(0.1)


def open_files(srv):
    """What the server process has open (None where /proc can't tell)."""
    if not sys.platform.startswith("linux"):
        return None
    out = set()
    fd_dir = f"/proc/{srv.proc.pid}/fd"
    for fd in os.listdir(fd_dir):
        with contextlib.suppress(OSError):
            out.add(os.readlink(os.path.join(fd_dir, fd)))
    return out


def api(srv, method, path, token=OP, body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(srv.base + "/api/v1" + path, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def audit(srv, action):
    with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
        return [ln.split("\t") for ln in fh.read().splitlines() if f"\t{action}\t" in ln]


def stored(srv, mid):
    return next(m for m in srv.saved_config()["mappings"] if m["id"] == mid)["options"]


def test_rest_traces():
    srv = Server({"users": USERS, "api_tokens": TOKENS,
                  "mappings": [mapping(A, "A", trace_both="a.log"), mapping(B, "B")]})
    traces = os.path.join(srv.data, "traces")
    os.makedirs(traces)
    for name in (f"{A}-a.log.1", f"{A}-old.log", f"{B}-b.log"):
        with open(os.path.join(traces, name), "w", encoding="utf-8", newline="") as fh:
            fh.write(f"content of {name}\n")  # byte-exact (no \r\n on Windows)
    with srv:
        wait(lambda: os.path.isfile(os.path.join(traces, f"{A}-a.log")), "A's trace opened")
        st, _, body = api(srv, "GET", f"/mappings/{A}/traces", VIEW)
        assert st == 403 and json.loads(body)["error"] == "forbidden", (st, body)
        assert b"content of" not in body and f"{A}-old.log".encode() not in body
        for token in (OP, ADMIN):
            st, _, body = api(srv, "GET", f"/mappings/{A}/traces", token)
            view = json.loads(body)
            assert st == 200 and view["id"] == A and view["tracing"] is True, (st, view)
            assert view["file"] == f"{A}-a.log" and view["capture_until"] is None, view
            assert {t["name"] for t in view["traces"]} == {
                f"{A}-a.log", f"{A}-a.log.1", f"{A}-old.log"}, view
            one = next(t for t in view["traces"] if t["name"] == f"{A}-old.log")
            assert one["size"] == len(f"content of {A}-old.log\n") and \
                abs(one["time"] - time.time()) < 120, one
        st, _, body = api(srv, "GET", f"/mappings/{B}/traces")
        assert json.loads(body)["tracing"] is False and json.loads(body)["file"] is None
        assert api(srv, "GET", "/mappings/nosuch/traces")[0] == 404
        assert api(srv, "GET", f"/mappings/{A}/traces", token=None)[0] == 401
        print("list: name, size and time of the mapping's own files, operator and admin "
              "tokens only  OK")

        st, _, body = api(srv, "GET", f"/mappings/{A}/traces/{A}-old.log", VIEW)
        assert st == 403 and json.loads(body)["error"] == "forbidden", (st, body)
        st, h, body = api(srv, "GET", f"/mappings/{A}/traces/{A}-old.log")
        assert st == 200 and body == f"content of {A}-old.log\n".encode(), (st, body)
        assert "attachment" in h["Content-Disposition"] and h["Content-Length"] == str(len(body))
        for bad in (f"{B}-b.log", "..%2Fconfig.json", "%2E%2E%2F%2E%2E%2Fall.log", "no-such"):
            assert api(srv, "GET", f"/mappings/{A}/traces/{bad}")[0] == 404, bad
            assert api(srv, "DELETE", f"/mappings/{A}/traces/{bad}")[0] == 404, bad
        records = audit(srv, "api_trace_download")
        assert [(r[3], r[4]) for r in records] == [(f"A/{A}-old.log", "api-token:ci")], records
        print("download: operator and admin tokens only, a name of its own list, audited  OK")

        st, _, body = api(srv, "DELETE", f"/mappings/{A}/traces/{A}-old.log", VIEW)
        assert st == 403 and os.path.isfile(os.path.join(traces, f"{A}-old.log")), st
        assert api(srv, "DELETE", f"/mappings/{A}/traces", VIEW)[0] == 403
        st, _, body = api(srv, "DELETE", f"/mappings/{A}/traces/{A}-old.log")
        assert st == 200 and json.loads(body) == {"id": A, "deleted": [f"{A}-old.log"],
                                                  "failed": []}, (st, body)
        assert not os.path.exists(os.path.join(traces, f"{A}-old.log"))
        st, _, body = api(srv, "DELETE", f"/mappings/{A}/traces")
        assert st == 200 and sorted(json.loads(body)["deleted"]) == [f"{A}-a.log", f"{A}-a.log.1"]
        assert os.listdir(traces) == [f"{B}-b.log"], os.listdir(traces)
        st, _, body = api(srv, "DELETE", f"/mappings/{A}/traces")
        assert st == 200 and json.loads(body)["deleted"] == [], body
        assert api(srv, "DELETE", "/mappings/nosuch/traces")[0] == 404
        assert json.loads(api(srv, "GET", f"/mappings/{A}/traces")[2])["tracing"] is True
        details = [r[3] for r in audit(srv, "api_trace_delete")]
        assert details[0] == f"A/{A}-old.log" and details[1].startswith("A: 2 file(s): "), details
        print("delete: one or all, viewer tokens 403, the trace goes on, audited  OK")

        spec = json.loads(api(srv, "GET", "/openapi.json", token=None)[2])
        paths = spec["paths"]
        assert set(paths["/mappings/{mid}/traces"]) >= {"get", "delete"}
        assert set(paths["/mappings/{mid}/traces/{name}"]) >= {"get", "delete"}
        assert "403" in paths["/mappings/{mid}/traces/{name}"]["get"]["responses"]
        assert "403" in paths["/mappings/{mid}/traces"]["get"]["responses"]
        assert set(paths["/mappings/{mid}/capture"]) >= {"post", "delete"}
        assert "trace_until" in spec["components"]["schemas"]["MappingOptions"]["properties"]
        info = json.loads(api(srv, "GET", "/info")[2])
        assert {"traces", "capture"} <= set(info["features"]), info
        print("OpenAPI: the trace and capture calls are described; /info names them  OK")


def test_capture_live():
    srv = Server({"users": USERS, "api_tokens": TOKENS,
                  "mappings": [mapping(A, "A", trace_both="a.log"), mapping(B, "B")]})
    capture_file = os.path.join(srv.data, "traces", f"{B}-capture.log")
    with srv:
        assert not os.path.exists(capture_file)
        started = json.loads(api(srv, "GET", f"/mappings/{B}")[2])["status"]
        # ---- REST ----
        assert api(srv, "POST", f"/mappings/{B}/capture", VIEW, {"minutes": 15})[0] == 403
        for bad in ({}, {"minutes": 0}, {"minutes": "15"}, {"minutes": 1441}, {"minutes": True},
                    [15]):
            st, _, body = api(srv, "POST", f"/mappings/{B}/capture", body=bad)
            assert st == 400 and json.loads(body)["error"] == "validation", (bad, st, body)
        assert api(srv, "POST", "/mappings/nosuch/capture", body={"minutes": 15})[0] == 404
        st, _, body = api(srv, "POST", f"/mappings/{A}/capture", body={"minutes": 15})
        assert st == 409 and json.loads(body)["error"] == "conflict", (st, body)
        assert not os.path.exists(capture_file)
        etag = api(srv, "GET", f"/mappings/{B}")[1]["ETag"]
        t0 = time.time()
        st, _, body = api(srv, "POST", f"/mappings/{B}/capture", body={"minutes": 15})
        view = json.loads(body)
        assert st == 200 and view["tracing"] is True and view["file"] == f"{B}-capture.log", view
        assert t0 + 895 < view["capture_until"] < time.time() + 905, view
        assert os.path.isfile(capture_file), "the capture file is opened at once"
        assert t0 + 895 < stored(srv, B)["trace_until"] < time.time() + 905
        assert stored(srv, B)["trace_both"] == "", "a capture never sets a trace file name"
        fds = open_files(srv)
        assert fds is None or capture_file in fds
        now = json.loads(api(srv, "GET", f"/mappings/{B}")[2])["status"]
        assert now["state"] == started["state"] and "restart" not in srv.output().split(
            "timed capture started")[1], "the runner was not restarted"
        assert api(srv, "GET", f"/mappings/{B}")[1]["ETag"] == etag, \
            "a capture does not change the mapping's revision"
        # again: the new end replaces the old
        st, _, body = api(srv, "POST", f"/mappings/{B}/capture", body={"minutes": 60})
        assert st == 200 and json.loads(body)["capture_until"] > time.time() + 3500
        st, _, body = api(srv, "PATCH", f"/mappings/{B}", body={"name": "B2"})
        assert st == 200 and json.loads(body)["options"]["trace_until"] > time.time() + 3500
        wait(lambda: os.path.isfile(capture_file), "traced again after the edit")
        until = stored(srv, B)["trace_until"]
        # an edit form saved meanwhile keeps the capture
        op = Browser(srv.base)
        op.login("op", PW)
        st, _, form = op.get(f"/api/mappings/{B}/form")
        assert st == 200 and "trace_until" not in form
        st, _, body = op.post("/api/mappings/save", dict(browser_post(form)), htmx=True)
        assert st == 200 and stored(srv, B)["trace_until"] == until, (st, body[:300])
        # trace_until is no setting: a PUT of what a GET answered, one without it or with
        # another value, and a PATCH that names it keep the stored one and the runner
        got = json.loads(api(srv, "GET", f"/mappings/{B}")[2])
        got.pop("status")
        for value in (0, until + 9999, None):
            sent = json.loads(json.dumps(got))
            if value is None:
                del sent["options"]["trace_until"]
            else:
                sent["options"]["trace_until"] = value
            st, _, body = api(srv, "PUT", f"/mappings/{B}", body=sent)
            assert st == 200 and json.loads(body)["changed"] is False, (value, st, body[:300])
            assert stored(srv, B)["trace_until"] == until, value
        st, _, body = api(srv, "PATCH", f"/mappings/{B}", body={"options": {"trace_until": 0}})
        assert st == 200 and json.loads(body)["changed"] is False, body[:300]
        assert stored(srv, B)["trace_until"] == until and os.path.isfile(capture_file)
        # ... and a new mapping (REST create, the dashboard's Copy) has none
        st, _, body = api(srv, "POST", "/mappings", body={
            **mapping("x", "made", trace_until=until), "enabled": False})
        made = json.loads(body)
        assert st == 201 and made["options"]["trace_until"] == 0, (st, body[:300])
        assert api(srv, "DELETE", f"/mappings/{made['id']}")[0] == 200
        assert op.post(f"/api/mappings/{B}/duplicate", htmx=True)[0] == 200
        copy = next(m for m in srv.saved_config()["mappings"] if m["name"] == "B2 (copy)")
        assert copy["options"]["trace_until"] == 0, copy["options"]
        assert api(srv, "DELETE", f"/mappings/{copy['id']}")[0] == 200
        st, _, body = api(srv, "DELETE", f"/mappings/{B}/capture", VIEW)
        assert st == 403
        st, _, body = api(srv, "DELETE", f"/mappings/{B}/capture")
        view = json.loads(body)
        assert st == 200 and view["changed"] is True and view["tracing"] is False and \
            view["capture_until"] is None, view
        assert stored(srv, B)["trace_until"] == 0
        fds = open_files(srv)
        assert fds is None or capture_file not in fds, "the capture file is closed"
        assert os.path.isfile(capture_file), "what was recorded stays"
        st, _, body = api(srv, "DELETE", f"/mappings/{B}/capture")
        assert st == 200 and json.loads(body)["changed"] is False
        st, _, body = api(srv, "DELETE", f"/mappings/{A}/capture")
        assert json.loads(body)["changed"] is False and json.loads(body)["tracing"] is True
        assert stored(srv, A)["trace_both"] == "a.log"
        assert [r[4] for r in audit(srv, "api_trace_capture_start")] == ["api-token:ci"] * 2
        assert "B: 15 min, until " in audit(srv, "api_trace_capture_start")[0][3]
        assert [r[3] for r in audit(srv, "api_trace_capture_stop")] == ["B2"]
        print("REST: a capture begins without a restart, is stored, replaced, stopped; a "
              "traced mapping is refused; viewer tokens 403; audited  OK")

        # ---- the Trace panel ----
        view_b = Browser(srv.base)
        view_b.login("view", PW)
        st, _, body = view_b.post(f"/api/mappings/{B}/capture", {"minutes": "15"}, htmx=True)
        assert st == 403 and "requires the operator role" in body, (st, body[:200])
        assert view_b.post(f"/api/mappings/{B}/capture", method="DELETE", htmx=True)[0] == 403
        st, _, body = op.get("/api/status")
        assert f'hx-get="/api/mappings/{B}/traces"' in body, "Trace is offered without files"
        st, _, body = view_b.get("/api/status")
        assert f"/api/mappings/{B}/traces" not in body, "not to viewers"
        st, _, page = op.get(f"/api/mappings/{B}/traces")
        assert st == 200 and 'id="capture-form"' in page and 'id="capture-stop"' not in page
        assert all(f'<option value="{n}">' in page for n in capture.CHOICES_MIN), page
        st, _, page = op.get(f"/api/mappings/{A}/traces")
        assert 'id="capture-form"' not in page, "a traced mapping has nothing to time"
        for bad in ("", "7", "abc", "100000"):
            st, _, page = op.post(f"/api/mappings/{B}/capture", {"minutes": bad}, htmx=True)
            assert st == 400 and "Choose how long to record" in page, (bad, st)
        st, _, page = op.post(f"/api/mappings/{A}/capture", {"minutes": "15"}, htmx=True)
        assert st == 400 and "traced already" in page, (st, page[:300])
        st, _, page = op.post(f"/api/mappings/{B}/capture", {"minutes": "60"}, htmx=True)
        assert st == 200 and "Recording until" in page and 'id="capture-stop"' in page, page[:600]
        assert "Timed recording: stops by itself at" in page and f"{B}-capture.log" in page
        assert stored(srv, B)["trace_until"] > time.time() + 3500
        st, _, page = op.post(f"/api/mappings/{B}/capture", method="DELETE", htmx=True)
        assert st == 200 and "Recording stopped." in page and 'id="capture-stop"' not in page
        assert stored(srv, B)["trace_until"] == 0
        assert [(r[3][:17], r[4]) for r in audit(srv, "trace_capture_start")] == [
            ("B2: 1 h, until 20", "op")], audit(srv, "trace_capture_start")
        assert [(r[3], r[4]) for r in audit(srv, "trace_capture_stop")] == [("B2", "op")]
        print("Trace panel: operators start (15 min / 1 h / 24 h) and stop a capture; "
              "viewers 403; audited  OK")


def test_capture_survives_restart():
    now = time.time()
    srv = Server({"users": USERS, "api_tokens": TOKENS, "mappings": [
        mapping(A, "permanent", trace_both="a.log", trace_until=now + 4),
        mapping(B, "running", trace_until=now + 4),
        mapping(C, "overdue", trace_until=now - 60)]})
    traces = os.path.join(srv.data, "traces")
    a_file, b_file, c_file = (os.path.join(traces, f"{m}-{n}") for m, n in (
        (A, "a.log"), (B, "capture.log"), (C, "capture.log")))
    with srv:
        assert stored(srv, C)["trace_until"] == 0, "ended before the mappings started"
        assert not os.path.exists(c_file), "an overdue capture is never traced again"
        assert "timed capture ended: its time passed while ser2netweb was not running" in \
            srv.output()
        if time.time() < now + 3:  # (a slow start: the capture may have ended already)
            assert os.path.isfile(b_file), "a stored capture goes on after a restart"
            view = json.loads(api(srv, "GET", f"/mappings/{B}/traces")[2])
            assert view["tracing"] is True and now + 3 < view["capture_until"] < now + 5, view
        wait(lambda: stored(srv, B)["trace_until"] == 0, "the capture ends by itself")
        assert time.time() >= now + 3.5, "not before its time"
        wait(lambda: stored(srv, A)["trace_until"] == 0, "the other one too")
        # (an end is audited once its trace is closed, after config.json keeps it)
        wait(lambda: len(audit(srv, "trace_capture_end")) == 3, "each end audited")
        assert os.path.isfile(b_file)
        if open_files(srv) is not None:  # (closed right after config.json keeps the end)
            wait(lambda: b_file not in open_files(srv), "its file is closed")
        fds = open_files(srv)
        assert fds is None or a_file in fds, "a trace that is on for good stays on"
        assert stored(srv, A)["trace_both"] == "a.log"
        view = json.loads(api(srv, "GET", f"/mappings/{A}/traces")[2])
        assert view["tracing"] is True and view["capture_until"] is None, view
        assert json.loads(api(srv, "GET", f"/mappings/{B}/traces")[2])["tracing"] is False
        ended = sorted(r[3] for r in audit(srv, "trace_capture_end"))
        assert ended == ["overdue", "permanent", "running"], ended
    print("restart: a stored capture goes on and ends by itself, an overdue one ends "
          "before the start, a trace that is on for good is never turned off  OK")


def test_units():
    # a 3.0.x mapping (no trace_until) loads as before; odd stored values read as none
    m = MappingConfig.from_dict({"name": "old", "options": {"trace_both": "x.log"}})
    assert m.options.trace_until == 0 and m.options.trace_file == "x.log"
    for odd in (-5, "soon", None, float("nan"), float("inf"), True, [1]):
        got = MappingConfig.from_dict({"name": "odd", "options": {"trace_until": odd}})
        assert got.options.trace_until == 0 and got.options.trace_file == "", (odd, got.options)
    timed = MappingConfig.from_dict({"name": "t", "options": {"trace_until": 4102444800}})
    assert timed.options.trace_file == "capture.log"
    assert mapping_revision("k", timed) == mapping_revision(
        "k", MappingConfig.from_dict({**timed.to_dict(), "options": {}})), \
        "trace_until is not part of a mapping's revision"
    assert capture.duration_text(900) == "15 min" and capture.duration_text(3600) == "1 h"
    assert capture.duration_text(86400) == "24 h" and capture.duration_text(5400) == "1 h 30 min"

    class State:
        """What Captures uses of an AppState."""

        def __init__(self, mappings):
            self.config = type("C", (), {"mappings": mappings, "get_mapping": lambda s, i: next(
                (m for m in mappings if m.id == i), None)})()
            self.logged, self.audited, self.retraced = [], [], []
            outer = self
            self.supervisor = type("S", (), {"retrace": lambda s, mid: outer._retrace(mid)})()

        async def _retrace(self, mid):
            self.retraced.append(mid)
            return True

        @contextlib.asynccontextmanager
        async def edit_config(self, **_kw):
            yield

        def log(self, text):
            self.logged.append(text)

        def audit(self, ip, action, detail="", user=""):
            self.audited.append((action, detail))

    async def clocks():
        # a trace_until years ahead (a REST PUT, a hand edit) never lasts past CAPTURE_MAX_S
        far = MappingConfig.from_dict({"id": "far", "name": "far",
                                       "options": {"trace_until": time.time() + 10 * 365 * 86400}})
        st = State([far])
        caps = capture.Captures(st)
        left = caps.left(far)
        assert CAPTURE_MAX_S - 5 < left <= CAPTURE_MAX_S, left
        # the time left is counted on the monotonic clock: a step of the system clock
        # neither ends a capture early nor keeps it going
        real = time.time
        try:
            time.time = lambda: real() + 30 * 86400
            await caps.check()
            assert far.options.trace_until and not st.audited, "ended by a clock step"
            time.time = lambda: real() - 30 * 86400
            assert caps.left(far) <= CAPTURE_MAX_S
        finally:
            time.time = real
        caps._ends["far"] = (far.options.trace_until, time.monotonic() - 2, 1.0)  # its time has come
        await caps.check()
        assert far.options.trace_until == 0 and st.retraced == ["far"], st.retraced
        assert st.audited == [("trace_capture_end", "far")], st.audited
        for seconds in (0, 59, CAPTURE_MAX_S + 1):
            try:
                await caps.begin("far", seconds)
            except capture.CaptureRefused as e:
                assert e.code == "duration"
            else:
                raise AssertionError(f"{seconds} s accepted")
        far.options.trace_both = "mine.log"
        try:
            await caps.begin("far", 900)
        except capture.CaptureRefused as e:
            assert e.code == "tracing"
        else:
            raise AssertionError("a traced mapping was captured")

    asyncio.run(clocks())

    async def extended_while_ending():
        # an operator extends a capture while the watcher, at its end, waits for the
        # configuration: the extension stands (compare-and-clear), nothing is ended
        m = MappingConfig.from_dict({"id": "race", "name": "race",
                                     "options": {"trace_until": time.time() + 600}})
        st = State([m])
        lock = asyncio.Lock()

        @contextlib.asynccontextmanager
        async def edit_config(**_kw):
            async with lock:
                yield

        st.edit_config = edit_config
        caps = capture.Captures(st)
        caps._ends["race"] = (m.options.trace_until, time.monotonic() - 2, 1.0)  # its time has come
        async with lock:  # (another edit holds the configuration meanwhile)
            extend = asyncio.create_task(caps.begin("race", 3600))
            await asyncio.sleep(0.05)
            watcher = asyncio.create_task(caps.check())
            await asyncio.sleep(0.05)
        until = await extend
        await watcher
        assert m.options.trace_until == until > time.time() + 3500, m.options.trace_until
        assert not st.audited and not any("ended" in ln for ln in st.logged), st.logged
        assert 3500 < caps.left(m) <= 3600, caps.left(m)
        # an operator's stop (no `until`) still ends it whatever its end
        assert await caps.end("race") and m.options.trace_until == 0

    asyncio.run(extended_while_ending())

    async def frozen_clock():
        # where the monotonic clock has not moved since the start (a coarse one, as on
        # Windows), the time left is the length itself, never a hair over it
        m = MappingConfig.from_dict({"id": "exact", "name": "exact"})
        caps = capture.Captures(State([m]))
        real = time.monotonic
        try:
            time.monotonic = lambda: 1000.1  # ((1000.1 + 3600) - 1000.1 > 3600 in floats)
            await caps.begin("exact", 3600)
            assert caps.left(m) == 3600.0, caps.left(m)
            time.monotonic = lambda: 1000.1 + 1800
            assert caps.left(m) == 1800.0, caps.left(m)
        finally:
            time.monotonic = real

    asyncio.run(frozen_clock())

    async def hot_swap():
        # bridge.retrace opens and closes a runner's trace as its options ask for now
        with tempfile.TemporaryDirectory() as tmp:
            m = MappingConfig.from_dict({"id": "hot", "name": "hot"})
            runner = type("R", (), {"mapping": m, "_tracer": None,
                                    "status": bridge.RunnerStatus()})()
            log = []
            assert await bridge.retrace(runner, tmp, log.append) is False
            m.options.trace_until = time.time() + 60
            assert await bridge.retrace(runner, tmp, log.append) is True
            assert await bridge.retrace(runner, tmp, log.append) is False
            runner._tracer.write("ser>net", b"hello")
            m.options.trace_until = 0.0
            assert await bridge.retrace(runner, tmp, log.append) is True
            assert runner._tracer is None
            with open(os.path.join(tmp, "hot-capture.log"), "rb") as fh:
                assert b"ser>net hello" in fh.read()
            assert any("ended" in line for line in log), log

    asyncio.run(hot_swap())

    async def another_object():
        # a runner built from an equal mapping that is not the configuration's object (kept
        # across a merge import): Supervisor.retrace hands it the capture's end
        with tempfile.TemporaryDirectory() as tmp:
            current = MappingConfig.from_dict({"id": "kept", "name": "kept"})
            built = MappingConfig.from_dict(current.to_dict())
            status = bridge.RunnerStatus()
            status.state = "running"
            runner = type("R", (), {"mapping": built, "_tracer": None, "status": status})()
            runner.retrace = lambda: bridge.retrace(runner, tmp, lambda _m: None)
            sup = Supervisor(trace_dir=tmp, lookup=lambda mid: current)
            sup._runners["kept"] = runner
            current.options.trace_until = time.time() + 60
            assert await sup.retrace("kept") is True and runner._tracer is not None
            assert built.to_dict() == current.to_dict(), "still the settings it was built from"
            current.options.trace_until = 0.0
            assert await sup.retrace("kept") is True and runner._tracer is None
            status.state = "stopped"
            current.options.trace_until = time.time() + 60
            assert await sup.retrace("kept") is False and runner._tracer is None, \
                "a stopped runner opens its trace when it starts"
            assert built.options.trace_until == current.options.trace_until
            assert await sup.retrace("nosuch") is False

    asyncio.run(another_object())

    # an import keeps a mapping's own capture end and brings none for a new one
    when = 4102444800.0
    current = [MappingConfig.from_dict({"id": "k1", "name": "k1",
                                        "options": {"trace_until": when}})]
    incoming = [MappingConfig.from_dict({"id": "k1", "name": "k1"}),
                MappingConfig.from_dict({"id": "k2", "name": "k2",
                                         "options": {"trace_until": when}})]
    for mode in importer.MODES:
        result, touched = importer.apply_import(current, incoming, mode)
        assert {m.id: m.options.trace_until for m in result} == {"k1": when, "k2": 0}, mode
        if mode == "merge":
            assert [m.id for m in touched] == ["k2"], "k1 is unchanged: its runner stays"
    print("units: trace_until loads leniently and is no part of the revision; a capture "
          "is bounded and counted on the monotonic clock; the trace is hot-swapped  OK")


def main():
    test_units()
    test_rest_traces()
    test_capture_live()
    test_capture_survives_restart()
    print("\nPASS: trace files in the REST API, timed captures")


if __name__ == "__main__":
    main()
