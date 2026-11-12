"""Upgrading a v2.7.1 install: its config.json keeps working, nothing is lost.

tests/fixtures/config-2.7.1.json was written by the real v2.7.1 code (every feature
in use: local/LDAP/OIDC users, API token, LDAP/OIDC/cluster settings, all mapping
kinds and protocols, RS-485, a stable-id match, MQTT, Modbus polling, traces with
absolute POSIX and Windows paths). This test:
  * loads it with the current code: it validates, and every key/value 2.7.1 wrote is
    still there after a save — except the documented trace-path change (only the
    file name is kept; traces go to data/traces) — while the new settings take their
    defaults;
  * boots the real server on it the Docker/headless way (SER2NET_BIND_IP re-saves the
    config): the old passwords and API token work, the trace move is logged, and the
    untouched 2.7.1 file is kept in data/backups/;
  * saves every mapping unchanged through the web form (posted as a browser posts it,
    tests/_formsubmit.py): nothing changes — PLC-1 keeps its MQTT client id and its USB
    identity, also when the form first came back with an error.

Run: python3 tests/test_upgrade_from_2_7.py
"""
import copy
import json
import os
import re
import urllib.parse
import urllib.request

from _formsubmit import browser_post
from _server import Browser, Server, free_port

from app.config import AppConfig, ConfigStore

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "config-2.7.1.json")
# what 2.8 changes in a 2.7.1 config on purpose (path -> file name; see config.trace_file_name)
EXPECTED_CHANGES = {
    ("mappings", 0, "options", "trace_both"): "plc1.trace",
    ("mappings", 1, "options", "trace_both"): "console.log",
}


def walk(old, new, path=()):
    """Yield (path, old, new) for every leaf of `old` whose value differs in `new`."""
    if isinstance(old, dict):
        for k, v in old.items():
            if not isinstance(new, dict) or k not in new:
                yield path + (k,), v, "<missing>"
            else:
                yield from walk(v, new[k], path + (k,))
    elif isinstance(old, list):
        if not isinstance(new, list) or len(new) != len(old):
            yield path, old, new
            return
        for i, (a, b) in enumerate(zip(old, new, strict=True)):
            yield from walk(a, b, path + (i,))
    elif old != new:
        yield path, old, new


def test_roundtrip(original):
    cfg = AppConfig.from_dict(copy.deepcopy(original))
    cfg.validate()
    saved = json.loads(json.dumps(cfg.to_dict()))
    diffs = {p: n for p, _o, n in walk(original, saved)}
    assert diffs == EXPECTED_CHANGES, f"unexpected changes: {diffs}"
    m0 = saved["mappings"][0]["options"]
    assert (m0["trace_max_mb"], m0["trace_keep"]) == (10.0, 3), "new settings must default"
    assert saved["session_timeout_s"] == 28800 and saved["admin_ui"]["require_login"] is True
    # LDAPS in the fixture: its certificate is checked from now on (LDAP is off there, so
    # loading it notes nothing about it)
    assert (saved["ldap"]["tls_verify"], saved["ldap"]["ca_file"]) == (True, "")
    assert not [n for n in cfg.load_notes if "LDAP" in n], cfg.load_notes
    # the API token keeps the role 2.7.1 saved (an unknown one would become viewer + a note)
    assert cfg.api_token_role == "operator" and not [n for n in cfg.load_notes if "api_token" in n]
    print("load + validate + save: every 2.7.1 value kept (only the 2 trace paths -> names)  OK")


def post_form(b, path, fields):
    """POST a form as htmx does (the CSRF token in its header)."""
    req = urllib.request.Request(b.base + path, data=urllib.parse.urlencode(fields).encode(),
                                 method="POST", headers={
                                     "Content-Type": "application/x-www-form-urlencoded",
                                     "X-CSRF-Token": b.csrf(), "HX-Request": "true"})
    st, headers, body = b._open(req)
    return st, headers, body.decode("utf-8", "replace")


def test_form_saves(srv):
    b = Browser(srv.base)
    b.login("admin", "old-admin-pass-1")
    before = srv.saved_config()["mappings"]
    for m in before:
        st, _h, page = b.get(f"/api/mappings/{m['id']}/form")
        assert st == 200, (m["name"], st)
        st, _h, _body = post_form(b, "/api/mappings/save", browser_post(page))
        assert st == 200, (m["name"], st, _body[:500])
    after = srv.saved_config()["mappings"]
    assert after == before, [(a["name"], a, b_) for a, b_ in zip(before, after, strict=True)
                             if a != b_][:1]
    assert after[0]["mqtt"]["client_id"] == "s2n-plc1"
    # a save that comes back with an error, then is saved as shown again
    st, _h, page = b.get(f"/api/mappings/{before[0]['id']}/form")
    fields = [(k, "10.0.0.300" if k == "network_bind_ip_custom" else
               "custom" if k == "network_bind_ip" else v) for k, v in browser_post(page)]
    st, _h, again = post_form(b, "/api/mappings/save", fields)
    assert st == 400 and "Invalid bind IP" in again, (st, again[:500])
    assert re.search(r'name="serial_usb_match" checked', again), "the USB box came back unticked"
    fields = [(k, before[0]["network"]["bind_ip"] if k == "network_bind_ip_custom" else v)
              for k, v in browser_post(again)]
    st, _h, _body = post_form(b, "/api/mappings/save", fields)
    assert st == 200, (st, _body[:500])
    assert srv.saved_config()["mappings"] == before, "the save after an error changed PLC-1"
    print("web form: every 2.7.1 mapping saved unchanged keeps every setting (PLC-1's MQTT "
          "client id, its USB identity also after an error)  OK")


def main():
    with open(FIXTURE, encoding="utf-8") as fh:
        original = json.load(fh)
    test_roundtrip(original)

    boot = copy.deepcopy(original)
    for m in boot["mappings"]:  # free ports, so the listeners can't clash on this machine
        if m["network"]["mode"] in ("server", "udp"):
            m["network"]["port"] = free_port()
    with Server(boot) as srv:
        log = srv.output()
        assert "its trace file is now traces/aaaa00000001-plc1.trace" in log, log[-1500:]
        assert "is not a valid mapping id" not in log, "a 2.7.1 mapping id was replaced"
        backups = ConfigStore(srv.config_path).backup_names()[::-1]  # oldest first
        assert len(backups) == 2, backups  # the 2.7.1 file, then the one it was replaced by
        with open(os.path.join(srv.data, "backups", backups[0]), encoding="utf-8") as fh:
            assert json.load(fh) == boot, "the backup must be the untouched 2.7.1 file"
        with open(os.path.join(srv.data, "backups", backups[1]), encoding="utf-8") as fh:
            assert json.load(fh) == srv.saved_config(), "the newest backup is config.json"
        print("boot (re-save): trace move logged, the 2.7.1 config.json kept in data/backups  OK")

        for user, pw in (("admin", "old-admin-pass-1"), ("op1", "old-op-pass-1"),
                         ("viewer1", "old-viewer-pass-1")):
            Browser(srv.base).login(user, pw)
        req = urllib.request.Request(srv.base + "/api/v1/mappings",
                                     headers={"Authorization": "Bearer s2n_upgrade-from-2.7.1"})
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert len(body["mappings"]) == 7
        saved = srv.saved_config()
        assert [m["id"] for m in saved["mappings"]] == [m["id"] for m in boot["mappings"]]
        assert saved["mappings"][0]["serial"]["match"]["serial_number"] == "A12345"
        assert saved["mappings"][5]["modbus_poll"]["points"][0]["dtype"] == "float32"
        assert saved["users"][3]["source"] == "ldap" and saved["users"][4]["source"] == "oidc"
        print("old passwords (admin/operator/viewer) and the API token work; ids, match, "
              "Modbus points, LDAP/OIDC users intact  OK")
        test_form_saves(srv)
    print("\nPASS: upgrade from a v2.7.1 config")


if __name__ == "__main__":
    main()
