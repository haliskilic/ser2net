"""Named REST API tokens: several tokens, each with a name, a role and an optional expiry.

  * config: a config.json without api_tokens loads and saves exactly as before (no new
    key); api_tokens entries are normalized on load — no valid sha256: left out, a role
    that isn't one: viewer, a bad or doubled name or id: a new one — each with a note, and
    the config stays valid;
  * the token of api_token_hash keeps working with its role and its audit name
    'api-token'; Settings adds named tokens (shown once), each with its own role: a viewer
    token can't write and gets the MQTT password masked even while the main token is admin;
    the audit log names the token (api-token:NAME); /api/v1/metrics takes any of them;
  * an expired token gets 401 "API token expired"; revoking one token leaves the others
    working, also the main one's revoke while named tokens exist; with no token at all the
    API answers api_token_not_configured;
  * Settings refuses a bad name, a taken name, a role other than viewer/operator and a bad
    number of days; it shows when each token was last used (in memory, nothing saved);
  * restoring a backup keeps the current tokens unless its accounts are restored too.

Run: python3 tests/test_api_tokens.py
"""
import json
import os
import re
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app.config import AppConfig
from app.web.auth import hash_password, hash_token

MAIN = "s2n_api-tokens-main-admin-token"
OLD = "s2n_api-tokens-expired-token"


def test_config():
    legacy = {"api_token_hash": hash_token(MAIN), "users": [], "mappings": []}
    cfg = AppConfig.from_dict(legacy)
    assert cfg.api_tokens == [] and not cfg.load_notes and cfg.api_token_role == "admin"
    out = cfg.to_dict()
    assert "api_tokens" not in out and out["api_token_hash"] == hash_token(MAIN), out.keys()
    good = {"id": "0123456789abcdef", "name": "mon", "sha256": "a" * 64, "role": "Viewer",
            "created": 1700000000, "expires": 0}
    cfg = AppConfig.from_dict({**legacy, "api_tokens": [
        good, {"name": "nohash"}, {"sha256": "zz"}, "not a dict",
        {"id": "0123456789abcdef", "name": "mon", "sha256": "B" * 64, "role": "root"},
        {"name": "bad name!", "sha256": "c" * 64, "role": "operator", "expires": "soon"}]})
    cfg.validate()
    names = [(t.name, t.role, t.sha256[:1]) for t in cfg.api_tokens]
    assert names[0] == ("mon", "viewer", "a"), names
    assert [n[1:] for n in names[1:]] == [("viewer", "b"), ("operator", "c")], names
    assert names[1][0].startswith("token-") and names[2][0].startswith("token-"), names
    ids = [t.id for t in cfg.api_tokens]
    assert len(set(ids)) == 3 and ids[0] == "0123456789abcdef", ids
    assert cfg.api_tokens[2].expires == 0.0
    notes = "\n".join(cfg.load_notes)
    assert notes.count("has no valid sha256") == 3 and "'root'" in notes and "'bad name!'" in notes, notes
    again = AppConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert again.api_tokens == cfg.api_tokens and not again.load_notes, again.load_notes
    print("config: no api_tokens -> as before (no new key); entries normalized on load with "
          "notes; round trip stable  OK")


def call(base, method, path, token, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        r = e
    text = r.read()
    try:
        doc = json.loads(text)
    except ValueError:
        doc = text.decode()
    return (r.status if hasattr(r, "status") else r.code), doc


def mapping(name, **extra):
    return dict({"name": name, "enabled": False, "kind": "net",
                 "serial": {"port": f"/dev/ser2net-api-tokens-{name}"},
                 "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": free_port()}},
                **extra)


def test_tokens():
    config = {"users": [{"username": "admin", "password_hash": hash_password("tokenpass1"),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(MAIN),   # no api_token_role: a v2.2 token, admin
              "api_tokens": [{"id": "00000000000000aa", "name": "old", "role": "operator",
                              "sha256": hash_token(OLD), "created": 1, "expires": 2}],
              "mappings": [mapping("secret", mqtt={"enabled": False, "password": "broker-pw"})]}
    with Server(config) as srv:
        base = srv.base
        admin = Browser(base)
        admin.login("admin", "tokenpass1")

        def create(name, role, days=""):
            st, _, page = admin.post("/settings/api-tokens",
                                     {"name": name, "role": role, "expires_days": days})
            assert st == 200, st
            m = re.search(r"s2n_[A-Za-z0-9_\-]+", page)
            return m.group(0) if m else page

        monitor = create("monitor", "viewer")
        deploy = create("deploy", "operator", "1")
        assert monitor.startswith("s2n_") and deploy.startswith("s2n_"), (monitor[:200], deploy[:200])
        saved = srv.saved_config()
        assert saved["api_token_hash"] == hash_token(MAIN) and saved["api_token_role"] == "admin"
        stored = {t["name"]: t for t in saved["api_tokens"]}
        assert stored["monitor"]["sha256"] == hash_token(monitor) and monitor not in json.dumps(saved)
        assert stored["monitor"]["expires"] == 0 and stored["deploy"]["role"] == "operator"
        assert 86000 < stored["deploy"]["expires"] - stored["deploy"]["created"] <= 86400
        print("settings: named tokens created (shown once, only the hash saved); the main "
              "token's keys unchanged  OK")

        # ---- each token with its own role ----
        st, doc = call(base, "GET", "/api/v1/mappings", MAIN)
        assert st == 200 and doc["mappings"][0]["mqtt"]["password"] == "broker-pw"
        st, doc = call(base, "GET", "/api/v1/mappings", monitor)
        assert st == 200 and doc["mappings"][0]["mqtt"]["password"] == "********", doc
        st, doc = call(base, "POST", "/api/v1/mappings", monitor, mapping("m-made"))
        assert st == 403 and doc["error"] == "forbidden", (st, doc)
        st, doc = call(base, "POST", "/api/v1/mappings", deploy, mapping("d-made"))
        assert st == 201, (st, doc)
        st, doc = call(base, "POST", "/api/v1/mappings", MAIN, mapping("main-made"))
        assert st == 201, (st, doc)
        st, text = call(base, "GET", "/api/v1/metrics", monitor)
        assert st == 200 and "ser2net_up 1" in text
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = fh.read()
        assert "\tapi_mapping_create\td-made\tapi-token:deploy" in audit, audit[-600:]
        assert "\tapi_mapping_create\tmain-made\tapi-token\n" in audit, audit[-600:]
        print("tokens: viewer reads (MQTT password masked), operator and the main token write; "
              "the audit log names the token; metrics for any token  OK")

        st, doc = call(base, "GET", "/api/v1/status", OLD)
        assert st == 401 and doc == {"error": "unauthorized", "detail": "API token expired"}, doc
        print("an expired token -> 401 'API token expired'  OK")

        # ---- Settings: last use shown, bad forms refused ----
        st, _, page = admin.get("/settings")
        assert "monitor" in page and "deploy" in page and "api-token:NAME" in page
        assert '<span class="tag">expired</span>' in page, "the expired token is marked"
        used = re.findall(r"last used: (\d{4}-\d\d-\d\d \d\d:\d\d|—)", page)
        assert len(used) == 4 and used.count("—") == 1, used  # main, old (never), monitor, deploy
        assert "last_used" not in json.dumps(srv.saved_config()), "last use is kept in memory only"
        before = srv.saved_config()
        for fields, want in (({"name": "bad name", "role": "viewer"}, "A token name is 1-64"),
                             ({"name": "", "role": "viewer"}, "A token name is 1-64"),
                             ({"name": "MONITOR", "role": "viewer"}, "named &#39;MONITOR&#39; already"),
                             ({"name": "x", "role": "admin"}, "Invalid role."),
                             ({"name": "x", "role": "viewer", "expires_days": "-1"}, "Enter 0"),
                             ({"name": "x", "role": "viewer", "expires_days": "9999"}, "Enter 0"),
                             ({"name": "x", "role": "viewer", "expires_days": "1.5"}, "Enter 0")):
            st, _, page = admin.post("/settings/api-tokens", fields)
            assert st == 400 and want in page and "s2n_" not in page, (fields, want)
        assert srv.saved_config() == before
        print("settings: a bad or taken name, another role, a bad expiry are refused  OK")

        # ---- revoke one: the others keep working ----
        mon_id = next(t["id"] for t in before["api_tokens"] if t["name"] == "monitor")
        st, _, page = admin.post("/settings/api-tokens/revoke", {"id": mon_id})
        assert st == 200 and "API token &#39;monitor&#39; revoked." in page, page[:300]
        assert call(base, "GET", "/api/v1/status", monitor)[0] == 401
        assert call(base, "GET", "/api/v1/status", deploy)[0] == 200
        assert call(base, "GET", "/api/v1/status", MAIN)[0] == 200
        st, _, page = admin.post("/settings/api-tokens/revoke", {"id": mon_id})
        assert "No such token" in page
        print("revoke: that token stops, the others keep working  OK")

        # ---- restore: tokens kept unless the accounts are restored ----
        backups = sorted(os.listdir(os.path.join(srv.data, "backups")))
        first = backups[0]   # from before any named token was created here
        st, _, page = admin.post("/settings/backups/restore", {"name": first})
        assert st == 200 and "Restored" in page, page[:300]
        assert call(base, "GET", "/api/v1/status", deploy)[0] == 200, "a restore brought tokens back"
        st, _, page = admin.get(f"/settings?restore={first}")
        assert "The other REST API tokens as they were then: old" in page, "preview names them"
        st, _, page = admin.post("/settings/backups/restore", {"name": first, "accounts": "on"})
        assert st == 200 and "Restored" in page
        assert call(base, "GET", "/api/v1/status", deploy)[0] == 401
        assert [t["name"] for t in srv.saved_config()["api_tokens"]] == ["old"]
        print("restore: named tokens kept; with the accounts, the backup's tokens  OK")

        # ---- the main token revoked: the named ones still work; none left: not configured
        admin = Browser(base)
        admin.login("admin", "tokenpass1")
        st, _, page = admin.post("/settings/api-token/revoke", {})
        assert st == 200 and "The other tokens keep working." in page, page[:400]
        assert call(base, "GET", "/api/v1/status", MAIN)[0] == 401
        assert call(base, "GET", "/api/v1/status", OLD)[1]["detail"] == "API token expired"
        old_id = srv.saved_config()["api_tokens"][0]["id"]
        admin.post("/settings/api-tokens/revoke", {"id": old_id})
        st, doc = call(base, "GET", "/api/v1/status", OLD)
        assert st == 401 and doc == {"error": "api_token_not_configured"}, doc
        assert "api_tokens" not in srv.saved_config(), "no named token: the key is left out"
        print("main token revoked: named tokens still work; no token at all: "
              "api_token_not_configured  OK")


def main():
    test_config()
    test_tokens()
    print("\nPASS: named REST API tokens")


if __name__ == "__main__":
    main()
