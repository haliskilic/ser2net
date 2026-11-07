"""Settings -> LAN cluster: the key is write-only and a key change keeps the fleet together.

  * the stored key never appears in the page (a blank field keeps it); a short or
    simple key is flagged; removing the key while the cluster is on is refused;
  * a changed key keeps the old one accepted for 7 days (a planned change: requests
    with it still pass) unless that is unticked (a leaked key); "Stop accepting the
    previous key" ends it early; "Generate a new key and save" shows a random key once;
  * legacy authentication and auto-discovery are switches; a form without them (a
    tab opened before the upgrade, a script) leaves them as they are;
  * a beacon signed with another key is noted in the fleet view (never contacted);

Run: python3 tests/test_cluster_settings.py
"""
import re
import time
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app.engine.cluster import key_header
from app.web.auth import hash_password

PW = "cluster-settings-1"
KEY = "settings-test-key-0123456789"


def local_status(srv, key) -> int:
    """GET /api/cluster/local with the raw key (as a node running 2.8.0 or older sends it)."""
    req = urllib.request.Request(srv.base + "/api/cluster/local",
                                 headers={"X-Cluster-Key": key_header(key)})
    try:
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    disc = free_port()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "cluster": {"enabled": True, "key": KEY, "discovery_port": disc}}
    with Server(cfg) as srv:
        admin = Browser(srv.base)
        admin.login("admin", PW)

        def cluster():
            return srv.saved_config()["cluster"]

        def save(**fields):
            form = {"cluster_form": "2", "enabled": "on", "legacy_auth": "on", "discovery": "on",
                    "keep_old_key": "on", "discovery_port": str(disc)}
            form.update(fields)
            form = {k: v for k, v in form.items() if v is not None}
            st, _, page = admin.post("/settings/cluster", form)
            assert st == 200, (st, page[:300])
            return page

        # ---- the key is write-only ----
        st, _, page = admin.get("/settings")
        assert st == 200 and KEY not in page, "the cluster key must never be rendered"
        m = re.search(r'<input type="password" name="key"[^>]*>', page)
        assert m and "value=" not in m.group(0) and 'autocomplete="new-password"' in m.group(0)
        assert "(unchanged)" in m.group(0) and "short or simple" not in page
        page = save()                                    # blank = unchanged
        assert cluster()["key"] == KEY and cluster()["previous_key"] == "" and KEY not in page
        print("cluster key: never rendered, a blank field keeps it  OK")

        # ---- a planned change: the old key is still accepted, for 7 days ----
        new_key = "the-replacement-key-0123456789"
        page = save(key=new_key)
        c = cluster()
        assert c["key"] == new_key and c["previous_key"] == KEY, c
        assert abs(c["previous_until"] - (time.time() + 7 * 86400)) < 120, c
        assert "The previous key is still accepted until" in page
        assert "The previous key is also accepted, until" in page and new_key not in page
        assert local_status(srv, KEY) == 200 and local_status(srv, new_key) == 200
        page = save(clear_previous_key="on")
        assert cluster()["previous_key"] == "" and local_status(srv, KEY) == 403
        print("key change: the previous key keeps working until it is stopped (or 7 days)  OK")

        # ---- a leaked key: no overlap; a weak key is flagged ----
        page = save(key="abc123", keep_old_key=None)
        assert cluster()["key"] == "abc123" and cluster()["previous_key"] == ""
        assert local_status(srv, new_key) == 403
        assert "Only the new key is accepted now" in page and "short or simple" in page
        print("key change without overlap; a short key is flagged  OK")

        # ---- generate: a random key, shown once, the old one kept for the change ----
        page = save(then="generate")
        gen = cluster()["key"]
        assert len(gen) >= 40 and gen != "abc123" and cluster()["previous_key"] == "abc123"
        assert f'<div class="mono token-box">{gen}</div>' in page and "short or simple" not in page
        assert gen not in admin.get("/settings")[2], "a generated key is shown only once"
        st, _, page = admin.post("/settings/cluster", {"cluster_form": "2", "enabled": "on",
                                                       "clear_key": "on",
                                                       "discovery_port": str(disc)})
        assert st == 200 and "no shared key is set" in page and cluster()["key"] == gen
        print("generate: a random key shown once; removing the key with the cluster on refused  OK")

        # ---- the switches; a form without them leaves them alone ----
        save(legacy_auth=None, discovery=None)
        assert (cluster()["legacy_auth"], cluster()["discovery"]) == (False, False)
        assert local_status(srv, gen) == 403, "legacy authentication off: the raw key is refused"
        assert "auto-discovery off" in srv.output()
        admin.post("/settings/cluster", {"enabled": "on", "discovery_port": str(disc)})
        assert (cluster()["legacy_auth"], cluster()["discovery"]) == (False, False), cluster()
        assert cluster()["key"] == gen
        save()
        assert (cluster()["legacy_auth"], cluster()["discovery"]) == (True, True)
        print("legacy authentication / auto-discovery switches; older forms keep them  OK")
    print("\nPASS: cluster settings (write-only key, key change, switches)")


if __name__ == "__main__":
    main()
