"""Starlette application factory: routes + security/auth/CSRF middleware.

The app is built around a shared AppState. Auth and CSRF are enforced in one
middleware so every route is protected uniformly: while no password is set, all
traffic is funneled to /setup; afterwards a valid session cookie is required for
everything except the login/setup/static endpoints.
"""
from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from . import auth
from .routes import build_routes

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

PUBLIC_PREFIXES = ("/static",)
PUBLIC_PATHS = {"/login", "/setup", "/healthz", "/favicon.ico"}

# High-frequency automatic UI refreshes — not logged to all.log to avoid flooding
# the audit trail (the user actions that drive them ARE logged).
QUIET_PATHS = {"/api/status", "/api/ports/table", "/api/ports.json", "/healthz", "/favicon.ico"}


def _wants_json(request) -> bool:
    return request.url.path.startswith("/api")


class GuardMiddleware(BaseHTTPMiddleware):
    """Single gate: CSRF + setup-redirect + session auth + security headers + csrf cookie."""

    async def dispatch(self, request, call_next):
        state = request.app.state.appstate
        cfg = state.config
        path = request.url.path

        # JSON REST API (/api/v1): bearer-token auth, no cookie session, no CSRF.
        if path.startswith("/api/v1"):
            return await self._dispatch_api(request, call_next, cfg)

        is_public = path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)

        # ensure a CSRF token exists for this request/response
        csrf_cookie = request.cookies.get(auth.CSRF_COOKIE)
        set_csrf = csrf_cookie is None
        if set_csrf:
            csrf_cookie = auth.new_csrf_token()
        request.state.csrf_token = csrf_cookie

        # CSRF: API/HTMX endpoints must carry a matching X-CSRF-Token header.
        # Plain HTML form posts (login/setup/settings/logout) carry _csrf in the
        # body and are validated inside their handlers — the body must NOT be read
        # here, since doing so in a BaseHTTPMiddleware drains the stream before the
        # route handler can parse the form.
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and path.startswith("/api/"):
            if not auth.csrf_token_matches(request, request.headers.get("x-csrf-token")):
                return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)

        # first-run: force password setup
        if not cfg.password_set:
            if path != "/setup" and not path.startswith("/static"):
                return RedirectResponse("/setup", status_code=303)
        else:
            # password is set: /setup is no longer available
            if path == "/setup":
                return RedirectResponse("/", status_code=303)
            if not is_public:
                token = request.cookies.get(auth.SESSION_COOKIE)
                user = auth.session_user(cfg, token)
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

        self._security_headers(response, cfg)

        if set_csrf:
            response.set_cookie(
                auth.CSRF_COOKIE, csrf_cookie, samesite="lax",
                httponly=True, secure=cfg.admin_ui.tls_enabled, path="/",
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
        response = await call_next(request)
        self._security_headers(response, cfg)
        return response

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


def build_app(state) -> Starlette:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    # template globals used across pages
    from ..config import (
        COMMON_BAUDRATES, BYTESIZES, PARITIES, PARITY_LABELS, STOPBITS,
        FLOWCONTROLS, PROTOCOLS, LINE_STATES,
    )
    templates.env.globals.update(
        baudrates=COMMON_BAUDRATES, bytesizes=BYTESIZES, parities=PARITIES,
        parity_labels=PARITY_LABELS, stopbits=STOPBITS, flowcontrols=FLOWCONTROLS,
        protocols=PROTOCOLS, line_states=LINE_STATES,
    )

    routes = build_routes(templates, state, STATIC_DIR)
    middleware = [Middleware(GuardMiddleware)]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.appstate = state
    app.state.templates = templates
    return app
