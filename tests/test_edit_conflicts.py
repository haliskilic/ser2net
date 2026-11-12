"""A mapping edit made for a revision the mapping no longer has is not saved silently.

  * the edit form carries the mapping's revision (an HMAC of its settings with the
    node's secret key: config.mapping_revision; never stored, it tells nothing about the
    settings and differs per node). When the mapping changed after the form was opened
    (by another user, another tab, a REST client, a start or stop), the save is refused
    with a note and the form comes back with the user's values and the new revision, so
    a second save replaces the changes on purpose;
  * a form whose mapping was deleted meanwhile comes back as a new mapping (no id):
    saving it adds the mapping again, with a new id, instead of an error dead end;
  * a form without a revision (a cluster node before 2.9.0) is saved as before;
  * REST API: GET answers the revision as ETag; a PUT or DELETE with If-Match is done only
    while the mapping is at that revision (else 412), one without If-Match as before.

Run: python3 tests/test_edit_conflicts.py
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from _formsubmit import browser_post
from _server import Browser, Server, free_port

from app.config import MappingConfig, mapping_revision
from app.web.auth import hash_password, hash_token

PW = "conflict-pass-1"
TOKEN = "s2n_conflict-token"


def test_revision():
    m = MappingConfig.from_dict({"id": "abcabcabcabc", "name": "m", "mqtt": {"password": "hunter2"}})
    rev = mapping_revision("k1", m)
    assert rev == mapping_revision("k1", MappingConfig.from_dict(m.to_dict())), "stable"
    assert rev != mapping_revision("k2", m), "another node's revision is its own"
    m.enabled = False
    assert rev != mapping_revision("k1", m), "a stop changes it"
    assert re.fullmatch(r"[0-9a-f]{24}", rev) and "hunter2" not in rev
    print("revision: an HMAC of the settings, per node key, changes with any setting  OK")


def post_form(b, path, fields):
    req = urllib.request.Request(b.base + path, data=urllib.parse.urlencode(fields).encode(),
                                 method="POST", headers={
                                     "Content-Type": "application/x-www-form-urlencoded",
                                     "X-CSRF-Token": b.csrf(), "HX-Request": "true"})
    st, _headers, body = b._open(req)
    return st, body.decode("utf-8", "replace")


def api(srv, method, path, body=None, headers=None):
    req = urllib.request.Request(srv.base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json", **(headers or {})})
    try:
        r = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        r = e
    return r.status if hasattr(r, "status") else r.code, r.headers, json.loads(r.read() or b"{}")


def field(fields, name):
    return dict(fields)[name]


def main():
    test_revision()
    mid = "conflict0001"
    config = {"users": [{"username": "admin", "password_hash": hash_password(PW),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(TOKEN), "api_token_role": "operator",
              "mappings": [{"id": mid, "name": "PLC-1", "enabled": False,
                            "serial": {"port": "/dev/ttyCONFLICT"},
                            "network": {"bind_ip": "127.0.0.1", "port": free_port()}}]}
    with Server(config) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)

        def stored():
            return next(m for m in srv.saved_config()["mappings"] if m["id"] == mid)

        # a form opened, then the mapping changed elsewhere (another tab saved it)
        _st, _h, page = b.get(f"/api/mappings/{mid}/form")
        mine = browser_post(page)
        assert field(mine, "rev"), "the form carries the revision"
        other = [(k, "PLC-1 renamed" if k == "name" else v) for k, v in mine]
        assert post_form(b, "/api/mappings/save", other)[0] == 200
        st, again = post_form(b, "/api/mappings/save",
                              [(k, "4711" if k == "network_port" else v) for k, v in mine])
        assert st == 400 and "was changed after this form was opened" in again, (st, again[:800])
        assert stored()["name"] == "PLC-1 renamed", "the stale form must not be saved"
        shown = browser_post(again)
        assert field(shown, "network_port") == "4711", "the form keeps the user's values"
        assert field(shown, "rev") != field(mine, "rev"), "...with the new revision"
        assert post_form(b, "/api/mappings/save", shown)[0] == 200, "a second save replaces"
        assert (stored()["name"], stored()["network"]["port"]) == ("PLC-1", 4711)
        print("stale form: refused with a note, shown again with the user's values; saving "
              "again replaces the changes  OK")

        # a stop (or start) after the form was opened is a change too
        _st, _h, page = b.get(f"/api/mappings/{mid}/form")
        mine = browser_post(page)
        assert post_form(b, f"/api/mappings/{mid}/start", {})[0] == 200
        assert post_form(b, "/api/mappings/save", mine)[0] == 400, "the form predates the start"
        # a form without a revision (an older cluster node's) is saved as before
        st, _body = post_form(b, "/api/mappings/save", [(k, v) for k, v in mine if k != "rev"])
        assert st == 200 and stored()["enabled"] is False, "no revision: no check"
        print("a start after the form was opened is a change; a form without a revision is "
              "saved as before  OK")

        # the mapping was deleted while its form was open: shown as a new mapping
        _st, _h, page = b.get(f"/api/mappings/{mid}/form")
        mine = browser_post(page)
        assert b.post(f"/api/mappings/{mid}", method="DELETE", htmx=True)[0] == 200
        st, again = post_form(b, "/api/mappings/save", mine)
        assert st == 400 and "no longer exists" in again and "New mapping" in again, again[:800]
        assert not srv.saved_config()["mappings"], "nothing re-created by the stale form"
        shown = browser_post(again)
        assert "id" not in dict(shown) and "rev" not in dict(shown), shown
        assert post_form(b, "/api/mappings/save", shown)[0] == 200
        (readded,) = srv.saved_config()["mappings"]
        assert readded["name"] == "PLC-1" and readded["id"] != mid, readded["id"]
        print("deleted while its form was open: shown as a new mapping, saved under a new "
              "id  OK")

        # REST API: ETag / If-Match
        mid = readded["id"]
        st, h, view = api(srv, "GET", f"/api/v1/mappings/{mid}")
        etag = h["ETag"]
        assert st == 200 and re.fullmatch(r'"[0-9a-f]{24}"', etag), etag
        body = {k: v for k, v in view.items() if k != "status"}
        st, h, _ = api(srv, "PUT", f"/api/v1/mappings/{mid}", dict(body, name="via API"),
                       {"If-Match": etag})
        assert st == 200 and h["ETag"] != etag, (st, h["ETag"])
        st, _, err = api(srv, "PUT", f"/api/v1/mappings/{mid}", dict(body, name="stale"),
                         {"If-Match": etag})
        assert st == 412 and err["error"] == "precondition_failed", (st, err)
        assert stored_name(srv, mid) == "via API"
        for value in ('W/' + etag, '"0000"'):   # weak tags never match; another revision
            assert api(srv, "PUT", f"/api/v1/mappings/{mid}", body, {"If-Match": value})[0] == 412
        assert api(srv, "PUT", f"/api/v1/mappings/{mid}", dict(body, name="any"),
                   {"If-Match": "*"})[0] == 200
        assert api(srv, "PUT", f"/api/v1/mappings/{mid}", dict(body, name="no header"))[0] == 200
        assert api(srv, "DELETE", f"/api/v1/mappings/{mid}", headers={"If-Match": etag})[0] == 412
        _st, h, _ = api(srv, "GET", f"/api/v1/mappings/{mid}")
        assert api(srv, "DELETE", f"/api/v1/mappings/{mid}",
                   headers={"If-Match": h["ETag"]})[0] == 200
        assert not srv.saved_config()["mappings"]
        print("REST: ETag on GET/PUT; If-Match (strong) guards PUT and DELETE (412); "
              "without it as before  OK")
    print("\nPASS: edits made for an older revision of a mapping are not saved silently")


def stored_name(srv, mid):
    return next(m["name"] for m in srv.saved_config()["mappings"] if m["id"] == mid)


if __name__ == "__main__":
    main()
