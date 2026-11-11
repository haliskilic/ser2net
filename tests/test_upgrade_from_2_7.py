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
    untouched 2.7.1 file is kept in data/backups/.

Run: python3 tests/test_upgrade_from_2_7.py
"""
import copy
import json
import os
import urllib.request

from _server import Browser, Server, free_port

from app.config import AppConfig

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
    print("load + validate + save: every 2.7.1 value kept (only the 2 trace paths -> names)  OK")


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
        backups = os.listdir(os.path.join(srv.data, "backups"))
        assert len(backups) == 1, backups
        with open(os.path.join(srv.data, "backups", backups[0]), encoding="utf-8") as fh:
            assert json.load(fh) == boot, "the backup must be the untouched 2.7.1 file"
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
    print("\nPASS: upgrade from a v2.7.1 config")


if __name__ == "__main__":
    main()
