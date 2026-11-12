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
  * the mapping form does not offer a Modbus gateway the options it doesn't use.

Run: python3 tests/test_ui_markup.py
"""
import html.parser
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from markupsafe import Markup

from app import __version__
from app.config import MappingConfig
from app.web.server import asset_version, make_templates, softbreak

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
         "running_count": 1, "mapping_count": 2, "mappings": node_maps},
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
    for asset in ("app.css", "app.js", "htmx.min.js"):
        assert f"/static/{asset}?v={version}" in page, f"{asset} lacks the ?v= cache-buster"
    assert "xterm" not in page, "xterm.js loads on the dashboard only"
    tpl = env.get_template("dashboard.html")
    head = "".join(tpl.blocks["head"](tpl.new_context({"request": Req})))
    for asset in ("xterm.js", "xterm.css", "xterm-addon-fit.js"):
        assert f"/static/{asset}?v={version}" in head, f"the dashboard lacks {asset}"
    with open(os.path.join(ROOT, "app", "web", "templates", "base.html"), encoding="utf-8") as fh:
        base = fh.read()
    assert base.index("{% block head %}") < base.index('href="/static/app.css'), \
        "app.css comes after a page's own CSS"
    print("ports table contract + versioned static asset URLs, xterm on the dashboard only  OK")


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
                                 "network": {"protocol": "modbus", "port": 502}})
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
    for name in ("mqtt_apply_retained_set", "mqtt_apply_retained_set_shown"):
        assert conditions(name) == ["mqtt_payload_format=plain", "network_protocol=modbus"], \
            (name, conditions(name))
    print("mapping form: banner/close-on/client buffer not offered for modbus; the retained "
          "/set option only for plain-format points  OK")


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


def main():
    test_softbreak()
    test_mappings_table()
    test_cluster_table()
    test_ports_table_and_assets()
    test_mapping_form_modbus_fields()
    test_mapping_form_udp_fields()
    test_mapping_form_rfc2217_restore()
    test_mapping_form_kick_hint()
    test_mqtt_command_tags()
    print("\nPASS: UI markup contract")


if __name__ == "__main__":
    main()
