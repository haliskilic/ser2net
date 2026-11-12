"""Actions that disconnect clients, or that can't be undone, ask first — only those.

  * a row's Stop / Restart (this node's table and the fleet view's peers) ask only while
    clients are connected, with how many will be disconnected (a question at every click
    is soon clicked through); Start never asks;
  * deleting a mapping says its log and trace files go too (no backup keeps them);
  * Settings: deleting a local account asks (its password is gone with it), a directory
    account (LDAP / OIDC, back at its next sign-in) does not; generating a self-signed
    certificate asks while a certificate is set; the questions are in the page's
    language; app.js asks a submit button's own question (data-confirm on the button),
    else the form's.

The bulk actions' question is built by app.js (test_ui_layout.py checks it in Chromium);
regenerating / revoking the API token: test_api_token_role.py; a second certificate
generation: test_tls_selfsigned.py.

Run: python3 tests/test_confirmations.py
"""
import json
import os
import re

from _server import ROOT, Browser, Server

from app.config import MappingConfig
from app.web import selfsigned
from app.web.auth import hash_password
from app.web.server import make_templates

PW = "confirm-pass-1"
with open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8") as _fh:
    TR = json.load(_fh)


def confirms(markup, button_id):
    m = re.search(r'<button[^>]*\bid="%s"[^>]*>' % re.escape(button_id), markup)
    assert m, f"no button {button_id}"
    q = re.search(r'hx-confirm="([^"]*)"', m.group(0))
    return q and q.group(1)


def test_rows():
    tpl = make_templates().env.get_template("_mappings_body.html")
    maps = [MappingConfig.from_dict({"id": i, "name": f"Map {i}", "enabled": on,
                                     "serial": {"port": f"/dev/{i}"}, "network": {"port": 4000 + n}})
            for n, (i, on) in enumerate((("busy", True), ("idle", True), ("off", False)))]
    out = tpl.render(mappings=maps, can_edit=True,
                     statuses={"busy": {"state": "running", "client_count": 2},
                               "idle": {"state": "running", "client_count": 0}})
    q = confirms(out, "m-busy-run")
    assert q and "Stop “Map busy”" in q and "2 connected clients will" in q and "stays stopped" in q, q
    assert "2 connected clients will" in confirms(out, "m-busy-restart")
    assert confirms(out, "m-idle-run") is None and confirms(out, "m-idle-restart") is None, \
        "no clients: no question"
    assert confirms(out, "m-off-run") is None, "Start never asks"
    assert "Its log and trace files are deleted too" in confirms(out, "m-idle-delete")

    peer = {"name": "peer", "self": False, "source": "auto", "ip": "10.0.0.2", "port": 8080,
            "addr": "10.0.0.2:8080", "online": True, "caps": ["control", "edit"],
            "mappings": [{"id": "p1", "name": "Remote", "enabled": True, "client_count": 1},
                         {"id": "p2", "name": "Quiet", "enabled": True, "client_count": 0},
                         {"id": "p3", "name": "Odd", "enabled": True, "client_count": "lots"}]}
    out = make_templates().env.get_template("_cluster_body.html").render(can_edit=True, nodes=[peer])
    assert "1 connected client will" in confirms(out, "c-10_0_0_2_8080-p1-run")
    assert "1 connected client will" in confirms(out, "c-10_0_0_2_8080-p1-restart")
    assert confirms(out, "c-10_0_0_2_8080-p2-run") is None
    assert confirms(out, "c-10_0_0_2_8080-p3-run") is None, "a peer's odd count is no number"
    print("rows and peers: Stop / Restart ask only with clients connected; delete names log + traces  OK")


def test_settings():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1},
             {"username": "local-op", "password_hash": hash_password(PW), "role": "operator",
              "pwd_version": 1, "source": "local"},
             {"username": "dir-user", "password_hash": "", "role": "viewer", "pwd_version": 1,
              "source": "ldap"}]
    with Server({"users": users}) as srv:
        b = Browser(srv.base)
        b.op.addheaders = [("Accept-Language", "tr")]
        b.login("admin", PW)
        page = b.get("/settings")[2]
        gen = re.search(r'<form method="post" action="/settings/tls/generate"[^>]*>', page).group(0)
        assert "data-confirm" not in gen, "no certificate set: nothing to replace"
        # a certificate of its own (used from the next start; a pair that loads: an edit
        # test-loads it)
        cert, key = os.path.join(srv.data, "ours.pem"), os.path.join(srv.data, "ours.key")
        try:
            selfsigned.generate(cert, key)
        except selfsigned.CertError:
            cert = None
            print("SKIP (part): neither cryptography nor openssl to make a certificate")
        if cert:
            st, _, page = b.post("/settings/tls", {"tls_cert": cert, "tls_key": key})
            assert srv.saved_config()["admin_ui"]["tls_cert"] == cert, \
                page[page.find("banner"):][:200]
        st, _, page = b.get("/settings")
        assert st == 200

        def delete_form(user):
            for m in re.finditer(r'<form method="post" action="/settings/users/delete"[^>]*>(.*?)</form>',
                                 page, re.S):
                if f'value="{user}"' in m.group(1):
                    return m.group(0)
            raise AssertionError(f"no delete form for {user}")
        want = TR["Delete the user “%(user)s”? The password goes with it: only restoring a "
                  "configuration backup brings it back."] % {"user": "local-op"}
        assert f'data-confirm="{want}"' in delete_form("local-op"), delete_form("local-op")[:300]
        assert "data-confirm" not in delete_form("dir-user"), "a directory account comes back"
        gen = re.search(r'<form method="post" action="/settings/tls/generate"[^>]*>', page).group(0)
        assert not cert or ("data-confirm=" in gen and "ours.pem" in gen), gen
    js = open(os.path.join(ROOT, "app", "web", "static", "app.js"), encoding="utf-8").read()
    assert 'e.submitter' in js and '(b && b.getAttribute("data-confirm"))' in js
    print("settings: local-account delete and replacing a set certificate ask (in Turkish)  OK")


def main():
    test_rows()
    test_settings()
    print("\nPASS: confirmations for disruptive actions")


if __name__ == "__main__":
    main()
