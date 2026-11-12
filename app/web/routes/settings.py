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
import shutil
import socket
import time
import types
import unicodedata
from urllib.parse import urlencode

from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ... import clock, console, i18n, importer, optional, reload, storage, tlsreload
from ...config import (
    API_TOKEN_DAYS_MAX, API_TOKEN_NAME, API_TOKEN_ROLES, API_TOKENS_MAX, BACKUPS_MIN_KEPT,
    EXPIRE_DAYS_MAX, EXPIRE_DAYS_MIN, HSTS_MAX_AGE_MAX, LOG_DAYS_MAX, NODE_NAME_ENV,
    NODE_NAME_MAX, NOTIFY_EVENTS, REQUEST_LOG_MODES, ROLES, SMTP_SECURITY, TRACE_DAYS_MAX,
    UI_LANGUAGES, WEBHOOK_FORMATS, LOGIN_NOTICE_MAX,
    LOGIN_NOTICE_URL_MAX, ApiToken, AppConfig, ClusterSettings, ConfigError,
    NotificationSettings, User, backup_time, clean_login_notice, data_path, host_port,
    login_notice_problem, login_notice_url_problem, new_api_token_id, new_pwd_version,
    node_name_from_env, node_name_problem, normalize_public_url, parse_cluster_peer,
    proxy_entry_problem, public_url_problem, split_proxy_entries, tls_file_message,
)
from ...engine import tls
from ...engine.cluster import KEY_OVERLAP_S, key_bytes
from ...explain import explain
from ...i18n import _, ngettext
from ...textfold import search_key
from .. import auth, certinfo, ldap_auth, oidc_auth, proxy, selfsigned
from . import ops
from .common import Ctx, client_ip, csrf_failed, read_form
from .forms import (
    _checkbox, _ldap_from_form, _oidc_from_form, _password_problem, _username_problem, json_text,
)

# a mappings file to import may be larger than other requests (server.MAX_BODY_BYTES):
# the importer's file limit, plus the form's other fields
IMPORT_MAX_BYTES = importer.MAX_FILE_BYTES + 64 * 1024

# how long what a Settings form did waits for the page its redirect leads to
FLASH_TTL_S = 120


class _Flash:
    """What the Settings page says after a form's redirect (Post/Redirect/Get): the form's
    message and section, and a token or key that is shown only once. Kept in memory for
    one browser and user - the browser's CSRF cookie (a random value it keeps for its
    session) and the user name - and taken by the next Settings page that browser loads;
    never put in a URL or a cookie."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def put(self, browser: str, user: str, **what) -> None:
        if not browser:
            return
        now = time.monotonic()
        for k in [k for k, v in self._items.items() if now - v["at"] > FLASH_TTL_S]:
            del self._items[k]
        self._items.pop(browser, None)   # the newest, last
        while len(self._items) >= 100:
            self._items.pop(next(iter(self._items)))
        self._items[browser] = {"at": now, "user": user, **what}

    def take(self, browser: str, user: str) -> dict | None:
        item = self._items.pop(browser, None) if browser else None
        if item is None or item["user"] != user or time.monotonic() - item["at"] > FLASH_TTL_S:
            return None
        return item


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

    def _server_clock(admin: bool) -> dict:
        """The server's time for the Admin interface panel, and (admins only: it names the
        newest backup's time) whether the clock reads earlier than it can (clock.behind)."""
        now = time.time()
        behind = clock.behind(state.store, now) if admin else None
        return {"server_utc": clock.utc_text(now), "server_local": clock.stamp(now),
                "clock_behind": {"what": behind[0], "when": clock.utc_text(behind[1])}
                if behind else None}

    def _tls_certificate() -> dict | None:
        """What the admin UI's certificate file says (certinfo), for Settings → Admin TLS:
        None without one, {"problem", "detail"} when it can't be read."""
        path = state.config.admin_ui.tls_cert
        if not path:
            return None
        try:
            info = certinfo.read(path)
        except certinfo.CertReadError as e:
            return {"problem": e.code, "detail": str(e)}
        now = time.time()
        return {"subject": info.subject, "names": ", ".join(info.names),
                "self_signed": info.self_signed, "state": info.state(now),
                "days": info.days_left(now), "start": clock.utc_text(info.not_before)[:16],
                "end": clock.utc_text(info.not_after)[:16]}

    def _clock_and_certificate(admin: bool) -> dict:
        return {**_server_clock(admin), "tls_certificate": _tls_certificate() if admin else None}
    flash = _Flash()

    def _browser(request) -> str:
        """Which browser asks, for the flash: its CSRF token (a form's POST has one), as
        GuardMiddleware read it from the cookie under this port's name (or moved it from
        the name of 2.8), so the page the redirect leads to finds it under that name."""
        return (getattr(request.state, "csrf_token", None)
                or auth.cookie(request, auth.CSRF_COOKIE) or "")

    async def settings_get(request, ok=None, error=None, new_api_token=None, import_preview=None,
                           new_cluster_key=None, restore_preview=None, section=None,
                           status_code=200, submitted=None):
        """The Settings page. `section`: the id of the part whose form `ok` / `error` is
        about (shown there); `submitted`: what a refused form had, shown again in it."""
        me = current_user(request)
        if request.method == "GET" and (done := flash.take(_browser(request), _who(request))):
            ok, error, section = done["ok"], done["error"], done["section"]
            new_api_token = done.get("new_api_token")
            new_cluster_key = done.get("new_cluster_key")
        admin = me is not None and me.role == "admin"
        # the list reads the backup files: not on the event loop (ConfigStore.backups)
        backups = await asyncio.to_thread(state.store.backups) if admin else []
        # names in a backup that are no account now (deleted since): Settings can take
        # them out of every backup
        current = {u.username for u in state.config.users}
        gone_accounts = sorted({a["username"] for b in backups for a in b["accounts"]
                                if a["username"] and a["username"] not in current})
        disk = await asyncio.to_thread(storage.usage, state.data_dir) if admin else None
        # config.json edited outside ser2net: not in use yet, or replaced by a save since
        outside = await asyncio.to_thread(state.store.changed_on_disk) if admin else None
        replaced = state.store.replaced_edit if admin else None
        ldap_problem, oidc_problem = await asyncio.to_thread(_package_problems)
        # the redirect URI a sign-in started from this page's address would send
        published = proxy.public_url(state.config.admin_ui)
        oidc_redirect = oidc_auth.redirect_uri(state.config.oidc, request, published)
        server_clock = await asyncio.to_thread(_clock_and_certificate, admin)
        # a page sent as the answer to a form names the page's own address: the address bar
        # gets it (app.js), and the TR / EN switch goes back there (the form's URL may answer
        # only POST); without the base path, as the TR / EN switch takes it
        page_url = None if request.method == "GET" else "/settings" + (f"#{section}" if section else "")
        serve_ip, serve_port, serving_tls = state.admin_serving()
        configured = state.config.admin_ui
        hint_kind, hint_command = console.reconfigure_hint(state.data_dir, state.service_name)
        resp = render(request, "settings.html", status_code=status_code, ok=ok, error=error,
                      **server_clock, section=section, submitted=submitted or {},
                      page_url=page_url,
                      outside_edit=_local_time(outside) if outside else "",
                      outside_gone=outside == 0.0,
                      replaced_edit=dict(replaced, edited=_local_time(replaced["edited"]),
                                         saved=_local_time(replaced["saved"]))
                      if replaced else None,
                      serving_address=host_port(serve_ip, serve_port), serving_tls=serving_tls,
                      # what config.json sets and the admin UI uses only from the next start
                      pending_address="" if (configured.bind_ip, configured.port) == (
                          serve_ip, serve_port) else host_port(configured.bind_ip, configured.port),
                      pending_tls=bool(state.admin_tls_waiting)
                      or configured.tls_enabled != serving_tls,
                      address_hint=hint_kind, address_command=hint_command,
                      import_preview=import_preview, restore_preview=restore_preview,
                      backups=backups, backup_gone_accounts=gone_accounts,
                      newer_writer=state.config.newer_writer,
                      unknown_keys=state.config.unknown_keys,
                      admin=state.config.admin_ui, hsts_max=HSTS_MAX_AGE_MAX,
                      hostname=socket.gethostname(), node_name_max=NODE_NAME_MAX,
                      request_log_modes=REQUEST_LOG_MODES, login_notice_max=LOGIN_NOTICE_MAX,
                      login_notice_url_max=LOGIN_NOTICE_URL_MAX,
                      node_name_env=NODE_NAME_ENV if node_name_from_env() else "",
                      public_url=published, public_url_env=bool(proxy.public_url_from_env()),
                      trusted=proxy.trusted_entries(state.config.admin_ui),
                      api_token_set=bool(state.config.api_token_hash),
                      api_token_role=state.config.api_token_role,
                      api_token_last_used=_used(""), api_token_rows=_api_token_rows(),
                      api_token_max=API_TOKENS_MAX, api_token_days_max=API_TOKEN_DAYS_MAX,
                      new_api_token=new_api_token, new_cluster_key=new_cluster_key,
                      me=current_user(request), users=state.config.users, roles=ROLES,
                      last_sign_in={u.username: _sign_in_time(u) for u in state.config.users},
                      expire_days=state.config.external_users_expire_days,
                      expire_min=EXPIRE_DAYS_MIN, expire_max=EXPIRE_DAYS_MAX,
                      trace_days=state.config.trace_max_age_days, trace_days_max=TRACE_DAYS_MAX,
                      log_days=state.config.log_max_age_days, log_days_max=LOG_DAYS_MAX,
                      backups_min=BACKUPS_MIN_KEPT, disk=disk, size_text=storage.size_text,
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
                      webhook_formats=WEBHOOK_FORMATS, smtp_security=SMTP_SECURITY)
        if new_api_token or new_cluster_key:
            resp.headers["Cache-Control"] = "no-store"   # shown once: kept by no cache either
        return resp

    def _done(request, section: str, ok: str | None = None, error: str | None = None,
              **once) -> Response:
        """The answer to a Settings form that did its work (Post/Redirect/Get): a redirect
        to the page at the form's section, which then shows `ok` (or `error`: saved, but
        something after it failed) and `once` (a token or key shown only once) from the
        flash. A reload or Back then loads the page instead of posting the form again (a
        new API token, a new certificate, a restore that restarts every mapping), and the
        address is one the TR / EN switch can go back to."""
        flash.put(_browser(request), _who(request), section=section, ok=ok, error=error, **once)
        return proxy.redirect(request, f"/settings#{section}")

    async def _failed(request, section: str, error: str, **submitted) -> Response:
        """The answer to a Settings form that changed nothing: the page at once, as a 400,
        with the reason in the form's section and the form filled in with what was sent
        (`submitted`; never a password or key)."""
        return await settings_get(request, error=error, section=section, status_code=400,
                                  submitted=submitted)

    def _typed_again(form, *names) -> str:
        """For the error of a form that is shown again: a password or key typed in it
        isn't (it never goes into a page)."""
        if any(str(form.get(n) or "").strip() for n in names):
            return " " + _("Passwords and keys you typed are not shown again: type them once more.")
        return ""

    async def settings_page(request):
        """GET /settings; ?restore=<backup> shows what restoring that backup would change,
        ?import=<id> the preview of an uploaded mappings file (_import_preview)."""
        if (pending := request.query_params.get("import")) is not None \
                and not require_role(request, "operator"):
            item = _pending_get(pending, _who(request))
            if item is None:
                return await settings_get(request, section="import", error=_(
                    "This import preview has expired. Upload the file again."))
            return await settings_get(request, import_preview=_preview_of(pending, item))
        name = request.query_params.get("restore")
        if name is None or require_role(request, "admin"):
            return await settings_get(request)
        try:
            backup = AppConfig.from_dict(json.loads(
                await asyncio.to_thread(state.store.read_backup, name)))
        except (ConfigError, OSError, ValueError, TypeError, AttributeError) as e:
            return await settings_get(request, section="backups",
                                      error=_("Could not read the backup: {0}").format(_unreadable(e)))
        return await settings_get(request, restore_preview=_restore_preview(
            name, state.config, backup, current_user(request)))

    async def settings_password_post(request):
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        user = current_user(request)
        if user is not None and user.source == "anonymous":
            return await _failed(request, "account", _("Sign in to change your password."))
        if user is not None and user.source != "local":
            return await _failed(request, "account",
                                 _("Your account is managed by {0}; change your "
                                   "password there.").format(user.source.upper()))
        cur = form.get("current", "")
        new = form.get("password", "")
        new2 = form.get("password2", "")
        if user is None:
            return await _failed(request, "account", _("Current password is incorrect."))
        # wrong current passwords count like failed sign-ins under this name (a session
        # someone else got hold of must not be a way to guess the password itself)
        if state.name_limiter.blocked(user.username):
            return await _failed(request, "account", _("Too many attempts. Wait a few minutes "
                                                       "and try again."))
        state.name_limiter.record_failure(user.username)
        if not await state.password_work.run(auth.verify_password, cur, user.password_hash):
            state.log(f"password change for {user.username} refused: wrong current password "
                      f"(from {ctx.client_ip(request)})")
            ctx.audit(request, "password_change_failed", user.username)
            return await _failed(request, "account", _("Current password is incorrect."))
        state.name_limiter.reset(user.username)
        err = _password_problem(new, new2)
        if err:
            return await _failed(request, "account", err)
        new_hash = await state.password_work.run(auth.hash_password, new)
        try:
            async with state.edit_config():
                user.password_hash = new_hash
                user.pwd_version += 1
        except ConfigError as e:  # not saved: the password stays as it was
            return await _failed(request, "account", str(e))
        state.log(f"password changed for {user.username}; their other sessions signed out")
        ctx.audit(request, "password_change", user.username)
        # refresh THIS session so the user isn't logged out by their own change
        resp = _done(request, "account", ok=_("Password updated. Your other sessions were "
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

    def _bump_sessions(username: str):
        """Sign every session of `username` out (pwd_version + 1), in a state.edit_config()
        block. Returns the user; raises ConfigError when there is none."""
        target = state.config.get_user(username)
        if target is None:
            raise ConfigError(_("User not found."))
        for u in (target, *_spellings(target)):
            u.pwd_version += 1
        return target

    async def settings_signout_others(request):
        """Any signed-in user: end their own sessions elsewhere, keep this one. Nothing
        to do (no save) when no session was issued for the account since its last
        click (AppState.others_signed_out)."""
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        me = current_user(request)
        if me is None or me.source == "anonymous":
            return await _failed(request, "account", _("Sign in to manage your sessions."))
        user = None
        try:
            async with state.edit_config():
                if state.others_signed_out.get(me.username) != (
                        me.pwd_version, state.sessions_issued.get(me.username, 0)):
                    user = _bump_sessions(me.username)
        except ConfigError as e:
            return await _failed(request, "account", str(e))
        if user is None:
            return _done(request, "account", ok=_("Your other sessions are already signed out."))
        state.log(f"{me.username} signed out their other sessions")
        ctx.audit(request, "sessions_signout_others", me.username)
        resp = _done(request, "account", ok=_("Your other sessions were signed out."))
        set_session(resp, request, user)
        state.others_signed_out[user.username] = (user.pwd_version,
                                                  state.sessions_issued.get(user.username, 0))
        return resp

    async def settings_tls_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        cert, key = form.get("tls_cert", "").strip(), form.get("tls_key", "").strip()
        ui = state.config.admin_ui
        # never shown: a blank field keeps the stored password, unless the box removes it
        password = (form.get("tls_key_password", "") or (
            "" if _checkbox(form, "tls_key_password_clear") else ui.tls_key_password))
        hsts = ui.hsts_max_age   # kept when the form has no such field (an older page)
        # refused: the form again with what was sent (the stored key password only says
        # whether one is stored; it is never shown)
        shown = {"tls_cert": cert, "tls_key": key, "tls_key_password": ui.tls_key_password,
                 "hsts_max_age": form.get("hsts_max_age", hsts)}
        if "hsts_max_age" in form:
            raw = str(form.get("hsts_max_age") or "").strip()
            if not (raw.isascii() and raw.isdigit() and int(raw) <= HSTS_MAX_AGE_MAX):
                return await _failed(request, "tls", _(
                    "The HSTS max-age must be a whole number of seconds from 0 to {0}.")
                    .format(HSTS_MAX_AGE_MAX) + _typed_again(form, "tls_key_password"), tls=shown)
            hsts = int(raw)
        old_hsts = ui.hsts_max_age
        try:
            async with state.edit_config():
                admin = state.config.admin_ui
                admin.tls_cert, admin.tls_key, admin.hsts_max_age = cert, key, hsts
                admin.tls_key_password = password
                admin.check_tls_files()  # here, not at every save
                # a pair that exists but doesn't load would stop the next start
                # (runtime._serve); one that loads is what new connections get
                prepared = await _admin_tls(admin)
        except ConfigError as e:
            return await _failed(request, "tls", str(e) + _typed_again(form, "tls_key_password"),
                                 tls=shown)
        if hsts != old_hsts:
            state.log(f"admin TLS: HSTS max-age set to {hsts} s"
                      + (" (max-age=0: browsers forget it)" if hsts == 0 else ""))
        ctx.audit(request, "admin_tls_set", cert + ("" if hsts == old_hsts else f" (HSTS {hsts} s)"))
        applied, switched = _tls_applied(request, prepared)
        return await _tls_answer(request, "tls", switched, ok=_("Admin TLS saved.") + " " + applied)

    async def _admin_tls(admin):
        """The admin UI's TLS context for its settings (in an edit_config block): files
        that can't be used refuse the change, as they would stop the next start. Without
        a listener in this process the files are still loaded, to be checked."""
        prepared = await state.prepare_admin_tls(admin)
        if not prepared.error and prepared.later and admin.tls_enabled:
            try:
                await asyncio.to_thread(_load_tls_pair, admin.tls_cert, admin.tls_key,
                                        admin.tls_key_password)
            except tls.TlsFileError as e:
                prepared = tlsreload.Prepared(error=str(e), exc=e)
        if prepared.error:
            why = (tls_file_message(prepared.exc) if isinstance(prepared.exc, tls.TlsFileError)
                   else prepared.error)
            raise ConfigError(_("The TLS certificate and key could not be loaded: {0}").format(why))
        return prepared

    def _tls_applied(request, prepared) -> tuple[str, bool]:
        """Hand a saved admin TLS change to the UI's listener, and say so: new connections
        use it at once (the one this page came over stays as it is). Also whether they use
        another scheme now (HTTPS for HTTP, or back)."""
        was = state.serving_tls
        if not state.use_admin_tls(prepared):
            return _("Restart to apply."), False  # (no listener in this process)
        _ip, port, tls_on = state.admin_serving()
        host = request.url.hostname or "127.0.0.1"
        host = f"[{host}]" if ":" in host else host
        url = f"{'https' if tls_on else 'http'}://{host}:{port}{proxy.base(request)}/settings"
        if tls_on:
            text = _("New connections to this UI use HTTPS now: continue at {0}").format(url)
        else:
            text = _("New connections to this UI use plain HTTP now: continue at {0}").format(url)
        return text, tls_on != was

    async def _tls_answer(request, section: str, switched: bool, **flash) -> Response:
        """The answer to a form that changed the admin UI's TLS: the redirect of every Settings
        form (_done), unless new connections use another scheme now: the browser would follow
        the redirect with the old one, which the UI no longer serves, so the page itself is
        the answer, over the connection the form came on (its message says where to go on)."""
        if switched:
            return await settings_get(request, section=section, **flash)
        return _done(request, section, **flash)

    async def settings_tls_generate(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        tdir = os.path.join(state.data_dir, "tls")
        os.makedirs(tdir, exist_ok=True)
        cert, key = os.path.join(tdir, "cert.pem"), os.path.join(tdir, "key.pem")
        try:
            # the ones generated before (the configured certificate, it may be) stay as
            # *.bak: backups of config.json don't cover data/tls
            kept = await asyncio.to_thread(_keep_previous, (cert, key))
            await asyncio.to_thread(selfsigned.generate, cert, key)
        except (selfsigned.CertError, OSError) as e:
            return await _failed(request, "tls", _("Could not generate a certificate: {0}").format(
                explain(e, "file") or e))
        try:
            async with state.edit_config():
                admin = state.config.admin_ui
                admin.tls_cert = cert
                admin.tls_key = key
                admin.tls_key_password = ""  # (the key it writes is not encrypted)
                prepared = await _admin_tls(admin)
        except ConfigError as e:
            return await _failed(request, "tls", _("The certificate was generated, but not set "
                                                   "up: {0}").format(e))
        ctx.audit(request, "admin_tls_generate", "")
        ok = _("Self-signed certificate generated (browsers warn about a self-signed "
               "certificate until you accept it).")
        applied, switched = _tls_applied(request, prepared)
        ok += " " + applied
        if kept:
            ok += " " + _("The previous certificate and key are kept as {0}.").format(
                ", ".join(os.path.basename(p) for p in kept))
        # not refused: the clock may be right after all (a backup made while it was ahead)
        behind = await asyncio.to_thread(clock.behind, state.store)
        warning = _("The server’s clock reads {0} UTC, which can’t be right (Settings → "
                    "Admin interface says why), and the certificate is dated from it: clients "
                    "whose clock is right may take it for expired or not valid yet. Set the "
                    "clock, then generate the certificate again.").format(
                        clock.utc_text(time.time())) if behind else None
        return await _tls_answer(request, "tls", switched, ok=ok, error=warning)

    # ---------------- web-UI access (login requirement) ----------------
    async def settings_access_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        require_login = _checkbox(form, "require_login")
        role = form.get("anonymous_role", "viewer")
        language = form.get("default_language") or state.config.admin_ui.language
        if language not in UI_LANGUAGES:
            language = state.config.admin_ui.language
        submitted = {"access": {"require_login": require_login, "anonymous_role": role,
                                "language": language},
                     "session_minutes": form.get("session_minutes")}
        if role not in ROLES:
            return await _failed(request, "access", _("Invalid role for visitors without login."),
                                 **submitted)
        timeout = state.config.session_timeout_s
        if "session_minutes" in form:
            try:
                timeout = int(float(form.get("session_minutes") or "") * 60)
            except (ValueError, OverflowError):  # OverflowError: "inf"
                return await _failed(request, "access", _("Session lifetime must be a number "
                                                          "of minutes."), **submitted)
        ui = state.config.admin_ui
        old_timeout = state.config.session_timeout_s
        try:
            async with state.edit_config():
                ui.require_login, ui.anonymous_role = require_login, role
                state.config.session_timeout_s, ui.language = timeout, language
        except ConfigError as e:
            return await _failed(request, "access", str(e), **submitted)
        if timeout != old_timeout:
            state.log(f"web UI session lifetime set to {timeout // 60} min (new sign-ins)")
        # said on the page after the redirect, in its language: the default saved here may
        # be another one (for a visitor who hasn't picked one)
        lang = i18n.set_language(i18n.negotiate(request.cookies.get(i18n.COOKIE),
                                                request.headers.get("accept-language"), language))
        if require_login:
            state.log("web UI login requirement ENABLED (every visitor must sign in)")
            ok = _("Login is required again for the web UI.")
        else:
            state.log(f"WARNING: web UI login requirement DISABLED — visitors without a "
                      f"session get the '{role}' role (by IP / localhost / host name)")
            ok = _("Open access enabled: visitors without a login now get the '{0}' role. "
                   "Use this only on a trusted network.").format(role)
        i18n.reset(lang)
        ctx.audit(request, "web_access",
                    "login-required" if require_login else f"open:{role}")
        return _done(request, "access", ok=ok)

    # ---------------- the notice on the sign-in page ----------------
    async def settings_login_notice_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ui = state.config.admin_ui
        text = str(form.get("login_notice", ui.login_notice))
        url = str(form.get("login_notice_url", ui.login_notice_url)).strip()
        shown = {"login_notice": text, "login_notice_url": url, "require_login": ui.require_login}
        if problem := login_notice_problem(text) or login_notice_url_problem(url):
            return await _failed(request, "access", problem, login_notice=shown)
        text = clean_login_notice(text)
        old = (ui.login_notice, ui.login_notice_url)
        try:
            async with state.edit_config():
                old = (ui.login_notice, ui.login_notice_url)
                ui.login_notice, ui.login_notice_url = text, url
        except ConfigError as e:  # not saved: nothing changed
            return await _failed(request, "access", str(e), login_notice=shown)
        what = (f"{len(text)} characters" if text else "none") + f", link {url or 'none'}"
        if (text, url) != old:
            state.log(f"sign-in notice set: {what}")
        ctx.audit(request, "login_notice_set", what)
        return _done(request, "access", ok=_("Sign-in notice saved.") if text or url else
                     _("Sign-in notice removed."))

    # ---------------- this instance's name ----------------
    async def settings_node_name_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ui = state.config.admin_ui
        if "node_name" not in form:   # the environment sets it (the field is disabled)
            return proxy.redirect(request, "/settings#admin-interface")
        name = str(form.get("node_name") or "")
        if problem := node_name_problem(name):
            return await _failed(request, "admin-interface", problem, node={"node_name": name})
        name = name.strip()
        old = ui.node_name
        try:
            async with state.edit_config():
                old, ui.node_name = ui.node_name, name
        except ConfigError as e:  # not saved: nothing changed
            return await _failed(request, "admin-interface", str(e), node={"node_name": name})
        if name != old:
            state.log(f"instance name set to {name!r}" if name else
                      "instance name cleared: the host name is used")
        ctx.audit(request, "node_name_set", name or "(host name)")
        return _done(request, "admin-interface", ok=_("Instance name saved."))

    # ---------------- which web UI requests all.log records ----------------
    async def settings_request_log_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        mode = str(form.get("request_log") or "")
        if mode not in REQUEST_LOG_MODES:
            return await _failed(request, "audit-log", _("Choose which web UI requests all.log "
                                                         "records."))
        ui = state.config.admin_ui
        old = ui.request_log
        try:
            async with state.edit_config():
                old, ui.request_log = ui.request_log, mode
        except ConfigError as e:  # not saved: nothing changed
            return await _failed(request, "audit-log", str(e))
        if mode != old:  # logged before "off" would keep this request's own line out
            state.log(f"web UI requests in all.log: {mode} (was {old})")
        ctx.audit(request, "request_log_set", mode)
        return _done(request, "audit-log", ok=_("Saved: which web UI requests all.log records."))

    # ---------------- behind a reverse proxy ----------------
    async def settings_proxy_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ui = state.config.admin_ui
        # a field the form doesn't have (disabled: the environment sets it) keeps its value
        url, entries = ui.public_url, list(ui.trusted_proxies)
        hsts = _checkbox(form, "proxy_hsts")
        # refused: the form again with what was sent
        shown = {"public_url": str(form.get("public_url", url) or "").strip(),
                 "trusted_proxies": split_proxy_entries(str(form.get("trusted_proxies") or ""))
                 if "trusted_proxies" in form else entries, "proxy_hsts": hsts}
        if "public_url" in form:
            raw = str(form.get("public_url") or "").strip()
            if problem := public_url_problem(raw):
                return await _failed(request, "reverse-proxy", problem, proxy=shown)
            url = normalize_public_url(raw)
        if "trusted_proxies" in form:
            entries = list(dict.fromkeys(split_proxy_entries(str(form.get("trusted_proxies") or ""))))
            if problem := next(filter(None, map(proxy_entry_problem, entries)), None):
                return await _failed(request, "reverse-proxy", problem, proxy=shown)
        old = (ui.public_url, list(ui.trusted_proxies), ui.proxy_hsts)
        try:
            async with state.edit_config():
                old = (ui.public_url, list(ui.trusted_proxies), ui.proxy_hsts)
                ui.public_url, ui.trusted_proxies, ui.proxy_hsts = url, entries, hsts
        except ConfigError as e:  # not saved: nothing changed
            return await _failed(request, "reverse-proxy", str(e), proxy=shown)
        changes = [what for what, changed in (
            (f"public URL {url or '(none)'}", url != old[0]),
            (f"trusted proxies {' '.join(entries) or '(none)'}", entries != old[1]),
            (f"HSTS through the proxy {'on' if hsts else 'off'}", hsts != old[2])) if changed]
        if changes:
            state.log(f"reverse proxy settings: {'; '.join(changes)}")
        ctx.audit(request, "reverse_proxy_set", "; ".join(changes) or "unchanged")
        return _done(request, "reverse-proxy", ok=_("Reverse proxy settings saved."))

    async def settings_sessions_revoke_all(request):
        """A new session key: every web UI session ends at once, including sessions
        signed with the key in an old copy of config.json or of a backup."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        try:
            async with state.edit_config():
                state.config.secret_key = secrets.token_hex(32)
        except ConfigError as e:
            return await _failed(request, "access", str(e))
        state.revoked_sessions.clear()  # signed with the old key: dead anyway
        state.log("new session key: every web UI session was signed out")
        ctx.audit(request, "sessions_revoke_all", "")
        resp = _done(request, "access", ok=_("Everyone was signed out. You stay signed in "
                                             "here."))
        me = current_user(request)
        if me is not None and me.source != "anonymous":
            set_session(resp, request, me)
        return resp

    # ---------------- REST API token ----------------
    async def settings_api_token_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        role = form.get("api_token_role")  # no field: a form from before token roles
        role = "admin" if role is None else str(role).strip().lower()
        if role not in ROLES:
            return await _failed(request, "api-token", _("Invalid role."))
        token = auth.new_api_token()
        try:
            async with state.edit_config():
                state.config.api_token_hash = auth.hash_token(token)
                state.config.api_token_role = role
        except ConfigError as e:  # not saved: the token in use stays (this one is never shown)
            return await _failed(request, "api-token", str(e))
        state.api_token_used.pop("", None)  # the token it replaced was used, not this one
        state.log(f"REST API token generated (role: {role})")
        ctx.audit(request, "api_token_generate", role)
        # show the token exactly once — only its hash is stored (on the page the redirect
        # leads to, from the flash: never in a URL)
        return _done(request, "api-token", new_api_token=token,
                     ok=_("New API token generated (role: {0}). Copy it now — it "
                          "is not shown again.").format(role))

    async def settings_api_token_role(request):
        """Give the REST API token another role, keeping the token: what uses it goes on
        working (regenerating it to change the role broke every integration at once)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        role = str(form.get("api_token_role") or "").strip().lower()
        if role not in ROLES:
            return await _failed(request, "api-token", _("Invalid role."))
        before = state.config.api_token_role
        try:
            async with state.edit_config():
                before = state.config.api_token_role
                if not state.config.api_token_hash:
                    raise ConfigError(_("There is no REST API token to give a role: generate one."))
                state.config.api_token_role = role
        except ConfigError as e:  # not saved: nothing changed
            return await _failed(request, "api-token", str(e))
        state.log(f"REST API token role changed: {before} -> {role}")
        ctx.audit(request, "api_token_role", f"{before} -> {role}")
        return _done(request, "api-token", ok=_("The REST API token’s role is now {0}; the token "
                                                "stays the same.").format(role))

    async def settings_api_token_revoke(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        try:
            async with state.edit_config():
                state.config.api_token_hash = ""
        except ConfigError as e:  # not saved: the token still works
            return await _failed(request, "api-token", str(e))
        state.api_token_used.pop("", None)
        state.log("REST API token revoked")
        ctx.audit(request, "api_token_revoke", "")
        if state.config.api_tokens:
            return _done(request, "api-token", ok=_("API token revoked. The other tokens keep "
                                                    "working."))
        return _done(request, "api-token", ok=_("API token revoked. The REST API is now disabled."))

    # ---------------- named REST API tokens ----------------
    def _used(token_id: str) -> str:
        when = state.api_token_used.get(token_id)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "—"

    def _api_token_rows() -> list[dict]:
        def day(when: float) -> str:
            return time.strftime("%Y-%m-%d", time.localtime(when)) if when else ""

        return [{"id": t.id, "name": t.name, "role": t.role, "created": day(t.created) or "—",
                 "expires": day(t.expires), "expired": t.expired(), "last_used": _used(t.id)}
                for t in state.config.api_tokens]

    async def settings_api_tokens_create(request):
        """Another REST API token, with its own name, role and optional expiry; the token
        of api_token_hash stays as it is."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        name = str(form.get("name") or "").strip()
        role = str(form.get("role") or "viewer").strip().lower()
        raw_days = str(form.get("expires_days") or "").strip()
        try:
            days = int(raw_days) if raw_days else 0
        except ValueError:
            days = -1
        # refused: the form again with what was sent
        shown = {"name": name, "role": role, "expires_days": raw_days}
        if not API_TOKEN_NAME.fullmatch(name):
            return await _failed(request, "api-token", _(
                "A token name is 1-64 characters: English letters, digits, '.', '_' or '-'."),
                new_token=shown)
        if role not in API_TOKEN_ROLES:
            return await _failed(request, "api-token", _("Invalid role."), new_token=shown)
        if not 0 <= days <= API_TOKEN_DAYS_MAX:
            return await _failed(request, "api-token", _(
                "Enter 0 (never) or a number of days from 1 to {0}.").format(API_TOKEN_DAYS_MAX),
                new_token=shown)
        token = auth.new_api_token()
        now = time.time()
        try:
            async with state.edit_config():
                tokens = state.config.api_tokens
                if any(t.name.casefold() == name.casefold() for t in tokens):
                    raise ConfigError(_("There is a token named '{0}' already.").format(name))
                if len(tokens) >= API_TOKENS_MAX:
                    raise ConfigError(_("There are {0} tokens already, the most there can be: "
                                        "revoke one first.").format(API_TOKENS_MAX))
                tokens.append(ApiToken(id=new_api_token_id(), name=name,
                                       sha256=auth.hash_token(token), role=role, created=now,
                                       expires=now + days * 86400 if days else 0.0))
        except ConfigError as e:  # not saved: no token (this one is never shown)
            return await _failed(request, "api-token", str(e), new_token=shown)
        state.log(f"REST API token {name!r} created (role: {role}"
                  + (f", expires in {days} days)" if days else ")"))
        ctx.audit(request, "api_token_create", f"{name}:{role}" + (f":{days}d" if days else ""))
        # shown once, on the page the redirect leads to (from the flash: never in a URL)
        return _done(request, "api-token", new_api_token=token, ok=_(
            "New API token '{0}' (role: {1}). Copy it now — it is not shown again.").format(
                name, role))

    async def settings_api_tokens_revoke(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        token_id = str(form.get("id") or "")
        try:
            async with state.edit_config():
                tokens = state.config.api_tokens
                gone = next((t for t in tokens if t.id == token_id), None)
                if gone is None:
                    raise ConfigError(_("No such token (it may have been revoked already)."))
                tokens.remove(gone)
        except ConfigError as e:  # not saved: the token still works
            return await _failed(request, "api-token", str(e))
        state.api_token_used.pop(gone.id, None)
        state.log(f"REST API token {gone.name!r} revoked")
        ctx.audit(request, "api_token_revoke", gone.name)
        return _done(request, "api-token", ok=_("API token '{0}' revoked.").format(gone.name))

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
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        old, new = state.config.ldap, None
        try:
            async with state.edit_config():
                old = state.config.ldap
                before = state.config.usable_admin_count()
                state.config.ldap = new = _ldap_from_form(form, old)
                if problem := (_directory_off_problem(before, "LDAP")
                               or _ca_file_problem(state.config.ldap)):
                    raise ConfigError(problem)
        except ConfigError as e:
            # refused: the form again with what was sent, but the stored bind password
            shown = dataclasses.replace(new, bind_password=old.bind_password) if new else None
            return await _failed(request, "ldap",
                                 str(e) + _typed_again(form, "ldap_bind_password"), ldap=shown)
        _clear_group_warnings("ldap")
        state.ldap_tls_problem = None   # answered; the next failing sign-in shows it again
        state.log(f"LDAP settings updated (enabled={state.config.ldap.enabled})")
        if old.tls_verify != state.config.ldap.tls_verify:
            state.log("WARNING: LDAP: the directory server's certificate is no longer checked "
                      "(insecure)" if old.tls_verify else
                      "LDAP: the directory server's certificate is checked again")
        ctx.audit(request, "ldap_settings",
                    "enabled" if state.config.ldap.enabled else "disabled")
        return _done(request, "ldap", ok=_("LDAP settings saved.")
                     + _directory_off_note(old.enabled, state.config.ldap.enabled, "ldap", "LDAP"))

    async def settings_oidc_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        old, new = state.config.oidc, None
        try:
            async with state.edit_config():
                old = state.config.oidc
                before = state.config.usable_admin_count()
                state.config.oidc = new = _oidc_from_form(form, old)
                if problem := _directory_off_problem(before, "OIDC"):
                    raise ConfigError(problem)
        except ConfigError as e:
            # refused: the form again with what was sent, but the stored client secret
            shown = dataclasses.replace(new, client_secret=old.client_secret) if new else None
            return await _failed(request, "oidc",
                                 str(e) + _typed_again(form, "oidc_client_secret"), oidc=shown)
        _clear_group_warnings("oidc")
        state.log(f"OIDC settings updated (enabled={state.config.oidc.enabled})")
        ctx.audit(request, "oidc_settings",
                    "enabled" if state.config.oidc.enabled else "disabled")
        return _done(request, "oidc", ok=_("OIDC (SSO) settings saved.")
                     + _directory_off_note(old.enabled, state.config.oidc.enabled, "oidc", "OIDC"))

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
        form = await read_form(request)
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
        shown = {"username": username, "role": role}   # the passwords are typed again
        if err:
            return await _failed(request, "users", err, new_user=shown)
        pw_hash = await state.password_work.run(auth.hash_password, pw)
        try:
            async with state.edit_config():
                if err := taken():  # created while the password was hashed
                    raise ConfigError(err)
                state.config.users.append(User(username=username, password_hash=pw_hash,
                                               role=role, pwd_version=new_pwd_version()))
        except ConfigError as e:
            return await _failed(request, "users", str(e), new_user=shown)
        state.log(f"user created: {username} ({role})")
        ctx.audit(request, "user_create", f"{username}:{role}")
        return _done(request, "users", ok=_("User '{0}' created.").format(username))

    async def settings_users_role(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = _target_name(request, form)
        role = form.get("role", "viewer")
        try:
            async with state.edit_config():  # checked under the lock: two admins at once
                target = state.config.get_user(username)
                if target is None:
                    raise ConfigError(_("User not found."))
                if role not in ROLES:
                    raise ConfigError(_("Invalid role."))
                if target.role == "admin" and role != "admin" and _last_admin(target):
                    raise ConfigError(_("Cannot demote the last admin."))
                target.role = role
                target.pwd_version += 1  # force re-login so the new role applies everywhere
        except ConfigError as e:
            return await _failed(request, "users", str(e))
        state.log(f"user role changed: {username} -> {role}")
        ctx.audit(request, "user_role", f"{username}:{role}")
        return _done(request, "users", ok=_("Role updated for '{0}'. They must sign in again.").format(username))

    async def settings_users_delete(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = _target_name(request, form)
        try:
            async with state.edit_config():  # checked under the lock: two admins at once
                target = state.config.get_user(username)
                if target is None:
                    raise ConfigError(_("User not found."))
                if _is_me(request, username):
                    raise ConfigError(_("You cannot delete your own account."))
                if target.role == "admin" and _last_admin(target):
                    raise ConfigError(_("Cannot delete the last admin."))
                others = [u for u in _spellings(target) if not _is_me(request, u.username)]
                if others and _leaves_no_admin([target, *others]):
                    raise ConfigError(_("Cannot delete the last admin."))
                gone = [target, *others]
                state.config.users = [u for u in state.config.users
                                      if not any(u is g for g in gone)]
        except ConfigError as e:
            return await _failed(request, "users", str(e))
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
        names = {u.username for u in gone}
        kept = sum(1 for b in await asyncio.to_thread(state.store.backups)
                   if any(a["username"] in names for a in b["accounts"]))
        if kept:
            ok += " " + ngettext(
                "{0} configuration backup still holds it (made before the deletion; a restore "
                "with accounts would bring it back): Configuration backups below can remove it "
                "from that backup.",
                "{0} configuration backups still hold it (made before the deletion; a restore "
                "with accounts would bring it back): Configuration backups below can remove it "
                "from them.", kept).format(kept)
        return _done(request, "users", ok=ok)

    async def settings_users_expiry(request):
        """How many days without a sign-in an LDAP/OIDC account is kept (0: for ever)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        try:
            days = int(str(form.get("expire_days", "")).strip())
        except ValueError:
            days = -1
        shown = str(form.get("expire_days", ""))
        if not (days == 0 or EXPIRE_DAYS_MIN <= days <= EXPIRE_DAYS_MAX):
            return await _failed(request, "users", _(
                "Enter 0 (never) or a number of days from {0} to {1}.").format(
                    EXPIRE_DAYS_MIN, EXPIRE_DAYS_MAX), expire_days=shown)
        try:
            async with state.edit_config():
                state.config.external_users_expire_days = days
        except ConfigError as e:
            return await _failed(request, "users", str(e), expire_days=shown)
        state.log(f"LDAP/OIDC accounts are removed after {days} days without a sign-in" if days
                  else "LDAP/OIDC accounts are no longer removed for not signing in")
        ctx.audit(request, "users_expiry", str(days))
        gone = await state.expire_external_accounts()
        ok = (_("Saved: LDAP / OIDC accounts that haven’t signed in for {0} days are removed "
                "(checked every hour).").format(days) if days else
              _("Saved: LDAP / OIDC accounts are never removed for not signing in."))
        if gone:
            ok += " " + _("Removed now: {0}.").format(", ".join(u.username for u in gone))
        return _done(request, "users", ok=ok)

    async def settings_retention(request):
        """How many days a trace file is kept after it was last written (0: until it is
        deleted in its mapping's Trace panel, the default), and how many days log and audit
        records and configuration backups are kept (0: only their size and count limits,
        the default). A setting the form doesn't send (a page of an older version) stays."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        wanted: dict[str, int] = {}
        # refused: the form again with what was sent (a field not sent: as it is)
        shown = {key: form.get(key, getattr(state.config, key))
                 for key in ("trace_max_age_days", "log_max_age_days")}
        for key, top, problem in (
                ("trace_max_age_days", TRACE_DAYS_MAX,
                 _("Enter 0 (keep them) or a number of days from 1 to {0}.")),
                ("log_max_age_days", LOG_DAYS_MAX,
                 _("Enter 0 (only the size limits) or a number of days from 1 to {0}."))):
            if key not in form:
                continue
            try:
                days = int(str(form.get(key, "")).strip())
            except ValueError:
                days = -1
            if not 0 <= days <= top:
                return await _failed(request, "retention", problem.format(top), retention=shown)
            wanted[key] = days
        before = {key: getattr(state.config, key) for key in wanted}
        try:
            async with state.edit_config():
                for key, days in wanted.items():
                    setattr(state.config, key, days)
        except ConfigError as e:
            return await _failed(request, "retention", str(e), retention=shown)
        parts = []
        if "trace_max_age_days" in wanted:
            days = wanted["trace_max_age_days"]
            if days != before["trace_max_age_days"]:
                state.log(f"trace files not written for {days} days are deleted every hour"
                          if days else "trace files are kept until they are deleted by hand")
                ctx.audit(request, "trace_retention", str(days))
            gone = await state.run_trace_maintenance()
            parts.append(_("Saved: trace files not written for {0} days are deleted (checked "
                           "every hour).").format(days) if days else
                         _("Saved: trace files are kept until they are deleted in a mapping’s "
                           "Trace panel."))
            if gone:
                parts.append(ngettext("Deleted now: {0} trace file.", "Deleted now: {0} trace files.",
                                      gone).format(gone))
        if "log_max_age_days" in wanted:
            days = wanted["log_max_age_days"]
            if days != before["log_max_age_days"]:
                state.log(f"log and audit records and configuration backups older than {days} "
                          "days are deleted every hour" if days else
                          "log and audit records and configuration backups are kept within "
                          "their size and count limits")
                ctx.audit(request, "log_retention", str(days))
            gone = await state.run_retention()
            parts.append(_("Log and audit records and configuration backups older than {0} "
                           "days are deleted (checked every hour; the newest {1} backups stay)."
                           ).format(days, BACKUPS_MIN_KEPT) if days else
                         _("Logs, the audit log and the configuration backups are kept within "
                           "their size and count limits."))
            if gone:
                parts.append(ngettext("Deleted now: {0} log file or backup.",
                                      "Deleted now: {0} log files and backups.", gone).format(gone))
        return _done(request, "retention", ok=" ".join(parts) or _("Saved."))

    async def settings_users_signout(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = _target_name(request, form)
        try:
            async with state.edit_config():
                target = _bump_sessions(username)
        except ConfigError as e:
            return await _failed(request, "users", str(e))
        state.log(f"all sessions of {username} signed out")
        ctx.audit(request, "user_signout", username)
        resp = _done(request, "users", ok=_("'{0}' was signed out everywhere.").format(username))
        if _is_me(request, username):
            set_session(resp, request, target)  # your own row: you stay signed in here
        return resp

    async def settings_users_password(request):
        """An admin sets another local account's password (it forgot it, or it must
        be taken away from someone): the account is signed out everywhere."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
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

        # refused: the same account stays chosen (not the list's first one)
        if err := problem() or _password_problem(pw, pw2):
            return await _failed(request, "users", err, reset_user=username)
        pw_hash = await state.password_work.run(auth.hash_password, pw)
        try:
            async with state.edit_config():
                if err := problem():  # changed while the password was hashed
                    raise ConfigError(err)
                target = state.config.get_user(username)
                if target is None:  # (problem() has just found it)
                    raise ConfigError(_("User not found."))
                target.password_hash, target.pwd_version = pw_hash, target.pwd_version + 1
        except ConfigError as e:
            return await _failed(request, "users", str(e), reset_user=username)
        state.log(f"password of {username} set by an admin; their sessions were signed out")
        ctx.audit(request, "user_password_set", username)
        return _done(request, "users", ok=_("New password set for '{0}'; they were signed out "
                                            "everywhere.").format(username))

    async def settings_users_totp_reset(request):
        """An admin turns off another local account's two-factor sign-in (its authenticator
        was lost): it is signed out everywhere, and its password alone signs it in until
        it sets two-factor sign-in up again."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = str(form.get("username") or "")
        try:
            async with state.edit_config():  # checked under the lock: two admins at once
                target = state.config.get_user(username)
                if target is None:
                    raise ConfigError(_("User not found."))
                if _is_me(request, username):
                    raise ConfigError(_("To turn off your own two-factor sign-in, use "
                                        "“Two-factor sign-in” at the top (it asks for a code)."))
                if not target.totp_on:
                    raise ConfigError(_("'{0}' doesn’t use two-factor sign-in.").format(username))
                target.totp_secret, target.pwd_version = "", target.pwd_version + 1
        except ConfigError as e:  # not saved: nothing changed
            return await _failed(request, "users", str(e))
        state.log(f"two-factor sign-in of {username} turned off by an admin; their sessions "
                  "were signed out")
        ctx.audit(request, "user_totp_reset", username)
        return _done(request, "users", ok=_("Two-factor sign-in is off for '{0}'; they were "
                                            "signed out everywhere.").format(username))

    # ---------------- config export / import ----------------
    async def config_export(request):
        # mappings only — never export password_hash / secret_key. Mappings can carry
        # secrets (the MQTT password and client key password, the TLS key password), so —
        # like import — this needs operator+ (the UI only offers it to operators; a viewer,
        # possibly an anonymous one, must not read them).
        if deny := require_role(request, "operator"):
            return deny
        mappings = state.config.mappings
        # non-ASCII text as it is (mapping and point names, topics), like config.json
        data = {"version": 1, "mappings": [m.to_dict() for m in mappings]}
        secrets_in = sum(1 for m in mappings if m.mqtt.password or m.mqtt.client_key_password
                         or m.network.tls_key_password)
        ctx.audit(request, "config_export", f"{len(mappings)} mappings"
                  + (f", {secrets_in} with a stored password (MQTT, MQTT or TLS key)"
                     if secrets_in else ""))
        return Response(
            json_text(data, indent=2), media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=ser2net-mappings.json"})

    # ---- import: the form first shows a preview (step=preview), then applies it in
    #      "merge" or "replace" mode. A POST without step (scripts written for 2.7)
    #      keeps the old behaviour: replace everything at once, answered with the page;
    #      a classic ser2net file (which 2.7 did not take) only goes through the preview.
    PENDING_TTL_S = 900   # (time.monotonic(): a clock step neither ends nor extends it)

    def _pending_put(user: str, plan, mode: str) -> str:
        now, pend = time.monotonic(), state.pending_imports
        for k in [k for k, v in pend.items() if now - v["created"] > PENDING_TTL_S]:
            del pend[k]
        while len(pend) >= 20:
            pend.pop(next(iter(pend)))
        token = secrets.token_urlsafe(16)
        pend[token] = {"created": now, "user": user, "plan": plan, "mode": mode}
        return token

    def _pending_get(token: str, user: str):
        item = state.pending_imports.get(token or "")
        if not item or item["user"] != user or time.monotonic() - item["created"] > PENDING_TTL_S:
            return None
        return item

    def _who(request) -> str:
        u = current_user(request)
        return u.username if u is not None else ""

    async def config_import(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form(max_files=1)  # the mappings file
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        upload = form.get("file")
        legacy = form.get("step") != "preview"
        try:
            raw = await upload.read() if hasattr(upload, "read") else str(upload or "").encode()
            # parsing is bounded, but a big file still takes a moment: not on the event loop
            plan = await asyncio.to_thread(importer.parse_for_preview, raw,
                                           getattr(upload, "filename", "") or "")
        except ConfigError as e:
            error = _("Import failed: {0}").format(e)
            if legacy:   # answered as before
                return await settings_get(request, section="import", error=error)
            return await _failed(request, "import", error)
        mode = form.get("mode", "merge")
        if not legacy:
            if mode not in importer.MODES:
                return await _failed(request, "import", _("Unknown import mode."))
            return await _import_preview(request, plan, mode)
        if plan.classic:  # what it changes and leaves out is only seen in the preview
            return await settings_get(request, section="import", status_code=400, error=_(
                "A classic ser2net file is imported through the preview: choose it in "
                "Settings → Mappings export / import (or post it with step=preview), check "
                "the preview, then apply it."))
        # legacy one-step import: replace all mappings
        return await _apply_import(request, plan, "replace", legacy=True)

    async def _import_preview(request, plan, mode):
        """Keep `plan` for this user, and lead to the page that shows what importing it
        would change (?import=<id>: a reload or the TR / EN switch shows it again; the id
        is good only for this user, for a while). Nothing changes until it is applied."""
        token = _pending_put(_who(request), plan, mode)
        return proxy.redirect(request, f"/settings?import={token}#import")

    def _preview_of(token: str, item) -> dict:
        """What importing a pending plan would change, against the mappings as they are now
        (ops.import_preview, shared with the REST API's dry run)."""
        plan, mode = item["plan"], item["mode"]
        rows, problem = ops.import_preview(state, plan, mode)
        general, _notes = plan.split_warnings()   # a mapping's own warnings are in its row
        return {"token": token, "mode": mode, "rows": rows, "source": plan.source,
                "warnings": general, "problem": problem, "count": len(plan.mappings)}

    async def config_import_apply(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        token = form.get("token", "")
        item = _pending_get(token, _who(request))
        if item is None:
            return await _failed(request, "import", _("This import preview has expired. "
                                                      "Upload the file again."))
        mode = form.get("mode", "merge")
        if mode not in importer.MODES:
            return await _failed(request, "import", _("Unknown import mode."))
        state.pending_imports.pop(token, None)
        return await _apply_import(request, item["plan"], mode)

    async def _apply_import(request, plan, mode, legacy=False):
        """`legacy`: the one-step import of 2.7 scripts, answered with the page as before."""
        try:
            rows, new_maps, backed_up = await ops.import_mappings(state, plan, mode)
        except ConfigError as e:
            error = _("Import rejected: {0}").format(e)
            if legacy:
                return await settings_get(request, section="import", error=error)
            return await _failed(request, "import", error)
        count = ops.import_counts(rows)
        # in English (parse_for_preview): what the import left out or changed stays on record
        warnings = importer.log_lines(plan)
        state.log(f"mappings imported ({mode}) from {plan.log_source or plan.source}: "
                  f"{count['add']} added, {count['update']} updated, {count['same']} unchanged, "
                  f"{count['remove']} removed, {len(warnings)} warnings")
        for w in warnings:
            state.log(f"import warning: {w}")
        ctx.audit(request, f"config_import_{mode}", importer.audit_detail(plan))
        if mode == "replace":
            ok = ngettext("Imported {0} mapping (replaced the existing ones).",
                          "Imported {0} mappings (replaced the existing ones).",
                          len(new_maps)).format(len(new_maps))
        else:
            ok = _("Imported: {0} added, {1} updated, {2} unchanged.").format(
                count["add"], count["update"], count["same"])
        ok += " " + (_("The previous configuration was kept as a backup.") if backed_up else
                     _("The previous configuration could not be kept as a backup (see all.log)."))
        if legacy:
            return await settings_get(request, section="import", ok=ok)
        return _done(request, "import", ok=ok)

    # ---------------- event notifications (admin) ----------------
    def _notify_from_form(form, old: NotificationSettings) -> NotificationSettings:
        try:
            port = int(form.get("notify_smtp_port") or 587)
            repeat = int(float(form.get("notify_repeat_min") or 5) * 60)
        except (ValueError, OverflowError):  # OverflowError: "inf"
            raise ConfigError(_("SMTP port and repeat interval must be numbers.")) from None
        url, pw = form.get("notify_webhook_url", "").strip(), form.get("notify_smtp_password", "")
        # the personal-data boxes: a form without them (a page opened before the upgrade)
        # keeps what is stored
        privacy = "notify_privacy_fields" in form
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
            "mask_personal_data": (_checkbox(form, "notify_mask_personal_data") if privacy
                                   else old.mask_personal_data),
            "email_separately": (_checkbox(form, "notify_email_separately") if privacy
                                 else old.email_separately),
        })

    async def settings_notifications_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        old = sent = state.config.notifications
        try:
            async with state.edit_config():
                old = state.config.notifications
                state.config.notifications = sent = _notify_from_form(form, old)
        except ConfigError as e:
            # refused: the form again with what was sent (if it could be read), but the
            # stored webhook URL and SMTP password
            shown = (None if sent is old else dataclasses.replace(
                sent, webhook_url=old.webhook_url, smtp_password=old.smtp_password))
            return await _failed(request, "notifications", str(e) + _typed_again(
                form, "notify_webhook_url", "notify_smtp_password"), notify=shown)
        new = state.config.notifications
        state.log(f"notification settings saved (enabled={new.enabled}, events={','.join(new.events)}, "
                  f"personal data {'masked' if new.mask_personal_data else 'sent'}, e-mail "
                  f"{'to each recipient' if new.email_separately else 'to all recipients at once'})")
        ctx.audit(request, "notification_settings", "; ".join(
            ["enabled" if new.enabled else "disabled"]
            + (["personal data masked"] if new.mask_personal_data else [])
            + (["e-mail to each recipient"] if new.email_separately else [])))
        if form.get("then") == "test":
            # (the test message in the notification language: deliver() writes it)
            warnings: list[str] = []
            errors = await state.notifier.deliver("test", "", "", warnings=warnings)
            for problem in [str(f) for f in errors] + warnings:
                state.log(f"notifications: {problem} (test)")
            if errors:   # saved: a reload must not send the test again either
                return _done(request, "notifications", error=_("Saved, but the test failed: {0}")
                             .format("; ".join([f.shown() for f in errors] + warnings)))
            if warnings:
                return _done(request, "notifications", error=_(
                    "Saved; the test notification was sent, but: {0}").format("; ".join(warnings)))
            return _done(request, "notifications", ok=_("Saved; the test notification was sent."))
        return _done(request, "notifications", ok=_("Notification settings saved."))

    # ---------------- audit log viewer (admin) ----------------
    AUDIT_PAGE = 100

    async def _audit_records(request):
        params = request.query_params
        q = (params.get("q") or "").strip()
        action = (params.get("action") or "").strip()
        user = (params.get("user") or "").strip()
        records, actions, users = await asyncio.to_thread(_audit_matching, q, action, user)
        return records, {"q": q, "action": action, "user": user}, actions, users

    def _audit_matching(q: str, action: str, user: str):
        """(the audit records that match, every action, every user), in a worker thread:
        the search folds the text of each record (search_key: 'istanbul' finds
        'İstanbul', 'dairesi' 'DAİRESİ'). The fields are joined with NUL, which the fold
        keeps, so a search doesn't match across two of them."""
        records = state.read_audit()
        actions = sorted({r["action"] for r in records})
        users = sorted({r["user"] for r in records if r["user"]})
        if action:
            records = [r for r in records if r["action"] == action]
        if user:
            records = [r for r in records if r["user"] == user]
        needle = search_key(q.replace("\0", ""))
        if needle:
            records = [r for r in records if needle in search_key("\0".join(r.values()))]
        return records, actions, users

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
                      log_days=state.config.log_max_age_days,
                      total=len(records), page=page, pages=pages, filters=filters,
                      actions=actions, users=users, me=current_user(request),
                      qs=urlencode({k: v for k, v in filters.items() if v}))

    async def audit_csv(request):
        if deny := require_role(request, "admin"):
            return deny
        records, filters, *_ = await _audit_records(request)
        shown = ", ".join(f"{k}={v}" for k, v in filters.items() if v)
        ctx.audit(request, "audit_export", f"{len(records)} records"
                  + (f" ({shown})" if shown else ""))
        # ?excel=1: UTF-16 with a BOM, tab-separated — Excel opens that by double-click
        # with every letter and column whatever the regional settings. The default file
        # (UTF-8 without a BOM, commas) is what scripts read; Excel reads it in the ANSI
        # code page, split at the regional list separator (Turkish Windows: ';'), so all
        # in column A with garbled letters. Audit fields hold no tabs (state.audit).
        excel = request.query_params.get("excel") == "1"
        buf = io.StringIO()
        out = csv.writer(buf, delimiter="\t" if excel else ",")
        out.writerow(["time", "ip", "user", "action", "detail"])
        for r in records:
            out.writerow([_csv_cell(r[k]) for k in ("time", "ip", "user", "action", "detail")])
        if excel:
            return Response(("\ufeff" + buf.getvalue()).encode("utf-16-le"),
                            media_type="text/csv; charset=utf-16",
                            headers={"Content-Disposition":
                                     "attachment; filename=ser2net-audit-excel.csv"})
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
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        name = form.get("name", "")
        with_accounts = _checkbox(form, "accounts")
        try:
            restored = AppConfig.from_dict(json.loads(await asyncio.to_thread(state.store.read_backup, name)))
        except (ConfigError, OSError, ValueError, TypeError, AttributeError) as e:
            return await _failed(request, "backups", _("Could not read the backup: {0}").format(_unreadable(e)))
        if with_accounts and not restored.users:
            return await _failed(request, "backups", _("This backup has no user accounts (it "
                                                       "was made before the first-run setup); "
                                                       "restoring it would open the setup page "
                                                       "to anyone. Not restored."))
        if with_accounts and not restored.admin_count():
            return await _failed(request, "backups", _("This backup has no admin account, so its "
                                                       "accounts can't be restored. Not restored."))
        # never from the backup: the session key (renewed below with the accounts), this
        # node's cluster id, what start-up accepted while broken, so a restore is refused
        # only for what it changes or adds (AppConfig.validate_for_save), and what start-up
        # found in config.json (Settings says so); and unless asked, the accounts and
        # credentials
        keep = {"secret_key", "instance_id", "load_notes", "ids_created", "tolerated",
                "unknown_keys", "newer_writer"}
        if not with_accounts:
            keep |= {"users", "api_token_hash", "api_token_role", "api_tokens"}
        notes: list[str] = []
        moved: list[str] = []
        cur = state.config
        prepared = None
        try:
            async with state.edit_config() as edit:
                previous = {f.name: getattr(cur, f.name) for f in dataclasses.fields(AppConfig)}
                applied_from = reload.capture(cur)
                usable_before = cur.usable_admin_count()
                for f in dataclasses.fields(AppConfig):
                    if f.name not in keep:
                        setattr(cur, f.name, getattr(restored, f.name))
                # a mapping the backup has as it is keeps its object (its runner's)
                kept = {m.id: m for m in previous["mappings"]}
                cur.mappings = [kept[m.id] if m.id in kept and kept[m.id].to_dict() == m.to_dict()
                                else m for m in cur.mappings]
                reload.keep_env_address(cur.admin_ui)
                # a backup made before the data directory moved (or on another machine) names
                # TLS files of the old one: those of this data directory's tls folder are used
                moved = cur.find_moved_tls_files(state.data_dir)
                if moved:
                    notes.append(_("TLS files the backup names in another data directory are "
                                   "used from this one's tls folder."))
                was_admin = previous["admin_ui"]
                if (cur.admin_ui.tls_cert, cur.admin_ui.tls_key, cur.admin_ui.tls_key_password) != (
                        was_admin.tls_cert, was_admin.tls_key, was_admin.tls_key_password):
                    prepared = await _admin_tls(cur.admin_ui)  # (as Settings -> Admin TLS)
                if with_accounts:
                    cur.secret_key = secrets.token_hex(32)
                else:  # both cluster keys are credentials: the key and the one still accepted
                    was = previous["cluster"]
                    cur.cluster = dataclasses.replace(cur.cluster, key=was.key,
                                                      previous_key=was.previous_key,
                                                      previous_until=was.previous_until)
                    if cur.cluster.enabled and not cur.cluster.key.strip():
                        cur.cluster.enabled = False
                        notes.append(_("The cluster stays off: this backup had it on, but its "
                                       "key was not restored."))
                if usable_before and not cur.usable_admin_count():
                    raise ConfigError(_("no admin could sign in afterwards (LDAP/OIDC accounts "
                                        "need their sign-in method on)"))
        except ConfigError as e:  # nothing restored
            return await _failed(request, "backups", _("Restore rejected: {0}").format(e))
        if with_accounts:
            state.revoked_sessions.clear()  # signed with the old key: dead anyway
        # as a reload applies config.json: an unchanged mapping keeps running with its
        # clients, a changed one restarts, a removed one goes, the cluster restarts only
        # when its settings changed
        await reload.apply_changes(state, applied_from)
        if state.admin_guard is not None:
            state.admin_guard.use_limits(cur.admin_ui)
        if (state.store.replaced_edit or {}).get("backup") == name:
            state.store.replaced_edit = None  # (that edit is the one in use now)
        before = types.SimpleNamespace(**previous)
        changes = _restore_changes(before, cur, with_accounts)
        tok = i18n.set_language("en")  # the log and the audit log stay English
        try:
            audit_changes = _restore_changes(before, cur, with_accounts)
        finally:
            i18n.reset(tok)
        state.log(f"configuration restored from backup {name} "
                  f"({'with' if with_accounts else 'without'} its accounts)")
        for note in restored.load_notes + moved:  # what loading the backup changed, as at start-up
            state.log(note)
        ctx.audit(request, "config_backup_restore",
                  "; ".join([name, "accounts restored" if with_accounts else "accounts kept",
                             *audit_changes]))
        ok = [(_("Restored {0}; the configuration it replaced was backed up too.")
               if edit.backed_up else
               _("Restored {0}, but the configuration it replaced could not be backed up (see "
                 "all.log).")).format(name),
              _("Mappings it leaves as they are kept running.")]
        switched = False
        if prepared is not None:
            applied, switched = _tls_applied(request, prepared)
            ok.append(applied)
        ip, port, _tls = state.admin_serving()
        if (cur.admin_ui.bind_ip, cur.admin_ui.port) != (ip, port):
            ok.append(_("Its admin UI address {0} takes effect at the next start.").format(
                f"{cur.admin_ui.bind_ip}:{cur.admin_ui.port}"))
        ok.append(_("Everyone else was signed out.") if with_accounts else
                  _("User accounts, the REST API token and the cluster key were kept."))
        if changes:
            ok.append(_("Access-related changes: {0}.").format("; ".join(changes)))
        resp = await _tls_answer(request, "backups", switched, ok=" ".join(ok + notes))
        me = current_user(request)
        mine = (cur.get_user(me.username)
                if with_accounts and me is not None and me.source != "anonymous" else None)
        if mine is not None:
            set_session(resp, request, mine)
        return resp

    async def backup_delete(request):
        """Delete one backup (`name`), or every one (`all`)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        name = str(form.get("name") or "")
        async with state.config_lock:   # no save makes or prunes a backup meanwhile
            try:
                if _checkbox(form, "all"):
                    gone = await asyncio.to_thread(state.store.delete_backups)
                else:
                    await asyncio.to_thread(state.store.delete_backup, name)
            except ConfigError as e:
                return await _failed(request, "backups", str(e))
            except OSError as e:
                return await _failed(request, "backups", _("Could not delete: {0}").format(e))
        if _checkbox(form, "all"):
            state.log(f"all configuration backups deleted ({gone})")
            ctx.audit(request, "config_backups_delete_all", f"{gone} backups")
            return _done(request, "backups", ok=ngettext(
                "{0} configuration backup deleted.", "{0} configuration backups deleted.",
                gone).format(gone))
        state.log(f"configuration backup {name} deleted")
        ctx.audit(request, "config_backup_delete", name)
        return _done(request, "backups", ok=_("Backup of {0} UTC deleted.").format(backup_time(name)))

    async def backup_remove_account(request):
        """Take an account that no longer exists out of every backup that still has it."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        username = str(form.get("username") or "")
        async with state.config_lock:
            if any(u.username == username for u in state.config.users):
                return await _failed(request, "backups", _(
                    "'{0}' is an account now: delete it under Users first.").format(username))
            try:
                changed = await asyncio.to_thread(state.store.remove_account, username)
            except OSError as e:
                return await _failed(request, "backups", _("Could not change the backups: "
                                                           "{0}").format(e))
        if not changed:
            return await _failed(request, "backups", _("No backup holds an account named "
                                                       "'{0}'.").format(username))
        state.log(f"account {username!r} removed from {changed} configuration backups")
        ctx.audit(request, "config_backup_account_remove", f"{username}: {changed} backups")
        return _done(request, "backups", ok=ngettext(
            "'{0}' was removed from {1} configuration backup.",
            "'{0}' was removed from {1} configuration backups.", changed).format(username, changed))

    async def config_reload(request):
        """Read config.json again and apply it, as SIGHUP / `ser2net --reload` do
        (app/reload.py): for an edit made outside the UI (by hand, Ansible ...)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ok, message = await reload.reload_config(state, "Settings", client_ip(request),
                                                 ctx.actor(request))
        if ok:
            return _done(request, "backups", ok=_("config.json was read again and applied: "
                                                  "{0}").format(message))
        return await _failed(request, "backups", _("config.json was not reloaded, the "
                                                   "configuration in use stays: {0}").format(
            message))

    async def backup_restore_mappings(request):
        """Restore only a backup's mappings, through the import preview (replace)."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        name = form.get("name", "")
        try:
            raw = await asyncio.to_thread(state.store.read_backup, name)
            plan = await asyncio.to_thread(importer.parse_for_preview, raw.encode("utf-8"), name)
        except (ConfigError, OSError) as e:
            return await _failed(request, "backups", _("Could not read the backup: {0}").format(_unreadable(e)))
        plan.source = _("configuration backup ({0} UTC)").format(backup_time(name))
        plan.log_source = f"configuration backup ({backup_time(name)} UTC)"
        return await _import_preview(request, plan, "replace")

    async def settings_cluster_post(request):
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        def refused(problem: str):
            """The form again with what was sent - never the key (typed again)."""
            old, current_form = state.config.cluster, form.get("cluster_form") == "2"
            shown = types.SimpleNamespace(
                enabled=form.get("enabled") == "on",
                discovery_port=form.get("discovery_port") or old.discovery_port,
                advertise_ip=(form.get("advertise_ip") or "").strip(),
                peers_text=form.get("peers") or "",
                legacy_auth=_checkbox(form, "legacy_auth") if current_form else old.legacy_auth,
                discovery=_checkbox(form, "discovery") if current_form else old.discovery,
                keep_old_key=_checkbox(form, "keep_old_key"),
                clear_previous_key=_checkbox(form, "clear_previous_key"),
                clear_key=_checkbox(form, "clear_key"))
            return _failed(request, "cluster-settings", problem + _typed_again(form, "key"),
                           cluster=shown)

        try:
            port = int(form.get("discovery_port") or 41750)
        except ValueError:
            return await refused(_("Cluster discovery port must be a number."))
        peers = [ln.strip() for ln in (form.get("peers") or "").splitlines() if ln.strip()]
        try:  # each entry saved here must name an IP address or host name
            for entry in peers:
                parse_cluster_peer(entry, strict=True)
        except ConfigError as e:
            return await refused(str(e))
        generate = form.get("then") == "generate"
        old = state.config.cluster
        try:
            async with state.edit_config():
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
        except ConfigError as e:
            return await refused(str(e))
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
        if generate:   # shown once, on the page the redirect leads to (never in a URL)
            return _done(request, "cluster-settings", new_cluster_key=key, ok=ok)
        return _done(request, "cluster-settings", ok=ok)

    async def settings_cluster_identity(request):
        """Give this node a new cluster id and a new session key - the way out for a copy
        of another installation (its data folder, disk or VM image): the copies no longer
        hide each other in the cluster, and a sign-in to one is not valid on the other.
        Everyone signed in here is signed out, except the admin doing this."""
        if deny := require_role(request, "admin"):
            return deny
        form = await read_form(request)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        old = (state.config.instance_id, state.config.secret_key)
        try:
            async with state.edit_config():
                old = (state.config.instance_id, state.config.secret_key)
                state.config.instance_id = secrets.token_hex(8)
                state.config.secret_key = secrets.token_hex(32)
        except ConfigError as e:
            return await _failed(request, "cluster-settings", str(e))
        await state.cluster.stop()
        await state.cluster.start()
        new_id = state.config.instance_id
        state.log(f"new identity for this node: cluster id {old[0][:8]} -> {new_id[:8]}, new "
                  "session key (everyone else signed in here is signed out)")
        ctx.audit(request, "node_new_identity", f"{old[0][:8]} -> {new_id[:8]}")
        resp = _done(request, "cluster-settings",
                     ok=_("This node has a new identity (cluster ID {0}). "
                          "Everyone else signed in to it was signed out.").format(new_id[:8]))
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
        Route("/settings/request-log", settings_request_log_post, methods=["POST"]),
        Route("/settings/login-notice", settings_login_notice_post, methods=["POST"]),
        Route("/settings/sessions/revoke-all", settings_sessions_revoke_all, methods=["POST"]),
        Route("/settings/api-token", settings_api_token_post, methods=["POST"]),
        Route("/settings/api-token/role", settings_api_token_role, methods=["POST"]),
        Route("/settings/api-token/revoke", settings_api_token_revoke, methods=["POST"]),
        Route("/settings/api-tokens", settings_api_tokens_create, methods=["POST"]),
        Route("/settings/api-tokens/revoke", settings_api_tokens_revoke, methods=["POST"]),
        Route("/settings/users", settings_users_create, methods=["POST"]),
        Route("/settings/users/password", settings_users_password, methods=["POST"]),
        Route("/settings/users/role", settings_users_role, methods=["POST"]),
        Route("/settings/users/delete", settings_users_delete, methods=["POST"]),
        Route("/settings/users/signout", settings_users_signout, methods=["POST"]),
        Route("/settings/users/expiry", settings_users_expiry, methods=["POST"]),
        Route("/settings/users/totp-reset", settings_users_totp_reset, methods=["POST"]),
        Route("/settings/retention", settings_retention, methods=["POST"]),
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
        Route("/settings/config/reload", config_reload, methods=["POST"]),
        Route("/settings/backups/restore-mappings", backup_restore_mappings, methods=["POST"]),
        Route("/settings/backups/delete", backup_delete, methods=["POST"]),
        Route("/settings/backups/remove-account", backup_remove_account, methods=["POST"]),
        Route("/settings/backups/{name}", backup_download),
    ]


def _load_tls_pair(cert: str, key: str, password: str) -> None:
    """Load a certificate and key as the admin UI will, an encrypted key with ``password``
    (raises tls.TlsFileError). A file that is missing is left to the save, which says
    which one."""
    if not (os.path.isfile(data_path(cert)) and os.path.isfile(data_path(key))):
        return
    tls.tls_context(server_side=True, cert=cert, key=key, password=password)


def _keep_previous(paths) -> list[str]:
    """Copy each of `paths` that exists to <path>.bak (permissions kept: a private key
    stays private); the copies made."""
    kept = []
    for path in paths:
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
            kept.append(path + ".bak")
    return kept


def _local_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else "?"


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
    diff(_("Admin UI address"), host_port(o.bind_ip, o.port), host_port(n.bind_ip, n.port))
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
    if two_factor := [n for n in after if n in before
                      and after[n].totp_secret != before[n].totp_secret]:
        out.append(_("Accounts whose two-factor sign-in changes: {0}").format(", ".join(two_factor)))
    if (old.api_token_hash, old.api_token_role) != (new.api_token_hash, new.api_token_role):
        out.append(_("The REST API token as it was then (the current one stops working)"))
    if ({(t.sha256, t.role, t.expires) for t in old.api_tokens}
            != {(t.sha256, t.role, t.expires) for t in new.api_tokens}):
        out.append(_("The other REST API tokens as they were then: {0}").format(
            ", ".join(t.name for t in new.api_tokens) or _("none")))
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


def _unreadable(e: BaseException) -> str:
    """Why a backup could not be read, for the page — in its language for the usual
    causes, not Python's own text (a JSON parser's "Expecting ',' delimiter: line 3
    column 5 (char 20)", "'list' object has no attribute 'get'")."""
    if isinstance(e, ConfigError):
        return str(e)
    if isinstance(e, json.JSONDecodeError):
        return _("it is not valid JSON (line {0}, column {1})").format(e.lineno, e.colno)
    if isinstance(e, UnicodeDecodeError):
        return _("it is not UTF-8 text")
    if isinstance(e, OSError):
        return explain(e, "file") or str(e)
    return _("it is not a ser2net configuration")


def _csv_cell(value: str) -> str:
    """A spreadsheet must not run a cell as a formula (mapping names and user names
    end up in the audit detail): prefix the characters that start one."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
