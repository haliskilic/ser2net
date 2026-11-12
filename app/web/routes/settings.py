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
import socket
import time
import types
import unicodedata
from urllib.parse import urlencode

from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ... import i18n, importer, optional
from ...config import (
    EXPIRE_DAYS_MAX, EXPIRE_DAYS_MIN, HSTS_MAX_AGE_MAX, NODE_NAME_ENV, NODE_NAME_MAX,
    NOTIFY_EVENTS, ROLES, SMTP_SECURITY, UI_LANGUAGES, WEBHOOK_FORMATS, AppConfig,
    ClusterSettings, ConfigError, NotificationSettings, User, backup_time, new_pwd_version,
    node_name_from_env, node_name_problem, normalize_public_url, parse_cluster_peer,
    proxy_entry_problem, public_url_problem, split_proxy_entries,
)
from ...engine.cluster import KEY_OVERLAP_S, key_bytes
from ...i18n import _
from .. import auth, ldap_auth, oidc_auth, proxy, selfsigned
from .common import Ctx, csrf_failed
from .forms import (
    _checkbox, _ldap_from_form, _oidc_from_form, _password_problem, _username_problem,
)

# a mappings file to import may be larger than other requests (server.MAX_BODY_BYTES):
# the importer's file limit, plus the form's other fields
IMPORT_MAX_BYTES = importer.MAX_FILE_BYTES + 64 * 1024


def routes(ctx: Ctx) -> list:
    state, render = ctx.state, ctx.render
    current_user, require_role, set_session = ctx.current_user, ctx.require_role, ctx.set_session

    def _cluster_view() -> dict:
        """What the LAN cluster panel shows - never the key itself."""
        c = state.config.cluster
        keys = state.cluster.keys()
        until = c.previous_until if len(keys) > 1 else 0.0
        return {"cluster": c, "instance_id": state.config.instance_id,
                "cluster_peers_text": "\n".join(c.peers),
                "cluster_key_set": bool(c.key.strip()), "cluster_key_hint": _key_hint(c.key),
                "cluster_previous": len(keys) > 1,
                "cluster_previous_until": time.strftime("%Y-%m-%d %H:%M", time.gmtime(until))
                if until else "",
                "cluster_copies": state.cluster.copies(),
                "cluster_legacy": state.cluster.legacy_nodes() if c.active else []}

    def _package_problems():
        """Why LDAP / OIDC sign-in can't work for want of its package, while it is on: a
        real import (find_spec would call a package that fails to import present)."""
        return (optional.load("ldap3", "ldap3")[1] if state.config.ldap.enabled else None,
                optional.load("authlib.jose", "authlib")[1] if state.config.oidc.enabled else None)

    def _sign_in_time(user) -> str:
        when = state.signins.last(user)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "—"

    async def settings_get(request, ok=None, error=None, new_api_token=None, import_preview=None,
                           new_cluster_key=None, restore_preview=None):
        me = current_user(request)
        backups = state.store.backups() if me is not None and me.role == "admin" else []
        ldap_problem, oidc_problem = await asyncio.to_thread(_package_problems)
        # the redirect URI a sign-in started from this page's address would send
        published = proxy.public_url(state.config.admin_ui)
        oidc_redirect = oidc_auth.redirect_uri(state.config.oidc, request, published)
        return render(request, "settings.html", ok=ok, error=error,
                      import_preview=import_preview, restore_preview=restore_preview,
                      backups=backups,
                      admin=state.config.admin_ui, hsts_max=HSTS_MAX_AGE_MAX,
                      hostname=socket.gethostname(), node_name_max=NODE_NAME_MAX,
                      node_name_env=NODE_NAME_ENV if node_name_from_env() else "",
                      public_url=published, public_url_env=bool(proxy.public_url_from_env()),
                      trusted=proxy.trusted_entries(state.config.admin_ui),
                      api_token_set=bool(state.config.api_token_hash),
                      api_token_role=state.config.api_token_role,
                      new_api_token=new_api_token, new_cluster_key=new_cluster_key,
                      me=current_user(request), users=state.config.users, roles=ROLES,
                      last_sign_in={u.username: _sign_in_time(u) for u in state.config.users},
                      expire_days=state.config.external_users_expire_days,
                      expire_min=EXPIRE_DAYS_MIN, expire_max=EXPIRE_DAYS_MAX,
                      ldap=state.config.ldap, ldap_problem=ldap_problem,
                      ldap_tls_problem=state.ldap_tls_problem,
                      oidc=state.config.oidc, oidc_problem=oidc_problem,
                      oidc_redirect_uri=oidc_redirect,
                      oidc_redirect_auto=not state.config.oidc.redirect_uri.strip() and not published,
                      oidc_redirect_public=not state.config.oidc.redirect_uri.strip() and bool(published),
                      oidc_redirect_http=oidc_auth.redirect_uri_is_plain_http(oidc_redirect),
                      group_warnings={src: [w for (s, _k), w in sorted(state.group_warnings.items())
                                            if s == src] for src in ("ldap", "oidc")},
                      **_cluster_view(),
                      session_minutes=state.config.session_timeout_s // 60,
                      notify=state.config.notifications, notify_events=NOTIFY_EVENTS,
                      webhook_formats=WEBHOOK_FORMATS, smtp_security=SMTP_SECURITY,
                      uptime=int(state.started_at))

    async def settings_page(request):
        """GET /settings; ?restore=<backup> shows what restoring that backup would change."""
        name = request.query_params.get("restore")
        if name is None or require_role(request, "admin"):
            return await settings_get(request)
        try:
            backup = AppConfig.from_dict(json.loads(
                await asyncio.to_thread(state.store.read_backup, name)))
        except (ConfigError, OSError, ValueError, TypeError, AttributeError) as e:
            return await settings_get(request, error=_("Could not read the backup: {0}").format(e))
        return await settings_get(request, restore_preview=_restore_preview(
            name, state.config, backup, current_user(request)))

    async def settings_password_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        user = current_user(request)
        if user is not None and user.source == "anonymous":
            return await settings_get(request, error=_("Sign in to change your password."))
        if user is not None and user.source != "local":
            return await settings_get(request,
                                      error=_("Your account is managed by {0}; change your "
                                              "password there.").format(user.source.upper()))
        cur = form.get("current", "")
        new = form.get("password", "")
        new2 = form.get("password2", "")
        if user is None:
            return await settings_get(request, error=_("Current password is incorrect."))
        # wrong current passwords count like failed sign-ins under this name (a session
        # someone else got hold of must not be a way to guess the password itself)
        if state.name_limiter.blocked(user.username):
            return await settings_get(request, error=_("Too many attempts. Wait a few minutes "
                                                       "and try again."))
        state.name_limiter.record_failure(user.username)
        if not await state.password_work.run(auth.verify_password, cur, user.password_hash):
            state.log(f"password change for {user.username} refused: wrong current password "
                      f"(from {ctx.client_ip(request)})")
            ctx.audit(request, "password_change_failed", user.username)
            return await settings_get(request, error=_("Current password is incorrect."))
        state.name_limiter.reset(user.username)
        err = _password_problem(new, new2)
        if err:
            return await settings_get(request, error=err)
        new_hash = await state.password_work.run(auth.hash_password, new)
        async with state.config_lock:
            user.password_hash = new_hash
            user.pwd_version += 1
            await state.asave()
        state.log(f"password changed for {user.username}; their other sessions signed out")
        ctx.audit(request, "password_change", user.username)
        # refresh THIS session so the user isn't logged out by their own change
        resp = await settings_get(request, ok=_("Password updated. Your other sessions were "
                                                "signed out."))
        set_session(resp, request, user)
        return resp

    def _spellings(target) -> list:
        """The other accounts of an LDAP user, in another letter case (made by earlier
        versions, one per spelling; a sign-in now merges them): signing out or deleting
        that user covers them too."""
        if target.source != "ldap":
            return []
        return [u for u in state.config.external_accounts(target.username, "ldap")
                if u is not target]

    async def _bump_sessions(username: str):
        """Sign every session of `username` out (pwd_version + 1). Returns the user, or
        an error text. Caller holds config_lock."""
        target = state.config.get_user(username)
        if target is None:
            return None, _("User not found.")
        accounts = [target, *_spellings(target)]
        for u in accounts:
            u.pwd_version += 1
        try:
            await state.asave()
        except ConfigError as e:
            for u in accounts:
                u.pwd_version -= 1
            return None, str(e)
        return target, None

    async def settings_signout_others(request):
        """Any signed-in user: end their own sessions elsewhere, keep this one. Nothing
        to do (no save) when no session was issued for the account since its last
        click (AppState.others_signed_out)."""
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        me = current_user(request)
        if me is None or me.source == "anonymous":
            return await settings_get(request, error=_("Sign in to manage your sessions."))
        async with state.config_lock:
            if state.others_signed_out.get(me.username) == (
                    me.pwd_version, state.sessions_issued.get(me.username, 0)):
                return await settings_get(request,
                                          ok=_("Your other sessions are already signed out."))
            user, err = await _bump_sessions(me.username)
        if err:
            return await settings_get(request, error=err)
        state.log(f"{me.username} signed out their other sessions")
        ctx.audit(request, "sessions_signout_others", me.username)
        resp = await settings_get(request, ok=_("Your other sessions were signed out."))
        set_session(resp, request, user)
        state.others_signed_out[user.username] = (user.pwd_version,
                                                  state.sessions_issued.get(user.username, 0))
        return resp

    async def settings_tls_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        cert, key = form.get("tls_cert", "").strip(), form.get("tls_key", "").strip()
        ui = state.config.admin_ui
        hsts = ui.hsts_max_age   # kept when the form has no such field (an older page)
        if "hsts_max_age" in form:
            raw = str(form.get("hsts_max_age") or "").strip()
            if not (raw.isascii() and raw.isdigit() and int(raw) <= HSTS_MAX_AGE_MAX):
                return await settings_get(request, error=_(
                    "The HSTS max-age must be a whole number of seconds from 0 to {0}.")
                    .format(HSTS_MAX_AGE_MAX))
            hsts = int(raw)
        async with state.config_lock:
            old = (ui.tls_cert, ui.tls_key, ui.hsts_max_age)
            ui.tls_cert, ui.tls_key, ui.hsts_max_age = cert, key, hsts
            try:
                await state.asave()
            except ConfigError as e:
                ui.tls_cert, ui.tls_key, ui.hsts_max_age = old
                return await settings_get(request, error=str(e))
        if hsts != old[2]:
            state.log(f"admin TLS: HSTS max-age set to {hsts} s"
                      + (" (max-age=0: browsers forget it)" if hsts == 0 else ""))
        ctx.audit(request, "admin_tls_set", cert + ("" if hsts == old[2] else f" (HSTS {hsts} s)"))
        return await settings_get(request, ok=_("Admin TLS saved. Restart to apply."))

    async def settings_tls_generate(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        tdir = os.path.join(state.data_dir, "tls")
        os.makedirs(tdir, exist_ok=True)
        cert, key = os.path.join(tdir, "cert.pem"), os.path.join(tdir, "key.pem")
        try:
            await asyncio.to_thread(selfsigned.generate, cert, key)
        except (selfsigned.CertError, OSError) as e:
            return await settings_get(request, error=_("Could not generate a certificate: {0}").format(e))
        async with state.config_lock:
            state.config.admin_ui.tls_cert = cert
            state.config.admin_ui.tls_key = key
            await state.asave()
        ctx.audit(request, "admin_tls_generate", "")
        return await settings_get(request, ok=_("Self-signed certificate generated. Restart to "
                                                "apply TLS."))

    # ---------------- web-UI access (login requirement) ----------------
    async def settings_access_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        require_login = _checkbox(form, "require_login")
        role = form.get("anonymous_role", "viewer")
        if role not in ROLES:
            return await settings_get(request, error=_("Invalid role for visitors without login."))
        language = form.get("default_language") or state.config.admin_ui.language
        if language not in UI_LANGUAGES:
            language = state.config.admin_ui.language
        timeout = state.config.session_timeout_s
        if "session_minutes" in form:
            try:
                timeout = int(float(form.get("session_minutes") or "") * 60)
            except ValueError:
                return await settings_get(request, error=_("Session lifetime must be a number "
                                                           "of minutes."))
        ui = state.config.admin_ui
        async with state.config_lock:
            old = (ui.require_login, ui.anonymous_role, state.config.session_timeout_s, ui.language)
            ui.require_login, ui.anonymous_role = require_login, role
            state.config.session_timeout_s, ui.language = timeout, language
            try:
                await state.asave()
            except ConfigError as e:
                (ui.require_login, ui.anonymous_role, state.config.session_timeout_s,
                 ui.language) = old
                return await settings_get(request, error=str(e))
        if timeout != old[2]:
            state.log(f"web UI session lifetime set to {timeout // 60} min (new sign-ins)")
        if require_login:
            state.log("web UI login requirement ENABLED (every visitor must sign in)")
            ok = _("Login is required again for the web UI.")
        else:
            state.log(f"WARNING: web UI login requirement DISABLED — visitors without a "
                      f"session get the '{role}' role (by IP / localhost / host name)")
            ok = _("Open access enabled: visitors without a login now get the '{0}' role. "
                   "Use this only on a trusted network.").format(role)
        ctx.audit(request, "web_access",
                    "login-required" if require_login else f"open:{role}")
        return await settings_get(request, ok=ok)

    # ---------------- this instance's name ----------------
    async def settings_node_name_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ui = state.config.admin_ui
        if "node_name" not in form:   # the environment sets it (the field is disabled)
            return await settings_get(request)
        name = str(form.get("node_name") or "")
        if problem := node_name_problem(name):
            return await settings_get(request, error=problem)
        name = name.strip()
        async with state.config_lock:
            old, ui.node_name = ui.node_name, name
            try:
                await state.asave()
            except ConfigError as e:
                ui.node_name = old
                return await settings_get(request, error=str(e))
        if name != old:
            state.log(f"instance name set to {name!r}" if name else
                      "instance name cleared: the host name is used")
        ctx.audit(request, "node_name_set", name or "(host name)")
        return await settings_get(request, ok=_("Instance name saved."))

    # ---------------- behind a reverse proxy ----------------
    async def settings_proxy_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ui = state.config.admin_ui
        # a field the form doesn't have (disabled: the environment sets it) keeps its value
        url, entries = ui.public_url, list(ui.trusted_proxies)
        if "public_url" in form:
            raw = str(form.get("public_url") or "").strip()
            if problem := public_url_problem(raw):
                return await settings_get(request, error=problem)
            url = normalize_public_url(raw)
        if "trusted_proxies" in form:
            entries = list(dict.fromkeys(split_proxy_entries(str(form.get("trusted_proxies") or ""))))
            if problem := next(filter(None, map(proxy_entry_problem, entries)), None):
                return await settings_get(request, error=problem)
        hsts = _checkbox(form, "proxy_hsts")
        async with state.config_lock:
            old = (ui.public_url, list(ui.trusted_proxies), ui.proxy_hsts)
            ui.public_url, ui.trusted_proxies, ui.proxy_hsts = url, entries, hsts
            try:
                await state.asave()
            except ConfigError as e:
                ui.public_url, ui.trusted_proxies, ui.proxy_hsts = old
                return await settings_get(request, error=str(e))
        changes = [what for what, changed in (
            (f"public URL {url or '(none)'}", url != old[0]),
            (f"trusted proxies {' '.join(entries) or '(none)'}", entries != old[1]),
            (f"HSTS through the proxy {'on' if hsts else 'off'}", hsts != old[2])) if changed]
        if changes:
            state.log(f"reverse proxy settings: {'; '.join(changes)}")
        ctx.audit(request, "reverse_proxy_set", "; ".join(changes) or "unchanged")
        return await settings_get(request, ok=_("Reverse proxy settings saved."))

    async def settings_sessions_revoke_all(request):
        """A new session key: every web UI session ends at once, including sessions
        signed with the key in an old copy of config.json or of a backup."""
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        async with state.config_lock:
            old = state.config.secret_key
            state.config.secret_key = secrets.token_hex(32)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.secret_key = old
                return await settings_get(request, error=str(e))
        state.revoked_sessions.clear()  # signed with the old key: dead anyway
        state.log("new session key: every web UI session was signed out")
        ctx.audit(request, "sessions_revoke_all", "")
        resp = await settings_get(request, ok=_("Everyone was signed out. You stay signed in "
                                                "here."))
        me = current_user(request)
        if me is not None and me.source != "anonymous":
            set_session(resp, request, me)
        return resp

    # ---------------- REST API token ----------------
    async def settings_api_token_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        role = form.get("api_token_role")  # no field: a form from before token roles
        role = "admin" if role is None else str(role).strip().lower()
        if role not in ROLES:
            return await settings_get(request, error=_("Invalid role."))
        token = auth.new_api_token()
        async with state.config_lock:
            state.config.api_token_hash = auth.hash_token(token)
            state.config.api_token_role = role
            await state.asave()
        state.log(f"REST API token generated (role: {role})")
        ctx.audit(request, "api_token_generate", role)
        # show the token exactly once — only its hash is stored
        return await settings_get(request, new_api_token=token,
                                  ok=_("New API token generated (role: {0}). Copy it now — it "
                                       "is not shown again.").format(role))

    async def settings_api_token_revoke(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        async with state.config_lock:
            state.config.api_token_hash = ""
            await state.asave()
        state.log("REST API token revoked")
        ctx.audit(request, "api_token_revoke", "")
        return await settings_get(request, ok=_("API token revoked. The REST API is now disabled."))

    # ---------------- LDAP / AD settings (admin only) ----------------
    def _directory_off_problem(before: int, name: str) -> str | None:
        """Switching LDAP/OIDC off signs its accounts out (auth.session_user): refuse
        when that would leave no admin who can sign in."""
        if before and not state.config.usable_admin_count():
            return _("Not saved: no admin could sign in any more, because every admin account "
                     "comes from {0}. Add a local admin account first (Users), or keep {0} "
                     "sign-in on.").format(name)
        return None

    def _directory_off_note(was_on: bool, now_on: bool, source: str, name: str) -> str:
        if was_on and not now_on and any(u.source == source for u in state.config.users):
            return " " + _("{0} accounts are signed out and can't sign in while {0} is "
                           "off.").format(name)
        return ""

    def _ca_file_problem(ldap) -> str | None:
        """Checked when the LDAP form is saved with LDAP on over TLS (the only time the
        file is used; switching LDAP off must work while it is missing): the CA
        certificate file can be read and holds a certificate."""
        if not (ldap.enabled and ldap.uses_tls and ldap.tls_verify and ldap.ca_file.strip()):
            return None
        try:
            ldap_auth.tls_context(ldap)
        except ldap_auth.CaFileError as e:
            return (_("Not saved: the CA certificate file {0} can't be read.") if e.unreadable
                    else _("Not saved: the CA certificate file {0} holds no certificate (PEM "
                           "or DER).")).format(e.path)
        return None

    def _clear_group_warnings(source: str) -> None:
        """Saving that section answers its group-setting warnings (a sign-in that still
        matches only part of a group name shows a new one)."""
        for key in [k for k in state.group_warnings if k[0] == source]:
            del state.group_warnings[key]

    async def settings_ldap_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        async with state.config_lock:
            old = state.config.ldap
            before = state.config.usable_admin_count()
            state.config.ldap = _ldap_from_form(form, old)
            if problem := (_directory_off_problem(before, "LDAP")
                           or _ca_file_problem(state.config.ldap)):
                state.config.ldap = old
                return await settings_get(request, error=problem)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.ldap = old
                return await settings_get(request, error=str(e))
        _clear_group_warnings("ldap")
        state.ldap_tls_problem = None   # answered; the next failing sign-in shows it again
        state.log(f"LDAP settings updated (enabled={state.config.ldap.enabled})")
        if old.tls_verify != state.config.ldap.tls_verify:
            state.log("WARNING: LDAP: the directory server's certificate is no longer checked "
                      "(insecure)" if old.tls_verify else
                      "LDAP: the directory server's certificate is checked again")
        ctx.audit(request, "ldap_settings",
                    "enabled" if state.config.ldap.enabled else "disabled")
        return await settings_get(request, ok=_("LDAP settings saved.")
                                  + _directory_off_note(old.enabled, state.config.ldap.enabled,
                                                        "ldap", "LDAP"))

    async def settings_oidc_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        async with state.config_lock:
            old = state.config.oidc
            before = state.config.usable_admin_count()
            state.config.oidc = _oidc_from_form(form, old)
            if problem := _directory_off_problem(before, "OIDC"):
                state.config.oidc = old
                return await settings_get(request, error=problem)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.oidc = old
                return await settings_get(request, error=str(e))
        _clear_group_warnings("oidc")
        state.log(f"OIDC settings updated (enabled={state.config.oidc.enabled})")
        ctx.audit(request, "oidc_settings",
                    "enabled" if state.config.oidc.enabled else "disabled")
        return await settings_get(request, ok=_("OIDC (SSO) settings saved.")
                                  + _directory_off_note(old.enabled, state.config.oidc.enabled,
                                                        "oidc", "OIDC"))

    # ---------------- user management (admin only) ----------------
    def _target_name(request, form) -> str:
        """The account a Users-panel form acts on: its `username` field (any name works
        there, also DOMAIN\\user or one with / # ?), or the older form's URL
        /settings/users/<name>/... (kept for scripts and older pages)."""
        return request.path_params.get("username") or str(form.get("username") or "")

    def _is_me(request, username: str) -> bool:
        """The signed-in account itself (never the open-access pseudo-user)."""
        me = current_user(request)
        return me is not None and me.source != "anonymous" and me.username == username

    def _last_admin(target) -> bool:
        """Would demoting / deleting this admin leave no admin, or none who can sign in
        (an LDAP/OIDC admin can't while that method is off)?"""
        cfg = state.config
        return cfg.admin_count() <= 1 or (cfg.source_enabled(target)
                                          and cfg.usable_admin_count() <= 1)

    def _leaves_no_admin(gone) -> bool:
        """Would deleting these accounts leave no admin, or none who can sign in?"""
        cfg = state.config
        admins = [u for u in gone if u.role == "admin"]
        rest = [u for u in cfg.users if u.role == "admin" and not any(u is g for g in gone)]
        return bool(admins) and (not rest or (any(cfg.source_enabled(u) for u in admins)
                                              and not any(cfg.source_enabled(u) for u in rest)))

    async def settings_users_create(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = (form.get("username") or "").strip()
        role = form.get("role", "viewer")
        pw, pw2 = form.get("password", ""), form.get("password2", "")

        def taken():
            """Also an account of this name in another letter case: LDAP names compare
            that way, and two names that differ only in case are easily mistaken."""
            other = state.config.named(username)
            return _("User '{0}' already exists.").format(other[0].username) if other else None

        err = (_username_problem(username)
               or (None if role in ROLES else _("Invalid role."))
               or _password_problem(pw, pw2)
               or taken())
        if err:
            return await settings_get(request, error=err)
        pw_hash = await state.password_work.run(auth.hash_password, pw)
        async with state.config_lock:
            if err := taken():  # created while the password was hashed
                return await settings_get(request, error=err)
            state.config.users.append(User(username=username, password_hash=pw_hash,
                                           role=role, pwd_version=new_pwd_version()))
            await state.asave()
        state.log(f"user created: {username} ({role})")
        ctx.audit(request, "user_create", f"{username}:{role}")
        return await settings_get(request, ok=_("User '{0}' created.").format(username))

    async def settings_users_role(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = _target_name(request, form)
        role = form.get("role", "viewer")
        async with state.config_lock:  # checked under the lock: two admins at once
            target = state.config.get_user(username)
            if target is None:
                return await settings_get(request, error=_("User not found."))
            if role not in ROLES:
                return await settings_get(request, error=_("Invalid role."))
            if target.role == "admin" and role != "admin" and _last_admin(target):
                return await settings_get(request, error=_("Cannot demote the last admin."))
            target.role = role
            target.pwd_version += 1  # force re-login so the new role applies everywhere
            await state.asave()
        state.log(f"user role changed: {username} -> {role}")
        ctx.audit(request, "user_role", f"{username}:{role}")
        return await settings_get(request, ok=_("Role updated for '{0}'. They must sign in again.").format(username))

    async def settings_users_delete(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = _target_name(request, form)
        async with state.config_lock:  # checked under the lock: two admins at once
            target = state.config.get_user(username)
            if target is None:
                return await settings_get(request, error=_("User not found."))
            if _is_me(request, username):
                return await settings_get(request, error=_("You cannot delete your own account."))
            if target.role == "admin" and _last_admin(target):
                return await settings_get(request, error=_("Cannot delete the last admin."))
            others = [u for u in _spellings(target) if not _is_me(request, u.username)]
            if others and _leaves_no_admin([target, *others]):
                return await settings_get(request, error=_("Cannot delete the last admin."))
            gone = [target, *others]
            state.config.users = [u for u in state.config.users if not any(u is g for g in gone)]
            await state.asave()
        for u in gone:
            state.log(f"user deleted: {u.username}")
            ctx.audit(request, "user_delete", u.username)
        ok = _("User '{0}' deleted.").format(username)
        if others:
            ok += " " + _("Its accounts in another letter case were deleted too: {0}.").format(
                ", ".join(u.username for u in others))
        if target.source != "local":
            ok += " " + _("They are signed out; while {0} still gives them a role, their next "
                          "sign-in makes a new account.").format(target.source.upper())
        return await settings_get(request, ok=ok)

    async def settings_users_expiry(request):
        """How many days without a sign-in an LDAP/OIDC account is kept (0: for ever)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        try:
            days = int(str(form.get("expire_days", "")).strip())
        except ValueError:
            days = -1
        if not (days == 0 or EXPIRE_DAYS_MIN <= days <= EXPIRE_DAYS_MAX):
            return await settings_get(request, error=_(
                "Enter 0 (never) or a number of days from {0} to {1}.").format(
                    EXPIRE_DAYS_MIN, EXPIRE_DAYS_MAX))
        async with state.config_lock:
            old = state.config.external_users_expire_days
            state.config.external_users_expire_days = days
            try:
                await state.asave()
            except ConfigError as e:
                state.config.external_users_expire_days = old
                return await settings_get(request, error=str(e))
        state.log(f"LDAP/OIDC accounts are removed after {days} days without a sign-in" if days
                  else "LDAP/OIDC accounts are no longer removed for not signing in")
        ctx.audit(request, "users_expiry", str(days))
        gone = await state.expire_external_accounts()
        ok = (_("Saved: LDAP / OIDC accounts that haven’t signed in for {0} days are removed "
                "(checked every hour).").format(days) if days else
              _("Saved: LDAP / OIDC accounts are never removed for not signing in."))
        if gone:
            ok += " " + _("Removed now: {0}.").format(", ".join(u.username for u in gone))
        return await settings_get(request, ok=ok)

    async def settings_users_signout(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = _target_name(request, form)
        async with state.config_lock:
            target, err = await _bump_sessions(username)
        if err:
            return await settings_get(request, error=err)
        state.log(f"all sessions of {username} signed out")
        ctx.audit(request, "user_signout", username)
        resp = await settings_get(request, ok=_("'{0}' was signed out everywhere.").format(username))
        if _is_me(request, username):
            set_session(resp, request, target)  # your own row: you stay signed in here
        return resp

    async def settings_users_password(request):
        """An admin sets another local account's password (it forgot it, or it must
        be taken away from someone): the account is signed out everywhere."""
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = (form.get("username") or "").strip()
        pw, pw2 = form.get("password", ""), form.get("password2", "")

        def problem():
            target = state.config.get_user(username)
            if target is None:
                return _("User not found.")
            if target.source != "local":
                return _("'{0}' signs in with {1}; their password is managed there.").format(
                    username, target.source.upper())
            if _is_me(request, username):
                return _("To change your own password, use the form at the top (it asks for "
                         "the current one).")
            return None

        if err := problem() or _password_problem(pw, pw2):
            return await settings_get(request, error=err)
        pw_hash = await state.password_work.run(auth.hash_password, pw)
        async with state.config_lock:
            if err := problem():  # changed while the password was hashed
                return await settings_get(request, error=err)
            target = state.config.get_user(username)
            old = (target.password_hash, target.pwd_version)
            target.password_hash, target.pwd_version = pw_hash, target.pwd_version + 1
            try:
                await state.asave()
            except ConfigError as e:
                target.password_hash, target.pwd_version = old
                return await settings_get(request, error=str(e))
        state.log(f"password of {username} set by an admin; their sessions were signed out")
        ctx.audit(request, "user_password_set", username)
        return await settings_get(request, ok=_("New password set for '{0}'; they were signed out "
                                                "everywhere.").format(username))

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
            return csrf_failed()
        upload = form.get("file")
        try:
            raw = await upload.read() if hasattr(upload, "read") else str(upload or "").encode()
            # parsing is bounded, but a big file still takes a moment: not on the event loop
            plan = await asyncio.to_thread(importer.parse_mappings_file, raw,
                                           getattr(upload, "filename", "") or "")
        except ConfigError as e:
            return await settings_get(request, error=_("Import failed: {0}").format(e))
        mode = form.get("mode", "merge")
        if form.get("step") == "preview":
            if mode not in importer.MODES:
                return await settings_get(request, error=_("Unknown import mode."))
            return await _import_preview(request, plan, mode)
        # legacy one-step import: replace all mappings
        return await _apply_import(request, plan, "replace")

    async def _import_preview(request, plan, mode):
        """Show what importing `plan` would change; nothing changes until it is applied."""
        current = state.config.mappings
        rows = importer.plan_rows(current, plan.mappings, mode)
        candidate, _touched = importer.apply_import(current, plan.mappings, mode)
        problem = None
        try:
            dataclasses.replace(state.config, mappings=candidate).validate_for_save()
        except ConfigError as e:
            problem = str(e)
        preview = {"token": _pending_put(_who(request), plan), "mode": mode, "rows": rows,
                   "source": plan.source, "warnings": plan.warnings, "problem": problem,
                   "count": len(plan.mappings)}
        return await settings_get(request, import_preview=preview)

    async def config_import_apply(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        token = form.get("token", "")
        item = _pending_get(token, _who(request))
        if item is None:
            return await settings_get(request, error=_("This import preview has expired. "
                                                       "Upload the file again."))
        mode = form.get("mode", "merge")
        if mode not in importer.MODES:
            return await settings_get(request, error=_("Unknown import mode."))
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
                return await settings_get(request, error=_("Import rejected: {0}").format(e))
        if mode == "replace":
            await state.supervisor.stop_all()
            state.forget_mappings({m.id for m in old} - {m.id for m in new_maps})
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
            ok = _("Imported {0} mappings (replaced the existing ones).").format(len(new_maps))
        else:
            ok = _("Imported: {0} added, {1} updated, {2} unchanged.").format(
                count["add"], count["update"], count["same"])
        return await settings_get(request, ok=ok + " " + _("The previous configuration was kept "
                                                         "as a backup."))

    # ---------------- event notifications (admin) ----------------
    def _notify_from_form(form, old: NotificationSettings) -> NotificationSettings:
        try:
            port = int(form.get("notify_smtp_port") or 587)
            repeat = int(float(form.get("notify_repeat_min") or 5) * 60)
        except ValueError:
            raise ConfigError(_("SMTP port and repeat interval must be numbers.")) from None
        url, pw = form.get("notify_webhook_url", "").strip(), form.get("notify_smtp_password", "")
        return NotificationSettings.from_dict({
            "enabled": _checkbox(form, "notify_enabled"),
            "events": [e for e in NOTIFY_EVENTS if _checkbox(form, f"notify_event_{e}")],
            "repeat_s": repeat,
            "webhook_url": url if url else ("" if _checkbox(form, "notify_webhook_clear") else old.webhook_url),
            "webhook_format": form.get("notify_webhook_format", "json"),
            "telegram_chat_id": form.get("notify_telegram_chat_id", "").strip(),
            "email_to": form.get("notify_email_to", "").strip(),
            "email_from": form.get("notify_email_from", "").strip(),
            "smtp_host": form.get("notify_smtp_host", "").strip(),
            "smtp_port": port,
            "smtp_security": form.get("notify_smtp_security", "starttls"),
            "smtp_username": form.get("notify_smtp_username", "").strip(),
            "smtp_password": pw if pw else old.smtp_password,   # blank => keep the stored one
        })

    async def settings_notifications_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        async with state.config_lock:
            old = state.config.notifications
            try:
                state.config.notifications = _notify_from_form(form, old)
                await state.asave()
            except ConfigError as e:
                state.config.notifications = old
                return await settings_get(request, error=str(e))
        new = state.config.notifications
        state.log(f"notification settings saved (enabled={new.enabled}, events={','.join(new.events)})")
        ctx.audit(request, "notification_settings", "enabled" if new.enabled else "disabled")
        if form.get("then") == "test":
            errors = await state.notifier.deliver("test", "", _("If you can read this, "
                                                    "ser2net can reach you."))
            if errors:
                return await settings_get(request, error=_("Saved, but the test failed: {0}").format(
                    "; ".join(errors)))
            return await settings_get(request, ok=_("Saved; the test notification was sent."))
        return await settings_get(request, ok=_("Notification settings saved."))

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
            return PlainTextResponse(_("No such backup."), status_code=404)
        ctx.audit(request, "config_backup_download", name)
        return Response(_without_session_key(text), media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    async def backup_restore(request):
        """Restore a backup's mappings and settings. User accounts, the REST API token
        and the cluster key stay as they are unless `accounts` is ticked: a backup from
        before a password change, a deleted account or a revoked token would otherwise
        bring those back. With `accounts`, they come from the backup too and a new
        session key ends every session made since (the admin restoring stays signed in
        if the backup has their account). A POST without the box (a script written for
        2.8) restores everything else, as before."""
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        name = form.get("name", "")
        with_accounts = _checkbox(form, "accounts")
        try:
            restored = AppConfig.from_dict(json.loads(await asyncio.to_thread(state.store.read_backup, name)))
        except (ConfigError, OSError, ValueError, TypeError, AttributeError) as e:
            return await settings_get(request, error=_("Could not read the backup: {0}").format(e))
        if with_accounts and not restored.users:
            return await settings_get(request, error=_("This backup has no user accounts (it "
                                                       "was made before the first-run setup); "
                                                       "restoring it would open the setup page "
                                                       "to anyone. Not restored."))
        if with_accounts and not restored.admin_count():
            return await settings_get(request, error=_("This backup has no admin account, so its "
                                                       "accounts can't be restored. Not restored."))
        # never from the backup: the session key (renewed below with the accounts), this
        # node's cluster id and what start-up accepted while broken, so a restore is refused
        # only for what it changes or adds (AppConfig.validate_for_save); and unless asked,
        # the accounts and credentials
        keep = {"secret_key", "instance_id", "load_notes", "ids_created", "tolerated"}
        if not with_accounts:
            keep |= {"users", "api_token_hash", "api_token_role"}
        notes = []
        async with state.config_lock:
            cur = state.config
            previous = {f.name: getattr(cur, f.name) for f in dataclasses.fields(AppConfig)}
            usable_before = cur.usable_admin_count()
            for f in dataclasses.fields(AppConfig):
                if f.name not in keep:
                    setattr(cur, f.name, getattr(restored, f.name))
            if with_accounts:
                cur.secret_key = secrets.token_hex(32)
            else:  # both cluster keys are credentials: the key and the one still accepted
                was = previous["cluster"]
                cur.cluster = dataclasses.replace(cur.cluster, key=was.key,
                                                  previous_key=was.previous_key,
                                                  previous_until=was.previous_until)
                if cur.cluster.enabled and not cur.cluster.key.strip():
                    cur.cluster.enabled = False
                    notes.append(_("The cluster stays off: this backup had it on, but its key "
                                   "was not restored."))
            problem = None
            if usable_before and not cur.usable_admin_count():
                problem = _("no admin could sign in afterwards (LDAP/OIDC accounts need their "
                            "sign-in method on)")
            else:
                try:
                    await state.asave()
                except ConfigError as e:
                    problem = str(e)
            if problem:
                for k, v in previous.items():
                    setattr(cur, k, v)
                return await settings_get(request, error=_("Restore rejected: {0}").format(problem))
        if with_accounts:
            state.revoked_sessions.clear()  # signed with the old key: dead anyway
        await state.supervisor.stop_all()
        state.forget_mappings({m.id for m in previous["mappings"]} - {m.id for m in cur.mappings})
        await state.supervisor.start_all(state.config)
        await state.cluster.stop()
        await state.cluster.start()
        before = types.SimpleNamespace(**previous)
        changes = _restore_changes(before, cur, with_accounts)
        tok = i18n.set_language("en")  # the log and the audit log stay English
        try:
            audit_changes = _restore_changes(before, cur, with_accounts)
        finally:
            i18n.reset(tok)
        state.log(f"configuration restored from backup {name} "
                  f"({'with' if with_accounts else 'without'} its accounts)")
        for note in restored.load_notes:  # what loading the backup changed, as at start-up
            state.log(note)
        ctx.audit(request, "config_backup_restore",
                  "; ".join([name, "accounts restored" if with_accounts else "accounts kept",
                             *audit_changes]))
        ok = [_("Restored {0}; the configuration it replaced was backed up too. A changed admin "
                "UI address or TLS takes effect after a restart.").format(name)]
        ok.append(_("Everyone else was signed out.") if with_accounts else
                  _("User accounts, the REST API token and the cluster key were kept."))
        if changes:
            ok.append(_("Access-related changes: {0}.").format("; ".join(changes)))
        resp = await settings_get(request, ok=" ".join(ok + notes))
        me = current_user(request)
        mine = (cur.get_user(me.username)
                if with_accounts and me is not None and me.source != "anonymous" else None)
        if mine is not None:
            set_session(resp, request, mine)
        return resp

    async def backup_restore_mappings(request):
        """Restore only a backup's mappings, through the import preview (replace)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        name = form.get("name", "")
        try:
            raw = await asyncio.to_thread(state.store.read_backup, name)
            plan = importer.parse_mappings_file(raw.encode("utf-8"), name)
        except (ConfigError, OSError) as e:
            return await settings_get(request, error=_("Could not read the backup: {0}").format(e))
        plan.source = _("configuration backup ({0} UTC)").format(backup_time(name))
        return await _import_preview(request, plan, "replace")

    async def settings_cluster_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        try:
            port = int(form.get("discovery_port") or 41750)
        except ValueError:
            return await settings_get(request, error=_("Cluster discovery port must be a number."))
        peers = [ln.strip() for ln in (form.get("peers") or "").splitlines() if ln.strip()]
        try:  # each entry saved here must name an IP address or host name
            for entry in peers:
                parse_cluster_peer(entry, strict=True)
        except ConfigError as e:
            return await settings_get(request, error=str(e))
        generate = form.get("then") == "generate"
        async with state.config_lock:
            old = state.config.cluster
            # The key is never shown: a blank field keeps it, "Generate" makes one (shown
            # once). A changed key keeps the old one accepted for a while unless that is
            # unticked (a planned change vs. a leaked key), so the fleet isn't split
            # until every node has the new one.
            key = secrets.token_urlsafe(32) if generate else (
                (form.get("key") or "").strip() or old.key)
            if _checkbox(form, "clear_key") and not generate:
                key = ""
            previous, until = old.previous_key, old.previous_until
            changed = key_bytes(key) != key_bytes(old.key)
            if changed:
                keep = bool(old.key.strip()) and _checkbox(form, "keep_old_key")
                previous, until = (old.key, time.time() + KEY_OVERLAP_S) if keep else ("", 0.0)
            if _checkbox(form, "clear_previous_key") or not key.strip():
                previous, until = "", 0.0
            # a form from before these switches existed (an open tab during an upgrade, a
            # script) leaves them as they are; an unticked box is only "off" in this form
            current_form = form.get("cluster_form") == "2"
            new = ClusterSettings(
                enabled=form.get("enabled") == "on",
                key=key,
                discovery_port=port,
                advertise_ip=(form.get("advertise_ip") or "").strip(),
                peers=peers,
                previous_key=previous, previous_until=until,
                legacy_auth=_checkbox(form, "legacy_auth") if current_form else old.legacy_auth,
                discovery=_checkbox(form, "discovery") if current_form else old.discovery,
            )
            state.config.cluster = new
            try:
                await state.asave()
            except ConfigError as e:
                state.config.cluster = old
                return await settings_get(request, error=str(e))
        # apply live: restart discovery so a key/port/enable change takes effect now
        await state.cluster.stop()
        await state.cluster.start()
        state.log(f"cluster settings updated (enabled={new.enabled}, manual_peers={len(peers)}, "
                  f"legacy_auth={new.legacy_auth}, discovery={new.discovery})")
        ctx.audit(request, "cluster_settings",
                    "enabled" if new.active else "disabled")
        ok = _("Cluster settings saved.")
        if changed:
            kept = bool(new.previous_key) and new.previous_key == old.key
            how = "generated" if generate else ("removed" if not key else "set")
            until_text = time.strftime("%Y-%m-%d %H:%M", time.gmtime(until)) if kept else ""
            previous_note = (f"previous key accepted until {until_text} UTC" if kept
                             else "previous key not accepted")
            state.log(f"cluster key {how}, {previous_note}")
            ctx.audit(request, "cluster_key_change", f"{how}, {previous_note}")
            if kept:
                ok += " " + _("The previous key is still accepted until {0} UTC, so the nodes "
                              "not changed yet keep working; set the new key on each of "
                              "them.").format(until_text)
            elif key:
                ok += " " + _("Only the new key is accepted now: set it on every node.")
        elif old.previous_key and not new.previous_key:
            state.log("cluster: the previous key is no longer accepted")
            ctx.audit(request, "cluster_key_change", "previous key no longer accepted")
        if generate:
            return await settings_get(request, new_cluster_key=key, ok=ok)
        return await settings_get(request, ok=ok)

    async def settings_cluster_identity(request):
        """Give this node a new cluster id and a new session key - the way out for a copy
        of another installation (its data folder, disk or VM image): the copies no longer
        hide each other in the cluster, and a sign-in to one is not valid on the other.
        Everyone signed in here is signed out, except the admin doing this."""
        if deny := require_role(request, "admin"):
            return deny
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        async with state.config_lock:
            old = (state.config.instance_id, state.config.secret_key)
            state.config.instance_id = secrets.token_hex(8)
            state.config.secret_key = secrets.token_hex(32)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.instance_id, state.config.secret_key = old
                return await settings_get(request, error=str(e))
        await state.cluster.stop()
        await state.cluster.start()
        new_id = state.config.instance_id
        state.log(f"new identity for this node: cluster id {old[0][:8]} -> {new_id[:8]}, new "
                  "session key (everyone else signed in here is signed out)")
        ctx.audit(request, "node_new_identity", f"{old[0][:8]} -> {new_id[:8]}")
        resp = await settings_get(request, ok=_("This node has a new identity (cluster ID {0}). "
                                                "Everyone else signed in to it was signed out.")
                                  .format(new_id[:8]))
        user = current_user(request)
        if user is not None and user.source != "anonymous":
            set_session(resp, request, user)  # signed with the new key: this admin stays in
        return resp

    return [
        Route("/settings", settings_page, methods=["GET"]),
        Route("/settings/password", settings_password_post, methods=["POST"]),
        Route("/settings/sessions/signout-others", settings_signout_others, methods=["POST"]),
        Route("/settings/tls", settings_tls_post, methods=["POST"]),
        Route("/settings/tls/generate", settings_tls_generate, methods=["POST"]),
        Route("/settings/access", settings_access_post, methods=["POST"]),
        Route("/settings/proxy", settings_proxy_post, methods=["POST"]),
        Route("/settings/node-name", settings_node_name_post, methods=["POST"]),
        Route("/settings/sessions/revoke-all", settings_sessions_revoke_all, methods=["POST"]),
        Route("/settings/api-token", settings_api_token_post, methods=["POST"]),
        Route("/settings/api-token/revoke", settings_api_token_revoke, methods=["POST"]),
        Route("/settings/users", settings_users_create, methods=["POST"]),
        Route("/settings/users/password", settings_users_password, methods=["POST"]),
        Route("/settings/users/role", settings_users_role, methods=["POST"]),
        Route("/settings/users/delete", settings_users_delete, methods=["POST"]),
        Route("/settings/users/signout", settings_users_signout, methods=["POST"]),
        Route("/settings/users/expiry", settings_users_expiry, methods=["POST"]),
        # the user name in the URL: older pages and scripts (only names without /)
        Route("/settings/users/{username}/role", settings_users_role, methods=["POST"]),
        Route("/settings/users/{username}/delete", settings_users_delete, methods=["POST"]),
        Route("/settings/users/{username}/signout", settings_users_signout, methods=["POST"]),
        Route("/settings/ldap", settings_ldap_post, methods=["POST"]),
        Route("/settings/oidc", settings_oidc_post, methods=["POST"]),
        Route("/settings/cluster", settings_cluster_post, methods=["POST"]),
        Route("/settings/cluster/identity", settings_cluster_identity, methods=["POST"]),
        Route("/settings/config/export", config_export),
        # the body is refused (413) past the import file limit, before it is spooled
        Route("/settings/config/import", config_import, methods=["POST"],
              max_body_size=IMPORT_MAX_BYTES),
        Route("/settings/config/import/apply", config_import_apply, methods=["POST"]),
        Route("/settings/notifications", settings_notifications_post, methods=["POST"]),
        Route("/settings/audit", audit_page),
        Route("/settings/audit.csv", audit_csv),
        Route("/settings/backups/restore", backup_restore, methods=["POST"]),
        Route("/settings/backups/restore-mappings", backup_restore_mappings, methods=["POST"]),
        Route("/settings/backups/{name}", backup_download),
    ]


def _key_hint(key: str) -> str:
    """Why the cluster key deserves a warning: "weak" (any host on the network can try
    to guess it offline from a beacon), "ascii" (nodes up to 2.8.0 can't use it) or ""."""
    k = unicodedata.normalize("NFKC", key or "").strip()
    if not k:
        return ""
    if len(k) < 16 or len(set(k)) < 8:
        return "weak"
    return "" if k.isascii() else "ascii"


# ---------------- what a restore changes for sign-in and access ----------------
def _setting_changes(old, new) -> list[str]:
    """Sign-in / access settings and the cluster's that differ between two configurations
    (AppConfig or the same fields), as lines for the user (and, in English, the audit
    log)."""
    def onoff(value) -> str:
        return _("enabled") if value else _("disabled")

    def role(value) -> str:
        return value or _("— deny login —")

    out = []

    def diff(label, a, b) -> None:
        if a != b:
            out.append(f"{label}: {a} → {b}")

    o, n = old.admin_ui, new.admin_ui
    diff(_("Login required"), onoff(o.require_login), onoff(n.require_login))
    diff(_("Role for visitors without login"), o.anonymous_role, n.anonymous_role)
    diff(_("Admin UI address"), f"{o.bind_ip}:{o.port}", f"{n.bind_ip}:{n.port}")
    diff(_("Instance name"), o.node_name or "—", n.node_name or "—")
    diff(_("Admin TLS (HTTPS)"), onoff(o.tls_enabled), onoff(n.tls_enabled))
    diff(_("HSTS max-age (seconds)"), o.hsts_max_age, n.hsts_max_age)
    diff(_("Public URL"), o.public_url or "—", n.public_url or "—")
    diff(_("Trusted proxies"), " ".join(o.trusted_proxies) or "—",
         " ".join(n.trusted_proxies) or "—")
    diff(_("HSTS through the reverse proxy"), onoff(o.proxy_hsts), onoff(n.proxy_hsts))
    for label, a, b in (("LDAP", old.ldap, new.ldap), ("OIDC", old.oidc, new.oidc)):
        diff(label, onoff(a.enabled), onoff(b.enabled))
        diff(f"{label} · {_('Default role')}", role(a.default_role), role(b.default_role))
        diff(f"{label} · {_('Admin group')}", a.admin_group or "—", b.admin_group or "—")
    diff(f"LDAP · {_('Server certificate check')}", onoff(old.ldap.tls_verify),
         onoff(new.ldap.tls_verify))

    def days(value) -> str:
        return _("{0} days").format(value) if value else _("never")

    diff(_("Remove LDAP / OIDC accounts not signed in for"),
         days(old.external_users_expire_days), days(new.external_users_expire_days))
    # the cluster's settings (its keys are credentials: _account_changes)
    o, n = old.cluster, new.cluster
    cluster = _("LAN cluster")
    diff(cluster, onoff(o.enabled), onoff(n.enabled))
    diff(f"{cluster} · {_('Legacy authentication')}", onoff(o.legacy_auth), onoff(n.legacy_auth))
    diff(f"{cluster} · {_('Automatic discovery')}", onoff(o.discovery), onoff(n.discovery))
    diff(f"{cluster} · {_('Discovery UDP port')}", o.discovery_port, n.discovery_port)
    diff(f"{cluster} · {_('Advertise IP')}", o.advertise_ip or "—", n.advertise_ip or "—")
    diff(f"{cluster} · {_('Manual peers')}", ", ".join(o.peers) or "—", ", ".join(n.peers) or "—")
    return out


def _account_changes(old, new) -> list[str]:
    """What restoring a backup's accounts and credentials would change."""
    before = {u.username: u for u in old.users}
    after = {u.username: u for u in new.users}
    out = []
    if added := [n for n in after if n not in before]:
        out.append(_("Accounts it brings back: {0}").format(", ".join(added)))
    if removed := [n for n in before if n not in after]:
        out.append(_("Accounts it removes: {0}").format(", ".join(removed)))
    if changed := [n for n in after if n in before and
                   (after[n].password_hash, after[n].role, after[n].source)
                   != (before[n].password_hash, before[n].role, before[n].source)]:
        out.append(_("Accounts with another password or role: {0}").format(", ".join(changed)))
    if (old.api_token_hash, old.api_token_role) != (new.api_token_hash, new.api_token_role):
        out.append(_("The REST API token as it was then (the current one stops working)"))
    if (old.cluster.key, old.cluster.previous_key) != (new.cluster.key, new.cluster.previous_key):
        out.append(_("The cluster key as it was then"))
    return out


def _restore_changes(old, new, with_accounts: bool) -> list[str]:
    return _setting_changes(old, new) + (_account_changes(old, new) if with_accounts else [])


def _restore_preview(name: str, cur, backup, me) -> dict:
    notes = []
    accounts_ok = bool(backup.users) and backup.admin_count() > 0
    if not accounts_ok:
        notes.append(_("This backup has no admin account, so its accounts can't be restored."))
    elif me is not None and me.source != "anonymous":
        mine = backup.get_user(me.username)
        if mine is None:
            notes.append(_("Your account “{0}” is not in this backup: restoring its accounts signs "
                           "you out.").format(me.username))
        elif mine.role != me.role:
            notes.append(_("In this backup your account “{0}” has the role {1}.").format(
                me.username, mine.role))
    return {"name": name, "time": backup_time(name), "settings": _setting_changes(cur, backup),
            "accounts": _account_changes(cur, backup), "accounts_ok": accounts_ok, "notes": notes}


def _without_session_key(text: str) -> str:
    """A downloaded backup leaves out secret_key, which signs the session cookies.
    Restoring never takes the key from a backup, and a config.json without one gets a
    new key at the next start."""
    try:
        doc = json.loads(text)
    except ValueError:
        return text
    if not isinstance(doc, dict) or "secret_key" not in doc:
        return text
    del doc["secret_key"]
    return json.dumps(doc, indent=2, ensure_ascii=False)


def _csv_cell(value: str) -> str:
    """A spreadsheet must not run a cell as a formula (mapping names and user names
    end up in the audit detail): prefix the characters that start one."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
