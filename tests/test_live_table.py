"""The dashboard's self-refreshing tables are answered cheaply, and off the event loop.

No server, no sockets: LiveTable (app/web/routes/common.py) and the snapshots the mappings
table and the fleet view give it.

  * the hash is "<shape>.<counters>"; asking with the current one gets 204, with only
    the counters out of date just those (htmx out-of-band swaps of the counter spans,
    HX-Reswap: none, escaped), with another shape — or a hash from before the upgrade —
    the whole table;
  * the whole table is rendered in a worker thread (not on the event loop that runs the
    bridges), once per content: another tab asking for the same table gets that copy;
    Ctx.arender (the ports, a mapping's log) renders off the loop too;
  * the hash is keyed with a secret of the process: it can't be checked against
    guessed settings (the shape covers ones the page doesn't show);
  * the mappings table's shape ignores what changes all the time — the clients'
    connection times (the browser counts them on from connected_at) and In / Out — but
    not a client coming or going; a counter span patched in shows the same text as the
    table renders;
  * the fleet view's shape ignores the uptime in seconds and the counters, unless two
    of its rows would get the same counter id (then a change is a whole table again).

Run: python3 tests/test_live_table.py
"""
import asyncio
import os
import re
import sys
import threading

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from app.config import MappingConfig  # noqa: E402
from app.engine.bridge import RunnerStatus  # noqa: E402
from app.web.routes.cluster import _fleet_snapshot  # noqa: E402
from app.web.routes.common import Ctx, LiveTable  # noqa: E402
from app.web.routes.mappings import _SHOWN, _in_out, _steady  # noqa: E402
from app.web.server import make_templates  # noqa: E402


class Request:
    def __init__(self, sent=""):
        self.headers = {"x-content-hash": sent} if sent else {}

        class state:
            lang = "en"
        self.state = state


class Template:
    """Counts its renders and records the thread they ran in."""
    def __init__(self):
        self.renders, self.threads = 0, []

    def render(self, context):
        self.renders += 1
        self.threads.append(threading.current_thread())
        return f"<table>{context['rows']}</table>"


class Templates:
    def __init__(self):
        self.tpl = Template()

    def get_template(self, name):
        return self.tpl


def ask(table, sent, shape, spans, **context):
    return asyncio.run(table.answer(Request(sent), lambda: (shape, spans), **context))


def test_answers():
    ctx = Ctx(Templates(), state=None)
    table = LiveTable(ctx, "t.html")
    spans = {"m-a-bytes": "1.0 KB / 0 B", "m-b-bytes": "<0 B>"}
    full = ask(table, "", ("rows", 2), spans, rows=2)
    tag = full.headers["X-Content-Hash"]
    shape_part, _, counters_part = tag.partition(".")
    assert full.status_code == 200 and full.body == b"<table>2</table>" and counters_part
    assert "HX-Reswap" not in full.headers
    assert ctx.templates.tpl.threads[0] is not threading.main_thread(), "rendered on the loop"

    same = ask(table, tag, ("rows", 2), dict(spans), rows=2)
    assert same.status_code == 204 and same.headers["X-Content-Hash"] == tag

    moved = dict(spans, **{"m-a-bytes": "2.0 KB / 0 B"})
    patch = ask(table, tag, ("rows", 2), moved, rows=2)
    assert patch.status_code == 200 and patch.headers.get("HX-Reswap") == "none", patch.headers
    assert patch.headers["X-Content-Hash"].partition(".")[0] == shape_part
    assert patch.body.decode() == ('<span id="m-a-bytes" hx-swap-oob="true">2.0 KB / 0 B</span>'
                                   '<span id="m-b-bytes" hx-swap-oob="true">&lt;0 B&gt;</span>'), patch.body
    assert ctx.templates.tpl.renders == 1, "only the counters: no render"

    other = ask(table, tag, ("rows", 3), moved, rows=3)
    assert other.status_code == 200 and other.body == b"<table>3</table>"
    assert "HX-Reswap" not in other.headers
    legacy = ask(table, "0123456789abcdef", ("rows", 3), moved, rows=3)   # a 2.8 tab's hash
    assert legacy.status_code == 200 and legacy.body == b"<table>3</table>"
    assert ctx.templates.tpl.renders == 2, "a second tab asking for that table gets the copy"
    nospans = ask(table, other.headers["X-Content-Hash"].partition(".")[0] + ".x", ("rows", 3),
                  None, rows=3)
    assert nospans.status_code == 200 and nospans.body == b"<table>3</table>", "no spans: no patch"
    # keyed per process: another table (another process) gives other hashes for the same
    assert ask(LiveTable(ctx, "t.html"), "", ("rows", 2), spans, rows=2).headers["X-Content-Hash"] != tag
    print("LiveTable: 204 / counters only / whole table (rendered once, in a worker thread)  OK")


def test_arender_off_loop():
    seen = []

    class T:
        def render(self, context):
            seen.append(threading.current_thread())
            return "<p>%s</p>" % context["x"]

    class Ts:
        def get_template(self, name):
            return T()
    resp = asyncio.run(Ctx(Ts(), state=None).arender(Request(), "x.html", status_code=286, x=1))
    assert resp.status_code == 286 and resp.body == b"<p>1</p>"
    assert seen and seen[0] is not threading.main_thread()
    print("Ctx.arender: in a worker thread  OK")


def status(clients=(), bytes_in=0, bytes_out=0, now=1000.0):
    st = RunnerStatus(state="running", bytes_in=bytes_in, bytes_out=bytes_out)

    class C:
        def __init__(self, peer, at):
            self.peer, self.connected_at, self.priority = peer, at, False
    st.set_clients([C(p, at) for p, at in clients])
    return st


def test_mappings_shape():
    one = [("10.0.0.5:5000", 900.0)]
    a = status(one, 10, 20).as_dict()
    assert a["clients"][0]["connected_at"] == 900.0, "REST /status: connected_at as well"
    b = status(one, 4096, 20).as_dict()
    b["clients"][0]["duration"] = "9h 59m"   # later: the time counted on
    assert _steady(a) == _steady(b), "connection times and counters are not the shape"
    assert _steady(status(one + [("10.0.0.6:1", 950.0)]).as_dict()) != _steady(a), "a new client is"
    assert _steady(status(one).as_dict()) != _steady(dict(a, state="error"))
    assert _steady(dict(a, reconnects=7, queue_overflows=3)) == _steady(a), "not shown: not the shape"
    assert _in_out(b) == "4.0 KB / 20 B" and _in_out(None) == "0 B / 0 B"
    src = open(os.path.join(ROOT, "app", "web", "templates", "_mappings_body.html"),
               encoding="utf-8").read()
    used = set(re.findall(r"\bst\.(\w+)", src))
    assert used <= set(_SHOWN) | {"bytes_in", "bytes_out", "clients"}, \
        f"the table shows {used - set(_SHOWN)}: add it to mappings._SHOWN"
    m = MappingConfig.from_dict({"id": "m1", "name": "A", "serial": {"port": "/dev/x"},
                                 "network": {"port": 4001}})
    out = make_templates().env.get_template("_mappings_body.html").render(
        mappings=[m], statuses={"m1": b}, can_edit=True)
    assert f'<span id="m-m1-bytes">{_in_out(b)}</span>' in out, "a patched span shows what the table does"
    # the time shown is counted from connected_at, in the page's language (since filter);
    # a client without it (an older peer's payload) shows the duration it came with
    assert 'data-since="900.0"' in out and re.search(r'<span class="dur">\d+h \d\dm</span>', out), out
    del b["clients"][0]["connected_at"]
    out = make_templates().env.get_template("_mappings_body.html").render(
        mappings=[m], statuses={"m1": b}, can_edit=True)
    assert "data-since" not in out and '<span class="dur">9h 59m</span>' in out, out
    print("mappings table: connection times and counters are not part of the shape  OK")


def test_fleet_shape():
    def node(addr, uptime_s, maps):
        return {"addr": addr, "uptime_s": uptime_s, "uptime": "1h 2m", "online": True,
                "mappings": maps}

    def view(*nodes):
        return {"nodes": list(nodes), "can_edit": True, "discovery_error": ""}

    m = {"id": "x1", "name": "a", "state": "running", "bytes_in": 5, "bytes_out": 6}
    s1, spans1 = _fleet_snapshot(view(node("10.0.0.1:8080", 100, [m])))
    s2, spans2 = _fleet_snapshot(view(node("10.0.0.1:8080", 160, [dict(m, bytes_in=7000)])))
    assert s1 == s2 and spans1 != spans2, (s1, s2)
    assert spans2 == {"c-10_0_0_1_8080-x1-bytes": "6.8 KB / 6 B"}, spans2
    s3, _ = _fleet_snapshot(view(node("10.0.0.1:8080", 160, [dict(m, state="stopped")])))
    assert s3 != s1
    # a peer whose two mappings have one id: no patching by id; the counters are the shape
    dup = view(node("10.0.0.2:8080", 1, [m, dict(m)]))
    s4, spans4 = _fleet_snapshot(dup)
    dup["nodes"][0]["mappings"][1]["bytes_in"] = 99
    assert spans4 is None and _fleet_snapshot(dup)[0] != s4
    # what the template renders for a counter span is what a patch would put in
    nodes = [dict(node("10.0.0.1:8080", 1, [dict(m, bytes_in=7000)]), self=True, name="n")]
    out = make_templates().env.get_template("_cluster_body.html").render(can_edit=True, nodes=nodes)
    assert '<span id="c-10_0_0_1_8080-x1-bytes">6.8 KB / 6 B</span>' in out
    print("fleet view: uptime and counters are not part of the shape (unless ids collide)  OK")


def main():
    test_answers()
    test_arender_off_loop()
    test_mappings_shape()
    test_fleet_shape()
    print("\nPASS: dashboard tables answered without re-rendering, off the event loop")


if __name__ == "__main__":
    main()
