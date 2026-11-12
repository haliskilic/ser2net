"""REST API token roles: a 'viewer' token is read-only (Phase 2.3).

The API bearer token now carries a role; viewer tokens may only GET, while
operator/admin tokens may also create/modify. Spawns the real server. Also:
  * api_token_role in config.json: no key -> admin (tokens from before roles), a role
    in other case / with spaces -> that role, anything else -> viewer (never more
    access) with a start-up log note; the config stays valid;
  * Settings refuses a role that isn't one instead of making the token admin;
  * the new token is shown once, in a box one click selects, with a Copy button;
  * "Change the role only" gives the token another role and keeps it (audited); there
    is nothing to change without a token; Regenerate (with a token) and Revoke ask first.
Run:
    python3 tests/test_api_token_role.py
"""
import copy
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from _server import Browser, Server, free_port, server_log, show_log_on_failure, ui_cookie

from app.config import AppConfig
from app.web.auth import hash_password, hash_token

ROOT = os.path.join(os.path.dirname(__file__), "..")
UI = free_port()
BASE = f"http://127.0.0.1:{UI}"


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def api(method, path, token, body=None, expect=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        code = urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        code = e.code
    if expect is not None:
        assert code == expect, f"{method} {path} -> {code} (wanted {expect})"
    return code


def mapping(port, serial):
    return {"name": f"m{port}", "kind": "net", "serial": {"port": serial, "baudrate": 9600},
            "network": {"mode": "server", "bind_ip": "127.0.0.1", "port": port}}


def test_role_on_load():
    token = {"api_token_hash": hash_token("s2n_t")}
    cases = [({}, "admin"), ({"api_token_role": "operator"}, "operator"),
             ({"api_token_role": "Viewer"}, "viewer"), ({"api_token_role": " OPERATOR "}, "operator"),
             ({"api_token_role": "Admin"}, "admin")]
    for extra, want in cases:
        cfg = AppConfig.from_dict({**token, **extra})
        assert cfg.api_token_role == want and not cfg.load_notes, (extra, cfg.api_token_role, cfg.load_notes)
    for raw in ("read-only", "readonly", "ro", "root", "", None, 3, ["admin"]):
        cfg = AppConfig.from_dict({**token, "api_token_role": raw})
        cfg.validate()  # still a valid config: Docker re-saves it at every start
        assert cfg.api_token_role == "viewer", (raw, cfg.api_token_role)
        assert cfg.to_dict()["api_token_role"] == "viewer"
        assert [n for n in cfg.load_notes if f"api_token_role {raw!r} is not one of" in n
                and "read-only (viewer)" in n], (raw, cfg.load_notes)
    # without a token the role does nothing (generating one picks it): no note
    cfg = AppConfig.from_dict({"api_token_role": "read-only"})
    assert cfg.api_token_role == "viewer" and not cfg.load_notes, cfg.load_notes
    print("config: missing role -> admin, case/space variants kept, anything else -> viewer + note  OK")


def test_unknown_role_in_config():
    secret = "s2n_hand-edited-token"
    config = {"users": [{"username": "admin", "password_hash": hash_password("tokenpass1"),
                         "role": "admin", "pwd_version": 1}],
              "api_token_hash": hash_token(secret), "api_token_role": "read-only",
              "mappings": [dict(mapping(free_port(), "/dev/ser2net-token-role"),
                                mqtt={"enabled": False, "password": "broker-secret"})]}
    with Server(copy.deepcopy(config)) as srv:
        base = srv.base

        def call(method, path, token, body=None):
            req = urllib.request.Request(base + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                r = e
            return r.status if hasattr(r, "status") else r.code, json.loads(r.read() or b"null")

        assert "api_token_role 'read-only' is not one of admin, operator, viewer" in srv.output()
        assert srv.saved_config()["api_token_role"] == "viewer"  # re-saved at start (Docker way)
        st, doc = call("GET", "/api/v1/mappings", secret)
        assert st == 200 and doc["mappings"][0]["mqtt"]["password"] == "********", doc
        st, doc = call("POST", "/api/v1/mappings", secret, mapping(free_port(), "/dev/ttyNOPE"))
        assert st == 403 and doc["error"] == "forbidden", (st, doc)
        st, _ = call("DELETE", f"/api/v1/mappings/{srv.saved_config()['mappings'][0]['id']}", secret)
        assert st == 403 and len(srv.saved_config()["mappings"]) == 1
        print("config: an unknown api_token_role boots as a read-only token, logged  OK")

        admin = Browser(base)
        admin.login("admin", "tokenpass1")
        for bad in ("superuser", "", "read-only"):
            st, _, page = admin.post("/settings/api-token", {"api_token_role": bad})
            assert st == 400 and "Invalid role." in page and "s2n_" not in page, (bad, st)
        saved = srv.saved_config()
        assert (saved["api_token_hash"], saved["api_token_role"]) == (hash_token(secret), "viewer")
        assert call("GET", "/api/v1/status", secret)[0] == 200, "a refused form must keep the token"
        st, _, page = admin.post("/settings/api-token", {"api_token_role": "Operator"})
        new = re.search(r"s2n_[A-Za-z0-9_\-]+", page).group(0)
        # shown once: in a box a click selects whole (.token-box), with a Copy button for it
        assert f'<div class="mono token-box">{new}</div>\n    <button type="button" class="btn sm mt-sm" data-copy>' in page
        assert srv.saved_config()["api_token_role"] == "operator" and new != secret
        assert call("GET", "/api/v1/status", secret)[0] == 401, "the old token must stop working"
        assert call("GET", "/api/v1/mappings", new)[1]["mappings"][0]["mqtt"]["password"] == "broker-secret"
        # a form without the field (written before token roles existed) still means admin
        st, _, page = admin.post("/settings/api-token", {})
        assert st == 200 and srv.saved_config()["api_token_role"] == "admin"
        print("settings: a role that isn't one is refused (token kept); 'Operator' -> operator  OK")

        # the role alone can change: the same token goes on working, with the new role
        new = re.search(r"s2n_[A-Za-z0-9_\-]+", page).group(0)
        assert 'formaction="/settings/api-token/role"' in page, "a 'Change the role only' button"
        before = srv.saved_config()["api_token_hash"]
        st, _, page = admin.post("/settings/api-token/role", {"api_token_role": "viewer"})
        assert st == 200 and "the token stays the same" in page and "s2n_" not in page, page[:200]
        saved = srv.saved_config()
        assert (saved["api_token_hash"], saved["api_token_role"]) == (before, "viewer")
        assert call("GET", "/api/v1/status", new)[0] == 200, "the token still works"
        assert call("POST", "/api/v1/mappings", new, mapping(free_port(), "/dev/ttyNOPE"))[0] == 403
        assert admin.post("/settings/api-token/role", {"api_token_role": "root"})[2].count("Invalid role.")
        st, _, page = admin.get("/settings/audit?action=api_token_role")
        assert ">1 record</p>" in page and "&gt; viewer</td>" in page, "audited: from - to"
        # the questions: replacing and revoking the token ask first, changing its role not
        st, _, page = admin.get("/settings")
        regen = page[page.index('action="/settings/api-token"'):page.index('action="/settings/api-token/revoke"')]
        assert regen.count("data-confirm=") == 1 and "gets 401 until it is given the new one" in regen
        revoke = page[page.index('action="/settings/api-token/revoke"'):][:300]
        assert "data-confirm=" in revoke, revoke
        st, _, page = admin.post("/settings/api-token/revoke", {})
        assert call("GET", "/api/v1/status", new)[0] == 401
        st, _, page = admin.post("/settings/api-token/role", {"api_token_role": "admin"})
        assert "There is no REST API token to give a role" in page and \
            srv.saved_config()["api_token_role"] == "viewer", "no token: nothing to change"
        st, _, page = admin.get("/settings")
        form = page[page.index('action="/settings/api-token"'):][:900]
        assert "data-confirm=" not in form.split("</form>")[0], "generating a first token asks nothing"
        print("settings: the role alone changes (same token); replace / revoke ask first  OK")


def main():
    test_role_on_load()
    test_unknown_role_in_config()
    tmp = tempfile.mkdtemp(prefix="ser2net_tok_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}}, fh)
    out = server_log(tmp)  # the server's output: a file, never an unread pipe
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=out, stderr=subprocess.STDOUT)
    out.close()  # (the server has its own copy)
    try:
        assert wait_port(UI), "server did not start"
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def csrf():
            return next((c.value for c in jar if c.name == ui_cookie("ser2net_csrf", UI)), "")

        def post_form(path, data):
            req = urllib.request.Request(BASE + path,
                                         data=urllib.parse.urlencode(data).encode(), method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("X-CSRF-Token", csrf())
            return op.open(req, timeout=10)

        op.open(BASE + "/setup")
        post_form("/setup", {"password": "supersecret1", "password2": "supersecret1", "_csrf": csrf()})

        def gen_token(role):
            body = post_form("/settings/api-token", {"api_token_role": role, "_csrf": csrf()}).read().decode()
            return re.search(r"s2n_[A-Za-z0-9_\-]+", body).group(0)

        # viewer token: reads OK, writes forbidden
        vtok = gen_token("viewer")
        api("GET", "/api/v1/mappings", vtok, expect=200)
        api("GET", "/api/v1/status", vtok, expect=200)
        api("POST", "/api/v1/mappings", vtok, body=mapping(free_port(), "/dev/ttyV"), expect=403)
        print("viewer token: GET allowed, POST -> 403 (read-only)  OK")

        # operator token: writes allowed
        otok = gen_token("operator")
        api("GET", "/api/v1/mappings", otok, expect=200)
        api("POST", "/api/v1/mappings", otok, body=mapping(free_port(), "/dev/ttyO"), expect=201)
        print("operator token: GET + POST allowed (read-write)  OK")

        # stored secrets: operator sees the MQTT password (GET -> PUT round trip), a
        # viewer token gets it redacted
        with_secret = dict(mapping(free_port(), "/dev/ttyS"),
                           mqtt={"enabled": False, "password": "broker-secret"})
        api("POST", "/api/v1/mappings", otok, body=with_secret, expect=201)

        def mqtt_passwords(token):
            req = urllib.request.Request(BASE + "/api/v1/mappings",
                                         headers={"Authorization": f"Bearer {token}"})
            doc = json.loads(urllib.request.urlopen(req, timeout=10).read())
            return {m["name"]: m["mqtt"]["password"] for m in doc["mappings"]}

        assert mqtt_passwords(otok)[with_secret["name"]] == "broker-secret"
        vtok = gen_token("viewer")
        seen = mqtt_passwords(vtok)
        assert seen[with_secret["name"]] == "********", seen
        assert "broker-secret" not in json.dumps(seen)
        print("viewer token: stored MQTT password redacted; operator token sees it  OK")

        print("\nPASS: REST API token roles")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
        show_log_on_failure(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
