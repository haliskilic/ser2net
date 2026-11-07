"""Settings -> LAN cluster: the key is write-only, a key change keeps the fleet together,
and a copy of this installation is reported and can be given a new identity.

  * the stored key never appears in the page (a blank field keeps it); a short or
    simple key is flagged; removing the key while the cluster is on is refused;
  * a changed key keeps the old one accepted for 7 days (a planned change: requests
    with it still pass) unless that is unticked (a leaked key); "Stop accepting the
    previous key" ends it early; "Generate a new key and save" shows a random key once;
  * legacy authentication and auto-discovery are switches; a form without them (a
    tab opened before the upgrade, a script) leaves them as they are;
  * a beacon signed with another key is noted in the fleet view (never contacted);
  * a beacon with this node's own id from another process (a copy of its data folder,
    disk or VM image) is reported in Settings, the fleet view and the log; "Give this
    node a new identity" changes the cluster id and the session key: the admin doing
    it stays signed in, every other session ends.

Run: python3 tests/test_cluster_settings.py
"""
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from _server import Browser, Server, free_port

from app.engine.cluster import _sign, key_header
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


def beacon(key, **fields) -> bytes:
    payload = {"v": 2, "name": "copy-of-me", "ip": "127.0.0.1", "port": 8080,
               "scheme": "http", "t": round(time.time(), 3), "b": "a0ther0run0000ff",
               "ips": [], **fields}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return json.dumps({"d": body, "s": _sign(key, body)}).encode()


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


def main():
    disc = free_port()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "cluster": {"enabled": True, "key": KEY, "discovery_port": disc}}
    with Server(cfg) as srv:
        admin, other = Browser(srv.base), Browser(srv.base)
        admin.login("admin", PW)
        other.login("admin", PW)

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

        # ---- a node with another key, and a copy of this node, on the network ----
        node_id = srv.saved_config()["instance_id"]
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            def broadcast():
                udp.sendto(beacon("some-other-fleet-key-000", id="stranger1"), ("127.0.0.1", disc))
                udp.sendto(beacon(gen, id=node_id), ("127.0.0.1", disc))
                return "Another node uses this node’s identity" in admin.get("/settings")[2]
            assert wait_for(broadcast), "a copy of this node must be reported in Settings"
            st, _, fleet = admin.get("/api/cluster/status")
            assert "Another node uses this node’s identity" in fleet and "copy-of-me" in fleet
            assert "broadcast with a different cluster key" in fleet and "127.0.0.1" in fleet
            assert "stranger1" not in fleet, "a node with another key is not listed"
            assert "another node uses this node's identity" in srv.output()
        finally:
            udp.close()
        print("a copy of this node and a node with another key are reported  OK")

        # ---- a new identity: new cluster id + session key, the admin stays signed in ----
        old_secret = srv.saved_config()["secret_key"]
        st, _, page = admin.post("/settings/cluster/identity", {})
        saved = srv.saved_config()
        assert st == 200 and "This node has a new identity" in page
        assert saved["instance_id"] != node_id and saved["secret_key"] != old_secret
        assert "Another node uses this node’s identity" not in page
        assert admin.get("/settings")[0] == 200, "the admin who did it stays signed in"
        st, headers, _ = other.get("/settings")
        assert st == 303 and headers.get("Location") == "/login", "other sessions end"
        audit = open(os.path.join(srv.data, "audit.log"), encoding="utf-8").read()
        assert "\tnode_new_identity\t" in audit and "\tcluster_key_change\t" in audit
        print("new identity: new cluster id and session key; this admin stays, others are "
              "signed out  OK")
    print("\nPASS: cluster settings (write-only key, key change, switches, copies, identity)")


if __name__ == "__main__":
    main()
