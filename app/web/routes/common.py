"""What every route module shares: rendering, the caller's identity and role checks.

One ``Ctx`` is built per app (``build_routes``) and handed to each route module, so
the helpers that used to be closures inside one big ``build_routes()`` live here once.
"""
from __future__ import annotations

import json
import urllib.parse

from starlette.responses import PlainTextResponse

from ...config import ROLE_RANK
from ...i18n import _
from .. import auth

# HTMX response header: tells the dashboard to re-fetch the mappings table now
TRIGGER = {"HX-Trigger": "refreshMappings"}


def notice(text: str, kind: str = "ok") -> dict:
    """The response header that has the dashboard (app.js, its feedback area) tell the
    user what an action did: "ok" as a status message, "warn" as an alert. A header, so
    the action's answer and its status stay what they are; JSON, percent-encoded (a
    header value is Latin-1 only)."""
    return {"X-Notice": urllib.parse.quote(json.dumps({"kind": kind, "text": text},
                                                      ensure_ascii=False))}


def action_done(action: str, name: str) -> str:
    """What a start / stop / restart of the mapping `name` did, for notice()."""
    if action == "start":
        return _("“{0}” started.").format(name)
    if action == "stop":
        return _("“{0}” stopped.").format(name)
    return _("“{0}” restarted.").format(name)


def start_failed(state, mid: str) -> str:
    """Why the mapping `mid` did not start, when it was just started or restarted and
    its runner stopped with an error; else ""."""
    st = state.supervisor.status(mid) or {}
    return (st.get("last_error") or _("unknown error")) if st.get("state") == "error" else ""


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
    return PlainTextResponse(_("CSRF validation failed. Reload the page."), status_code=403)


def has_role(user, role: str) -> bool:
    """`user` (an account, the open-access pseudo-user or None) has `role` or a higher one."""
    return user is not None and ROLE_RANK.get(user.role, 0) >= ROLE_RANK[role]


def console_interactive(user, mapping) -> bool:
    """Does what `user` types in the console of `mapping` go to the device? Only on a
    network mapping that isn't read-only, and only for operators and admins."""
    return mapping.kind == "net" and not mapping.network.read_only and has_role(user, "operator")


def sees_to_device(user, mapping) -> bool:
    """Does `user`'s hex view of `mapping` show what is sent TO the device? On a network
    mapping that is what clients and web-console users type, passwords at a prompt the
    device doesn't echo included (trace files record it too), so a viewer's hex view shows
    only what the device sends. A serial↔serial bridge has a device on both ports, and its
    console has always shown both directions."""
    return mapping.kind != "net" or has_role(user, "operator")


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
        self.state.signins.note(user.username, user.source)   # its last sign-in
        issued = self.state.sessions_issued
        issued[user.username] = issued.get(user.username, 0) + 1
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(cfg.secret_key, ttl, user.username, user.pwd_version),
            max_age=ttl, httponly=True, samesite="lax",
            secure=cfg.admin_ui.tls_enabled, path="/",
        )

    @staticmethod
    def require_role(request, role):
        """Return a 403 response if the session user's role is below `role`, else None."""
        if not has_role(current_user(request), role):
            return PlainTextResponse(_("Forbidden: requires the {0} role.").format(role), status_code=403)
        return None

    @staticmethod
    def can_edit(request) -> bool:
        return has_role(current_user(request), "operator")

    @staticmethod
    def is_anonymous(request) -> bool:
        u = current_user(request)
        return u is not None and u.source == "anonymous"

    @staticmethod
    def actor(request) -> str:
        """Who acts, for the audit log: the signed-in user, "(no login)" for an
        open-access visitor."""
        u = current_user(request)
        return (u.username if u.source != "anonymous" else "(no login)") if u is not None else ""

    def audit(self, request, action: str, detail: str = "", user: str | None = None) -> None:
        """An audit record for this request: client IP plus the acting user (the
        signed-in user unless given)."""
        self.state.audit(client_ip(request), action, detail,
                         user=self.actor(request) if user is None else user)

    def require_peer_access(self, request):
        """Cluster peer actions act on OTHER nodes with the shared key, so they need a
        signed-in operator: open access on this node must not open every peer (which
        may well require a login of its own)."""
        if self.is_anonymous(request):
            return PlainTextResponse(_("Sign in to manage other cluster nodes."), status_code=403)
        return self.require_role(request, "operator")
