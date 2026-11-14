"""Starlette application factory: routes + security/auth/CSRF middleware.

The app is built around a shared AppState. Auth and CSRF are enforced in one
middleware so every route is protected uniformly: while no password is set, all
traffic is funneled to /setup; afterwards a valid session cookie is required for
everything except the login/setup/static endpoints — unless an admin switched the
login requirement off, in which case a visitor without a session acts as the
anonymous pseudo-user (see auth.anonymous_user) and role checks still apply.
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
import time
import urllib.parse
from typing import TYPE_CHECKING

from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.templating import Jinja2Templates

from .. import __version__, clock, i18n, licences
from ..config import ROLE_RANK, node_name
from ..engine import netguard
from ..explain import explain
from ..textfold import search_key
from . import auth, proxy, tlsupload
from .routes import build_routes
from .routes.common import bytesize, client_ip, domid, duration, since
from .routes.forms import escaped_text, in_text_box
from .starlette_compat import RequestBodyLimitMiddleware

if TYPE_CHECKING:
    from ..state import AppState

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_SOFT_BREAK_AFTER = re.compile(r"([/\\_.:@-])")


def softbreak(value) -> Markup:
    """Escape *value* and allow a line break after / \\ _ . : @ - so a long device
    path or name wraps at a separator instead of mid-word (<wbr> adds no text,
    so copy/paste is unchanged). Safe: the separators never occur in entities."""
    return Markup(_SOFT_BREAK_AFTER.sub(r"\1<wbr>", str(escape(value))))  # noqa: S704 - escaped


PUBLIC_PREFIXES = ("/static", "/auth/", "/lang/")  # /auth/oidc/*: the SSO flow; /lang/xx: UI language
# The peer-facing cluster endpoints are authenticated by the shared cluster KEY
# (checked in the handler), not a user session — so they skip the session gate.
# (The *-peer / peer-* proxies are browser-facing and keep the normal session + CSRF.)
_CLUSTER_PEER_PATHS = {"/api/cluster/local", "/api/cluster/control",
                       "/api/cluster/mapping-data", "/api/cluster/mapping-save"}
PUBLIC_PATHS = {"/login", "/setup", "/healthz", "/favicon.ico"} | _CLUSTER_PEER_PATHS

# A request body larger than this is refused (413) before a handler reads it. A route can
# set its own limit: the sign-in forms a smaller one, the configuration import a larger.
MAX_BODY_BYTES = 2 * 1024 * 1024

# High-frequency automatic UI refreshes — not logged to all.log to avoid flooding
# the audit trail (the user actions that drive them ARE logged).
QUIET_PATHS = {"/api/status", "/api/cluster/status", "/api/cluster/local",
               "/api/ports/table", "/api/ports.json", "/healthz", "/favicon.ico"}
# ... and an open log panel's, every 5 s
QUIET_PATTERN = re.compile(r"/api/mappings/[^/]+/log")
# The REST API's counterpart: what monitoring polls. Every other /api/v1 request (any
# change, a read of the mappings — they hold the MQTT passwords — and any refusal) is.
API_QUIET_PATHS = {"/api/v1/health", "/api/v1/status", "/api/v1/ports", "/api/v1/metrics",
                   "/api/v1/openapi.json"}
# What monitoring polls in the web UI: not logged while it is answered (a refusal or an
# error is), and a client that is not a browser is refused with 401, not sent to the
# sign-in page (_monitoring_refusal)
MONITORING_PATHS = {"/metrics"}


def logs_request(mode: str, method: str, path: str, status: int) -> bool:
    """Whether a web UI request gets its "HTTP ..." line in all.log (admin_ui.request_log):
    "all" every request but the static files, the automatic refreshes (QUIET_PATHS,
    QUIET_PATTERN) and the answered scrapes of MONITORING_PATHS; "changes" only those that
    change something (not GET / HEAD / OPTIONS) and the refused or failed ones (400 and
    up); "off" none."""
    if path.startswith("/static") or mode == "off":
        return False
    if mode == "changes":
        return method not in auth.SAFE_METHODS or status >= 400
    return not (method == "GET" and (path in QUIET_PATHS or QUIET_PATTERN.fullmatch(path)
                                     or (path in MONITORING_PATHS and status < 400)))


def _wants_json(request) -> bool:
    return proxy.route_path(request).startswith("/api")


def _login_url(request) -> str:
    """The sign-in page for a page request without a session, as proxy.redirect takes it
    (a path under the base path): with ?next= (where the sign-in leads back to: the page's
    path without the base path, then also given to proxy.redirect) for a page other than
    the dashboard; a POST (a form whose session ended) goes back to the dashboard."""
    path = proxy.route_path(request)
    if request.method != "GET" or path == "/":
        return "/login"
    here = path + ("?" + request.url.query if request.url.query else "")
    return "/login?next=" + urllib.parse.quote(here, safe="")


def _monitoring_refusal(request, path: str):
    """The answer for MONITORING_PATHS without a session to a client that is not a browser
    (it sends credentials, or doesn't ask for HTML): 401, which a Prometheus scrape reports
    as such, instead of the redirect to the sign-in page, which it follows and then can't
    parse. None for a browser (and every other path): it goes to the sign-in page."""
    if path not in MONITORING_PATHS:
        return None
    if "authorization" not in request.headers and \
            "text/html" in request.headers.get("accept", ""):
        return None
    return PlainTextResponse("unauthorized: sign in to the web UI, or read /api/v1/metrics "
                             "with a REST API token\n", status_code=401,
                             headers={"WWW-Authenticate": 'Bearer realm="ser2netweb"'})


async def _answer(request, call_next, state):
    """The handler's answer. One that failed with an exception (answered 500 by
    server_error) is logged here, as the request line it would have had; its traceback
    reaches all.log through uvicorn's error log (logsetup.LibraryLogHandler)."""
    try:
        return await call_next(request)
    except Exception as e:
        ip = client_ip(request)
        state.log(f"HTTP {ip} {request.method} {proxy.route_path(request)} -> 500 "
                  f"({type(e).__name__}: {e})")
        raise


class GuardMiddleware(BaseHTTPMiddleware):
    """Single gate: cross-site refusal + CSRF + setup-redirect + session auth + security
    headers + csrf cookie."""

    _cross_site_log: netguard.LogEvery | None = None

    def _cross_site_note(self, state):
        """Refused cross-site requests are logged at most once a minute."""
        if self._cross_site_log is None:
            self._cross_site_log = netguard.LogEvery(lambda msg: state.log(msg))
        return self._cross_site_log

    async def dispatch(self, request, call_next):
        state = request.app.state.appstate
        cfg = state.config
        # the path the UI's routes see: without the public URL's path when the request came
        # under it (proxy.ProxyMiddleware), so every check below holds there too
        path = proxy.route_path(request)

        # JSON REST API (/api/v1): bearer-token auth, no cookie session, no CSRF — and
        # English, like the logs (only the web UI is translated).
        if path.startswith("/api/v1"):
            response = await self._dispatch_api(request, call_next, cfg, path)
            self._security_headers(response, state, path, request)
            return response

        lang = i18n.negotiate(auth.cookie(request, i18n.COOKIE),
                              request.headers.get("accept-language"), cfg.admin_ui.language)
        request.state.lang = lang
        token = i18n.set_language(lang)
        try:
            response = await self._dispatch_ui(request, call_next, state, cfg, path)
        finally:
            i18n.reset(token)
        self._security_headers(response, state, path, request)  # also on a refusal or a redirect
        return response

    async def _dispatch_ui(self, request, call_next, state, cfg, path):
        ip = client_ip(request)
        early = self._gate_ui(request, state, cfg, path)
        if early is not None:  # refused or sent elsewhere before any handler ran
            # anyone can cause these as often as they like: one line per address and status
            # a minute (state.log_refused); "changes" keeps the refusals, "off" none of them
            mode = cfg.admin_ui.request_log
            if mode == "all" or logs_request(mode, request.method, path, early.status_code):
                state.log_refused((ip, early.status_code),
                                  f"HTTP {ip} {request.method} {path} -> {early.status_code}")
            return early

        response = await _answer(request, call_next, state)

        # all.log: every state-changing request, plus meaningful page loads (skip the
        # static assets and the periodic poll endpoints), or less (admin_ui.request_log)
        if logs_request(cfg.admin_ui.request_log, request.method, path, response.status_code):
            state.log(f"HTTP {ip} {request.method} {path} -> {response.status_code}")

        # The CSRF cookie lasts as long as a session: a browser restart that keeps the
        # session cookie keeps it too, so there is no time without one in which a page of
        # another site could put its own token into the browser.
        if (request.state.csrf_token != request.state.csrf_cookie
                or getattr(request.state, "csrf_renew", False)):
            auth.set_cookie(response, request, cfg, auth.CSRF_COOKIE, request.state.csrf_token,
                            max_age=cfg.session_timeout_s)
        # A page whose token is no longer the browser's (a sign-in in another tab gave the
        # browser a new one: Ctx.new_csrf_token) gets the current one with the answer to its
        # next htmx request, which app.js takes, so a form left open while the session ended
        # can still be saved after signing in again. Only the page's own requests can read
        # an answer's headers, as they can read its <meta name="csrf-token">.
        if (request.headers.get("hx-request") == "true"
                and request.headers.get("x-csrf-token") != request.state.csrf_token):
            response.headers["X-CSRF-Token"] = request.state.csrf_token
        if request.state.legacy_session is not None:
            auth.move_session(response, request, cfg, request.state.legacy_session)
        return response

    def _gate_ui(self, request, state, cfg, path):
        """The answer for a UI request that may not reach its handler (a request of another
        site, a CSRF failure, first-run setup, no session), or None. Sets
        request.state.csrf_token (csrf_cookie: the one the browser sent under this
        instance's name, or None; legacy_session: a 2.8 session cookie to move) and, for a
        signed-in request, request.state.user."""
        is_public = path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)

        # A state-changing request that a page of another site made the browser send is
        # refused before anything else (auth.cross_site): another port of this host or a
        # sibling subdomain can put a CSRF cookie of its own into the browser, so the token
        # alone doesn't prove the request came from this UI. Cluster peers call from their
        # server, not a browser, and are checked by the key.
        if request.method not in auth.SAFE_METHODS and path not in _CLUSTER_PEER_PATHS:
            if why := auth.cross_site(request.headers, also=proxy.published_origins(cfg.admin_ui)):
                ip = request.client.host if request.client else "?"
                self._cross_site_note(state)(f"refused {request.method} {path} from {ip}: "
                                             f"sent by a page of another site ({why}); "
                                             f"{auth.PROXY_HINT}")
                return PlainTextResponse(i18n.gettext(
                    "Refused: this request was sent by a page of another web site. If you used "
                    "ser2netweb’s own page, its address must reach ser2netweb unchanged (behind a "
                    "reverse proxy: as Host or X-Forwarded-Host)."), status_code=403)

        # the CSRF token of this browser: its cookie's (one under the name of 2.8 and older
        # is taken over, and set under its name now), or a new one. A handler may issue a
        # new one (Ctx.new_csrf_token: a sign-in, a sign-out) or renew the cookie's lifetime
        # with a session's (Ctx.set_session); the cookie is set in _dispatch_ui.
        csrf_cookie = auth.cookie(request, auth.CSRF_COOKIE, legacy=False)
        request.state.csrf_cookie = csrf_cookie
        request.state.csrf_token = (csrf_cookie or auth.cookie(request, auth.CSRF_COOKIE)
                                    or auth.new_csrf_token())
        # a sign-in from before the upgrade, under the session cookie's old name: moved to
        # its name now with this response (not a static file's, which may be cached)
        request.state.legacy_session = (
            None if path.startswith("/static")
            else auth.legacy_session(request, cfg, state.revoked_sessions))

        # CSRF: API/HTMX endpoints must carry a matching X-CSRF-Token header.
        # Plain HTML form posts (login/setup/settings/logout) carry _csrf in the
        # body and are validated inside their handlers — the body must NOT be read
        # here, since doing so in a BaseHTTPMiddleware drains the stream before the
        # route handler can parse the form.
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api/") \
                and path not in _CLUSTER_PEER_PATHS:  # peer-to-peer calls: key-authed, no cookie
            if not auth.csrf_token_matches(request, request.headers.get("x-csrf-token")):
                return PlainTextResponse(i18n.gettext("CSRF validation failed. Reload the page."),
                                         status_code=403)

        # first-run: force password setup (the health probe still answers, so a fresh
        # container isn't reported unhealthy while it waits for its admin password)
        if not cfg.password_set:
            if path not in ("/setup", "/healthz") and not path.startswith(("/static", "/lang/")):
                return proxy.redirect(request, "/setup")
        else:
            # password is set: /setup is no longer available
            if path == "/setup":
                return proxy.redirect(request, "/")
            if not is_public:
                token = auth.cookie(request, auth.SESSION_COOKIE)
                user = auth.request_user(cfg, token, request.headers, state.revoked_sessions)
                if user is None:
                    if _wants_json(request):
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                    if (refused := _monitoring_refusal(request, path)) is not None:
                        return refused   # a scraper: 401 (a browser goes to the sign-in page)
                    return proxy.redirect(request, _login_url(request))
                request.state.user = user  # available to handlers for role checks
        return None

    async def _dispatch_api(self, request, call_next, cfg, path):
        """Gate for the JSON REST API: bearer-token auth, no cookies/CSRF, JSON errors.
        The token found (config.ApiToken) is request.state.api_token: its role and its
        name for the audit log. Requests are logged to all.log (but the successful polls
        of API_QUIET_PATHS), refusals one line per address and kind a minute; one refused
        for its token is recorded in the audit log too (api_auth_failed, bounded)."""
        state = request.app.state.appstate
        ip = client_ip(request)
        early, token = self._gate_api(request, cfg, state, path)
        if early is not None:
            by = f" by {token.audit_label}" if token is not None else ""
            state.log_refused((ip, early.status_code),
                              f"HTTP {ip} {request.method} {path} -> {early.status_code}{by}")
            return early
        response = await _answer(request, call_next, state)
        if not (request.method in ("GET", "HEAD") and response.status_code < 400
                and path in API_QUIET_PATHS):
            by = f" by {token.audit_label}" if token is not None else ""
            state.log(f"HTTP {ip} {request.method} {path} -> {response.status_code}{by}")
        return response

    @staticmethod
    def _gate_api(request, cfg, state, path):
        """(the answer for a REST API request that may not reach its handler, or None;
        the API token it presented, also an expired one, or None)."""
        if path in ("/api/v1/health", "/api/v1/openapi.json"):
            return None, None
        if not cfg.password_set:
            return JSONResponse({"error": "setup_incomplete"}, status_code=503), None
        if not cfg.api_token_hash and not cfg.api_tokens:
            return JSONResponse({"error": "api_token_not_configured"}, status_code=401), None
        given = auth.bearer_token(request)
        token = auth.find_api_token(cfg, given)
        if token is None or token.expired():
            ip = client_ip(request)
            why = ("expired token" if token is not None else
                   "wrong token" if given else "no token")
            state.api_auth_failures.note(ip, token.audit_label if token is not None else "",
                                         "api_auth_failed",
                                         f"{request.method} {path[:200]}: {why}", key=(ip,))
        if token is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"}), None
        if token.expired():
            return JSONResponse({"error": "unauthorized", "detail": "API token expired"},
                                status_code=401,
                                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'}), token
        request.state.api_token = token
        used = getattr(state, "api_token_used", None)
        if used is not None:  # in memory only (AppState.api_token_used)
            used[token.id] = time.time()
        # a 'viewer' token is read-only: block state-changing methods
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and \
                ROLE_RANK.get(token.role, 0) < ROLE_RANK["operator"]:
            return JSONResponse(
                {"error": "forbidden", "detail": "API token role 'viewer' is read-only"},
                status_code=403), token
        return None, token

    @staticmethod
    def _security_headers(response, state, path: str, request=None) -> None:
        # Nothing but the static files may be stored by the browser: pages show a new API
        # token or cluster key once, the mapping form an MQTT password, a download a
        # backup or the export, and a copy in the disk cache or the back/forward cache
        # outlives the session (Back after a logout on a shared PC). The static files
        # carry ?v=<version> and stay cacheable.
        cfg = state.config
        if not path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        # script-src stays 'self' (no inline JS — the important XSS guard). style-src
        # allows 'unsafe-inline' because the console's xterm.js DOM renderer adds <style>
        # elements and style attributes at runtime, without a nonce (xtermjs/xterm.js#4445).
        # No <base>, no plugins, and forms only post to this UI.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'; object-src 'none'",
        )
        # HSTS covers the host name on every port (Settings -> Admin TLS sets how long; 0
        # sends max-age=0, which makes browsers forget it). Behind a TLS proxy only when
        # asked for (Settings -> Reverse proxy): a direct http://name:port would stop working.
        # TLS: what this listener serves (AppState.serving_tls), not what waits for a restart
        if state.serving_tls or (cfg.admin_ui.proxy_hsts and request is not None
                                 and auth.is_https(request, cfg)):
            response.headers.setdefault("Strict-Transport-Security",
                                        f"max-age={cfg.admin_ui.hsts_max_age}")


def _page_context(request) -> dict:
    """What every template gets: base, the path prefix of the request, which its links go
    under (ser2netweb/web/proxy.py); node_name, this instance's name ('' = none: Settings -> Admin
    interface), which the signed-in pages (_shell.html) show in the title and the top bar;
    login_notice / login_notice_url, the notice the sign-in and setup pages show (every one
    of their renders, also a refusal's) and open-access visitors get a link to (Settings ->
    Web UI access)."""
    state = getattr(request.app.state, "appstate", None)
    ui = state.config.admin_ui if state is not None else None
    return {"base": proxy.base(request),
            "node_name": node_name(ui) if ui is not None else "",
            "login_notice": ui.login_notice if ui is not None else "",
            "login_notice_url": ui.login_notice_url if ui is not None else ""}


@functools.lru_cache(maxsize=None)
def asset_version(static_dir: str = STATIC_DIR) -> str:
    """A short hash of every file under static/ (names and contents)."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(static_dir):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            h.update(os.path.relpath(path, static_dir).replace(os.sep, "/").encode() + b"\0")
            with open(path, "rb") as fh:
                h.update(fh.read())
            h.update(b"\0")
    return h.hexdigest()[:12]


def make_templates() -> Jinja2Templates:
    """The Jinja environment every page is rendered with (globals + filters)."""
    # _page_context for every page; base_path(request) / route_path(request) for macros
    # imported without the context
    templates = Jinja2Templates(directory=TEMPLATES_DIR, context_processors=[_page_context])

    # template globals used across pages
    from ..config import (
        COMMON_BAUDRATES, BYTESIZES, PARITIES, PARITY_LABELS, STOPBITS,
        FLOWCONTROLS, PROTOCOLS, LINE_STATES, host_port, identity_text, shown_host,
    )
    templates.env.globals.update(
        baudrates=COMMON_BAUDRATES, bytesizes=BYTESIZES, parities=PARITIES,
        parity_labels=PARITY_LABELS, stopbits=STOPBITS, flowcontrols=FLOWCONTROLS,
        protocols=PROTOCOLS, line_states=LINE_STATES, identity_text=identity_text,
        host_port=host_port, shown_host=shown_host,
        # ?v=<hash of the static files> on the asset URLs: browsers fetch the new CSS/JS as
        # soon as an asset changes (an upgrade, a build from main) and may keep it for good
        # until then (StaticAssets), also across restarts of the one-file executable
        asset_version=asset_version(),
        app_version=__version__,   # what the pages name as this version
        source_url=licences.source_url(),  # the footer's source link (SER2NETWEB_SOURCE_URL)
        # the server's time zone now ("UTC+03:00"), next to the times it shows
        server_zone=clock.zone,
        # the server's clock when it rendered the page (<body data-server-time>): app.js
        # compares it with the browser's (not the Date header: a reverse proxy sets its own)
        server_now=lambda: int(time.time()),
    )
    templates.env.filters["softbreak"] = softbreak
    from ..engine.portlist import choice as port_choice
    templates.env.globals["port_choice"] = port_choice
    templates.env.filters["domid"] = domid
    templates.env.filters["bytesize"] = bytesize
    templates.env.filters["duration"] = duration   # seconds, in the page's language
    templates.env.filters["since"] = since         # a Unix time until now, as duration
    # what a raw OS / network error means, in the page's language (None: show it as it is)
    templates.env.filters["explain"] = explain
    # data-search of the dashboard rows: app.js folds what is typed the same way
    templates.env.filters["searchkey"] = search_key
    templates.env.filters["in_text_box"] = in_text_box   # a value as a text box sends it
    templates.env.filters["escaped_text"] = escaped_text  # a text of escapes, control chars too
    from ..notify import no_data_for
    templates.env.globals["no_data_for"] = no_data_for  # a mapping's no-data alarm, now
    # _("...") in every template, translated for the request's language (ser2netweb/i18n.py);
    # newstyle: _("Hello %(name)s", name=x) formats and escapes x
    templates.env.add_extension("jinja2.ext.i18n")
    templates.env.install_gettext_callables(i18n.gettext, i18n.ngettext, newstyle=True)  # type: ignore[attr-defined]
    templates.env.globals["languages"] = i18n.LANGUAGES
    templates.env.globals["tls_upload_kb"] = tlsupload.MAX_FILE_BYTES // 1024
    templates.env.globals.update(base_path=proxy.base, route_path=proxy.route_path)
    return templates


async def server_error(request, exc) -> PlainTextResponse | JSONResponse:
    """The answer to a request that failed with an unexpected exception (the server logs
    its traceback): JSON under the REST API, whose clients expect nothing else, else the
    usual plain text."""
    if request.url.path.startswith("/api/v1"):
        return JSONResponse({"error": "internal", "detail": "internal server error"},
                            status_code=500)
    return PlainTextResponse("Internal Server Error", status_code=500)


def build_app(state: AppState) -> Starlette:
    templates = make_templates()
    routes = build_routes(templates, state, STATIC_DIR)
    # The body limit holds for every route, and sits inside GuardMiddleware: in front of
    # it (Starlette(max_body_size=...)), a body over the limit would reach the handler as
    # an exception group from BaseHTTPMiddleware's receive, a 500 instead of the 413.
    # ProxyMiddleware goes first: the client address, the scheme and the path prefix of a
    # request through a reverse proxy are what every check after it sees
    middleware = [Middleware(proxy.ProxyMiddleware, state=state),
                  Middleware(GuardMiddleware),
                  Middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)]
    app = Starlette(routes=routes, middleware=middleware,
                    exception_handlers={Exception: server_error})
    app.state.appstate = state
    app.state.templates = templates
    return app
