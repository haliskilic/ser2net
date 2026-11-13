"""The markup the long-text layout fix relies on (issue #26) — no browser needed.

tests/test_ui_layout.py measures the real layout in Chromium (opt-in); this
portable test guards the template side of the contract on every platform:

  * the `softbreak` filter escapes first and only then adds <wbr> after
    / \\ _ . : @ -  (its output is marked safe, so this is the XSS guard);
  * the mappings / cluster / ports tables sit in a .table-scroll wrapper and are
    .stackable; every row button is inside td.col-actions > div.row-actions;
    every data cell (not the name, not the buttons) has a data-label for the
    phone card layout; each row spans exactly as many columns as the header;
  * a mapping name / device path full of HTML is rendered escaped;
  * the static assets carry ?v=<a hash of the static files> (fresh CSS/JS as soon as
    one changes), and only the dashboard (where the console opens) loads xterm.js;
  * htmx runs with eval, fragment <script>s and its injected <style> off;
  * every password field says what it is (autocomplete), and the fields of a third-party
    secret (MQTT, LDAP, OIDC, SMTP, webhook, cluster key) are not taken for a sign-in
    form: new-password, the password managers' opt-outs, their user name fields off;
  * the mapping form does not offer a Modbus gateway the options it doesn't use.

Run: python3 tests/test_ui_markup.py
"""
import glob
import html
import html.parser
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from markupsafe import Markup

from ser2netweb import __version__
from ser2netweb.config import MappingConfig
from ser2netweb.web.server import asset_version, make_templates, softbreak

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}
EVIL = '<img src=x onerror="alert(1)">_x'
BY_ID = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def classes(self):
        return set((self.attrs.get("class") or "").split())

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent


def parse(markup):
    """Tiny DOM: enough to check nesting (html.parser is stdlib)."""
    root = Node("#root", {}, None)

    class P(html.parser.HTMLParser):
        cur = root

        def handle_starttag(self, tag, attrs):
            node = Node(tag, attrs, self.cur)
            self.cur.children.append(node)
            if tag not in VOID:
                self.cur = node

        def handle_endtag(self, tag):
            n = self.cur
            while n is not root and n.tag != tag:
                n = n.parent
            if n is not root:
                self.cur = n.parent

    P().feed(markup)
    return root


def check_table_contract(markup, where):
    root = parse(markup)
    tables = [n for n in root.walk() if n.tag == "table"]
    assert tables, f"{where}: no table rendered"
    for t in tables:
        assert "stackable" in t.classes(), f"{where}: table is not .stackable"
        assert any("table-scroll" in a.classes() for a in t.ancestors()), \
            f"{where}: table is not inside a .table-scroll wrapper"
        ncols = sum(1 for n in t.walk() if n.tag == "th")
        for tr in (n for n in t.walk() if n.tag == "tr"):
            cells = [c for c in tr.children if c.tag in ("td", "th")]
            span = sum(int(c.attrs.get("colspan") or 1) for c in cells)
            assert span == ncols, f"{where}: a row spans {span} columns, the header has {ncols}"
            if len(cells) > 1 and cells[0].tag == "td":
                for c in cells:
                    if not (c.classes() & {"col-name", "col-actions"}):
                        assert c.attrs.get("data-label"), \
                            f"{where}: data cell without data-label (phone cards need it)"
    for b in (n for n in root.walk() if n.tag == "button"):
        anc = list(b.ancestors())
        assert any("row-actions" in a.classes() for a in anc), f"{where}: button outside .row-actions"
        assert any(a.tag == "td" and "col-actions" in a.classes() for a in anc), \
            f"{where}: button outside td.col-actions"
    return root


def test_softbreak():
    out = softbreak(BY_ID)
    assert isinstance(out, Markup)
    assert str(out).replace("<wbr>", "") == BY_ID, "softbreak must not change the text"
    assert "by-<wbr>id/<wbr>usb-<wbr>FTDI_<wbr>FT232R_" in out
    evil = str(softbreak(EVIL))
    assert "<img" not in evil and "&lt;img" in evil and "&#34;alert(1)&#34;" in evil, evil
    assert "&<wbr>" not in evil and ";<wbr>" not in str(softbreak("a&b;c")), "entity split"
    assert str(softbreak(r"C:\COM3")) == r"C:<wbr>\<wbr>COM3"
    assert str(softbreak("plain")) == "plain"
    print("softbreak: escapes first, then <wbr> after separators only  OK")


def mappings():
    long_name = "PRODUCTION_LINE_3_PLC_SIEMENS_S7_1500_MAIN_CONTROLLER_BUILDING_B"
    return [
        MappingConfig.from_dict({"id": "m1", "name": long_name, "enabled": True,
                                 "serial": {"port": BY_ID},
                                 "network": {"mode": "server", "port": 4001, "max_connections": 4}}),
        MappingConfig.from_dict({"id": "m2", "name": EVIL, "enabled": False,
                                 "serial": {"port": EVIL},
                                 "network": {"mode": "client", "remote_host": "host.example",
                                             "remote_port": 4001}}),
        MappingConfig.from_dict({"id": "m3", "name": "bridge", "kind": "serialbridge",
                                 "serial": {"port": BY_ID}, "serial_b": {"port": BY_ID + "-b"}}),
    ]


def test_mappings_table():
    tpl = make_templates().env.get_template("_mappings_body.html")
    statuses = {"m1": {"state": "reconnecting", "last_error": f"could not open port {BY_ID}",
                       "client_count": 2, "dropped_clients": 1, "bytes_in": 10, "bytes_out": 20,
                       "bytes_dropped": 7,
                       "clients": [{"peer": "127.0.0.1:50000", "duration": "3m", "priority": True}]}}
    for can_edit in (True, False):
        out = tpl.render(mappings=mappings(), statuses=statuses, can_edit=can_edit)
        root = check_table_contract(out, f"mappings table (can_edit={can_edit})")
        assert "<img" not in out and "&lt;img" in out, "mapping name/path must be escaped"
        assert "by-<wbr>id/" in out, "device paths should get soft line breaks"
        n_btn = sum(1 for n in root.walk() if n.tag == "button")
        assert n_btn == (7 + 6 + 7 if can_edit else 2 * 3), f"unexpected button count {n_btn}"
        err = next(n for n in root.walk() if "err-small" in n.classes())
        assert err.attrs.get("title", "").endswith(BY_ID), "full error belongs in the tooltip"
        # the counters say what they count; dropped data is flagged in the same cell
        th = [n for n in root.walk() if n.tag == "th"][5]
        assert th.attrs.get("title", "").startswith("In: bytes read from the serial device"), th.attrs
        cell = next(n for n in root.walk() if n.attrs.get("data-label") == "In / Out")
        warn = [n for n in cell.walk() if "err-small" in n.classes()]
        assert len(warn) == 1 and "not written to the serial device" in warn[0].attrs["title"], warn
        assert out.count("7 bytes dropped") == 1, "only the mapping that dropped data is flagged"
    empty = tpl.render(mappings=[], statuses={}, can_edit=True)
    check_table_contract(empty, "empty mappings table")
    print("mappings table: wrapper, card labels, buttons in their cell, escaping  OK")


def test_durations_translated():
    """Connection times and a node's uptime are written with the page's units (the REST
    API's duration stays as the engine writes it); a client without connected_at (an
    older peer's payload) shows the duration it came with."""
    import time

    from ser2netweb import i18n
    from ser2netweb.web.routes.cluster import _fmt_uptime
    tpl = make_templates().env.get_template("_mappings_body.html")
    clients = [{"peer": "10.0.0.5:5000", "duration": "1h 02m", "connected_at": time.time() - 3725},
               {"peer": "10.0.0.6:5000", "duration": "7m 01s"}]
    statuses = {"m1": {"state": "running", "client_count": 2, "clients": clients}}
    tok = i18n.set_language("tr")
    try:
        out = tpl.render(mappings=mappings(), statuses=statuses, can_edit=False)
        uptime = (_fmt_uptime(90000), _fmt_uptime(3725), _fmt_uptime(59))
    finally:
        i18n.reset(tok)
    assert '<span class="dur">1 sa 02 dk</span>' in out and "1h 02m" not in out, out[out.find("peer mono"):][:300]
    assert '<span class="dur">7m 01s</span>' in out, "without connected_at: the text it came with"
    assert uptime == ("1 gün 1 sa", "1 sa 2 dk", "0 dk"), uptime
    assert _fmt_uptime(90000) == "1d 1h", "English outside a Turkish request"
    js = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.js"), encoding="utf-8").read()
    assert 'tr("dur_hm", "{0}h {1}m"' in js and '"h "' not in js, "app.js counts on with the same units"
    print("durations: connection times and uptime in the page's units, older payloads as sent  OK")


def test_cluster_table():
    tpl = make_templates().env.get_template("_cluster_body.html")
    node_maps = [{"id": "x1", "name": EVIL, "enabled": True, "kind": "server/raw",
                  "endpoint": "127.0.0.1:4001", "serial": BY_ID, "state": "running",
                  "client_count": 1, "bytes_in": 1, "bytes_out": 2},
                 {"id": "x2", "name": "off", "enabled": False, "kind": "client/raw",
                  "endpoint": "→ h:1", "serial": "COM3", "state": "stopped",
                  "client_count": 0, "bytes_in": 0, "bytes_out": 0}]
    nodes = [
        {"name": "this-host", "self": True, "source": "self", "ip": "10.0.0.1", "port": 8080,
         "addr": "10.0.0.1:8080", "scheme": "http", "online": True, "uptime": "1h",
         "version": __version__, "running_count": 1, "mapping_count": 2, "mappings": node_maps},
        {"name": "peer-host", "self": False, "source": "manual", "ip": "fd00::2", "port": 8443,
         "addr": "[fd00::2]:8443", "url": "https://[fd00::2]:8443/", "reported": EVIL,
         "scheme": "https", "online": True, "uptime": "2h", "version": __version__,
         "running_count": 1, "mapping_count": 2, "mappings": node_maps,
         "caps": frozenset({"control", "edit"})},
        {"name": "gone", "self": False, "source": "auto", "ip": "10.0.0.3", "port": 8080,
         "addr": "10.0.0.3:8080", "url": "http://10.0.0.3:8080/", "scheme": "http",
         "online": False, "mappings": []},
    ]
    out = tpl.render(nodes=nodes, can_edit=True, peers_hidden=False, discovery_error="")
    root = check_table_contract(out, "cluster table")
    assert "<img" not in out and "&lt;img" in out, "peer names and addresses must be escaped"
    buttons = [n for n in root.walk() if n.tag == "button"]
    assert len(buttons) == 3 + 2, "only the peer's rows get buttons (Stop+Restart+Edit / Start+Edit)"
    # the actions name the peer by its host + port only (the server finds its scheme)
    vals = [json.loads(b.attrs["hx-vals"]) for b in buttons if "hx-vals" in b.attrs]
    assert vals and all(v["host"] == "fd00::2" and v["port"] == 8443 and "scheme" not in v
                        for v in vals), vals
    edit = next(b.attrs["hx-get"] for b in buttons if "hx-get" in b.attrs)
    assert edit.startswith("/api/cluster/peer-form?host=fd00%3A%3A2&port=8443&mid="), edit
    opens = [n.attrs["href"] for n in root.walk() if n.tag == "a"]
    assert opens == ["https://[fd00::2]:8443/"], "Open only for an online peer, at its URL"
    check_table_contract(tpl.render(nodes=[], can_edit=True), "empty cluster table")
    print("cluster table: wrapper, card labels, buttons in their cell, escaping  OK")

    # what a peer can do decides its buttons: 2.5.x (no version) is only shown, 2.6.0
    # has no remote edit; another version is tagged, and a mixed cluster gets a banner
    from ser2netweb.web.routes.cluster import peer_caps
    for payload, caps, labels in (
            ({"version": __version__, "caps": ["control", "edit"]}, {"control", "edit"},
             ["Stop", "Restart", "Edit", "Start", "Edit"]),
            ({}, set(), []), ({"version": "2.5.0"}, set(), []),
            ({"version": "2.6.0"}, {"control"}, ["Stop", "Restart", "Start"]),
            ({"version": "2.6.1"}, {"control", "edit"}, ["Stop", "Restart", "Edit", "Start", "Edit"]),
            ({"version": "3.0.0", "caps": ["control"]}, {"control"}, ["Stop", "Restart", "Start"])):
        assert peer_caps(payload) == caps, (payload, peer_caps(payload))
        peer = dict(nodes[1], caps=peer_caps(payload), version=payload.get("version", ""),
                    age="older" if payload.get("version", "x") < __version__ else "")
        out = tpl.render(nodes=[nodes[0], peer], can_edit=True,
                         notes={"versions": ["2.6.0", __version__]})
        got = re.findall(r'<button class="btn sm"[^>]*>([^<]*)</button>', out)
        assert got == labels, (payload, got)
        assert ("view only" in out) == (not caps) and ("no remote edit" in out) == (
            caps == {"control"}), (payload, out[:2000])
        assert ("older version" in out) == (peer["age"] == "older"), payload
        assert "run different versions (2.6.0, " in out
    print("cluster table: actions follow what the peer can do (caps, else its version); "
          "other versions tagged, a mixed cluster pointed out  OK")


def test_counters_and_empty_state():
    """In / Out in B / KB / MB / GB in both tables (the exact counts stay in the REST API
    and /metrics); a dashboard without mappings tells viewers nothing about an "Add
    mapping" button they don't have."""
    from ser2netweb.web.routes.common import bytesize
    assert [bytesize(n) for n in (0, 1023, 1024, 1536, 10 * 1048576 + 1, 5 << 30, 3 << 40)] == \
        ["0 B", "1023 B", "1.0 KB", "1.5 KB", "10.0 MB", "5.0 GB", "3.0 TB"]
    assert bytesize("junk") == "junk" and bytesize(None) == "None", "a peer's odd value as is"
    env = make_templates().env
    rows = env.get_template("_mappings_body.html").render(
        mappings=mappings(), can_edit=True,
        statuses={"m1": {"state": "running", "bytes_in": 2048, "bytes_out": 5 * 1048576}})
    assert '<span id="m-m1-bytes">2.0 KB / 5.0 MB</span>' in rows, rows
    nodes = [{"name": "n", "self": True, "source": "self", "ip": "10.0.0.1", "port": 8080,
              "addr": "10.0.0.1:8080", "online": True,
              "mappings": [{"id": "x1", "name": "a", "enabled": True, "bytes_in": 512,
                            "bytes_out": 3 << 30, "state": "running"}]}]
    cluster = env.get_template("_cluster_body.html").render(can_edit=True, nodes=nodes)
    assert '<span id="c-10_0_0_1_8080-x1-bytes">512 B / 3.0 GB</span>' in cluster, cluster
    tpl = env.get_template("_mappings_body.html")
    assert "Click “Add mapping”" in tpl.render(mappings=[], statuses={}, can_edit=True)
    viewer = tpl.render(mappings=[], statuses={}, can_edit=False)
    assert "No mappings yet." in viewer and "Add mapping" not in viewer, viewer
    print("counters in B/KB/MB/GB (both tables); viewers' empty table without the Add hint  OK")


def test_ports_table_and_assets():
    env = make_templates().env
    ports = [{"device": BY_ID, "description": "FT232R USB UART", "vid": "0403", "pid": "6001",
              "serial_number": "A50285BI", "manufacturer": "FTDI"}]
    out = env.get_template("_ports_table.html").render(ports=ports, in_use={BY_ID})
    check_table_contract(out, "ports table")

    class Req:
        class state:
            csrf_token = "t"
            user = None
    page = env.get_template("base.html").render(request=Req)
    version = asset_version()
    assert len(version) == 12 and version != __version__, version
    for asset in ("app.css", "app.js", "points.js", "theme.js", "htmx.min.js"):
        assert f"/static/{asset}?v={version}" in page, f"{asset} lacks the ?v= cache-buster"
    assert "xterm" not in page, "xterm.js loads on the dashboard only"
    tpl = env.get_template("dashboard.html")
    head = "".join(tpl.blocks["head"](tpl.new_context({"request": Req})))
    for asset in ("xterm.js", "xterm.css", "xterm-addon-fit.js"):
        assert f"/static/{asset}?v={version}" in head, f"the dashboard lacks {asset}"
    with open(os.path.join(ROOT, "ser2netweb", "web", "templates", "base.html"), encoding="utf-8") as fh:
        base = fh.read()
    assert base.index("{% block head %}") < base.index('/static/app.css'), \
        "app.css comes after a page's own CSS"
    print("ports table contract + versioned static asset URLs, xterm on the dashboard only  OK")


def test_htmx_config():
    """htmx runs without the features the UI doesn't use: eval (hx-on, js: values, trigger
    filters), <script> elements from fragments, the indicator <style> it would inject.
    The meta is read when htmx starts, so it must come before the htmx script."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    nodes = list(parse(make_templates().env.get_template("base.html").render(request=Req)).walk())
    meta = [i for i, n in enumerate(nodes) if n.tag == "meta"
            and n.attrs.get("name") == "htmx-config"]
    script = [i for i, n in enumerate(nodes) if n.tag == "script"
              and "/static/htmx.min.js" in (n.attrs.get("src") or "")]
    assert len(meta) == 1 and len(script) == 1 and meta[0] < script[0], (meta, script)
    config = json.loads(nodes[meta[0]].attrs["content"])
    assert config == {"allowEval": False, "allowScriptTags": False,
                      "includeIndicatorStyles": False}, config
    print("htmx: no eval, no <script> from fragments, no injected <style>  OK")


# third-party secrets and the user name / id beside each: never this UI's sign-in
SECRET_FIELDS = {"mqtt_password", "ldap_bind_password", "oidc_client_secret", "key",
                 "notify_smtp_password", "notify_webhook_url", "tls_key_password",
                 "network_tls_key_password"}
SECRET_IDS = {"mqtt_username", "ldap_bind_dn", "oidc_client_id", "notify_smtp_username"}
PM_OPT_OUT = ("data-1p-ignore", "data-lpignore", "data-bwignore")


def test_secret_fields():
    """A browser or password manager fills the saved ser2netweb sign-in into an empty password
    field that looks like the half of a login form; saved there, the admin password would
    end up in config.json as the MQTT, LDAP, OIDC or SMTP password, the cluster key or a TLS
    key password (an unencrypted key never needs one, so nothing would refuse it)."""
    inputs = []
    for f in sorted(glob.glob(os.path.join(ROOT, "ser2netweb", "web", "templates", "*.html"))):
        src = open(f, encoding="utf-8").read()
        inputs += [(os.path.basename(f), n) for n in parse(src).walk() if n.tag == "input"]
    for where, n in inputs:
        if n.attrs.get("type") == "password":
            assert n.attrs.get("autocomplete") in ("current-password", "new-password"), \
                (where, n.attrs.get("name"), n.attrs.get("autocomplete"))
    names = {n.attrs.get("name"): n for _w, n in inputs if n.attrs.get("type") != "hidden"}
    for name in SECRET_FIELDS:
        n = names[name]
        assert all(a in n.attrs for a in PM_OPT_OUT), (name, n.attrs)
        if name == "notify_webhook_url":   # never shown back; a URL, not a password
            assert n.attrs["type"] == "url" and n.attrs.get("autocomplete") == "off", n.attrs
            assert n.attrs.get("spellcheck") == "false", n.attrs
            css = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.css"), encoding="utf-8").read()
            assert "input[type=url]" in css, "a URL field is styled like the text fields"
        else:
            assert n.attrs["type"] == "password", (name, n.attrs)
            assert n.attrs.get("autocomplete") == "new-password", (name, n.attrs)
    for name in SECRET_IDS:
        assert names[name].attrs.get("autocomplete") == "off", (name, names[name].attrs)
    add_user = [n for w, n in inputs if w == "settings.html" and n.attrs.get("name") == "username"
                and n.attrs.get("type") == "text"]
    assert len(add_user) == 1 and add_user[0].attrs.get("autocomplete") == "off", add_user
    print("password fields declare autocomplete; third-party secrets: new-password + password "
          "manager opt-outs, their user names off; the webhook URL is a URL field  OK")


def test_mapping_form_modbus_fields():
    """The Modbus gateway uses no connect banner, close-on string or client buffer, so the
    form shows those only for raw / telnet / rfc2217 (app.js hides and disables a
    data-show-when block); Read-only, kick and priority IPs apply to masters and stay."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    m = MappingConfig.from_dict({"id": "mb1", "name": "gw", "serial": {"port": "COM3"},
                                 "network": {"protocol": "modbus", "port": 502},
                                 "options": {"modbus_response_timeout_s": 0.8}})
    root = parse(tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                            ports=[], ips=[], error=None))

    def conditions(name):
        field = next(n for n in root.walk() if n.attrs.get("name") == name)
        return [a.attrs["data-show-when"] for a in field.ancestors() if "data-show-when" in a.attrs]

    for name in ("opt_banner", "opt_closeon", "network_client_queue_max"):
        assert "network_protocol=raw,telnet,rfc2217" in conditions(name), (name, conditions(name))
    for name in ("network_read_only", "network_kick_old_user", "network_priority_client_ips",
                 "network_allowed_client_ips", "opt_idle_timeout_s"):
        assert not any(c.startswith("network_protocol=") for c in conditions(name)), name
    # plain-format /set only (Sparkplug commands are never retained); the hidden marker
    # sits with the checkbox, so it is posted exactly when the checkbox is offered
    for name in ("mqtt_apply_retained_set", "mqtt_apply_retained_set_shown",
                 "mqtt_unit_status", "mqtt_unit_status_shown", "mqtt_value_format"):
        assert conditions(name) == ["mqtt_payload_format=plain", "network_protocol=modbus"], \
            (name, conditions(name))
    # retained data: plain format, serial lines too (not only Modbus); marker beside it
    for name in ("mqtt_retain", "mqtt_retain_shown"):
        assert conditions(name) == ["mqtt_payload_format=plain", "kind=net"], conditions(name)
    # the Sparkplug primary host: Modbus gateways in Sparkplug B format; its own marker
    assert conditions("mqtt_sparkplug_primary_host_id") == [
        "mqtt_payload_format=sparkplug_b", "network_protocol=modbus", "kind=net"], \
        conditions("mqtt_sparkplug_primary_host_id")
    # the response timeout: Modbus gateways only; the field is its own presence marker
    assert conditions("opt_modbus_response_timeout_s") == ["network_protocol=modbus"]
    # the points' options marker is posted exactly when the points JSON is
    assert conditions("modbus_point_options_shown") == conditions("modbus_points") == [
        "network_protocol=modbus"], conditions("modbus_point_options_shown")
    field = next(n for n in root.walk() if n.attrs.get("name") == "opt_modbus_response_timeout_s")
    assert field.attrs.get("value") == "0.8" and field.attrs.get("type") == "number", field.attrs
    print("mapping form: banner/close-on/client buffer not offered for modbus; the retained "
          "/set and unit status options only for plain-format points; the response timeout "
          "for modbus  OK")


def test_mapping_form_requests_disable_themselves():
    """The mapping form disables its Save button while it is saved (hx-disabled-elt="find
    button[type=submit]"), which htmx hands down to every request inside the form: one
    that sets no hx-disabled-elt of its own (the Modbus test read) would look for a submit
    button inside itself and log an error in the browser at every click."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    m = MappingConfig.from_dict({"id": "gw1", "name": "gw", "serial": {"port": "COM3"},
                                 "network": {"protocol": "modbus", "port": 502},
                                 "modbus_poll": {"points": [{"name": "t", "fn": 3}]}})
    root = parse(tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                            ports=[], ips=[], error=None))
    form = next(n for n in root.walk() if n.tag == "form")
    inner = [n for n in root.walk()
             if n is not form and ("hx-post" in n.attrs or "hx-get" in n.attrs)]
    assert inner, "the test read button is in the form"
    bad = [n.attrs.get("hx-post") or n.attrs.get("hx-get") for n in inner
           if "hx-disabled-elt" not in n.attrs]
    assert not bad, bad
    print("mapping form: a request inside the form disables what it names, not the form's "
          "Save  OK")


def test_mapping_form_mqtt_tls_fields():
    """The MQTT TLS files sit with the other broker fields (every net mapping that
    publishes: serial lines, plain Modbus, Sparkplug B), under the MQTT fieldset only; the
    CA file field is their presence marker, so it must be posted whenever the others are;
    the key password is a password field; the host name check is ticked by default."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    for stored, checked in (({}, True), ({"tls_check_hostname": False}, False)):
        m = MappingConfig.from_dict({"id": "m1", "name": "m", "serial": {"port": "COM3"},
                                     "mqtt": dict({"enabled": True, "host": "b",
                                                   "base_topic": "t", "tls": True,
                                                   "client_key_password": "pw"}, **stored)})
        out = tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                         ports=[], ips=[], error=None)
        root = parse(out)
        fields = {n.attrs.get("name"): n for n in root.walk() if n.attrs.get("name")}
        # write-only: the stored key password is never in the page, only that there is one
        assert fields["mqtt_client_key_password"].attrs.get("value", "") == "" \
            and 'value="pw"' not in out, fields["mqtt_client_key_password"].attrs
        assert "unchanged" in fields["mqtt_client_key_password"].attrs.get("placeholder", "")
        assert "mqtt_client_key_password_clear" in fields
        for name in ("mqtt_ca_file", "mqtt_client_cert", "mqtt_client_key",
                     "mqtt_client_key_password", "mqtt_tls_check_hostname"):
            conditions = [a.attrs["data-show-when"] for a in fields[name].ancestors()
                          if "data-show-when" in a.attrs]
            assert conditions == ["kind=net"], (name, conditions)
        assert fields["mqtt_client_key_password"].attrs.get("type") == "password"
        assert ("checked" in fields["mqtt_tls_check_hostname"].attrs) == checked, stored
    print("mapping form: MQTT CA / client certificate / key / password / host name check "
          "with the broker fields  OK")


def test_mapping_form_hidden_characters():
    """A banner / close-on string with a NUL, CR or LF can't be shown in its text box (a
    browser shows U+FFFD and drops the line breaks): the form says so next to it."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")

    def hint(options):
        m = MappingConfig.from_dict({"id": "tb1", "name": "plc", "serial": {"port": "COM3"},
                                     "options": options})
        page = tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                          ports=[], ips=[], error=None)
        return page.count("control characters (NUL, CR or LF)")
    assert hint({"closeon": "ctrl-a\x01nil\x00"}) == 1
    assert hint({"closeon": "\r\n", "banner": "x\ny"}) == 2
    assert hint({"closeon": "NO CARRIER", "banner": "Port \\N\\r\\n"}) == 0
    print("mapping form: a banner / close-on string the box can't show is pointed out  OK")


def test_mapping_form_udp_fields():
    """The UDP reply address and peer timeout are offered for UDP only, in one block whose
    timeout field is the presence marker (forms.py _OPTIONAL_FORM_FIELDS), next to a note
    that the allow-list checks a forgeable source address."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    m = MappingConfig.from_dict({"id": "u1", "name": "udp", "serial": {"port": "COM3"},
                                 "network": {"mode": "udp", "port": 4001,
                                             "udp_peer_host": "10.0.0.9", "udp_peer_port": 5000,
                                             "udp_peer_timeout_s": 30}})
    out = tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                     ports=[], ips=[], error=None)
    root = parse(out)
    block = None
    for name, value in (("network_udp_peer_host", "10.0.0.9"), ("network_udp_peer_port", "5000"),
                        ("network_udp_peer_timeout_s", "30")):
        field = next(n for n in root.walk() if n.attrs.get("name") == name)
        assert field.attrs.get("value") == value, (name, field.attrs)
        owner = next(a for a in field.ancestors() if "data-show-when" in a.attrs)
        assert owner.attrs["data-show-when"] == "network_mode=udp", (name, owner.attrs)
        assert block in (None, owner), "one block, shown and posted together"
        block = owner
    assert any(n.tag == "p" for n in block.walk()), "the note belongs to the UDP block"
    assert "source address, which can be forged" in out
    print("mapping form: UDP reply address / peer timeout in one UDP-only block  OK")


def test_number_fields_required():
    """A number field whose blank value the server refuses is `required`, so the browser
    says what is missing before the form is sent (hidden blocks are disabled by app.js,
    so they don't hold the form up); the ones a blank value is fine for are not."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    m = MappingConfig.from_dict({"id": "n1", "name": "n", "serial": {"port": "COM3"}})
    root = parse(tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                            ports=[], ips=[], error=None))
    # (a blank poll interval: no polling, as 0)
    # (a blank Modbus response timeout: the default 1 s; a blank block gap: 0)
    blank_ok = {"network_udp_peer_port", "network_udp_peer_timeout_s", "modbus_poll_interval_s",
                "opt_poll_interval_s", "opt_modbus_response_timeout_s", "modbus_block_gap",
                "serial_rs485_delay_before_tx_ms", "serial_rs485_delay_after_tx_ms",
                "serial_b_rs485_delay_before_tx_ms", "serial_b_rs485_delay_after_tx_ms"}
    numbers = [n for n in root.walk() if n.tag == "input" and n.attrs.get("type") == "number"]
    bad = [n.attrs["name"] for n in numbers if ("required" in n.attrs) == (n.attrs["name"] in blank_ok)]
    assert numbers and not bad, bad
    assert {"serial_custom_baud", "network_port", "opt_trace_keep"} <= {n.attrs["name"] for n in numbers}
    print(f"mapping form: {len(numbers) - len(blank_ok)} number fields required, the optional ones not  OK")


def test_mapping_form_rfc2217_restore():
    """'Restore the serial settings when the RFC 2217 client leaves' is offered for RFC 2217
    mappings only, its hidden presence marker in the same block (posted exactly when the
    checkbox is offered), ticked when the option is on."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    for on in (False, True):
        m = MappingConfig.from_dict({"id": "vc1", "name": "vc", "serial": {"port": "COM3"},
                                     "network": {"protocol": "rfc2217", "port": 4001},
                                     "options": {"rfc2217_restore_on_disconnect": on}})
        root = parse(tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                                ports=[], ips=[], error=None))
        owners = []
        for name in ("opt_rfc2217_restore_on_disconnect",
                     "opt_rfc2217_restore_on_disconnect_shown"):
            field = next(n for n in root.walk() if n.attrs.get("name") == name)
            owners.append(next(a for a in field.ancestors() if "data-show-when" in a.attrs))
            if name == "opt_rfc2217_restore_on_disconnect":
                assert ("checked" in field.attrs) is on, field.attrs
        assert owners[0] is owners[1], "the marker and the checkbox share one block"
        assert owners[0].attrs["data-show-when"] == "network_protocol=rfc2217", owners[0].attrs
    print("mapping form: the RFC 2217 restore option, for RFC 2217 mappings only  OK")


def test_mapping_form_port_lock():
    """The port-lock options are offered for each serial side, each with its hidden presence
    marker in the same block (so an older peer's form, which has neither, keeps the stored
    values), ticked when on; the flock box says what it locks."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    for on in (False, True):
        adv = {"tiocexcl": on, "respect_uucp_locks": on}
        m = MappingConfig.from_dict({"id": "sb1", "name": "sb", "kind": "serialbridge",
                                     "serial": {"port": "COM3", "advanced": adv},
                                     "serial_b": {"port": "COM4", "advanced": adv}})
        out = tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                         ports=[], ips=[], error=None)
        root = parse(out)
        for prefix in ("serial_", "serial_b_"):
            fields = {n.attrs["name"]: n for n in root.walk()
                      if n.attrs.get("name", "").startswith(prefix)}
            block = [next(a for a in fields[prefix + name].ancestors() if a.tag == "details")
                     for name in ("port_lock_shown", "tiocexcl", "respect_uucp_locks")]
            assert block[0] is block[1] is block[2], prefix
            for name in ("tiocexcl", "respect_uucp_locks"):
                assert ("checked" in fields[prefix + name].attrs) is on, (prefix, name)
            assert ("open" in block[0].attrs) is on
        assert "Lock the port (flock)" in out and "Exclusive open" not in out
    print("mapping form: the port-lock options for each serial side, with their markers  OK")


def test_mqtt_command_tags():
    """The dashboard tags a Modbus mapping whose writable points take MQTT commands CMD,
    as a warning (CMD ⚠) when its broker connection has no TLS or no username; a read-only
    one is RO and never CMD. The mapping form shows the same warning as a banner."""
    class Req:
        class state:
            csrf_token = "t"
            user = None

    def gw(mid, mqtt=None, writable=True, **network):
        return MappingConfig.from_dict({
            "id": mid, "name": mid, "serial": {"port": "COM3"},
            "network": dict({"protocol": "modbus", "port": 502}, **network),
            "mqtt": dict({"enabled": True, "host": "b", "base_topic": "plant/a"}, **(mqtt or {})),
            "modbus_poll": {"points": [{"name": "pump", "fn": 1, "dtype": "bool",
                                        "writable": writable}]}})
    env = make_templates().env
    rows, form = env.get_template("_mappings_body.html"), env.get_template("_mapping_form.html")

    def row(m):
        out = rows.render(mappings=[m], statuses={}, can_edit=False)
        check_table_contract(out, f"mappings table ({m.id})")
        return out

    def form_of(m):
        return form.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                           ports=[], ips=[], error=None)

    for m in (gw("open"), gw("tls_only", {"tls": True}), gw("user_only", {"username": "u"})):
        out = row(m)
        assert ">CMD ⚠</span>" in out and "tag tag-danger" in out, (m.id, out)
        assert 'class="banner warn"' in form_of(m), m.id
    secure = gw("secure", {"tls": True, "username": "u"})
    out = row(secure)
    assert ">CMD</span>" in out and "tag-danger" not in out, out
    assert 'class="banner warn"' not in form_of(secure)
    read_only = gw("ro", read_only=True)
    assert ">RO</span>" in row(read_only) and ">CMD" not in row(read_only)
    assert 'class="banner warn"' not in form_of(read_only)
    for m in (gw("mqtt_off", {"enabled": False}), gw("no_writable", writable=False)):
        assert ">CMD" not in row(m) and 'class="banner warn"' not in form_of(m), m.id
    print("CMD tag (⚠ without TLS / username) and the form warning follow MQTT writes; "
          "read-only is RO only  OK")


def test_mqtt_tx_tags():
    """A serial mapping that writes MQTT messages on <base_topic>/tx to the device is tagged
    CMD too (CMD ⚠ and a form banner without TLS or a username); read-only or tx off: not."""
    class Req:
        class state:
            csrf_token = "t"
            user = None

    def serial(mid, mqtt=None, **network):
        return MappingConfig.from_dict({
            "id": mid, "name": mid, "serial": {"port": "COM3"},
            "network": dict({"protocol": "raw", "port": 4001}, **network),
            "mqtt": dict({"enabled": True, "host": "b", "base_topic": "lab/scale",
                          "tx_enabled": True}, **(mqtt or {}))})
    env = make_templates().env
    rows, form = env.get_template("_mappings_body.html"), env.get_template("_mapping_form.html")

    def row(m):
        out = rows.render(mappings=[m], statuses={}, can_edit=False)
        check_table_contract(out, f"mappings table ({m.id})")
        return out

    def form_of(m):
        return form.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                           ports=[], ips=[], error=None)

    out = row(serial("open"))
    assert ">CMD ⚠</span>" in out and "tag tag-danger" in out, out
    assert "&lt;topic&gt;/tx are written to the device" in out
    assert 'class="banner warn"' in form_of(serial("open"))
    secure = serial("secure", {"tls": True, "username": "u"})
    assert ">CMD</span>" in row(secure) and "tag-danger" not in row(secure)
    assert 'class="banner warn"' not in form_of(secure)
    for m in (serial("ro", read_only=True), serial("off", {"tx_enabled": False})):
        assert ">CMD" not in row(m) and 'class="banner warn"' not in form_of(m), m.id
    print("CMD tag and the form warning for MQTT writes to a serial mapping (<topic>/tx)  OK")


def test_units_not_answering():
    """A running Modbus gateway whose polled units stopped answering says which, under its
    state; not for units that answer or are not polled yet, nor while the mapping is not
    running (the port being down is said there already)."""
    tpl = make_templates().env.get_template("_mappings_body.html")
    m = MappingConfig.from_dict({"id": "gw", "name": "gw", "serial": {"port": "COM3"},
                                 "network": {"protocol": "modbus", "port": 502}})

    def units(*states):
        return [{"unit": n, "state": st, "cause": "no reply" if st == "offline" else "",
                 "since": None, "errors": 0} for n, st in states]
    for state, unit_states, shown in (
            ("running", [(1, "online"), (2, "offline"), (7, "offline"), (9, "unknown")], "2, 7"),
            ("running", [(1, "online"), (4, "offline")], "4"),
            ("running", [(1, "online"), (2, "unknown")], None),
            ("running", [], None),
            ("reconnecting", [(1, "offline")], None)):
        out = tpl.render(mappings=[m], statuses={"gw": {"state": state, "units": units(*unit_states)}},
                         can_edit=False)
        check_table_contract(out, f"mappings table ({state}, {unit_states})")
        root = parse(out)
        flagged = [n for n in root.walk() if "data-units-offline" in n.attrs]
        if shown is None:
            assert not flagged, (state, unit_states, out)
            continue
        word = "units" if "," in shown else "unit"   # (ngettext: one unit, or several)
        assert len(flagged) == 1 and f"Modbus {word} not answering: {shown}" in out, out
        assert "in 3 polls in a row" in flagged[0].attrs["title"], flagged[0].attrs
    out = tpl.render(mappings=[m], statuses={"gw": {"state": "running"}}, can_edit=False)
    assert "data-units-offline" not in out, "a status without units (an older peer's) works"
    print("dashboard: a running gateway lists the units that stopped answering  OK")


def test_mqtt_not_connected():
    """A mapping whose MQTT publisher has no connection to its broker, or could not start,
    says so under its state, with the reason; not while connected or connecting, nor for
    a status without mqtt (MQTT off, an older peer's)."""
    tpl = make_templates().env.get_template("_mappings_body.html")
    m = MappingConfig.from_dict({"id": "m1", "name": "m1", "serial": {"port": "COM3"},
                                 "mqtt": {"enabled": True, "host": "b", "base_topic": "t"}})
    for mq, shown in (
            ({"state": "down", "broker": "b:1883", "error": "b:1883 refused the connection: "
              "Not authorized"}, "MQTT: no connection to b:1883 (b:1883 refused the "
                                 "connection: Not authorized)"),
            ({"state": "off", "broker": "b:1883", "error": "paho-mqtt is not installed"},
             "MQTT off: paho-mqtt is not installed"),
            ({"state": "connected", "broker": "b:1883", "error": ""}, None),
            ({"state": "connecting", "broker": "b:1883", "error": ""}, None),
            ({}, None), (None, None)):
        st = {"state": "device-missing", "last_error": "no such device"}
        if mq is not None:
            st["mqtt"] = mq
        out = tpl.render(mappings=[m], statuses={"m1": st}, can_edit=False)
        check_table_contract(out, f"mappings table (mqtt {mq})")
        flagged = [n for n in parse(out).walk() if "data-mqtt-down" in n.attrs]
        if shown is None:
            assert not flagged, (mq, out)
            continue
        assert len(flagged) == 1 and html.escape(shown, quote=False) in out, out
        assert "the mapping’s log" in flagged[0].attrs["title"], flagged[0].attrs
    print("dashboard: an MQTT publisher without a broker connection says why  OK")


def test_mapping_form_kick_hint():
    """A server mapping with "Kick old client" on and no allow-list says, in its form, that any
    host reaching the port can take the connection over; with an allow-list, or without
    kicking, it says nothing."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")
    for kick, allowed, shown in ((True, [], True), (True, ["10.0.0.0/24"], False),
                                 (False, [], False)):
        m = MappingConfig.from_dict({"id": "kh1", "name": "kh", "serial": {"port": "COM3"},
                                     "network": {"port": 4001, "kick_old_user": kick,
                                                 "allowed_client_ips": allowed}})
        root = parse(tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                                ports=[], ips=[], error=None))
        hint = next((n for n in root.walk() if "data-kick-hint" in n.attrs), None)
        assert (hint is not None) is shown, (kick, allowed)
        if hint is not None:
            owner = next(a for a in hint.ancestors() if "data-show-when" in a.attrs)
            assert owner.attrs["data-show-when"] == "network_mode=server", owner.attrs
    print("mapping form: kick_old_user without an allow-list is pointed out (server mode)  OK")


class Page:
    """The `request` a full page (_shell.html) renders with."""
    class state:
        csrf_token = "t"
        user = None
        lang = "en"

    class url:
        path = "/"
        query = ""

    scope = {"type": "http", "path": "/", "root_path": "", "headers": []}   # (ser2netweb/web/proxy.py)


def dashboard(can_edit=True, cluster_on=False):
    return make_templates().env.get_template("dashboard.html").render(
        request=Page, can_edit=can_edit, cluster_on=cluster_on)


def test_feedback_area():
    """Every page of the shell has the two live regions app.js reports into (status for
    what an action did, alert for what failed), outside <main> so they float over it, and
    the page carries the texts app.js needs for them."""
    root = parse(dashboard())
    region = {n.attrs.get("id"): n for n in root.walk() if n.attrs.get("id") in ("ui-alert", "ui-status")}
    assert region["ui-alert"].attrs.get("role") == "alert", region["ui-alert"].attrs
    assert region["ui-status"].attrs.get("role") == "status", region["ui-status"].attrs
    for n in region.values():
        anc = list(n.ancestors())
        assert any("feedback" in a.classes() for a in anc) and not any(a.tag == "main" for a in anc)
    body = next(n for n in root.walk() if n.tag == "body")
    texts = json.loads(body.attrs["data-i18n"])
    for key in ("dismiss", "stale", "failed", "server_error", "offline", "session_ended", "sign_in"):
        assert texts.get(key), key
    assert "{0}" in texts["failed"] and "{0}" in texts["server_error"], "the status goes in"
    print("feedback area: alert + status live regions on every shell page, texts for app.js  OK")


def test_select_all_shown():
    """"Select all shown" sits in the toolbar, which is always there - not in the table's
    head (hidden on narrow screens, replaced at every refresh) nor in the bulk bar (hidden
    while nothing is ticked) - and only for those who may act; the rows carry their client
    count for the bulk action's question; app.js acts on the rows shown only."""
    root = parse(dashboard())
    box = [n for n in root.walk() if "data-select-shown" in n.attrs]
    assert len(box) == 1 and box[0].attrs.get("type") == "checkbox", box
    anc = list(box[0].ancestors())
    assert any("data-mapping-tools" in a.attrs for a in anc), "in the toolbar"
    assert not any("data-bulk" in a.attrs or a.tag == "thead" for a in anc)
    assert not any("data-select-shown" in n.attrs for n in parse(dashboard(can_edit=False)).walk())
    rows = make_templates().env.get_template("_mappings_body.html").render(
        mappings=mappings(), can_edit=True, statuses={"m1": {"state": "running", "client_count": 3}})
    assert 'data-mid="m1"' in rows and 'data-clients="3"' in rows and 'data-clients="0"' in rows
    js = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.js"), encoding="utf-8").read()
    assert "ids: rows.map(" in js and "Array.from(selected)" not in js, "only the rows shown"
    assert "window.confirm(q)" in js, "a bulk action asks first"
    texts = json.loads(next(n for n in root.walk() if n.tag == "body").attrs["data-i18n"])
    for key in ("selected_hidden", "bulk_start", "bulk_stop", "bulk_restart", "bulk_clients",
                "bulk_hidden", "no_match", "show_all"):
        assert texts.get(key), key
    print("select all shown: in the toolbar for operators; rows carry their clients  OK")


def test_actions_hold_a_second_click():
    """A state-changing button is disabled while its request runs (hx-disabled-elt), so a
    second click is not queued and sent afterwards (a second copy, a second restart)."""
    tpl = make_templates().env.get_template("_mappings_body.html")
    rows = parse(tpl.render(mappings=mappings(), statuses={}, can_edit=True))
    cluster = make_templates().env.get_template("_cluster_body.html").render(
        can_edit=True, nodes=[{"name": "peer", "self": False, "source": "auto", "ip": "10.0.0.2",
                               "port": 8080, "addr": "10.0.0.2:8080", "online": True,
                               "caps": ["control", "edit"],
                               "mappings": [{"id": "x1", "name": "a", "enabled": True},
                                            {"id": "x2", "name": "b", "enabled": False}]}])
    acting = [n for root in (rows, parse(cluster)) for n in root.walk()
              if n.tag == "button" and ("hx-post" in n.attrs or "hx-delete" in n.attrs)]
    assert len(acting) == 4 + 3 + 4 + 3, len(acting)   # Stop Restart Copy Delete / Start Copy Delete
    for b in acting:
        assert b.attrs.get("hx-disabled-elt") == "this", b.attrs
    root = parse(dashboard())
    bulk = [n for n in root.walk() if "data-bulk-action" in n.attrs]
    assert len(bulk) == 3 and all(b.attrs.get("hx-disabled-elt") == "this" for b in bulk)
    form = parse(make_templates().env.get_template("_mapping_form.html").render(
        request=Page, mapping=MappingConfig(name=""), is_new=True, modbus_points_json="",
        ports=[], ips=[], error=None))
    f = next(n for n in form.walk() if n.tag == "form")
    assert f.attrs.get("hx-disabled-elt") == "find button[type=submit]", f.attrs
    print("row, peer, bulk and form actions: disabled while their request runs  OK")


def test_polled_controls_keep_their_ids():
    """htmx 2 puts the keyboard focus back after a swap only on an element with an id: every
    control of the polled tables (mappings, fleet view, ports) and of the self-refreshing
    log panel has one, the same in every refresh, unique in the page (the fleet view's are
    prefixed: this node's mappings are listed there too). Each row button is described by
    the mapping's name, so "Stop" says which mapping it stops."""
    env = make_templates().env
    tpl = env.get_template("_mappings_body.html")
    out = tpl.render(mappings=mappings(), statuses={}, can_edit=True)
    assert out == tpl.render(mappings=mappings(), statuses={}, can_edit=True)
    nodes = [{"name": "peer", "self": False, "source": "auto", "ip": "fd00::2", "port": 8443,
              "caps": ["control", "edit"],
              "addr": "[fd00::2]:8443", "online": True,
              "mappings": [{"id": "m1", "name": "same id as here", "enabled": True},
                           {"id": "x 2", "name": "odd id", "enabled": False}]}]
    cluster = env.get_template("_cluster_body.html").render(can_edit=True, nodes=nodes)
    ports = env.get_template("_ports_table.html").render(ports=[], hidden=2, show_all=False)
    log = env.get_template("_mapping_log.html").render(mid="m1", mapping_name="a",
                                                      lines=["x"], content_hash="0123")
    ids = []
    for markup in (out, cluster, ports, log):
        root = parse(markup)
        named = {n.attrs["id"] for n in root.walk() if n.attrs.get("id", "").endswith("-name")}
        for n in root.walk():
            if n.tag in ("button", "input") and n.attrs.get("type") != "hidden":
                assert n.attrs.get("id"), f"no id: {n.tag} {n.attrs}"
                assert re.fullmatch(r"[A-Za-z0-9_-]+", n.attrs["id"]), n.attrs["id"]
                ids.append(n.attrs["id"])
                if n.attrs["id"].startswith(("m-", "c-")) and n.tag == "button":
                    assert n.attrs.get("aria-describedby") in named, n.attrs
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)
    assert "m-m1-run" in ids and "m-m1-edit" in ids and "c-_fd00__2__8443-m1-run" in ids, ids
    print("polled tables and the log panel: stable, unique ids; row buttons name their mapping  OK")


def test_panel_openers_scroll_it_into_view():
    """The form panel sits above the table: every button that opens it (Add, a row's Edit /
    Log / Monitor / Trace, a peer's Edit) and the form itself (an error re-render) scroll
    it into view (show:top); the log's and the traces' own refreshes don't (no jumping).
    Each panel has a heading app.js can focus, and the form's error banner is an alert."""
    env = make_templates().env
    rows = parse(env.get_template("_mappings_body.html").render(
        mappings=[MappingConfig.from_dict({"id": "t1", "name": "t", "serial": {"port": "COM3"},
                                           "options": {"trace_both": "t.log"}})],
        statuses={}, can_edit=True))
    cluster = parse(env.get_template("_cluster_body.html").render(
        can_edit=True, nodes=[{"name": "p", "self": False, "ip": "10.0.0.2", "port": 8080,
                               "addr": "10.0.0.2:8080", "online": True,
                               "caps": ["control", "edit"],
                               "mappings": [{"id": "x1", "name": "a", "enabled": True}]}]))
    openers = [n for root in (rows, cluster, parse(dashboard())) for n in root.walk()
               if n.attrs.get("hx-target") == "#form-panel"]
    assert len(openers) == 4 + 1 + 1, [o.attrs for o in openers]   # Edit Log Monitor Trace / Edit / Add
    for o in openers:
        assert o.attrs.get("hx-swap") == "innerHTML show:top", o.attrs
    form = parse(env.get_template("_mapping_form.html").render(
        request=Page, mapping=MappingConfig(name="x"), is_new=False, modbus_points_json="",
        ports=[], ips=[], error="Port 4001 is in use"))
    f = next(n for n in form.walk() if n.tag == "form")
    assert f.attrs["hx-swap"] == "innerHTML show:top", f.attrs
    banner = next(n for n in form.walk() if "error" in n.classes())
    assert banner.attrs.get("role") == "alert" and banner.attrs.get("tabindex") == "-1"
    log = parse(env.get_template("_mapping_log.html").render(mid="t1", mapping_name="t",
                                                            lines=[], content_hash="0"))
    traces = parse(env.get_template("_mapping_traces.html").render(mid="t1", mapping_name="t",
                                                                  files=[]))
    console = parse(env.get_template("_console.html").render(mid="t1", mapping_name="t",
                                                            interactive=True))
    gone = parse(env.get_template("_mapping_log.html").render(gone=True))
    for root in (form, log, traces, console, gone):
        title = [n for n in root.walk() if n.attrs.get("id") == "panel-title"]
        assert len(title) == 1 and title[0].tag == "h3" and title[0].attrs.get("tabindex") == "-1"
    for root in (log, traces):
        for n in root.walk():
            if "hx-get" in n.attrs:
                assert n.attrs.get("hx-swap") == "innerHTML", n.attrs
    print("panel openers and the form scroll the panel into view, its own refreshes don't; "
          "each panel has a focusable heading  OK")


def test_keyboard_and_screen_readers():
    """A checkbox, radio button or file picker shows where the focus is (the border colour
    the other fields get does nothing there); the console can be left with the keyboard
    (xterm keeps Tab and Shift+Tab: app.js lets Shift+Tab go, Tab too in a read-only
    console) and an operator's console says how; the dashboard's counts are live regions;
    the audit log's filters have names; error banners are alerts."""
    env = make_templates().env
    css = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.css"), encoding="utf-8").read()
    rule = re.search(r"input\[type=checkbox\]:focus-visible[^{]*\{([^}]*)\}", css)
    assert rule and "outline: 2px solid" in rule.group(1), "checkboxes need a focus outline"
    for kind in ("radio", "file"):
        assert f"input[type={kind}]:focus-visible" in css, kind
    js = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.js"), encoding="utf-8").read()
    assert "attachCustomKeyEventHandler" in js and 'ev.key === "Tab"' in js and "ev.shiftKey" in js
    assert 'promptLabel = tr("terminal_input"' in js, "the console's input has a translated name"
    console = env.get_template("_console.html")
    assert "Shift+Tab leaves the console" in console.render(mid="a", mapping_name="a", interactive=True)
    assert "Shift+Tab" not in console.render(mid="a", mapping_name="a", interactive=False), \
        "a read-only console: Tab leaves it as anywhere else"
    root = parse(dashboard())
    count = next(n for n in root.walk() if "data-filter-count" in n.attrs)
    assert count.attrs.get("role") == "status", count.attrs
    assert next(n for n in root.walk() if "data-bulk-count" in n.attrs).attrs.get("aria-live") == "polite"
    audit = parse(env.get_template("audit.html").render(
        request=Page, filters={"q": "", "action": "", "user": ""}, actions=["login"],
        users=["admin"], qs="", total=0, pages=1, page=1, rows=[]))
    form = next(n for n in audit.walk() if n.tag == "form" and n.attrs.get("action") == "/settings/audit")
    for n in form.walk():
        if n.tag in ("input", "select"):
            assert n.attrs.get("aria-label"), n.attrs
    for name in ("login.html", "setup.html"):
        src = open(os.path.join(ROOT, "ser2netweb", "web", "templates", name), encoding="utf-8").read()
        assert 'class="banner error">' not in src and 'class="banner error" role="alert"' in src, name
    print("focus outlines on checkboxes, a way out of the console, live counts, named audit "
          "filters, error banners as alerts  OK")


def test_console_says_why():
    """The console panel: a Modbus gateway's opens in the hex view (data-mode); the typing
    hint is for the text view only (app.js hides [data-text-only] in the hex view); a
    console that ends says why — a refused handshake (the browser only sees 1006), each
    close code routes/console.py sends — and connects again after a restart; its notices
    (dropped bytes, mapping not running) and those texts are translated (base.html)."""
    from ser2netweb.web.routes import console as routes_console

    tpl = make_templates().env.get_template("_console.html")
    term = next(n for n in parse(tpl.render(mid="a", mapping_name="a", hex_first=True)).walk()
                if n.attrs.get("id") == "term")
    assert term.attrs.get("data-mode") == "hex", term.attrs
    term = next(n for n in parse(tpl.render(mid="a", mapping_name="a", interactive=True)).walk()
                if n.attrs.get("id") == "term")
    assert term.attrs.get("data-mode") == "text", term.attrs
    js = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.js"), encoding="utf-8").read()
    assert '"[data-text-only]"' in js and 'n.hidden = mode === "hex"' in js
    assert 'path + "?v=2"' in js, "the page asks for protocol 2"
    for name in ("CLOSE_BUSY", "CLOSE_STOPPED", "CLOSE_ACCESS", "CLOSE_SLOW", "CLOSE_GONE"):
        assert f"case {getattr(routes_console, name)}:" in js, f"app.js does not explain {name}"
    assert "ev.code === %d" % routes_console.CLOSE_STOPPED in js, "connects again after a restart"
    base = open(os.path.join(ROOT, "ser2netweb", "web", "templates", "base.html"), encoding="utf-8").read()
    for key in ("dropped", "ws_not_running", "ws_reconnected", "ws_reconnecting", "ws_refused",
                "ws_busy", "ws_stopped", "ws_access", "ws_slow", "ws_gone"):
        # (a text with a count: a pair of forms, picked by trn)
        assert (f"'{key}': _(" in base and f'tr("{key}"' in js
                or f"'{key}': [_(" in base and f'trn("{key}"' in js), key
    # the console text encoding: the form offers exactly config.CONSOLE_ENCODINGS, for every
    # kind and protocol (no data-show-when), and the panel carries the code page's table
    from ser2netweb.config import CONSOLE_ENCODINGS, console_charset

    class Req:
        class state:
            csrf_token = "t"
            user = None
    m = MappingConfig.from_dict({"id": "e1", "name": "e", "serial": {"port": "COM3"},
                                 "options": {"console_encoding": "cp857"}})
    form = parse(make_templates().env.get_template("_mapping_form.html").render(
        request=Req, mapping=m, is_new=False, modbus_points_json="", ports=[], ips=[], error=None))
    select = next(n for n in form.walk() if n.attrs.get("name") == "opt_console_encoding")
    options = [n for n in select.walk() if n.tag == "option"]
    assert tuple(n.attrs["value"] for n in options) == CONSOLE_ENCODINGS
    assert [n.attrs["value"] for n in options if "selected" in n.attrs] == ["cp857"]
    assert not any("data-show-when" in a.attrs for a in select.ancestors())
    page = tpl.render(mid="a", mapping_name="a", encoding="cp857", charset=console_charset("cp857"))
    term = next(n for n in parse(page).walk() if n.attrs.get("id") == "term")
    assert term.attrs["data-charset"] == console_charset("cp857") and "Text encoding: CP857." in page
    print("console: a Modbus gateway's opens in hex, the typing hint only in the text view, "
          "every close says why (translated), connects again after a restart; the form offers "
          "the text encodings, the panel carries the code page  OK")


def test_unsaved_form_is_guarded():
    """A form shown again after a failed save holds values that are not saved: it comes
    marked dirty (data-dirty), so app.js asks before Cancel, another panel or leaving the
    page throws them away; a fresh form is not (it only gets dirty by being edited)."""
    tpl = make_templates().env.get_template("_mapping_form.html")
    m = MappingConfig(name="x")

    def form(error):
        root = parse(tpl.render(request=Page, mapping=m, is_new=False, modbus_points_json="",
                                ports=[], ips=[], error=error))
        return next(n for n in root.walk() if n.tag == "form")
    assert form("Port 4001 is in use").attrs.get("data-dirty") == "1"
    assert "data-dirty" not in form(None).attrs
    texts = json.loads(next(n for n in parse(dashboard()).walk() if n.tag == "body").attrs["data-i18n"])
    assert texts.get("discard_changes") and texts.get("sign_in_tab")
    js = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.js"), encoding="utf-8").read()
    assert '"beforeunload"' in js and 'tr("discard_changes"' in js
    print("mapping form: re-shown after a failed save it counts as unsaved; the guard's texts  OK")


def test_app_js_inserts_no_server_html():
    """app.js puts server texts into the page with textContent: the only innerHTML it
    writes is the empty string (closing a panel), and nothing else parses HTML."""
    src = open(os.path.join(ROOT, "ser2netweb", "web", "static", "app.js"), encoding="utf-8").read()
    writes = re.findall(r"\.innerHTML\s*=(?!=)\s*([^;]*);", src)
    assert writes and all(w.strip() == '""' for w in writes), writes
    # (.outerHTML: the property; htmx.ajax's swap: "outerHTML" is htmx swapping a server
    # fragment in, as an hx-swap attribute does)
    for sink in ("insertAdjacentHTML", ".outerHTML", "document.write", "DOMParser", "eval("):
        assert sink not in src, sink
    assert "textContent = text" in src, "feedback messages go in as text"
    print("app.js: server texts as textContent, no HTML sinks  OK")
def test_mapping_form_protocols_and_ignored_settings():
    """UDP offers raw only (app.js hides and disables an <option> whose data-show-when
    doesn't hold, and picks the first one left); the MQTT box says what a Modbus gateway
    publishes; a setting the mapping has but doesn't use is pointed out at the top."""
    import re

    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")

    def render(d):
        m = MappingConfig.from_dict({"id": "pr1", "name": "pr", "serial": {"port": "COM3"}, **d})
        return tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                          ports=[], ips=[], error=None)

    out = render({"network": {"mode": "server", "protocol": "telnet"}})
    select = next(n for n in parse(out).walk() if n.attrs.get("name") == "network_protocol")
    options = {o.attrs["value"]: o.attrs.get("data-show-when")
               for o in select.walk() if o.tag == "option"}
    assert options == {"raw": None, "telnet": "network_mode=server,client",
                       "rfc2217": "network_mode=server,client",
                       "modbus": "network_mode=server,client"}, options
    box = out[out.index('name="mqtt_enabled"'):]
    box = box[:box.index("</label>")]
    assert ('<span data-show-when="network_protocol=raw,telnet,rfc2217">Publish serial lines '
            'to MQTT</span><span data-show-when="network_protocol=modbus">Publish the Modbus '
            'poll points to MQTT (add them below)</span>') in box, box

    def banners(html_text):
        return re.findall(r'<div class="banner warn">(.*?)</div>', html_text)

    assert banners(out) == [], banners(out)
    mqtt = {"enabled": True, "host": "b", "base_topic": "t"}
    assert banners(render({"kind": "serialbridge", "serial_b": {"port": "COM4"},
                           "mqtt": mqtt})) == [
        "MQTT is on, but a serial-to-serial bridge does not publish to MQTT: its MQTT "
        "settings are not used."]
    assert "nothing is published" in banners(render(
        {"network": {"protocol": "modbus", "port": 502}, "mqtt": mqtt}))[0]
    assert "no connections to close" in banners(render(
        {"network": {"mode": "udp"}, "options": {"closeon": "BYE"}}))[0]
    print("mapping form: raw only over UDP, the MQTT box per protocol, unused settings "
          "pointed out  OK")


def test_mapping_form_unit_warnings():
    """Poll points on unit 0 (broadcast: never read, written to every slave) or on the
    reserved units 248-255 are kept, and the form says so in a warning naming them
    (escaped); points on 1-247 get none."""
    class Req:
        class state:
            csrf_token = "t"
            user = None
    tpl = make_templates().env.get_template("_mapping_form.html")

    def warnings(units):
        m = MappingConfig.from_dict({
            "id": "uw1", "name": "gw", "serial": {"port": "COM3"},
            "network": {"protocol": "modbus", "port": 502},
            "modbus_poll": {"points": [{"name": f"p<{u}>", "unit": u, "fn": 3, "address": 0,
                                        "dtype": "uint16", "writable": u == 0}
                                       for u in units]}})
        out = tpl.render(request=Req, mapping=m, is_new=False, modbus_points_json="",
                         ports=[], ips=[], error=None)
        for n in (n for n in parse(out).walk() if "data-unit-warning" in n.attrs):
            owner = next(a for a in n.ancestors() if "data-show-when" in a.attrs)
            assert owner.attrs["data-show-when"] == "network_protocol=modbus", owner.attrs
        return [html.unescape(t) for t in
                re.findall(r"<div class=\"banner warn\" data-unit-warning>(.*?)</div>", out)]

    assert warnings([1, 17, 247]) == []
    both = warnings([1, 0, 248, 255])
    assert len(both) == 2, both
    assert "unit 0, the broadcast address: p<0>." in both[0], both[0]
    assert "units 248–255" in both[1] and "p<248>, p<255>." in both[1], both[1]
    out = tpl.render(request=Req, mapping=MappingConfig.from_dict({
        "id": "uw2", "name": "gw", "serial": {"port": "COM3"},
        "network": {"protocol": "modbus", "port": 502},
        "modbus_poll": {"points": [{"name": "<b>x</b>", "unit": 0, "fn": 3, "address": 0,
                                    "dtype": "uint16"}]}}),
        is_new=False, modbus_points_json="", ports=[], ips=[], error=None)
    assert "&lt;b&gt;x&lt;/b&gt;" in out and "<b>x</b>" not in out, "point names are escaped"
    print("mapping form: points on unit 0 / 248-255 named in a warning (escaped); none for "
          "1-247  OK")


def main():
    test_softbreak()
    test_mappings_table()
    test_durations_translated()
    test_cluster_table()
    test_counters_and_empty_state()
    test_ports_table_and_assets()
    test_htmx_config()
    test_secret_fields()
    test_mapping_form_modbus_fields()
    test_mapping_form_hidden_characters()
    test_mapping_form_mqtt_tls_fields()
    test_mapping_form_requests_disable_themselves()
    test_mapping_form_udp_fields()
    test_number_fields_required()
    test_mapping_form_rfc2217_restore()
    test_mapping_form_port_lock()
    test_mapping_form_kick_hint()
    test_mapping_form_protocols_and_ignored_settings()
    test_mqtt_command_tags()
    test_mqtt_tx_tags()
    test_feedback_area()
    test_select_all_shown()
    test_actions_hold_a_second_click()
    test_polled_controls_keep_their_ids()
    test_panel_openers_scroll_it_into_view()
    test_keyboard_and_screen_readers()
    test_console_says_why()
    test_unsaved_form_is_guarded()
    test_app_js_inserts_no_server_html()
    test_mapping_form_unit_warnings()
    test_units_not_answering()
    test_mqtt_not_connected()
    print("\nPASS: UI markup contract")


if __name__ == "__main__":
    main()
