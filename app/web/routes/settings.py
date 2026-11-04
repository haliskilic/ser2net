"""The Settings page and its forms: password, web-UI access, users, LDAP, OIDC,
cluster, admin TLS, REST API token and mappings export/import."""
from __future__ import annotations

import asyncio
import csv
import dataclasses
import io
import json
import os
import secrets
import time
from urllib.parse import urlencode

from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ... import importer
from ...config import ROLES, AppConfig, ClusterSettings, ConfigError, User
from .. import auth, selfsigned
from .common import Ctx
from .forms import (
    _checkbox, _ldap_from_form, _module_present, _oidc_from_form, _password_problem,
    _username_problem,
)


def routes(ctx: Ctx) -> list:
    state, render = ctx.state, ctx.render
    current_user, require_role, set_session = ctx.current_user, ctx.require_role, ctx.set_session

    async def settings_get(request, ok=None, error=None, new_api_token=None, import_preview=None):
        me = current_user(request)
        backups = state.store.backups() if me is not None and me.role == "admin" else []
        return render(request, "settings.html", ok=ok, error=error,
                      import_preview=import_preview, backups=backups,
                      admin=state.config.admin_ui,
                      api_token_set=bool(state.config.api_token_hash),
                      api_token_role=state.config.api_token_role,
                      new_api_token=new_api_token,
                      me=current_user(request), users=state.config.users, roles=ROLES,
                      ldap=state.config.ldap, ldap_lib=_module_present("ldap3"),
                      oidc=state.config.oidc, oidc_lib=_module_present("authlib"),
                      cluster=state.config.cluster, instance_id=state.config.instance_id,
                      cluster_peers_text="\n".join(state.config.cluster.peers),
                      session_minutes=state.config.session_timeout_s // 60,
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
        ctx.audit(request, "password_change", user.username)
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
        ctx.audit(request, "admin_tls_set", cert)
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
        ctx.audit(request, "admin_tls_generate", "")
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
        timeout = state.config.session_timeout_s
        if "session_minutes" in form:
            try:
                timeout = int(float(form.get("session_minutes") or "") * 60)
            except ValueError:
                return await settings_get(request, error="Session lifetime must be a number of minutes.")
        ui = state.config.admin_ui
        async with state.config_lock:
            old = (ui.require_login, ui.anonymous_role, state.config.session_timeout_s)
            ui.require_login, ui.anonymous_role = require_login, role
            state.config.session_timeout_s = timeout
            try:
                await state.asave()
            except ConfigError as e:
                ui.require_login, ui.anonymous_role, state.config.session_timeout_s = old
                return await settings_get(request, error=str(e))
        if timeout != old[2]:
            state.log(f"web UI session lifetime set to {timeout // 60} min (new sign-ins)")
        if require_login:
            state.log("web UI login requirement ENABLED (every visitor must sign in)")
            ok = "Login is required again for the web UI."
        else:
            state.log(f"WARNING: web UI login requirement DISABLED — visitors without a "
                      f"session get the '{role}' role (by IP / localhost / host name)")
            ok = (f"Open access enabled: visitors without a login now get the '{role}' role. "
                  "Use this only on a trusted network.")
        ctx.audit(request, "web_access",
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
        ctx.audit(request, "api_token_generate", role)
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
        ctx.audit(request, "api_token_revoke", "")
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
        ctx.audit(request, "ldap_settings",
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
        ctx.audit(request, "oidc_settings",
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
        ctx.audit(request, "user_create", f"{username}:{role}")
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
        ctx.audit(request, "user_role", f"{username}:{role}")
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
        ctx.audit(request, "user_delete", username)
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

    # ---- import: the form first shows a preview (step=preview), then applies it in
    #      "merge" or "replace" mode. A POST without step (scripts written for 2.7)
    #      keeps the old behaviour: replace everything at once.
    PENDING_TTL_S = 900

    def _pending_put(user: str, plan) -> str:
        now, pend = time.time(), state.pending_imports
        for k in [k for k, v in pend.items() if now - v["created"] > PENDING_TTL_S]:
            del pend[k]
        while len(pend) >= 20:
            pend.pop(next(iter(pend)))
        token = secrets.token_urlsafe(16)
        pend[token] = {"created": now, "user": user, "plan": plan}
        return token

    def _pending_get(token: str, user: str):
        item = state.pending_imports.get(token or "")
        if not item or item["user"] != user or time.time() - item["created"] > PENDING_TTL_S:
            return None
        return item

    def _who(request) -> str:
        u = current_user(request)
        return u.username if u is not None else ""

    async def config_import(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        upload = form.get("file")
        try:
            raw = await upload.read() if hasattr(upload, "read") else str(upload or "").encode()
            plan = importer.parse_mappings_file(raw, getattr(upload, "filename", "") or "")
        except ConfigError as e:
            return await settings_get(request, error=f"Import failed: {e}")
        mode = form.get("mode", "merge")
        if form.get("step") == "preview":
            if mode not in importer.MODES:
                return await settings_get(request, error="Unknown import mode.")
            current = state.config.mappings
            rows = importer.plan_rows(current, plan.mappings, mode)
            candidate, _ = importer.apply_import(current, plan.mappings, mode)
            problem = None
            try:
                dataclasses.replace(state.config, mappings=candidate).validate()
            except ConfigError as e:
                problem = str(e)
            preview = {"token": _pending_put(_who(request), plan), "mode": mode, "rows": rows,
                       "source": plan.source, "warnings": plan.warnings, "problem": problem,
                       "count": len(plan.mappings)}
            return await settings_get(request, import_preview=preview)
        # legacy one-step import: replace all mappings
        return await _apply_import(request, plan, "replace")

    async def config_import_apply(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        token = form.get("token", "")
        item = _pending_get(token, _who(request))
        if item is None:
            return await settings_get(request, error="This import preview has expired. Upload the file again.")
        mode = form.get("mode", "merge")
        if mode not in importer.MODES:
            return await settings_get(request, error="Unknown import mode.")
        state.pending_imports.pop(token, None)
        return await _apply_import(request, item["plan"], mode)

    async def _apply_import(request, plan, mode):
        rows = importer.plan_rows(state.config.mappings, plan.mappings, mode)
        async with state.config_lock:
            old = state.config.mappings
            new_maps, touched = importer.apply_import(old, plan.mappings, mode)
            state.config.mappings = new_maps
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = old
                return await settings_get(request, error=f"Import rejected: {e}")
        if mode == "replace":
            await state.supervisor.stop_all()
            await state.supervisor.start_all(state.config)
        else:
            for m in touched:
                await state.supervisor.apply_mapping(m)
        count = {a: sum(1 for r in rows if r["action"] == a) for a in ("add", "update", "same", "remove")}
        state.log(f"mappings imported ({mode}): {count['add']} added, {count['update']} updated, "
                  f"{count['same']} unchanged, {count['remove']} removed")
        ctx.audit(request, f"config_import_{mode}",
                    f"{len(plan.mappings)} mappings ({plan.source})")
        if mode == "replace":
            ok = f"Imported {len(new_maps)} mappings (replaced the existing ones)."
        else:
            ok = (f"Imported: {count['add']} added, {count['update']} updated, "
                  f"{count['same']} unchanged.")
        return await settings_get(request, ok=ok + " The previous configuration was kept as a backup.")

    # ---------------- audit log viewer (admin) ----------------
    AUDIT_PAGE = 100

    async def _audit_records(request):
        params = request.query_params
        q = (params.get("q") or "").strip()
        action = (params.get("action") or "").strip()
        user = (params.get("user") or "").strip()
        records = await asyncio.to_thread(state.read_audit)
        actions = sorted({r["action"] for r in records})
        users = sorted({r["user"] for r in records if r["user"]})
        if action:
            records = [r for r in records if r["action"] == action]
        if user:
            records = [r for r in records if r["user"] == user]
        if q:
            needle = q.lower()
            records = [r for r in records if needle in "\t".join(r.values()).lower()]
        return records, {"q": q, "action": action, "user": user}, actions, users

    async def audit_page(request):
        if deny := require_role(request, "admin"):
            return deny
        records, filters, actions, users = await _audit_records(request)
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, -(-len(records) // AUDIT_PAGE))
        page = min(page, pages)
        return render(request, "audit.html", rows=records[(page - 1) * AUDIT_PAGE:page * AUDIT_PAGE],
                      total=len(records), page=page, pages=pages, filters=filters,
                      actions=actions, users=users, me=current_user(request),
                      qs=urlencode({k: v for k, v in filters.items() if v}))

    async def audit_csv(request):
        if deny := require_role(request, "admin"):
            return deny
        records, *_ = await _audit_records(request)
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(["time", "ip", "user", "action", "detail"])
        for r in records:
            out.writerow([_csv_cell(r[k]) for k in ("time", "ip", "user", "action", "detail")])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=ser2net-audit.csv"})

    # ---------------- configuration backups (admin) ----------------
    async def backup_download(request):
        if deny := require_role(request, "admin"):
            return deny
        name = request.path_params["name"]
        try:
            text = await asyncio.to_thread(state.store.read_backup, name)
        except (ConfigError, OSError):
            return PlainTextResponse("No such backup.", status_code=404)
        ctx.audit(request, "config_backup_download", name)
        return Response(text, media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    async def backup_restore(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        name = form.get("name", "")
        try:
            restored = AppConfig.from_dict(json.loads(await asyncio.to_thread(state.store.read_backup, name)))
        except (ConfigError, OSError, ValueError, TypeError, AttributeError) as e:
            return await settings_get(request, error=f"Could not read the backup: {e}")
        if not restored.users:
            return await settings_get(request, error="This backup has no user accounts (it was made "
                                                     "before the first-run setup); restoring it would "
                                                     "open the setup page to anyone. Not restored.")
        keep = {"secret_key", "instance_id", "load_notes"}  # sessions + this node's cluster id
        async with state.config_lock:
            cur = state.config
            previous = {f.name: getattr(cur, f.name) for f in dataclasses.fields(AppConfig)}
            for f in dataclasses.fields(AppConfig):
                if f.name not in keep:
                    setattr(cur, f.name, getattr(restored, f.name))
            try:
                await state.asave()
            except ConfigError as e:
                for k, v in previous.items():
                    setattr(cur, k, v)
                return await settings_get(request, error=f"Restore rejected: {e}")
        await state.supervisor.stop_all()
        await state.supervisor.start_all(state.config)
        await state.cluster.stop()
        await state.cluster.start()
        state.log(f"configuration restored from backup {name}")
        ctx.audit(request, "config_backup_restore", name)
        return await settings_get(request, ok=f"Restored {name}; the configuration it replaced was "
                                              "backed up too. A changed admin UI address or TLS "
                                              "takes effect after a restart.")

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
        ctx.audit(request, "cluster_settings",
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
        Route("/settings/config/import/apply", config_import_apply, methods=["POST"]),
        Route("/settings/audit", audit_page),
        Route("/settings/audit.csv", audit_csv),
        Route("/settings/backups/restore", backup_restore, methods=["POST"]),
        Route("/settings/backups/{name}", backup_download),
    ]


def _csv_cell(value: str) -> str:
    """A spreadsheet must not run a cell as a formula (mapping names and user names
    end up in the audit detail): prefix the characters that start one."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
