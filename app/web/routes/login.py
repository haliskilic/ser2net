"""Sign-in pages: login/logout, first-run setup and OIDC single sign-on."""
from __future__ import annotations

import asyncio
import secrets
import time

from starlette.responses import RedirectResponse
from starlette.routing import Route

from ...config import (
    AccountNameError, ConfigError, User, external_name, external_name_problem, new_pwd_version,
)
from ...engine import netinfo
from ...i18n import _
from .. import auth, ldap_auth, oidc_auth
from .common import Ctx, csrf_failed
from .forms import _password_problem, _username_problem

OIDC_COOKIE = "ser2net_oidc"


def routes(ctx: Ctx) -> list:
    state, render, client_ip, set_session = ctx.state, ctx.render, ctx.client_ip, ctx.set_session

    async def login_get(request):
        return render(request, "login.html", error=None,
                      oidc_enabled=state.config.oidc.enabled)

    # ---------------- LDAP / OIDC groups -> role ----------------
    # (source, user name) whose sessions a refused sign-in already ended, so that more
    # refused tries don't save config.json (and push out a backup) each time; their next
    # successful sign-in takes them off again
    refused: set[tuple[str, str]] = set()

    def _directory_role(source: str, username: str, groups, settings) -> str | None:
        """The role the groups reported by the directory (LDAP) or the identity provider
        (OIDC) give `username`. Logs which groups they have when no group setting
        matched, and warns when a setting matched one only as part of its name."""
        name = source.upper()
        d = ldap_auth.role_decision(groups, settings)
        if d.partial is not None:
            _warn_partial(source, username, d)
        if not d.setting:
            why = ("their groups could not be read (see above)"
                   if getattr(groups, "lookup_failed", False) else
                   f"no group setting matches their groups: {ldap_auth.group_names(groups)}")
            if d.role:
                state.log(f"{name} login for {username!r}: {why}; default role {d.role}")
            else:
                state.log(f"{name} login denied for {username!r}: {why}")
        return d.role

    def _warn_partial(source: str, username: str, d: ldap_auth.RoleDecision) -> None:
        if d.partial is None:
            return
        setting, value, group = d.partial
        section, whole = (("LDAP / Active Directory", "its full DN") if source == "ldap" else
                          ("Single sign-on (OIDC)", "the whole value"))
        state.log(f"WARNING: {source.upper()} {setting} {value!r} is only part of the name of "
                  f"the group {group!r}, which no longer counts as a match: {username!r} gets "
                  f"{d.role or 'no access'} instead of {d.partial_role}. A group setting has to "
                  f"name the whole group: set it to {ldap_auth.group_name(group)!r} or {whole} "
                  f"under Settings → {section} ({source}.{setting} in config.json). If no "
                  f"admin can sign in any more, `ser2net --set-password NAME` with a new NAME "
                  f"creates a local admin account.")
        state.group_warnings[(source, setting)] = {
            "setting": setting, "value": value, "group": group,
            "name": ldap_auth.group_name(group), "user": username, "got": d.role or "",
            "was": d.partial_role, "time": time.strftime("%Y-%m-%d %H:%M")}

    def _note_tls_problem(problem: ldap_auth.TlsProblem) -> None:
        """A sign-in found the directory server's certificate refused (or ldap.ca_file
        unusable): Settings → LDAP shows it until that section is saved or a directory
        sign-in works. Runs in the sign-in's worker thread."""
        state.ldap_tls_problem = {"kind": problem.kind, "reason": problem.reason,
                                  "uri": problem.uri, "ca_file": problem.ca_file,
                                  "unreadable": problem.unreadable,
                                  "time": time.strftime("%Y-%m-%d %H:%M")}

    async def _end_refused_sessions(source: str, username: str) -> None:
        """The directory or identity provider confirmed who this is, but their groups
        give no role any more: end the sessions their shadow account still has, which
        would otherwise keep the role saved with it until they expire."""
        if (source, username) in refused:
            return
        async with state.config_lock:
            user = state.config.get_user(username)
            if user is None or user.source != source:
                return
            user.pwd_version += 1
            refused.add((source, username))
            try:
                await state.asave()
            except (ConfigError, OSError) as e:  # still ended in memory, until a restart
                state.log(f"could not save the sign-out of {username!r}: {e}")
        state.log(f"{source.upper()}: {username!r} signed out everywhere: their groups give "
                  f"no role any more")

    def _name_refused(request, source: str, username: str) -> str | None:
        """A sign-in through the directory (LDAP) or the identity provider (OIDC) that
        can't have an account under this name is refused: the name of a local account,
        letter case aside — such a sign-in must never take over, convert or demote that
        account (upsert_external_user refuses it too) — or a name no account can have
        (external_name_problem). Logged, audited and notified like a failed sign-in.
        Returns the sign-in page's message, or None when the name can be used."""
        local = state.config.named(username, "local")
        why = (f"the local account {local[0].username!r} has this name" if local
               else external_name_problem(username))
        if why is None:
            return None
        ip, what = client_ip(request), source.upper()
        if local:
            state.log(f"{what} login denied for {username!r}: {why}, and an {what} sign-in "
                      f"never takes over a local account. To let them sign in with {what}, "
                      f"delete the local account in Settings → Users (another admin must "
                      f"remain).")
        else:
            hint = ("; Settings → Single sign-on (OIDC) → Username claim can name another "
                    "claim" if source == "oidc" else "")
            state.log(f"{what} login denied for {username!r}: no account can have this name "
                      f"here: {why}{hint}")
        ctx.audit(request, f"{source}_login_denied",
                  f"local account {local[0].username}" if local else why, user=username)
        state.notifier.emit("login_failed", ip, "", f"{what} user {username!r} from {ip}: "
                            + ("the name of a local account" if local else why))
        if local:
            return _("There is a local account named {0} here, so you can't sign in under that "
                     "name with SSO or a directory account. Sign in with that account’s "
                     "password, or ask an admin to delete the local account first.").format(
                         local[0].username)
        return _("Your user name from the directory or the identity provider can’t be used "
                 "here; an admin can see why in all.log.")

    def _refusal_page(request, source: str, message: str | None):
        if source == "oidc":
            return render(request, "login.html", status_code=403, error=None, oidc_enabled=True,
                          oidc_error=message)
        return render(request, "login.html", status_code=403, error=message,
                      oidc_enabled=state.config.oidc.enabled)

    async def _save_external(source: str, username: str, role: str) -> User | None:
        """The saved shadow account of an LDAP/OIDC user who signed in; None when a local
        account got this name while the sign-in waited for the lock."""
        async with state.config_lock:
            try:
                user = state.config.upsert_external_user(username, role, source)
            except AccountNameError:
                return None
            await state.asave()
        return user

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
                          oidc_enabled=True, oidc_error=_("SSO provider is unreachable."))
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
        if not (data and code and st.isascii()  # (compare_digest raises on non-ASCII text)
                and secrets.compare_digest(st, str(data.get("s", "")))):
            return render(request, "login.html", status_code=400, error=None, oidc_enabled=True,
                          oidc_error=_("SSO state mismatch — please try again."))
        claims = await asyncio.to_thread(oidc_auth.complete, oidc, data["r"], code, data["n"], state.log)
        if claims is None:
            return render(request, "login.html", status_code=401, error=None, oidc_enabled=True,
                          oidc_error=_("SSO sign-in failed."))
        username = oidc_auth.username_from_claims(claims, oidc)
        if not username:
            state.log("OIDC login denied: the ID token names no user")
        elif refusal := _name_refused(request, "oidc", username):
            return _refusal_page(request, "oidc", refusal)
        role = username and _directory_role("oidc", username,
                                            oidc_auth.groups_from_claims(claims, oidc), oidc)
        if not username or not role:
            if username:
                await _end_refused_sessions("oidc", username)
            return render(request, "login.html", status_code=403, error=None, oidc_enabled=True,
                          oidc_error=_("Your account isn't mapped to a role."))
        user = await _save_external("oidc", username, role)
        if user is None:   # a local account got the name meanwhile
            return _refusal_page(request, "oidc", _name_refused(request, "oidc", username))
        refused.discard(("oidc", username))
        state.log(f"login: {username} ({role}, oidc) from {client_ip(request)}")
        ctx.audit(request, "oidc_login", username, user=username)
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, request, user)
        resp.delete_cookie(OIDC_COOKIE, path="/auth/oidc")
        return resp

    async def login_post(request):
        ip = client_ip(request)
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        if state.rate_limiter.blocked(ip):
            return render(request, "login.html", status_code=429,
                          error=_("Too many attempts. Wait a few minutes and try again."),
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
            username = external_name(username)
            groups = await asyncio.to_thread(
                ldap_auth.authenticate, state.config.ldap, username, password, state.log,
                on_tls_problem=_note_tls_problem)
            if groups is not None:
                state.ldap_tls_problem = None   # the connection to the directory works
                if refusal := _name_refused(request, "ldap", username):
                    return _refusal_page(request, "ldap", refusal)
                role = _directory_role("ldap", username, groups, state.config.ldap)
                if role:
                    authed = await _save_external("ldap", username, role)
                    if authed is None:   # a local account got the name meanwhile
                        return _refusal_page(request, "ldap",
                                             _name_refused(request, "ldap", username))
                    refused.discard(("ldap", username))
                else:
                    await _end_refused_sessions("ldap", username)
        if authed is not None:
            state.rate_limiter.reset(ip)
            state.log(f"login: {username} ({authed.role}, {authed.source}) from {ip}")
            ctx.audit(request, "login", f"{authed.role}, {authed.source}", user=username)
            resp = RedirectResponse("/", status_code=303)
            set_session(resp, request, authed)
            return resp
        state.rate_limiter.record_failure(ip)
        state.log(f"failed login for {username!r} from {ip}")
        ctx.audit(request, "login_failed", "", user=username)
        state.notifier.emit("login_failed", ip, "", f"user {username!r} from {ip}")
        return render(request, "login.html", status_code=401,
                      error=_("Invalid username or password."),
                      oidc_enabled=state.config.oidc.enabled)

    async def logout_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        # the cookie is a signed token: end it on the server too, so a copy of it is
        # refused from now on (not just deleted in this browser)
        state.revoked_sessions.add(state.config.secret_key,
                                   request.cookies.get(auth.SESSION_COOKIE))
        ctx.audit(request, "logout")
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp

    # ---------------- first-run setup ----------------
    # Only while no account exists (GuardMiddleware sends everything here then). Like
    # open access, it trusts a request only when every host name it claims is this
    # machine's (auth.claims_own_host, the DNS-rebinding guard). Through another name
    # (a reverse proxy, Docker's host name) the page asks for the one-time code that
    # AppState.prepare_first_run wrote to setup-code.txt. No redirect: every path leads here.
    def setup_page(request, status_code=200, error=None):
        return render(request, "setup.html", status_code=status_code, error=error,
                      ips=netinfo.list_ip_candidates(), admin=state.config.admin_ui,
                      locked=state.setup_locked, need_code=not auth.claims_own_host(request.headers),
                      host=auth.foreign_host(request.headers))

    async def setup_get(request):
        trusted = auth.claims_own_host(request.headers) and not state.setup_locked
        return setup_page(request, 200 if trusted else 403)

    async def setup_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        if state.setup_locked:
            return setup_page(request, status_code=403)
        ip = client_ip(request)
        if not auth.claims_own_host(request.headers):
            if state.rate_limiter.blocked(ip):
                return setup_page(request, 429, _("Too many attempts. Wait a few minutes and try "
                                                  "again."))
            if not state.setup_code_ok(form.get("setup_code") or ""):
                state.rate_limiter.record_failure(ip)
                state.log(f"first-run setup refused: wrong setup code from {ip} "
                          f"(host {auth.foreign_host(request.headers)!r})")
                return setup_page(request, 403, _("That setup code is not right. It is in "
                                                  "setup-code.txt in ser2net’s data folder."))
        username = (form.get("username") or "admin").strip() or "admin"
        pw = form.get("password", "")
        pw2 = form.get("password2", "")
        err = _username_problem(username) or _password_problem(pw, pw2)
        if err:
            return setup_page(request, 400, err)
        pw_hash = await asyncio.to_thread(auth.hash_password, pw)
        async with state.config_lock:
            # checked again here: another setup request may have finished while this one
            # was sending its form or hashing — it must not replace that admin
            claimed = state.config.password_set
            if not claimed:
                state.config.users = [User(username=username, password_hash=pw_hash,
                                           role="admin", pwd_version=new_pwd_version())]
                try:
                    await state.asave()
                except ConfigError as e:
                    state.config.users = []
                    return setup_page(request, 400, str(e))
        if claimed:
            state.log(f"first-run setup from {ip} ignored: the admin account exists already")
            return RedirectResponse("/login", status_code=303)
        state.first_run_done()
        user = state.config.get_user(username)
        state.log(f"first-run setup complete; admin user '{username}' created")
        ctx.audit(request, "first_run_setup", username, user=username)
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, request, user)
        return resp

    return [
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout_post, methods=["POST"]),
        Route("/auth/oidc/login", oidc_login, methods=["GET"]),
        Route("/auth/oidc/callback", oidc_callback, methods=["GET"]),
        Route("/setup", setup_get, methods=["GET"]),
        Route("/setup", setup_post, methods=["POST"]),
    ]
