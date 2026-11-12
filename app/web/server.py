"""Starlette application factory: routes + security/auth/CSRF middleware.

The app is built around a shared AppState. Auth and CSRF are enforced in one
middleware so every route is protected uniformly: while no password is set, all
traffic is funneled to /setup; afterwards a valid session cookie is required for
everything except the login/setup/static endpoints — unless an admin switched the
login requirement off, in which case a visitor without a session acts as the
anonymous pseudo-user (see auth.anonymous_user) and role checks still apply.
"""
from __future__ import annotations

import os
import re
import time

from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from .. import clock, i18n
from ..config import ROLE_RANK
from . import auth
from .routes import build_routes
from .routes.common import client_ip

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_SOFT_BREAK_AFTER = re.compile(r"([/\\_.:@-])")


def softbreak(value) -> Markup:
    """Escape *value* and allow a line break after / \\ _ . : @ - so a long device
    path or name wraps at a separator instead of mid-word (<wbr> adds no text,
    so copy/paste is unchanged). Safe: the separators never occur in entities."""
    return Markup(_SOFT_BREAK_AFTER.sub(r"\1<wbr>", str(escape(value))))


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
# The REST API's counterpart: what monitoring polls. Every other /api/v1 request (any
# change, a read of the mappings — they hold the MQTT passwords — and any refusal) is.
API_QUIET_PATHS = {"/api/v1/health", "/api/v1/status", "/api/v1/ports", "/api/v1/metrics",
                   "/api/v1/openapi.json"}


def _wants_json(request) -> bool:
    return request.url.path.startswith("/api")


class GuardMiddleware(BaseHTTPMiddleware):
    """Single gate: CSRF + setup-redirect + session auth + security headers + csrf cookie."""

    async def dispatch(self, request, call_next):
        state = request.app.state.appstate
        cfg = state.config
        path = request.url.path

        # JSON REST API (/api/v1): bearer-token auth, no cookie session, no CSRF — and
        # English, like the logs (only the web UI is translated).
        if path.startswith("/api/v1"):
            return await self._dispatch_api(request, call_next, cfg)

        lang = i18n.negotiate(request.cookies.get(i18n.COOKIE),
                              request.headers.get("accept-language"), cfg.admin_ui.language)
        request.state.lang = lang
        token = i18n.set_language(lang)
        try:
            return await self._dispatch_ui(request, call_next, state, cfg, path)
        finally:
            i18n.reset(token)

    async def _dispatch_ui(self, request, call_next, state, cfg, path):
        early = self._gate_ui(request, state, cfg, path)
        if early is not None:  # refused or sent elsewhere before any handler ran
            ip = request.client.host if request.client else "?"
            state.log_refused((ip, early.status_code),
                              f"HTTP {ip} {request.method} {path} -> {early.status_code}")
            self._security_headers(early, cfg)
            return early

        response = await call_next(request)

        # audit log: every state-changing request, plus meaningful page loads
        # (skip the static assets and the periodic poll endpoints)
        if not path.startswith("/static") and not (
            request.method == "GET" and path in QUIET_PATHS
        ):
            ip = request.client.host if request.client else "?"
            state.log(f"HTTP {ip} {request.method} {path} -> {response.status_code}")

        self._security_headers(response, cfg)

        if request.state.set_csrf:
            response.set_cookie(
                auth.CSRF_COOKIE, request.state.csrf_token, samesite="lax",
                httponly=True, secure=cfg.admin_ui.tls_enabled, path="/",
            )
        return response

    def _gate_ui(self, request, state, cfg, path):
        """The answer for a UI request that may not reach its handler (a CSRF failure,
        first-run setup, no session), or None. Sets request.state.csrf_token (and
        set_csrf: the cookie is new) and, for a signed-in request, request.state.user."""
        is_public = path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)

        # ensure a CSRF token exists for this request/response
        csrf_cookie = request.cookies.get(auth.CSRF_COOKIE)
        request.state.set_csrf = csrf_cookie is None
        if csrf_cookie is None:
            csrf_cookie = auth.new_csrf_token()
        request.state.csrf_token = csrf_cookie

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
                return RedirectResponse("/setup", status_code=303)
        else:
            # password is set: /setup is no longer available
            if path == "/setup":
                return RedirectResponse("/", status_code=303)
            if not is_public:
                token = request.cookies.get(auth.SESSION_COOKIE)
                user = auth.request_user(cfg, token, request.headers, state.revoked_sessions)
                if user is None:
                    if _wants_json(request):
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                    return RedirectResponse("/login", status_code=303)
                request.state.user = user  # available to handlers for role checks
        return None

    async def _dispatch_api(self, request, call_next, cfg):
        """Gate for the JSON REST API: bearer-token auth, no cookies/CSRF, JSON errors.
        The token found (config.ApiToken) is request.state.api_token: its role and its
        name for the audit log. Requests are logged to all.log (but the successful polls
        of API_QUIET_PATHS), refusals one line per address and kind a minute; one refused
        for its token is recorded in the audit log too (api_auth_failed, bounded)."""
        state = request.app.state.appstate
        path, ip = request.url.path, request.client.host if request.client else "?"
        early, token = self._gate_api(request, cfg, state, path)
        if early is not None:
            by = f" by {token.audit_label}" if token is not None else ""
            state.log_refused((ip, early.status_code),
                              f"HTTP {ip} {request.method} {path} -> {early.status_code}{by}")
            self._security_headers(early, cfg)
            return early
        response = await call_next(request)
        if not (request.method in ("GET", "HEAD") and response.status_code < 400
                and path in API_QUIET_PATHS):
            by = f" by {token.audit_label}" if token is not None else ""
            state.log(f"HTTP {ip} {request.method} {path} -> {response.status_code}{by}")
        self._security_headers(response, cfg)
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
    def _security_headers(response, cfg) -> None:
        # script-src stays 'self' (no inline JS — the important XSS guard). style-src
        # allows 'unsafe-inline' because xterm.js injects a <style> element at runtime.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        if cfg.admin_ui.tls_enabled:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")


def make_templates() -> Jinja2Templates:
    """The Jinja environment every page is rendered with (globals + filters)."""
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    # template globals used across pages
    from ..config import (
        COMMON_BAUDRATES, BYTESIZES, PARITIES, PARITY_LABELS, STOPBITS,
        FLOWCONTROLS, PROTOCOLS, LINE_STATES,
    )
    from .. import __version__
    templates.env.globals.update(
        baudrates=COMMON_BAUDRATES, bytesizes=BYTESIZES, parities=PARITIES,
        parity_labels=PARITY_LABELS, stopbits=STOPBITS, flowcontrols=FLOWCONTROLS,
        protocols=PROTOCOLS, line_states=LINE_STATES,
        # ?v=<version> on the static URLs: browsers fetch the new CSS/JS right after an
        # upgrade instead of reusing a heuristically cached copy with the new templates
        asset_version=__version__,
        # the server's time zone now ("UTC+03:00"), next to the times it shows
        server_zone=clock.zone,
    )
    templates.env.filters["softbreak"] = softbreak
    # _("...") in every template, translated for the request's language (app/i18n.py);
    # newstyle: _("Hello %(name)s", name=x) formats and escapes x
    templates.env.add_extension("jinja2.ext.i18n")
    templates.env.install_gettext_callables(i18n.gettext, i18n.ngettext, newstyle=True)  # type: ignore[attr-defined]
    templates.env.globals["languages"] = i18n.LANGUAGES
    return templates


def build_app(state) -> Starlette:
    templates = make_templates()
    routes = build_routes(templates, state, STATIC_DIR)
    # The body limit holds for every route, and sits inside GuardMiddleware: in front of
    # it (Starlette(max_body_size=...)), a body over the limit would reach the handler as
    # an exception group from BaseHTTPMiddleware's receive, a 500 instead of the 413.
    middleware = [Middleware(GuardMiddleware),
                  Middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.appstate = state
    app.state.templates = templates
    return app
