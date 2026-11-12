"""REST API: POST /api/v1/import (one atomic change, with a dry run) and /api/v1/metrics.

  * /api/v1/metrics answers the Prometheus metrics to an API token (Prometheus can scrape
    them with `authorization: credentials_file`); without a token it is refused, and
    /metrics still needs a web UI session;
  * a dry run answers what the import would do (add / update / same / remove rows and
    whether the result could be saved) and changes nothing — not even a backup;
  * a real merge import adds and updates in one save (one backup), audited; what
    GET /api/v1/mappings answers can be sent back as the body;
  * replace with a result that is not valid (two mappings on one port) is refused as a
    whole: nothing changes, no backup; the dry run names the problem;
  * a classic ser2net.conf is accepted as text; a body that is nothing importable and
    unknown query values are refused.

Run: python3 tests/test_rest_import.py
"""
import json
import os
import urllib.error
import urllib.request

from _server import Server, free_port

from ser2netweb.web.auth import hash_password, hash_token

TOKEN = "s2n_rest-import-operator-token"


def call(base, method, path, body=None, token=TOKEN, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": ctype}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        r = e
    text = r.read()
    code = r.status if hasattr(r, "status") else r.code
    try:
        return code, r.headers, json.loads(text)
    except ValueError:
        return code, r.headers, text.decode("utf-8", "replace")


def mapping(name, port, mid=None, **extra):
    m = {"name": name, "enabled": False, "kind": "net",
         "serial": {"port": f"/dev/ser2net-rest-import-{name}"},
         "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port}}
    if mid:
        m["id"] = mid
    m.update(extra)
    return m


def main():
    port_a, port_b = free_port(), free_port()
    config = {"users": [{"username": "admin", "password_hash": hash_password("importpass1"),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "operator",
              "mappings": [mapping("alpha", port_a, "alpha0000001"),
                           mapping("beta", port_b, "beta00000001")]}
    with Server(config) as srv:
        base = srv.base
        backups = os.path.join(srv.data, "backups")

        def backup_count():
            return len(os.listdir(backups)) if os.path.isdir(backups) else 0

        # ---- metrics with the API token ----
        st, h, text = call(base, "GET", "/api/v1/metrics")
        assert st == 200 and h["Content-Type"].startswith("text/plain"), (st, text)
        assert "ser2net_up 1" in text and 'ser2net_mapping_running{mapping="alpha"' in text, text
        st, _, doc = call(base, "GET", "/api/v1/metrics", token=None)
        assert st == 401 and doc["error"] == "unauthorized", (st, doc)
        st, _, _ = call(base, "GET", "/api/v1/metrics", token="s2n_wrong")
        assert st == 401
        req = urllib.request.Request(base + "/metrics",
                                     headers={"Authorization": f"Bearer {TOKEN}"})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        try:
            urllib.request.build_opener(NoRedirect).open(req, timeout=10)
            raise AssertionError("/metrics answered without a session")
        except urllib.error.HTTPError as e:
            # a client that sends credentials is no browser: 401, which names the REST route
            assert e.code == 401 and e.headers["WWW-Authenticate"].startswith("Bearer"), e.code
            assert b"/api/v1/metrics" in e.read()
        print("metrics: /api/v1/metrics with the API token; no token -> 401; /metrics still "
              "needs a session (a token there: 401)  OK")

        # ---- dry run: what GET /mappings answers, one changed + one new ----
        st, _, listing = call(base, "GET", "/api/v1/mappings")
        assert st == 200 and len(listing["mappings"]) == 2
        doc = {"mappings": listing["mappings"] + [mapping("gamma", free_port())]}
        doc["mappings"][0]["serial"]["baudrate"] = 115200          # alpha: update
        before, n_backups = srv.saved_config(), backup_count()
        st, _, res = call(base, "POST", "/api/v1/import?mode=merge&dry_run=true", doc)
        assert st == 200 and res["dry_run"] is True and res["applied"] is False, (st, res)
        assert res["valid"] is True and res["problem"] is None and res["count"] == 3, res
        assert {r["name"]: r["action"] for r in res["rows"]} == {
            "alpha": "update", "beta": "same", "gamma": "add"}, res["rows"]
        assert res["summary"] == {"add": 1, "update": 1, "same": 1, "remove": 0}, res
        assert srv.saved_config() == before and backup_count() == n_backups, \
            "a dry run changed the configuration"
        print("import dry run: rows and summary, valid; nothing saved, no backup  OK")

        # ---- the real merge: one save, one backup ----
        st, _, res = call(base, "POST", "/api/v1/import", doc)  # mode: merge by default
        assert st == 200 and res["applied"] is True and res["mode"] == "merge", (st, res)
        after = srv.saved_config()
        names = {m["name"]: m for m in after["mappings"]}
        assert sorted(names) == ["alpha", "beta", "gamma"], names
        assert names["alpha"]["serial"]["baudrate"] == 115200
        assert names["alpha"]["id"] == "alpha0000001", "a merge keeps the id of what it updates"
        assert backup_count() == n_backups + 1, "an import must be one save (one backup)"
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            rec = next(ln.rstrip("\n").split("\t") for ln in fh
                       if "\tapi_config_import_merge\t" in ln)
        # (with what the import left as it was: the settings the body doesn't have)
        assert rec[3].startswith("3 mappings (ser2net export)") and rec[4] == "api-token", rec
        print("import merge: added + updated in one save (one backup), ids kept, audited  OK")

        # ---- replace with a clash: refused as a whole ----
        clash = {"mappings": [mapping("one", port_a), mapping("two", port_a)]}
        before, n_backups = srv.saved_config(), backup_count()
        st, _, res = call(base, "POST", "/api/v1/import?mode=replace&dry_run=1", clash)
        assert st == 200 and res["valid"] is False and str(port_a) in res["problem"], res
        assert [r["action"] for r in res["rows"]].count("remove") == 3, res["rows"]
        st, _, res = call(base, "POST", "/api/v1/import?mode=replace", clash)
        assert st == 400 and res["error"] == "validation" and res["applied"] is False, (st, res)
        assert srv.saved_config() == before and backup_count() == n_backups
        print("import replace: a result that isn't valid is refused whole (dry run names the "
              "problem); nothing changed  OK")

        # ---- a classic ser2net.conf, as text ----
        conf = f"{free_port()}:raw:600:/dev/ttyS7:9600 8DATABITS NONE 1STOPBIT\n".encode()
        st, _, res = call(base, "POST", "/api/v1/import?dry_run=true", raw=conf,
                          ctype="text/plain")
        assert st == 200 and res["source"] == "ser2net.conf (ser2net 3.x)", (st, res)
        assert res["rows"][0]["action"] == "add" and res["valid"] is True, res
        print("import: a classic ser2net.conf body (dry run)  OK")

        # ---- refused requests ----
        for path, body, raw, want in (
                ("/api/v1/import?mode=sideways", {"mappings": []}, None, "invalid_parameter"),
                ("/api/v1/import?dry_run=maybe", {"mappings": []}, None, "invalid_parameter"),
                ("/api/v1/import", None, b"not a configuration", "invalid_import"),
                ("/api/v1/import", {"no": "mappings"}, None, "invalid_import"),
                ("/api/v1/import", {"mappings": [{"name": "u", "serial": {"port": "socket://x:1"}}]},
                 None, "invalid_import")):
            st, _, res = call(base, "POST", path, body, raw=raw)
            assert st == 400 and res["error"] == want, (path, st, res)
        assert srv.saved_config() == before
        print("import: an unknown mode or dry_run value and a body that is nothing importable "
              "are refused  OK")

    print("\nPASS: REST API import (atomic, dry run) and metrics with the API token")


if __name__ == "__main__":
    main()
