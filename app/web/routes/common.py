"""What every route module shares: rendering, the caller's identity and role checks.

One ``Ctx`` is built per app (``build_routes``) and handed to each route module, so
the helpers that used to be closures inside one big ``build_routes()`` live here once.
"""
from __future__ import annotations

from starlette.responses import PlainTextResponse

from ...config import ROLE_RANK
from .. import auth

# HTMX response header: tells the dashboard to re-fetch the mappings table now
TRIGGER = {"HX-Trigger": "refreshMappings"}


def client_ip(request) -> str:
    ip = request.client.host if request.client else "?"
    # normalize IPv4-mapped IPv6 (::ffff:1.2.3.4 -> 1.2.3.4) so the rate-limit
    # key is consistent on dual-stack listeners
    if ip.startswith("::ffff:") and "." in ip:
        ip = ip[len("::ffff:"):]
    return ip


def current_user(request):
    return getattr(request.state, "user", None)


def csrf_failed() -> PlainTextResponse:
    return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)


class Ctx:
    """The templates + shared AppState, and the per-request helpers built on them."""

    client_ip = staticmethod(client_ip)
    current_user = staticmethod(current_user)

    def __init__(self, templates, state) -> None:
        self.templates = templates
        self.state = state

    def render(self, request, name, status_code=200, headers=None, **ctx):
        return self.templates.TemplateResponse(request, name, ctx,
                                               status_code=status_code, headers=headers)

    def set_session(self, response, request, user) -> None:
        cfg = self.state.config
        ttl = cfg.session_timeout_s  # Settings -> Web UI access -> session lifetime
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(cfg.secret_key, ttl, user.username, user.pwd_version),
            max_age=ttl, httponly=True, samesite="lax",
            secure=cfg.admin_ui.tls_enabled, path="/",
        )

    @staticmethod
    def require_role(request, role):
        """Return a 403 response if the session user's role is below `role`, else None."""
        u = current_user(request)
        if u is None or ROLE_RANK.get(u.role, 0) < ROLE_RANK[role]:
            return PlainTextResponse(f"Forbidden: requires {role} role.", status_code=403)
        return None

    @staticmethod
    def can_edit(request) -> bool:
        u = current_user(request)
        return u is not None and ROLE_RANK.get(u.role, 0) >= ROLE_RANK["operator"]

    @staticmethod
    def is_anonymous(request) -> bool:
        u = current_user(request)
        return u is not None and u.source == "anonymous"

    def audit(self, request, action: str, detail: str = "", user: str | None = None) -> None:
        """An audit record for this request: client IP plus the acting user (the
        signed-in user unless given)."""
        if user is None:
            u = current_user(request)
            user = (u.username if u.source != "anonymous" else "(no login)") if u is not None else ""
        self.state.audit(client_ip(request), action, detail, user=user)

    def require_peer_access(self, request):
        """Cluster peer actions act on OTHER nodes with the shared key, so they need a
        signed-in operator: open access on this node must not open every peer (which
        may well require a login of its own)."""
        if self.is_anonymous(request):
            return PlainTextResponse("Sign in to manage other cluster nodes.", status_code=403)
        return self.require_role(request, "operator")
