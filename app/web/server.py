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

from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from .. import i18n
from ..config import ROLE_RANK
from ..engine import netguard
from . import auth
from .routes import build_routes

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


def _wants_json(request) -> bool:
    return request.url.path.startswith("/api")


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
        path = request.url.path

        # JSON REST API (/api/v1): bearer-token auth, no cookie session, no CSRF — and
        # English, like the logs (only the web UI is translated).
        if path.startswith("/api/v1"):
            response = await self._dispatch_api(request, call_next, cfg)
            self._security_headers(response, cfg, path)
            return response

        lang = i18n.negotiate(request.cookies.get(i18n.COOKIE),
                              request.headers.get("accept-language"), cfg.admin_ui.language)
        request.state.lang = lang
        token = i18n.set_language(lang)
        try:
            response = await self._dispatch_ui(request, call_next, state, cfg, path)
        finally:
            i18n.reset(token)
        self._security_headers(response, cfg, path)  # also on a refusal or a redirect
        return response

    async def _dispatch_ui(self, request, call_next, state, cfg, path):

        is_public = path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)

        # A state-changing request that a page of another site made the browser send is
        # refused before anything else (auth.cross_site): another port of this host or a
        # sibling subdomain can put a CSRF cookie of its own into the browser, so the token
        # alone doesn't prove the request came from this UI. Cluster peers call from their
        # server, not a browser, and are checked by the key.
        if request.method not in auth.SAFE_METHODS and path not in _CLUSTER_PEER_PATHS:
            if why := auth.cross_site(request.headers):
                ip = request.client.host if request.client else "?"
                self._cross_site_note(state)(f"refused {request.method} {path} from {ip}: "
                                             f"sent by a page of another site ({why}); "
                                             f"{auth.PROXY_HINT}")
                return PlainTextResponse(i18n.gettext(
                    "Refused: this request was sent by a page of another web site. If you used "
                    "ser2net’s own page, its address must reach ser2net unchanged (behind a "
                    "reverse proxy: as Host or X-Forwarded-Host)."), status_code=403)

        # the CSRF token of this browser: its cookie's, or a new one. A handler may issue a
        # new one (Ctx.new_csrf_token: a sign-in, a sign-out) or renew the cookie's lifetime
        # with a session's (Ctx.set_session); the cookie is set below.
        csrf_cookie = request.cookies.get(auth.CSRF_COOKIE)
        request.state.csrf_token = csrf_cookie or auth.new_csrf_token()

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

        response = await call_next(request)

        # audit log: every state-changing request, plus meaningful page loads
        # (skip the static assets and the periodic poll endpoints)
        if not path.startswith("/static") and not (
            request.method == "GET" and path in QUIET_PATHS
        ):
            ip = request.client.host if request.client else "?"
            state.log(f"HTTP {ip} {request.method} {path} -> {response.status_code}")

        # The CSRF cookie lasts as long as a session: a browser restart that keeps the
        # session cookie keeps it too, so there is no time without one in which a page of
        # another site could put its own token into the browser.
        if (request.state.csrf_token != csrf_cookie
                or getattr(request.state, "csrf_renew", False)):
            response.set_cookie(
                auth.CSRF_COOKIE, request.state.csrf_token, max_age=cfg.session_timeout_s,
                samesite="lax", httponly=True, secure=cfg.admin_ui.tls_enabled, path="/",
            )
        return response

    async def _dispatch_api(self, request, call_next, cfg):
        """Gate for the JSON REST API: bearer-token auth, no cookies/CSRF, JSON errors."""
        path = request.url.path
        if path not in ("/api/v1/health", "/api/v1/openapi.json"):
            if not cfg.password_set:
                return JSONResponse({"error": "setup_incomplete"}, status_code=503)
            if not cfg.api_token_hash:
                return JSONResponse({"error": "api_token_not_configured"}, status_code=401)
            if not auth.verify_token(auth.bearer_token(request), cfg.api_token_hash):
                return JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            # a 'viewer' token is read-only: block state-changing methods
            if request.method in ("POST", "PUT", "DELETE", "PATCH") and \
                    ROLE_RANK.get(cfg.api_token_role, 0) < ROLE_RANK["operator"]:
                return JSONResponse(
                    {"error": "forbidden", "detail": "API token role 'viewer' is read-only"},
                    status_code=403)
        return await call_next(request)

    @staticmethod
    def _security_headers(response, cfg, path: str) -> None:
        # Nothing but the static files may be stored by the browser: pages show a new API
        # token or cluster key once, the mapping form an MQTT password, a download a
        # backup or the export, and a copy in the disk cache or the back/forward cache
        # outlives the session (Back after a logout on a shared PC). The static files
        # carry ?v=<version> and stay cacheable.
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
        # sends max-age=0, which makes browsers forget it)
        if cfg.admin_ui.tls_enabled:
            response.headers.setdefault("Strict-Transport-Security",
                                        f"max-age={cfg.admin_ui.hsts_max_age}")


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
