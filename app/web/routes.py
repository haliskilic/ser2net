"""HTTP routes: pages (login/setup/dashboard/settings) + HTMX/JSON API.

Interaction model (HTMX, no SPA):
  - the mappings panel polls GET /api/status every 2s and also refreshes when a
    response sets the `HX-Trigger: refreshMappings` header,
  - add/edit load a server-rendered form fragment into #form-panel,
  - save/delete/start/stop return an empty body + the refresh trigger.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
from urllib.parse import urlparse

from starlette.responses import (
    JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from ..config import (
    ROLE_RANK, ROLES, ClusterSettings, ConfigError, LdapSettings, MappingConfig,
    OidcSettings, User,
)
from ..engine import netinfo
from . import auth, ldap_auth, oidc_auth
from .api import build_api_routes

OIDC_COOKIE = "ser2net_oidc"

SESSION_TTL = 8 * 3600
_TRIGGER = {"HX-Trigger": "refreshMappings"}


def _mapping_kind_label(m) -> str:
    if m.kind == "serialbridge":
        return "serial↔serial"
    return f"{m.network.mode}/{m.network.protocol}"


def _mapping_endpoint(m) -> str:
    if m.kind == "serialbridge":
        return f"{m.serial.port} ↔ {m.serial_b.port}"
    if m.network.mode == "client":
        return f"→ {m.network.remote_host}:{m.network.remote_port}"
    return f"{m.network.bind_ip}:{m.network.port}"


def build_routes(templates, state, static_dir):
    def render(request, name, status_code=200, headers=None, **ctx):
        return templates.TemplateResponse(request, name, ctx,
                                          status_code=status_code, headers=headers)

    def client_ip(request) -> str:
        ip = request.client.host if request.client else "?"
        # normalize IPv4-mapped IPv6 (::ffff:1.2.3.4 -> 1.2.3.4) so the rate-limit
        # key is consistent on dual-stack listeners
        if ip.startswith("::ffff:") and "." in ip:
            ip = ip[len("::ffff:"):]
        return ip

    def set_session(response, request, user):
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(state.config.secret_key, SESSION_TTL, user.username, user.pwd_version),
            max_age=SESSION_TTL, httponly=True, samesite="lax",
            secure=state.config.admin_ui.tls_enabled, path="/",
        )

    def current_user(request):
        return getattr(request.state, "user", None)

    def require_role(request, role):
        """Return a 403 response if the session user's role is below `role`, else None."""
        u = current_user(request)
        if u is None or ROLE_RANK.get(u.role, 0) < ROLE_RANK[role]:
            return PlainTextResponse(f"Forbidden: requires {role} role.", status_code=403)
        return None

    def can_edit(request) -> bool:
        u = current_user(request)
        return u is not None and ROLE_RANK.get(u.role, 0) >= ROLE_RANK["operator"]

    # ---------------- pages ----------------
    async def healthz(request):
        return PlainTextResponse("ok")

    async def login_get(request):
        return render(request, "login.html", error=None,
                      oidc_enabled=state.config.oidc.enabled)

    # ---------------- OIDC single sign-on ----------------
    def _oidc_redirect_uri(request) -> str:
        cfg_uri = state.config.oidc.redirect_uri.strip()
        if cfg_uri:
            return cfg_uri
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host")
                or request.url.netloc)
        return f"{scheme}://{host}/auth/oidc/callback"

    async def oidc_login(request):
        oidc = state.config.oidc
        if not oidc.enabled:
            return RedirectResponse("/login", status_code=303)
        redirect_uri = _oidc_redirect_uri(request)
        st, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        try:
            url = await asyncio.to_thread(oidc_auth.build_authorize_url, oidc, redirect_uri, st, nonce)
        except Exception as e:
            state.log(f"OIDC login init failed: {e}")
            return render(request, "login.html", status_code=502, error=None,
                          oidc_enabled=True, oidc_error="SSO provider is unreachable.")
        resp = RedirectResponse(url, status_code=303)
        resp.set_cookie(OIDC_COOKIE,
                        auth.sign_payload(state.config.secret_key,
                                          {"s": st, "n": nonce, "r": redirect_uri}, ttl_seconds=600),
                        max_age=600, httponly=True, samesite="lax",
                        secure=state.config.admin_ui.tls_enabled, path="/auth/oidc")
        return resp

    async def oidc_callback(request):
        oidc = state.config.oidc
        if not oidc.enabled:
            return RedirectResponse("/login", status_code=303)
        data = auth.read_payload(state.config.secret_key, request.cookies.get(OIDC_COOKIE))
        code = request.query_params.get("code", "")
        st = request.query_params.get("state", "")
        if not data or not code or not secrets.compare_digest(st, data.get("s", "")):
            return render(request, "login.html", status_code=400, error=None, oidc_enabled=True,
                          oidc_error="SSO state mismatch — please try again.")
        claims = await asyncio.to_thread(oidc_auth.complete, oidc, data["r"], code, data["n"], state.log)
        if claims is None:
            return render(request, "login.html", status_code=401, error=None, oidc_enabled=True,
                          oidc_error="SSO sign-in failed.")
        username = oidc_auth.username_from_claims(claims, oidc)
        role = oidc_auth.role_from_claims(claims, oidc)
        if not username or not role:
            state.log(f"OIDC login denied: user={username!r} role={role!r} (no mapped group)")
            return render(request, "login.html", status_code=403, error=None, oidc_enabled=True,
                          oidc_error="Your account isn't mapped to a role.")
        async with state.config_lock:
            user = state.config.upsert_external_user(username, role, "oidc")
            await state.asave()
        state.log(f"login: {username} ({role}, oidc) from {client_ip(request)}")
        state.audit(client_ip(request), "oidc_login", username)
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, request, user)
        resp.delete_cookie(OIDC_COOKIE, path="/auth/oidc")
        return resp

    async def login_post(request):
        ip = client_ip(request)
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        if state.rate_limiter.blocked(ip):
            return render(request, "login.html", status_code=429,
                          error="Too many attempts. Wait a few minutes and try again.",
                          oidc_enabled=state.config.oidc.enabled)
        # username defaults to "admin" so single-user (password-only) logins keep working
        username = (form.get("username") or "admin").strip() or "admin"
        password = form.get("password", "")
        user = state.config.get_user(username)
        authed = None
        # local account: verify the scrypt hash off the event loop (it's slow on purpose)
        if user is not None and user.source == "local":
            if await asyncio.to_thread(auth.verify_password, password, user.password_hash):
                authed = user
        # LDAP/AD: for unknown users (or LDAP shadow accounts) when LDAP is enabled
        elif state.config.ldap.enabled:
            groups = await asyncio.to_thread(
                ldap_auth.authenticate, state.config.ldap, username, password, state.log)
            if groups is not None:
                role = ldap_auth.role_for_groups(groups, state.config.ldap)
                if role:
                    async with state.config_lock:
                        authed = ldap_auth.upsert_ldap_user(state.config, username, role)
                        await state.asave()
                else:
                    state.log(f"LDAP login denied for {username!r}: in no mapped group")
        if authed is not None:
            state.rate_limiter.reset(ip)
            state.log(f"login: {username} ({authed.role}, {authed.source}) from {ip}")
            resp = RedirectResponse("/", status_code=303)
            set_session(resp, request, authed)
            return resp
        state.rate_limiter.record_failure(ip)
        state.log(f"failed login for {username!r} from {ip}")
        return render(request, "login.html", status_code=401, error="Invalid username or password.",
                      oidc_enabled=state.config.oidc.enabled)

    async def logout_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp

    async def setup_get(request):
        return render(request, "setup.html", error=None,
                      ips=netinfo.list_ip_candidates(),
                      admin=state.config.admin_ui)

    async def setup_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        username = (form.get("username") or "admin").strip() or "admin"
        pw = form.get("password", "")
        pw2 = form.get("password2", "")
        err = _username_problem(username) or _password_problem(pw, pw2)
        if err:
            return render(request, "setup.html", status_code=400, error=err,
                          ips=netinfo.list_ip_candidates(), admin=state.config.admin_ui)
        pw_hash = await asyncio.to_thread(auth.hash_password, pw)
        async with state.config_lock:
            state.config.users = [User(username=username, password_hash=pw_hash,
                                       role="admin", pwd_version=1)]
            await state.asave()
        user = state.config.get_user(username)
        state.log(f"first-run setup complete; admin user '{username}' created")
        state.audit(client_ip(request), "first_run_setup", username)
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, request, user)
        return resp

    async def dashboard(request):
        return render(
            request, "dashboard.html",
            mappings=state.config.mappings,
            statuses=state.supervisor.all_status(),
            admin=state.config.admin_ui,
            cluster_on=state.config.cluster.active,
            me=current_user(request), can_edit=can_edit(request),
        )

    async def settings_get(request, ok=None, error=None, new_api_token=None):
        return render(request, "settings.html", ok=ok, error=error,
                      admin=state.config.admin_ui,
                      api_token_set=bool(state.config.api_token_hash),
                      api_token_role=state.config.api_token_role,
                      new_api_token=new_api_token,
                      me=current_user(request), users=state.config.users, roles=ROLES,
                      ldap=state.config.ldap, ldap_lib=_module_present("ldap3"),
                      oidc=state.config.oidc, oidc_lib=_module_present("authlib"),
                      cluster=state.config.cluster, instance_id=state.config.instance_id,
                      uptime=int(state.started_at))

    async def settings_password_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        user = current_user(request)
        if user is not None and user.source != "local":
            return await settings_get(request,
                                      error=f"Your account is managed by {user.source.upper()}; "
                                            "change your password there.")
        cur = form.get("current", "")
        new = form.get("password", "")
        new2 = form.get("password2", "")
        if user is None or not await asyncio.to_thread(auth.verify_password, cur, user.password_hash):
            return await settings_get(request, error="Current password is incorrect.")
        err = _password_problem(new, new2)
        if err:
            return await settings_get(request, error=err)
        new_hash = await asyncio.to_thread(auth.hash_password, new)
        async with state.config_lock:
            user.password_hash = new_hash
            user.pwd_version += 1
            await state.asave()
        state.log(f"password changed for {user.username}; their other sessions signed out")
        state.audit(client_ip(request), "password_change", user.username)
        # refresh THIS session so the user isn't logged out by their own change
        resp = await settings_get(request, ok="Password updated. Your other sessions were signed out.")
        set_session(resp, request, user)
        return resp

    async def settings_tls_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        cert, key = form.get("tls_cert", "").strip(), form.get("tls_key", "").strip()
        async with state.config_lock:
            old = (state.config.admin_ui.tls_cert, state.config.admin_ui.tls_key)
            state.config.admin_ui.tls_cert = cert
            state.config.admin_ui.tls_key = key
            try:
                await state.asave()
            except ConfigError as e:
                state.config.admin_ui.tls_cert, state.config.admin_ui.tls_key = old
                return await settings_get(request, error=str(e))
        state.audit(client_ip(request), "admin_tls_set", cert)
        return await settings_get(request, ok="Admin TLS saved. Restart to apply.")

    async def settings_tls_generate(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        tdir = os.path.join(state.data_dir, "tls")
        os.makedirs(tdir, exist_ok=True)
        cert, key = os.path.join(tdir, "cert.pem"), os.path.join(tdir, "key.pem")
        try:
            await asyncio.to_thread(subprocess.run, [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key, "-out", cert, "-days", "825", "-subj", "/CN=ser2net",
            ], check=True, capture_output=True, timeout=30)
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            return await settings_get(request, error=f"openssl not available or failed: {e}")
        async with state.config_lock:
            state.config.admin_ui.tls_cert = cert
            state.config.admin_ui.tls_key = key
            await state.asave()
        state.audit(client_ip(request), "admin_tls_generate", "")
        return await settings_get(request, ok="Self-signed certificate generated. Restart to apply TLS.")

    # ---------------- REST API token ----------------
    async def settings_api_token_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        role = form.get("api_token_role", "admin")
        if role not in ROLES:
            role = "admin"
        token = auth.new_api_token()
        async with state.config_lock:
            state.config.api_token_hash = auth.hash_token(token)
            state.config.api_token_role = role
            await state.asave()
        state.log(f"REST API token generated (role: {role})")
        state.audit(client_ip(request), "api_token_generate", role)
        # show the token exactly once — only its hash is stored
        return await settings_get(request, new_api_token=token,
                                  ok=f"New API token generated (role: {role}). "
                                     "Copy it now — it is not shown again.")

    async def settings_api_token_revoke(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        async with state.config_lock:
            state.config.api_token_hash = ""
            await state.asave()
        state.log("REST API token revoked")
        state.audit(client_ip(request), "api_token_revoke", "")
        return await settings_get(request, ok="API token revoked. The REST API is now disabled.")

    # ---------------- LDAP / AD settings (admin only) ----------------
    async def settings_ldap_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        async with state.config_lock:
            old = state.config.ldap
            state.config.ldap = _ldap_from_form(form, old)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.ldap = old
                return await settings_get(request, error=str(e))
        state.log(f"LDAP settings updated (enabled={state.config.ldap.enabled})")
        state.audit(client_ip(request), "ldap_settings",
                    "enabled" if state.config.ldap.enabled else "disabled")
        return await settings_get(request, ok="LDAP settings saved.")

    async def settings_oidc_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        async with state.config_lock:
            old = state.config.oidc
            state.config.oidc = _oidc_from_form(form, old)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.oidc = old
                return await settings_get(request, error=str(e))
        state.log(f"OIDC settings updated (enabled={state.config.oidc.enabled})")
        state.audit(client_ip(request), "oidc_settings",
                    "enabled" if state.config.oidc.enabled else "disabled")
        return await settings_get(request, ok="OIDC (SSO) settings saved.")

    # ---------------- user management (admin only) ----------------
    async def settings_users_create(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        username = (form.get("username") or "").strip()
        role = form.get("role", "viewer")
        pw, pw2 = form.get("password", ""), form.get("password2", "")
        err = (_username_problem(username)
               or (None if role in ROLES else "Invalid role.")
               or _password_problem(pw, pw2))
        if not err and state.config.get_user(username):
            err = f"User '{username}' already exists."
        if err:
            return await settings_get(request, error=err)
        pw_hash = await asyncio.to_thread(auth.hash_password, pw)
        async with state.config_lock:
            state.config.users.append(User(username=username, password_hash=pw_hash,
                                           role=role, pwd_version=1))
            await state.asave()
        state.log(f"user created: {username} ({role})")
        state.audit(client_ip(request), "user_create", f"{username}:{role}")
        return await settings_get(request, ok=f"User '{username}' created.")

    async def settings_users_role(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        username = request.path_params["username"]
        role = form.get("role", "viewer")
        target = state.config.get_user(username)
        if target is None:
            return await settings_get(request, error="User not found.")
        if role not in ROLES:
            return await settings_get(request, error="Invalid role.")
        if target.role == "admin" and role != "admin" and state.config.admin_count() <= 1:
            return await settings_get(request, error="Cannot demote the last admin.")
        async with state.config_lock:
            target.role = role
            target.pwd_version += 1  # force re-login so the new role applies everywhere
            await state.asave()
        state.log(f"user role changed: {username} -> {role}")
        state.audit(client_ip(request), "user_role", f"{username}:{role}")
        return await settings_get(request, ok=f"Role updated for '{username}'. They must sign in again.")

    async def settings_users_delete(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        username = request.path_params["username"]
        target = state.config.get_user(username)
        if target is None:
            return await settings_get(request, error="User not found.")
        me = current_user(request)
        if me and me.username == username:
            return await settings_get(request, error="You cannot delete your own account.")
        if target.role == "admin" and state.config.admin_count() <= 1:
            return await settings_get(request, error="Cannot delete the last admin.")
        async with state.config_lock:
            state.config.users = [u for u in state.config.users if u.username != username]
            await state.asave()
        state.log(f"user deleted: {username}")
        state.audit(client_ip(request), "user_delete", username)
        return await settings_get(request, ok=f"User '{username}' deleted.")

    # ---------------- config export / import ----------------
    async def config_export(request):
        # mappings only — never export password_hash / secret_key
        data = {"version": 1, "mappings": [m.to_dict() for m in state.config.mappings]}
        return Response(
            json.dumps(data, indent=2), media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=ser2net-mappings.json"})

    async def config_import(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        upload = form.get("file")
        try:
            raw = await upload.read() if hasattr(upload, "read") else (upload or "")
            doc = json.loads(raw)
            new_maps = [MappingConfig.from_dict(x) for x in doc.get("mappings", [])]
        except (AttributeError, ValueError, TypeError) as e:
            return await settings_get(request, error=f"Import failed: invalid file ({e}).")
        async with state.config_lock:
            backup = state.config.mappings
            state.config.mappings = new_maps
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return await settings_get(request, error=f"Import rejected: {e}")
        await state.supervisor.stop_all()
        await state.supervisor.start_all(state.config)
        state.audit(client_ip(request), "config_import", f"{len(new_maps)} mappings")
        return await settings_get(request, ok=f"Imported {len(new_maps)} mappings (replaced existing).")

    async def mapping_duplicate(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if not m:
                return PlainTextResponse("Mapping not found", status_code=404)
            d = m.to_dict()
            d.pop("id", None)
            d["name"] = f"{m.name} (copy)"
            d["enabled"] = False  # don't auto-start a duplicate (avoids port clash)
            dup = MappingConfig.from_dict(d)
            # bump a listening port to the next free one
            if dup.kind == "net" and dup.network.listens:
                used = {mm.network.port for mm in state.config.mappings if mm.kind == "net"}
                while dup.network.port in used and dup.network.port < 65535:
                    dup.network.port += 1
            state.config.mappings.append(dup)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings.remove(dup)
                return PlainTextResponse(f"Duplicate failed: {e}", status_code=400)
        state.audit(client_ip(request), "mapping_duplicate", dup.name)
        return Response("", headers=_TRIGGER)

    # ---------------- console (xterm over WebSocket) ----------------
    async def console_ws(websocket):
        cfg = state.config
        # BaseHTTPMiddleware does NOT run for WebSocket scope, so authenticate here.
        token = websocket.cookies.get(auth.SESSION_COOKIE)
        ws_user = auth.session_user(cfg, token)
        if ws_user is None:
            await websocket.close(code=1008)
            return
        # CSWSH guard: cross-origin WebSocket is rejected.
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=1008)
            return
        mid = websocket.path_params["mid"]
        runner = state.supervisor.get_runner(mid)
        mapping = cfg.get_mapping(mid)
        if runner is None or mapping is None:
            await websocket.close(code=1011)
            return

        await websocket.accept()
        mon = _Monitor()
        runner.add_monitor(mon)
        # viewers get an observe-only console; writing to the device needs operator+
        can_write = ROLE_RANK.get(ws_user.role, 0) >= ROLE_RANK["operator"]
        interactive = (mapping.kind == "net" and not mapping.network.read_only and can_write)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] console opened by {ws_user.username} ({peer})")

        async def sender():
            while True:
                data = await mon.queue.get()
                if data is None:  # mapping stopped
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    return
                await websocket.send_bytes(data)

        async def receiver():
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                if data is None and msg.get("text") is not None:
                    data = msg["text"].encode("utf-8", "replace")
                if data and interactive:
                    await runner.serial_write(data)

        st = asyncio.create_task(sender())
        rt = asyncio.create_task(receiver())
        try:
            await asyncio.wait({st, rt}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (st, rt):
                t.cancel()
            for t in (st, rt):
                with contextlib.suppress(BaseException):
                    await t
            runner.discard_monitor(mon)
            with contextlib.suppress(Exception):
                await websocket.close()
            state.log(f"[{mapping.name}] console closed ({peer})")

    # ---------------- status / ports / ips ----------------
    async def metrics(request):
        statuses = state.supervisor.all_status()
        lines = [
            "# ser2net metrics",
            "ser2net_up 1",
            f"ser2net_mappings_total {len(state.config.mappings)}",
        ]
        for m in state.config.mappings:
            st = statuses.get(m.id) or {}
            lbl = f'mapping="{_metric_label(m.name)}",id="{m.id}"'
            running = 1 if st.get("state") == "running" else 0
            lines += [
                f"ser2net_mapping_running{{{lbl}}} {running}",
                f"ser2net_mapping_clients{{{lbl}}} {st.get('client_count', 0)}",
                f"ser2net_mapping_bytes_in_total{{{lbl}}} {st.get('bytes_in', 0)}",
                f"ser2net_mapping_bytes_out_total{{{lbl}}} {st.get('bytes_out', 0)}",
                f"ser2net_mapping_reconnects_total{{{lbl}}} {st.get('reconnects', 0)}",
                f"ser2net_mapping_dropped_clients_total{{{lbl}}} {st.get('dropped_clients', 0)}",
            ]
        return PlainTextResponse("\n".join(lines) + "\n",
                                 media_type="text/plain; version=0.0.4")

    async def api_status(request):
        return render(request, "_mappings_body.html",
                      mappings=state.config.mappings,
                      statuses=state.supervisor.all_status(),
                      can_edit=can_edit(request))

    # ---------------- LAN cluster (federated read-only view) ----------------
    def local_node_payload() -> dict:
        """This node's identity + a status row per mapping — the unit other nodes
        aggregate. No secrets: just what the unified dashboard table shows."""
        cfg = state.config
        ip, port, scheme = state.cluster.advertised()
        statuses = state.supervisor.all_status()
        maps = []
        for m in cfg.mappings:
            st = statuses.get(m.id) or {}
            maps.append({
                "id": m.id, "name": m.name, "enabled": m.enabled,
                "kind": _mapping_kind_label(m), "endpoint": _mapping_endpoint(m),
                "serial": m.serial.port,
                "state": st.get("state", "stopped"),
                "client_count": st.get("client_count", 0),
                "bytes_in": st.get("bytes_in", 0), "bytes_out": st.get("bytes_out", 0),
            })
        return {"id": cfg.instance_id, "name": socket.gethostname(),
                "ip": ip, "port": port, "scheme": scheme, "mappings": maps}

    async def cluster_local(request):
        """Peer-facing: returns this node's mappings to another cluster node that
        presents the matching shared key. Guarded by the key, not a user session
        (added to PUBLIC_PATHS) — read-only, exposes no credentials."""
        cfg = state.config
        key = request.headers.get("x-cluster-key", "")
        if not (cfg.cluster.active and key and hmac.compare_digest(key, cfg.cluster.key)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(local_node_payload())

    async def cluster_status(request):
        """Aggregated view for the browser: local node + every discovered peer,
        fetched server-side with the shared key. Renders the unified table."""
        if not state.config.cluster.active:
            return render(request, "_cluster_body.html", nodes=[])
        local = local_node_payload()
        nodes = [{**local, "self": True, "online": True}]
        peers = state.cluster.peers()
        results = await asyncio.gather(*[state.cluster.fetch_peer(p) for p in peers]) \
            if peers else []
        for p, data in zip(peers, results, strict=False):
            if data:
                nodes.append({
                    "id": data.get("id", p["id"]), "name": data.get("name", p["name"]),
                    "ip": data.get("ip", p["ip"]), "port": data.get("port", p["port"]),
                    "scheme": data.get("scheme", p["scheme"]),
                    "self": False, "online": True, "mappings": data.get("mappings", []),
                })
            else:
                nodes.append({"id": p["id"], "name": p["name"], "ip": p["ip"],
                              "port": p["port"], "scheme": p["scheme"],
                              "self": False, "online": False, "mappings": []})
        return render(request, "_cluster_body.html", nodes=nodes)

    async def settings_cluster_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        try:
            port = int(form.get("discovery_port") or 41750)
        except ValueError:
            return await settings_get(request, error="Cluster discovery port must be a number.")
        new = ClusterSettings(
            enabled=form.get("enabled") == "on",
            key=(form.get("key") or "").strip(),
            discovery_port=port,
            advertise_ip=(form.get("advertise_ip") or "").strip(),
        )
        async with state.config_lock:
            old = state.config.cluster
            state.config.cluster = new
            try:
                await state.asave()
            except ConfigError as e:
                state.config.cluster = old
                return await settings_get(request, error=str(e))
        # apply live: restart discovery so a key/port/enable change takes effect now
        await state.cluster.stop()
        await state.cluster.start()
        state.log(f"cluster settings updated (enabled={new.enabled})")
        state.audit(client_ip(request), "cluster_settings",
                    "enabled" if new.active else "disabled")
        return await settings_get(request, ok="Cluster settings saved.")

    async def api_ports_json(request):
        return JSONResponse(state.ports.get())

    async def api_ports_refresh(request):
        ports = await state.ports.refresh_now()
        selected = request.query_params.get("selected", "")
        return render(request, "_serial_port_select.html", ports=ports, selected=selected)

    async def api_ports_table(request):
        return render(request, "_ports_table.html",
                      ports=state.ports.get(),
                      in_use=state.supervisor.devices_in_use())

    # ---------------- mapping form ----------------
    async def mapping_form_new(request):
        m = MappingConfig(name="")
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=True, modbus_points_json=_modbus_points_json(m),
                      ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_form_edit(request):
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=False, modbus_points_json=_modbus_points_json(m),
                      ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_log(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        lines = await asyncio.to_thread(state.read_mapping_log, mid, 1000)
        return render(request, "_mapping_log.html", mid=mid, mapping_name=m.name,
                      lines=lines)

    async def console_view(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        interactive = (m.kind == "net" and not m.network.read_only)
        return render(request, "_console.html", mid=mid, mapping_name=m.name,
                      interactive=interactive)

    async def mapping_save(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        try:
            data = _mapping_from_form(form)
            mapping = MappingConfig.from_dict(data)
            # The form doesn't expose every field; when editing, carry the unexposed
            # ones over so a save never silently wipes them (get_mapping is a sync
            # read — safe outside the config lock).
            _preserve_unmanaged_fields(mapping, state.config.get_mapping(mapping.id))
            mapping.validate()
        except (ValueError, ConfigError) as e:
            return _form_error(render, request, state, form, str(e))

        # Hold the lock only to mutate + persist config; release it BEFORE the
        # (potentially slow) supervisor call so other admin requests aren't blocked
        # while a serial port is being (re)opened.
        async with state.config_lock:
            backup = list(state.config.mappings)
            existing = state.config.get_mapping(mapping.id)
            if existing:
                state.config.mappings[state.config.mappings.index(existing)] = mapping
            else:
                state.config.mappings.append(mapping)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return _form_error(render, request, state, form, str(e))
        await state.supervisor.apply_mapping(mapping)
        state.log(f"mapping saved: {mapping.name}")
        state.audit(client_ip(request), "mapping_save", mapping.name)
        return Response("", headers=_TRIGGER)

    async def mapping_delete(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        removed = None
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if m:
                state.config.mappings.remove(m)
                await state.asave()
                removed = m
        if removed is not None:
            await state.supervisor.remove_mapping(mid)
            state.log(f"mapping deleted: {removed.name}")
            state.audit(client_ip(request), "mapping_delete", removed.name)
            state.delete_mapping_log(mid)
        return Response("", headers=_TRIGGER)

    async def mapping_action(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse("Unknown action", status_code=400)
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if not m:
                return PlainTextResponse("Mapping not found", status_code=404)
            m.enabled = action != "stop"
            await state.asave()
        if action == "stop":
            await state.supervisor.stop_mapping(mid)
        elif action == "restart":
            await state.supervisor.restart_mapping(m)
        else:  # start
            await state.supervisor.apply_mapping(m)
        state.log(f"mapping {action}: {m.name}")
        state.audit(client_ip(request), f"mapping_{action}", m.name)
        return Response("", headers=_TRIGGER)

    routes = [
        Route("/", dashboard),
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout_post, methods=["POST"]),
        Route("/auth/oidc/login", oidc_login, methods=["GET"]),
        Route("/auth/oidc/callback", oidc_callback, methods=["GET"]),
        Route("/setup", setup_get, methods=["GET"]),
        Route("/setup", setup_post, methods=["POST"]),
        Route("/settings", settings_get, methods=["GET"]),
        Route("/settings/password", settings_password_post, methods=["POST"]),
        Route("/settings/tls", settings_tls_post, methods=["POST"]),
        Route("/settings/tls/generate", settings_tls_generate, methods=["POST"]),
        Route("/settings/api-token", settings_api_token_post, methods=["POST"]),
        Route("/settings/api-token/revoke", settings_api_token_revoke, methods=["POST"]),
        Route("/settings/users", settings_users_create, methods=["POST"]),
        Route("/settings/users/{username}/role", settings_users_role, methods=["POST"]),
        Route("/settings/users/{username}/delete", settings_users_delete, methods=["POST"]),
        Route("/settings/ldap", settings_ldap_post, methods=["POST"]),
        Route("/settings/oidc", settings_oidc_post, methods=["POST"]),
        Route("/settings/cluster", settings_cluster_post, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/metrics", metrics),
        Route("/settings/config/export", config_export),
        Route("/settings/config/import", config_import, methods=["POST"]),
        # JSON REST API (bearer-token auth, enforced in GuardMiddleware)
        *build_api_routes(state),
        Route("/api/mappings/{mid}/duplicate", mapping_duplicate, methods=["POST"]),
        WebSocketRoute("/api/mappings/{mid}/console", console_ws),
        Route("/api/status", api_status),
        Route("/api/cluster/status", cluster_status),
        Route("/api/cluster/local", cluster_local),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/refresh", api_ports_refresh, methods=["POST"]),
        Route("/api/ports/table", api_ports_table),
        Route("/api/mappings/form", mapping_form_new),
        Route("/api/mappings/{mid}/form", mapping_form_edit),
        Route("/api/mappings/{mid}/log", mapping_log),
        Route("/api/mappings/{mid}/console-view", console_view),
        Route("/api/mappings/save", mapping_save, methods=["POST"]),
        Route("/api/mappings/{mid}", mapping_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/{action}", mapping_action, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=static_dir), name="static"),
    ]
    return routes


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _metric_label(name: str) -> str:
    """Escape a mapping name for a Prometheus label value."""
    return name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Monitor:
    """A browser console observer: serial traffic is pushed here (best-effort —
    drops oldest on overflow so a slow viewer never stalls the bridge)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    def feed(self, data: bytes) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            with contextlib.suppress(Exception):
                self.queue.get_nowait()
                self.queue.put_nowait(data)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.queue.put_nowait(None)  # sentinel: tells the sender to close


def _password_problem(pw: str, pw2: str) -> str | None:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "Passwords do not match."
    return None


def _module_present(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _ldap_from_form(form, old) -> LdapSettings:
    pw = form.get("ldap_bind_password", "")
    return LdapSettings.from_dict({
        "enabled": _checkbox(form, "ldap_enabled"),
        "server_uri": form.get("ldap_server_uri", "").strip(),
        "start_tls": _checkbox(form, "ldap_start_tls"),
        "user_dn_template": form.get("ldap_user_dn_template", "").strip(),
        "bind_dn": form.get("ldap_bind_dn", "").strip(),
        "bind_password": pw if pw else old.bind_password,   # blank => keep the stored one
        "user_search_base": form.get("ldap_user_search_base", "").strip(),
        "user_search_filter": form.get("ldap_user_search_filter", "").strip() or "(uid={username})",
        "group_attr": form.get("ldap_group_attr", "").strip() or "memberOf",
        "admin_group": form.get("ldap_admin_group", "").strip(),
        "operator_group": form.get("ldap_operator_group", "").strip(),
        "viewer_group": form.get("ldap_viewer_group", "").strip(),
        "default_role": form.get("ldap_default_role", "").strip(),
    })


def _oidc_from_form(form, old) -> OidcSettings:
    sec = form.get("oidc_client_secret", "")
    return OidcSettings.from_dict({
        "enabled": _checkbox(form, "oidc_enabled"),
        "issuer": form.get("oidc_issuer", "").strip(),
        "client_id": form.get("oidc_client_id", "").strip(),
        "client_secret": sec if sec else old.client_secret,   # blank => keep stored
        "redirect_uri": form.get("oidc_redirect_uri", "").strip(),
        "scopes": form.get("oidc_scopes", "").strip() or "openid email profile",
        "username_claim": form.get("oidc_username_claim", "").strip() or "preferred_username",
        "groups_claim": form.get("oidc_groups_claim", "").strip() or "groups",
        "admin_group": form.get("oidc_admin_group", "").strip(),
        "operator_group": form.get("oidc_operator_group", "").strip(),
        "viewer_group": form.get("oidc_viewer_group", "").strip(),
        "default_role": form.get("oidc_default_role", "").strip(),
    })


def _username_problem(username: str) -> str | None:
    if not username:
        return "Username is required."
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", username):
        return "Username must be 1-32 characters: letters, digits, '.', '_' or '-'."
    return None


def _checkbox(form, name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def _split(s: str) -> list:
    return [x for x in re.split(r"[\s,]+", s or "") if x]


def _num(form, name, default, *, strict, cast=int):
    v = form.get(name, default)
    if strict:
        return cast(v)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _serial_dict(form, prefix: str, strict: bool) -> dict:
    baud = form.get(prefix + "baudrate", "9600")
    if baud == "custom":
        baud = form.get(prefix + "custom_baud", "").strip()
    return {
        "port": form.get(prefix + "port", "").strip(),
        "baudrate": (int(baud) if strict else _num({"b": baud}, "b", 9600, strict=False)),
        "bytesize": _num(form, prefix + "bytesize", 8, strict=strict),
        "parity": form.get(prefix + "parity", "N"),
        "stopbits": _num(form, prefix + "stopbits", 1, strict=strict, cast=float),
        "flowcontrol": form.get(prefix + "flowcontrol", "none"),
        "rts_on_open": form.get(prefix + "rts_on_open", "keep"),
        "dtr_on_open": form.get(prefix + "dtr_on_open", "keep"),
        "exclusive": _checkbox(form, prefix + "exclusive"),
    }


def _build_mapping_dict(form, strict: bool) -> dict:
    bind = form.get("network_bind_ip", "0.0.0.0")
    if bind == "custom":
        bind = form.get("network_bind_ip_custom", "").strip()
    return {
        "id": form.get("id") or None,
        "name": (form.get("name", "").strip() if strict else form.get("name", "")),
        "enabled": _checkbox(form, "enabled"),
        "kind": form.get("kind", "net"),
        "serial": _serial_dict(form, "serial_", strict),
        "serial_b": _serial_dict(form, "serial_b_", strict),
        "network": {
            "mode": form.get("network_mode", "server"),
            "protocol": form.get("network_protocol", "raw"),
            "bind_ip": bind or "0.0.0.0",
            "port": _num(form, "network_port", 4001, strict=strict),
            "remote_host": form.get("network_remote_host", "").strip(),
            "remote_port": (_num(form, "network_remote_port", 0, strict=strict)
                            if form.get("network_remote_port") else 0),
            "max_connections": _num(form, "network_max_connections", 1, strict=strict),
            "kick_old_user": _checkbox(form, "network_kick_old_user"),
            "read_only": _checkbox(form, "network_read_only"),
            "allowed_client_ips": _split(form.get("network_allowed_client_ips", "")),
            "priority_client_ips": _split(form.get("network_priority_client_ips", "")),
            "client_queue_max": _num(form, "network_client_queue_max", 2048, strict=strict),
            "tls": _checkbox(form, "network_tls"),
            "tls_cert": form.get("network_tls_cert", "").strip(),
            "tls_key": form.get("network_tls_key", "").strip(),
        },
        "options": {
            "banner": form.get("opt_banner", ""),
            "idle_timeout_s": _num(form, "opt_idle_timeout_s", 0, strict=strict),
            "closeon": form.get("opt_closeon", ""),
            "trace_both": form.get("opt_trace_both", "").strip(),
            "trace_hexdump": _checkbox(form, "opt_trace_hexdump"),
        },
        "mqtt": {
            "enabled": _checkbox(form, "mqtt_enabled"),
            "host": form.get("mqtt_host", "").strip(),
            "port": _num(form, "mqtt_port", 1883, strict=strict),
            "base_topic": form.get("mqtt_base_topic", "").strip(),
            "qos": _num(form, "mqtt_qos", 0, strict=strict),
            "tls": _checkbox(form, "mqtt_tls"),
            "username": form.get("mqtt_username", "").strip(),
            "password": form.get("mqtt_password", ""),
            "client_id": form.get("mqtt_client_id", "").strip(),
        },
        "modbus_poll": {
            "interval_s": _num(form, "modbus_poll_interval_s", 5.0, strict=False, cast=float),
            "points": _parse_modbus_points(form.get("modbus_points", ""), strict),
        },
    }


def _modbus_points_json(mapping) -> str:
    pts = [{"name": p.name, "unit": p.unit, "fn": p.fn, "address": p.address,
            "dtype": p.dtype, "scale": p.scale} for p in mapping.modbus_poll.points]
    return json.dumps(pts, indent=2) if pts else ""


def _parse_modbus_points(raw: str, strict: bool) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        points = json.loads(raw)
    except (ValueError, TypeError):
        if strict:
            raise ValueError("Modbus poll points must be a valid JSON array of objects.") from None
        return []
    return points if isinstance(points, list) else []


def _mapping_from_form(form) -> dict:
    return _build_mapping_dict(form, strict=True)


def _preserve_unmanaged_fields(new_map, existing) -> None:
    """Carry over config fields the mapping form does not expose, so editing an
    existing mapping never silently discards them: the stable-id `match`, the
    advanced/RS-485 serial settings, open/close strings, the trace-timestamp flag
    and the RFC2217 interop knobs. No-op when creating a new mapping."""
    if existing is None:
        return
    for cur, old in ((new_map.serial, existing.serial),
                     (new_map.serial_b, existing.serial_b)):
        cur.match = old.match
        cur.advanced = old.advanced
    o, prev = new_map.options, existing.options
    o.openstr = prev.openstr
    o.closestr = prev.closestr
    o.trace_timestamp = prev.trace_timestamp
    o.rfc2217_poll_modem_interval_s = prev.rfc2217_poll_modem_interval_s
    o.rfc2217_net_timeout_s = prev.rfc2217_net_timeout_s
    o.modbus_response_timeout_s = prev.modbus_response_timeout_s


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner."""
    from ..engine import netinfo
    try:
        mapping = MappingConfig.from_dict(_build_mapping_dict(form, strict=False))
    except Exception:
        mapping = MappingConfig(name=form.get("name", ""))
    is_new = not form.get("id")
    return render(request, "_mapping_form.html", status_code=400,
                  mapping=mapping, is_new=is_new,
                  modbus_points_json=_modbus_points_json(mapping),
                  ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                  error=message)
