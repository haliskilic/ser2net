"""The Settings page and its forms: password, web-UI access, users, LDAP, OIDC,
cluster, admin TLS, REST API token and mappings export/import."""
from __future__ import annotations

import asyncio
import json
import os

from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ...config import ROLES, ClusterSettings, ConfigError, MappingConfig, User
from .. import auth, selfsigned
from .common import Ctx
from .forms import (
    _checkbox, _ldap_from_form, _module_present, _oidc_from_form, _password_problem,
    _username_problem,
)


def routes(ctx: Ctx) -> list:
    state, render, client_ip = ctx.state, ctx.render, ctx.client_ip
    current_user, require_role, set_session = ctx.current_user, ctx.require_role, ctx.set_session

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
                      cluster_peers_text="\n".join(state.config.cluster.peers),
                      uptime=int(state.started_at))

    async def settings_password_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        user = current_user(request)
        if user is not None and user.source == "anonymous":
            return await settings_get(request, error="Sign in to change your password.")
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
            await asyncio.to_thread(selfsigned.generate, cert, key)
        except (selfsigned.CertError, OSError) as e:
            return await settings_get(request, error=f"Could not generate a certificate: {e}")
        async with state.config_lock:
            state.config.admin_ui.tls_cert = cert
            state.config.admin_ui.tls_key = key
            await state.asave()
        state.audit(client_ip(request), "admin_tls_generate", "")
        return await settings_get(request, ok="Self-signed certificate generated. Restart to apply TLS.")

    # ---------------- web-UI access (login requirement) ----------------
    async def settings_access_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        require_login = _checkbox(form, "require_login")
        role = form.get("anonymous_role", "viewer")
        if role not in ROLES:
            return await settings_get(request, error="Invalid role for visitors without login.")
        ui = state.config.admin_ui
        async with state.config_lock:
            old = (ui.require_login, ui.anonymous_role)
            ui.require_login, ui.anonymous_role = require_login, role
            try:
                await state.asave()
            except ConfigError as e:
                ui.require_login, ui.anonymous_role = old
                return await settings_get(request, error=str(e))
        if require_login:
            state.log("web UI login requirement ENABLED (every visitor must sign in)")
            ok = "Login is required again for the web UI."
        else:
            state.log(f"WARNING: web UI login requirement DISABLED — visitors without a "
                      f"session get the '{role}' role (by IP / localhost / host name)")
            ok = (f"Open access enabled: visitors without a login now get the '{role}' role. "
                  "Use this only on a trusted network.")
        state.audit(client_ip(request), "web_access",
                    "login-required" if require_login else f"open:{role}")
        return await settings_get(request, ok=ok)

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
        # mappings only — never export password_hash / secret_key. Mappings can carry
        # secrets (MQTT password), so — like import — this needs operator+ (the UI only
        # offers it to operators; a viewer, possibly an anonymous one, must not read them).
        if deny := require_role(request, "operator"):
            return deny
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
        peers = [ln.strip() for ln in (form.get("peers") or "").splitlines() if ln.strip()]
        new = ClusterSettings(
            enabled=form.get("enabled") == "on",
            key=(form.get("key") or "").strip(),
            discovery_port=port,
            advertise_ip=(form.get("advertise_ip") or "").strip(),
            peers=peers,
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
        state.log(f"cluster settings updated (enabled={new.enabled}, manual_peers={len(peers)})")
        state.audit(client_ip(request), "cluster_settings",
                    "enabled" if new.active else "disabled")
        return await settings_get(request, ok="Cluster settings saved.")

    return [
        Route("/settings", settings_get, methods=["GET"]),
        Route("/settings/password", settings_password_post, methods=["POST"]),
        Route("/settings/tls", settings_tls_post, methods=["POST"]),
        Route("/settings/tls/generate", settings_tls_generate, methods=["POST"]),
        Route("/settings/access", settings_access_post, methods=["POST"]),
        Route("/settings/api-token", settings_api_token_post, methods=["POST"]),
        Route("/settings/api-token/revoke", settings_api_token_revoke, methods=["POST"]),
        Route("/settings/users", settings_users_create, methods=["POST"]),
        Route("/settings/users/{username}/role", settings_users_role, methods=["POST"]),
        Route("/settings/users/{username}/delete", settings_users_delete, methods=["POST"]),
        Route("/settings/ldap", settings_ldap_post, methods=["POST"]),
        Route("/settings/oidc", settings_oidc_post, methods=["POST"]),
        Route("/settings/cluster", settings_cluster_post, methods=["POST"]),
        Route("/settings/config/export", config_export),
        Route("/settings/config/import", config_import, methods=["POST"]),
    ]
