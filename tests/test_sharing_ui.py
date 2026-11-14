"""What the web UI shows of packing, the writer list, open on connect, port pools and the
connect-out target list.

  - the dashboard row: "waiting for a client" while an open-on-connect mapping runs with
    its port closed (not once a client is there, not while it is stopped; next to the
    missing device and why, when the last open failed), the pool a
    mapping is handed clients from (not for one a pool passes over: TLS), a client that
    only listens, the number of other connect-out targets and the target the link is on;
    without the settings a row is what it was;
  - the mapping form: each new field sits in a block shown for the mode and protocol it
    applies to, carries the stored value, and is left out for a cluster peer that would
    not read it (forms.FORM_BLOCKS_SINCE);
  - a mapping that sets one where it does nothing says so in the form (ignored_settings).
Pure rendering: no network.

Run: python3 tests/test_sharing_ui.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_ui_markup import parse

from ser2netweb.config import MappingConfig
from ser2netweb.engine.bridge import RunnerStatus
from ser2netweb.web.routes import forms
from ser2netweb.web.server import make_templates

ENV = make_templates().env
REQUEST = SimpleNamespace(state=SimpleNamespace(csrf_token="t", user=None, lang="en"))


def mapping(mid, **network):
    return MappingConfig.from_dict({"id": mid, "name": mid, "serial": {"port": "/dev/ttyS0"},
                                    "network": {"port": 4001, **network}})


def row(m, **status):
    st = RunnerStatus(state="running")
    for key, value in status.items():
        setattr(st, key, value)
    out = ENV.get_template("_mappings_body.html").render(
        mappings=[m], statuses={m.id: st.as_dict()}, can_edit=True)
    return parse(out), out


def marked(root, attr):
    return [n for n in root.walk() if attr in n.attrs]


def test_dashboard():
    plain = mapping("plain")
    root, out = row(plain)
    for attr in ("data-waiting", "data-pool", "data-link-target"):
        assert not marked(root, attr), attr
    assert "listens only" not in out

    od = mapping("od", open_on_connect=True)
    root, out = row(od, waiting=True)
    assert len(marked(root, "data-waiting")) == 1 and "waiting for a client" in out
    assert not marked(row(od, waiting=False)[0], "data-waiting"), "a client is connected"
    assert not marked(row(od, waiting=True, state="stopped")[0], "data-waiting")
    # its device could not be opened at the last try: that state and why, and waiting
    root, out = row(od, waiting=True, state="device-missing",
                    last_error="[Errno 2] could not open port /dev/ttyS0")
    assert len(marked(root, "data-waiting")) == 1 and "state-device-missing" in out, out
    assert "could not open port" in out, out

    root, out = row(mapping("member", pool="lab"))
    tags = marked(root, "data-pool")
    assert len(tags) == 1 and "lab" in out
    assert not marked(row(mapping("tls", pool="lab", tls=True, tls_cert="c", tls_key="k"))[0],
                      "data-pool"), "a pool never hands a client to a TLS mapping"
    assert not marked(row(mapping("udp", pool="lab", mode="udp"))[0], "data-pool")

    clients = [{"peer": "10.0.0.7:5000", "connected_at": 1.0, "priority": False,
                "listen_only": False},
               {"peer": "10.0.0.8:5001", "connected_at": 2.0, "priority": False,
                "listen_only": True}]
    root, out = row(mapping("w", writer_client_ips=["10.0.0.7"]), clients=clients,
                    client_count=2)
    peers = [n for n in root.walk() if "peer" in n.attrs.get("class", "").split()]
    assert len(peers) == 2
    assert "what it sends is not written to the device" not in peers[0].attrs["title"]
    assert "what it sends is not written to the device" in peers[1].attrs["title"]
    assert out.count("listens only") == 1

    out_m = mapping("out", mode="client", remote_host="a.example", remote_port=4001,
                    remote_targets=["b.example:4001", "c.example:4001"])
    root, out = row(out_m, link="up", link_target="b.example:4001")
    assert "+2" in out and len(marked(root, "data-link-target")) == 1
    assert "b.example:4001" in out
    one = mapping("one", mode="client", remote_host="a.example", remote_port=4001)
    assert not marked(row(one, link="up", link_target="a.example:4001")[0], "data-link-target")
    print("dashboard: waiting for a client, the pool, a client that only listens, the "
          "connect-out target in use; nothing new without the settings  OK")


def form(m, hide=frozenset()):
    out = ENV.get_template("_mapping_form.html").render(request=REQUEST, **forms.form_context(
        m, [], [], is_new=False, hide=hide))
    return parse(out), out


def shown_when(node):
    """The show-when rules of the blocks a field is in, innermost first."""
    rules = []
    while node is not None:
        if "data-show-when" in node.attrs:
            rules.append(node.attrs["data-show-when"])
        node = node.parent
    return rules


def test_form():
    m = MappingConfig.from_dict({
        "id": "f1", "name": "f", "serial": {"port": "/dev/ttyS0"},
        "network": {"port": 4001, "writer_client_ips": ["10.0.0.7", "10.1.0.0/16"],
                    "open_on_connect": True, "port_linger_s": 7.5, "pool": "lab",
                    "remote_targets": ["b.example:1", "[fd00::2]:2"], "failover_return_s": 60},
        "options": {"pack_gap_ms": 25, "pack_delimiter": r"\r\n", "pack_max_bytes": 1400}})
    root, _out = form(m)
    fields = {n.attrs["name"]: n for n in root.walk()
              if n.tag in ("input", "select", "textarea") and "name" in n.attrs}
    want = {
        "opt_pack_gap_ms": ("25", ["network_protocol=raw"]),
        "opt_pack_delimiter": (r"\r\n", ["network_protocol=raw"]),
        "opt_pack_max_bytes": ("1400", ["network_protocol=raw"]),
        "network_writer_client_ips": ("10.0.0.7 10.1.0.0/16", [
            "network_protocol=raw,telnet,rfc2217", "network_mode=server,udp"]),
        "network_port_linger_s": ("7.5", ["network_protocol=raw,telnet,rfc2217",
                                          "network_mode=server"]),
        "network_pool": ("lab", ["network_protocol=raw,telnet,rfc2217", "network_mode=server"]),
        "network_remote_targets": ("b.example:1 [fd00::2]:2", ["network_mode=client"]),
        "network_failover_return_s": ("60", ["network_mode=client"]),
    }
    for name, (value, rules) in want.items():
        node = fields[name]
        assert node.attrs.get("value") == value, (name, node.attrs)
        assert [r for r in shown_when(node) if r != "kind=net"] == rules, (name, shown_when(node))
    box = fields["network_open_on_connect"]
    assert "checked" in box.attrs and "network_open_on_connect_shown" in fields
    assert [r for r in shown_when(box) if r != "kind=net"] == \
        ["network_protocol=raw,telnet,rfc2217", "network_mode=server"]
    fresh = next(n for n in form(MappingConfig(name="n"))[0].walk()
                 if n.attrs.get("name") == "network_open_on_connect")
    assert "checked" not in fresh.attrs, "off for a new mapping"

    # a peer that runs a version without them is not offered the fields
    blocks = {"packing": ["opt_pack_gap_ms", "opt_pack_delimiter", "opt_pack_max_bytes"],
              "writer_ips": ["network_writer_client_ips"],
              "open_on_connect": ["network_open_on_connect", "network_open_on_connect_shown",
                                  "network_port_linger_s"],
              "pool": ["network_pool"],
              "failover": ["network_remote_targets", "network_failover_return_s"]}
    assert forms.blocks_not_read((3, 0, 1)) >= set(blocks)
    assert not forms.blocks_not_read((3, 1, 0)) & set(blocks)
    for block, names in blocks.items():
        _root, out = form(m, hide=frozenset({block}))
        for name in names:
            assert f'name="{name}"' not in out, (block, name)
        for other, others in blocks.items():
            if other != block:
                assert all(f'name="{n}"' in out for n in others), (block, other)

    # a setting where it does nothing: said in the form
    odd = MappingConfig.from_dict({
        "id": "f2", "name": "f", "serial": {"port": "/dev/ttyS0"},
        "network": {"mode": "client", "protocol": "telnet", "remote_host": "h",
                    "remote_port": 1, "open_on_connect": True, "pool": "lab",
                    "writer_client_ips": ["10.0.0.7"]},
        "options": {"pack_gap_ms": 25}})
    notes = odd.ignored_settings()
    assert len(notes) == 4, notes
    _root, out = form(odd)
    for part in ("Packing is set", "Writer client IPs are set",
                 "only while a client is connected applies to", "it is not a member"):
        assert any(part in n for n in notes) and part in out, part
    print("mapping form: the new fields in the blocks they apply to, with the stored "
          "values; left out for an older peer; unused settings named  OK")


def main():
    test_dashboard()
    test_form()
    print("\nPASS: the UI of packing, writers, open on connect, pools and connect-out targets")


if __name__ == "__main__":
    main()
