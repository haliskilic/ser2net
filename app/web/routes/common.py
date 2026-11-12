"""What every route module shares: rendering, the caller's identity and role checks.

One ``Ctx`` is built per app (``build_routes``) and handed to each route module, so
the helpers that used to be closures inside one big ``build_routes()`` live here once.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable

from markupsafe import escape
from starlette.responses import HTMLResponse, PlainTextResponse, Response

from ...config import ROLE_RANK
from ...i18n import _
from .. import auth

# HTMX response header: tells the dashboard to re-fetch the mappings table now
TRIGGER = {"HX-Trigger": "refreshMappings"}


def unchanged_or(request, resp):
    """A fragment the dashboard polls: 204 when the browser shows this content already
    (the X-Content-Hash it sent; htmx then keeps the panel as it is — no flicker, the
    focus, a text selection and the scroll position stay), else the fragment with its
    hash."""
    digest = hashlib.sha256(resp.body).hexdigest()[:16]
    if request.headers.get("x-content-hash") == digest:
        return Response(status_code=204, headers={"X-Content-Hash": digest})
    resp.headers["X-Content-Hash"] = digest
    return resp


class LiveTable:
    """A table the dashboard polls every few seconds (the mappings, the fleet view),
    answered without rendering it again when that can be helped.

    Its X-Content-Hash is "<shape>.<counters>": a fingerprint of everything the table
    shows except the In / Out counters, and one of those. The browser sends back the one
    it has, and the answer is
      * 204 when both match: the browser keeps its table;
      * when only the counters changed: just those, as htmx out-of-band swaps of their
        <span id=...> (HX-Reswap: none), so the rows, the keyboard focus, a text
        selection, a tooltip or a click in progress stay;
      * else the whole table, rendered off the event loop (a render of a few hundred
        rows takes 100+ ms, during which every bridge on the loop would wait - a Modbus
        RTU reply that pauses for 10 ms counts as finished), and kept for other tabs
        that ask for the same table. (So the template may not use the request beyond
        the language, which is part of the fingerprint.)
    The fingerprints are taken in the same worker thread (a repr of every mapping is
    not free either). They are keyed with a secret of this process: the shape covers
    settings the browser is not shown. A tab from before an upgrade or a restart sends
    a hash that matches neither and gets the whole table once."""

    def __init__(self, ctx: "Ctx", template: str, size: int = 8) -> None:
        self.ctx, self.template, self._size = ctx, template, size
        self._key = os.urandom(16)
        self._bodies: OrderedDict[str, str] = OrderedDict()   # tag -> rendered table
        self._lock = threading.Lock()

    def _digest(self, value) -> str:
        return hashlib.blake2b(repr(value).encode("utf-8", "backslashreplace"),
                               key=self._key, digest_size=8).hexdigest()

    async def answer(self, request, snapshot: Callable[[], tuple[object, dict[str, str] | None]],
                     headers=None, **context) -> Response:
        """`snapshot()` -> (shape, spans): what the table shows apart from the counters,
        and {element id: text} of the counter spans - None when they can't be patched by
        id, and then the shape has to include them. Called in a worker thread; `context`
        renders the table from the same data."""
        sent = request.headers.get("x-content-hash") or ""
        lang = getattr(request.state, "lang", "")

        def work() -> tuple[str, str | None, bool]:
            """(tag, body or None for "unchanged", whether body is only the counters)"""
            shape, spans = snapshot()
            tag = self._digest((lang, shape)) + "." + self._digest(spans)
            if sent == tag:
                return tag, None, False
            if spans is not None and sent.partition(".")[0] == tag.partition(".")[0]:
                return tag, "".join(f'<span id="{escape(k)}" hx-swap-oob="true">{escape(v)}</span>'
                                    for k, v in spans.items()), True
            with self._lock:
                body = self._bodies.get(tag)
            if body is None:
                body = self.ctx.templates.get_template(self.template).render(
                    {"request": request, **context})
                with self._lock:
                    self._bodies[tag] = body
                    while len(self._bodies) > self._size:
                        self._bodies.popitem(last=False)
            return tag, body, False

        tag, body, patch = await asyncio.to_thread(work)
        h = {**(headers or {}), "X-Content-Hash": tag}
        if body is None:
            return Response(status_code=204, headers=h)
        if patch:   # no swap of the table itself; htmx still does the out-of-band ones
            h["HX-Reswap"] = "none"
        return HTMLResponse(body, headers=h)


_NOT_ID_CHAR = re.compile(r"[^A-Za-z0-9_-]")


def domid(value) -> str:
    """*value* as part of an HTML id: other characters than letters, digits, _ and -
    become _. (htmx keeps the keyboard focus on a polled table's buttons across a
    refresh by their ids; a peer's address and mapping id are part of them.)"""
    return _NOT_ID_CHAR.sub("_", str(value))


def bytesize(value) -> str:
    """A byte count for people: 512 B, 1.5 KB, 12.3 MB, 4.0 GB (units of 1024, one
    decimal) — the dashboard's In / Out and the trace file sizes. Not Jinja's
    filesizeformat, which writes an untranslated "Bytes"; the exact counts stay in the
    REST API and /metrics."""
    try:
        n = int(value)
    except (TypeError, ValueError):   # a peer's payload in the fleet view: shown as it is
        return str(value)
    for unit, size in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def duration(secs) -> str:
    """A connection's age for people, in the page's language: 2m 05s, 1h 02m (as
    engine.bridge.fmt_duration writes it in English for the logs and the REST API;
    app.js counts it on in the browser with the same units)."""
    try:
        s = max(0, int(secs))
    except (TypeError, ValueError, OverflowError):
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return _("{0}h {1}m").format(h, f"{m:02d}")
    if m:
        return _("{0}m {1}s").format(m, f"{s:02d}")
    return _("{0}s").format(s)


def since(unix_time) -> str:
    """duration() from `unix_time` (a client's connected_at) until now."""
    try:
        return duration(time.time() - float(unix_time))
    except (TypeError, ValueError):
        return ""


def notice(text: str, kind: str = "ok", focus: str = "") -> dict:
    """The response header that has the dashboard (app.js, its feedback area) tell the
    user what an action did: "ok" as a status message, "warn" as an alert; `focus`: the
    id of the element that gets the keyboard focus when the form panel closes. A header,
    so the action's answer and its status stay what they are; JSON, percent-encoded (a
    header value is Latin-1 only)."""
    d = {"kind": kind, "text": text, **({"focus": focus} if focus else {})}
    return {"X-Notice": urllib.parse.quote(json.dumps(d, ensure_ascii=False))}


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


def local_path(value, default: str = "/") -> str:
    """`value` when it is a path on this site — where to go after a sign-in or a language
    switch — else `default`: no scheme or host, no //host or /\\host (other sites), no
    control characters (a browser drops a tab or newline and may read what is left as
    another site)."""
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or "\\" in value or len(value) > 2048 or any(c < " " or c == "\x7f" for c in value)):
        return default
    return value


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
    return (mapping.kind == "net" and not mapping.network.read_only
            # a Modbus gateway only watches its bus: raw bytes would corrupt its frames
            and mapping.network.protocol != "modbus" and has_role(user, "operator"))


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

    async def arender(self, request, name, status_code=200, headers=None, **ctx):
        """render() in a worker thread: for the panels that refresh themselves, whose
        render would hold up every bridge on the event loop for as long as it takes (the
        page's language, a context variable, goes along)."""
        template = self.templates.get_template(name)
        body = await asyncio.to_thread(template.render, {"request": request, **ctx})
        return HTMLResponse(body, status_code=status_code, headers=headers)

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
