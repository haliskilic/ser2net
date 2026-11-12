"""Modbus poll points: how they are typed, parsed from the mapping form and shown again.

  * a point's values typed loosely before 2.9 load as what was meant ("unit": "1",
    "scale": "0.1", "writable": "false"), validate() still takes every point it took and
    gives a ConfigError (never a TypeError) for the ones it refused, and an edit
    (validate_input) refuses the values it can't use, so the form, the REST API, an import
    and a cluster save answer 400, not 500;
  * a stored scale that is not a number is not multiplied in;
  * the points JSON of the form: where invalid JSON breaks (line, column), one object
    alone is a list of one, anything else that is not a list is refused (never "no
    points"), a point that is not an object is named, a misspelt key ("adress") is refused
    and any other unknown key left alone;
  * a refused form comes back with the points text exactly as typed and the posted id
    (the next Save edits the same mapping), also when the points can't be read at all;
    the cluster proxy re-renders the same way and checks the points before it forwards
    them;
  * non-ASCII point names are shown as they are in the points JSON ("Sıcaklık", not
    \\u escapes) and so is all text in the mappings export, which stays a JSON file the
    importer reads back; text UTF-8 can't carry (an unpaired surrogate escape such as
    \\ud800) is refused by an edit and in the points JSON, and never makes the form or the
    export fail;
  * end to end: the form, the REST API and an import answer such points with 400 and a
    message, and the stored mapping keeps its points; the edit form and the export show
    Turkish names unescaped.
Cross-platform (the server part binds 127.0.0.1 only).

Run: python3 tests/test_modbus_points.py
"""
import json
import types
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app.config import (ConfigError, MappingConfig, ModbusPoint, ModbusPoll, point_scale,
                        unstorable_text)
from app.importer import parse_mappings_file
from app.web.auth import hash_password, hash_token
from app.web.routes import cluster as cluster_routes
from app.web.routes.forms import (_build_mapping_dict, _form_error, _modbus_points_json,
                                  _parse_modbus_points, _preserve_unmanaged_fields,
                                  _reshown_mapping, json_text)

PW = "modbus-points-pass-1"
TOKEN = "s2n_modbus-points-token"


def gateway(points, **extra):
    return MappingConfig.from_dict({
        "name": "PLC", "serial": {"port": "/dev/ser2net-test-plc"},
        "network": {"protocol": "modbus", "bind_ip": "127.0.0.1", "port": 5020},
        "mqtt": {"enabled": True, "host": "broker.lan", "base_topic": "plant/plc"},
        "modbus_poll": {"points": points}, **extra})


def refused(points, *words):
    m = gateway(points)
    try:
        m.validate()
        m.validate_input()
    except ConfigError as e:
        assert all(w in str(e) for w in words), (str(e), words)
        return str(e)
    raise AssertionError(f"accepted: {points}")


def test_loose_values_load():
    p = ModbusPoint.from_dict({"name": "a", "unit": "1", "fn": " 3 ", "address": 10.0,
                               "scale": "0.1", "writable": "false"})
    assert (p.unit, p.fn, p.address, p.scale, p.writable) == (1, 3, 10, 0.1, False), p
    assert ModbusPoint.from_dict({"scale": "1e3"}).scale == 1000.0
    assert ModbusPoint.from_dict({"scale": "2"}).scale == 2
    for raw, want in (("true", True), ("1", True), ("on", True), ("yes", True), (1, True),
                      ("False", False), ("0", False), ("off", False), ("", False), (0, False),
                      (None, False)):
        assert ModbusPoint.from_dict({"writable": raw}).writable is want, raw
    # the keys 2.9 added are typed from the start (validate_input)
    p = ModbusPoint.from_dict({"min": "5", "poll": "no", "dtype": "UINT16"})
    assert (p.min, p.poll, p.dtype) == ("5", "no", "UINT16"), p
    # what can't be read so stays as it is, for an edit to refuse
    p = ModbusPoint.from_dict({"unit": "one", "scale": "x", "writable": "maybe", "address": 1.5})
    assert (p.unit, p.scale, p.writable, p.address) == ("one", "x", "maybe", 1.5), p
    assert ModbusPoint.from_dict({"scale": "nan"}).scale == "nan"
    print("loosely typed point values load as what was meant; the rest stays as it is  OK")


def test_validate_keeps_what_it_took():
    """validate() is run on every save and boot: it takes every point it took before (a
    stored config has to keep loading), and refuses the others with a ConfigError."""
    took = [{"name": "a", "unit": 1.0, "address": 2.5}, {"name": "b", "unit": True},
            {"name": "c", "scale": "x"}, {"name": "d", "writable": "maybe", "fn": 3}]
    gateway(took).validate()
    for bad, words in (({"name": 5}, ("names are text", "5")),
                       ({"name": "a", "unit": "one"}, ("unit must be 0-255",)),
                       ({"name": "a", "address": [1]}, ("address must be 0-65535",)),
                       ({"name": "a", "fn": "x"}, ("function must be",))):
        try:
            gateway([bad]).validate()
            raise AssertionError(f"validate() took {bad}")
        except ConfigError as e:
            assert all(w in str(e) for w in words), (str(e), words)
    for item in ("temp", 5, ["name", "x", "y"]):
        try:
            ModbusPoll.from_dict({"points": [{"name": "ok"}, item]})
            raise AssertionError(f"took {item!r} as a point")
        except ConfigError as e:
            assert str(e) == "Modbus poll point #2 is not an object.", e
    print("validate() takes what it took before, and refuses the rest with a ConfigError  OK")


def test_edits_refuse_unusable_values():
    refused([{"name": "a", "unit": "one"}], "'a'", "unit must be 0-255")
    refused([{"name": "a", "unit": 1.5}], "'a'", "unit must be a whole number")
    refused([{"name": "a", "unit": True}], "'a'", "unit must be a whole number")
    refused([{"name": "a", "address": 2.5}], "'a'", "address must be a whole number")
    refused([{"name": "a", "scale": "x"}], "'a'", "scale must be a number")
    refused([{"name": "a", "scale": float("nan")}], "'a'", "scale must be a number")
    refused([{"name": "a", "fn": 3, "writable": "maybe"}], "'a'", "writable must be true or false")
    refused([{"name": 5}], "names are text")
    # what loads as what was meant is taken
    m = gateway([{"name": "a", "unit": "2", "scale": "0.5", "fn": 3, "writable": "false"}])
    m.validate()
    m.validate_input()
    assert not m.accepts_mqtt_writes, "writable 'false' made a write path"
    print("edits refuse point values that can't be used; 'false' is read-only  OK")


def test_stored_scale_ignored():
    assert point_scale(ModbusPoint(scale=0.1)) == 0.1
    assert point_scale(ModbusPoint(scale=0)) == 0
    for bad in ("x", float("nan"), float("inf"), [0.1], None, True):
        assert point_scale(ModbusPoint(scale=bad)) == 1.0, bad
    print("a stored scale that is not a number reads as 1  OK")


def test_points_json_parse():
    def err(raw):
        try:
            _parse_modbus_points(raw, strict=True)
        except ValueError as e:
            return str(e)
        raise AssertionError(f"took {raw!r}")
    msg = err('[\n  {"name": "a",}\n]')
    assert msg.startswith("Modbus poll points: the JSON is not valid at line 2, column 15:"), msg
    for raw in ("5", "null", '"temp"', "true"):
        assert err(raw) == "Modbus poll points must be a valid JSON array of objects.", raw
    assert err('[{"name": "a"}, "hum"]') == "Modbus poll point #2 is not an object."
    assert err('[[{"name": "a"}]]') == "Modbus poll point #1 is not an object."
    assert err('[{"name": "a", "adress": 5}]') == (
        "Modbus poll point #1 ('a'): unknown key \"adress\". Did you mean \"address\"?")
    assert "Did you mean \"writable\"?" in err('[{"name": "a", "writeable": true}]')
    assert "Did you mean \"dtype\"?" in err('[{"name": "a", "type": "int16"}]')
    assert _parse_modbus_points('{"name": "a"}', strict=True) == [{"name": "a"}]
    # unknown keys a point may carry (a note; a newer node's option) are left alone
    assert _parse_modbus_points('[{"name": "a", "comment": "x", "deadband": 2}]', strict=True)
    assert _parse_modbus_points("  ", strict=True) == []
    # not strict (a refused form shown again): what can't be read is left out
    assert _parse_modbus_points("[{", strict=False) == []
    assert _parse_modbus_points('[{"name": "a"}, "b", 5]', strict=False) == [{"name": "a"}]
    assert _parse_modbus_points("5", strict=False) == []
    print("points JSON: position of a JSON error, objects only, a misspelt key named  OK")


def form_of(points_text, **extra):
    return {"id": "abcdef012345", "name": "PLC-9", "kind": "net", "enabled": "on",
            "serial_port": "/dev/ser2net-test-plc", "network_protocol": "modbus",
            "network_port": "5021", "mqtt_enabled": "on", "mqtt_host": "broker.lan",
            "mqtt_base_topic": "plant/plc9", "mqtt_payload_format": "plain",
            "modbus_point_options_shown": "1", "modbus_points": points_text, **extra}


class FakeForm(dict):
    def multi_items(self):
        return list(self.items())


def test_refused_form_shown_again():
    stored = gateway([{"name": "kept", "fn": 3, "writable": True, "min": 0, "max": 9}],
                     id="abcdef012345")
    state = types.SimpleNamespace(
        config=types.SimpleNamespace(get_mapping=lambda mid: stored if mid == stored.id else None),
        ports=types.SimpleNamespace(get=lambda: []))

    def render(_request, template, status_code=200, **ctx):
        return dict(ctx, template=template, status_code=status_code)

    for text in ('[{"name": "a",}]', '["temp", "hum"]', '5', '[[{"name": "a"}]]',
                 '[{"name": "a", "unit": "one"}]'):
        out = _form_error(render, None, state, form_of(text), "the error")
        assert out["status_code"] == 400 and out["error"] == "the error"
        assert out["modbus_points_json"] == text, (text, out["modbus_points_json"])
        m = out["mapping"]
        assert m.id == "abcdef012345" and not out["is_new"], (text, m.id)
        assert m.name == "PLC-9" and m.network.port == 5021 and m.mqtt.base_topic == "plant/plc9", text
    # a new mapping stays new; a form that posted no points shows the mapping's
    out = _form_error(render, None, state, dict(form_of("x"), id=""), "e")
    assert out["is_new"] and out["modbus_points_json"] == "x"
    f = form_of("")
    del f["modbus_points"]
    assert _reshown_mapping(f, None).id == "abcdef012345"
    # the cluster proxy: its re-render and its check before it forwards a form
    fields, error = cluster_routes._peer_save_fields(FakeForm(form_of('{"name": "a"')),
                                                     stored.to_dict(), "2.9.0")
    assert fields is None and "line 1, column 13" in error, error
    fields, error = cluster_routes._peer_save_fields(FakeForm(form_of("5")), stored.to_dict(),
                                                     "2.8.1")
    assert fields is None and "valid JSON array" in error, error
    print("a refused form comes back with the points as typed and its id  OK")


def test_non_ascii_text():
    m = gateway([{"name": "Sıcaklık", "fn": 4}, {"name": "Basınç/ölçüm", "fn": 3}])
    text = _modbus_points_json(m)
    assert '"name": "Sıcaklık"' in text and "\\u" not in text, text
    assert [p["name"] for p in json.loads(text)] == ["Sıcaklık", "Basınç/ölçüm"]
    assert [p["name"] for p in _parse_modbus_points(text, strict=True)] == ["Sıcaklık",
                                                                         "Basınç/ölçüm"]
    # an unpaired surrogate (only a JSON escape makes one): refused, never a failed page
    lone = gateway([{"name": "S\ud800k", "fn": 3}])
    assert unstorable_text(lone.to_dict()) == "modbus_poll.points[0].name"
    shown = _modbus_points_json(lone)
    shown.encode("utf-8")
    assert "\\ud800" in shown and json.loads(shown)[0]["name"] == "S\ud800k"
    try:
        lone.validate_input()
        raise AssertionError("an edit took an unpaired surrogate")
    except ConfigError as e:
        assert str(e).startswith("modbus_poll.points[0].name has a character that can't be "
                                 "stored"), e
    for bad in (gateway([], name="PLC \udfff"), gateway([], mqtt={"base_topic": "a\ud800"}),
                gateway([], options={"banner": "x\udc80"})):
        try:
            bad.validate_input()
            raise AssertionError("an edit took an unpaired surrogate")
        except ConfigError as e:
            assert "can't be stored" in str(e), e
    try:
        _parse_modbus_points('[{"name": "a"}, {"name": "S\\ud800"}]', strict=True)
        raise AssertionError("the points JSON took an unpaired surrogate")
    except ValueError as e:
        assert str(e).startswith("Modbus poll point #2 has a character that can't be stored"), e
    # the export: unescaped, valid JSON the importer reads; escaped only if it must be
    doc = {"version": 1, "mappings": [m.to_dict()]}
    out = json_text(doc, indent=2)
    assert "Sıcaklık" in out and "\\u" not in out
    plan = parse_mappings_file(out.encode("utf-8"))
    assert [p.name for p in plan.mappings[0].modbus_poll.points] == ["Sıcaklık", "Basınç/ölçüm"]
    assert "\\ud800" in json_text({"a": "\ud800"}) and json_text({"a": "ı"}) == '{"a": "ı"}'
    print("Turkish names unescaped in the points JSON and the export; unpaired surrogates "
          "refused  OK")


def test_form_point_keys():
    """word_order, bit and length are in the points JSON when set; a form that can't carry
    them (an older peer's: no options marker) keeps each point's stored ones by name, where
    they still fit the point as posted."""
    m = gateway([{"name": "a", "fn": 3, "dtype": "float32", "word_order": "CDAB"},
                 {"name": "s", "fn": 3, "dtype": "string", "length": 4, "word_order": "BADC"},
                 {"name": "b", "fn": 4, "dtype": "bool", "bit": 2},
                 {"name": "u", "fn": 3}], id="abcdef012345")
    shown = json.loads(_modbus_points_json(m))
    assert shown[0]["word_order"] == "CDAB" and shown[1]["length"] == 4 and shown[2]["bit"] == 2
    assert not {"word_order", "bit", "length"} & set(shown[3]), "defaults are not shown"
    older = [{"name": "a", "fn": 3, "dtype": "float32"}, {"name": "s", "fn": 3, "dtype": "string"},
             {"name": "b", "fn": 4, "dtype": "uint16"}, {"name": "u", "fn": 3}]
    form = form_of(json.dumps(older))
    del form["modbus_point_options_shown"]
    new = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
    _preserve_unmanaged_fields(new, m, form)
    a, s, b, u = new.modbus_poll.points
    assert (a.word_order, s.length, s.word_order, b.bit) == ("CDAB", 4, "BADC", None), new
    new.validate()
    new.validate_input()
    form = form_of(json.dumps(older))  # this version's form: its JSON is what counts
    new = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
    _preserve_unmanaged_fields(new, m, form)
    assert new.modbus_poll.points[0].word_order == "ABCD"
    print("form: word_order / bit / length shown when set, kept for an older peer's form  OK")


def test_form_block_reads():
    """Block reads are off by default and in the form (with their own marker); a form
    without them (an older peer's, another protocol) keeps the stored ones; an edit
    refuses values that can't be used and a stored one reads as off / gap 0."""
    from app.config import poll_block_gap
    assert poll_block_gap(gateway([]).modbus_poll) is None, "off by default"
    stored = gateway([{"name": "a", "fn": 3}], id="abcdef012345",
                     modbus_poll={"points": [{"name": "a", "fn": 3}], "block_reads": True,
                                  "block_gap": 4})
    assert poll_block_gap(stored.modbus_poll) == 4

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, stored, form)
        return m.modbus_poll.block_reads, m.modbus_poll.block_gap
    points = '[{"name": "a", "fn": 3}]'
    assert parsed(form_of(points)) == (True, 4), "kept for a form without the fields"
    assert parsed(form_of(points, modbus_block_reads_shown="1")) == (False, 0)
    assert parsed(form_of(points, modbus_block_reads_shown="1", modbus_block_reads="on",
                          modbus_block_gap="10")) == (True, 10)
    assert parsed(form_of(points, modbus_block_reads_shown="1", modbus_block_reads="on",
                          modbus_block_gap="")) == (True, 0)
    for poll, words in (({"block_reads": "yes"}, "Block reads must be on or off"),
                        ({"block_gap": 125}, "from 0 to 124"),
                        ({"block_gap": -1}, "from 0 to 124"),
                        ({"block_gap": 2.5}, "from 0 to 124")):
        m = gateway([], modbus_poll=dict({"points": [{"name": "a", "fn": 3}]}, **poll))
        m.validate()
        try:
            m.validate_input()
            raise AssertionError(f"took {poll}")
        except ConfigError as e:
            assert words in str(e), (poll, str(e))
    assert poll_block_gap(ModbusPoll(block_reads="yes")) is None
    assert poll_block_gap(ModbusPoll(block_reads=True, block_gap="x")) == 0
    print("form: block reads off by default, kept for an older form; unusable values "
          "refused, stored ones read safely  OK")


def test_form_unit_map():
    """The unit map in the form: "255=1, 0=1"; a form without the field (an older peer's,
    another protocol) keeps the stored one; a pair that is not one is refused."""
    from app.web.routes.forms import _parse_unit_map
    assert _parse_unit_map("255=1, 0=1;7=2  ", strict=True) == {"255": 1, "0": 1, "7": 2}
    assert _parse_unit_map("", strict=True) == {}
    for bad in ("255", "a=1", "1=b", "1=2=3", "²=1", "-1=2"):
        try:
            _parse_unit_map(bad, strict=True)
            raise AssertionError(f"took {bad!r}")
        except ValueError as e:
            assert "pairs such as 255=1" in str(e), e
    assert _parse_unit_map("255=1 junk", strict=False) == {"255": 1}
    stored = gateway([{"name": "a", "fn": 3}], id="abcdef012345",
                     options={"modbus_unit_map": {"255": 1}})
    assert stored.options.modbus_unit_map_text == "255=1"

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, stored, form)
        return m.options.modbus_unit_map
    points = '[{"name": "a", "fn": 3}]'
    assert parsed(form_of(points)) == {"255": 1}, "kept for a form without the field"
    assert parsed(form_of(points, opt_modbus_unit_map="")) == {}
    assert parsed(form_of(points, opt_modbus_unit_map="0=4, 255=4")) == {"0": 4, "255": 4}
    m = MappingConfig.from_dict(_build_mapping_dict(form_of(points, opt_modbus_unit_map="300=1"),
                                                    strict=True))
    try:
        m.validate_input()
        raise AssertionError("took 300=1")
    except ConfigError as e:
        assert "0-255" in str(e), e
    print("form: the unit map parsed, kept for an older form, unusable pairs refused  OK")


def boot_config(port):
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    m = gateway([{"name": "kept", "fn": 3, "address": 4}], id="aaaaaaaaaaa1",
                enabled=False).to_dict()
    m["name"] = "Hat-1 ölçüm"
    m["modbus_poll"]["points"][0]["name"] = "Sıcaklık"
    running = gateway([{"name": "kept", "fn": 3}], id="aaaaaaaaaaa2", name="Running",
                      mqtt={"enabled": False}).to_dict()
    running["serial"]["port"] = "/dev/ser2net-test-missing"
    running["network"]["port"] = free_port()
    m["network"]["port"] = port
    return {"users": users, "api_token_hash": hash_token(TOKEN), "api_token_role": "admin",
            "mappings": [m, running]}


def test_server():
    def api(method, path, body):
        req = urllib.request.Request(srv.base + path, method=method,
                                     data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {TOKEN}",
                                              "Content-Type": "application/json"})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def points():
        return [p["name"] for p in srv.saved_config()["mappings"][0]["modbus_poll"]["points"]]
    kept = ["Sıcaklık"]

    port = free_port()
    with Server(boot_config(port)) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, page = b.get("/api/mappings/aaaaaaaaaaa1/form")
        assert st == 200 and '&#34;name&#34;: &#34;Sıcaklık&#34;' in page, page[-1500:]
        assert "u0131" not in page
        assert 'hx-post="/api/mappings/aaaaaaaaaaa1/modbus-read"' in page
        st, _, page = b.get("/api/mappings/form")
        assert st == 200 and "modbus-read" not in page, "no test read for a new mapping"
        typed = '[{"name": "x", "fn": 3}, {"name": "w", "fn": 3, "writable": true, "poll": false}]'
        st, _, frag = b.post("/api/mappings/aaaaaaaaaaa1/modbus-read", {"modbus_points": typed},
                             htmx=True)
        assert st == 200 and "Start this Modbus gateway" in frag, (st, frag[:400])
        st, _, frag = b.post("/api/mappings/aaaaaaaaaaa2/modbus-read",
                             {"modbus_points": '[{"name": "x",'}, htmx=True)
        assert st == 200 and "line 1, column 15" in frag, (st, frag[:400])
        st, _, frag = b.post("/api/mappings/aaaaaaaaaaa2/modbus-read", {"modbus_points": typed},
                             htmx=True)
        assert st == 200 and "data-modbus-read" in frag, (st, frag[:600])
        assert ("the serial port is down" in frag or "no reply from unit 1" in frag), frag[:800]
        assert "not read: unit 0 (broadcast) or write-only" in frag, frag[:800]
        st, _, frag = b.post("/api/mappings/nosuchmap/modbus-read", {"modbus_points": typed},
                             htmx=True)
        assert st == 404
        print("test read: a stopped gateway says so, a JSON error is shown, a running one "
              "answers per point  OK")
        st, headers, raw = b.get("/settings/config/export", raw=True)
        assert st == 200 and headers.get("Content-Type") == "application/json; charset=utf-8"
        assert "Sıcaklık".encode() in raw and "Hat-1 ölçüm".encode() in raw and b"\\u" not in raw
        st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                             files={"file": ("ser2net-mappings.json", raw)})
        assert st == 200 and "Import failed" not in page, page[-800:]
        row = page.split("Import preview", 1)[1].split("<tbody>", 1)[1].split("</tr>", 1)[0]
        assert "ölçüm" in row and "unchanged" in row, row
        print("edit form and export: Turkish names unescaped; the export imports again  OK")

        edit = dict(form_of(""), id="aaaaaaaaaaa1", network_port=str(port), name="PLC")
        edit.pop("enabled")
        for text, words in (('[{"name": "a",}]', "line 1, column 14"),
                            ('{"name": "x"} junk', "line 1, column 15"),
                            ("5", "valid JSON array"),
                            ('["temp", "hum"]', "#1 is not an object"),
                            ('[{"name": "a", "unit": "one"}]', "unit must be 0-255"),
                            ('[{"name": "a", "unit": 1.5}]', "unit must be a whole number"),
                            ('[{"name": "a", "adress": 1}]', "Did you mean"),
                            ('[{"name": "S\\ud800"}]', "can&#39;t be stored")):
            st, _, body = b.post("/api/mappings/save", dict(edit, modbus_points=text))
            assert st == 400 and words in body, (text, st, body[:600])
            assert 'name="id" value="aaaaaaaaaaa1"' in body, text
            assert '<textarea name="modbus_points"' in body
            shown = body.split('<textarea name="modbus_points"', 1)[1].split(">", 1)[1]
            assert shown.startswith(text.replace('"', "&#34;")), (text, shown[:80])
            assert points() == kept and len(srv.saved_config()["mappings"]) == 2
        st, _, body = b.post("/api/mappings/save", dict(
            edit, modbus_points='[{"name": "b", "unit": "2", "scale": "0.5", "fn": 3, '
                                '"writable": "false"}]'))
        assert st == 200, (st, body[:600])
        saved = srv.saved_config()["mappings"][0]["modbus_poll"]["points"][0]
        assert (saved["unit"], saved["scale"], saved["writable"]) == (2, 0.5, False), saved
        print("form: invalid points answered 400 with the text and id kept; loose values "
              "saved as what was meant  OK")

        body = {"name": "R", "serial": {"port": "/dev/ser2net-test-R"},
                "network": {"protocol": "modbus", "bind_ip": "127.0.0.1", "port": free_port()},
                "modbus_poll": {"points": [{"name": "a", "unit": "one"}]}}
        st, out = api("POST", "/api/v1/mappings", body)
        assert st == 400 and "unit must be 0-255" in out.get("detail", ""), (st, out)
        body["modbus_poll"]["points"] = ["temp"]
        st, out = api("POST", "/api/v1/mappings", body)
        assert st == 400 and "#1 is not an object" in out.get("detail", ""), (st, out)
        body["modbus_poll"]["points"] = [{"name": "a", "scale": "x"}]
        st, out = api("POST", "/api/v1/mappings", body)
        assert st == 400 and "scale must be a number" in out.get("detail", ""), (st, out)
        print("REST API: unusable point values answered 400  OK")

        export = {"mappings": [dict(body, modbus_poll={"points": [{"name": "a", "unit": [1]}]})]}
        st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                             files={"file": ("m.json", json.dumps(export).encode())})
        assert st == 200 and "Import failed" in page and "unit must be a whole number" in page, page[-800:]
        print("import: unusable point values refused with a message  OK")


def main():
    test_loose_values_load()
    test_validate_keeps_what_it_took()
    test_edits_refuse_unusable_values()
    test_stored_scale_ignored()
    test_points_json_parse()
    test_refused_form_shown_again()
    test_non_ascii_text()
    test_form_point_keys()
    test_form_block_reads()
    test_form_unit_map()
    test_server()
    print("\nPASS: Modbus poll points")


if __name__ == "__main__":
    main()
