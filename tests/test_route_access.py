"""Every web route and WebSocket the app registers, checked against one access matrix.

ROUTES classifies each (method, path) — who may use it, and what refuses a request
made from another site:

  access   public       no session needed (sign-in pages, health, UI language, static files)
           setup        first-run setup; once an account exists the gate sends it to /
           viewer       any session, open-access visitors included
           account      a signed-in account (an open-access visitor is refused by the handler)
           operator     operator or admin (an open-access visitor with that role too)
           peer         a signed-in operator or admin: acts on other cluster nodes
           admin        admin only
           cluster-key  other cluster nodes, with the shared cluster key; never a session
           token-public, token-read, token-write
                        the REST API: no token / any token / an operator or admin token;
                        never a session cookie
  csrf     form    the _csrf form field, checked by the handler
           header  the X-CSRF-Token header, checked by GuardMiddleware for /api/
           origin  a WebSocket: a browser's Origin must be this server
           none    nothing to forge (GET, or authenticated by a key or token)

The test fails when
  * the app registers a route ROUTES doesn't classify, or ROUTES names one that is gone
    (like test_i18n with a string) — a new route has to be given its access class;
  * the public path sets of the session gate change, or don't match the matrix;
  * a running server answers a role differently from the matrix. Every request a role
    may not make must be refused for the expected reason — sign in first (303 /login or
    401), "requires the X role", a peer action from an open-access visitor, no or a wrong
    cluster key, no or a read-only API token, a cross-site WebSocket — and a role that may
    use a route must get past the access checks: a form POST without its CSRF token is
    then refused for exactly that, a GET answers whatever its handler answers.
Probes name mappings, users and files that don't exist (or post empty forms), and the
forms a role may post go without their CSRF token, so a missing check can't change the
server.

Also checked here: who sees what in the console and the hex view (the to-device
direction is for operators), see test_traffic_feed.

Run: python3 tests/test_route_access.py
"""
from __future__ import annotations

import http.client
import http.cookiejar
import json
import re
import types
import urllib.parse
import urllib.request

from _server import Browser, Server, _NoRedirect, free_port

from app.web import server as web_server
from app.web.auth import hash_password, hash_token

PUBLIC, SETUP, VIEWER, ACCOUNT = "public", "setup", "viewer", "account"
OPERATOR, PEER, ADMIN = "operator", "peer", "admin"
CLUSTER_KEY = "cluster-key"
TOKEN_PUBLIC, TOKEN_READ, TOKEN_WRITE = "token-public", "token-read", "token-write"
FORM, HEADER, ORIGIN, NONE = "form", "header", "origin", "none"

ROUTES = {
    # ---- routes/login.py ----
    ("GET", "/login"): (PUBLIC, NONE),
    ("POST", "/login"): (PUBLIC, FORM),
    ("POST", "/logout"): (VIEWER, FORM),
    ("GET", "/auth/oidc/login"): (PUBLIC, NONE),
    ("GET", "/auth/oidc/callback"): (PUBLIC, NONE),
    ("GET", "/setup"): (SETUP, NONE),
    ("POST", "/setup"): (SETUP, FORM),
    # ---- routes/settings.py ----
    ("GET", "/settings"): (VIEWER, NONE),  # the admin sections only for admins
    ("POST", "/settings/password"): (ACCOUNT, FORM),
    ("POST", "/settings/sessions/signout-others"): (ACCOUNT, FORM),
    ("POST", "/settings/tls"): (ADMIN, FORM),
    ("POST", "/settings/tls/generate"): (ADMIN, FORM),
    ("POST", "/settings/access"): (ADMIN, FORM),
    ("POST", "/settings/sessions/revoke-all"): (ADMIN, FORM),
    ("POST", "/settings/api-token"): (ADMIN, FORM),
    ("POST", "/settings/api-token/revoke"): (ADMIN, FORM),
    ("POST", "/settings/users"): (ADMIN, FORM),
    ("POST", "/settings/users/password"): (ADMIN, FORM),
    ("POST", "/settings/users/role"): (ADMIN, FORM),  # the user name in a form field
    ("POST", "/settings/users/delete"): (ADMIN, FORM),
    ("POST", "/settings/users/signout"): (ADMIN, FORM),
    ("POST", "/settings/users/expiry"): (ADMIN, FORM),
    ("POST", "/settings/users/{username}/role"): (ADMIN, FORM),  # the same, by path
    ("POST", "/settings/users/{username}/delete"): (ADMIN, FORM),
    ("POST", "/settings/users/{username}/signout"): (ADMIN, FORM),
    ("POST", "/settings/ldap"): (ADMIN, FORM),
    ("POST", "/settings/oidc"): (ADMIN, FORM),
    ("POST", "/settings/cluster"): (ADMIN, FORM),
    ("POST", "/settings/cluster/identity"): (ADMIN, FORM),
    ("GET", "/settings/config/export"): (OPERATOR, NONE),  # mappings carry the MQTT password
    ("POST", "/settings/config/import"): (OPERATOR, FORM),
    ("POST", "/settings/config/import/apply"): (OPERATOR, FORM),
    ("POST", "/settings/notifications"): (ADMIN, FORM),
    ("GET", "/settings/audit"): (ADMIN, NONE),
    ("GET", "/settings/audit.csv"): (ADMIN, NONE),
    ("POST", "/settings/backups/restore"): (ADMIN, FORM),
    ("POST", "/settings/backups/restore-mappings"): (ADMIN, FORM),
    ("GET", "/settings/backups/{name}"): (ADMIN, NONE),
    # ---- api.py: the REST API (bearer token, checked in GuardMiddleware) ----
    ("GET", "/api/v1/health"): (TOKEN_PUBLIC, NONE),
    ("GET", "/api/v1/openapi.json"): (TOKEN_PUBLIC, NONE),
    ("GET", "/api/v1/status"): (TOKEN_READ, NONE),
    ("GET", "/api/v1/metrics"): (TOKEN_READ, NONE),
    ("POST", "/api/v1/import"): (TOKEN_WRITE, NONE),
    ("GET", "/api/v1/ports"): (TOKEN_READ, NONE),
    ("GET", "/api/v1/mappings"): (TOKEN_READ, NONE),
    ("POST", "/api/v1/mappings"): (TOKEN_WRITE, NONE),
    ("GET", "/api/v1/mappings/{mid}"): (TOKEN_READ, NONE),
    ("PUT", "/api/v1/mappings/{mid}"): (TOKEN_WRITE, NONE),
    ("PATCH", "/api/v1/mappings/{mid}"): (TOKEN_WRITE, NONE),
    ("DELETE", "/api/v1/mappings/{mid}"): (TOKEN_WRITE, NONE),
    ("POST", "/api/v1/mappings/{mid}/{action}"): (TOKEN_WRITE, NONE),
    # ---- routes/mappings.py ----
    ("GET", "/"): (VIEWER, NONE),
    ("POST", "/api/mappings/{mid}/duplicate"): (OPERATOR, HEADER),
    ("GET", "/api/status"): (VIEWER, NONE),
    ("POST", "/api/mappings/bulk"): (OPERATOR, HEADER),
    ("GET", "/api/mappings/form"): (OPERATOR, NONE),  # the form renders stored secrets
    ("GET", "/api/mappings/{mid}/form"): (OPERATOR, NONE),
    ("GET", "/api/mappings/{mid}/log"): (VIEWER, NONE),
    ("GET", "/api/mappings/{mid}/console-view"): (VIEWER, NONE),
    ("GET", "/api/mappings/{mid}/traces"): (OPERATOR, NONE),  # traces hold what is sent to the device
    ("GET", "/api/mappings/{mid}/traces/{name}"): (OPERATOR, NONE),
    ("POST", "/api/mappings/save"): (OPERATOR, HEADER),
    ("DELETE", "/api/mappings/{mid}"): (OPERATOR, HEADER),
    ("POST", "/api/mappings/{mid}/{action}"): (OPERATOR, HEADER),
    # ---- routes/console.py (the to-device direction: see test_traffic_feed) ----
    ("WS", "/api/mappings/{mid}/console"): (VIEWER, ORIGIN),
    ("WS", "/api/mappings/{mid}/traffic"): (VIEWER, ORIGIN),
    # ---- routes/cluster.py ----
    ("GET", "/api/cluster/status"): (VIEWER, NONE),  # open-access visitors: this node only
    ("GET", "/api/cluster/local"): (CLUSTER_KEY, NONE),
    ("POST", "/api/cluster/control"): (CLUSTER_KEY, NONE),
    ("POST", "/api/cluster/control-peer"): (PEER, HEADER),
    ("GET", "/api/cluster/mapping-data"): (CLUSTER_KEY, NONE),
    ("POST", "/api/cluster/mapping-save"): (CLUSTER_KEY, NONE),
    ("GET", "/api/cluster/peer-form"): (PEER, NONE),
    ("POST", "/api/cluster/peer-save"): (PEER, HEADER),
    # ---- routes/system.py ----
    ("GET", "/lang/{code}"): (PUBLIC, NONE),
    ("GET", "/healthz"): (PUBLIC, NONE),
    ("GET", "/metrics"): (VIEWER, NONE),
    ("GET", "/api/ports.json"): (VIEWER, NONE),
    ("POST", "/api/ports/refresh"): (VIEWER, HEADER),
    ("GET", "/api/ports/table"): (VIEWER, NONE),
    ("MOUNT", "/static"): (PUBLIC, NONE),
}

# what the handler of an "account" route answers an open-access visitor (200, no change)
ACCOUNT_REFUSALS = {
    "/settings/password": "Sign in to change your password.",
    "/settings/sessions/signout-others": "Sign in to manage your sessions.",
}

# the session gate's public paths, pinned: widening them opens routes without a session
PUBLIC_PREFIXES = ("/static", "/auth/", "/lang/")
CLUSTER_PEER_PATHS = {"/api/cluster/local", "/api/cluster/control",
                      "/api/cluster/mapping-data", "/api/cluster/mapping-save"}
PUBLIC_PATHS = {"/login", "/setup", "/healthz", "/favicon.ico"} | CLUSTER_PEER_PATHS

RANK = {VIEWER: 1, ACCOUNT: 1, OPERATOR: 2, PEER: 2, ADMIN: 3}
SESSION_CLASSES = set(RANK)
WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")

ROLE_DENIED = "Forbidden: requires the {0} role."
CSRF_DENIED = "CSRF validation failed. Reload the page."
PEER_DENIED = "Sign in to manage other cluster nodes."
DENIALS = {ROLE_DENIED.format("operator"), ROLE_DENIED.format("admin"), CSRF_DENIED, PEER_DENIED}


def registered_routes(app) -> set:
    """(method, path) of every route of `app`: HTTP methods (HEAD comes with GET),
    "WS" for a WebSocket, "MOUNT" for a mounted app (a mounted router's own routes)."""
    out: set = set()

    def walk(routes, prefix: str) -> None:
        from starlette.routing import Mount, Route, WebSocketRoute

        for r in routes:
            if isinstance(r, Route):
                for method in sorted(r.methods - {"HEAD"}):
                    key = (method, prefix + r.path)
                    assert key not in out, f"{key} is registered twice: the second one never runs"
                    out.add(key)
            elif isinstance(r, WebSocketRoute):
                out.add(("WS", prefix + r.path))
            elif isinstance(r, Mount) and r.routes:
                walk(r.routes, prefix + r.path)
            elif isinstance(r, Mount):
                out.add(("MOUNT", prefix + r.path))
            else:
                raise AssertionError(f"a route of an unknown kind: {r!r}")

    walk(app.routes, "")
    return out


def gate_public(path: str) -> bool:
    """Does GuardMiddleware let `path` through without a session? (its own condition)"""
    return path in web_server.PUBLIC_PATHS or any(path.startswith(p) for p in web_server.PUBLIC_PREFIXES)


def test_inventory():
    app = web_server.build_app(types.SimpleNamespace())  # building the routes reads no state
    routes = registered_routes(app)
    unclassified = sorted(routes - set(ROUTES))
    stale = sorted(set(ROUTES) - routes)
    assert not unclassified, ("routes without an access class; add them to ROUTES in "
                              "tests/test_route_access.py:\n  " + "\n  ".join(map(str, unclassified)))
    assert not stale, "ROUTES names routes the app no longer has:\n  " + "\n  ".join(map(str, stale))
    assert web_server.PUBLIC_PREFIXES == PUBLIC_PREFIXES, web_server.PUBLIC_PREFIXES
    assert web_server._CLUSTER_PEER_PATHS == CLUSTER_PEER_PATHS, web_server._CLUSTER_PEER_PATHS
    assert web_server.PUBLIC_PATHS == PUBLIC_PATHS, web_server.PUBLIC_PATHS
    assert set(ACCOUNT_REFUSALS) == {p for (_m, p), (a, _c) in ROUTES.items() if a == ACCOUNT}

    for (method, path), (access, csrf) in ROUTES.items():
        where = f"{method} {path} ({access}, {csrf})"
        if method == "WS":  # the middleware doesn't run for WebSockets: the handler checks
            assert access in SESSION_CLASSES and csrf == ORIGIN, where
            continue
        if path.startswith("/api/v1"):  # token auth in the middleware, no cookies, no CSRF
            assert access in (TOKEN_PUBLIC, TOKEN_READ, TOKEN_WRITE) and csrf == NONE, where
            assert (access == TOKEN_WRITE) == (method in WRITE_METHODS), where
            continue
        assert access not in (TOKEN_PUBLIC, TOKEN_READ, TOKEN_WRITE), where
        assert gate_public(path) == (access in (PUBLIC, SETUP, CLUSTER_KEY)), \
            f"{where}: the session gate {'skips' if gate_public(path) else 'guards'} it"
        assert (path in CLUSTER_PEER_PATHS) == (access == CLUSTER_KEY), where
        if access == CLUSTER_KEY:
            assert csrf == NONE, where  # no cookie from another node: the key is the check
        elif method in WRITE_METHODS and path.startswith("/api/"):
            assert csrf == HEADER, f"{where}: GuardMiddleware checks X-CSRF-Token here"
        elif method in WRITE_METHODS:
            assert csrf == FORM, f"{where}: a form post outside /api/ must check _csrf"
        else:
            assert csrf == NONE, where
    print(f"inventory: all {len(routes)} routes and WebSockets classified; public path sets "
          "pinned and consistent with the matrix  OK")


def test_traffic_feed():
    """Who sees what in the console and the hex view (routes/common.py, console.py)."""
    from app.config import MappingConfig, User
    from app.engine.bridge import FROM_DEVICE, TO_DEVICE
    from app.web.routes.common import console_interactive, sees_to_device
    from app.web.routes.console import _TrafficMonitor

    net = MappingConfig.from_dict({"name": "net", "kind": "net"})
    read_only = MappingConfig.from_dict({"name": "ro", "kind": "net", "network": {"read_only": True}})
    bridge = MappingConfig.from_dict({"name": "pair", "kind": "serialbridge"})
    viewer, operator, admin = (User(username=r, role=r) for r in ("viewer", "operator", "admin"))
    visitors = [User(username="(no login)", role=r, source="anonymous") for r in ("viewer", "operator")]
    for u in (viewer, visitors[0]):
        assert not sees_to_device(u, net) and not console_interactive(u, net), u
        assert sees_to_device(u, bridge), "a serial↔serial bridge: devices on both ports"
    for u in (operator, admin, visitors[1]):
        assert sees_to_device(u, net) and console_interactive(u, net), u
        assert not console_interactive(u, read_only) and not console_interactive(u, bridge), u

    def fed(mon):
        mon.feed_traffic(TO_DEVICE, b"typed-secret")
        mon.feed_traffic(FROM_DEVICE, b"Password: ")
        return [mon.queue.get_nowait()[::2] for _ in range(mon.queue.qsize())]

    assert fed(_TrafficMonitor(to_device=False)) == [(FROM_DEVICE, b"Password: ")]
    assert fed(_TrafficMonitor(to_device=True)) == [(TO_DEVICE, b"typed-secret"),
                                                    (FROM_DEVICE, b"Password: ")]
    print("hex view feed: what is sent to the device only for operators/admins (and both "
          "ports of a serial bridge); typing only for operators/admins  OK")


# ---------------------------------------------------------------- the live matrix
PW = "route-pass-1"
MID = "aaaaaaaaaaa1"   # an existing mapping: the WebSockets need one
VIEWER_TOKEN = "s2n_route-access-viewer-token"
CLUSTER_KEY_VALUE = "route-access-cluster-key"
PARAMS = {"mid": "nosuchmapping", "name": "no-such-file", "username": "no-such-user",
          "action": "start", "code": "en"}


def probe_path(method: str, path: str) -> str:
    if method == "MOUNT":
        return path + "/app.css"
    return re.sub(r"\{(\w+)\}", lambda m: PARAMS[m.group(1)], path)


class _OneWriteConnection(http.client.HTTPConnection):
    """Sends the request head and a body in one write, as a browser sends a small form.

    Many probes are refused from the headers alone (no session, no token, no CSRF header),
    and the server then closes the connection, as urllib asks it to. A body sent in a
    second write can arrive after that close; Windows then resets the connection and the
    client loses the answer it hasn't read yet."""

    def _send_output(self, message_body=None, encode_chunked=False):
        if isinstance(message_body, bytes) and not encode_chunked:
            self._buffer.extend((b"", message_body))
            msg = b"\r\n".join(self._buffer)
            del self._buffer[:]
            self.send(msg)
            return
        super()._send_output(message_body, encode_chunked)


class _OneWriteHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_OneWriteConnection, req)


class Client(Browser):
    """A browser (cookies, no redirects) that can leave out the CSRF token."""

    def __init__(self, base: str, name: str, rank: int = 0, account: bool = False):
        super().__init__(base)
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                              _NoRedirect(), _OneWriteHandler())
        self.name, self.rank, self.account = name, rank, account

    def send(self, method: str, path: str, csrf: bool = True, fields=None, headers=None,
             body: bytes | None = None):
        fields, headers = dict(fields or {}), dict(headers or {})
        if csrf:
            fields["_csrf"] = headers["X-CSRF-Token"] = self.csrf()
        if body is None and method != "GET":
            body = urllib.parse.urlencode(fields).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        st, h, raw = self._open(urllib.request.Request(self.base + path, data=body,
                                                       headers=headers, method=method))
        return st, h, raw.decode("utf-8", "replace")


def gated(path, st, h, body) -> bool:
    """The session gate: sign in first."""
    if path.startswith("/api/"):
        return st == 401 and json.loads(body) == {"error": "unauthorized"}
    return st == 303 and h.get("Location") == "/login"


def passed(st, h, body) -> bool:
    """Got past every access check (whatever the handler answers then)."""
    return not (st == 401 or (st == 303 and h.get("Location") == "/login")
                or (st == 403 and body.strip() in DENIALS) or st >= 500)


def expectation(access: str, who: Client) -> str:
    if who.rank == 0:
        return "gate"
    if access == PEER and not who.account:
        return "peer"
    if who.rank < RANK[access]:
        return "operator" if RANK[access] == 2 else "admin"
    if access == ACCOUNT and not who.account:
        return "account"
    return "allowed"


def check_session_route(who: Client, method: str, path: str, access: str, csrf: str) -> None:
    probe = probe_path(method, path)
    want = expectation(access, who)
    if want == "allowed":
        if csrf in (FORM, HEADER):  # may use it: refused for the missing CSRF token only
            st, h, body = who.send(method, probe, csrf=False)
            ok = st == 403 and body.strip() == CSRF_DENIED
            if ok and csrf == HEADER:  # the middleware's check: now with the token
                st, h, body = who.send(method, probe)
                ok = passed(st, h, body)
        else:
            st, h, body = who.send(method, probe)
            ok = passed(st, h, body)
    else:  # with a valid CSRF token, so only the access rule can refuse it
        st, h, body = who.send(method, probe)
        if want == "gate":
            ok = gated(probe, st, h, body)
        elif want == "peer":
            ok = st == 403 and body.strip() == PEER_DENIED
        elif want == "account":
            ok = st == 200 and ACCOUNT_REFUSALS[path] in body
        else:
            ok = st == 403 and body.strip() == ROLE_DENIED.format(want)
    if want != "allowed":
        should = f"be refused ({want})"
    elif csrf in (FORM, HEADER):
        should = "get past the access checks, and be refused without its CSRF token"
    else:
        should = "get past the access checks"
    assert ok, (f"{who.name}: {method} {probe} ({access}) should {should}; got {st} "
                f"{h.get('Location') or ''} {body[:160]!r}")


def ws_status(base: str, path: str, cookie: str | None = None, origin: str | None = None) -> int:
    """101 when the WebSocket is accepted, else the HTTP status of the refused handshake."""
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    headers = {"Cookie": f"ser2net_session={cookie}"} if cookie else {}
    try:
        with connect("ws" + base[len("http"):] + path, additional_headers=headers,
                     origin=origin, open_timeout=10):
            return 101
    except InvalidStatus as e:
        return e.response.status_code


def session_routes():
    return [(m, p, a, c) for (m, p), (a, c) in ROUTES.items() if a in SESSION_CLASSES and m != "WS"]


def websocket_paths():
    return [p.replace("{mid}", MID) for (m, p) in ROUTES if m == "WS"]


def check_websockets(srv, who: Client, accepted: bool) -> None:
    session = who.cookie("ser2net_session")
    for path in websocket_paths():
        st = ws_status(srv.base, path, session.value if session else None)
        assert (st == 101) == accepted, f"{who.name}: WebSocket {path} -> {st}"


def test_live():
    users = [{"username": u, "password_hash": hash_password(PW), "role": r, "pwd_version": 1}
             for u, r in (("admin", "admin"), ("op", "operator"), ("view", "viewer"))]
    config = {
        "users": users,
        "api_token_hash": hash_token(VIEWER_TOKEN), "api_token_role": "viewer",
        "cluster": {"enabled": True, "key": CLUSTER_KEY_VALUE, "discovery_port": free_port()},
        "mappings": [{"id": MID, "name": "route-access", "enabled": True, "kind": "net",
                      "serial": {"port": "/dev/ser2net-route-access"},
                      "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                                  "port": free_port()}}],
    }
    with Server(config) as srv:
        try:
            _live(srv)
        except BaseException:
            print("--- server log (tail) ---\n" + srv.output()[-3000:])
            raise


def _live(srv) -> None:
    base = srv.base
    anon = Client(base, "no session (login required)")
    admin = Client(base, "admin", 3, True)
    operator = Client(base, "operator", 2, True)
    viewer = Client(base, "viewer", 1, True)
    for c, u in ((admin, "admin"), (operator, "op"), (viewer, "view")):
        c.login(u, PW)
    signed_in = (viewer, operator, admin)
    routes = session_routes()

    # ---- sessions: no session, viewer, operator, admin ----
    for who in (anon, *signed_in):
        for route in routes:
            check_session_route(who, *route)
    forged = Client(base, "a forged session cookie")
    forged.jar.set_cookie(http.cookiejar.Cookie(
        0, "ser2net_session", "eyJ1IjoiYWRtaW4ifQ.forged", None, False, "127.0.0.1", False, False,
        "/", True, False, None, False, None, None, {}))
    assert gated("/", *forged.send("GET", "/"))
    assert gated("/api/status", *forged.send("GET", "/api/status"))
    st, _, body = admin.send("POST", "/api/mappings/save", headers={"X-CSRF-Token": "not-the-cookie"},
                             csrf=False)
    assert st == 403 and body.strip() == CSRF_DENIED, "a CSRF header must match the cookie"
    print(f"sessions: {len(routes)} routes x (no session, viewer, operator, admin) answer as the "
          "matrix says; a form a role may post is refused without its CSRF token  OK")

    # ---- public routes and first-run setup (the gate's side: test_inventory) ----
    for (method, path), (access, csrf) in ROUTES.items():
        if access == PUBLIC:  # a handler may still send to /login itself (SSO switched off)
            st, h, body = anon.send(method, probe_path(method, path), csrf=csrf != FORM)
            ok = ((st == 403 and body.strip() == CSRF_DENIED) if csrf == FORM
                  else (st != 401 and st < 500))
            assert ok, f"public {method} {path} -> {st} {body[:120]!r}"
        elif access == SETUP:
            st, h, _ = anon.send(method, path)
            assert st == 303 and h.get("Location") == "/", f"{method} {path} after setup -> {st}"
    print("public routes answer without a session; setup is closed once an admin exists  OK")

    # ---- cluster-key routes: never by a session, only with the key ----
    for (method, path), (access, _c) in ROUTES.items():
        if access != CLUSTER_KEY:
            continue
        probe = path + ("?mid=" + PARAMS["mid"] if method == "GET" else "")
        for who, headers in ((anon, {}), (anon, {"X-Cluster-Key": "not-the-key"}),
                             (viewer, {}), (admin, {})):
            st, _, body = who.send(method, probe, csrf=False, headers=headers)
            assert st == 403 and body.strip() != CSRF_DENIED, f"{who.name} {method} {path} {headers} -> {st}"
    print("cluster peer routes: refused without the cluster key, sessions don't count  OK")

    # ---- REST API: bearer token only ----
    def api(method, path, token=None, cookie_client=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        body = b"not json" if method in ("POST", "PUT") else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        st, h, text = (cookie_client or anon).send(method, path, csrf=False, headers=headers, body=body)
        return st, text

    def check_token(token, may_write):
        for (method, path), (access, _c) in ROUTES.items():
            if access not in (TOKEN_PUBLIC, TOKEN_READ, TOKEN_WRITE):
                continue
            probe = probe_path(method, path)
            if access == TOKEN_PUBLIC:
                assert api(method, probe)[0] == 200, path
                continue
            for bad, who in ((None, None), ("s2n_wrong", None), (None, admin)):
                st, text = api(method, probe, bad, who)
                assert st == 401 and json.loads(text)["error"] == "unauthorized", \
                    f"{method} {probe} token={bad} cookie={who and who.name} -> {st}"
            st, text = api(method, probe, token)
            if access == TOKEN_WRITE and not may_write:
                assert st == 403 and json.loads(text)["error"] == "forbidden", f"{method} {probe} -> {st}"
            else:  # past the token check: the handler answers (404, 400 for the bad body)
                assert st not in (401, 403) and st < 500, f"{method} {probe} -> {st} {text[:80]}"

    check_token(VIEWER_TOKEN, may_write=False)
    st, _, page = admin.send("POST", "/settings/api-token", fields={"api_token_role": "operator"})
    check_token(re.search(r"s2n_[A-Za-z0-9_\-]+", page).group(0), may_write=True)
    print("REST API: no / a wrong token or a session cookie -> 401, a viewer token can't write, "
          "an operator token can  OK")

    # ---- WebSockets: a session (or open access), same origin ----
    check_websockets(srv, anon, accepted=False)
    for who in signed_in:
        check_websockets(srv, who, accepted=True)
    for path in websocket_paths():
        cookie = admin.cookie("ser2net_session").value
        assert ws_status(base, path, cookie, origin="http://attacker.example") == 403, path
        assert ws_status(base, path, cookie, origin=base) == 101, path
    print("WebSockets: need a session; a page of another site is refused  OK")

    # ---- open access: visitors without a session get a role ----
    for role, rank in (("viewer", 1), ("operator", 2)):
        st, _, page = admin.send("POST", "/settings/access", fields={"anonymous_role": role})
        assert st == 200 and "Open access enabled" in page, st
        visitor = Client(base, f"open-access visitor ({role})", rank, False)
        for route in routes:
            check_session_route(visitor, *route)
        check_websockets(srv, visitor, accepted=True)
    st, _, _ = admin.send("POST", "/settings/access", fields={"require_login": "on",
                                                             "anonymous_role": "viewer"})
    assert st == 200 and srv.saved_config()["admin_ui"]["require_login"] is True
    check_websockets(srv, Client(base, "no session (login required again)"), accepted=False)
    print(f"open access: visitors as viewer and as operator over all {len(routes)} routes; no "
          "peer actions, no account pages, nothing above their role  OK")


def main():
    test_inventory()
    test_traffic_feed()
    test_live()
    print("\nPASS: every route and WebSocket answers as the access matrix says")


if __name__ == "__main__":
    main()
