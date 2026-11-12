"""Sign-in pages: login/logout, first-run setup and OIDC single sign-on."""
from __future__ import annotations

import asyncio
import re
import secrets
import time

from starlette.responses import RedirectResponse
from starlette.routing import Route

from ... import notify
from ...config import (
    AccountNameError, ConfigError, User, external_name, external_name_problem, fold,
    new_pwd_version,
)
from ...engine import netguard, netinfo
from ...i18n import _
from .. import auth, ldap_auth, oidc_auth, proxy
from . import totp
from .common import Ctx, csrf_failed, local_path
from .forms import _password_problem, _username_problem

OIDC_COOKIE = "ser2net_oidc"
# The sign-in, first-run setup and sign-out forms are a few short fields: a larger request
# is refused (413) before it is read, a file or a long field (400) while it is.
FORM_MAX_BYTES = 64 * 1024
FORM_LIMITS = {"max_files": 0, "max_fields": 16, "max_part_size": 16 * 1024}


def routes(ctx: Ctx) -> list:
    state, render, client_ip, set_session = ctx.state, ctx.render, ctx.client_ip, ctx.set_session

    async def login_get(request):
        # ?next=: the page to go back to (GuardMiddleware sends a page request without a
        # session here with it; the dashboard's "session ended" link too)
        return render(request, "login.html", error=None,
                      oidc_enabled=state.config.oidc.enabled,
                      next=local_path(request.query_params.get("next"), ""))

    def _typed(form) -> str:
        """The user name a refused sign-in form had, to fill it in again: a retry that
        re-types only the password must not go to 'admin' (the field's default)."""
        return str(form.get("username") or "").strip()[:auth.LOGIN_NAME_MAX]

    # ---------------- LDAP / OIDC groups -> role ----------------
    # (source, user name) whose sessions a refused sign-in already ended, so that more
    # refused tries don't save config.json (and push out a backup) each time; their next
    # successful sign-in takes them off again. An LDAP name in any letter case is one
    # person (AppConfig.external_accounts).
    refused: set[tuple[str, str]] = set()

    def _person(source: str, username: str) -> tuple[str, str]:
        return source, fold(username) if source == "ldap" else username

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
        if _person(source, username) in refused:
            return
        accounts = []
        try:
            async with state.edit_config(keep_on_failure=True):  # ended in memory anyway
                accounts = state.config.external_accounts(username, source)
                for user in accounts:
                    user.pwd_version += 1
        except ConfigError as e:  # still ended in memory, until a restart
            state.log(f"could not save the sign-out of {username!r}: {e}")
        if not accounts:
            return
        refused.add(_person(source, username))
        state.log(f"{source.upper()}: {username!r} signed out everywhere: their groups give "
                  f"no role any more")

    def _record_failure(ip: str, user: str, source: str = "", reason: str = "",
                        action: str = "login_failed", detail: str = "") -> None:
        """The audit record (bounded: state.failed_logins) and the notification of a
        failed or refused sign-in (notify.sign_in_detail: personal data left out there
        when Settings -> Notifications says so)."""
        state.failed_logins.add(ip, user, action, detail)
        state.notifier.emit("login_failed", ip, "", notify.sign_in_detail(
            state.config.notifications, user, ip, source, reason))

    def _oidc_failed(request, reason: str, user: str = "") -> None:
        """An SSO sign-in that came back from the identity provider and failed: recorded
        (oidc_login_failed, with a short reason; all.log has the details) and notified
        like a failed local or LDAP sign-in."""
        _record_failure(client_ip(request), user, "OIDC", reason, "oidc_login_failed", reason)

    def _name_refused(request, source: str, username: str) -> str | None:
        """A sign-in through the directory (LDAP) or the identity provider (OIDC) that
        can't have an account under this name is refused: the name of a local account or
        of one of the other sign-in method, letter case aside — such a sign-in must never
        take over, convert or demote that account (upsert_external_user refuses it too) —
        or a name no account can have (external_name_problem). Logged, audited (within
        the failed sign-ins' limit) and notified like a failed sign-in. Returns the
        sign-in page's message, or None when the name can be used."""
        taken = state.config.taken_name(username, source)
        why = (f"the {taken.kind} account {taken.username!r} has this name" if taken
               else external_name_problem(username))
        if why is None:
            return None
        ip, what = client_ip(request), source.upper()
        if taken:
            other = ("a local account" if taken.source == "local"
                     else "an account of another sign-in method")
            state.log(f"{what} login denied for {username!r}: {why}, and an {what} sign-in "
                      f"never takes over {other}. To let them sign in with {what}, delete the "
                      f"{taken.kind} account in Settings → Users (another admin must remain).")
        else:
            hint = ("; Settings → Single sign-on (OIDC) → Username claim can name another "
                    "claim" if source == "oidc" else "")
            state.log(f"{what} login denied for {username!r}: no account can have this name "
                      f"here: {why}{hint}")
        _record_failure(ip, username, what,
                        f"the name of a {taken.kind} account" if taken else why,
                        f"{source}_login_denied",
                        f"{taken.kind} account {taken.username}" if taken else why)
        if taken and taken.source == "local":
            return _("There is a local account named {0} here, so you can't sign in under that "
                     "name with SSO or a directory account. Sign in with that account’s "
                     "password, or ask an admin to delete the local account first.").format(
                         taken.username)
        if taken:
            return _("An account named {0} here signs in another way (through the directory or "
                     "SSO), so you can't sign in under that name like this. Ask an admin to "
                     "delete that account first.").format(taken.username)
        return _("Your user name from the directory or the identity provider can’t be used "
                 "here; an admin can see why in all.log.")

    def _refusal_page(request, source: str, message: str | None, username: str = "",
                      nxt: str = ""):
        if source == "oidc":
            return render(request, "login.html", status_code=403, error=None, oidc_enabled=True,
                          oidc_error=message)
        return render(request, "login.html", status_code=403, error=message,
                      oidc_enabled=state.config.oidc.enabled, username=username, next=nxt)

    async def _save_external(source: str, username: str, role: str) -> User | None:
        """The saved shadow account of an LDAP/OIDC user who signed in; None when a local
        account got this name while the sign-in waited for the lock. The directory decides
        the role, also when it takes the last admin role away (refusing that would keep
        a removed admin's rights): that is logged as a warning."""
        before = state.config.usable_admin_count()
        user = None
        try:
            # saved only when the account changed (a repeated sign-in writes nothing); used
            # even when it can't be saved: the sign-in goes on, and it is made again later
            async with state.edit_config(keep_on_failure=True):
                before = state.config.usable_admin_count()
                user = state.config.upsert_external_user(username, role, source)
        except AccountNameError:
            return None
        except ConfigError as e:
            state.log(f"WARNING: {source.upper()}: could not save the account of "
                      f"{username!r} ({e}); they are signed in, but it is made again at their "
                      f"next sign-in after a restart")
        if user is None:  # (upsert_external_user returns the account or raises)
            return None
        if before and not state.config.usable_admin_count():
            state.log(f"WARNING: {source.upper()} gives {user.username!r} the role {role}, so "
                      f"no admin account can sign in any more. `ser2net --set-password "
                      f"NAME` with a new NAME creates a local admin account.")
        return user

    # ---------------- OIDC single sign-on ----------------
    async def oidc_login(request):
        oidc = state.config.oidc
        if not oidc.enabled:
            return proxy.redirect(request, "/login")
        redirect_uri = oidc_auth.redirect_uri(oidc, request,
                                              proxy.public_url(state.config.admin_ui))
        st, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        verifier = oidc_auth.new_code_verifier()   # PKCE
        try:
            url = await asyncio.to_thread(oidc_auth.build_authorize_url, oidc, redirect_uri, st,
                                          nonce, code_verifier=verifier)
        except oidc_auth.InsecureEndpoint as e:
            state.log(f"OIDC login init failed: {e}")
            return render(request, "login.html", status_code=502, error=None, oidc_enabled=True,
                          oidc_error=_("SSO sign-in is off: the identity provider gives an "
                                       "address that isn’t https:// (all.log says which)."))
        except Exception as e:
            state.log(f"OIDC login init failed: {e}")
            return render(request, "login.html", status_code=502, error=None,
                          oidc_enabled=True, oidc_error=_("SSO provider is unreachable."))
        # the address the provider sends the browser back to, which it must have registered:
        # when it refuses it, the user stays on the provider's error page
        state.log(f"OIDC login init from {client_ip(request)}: redirect URI {redirect_uri!r}"
                  + ("" if oidc.redirect_uri.strip() else
                     " (made from the request; Settings → Single sign-on (OIDC) shows it)"))
        resp = RedirectResponse(url, status_code=303)
        # "x": the page to go back to after the sign-in (?next=, as for a password sign-in)
        payload = {"s": st, "n": nonce, "r": redirect_uri, "v": verifier}
        if nxt := local_path(request.query_params.get("next"), ""):
            payload["x"] = nxt
        auth.set_cookie(resp, request, state.config, OIDC_COOKIE,
                        auth.sign_payload(state.config.secret_key, payload, ttl_seconds=600),
                        max_age=600, path="/auth/oidc")
        return resp

    def _same(given: str, expected) -> bool:
        """The state from the URL is the one in the signed cookie (compared as bytes: a
        str with non-ASCII text would make compare_digest raise)."""
        return secrets.compare_digest(given.encode(), str(expected or "").encode())

    def _provider_error(request, data, error: str):
        """The provider sent the browser back with an error instead of a code (RFC 6749
        4.1.2.1, OIDC Core 3.1.2.6): logged with its description, shown by its code —
        never the provider's own text."""
        st = request.query_params.get("state", "")
        ours = bool(data) and _same(st, data.get("s"))
        desc = request.query_params.get("error_description", "")
        state.log(f"OIDC: the identity provider refused the sign-in: {error[:64]!r}"
                  + (f" ({desc[:200]!r})" if desc else "")
                  + ("" if ours else " (not for a sign-in started in this browser)"))
        code = error if re.fullmatch(r"[a-z0-9_]{1,64}", error) else "?"
        _oidc_failed(request, f"the identity provider refused it ({code})"
                     + ("" if ours else ", not for a sign-in started in this browser"))
        if error == "access_denied":
            status, message = 403, _("The identity provider didn’t let you sign in to ser2net. "
                                     "Ask its administrator to give your account access.")
        elif error in ("temporarily_unavailable", "server_error"):
            status, message = 503, _("The identity provider can’t sign you in right now. Try "
                                     "again in a few minutes.")
        else:
            status, message = 400, _("The identity provider refused the sign-in request ({0}). "
                                     "An admin should check the SSO settings; all.log has "
                                     "the details.").format(code)
        return render(request, "login.html", status_code=status, error=None, oidc_enabled=True,
                      oidc_error=message)

    async def oidc_callback(request):
        oidc = state.config.oidc
        if not oidc.enabled:
            return proxy.redirect(request, "/login")
        stash = auth.cookie(request, OIDC_COOKIE, path="/auth/oidc")
        data = auth.read_payload(state.config.secret_key, stash)
        if (error := request.query_params.get("error")) is not None:
            return _provider_error(request, data, error)
        code = request.query_params.get("code", "")
        st = request.query_params.get("state", "")
        if (not data or not code or not _same(st, data.get("s"))
                or not isinstance(data.get("r"), str) or not isinstance(data.get("n"), str)):
            _oidc_failed(request, "the answer doesn't match a sign-in started in this browser"
                         + (" (none, or it took over 10 minutes)" if not data else ""))
            return render(request, "login.html", status_code=400, error=None, oidc_enabled=True,
                          oidc_error=_("SSO state mismatch — please try again."))
        # "v": the PKCE verifier (a state cookie from before it was sent has none)
        claims = await asyncio.to_thread(oidc_auth.complete, oidc, data["r"], code, data["n"],
                                         state.log, code_verifier=str(data.get("v") or ""))
        if claims is None:
            _oidc_failed(request, "the code or the ID token was refused (all.log says why)")
            return render(request, "login.html", status_code=401, error=None, oidc_enabled=True,
                          oidc_error=_("SSO sign-in failed."))
        username = oidc_auth.username_from_claims(claims, oidc)
        if not username:
            state.log(f"OIDC login denied: {oidc_auth.no_username_reason(claims, oidc)}")
            _oidc_failed(request, "no user name in the ID token (all.log says why)")
        elif refusal := _name_refused(request, "oidc", username):
            return _refusal_page(request, "oidc", refusal)
        role = username and _directory_role("oidc", username,
                                            oidc_auth.groups_from_claims(claims, oidc), oidc)
        if not username or not role:
            if username:
                _oidc_failed(request, "their groups give no role", username)
                await _end_refused_sessions("oidc", username)
            return render(request, "login.html", status_code=403, error=None, oidc_enabled=True,
                          oidc_error=_("Your account isn't mapped to a role."))
        user = await _save_external("oidc", username, role)
        if user is None:   # a local account got the name meanwhile
            return _refusal_page(request, "oidc", _name_refused(request, "oidc", username))
        refused.discard(_person("oidc", username))
        state.log(f"login: {user.username} ({role}, oidc) from {client_ip(request)}")
        ctx.audit(request, "oidc_login", user.username, user=user.username)
        # a state cookie from before 2.9 has no "x": the dashboard
        resp = proxy.redirect(request, local_path(data.get("x")))
        set_session(resp, request, user)
        ctx.new_csrf_token(request)
        auth.delete_cookie(resp, request, state.config, OIDC_COOKIE, path="/auth/oidc")
        return resp

    # sign-ins not tried because too many were being checked already: logged once a minute
    busy_note = netguard.LogEvery(lambda msg: state.log(msg))

    def _note_name_paused(label: str, username: str) -> None:
        """Log when failed attempts (from any address) pause sign-ins under a name: that is
        what keeps someone guessing one account's password from many addresses below the
        directory's own lock-out."""
        lim = state.name_limiter
        if lim.blocked(username):
            name = "a name that is no account's" if label == auth.UNKNOWN_USER else repr(label)
            state.log(f"sign-ins under {name} are refused for now: {lim.max_attempts} "
                      f"failed in {lim.window_s // 60} minutes (from any address)")

    def _is_account_name(name: str) -> bool:
        """Is `name` an account's (letter case aside)? Only then does a failed sign-in
        record the name typed."""
        folded = name.casefold()
        return any(u.username.casefold() == folded for u in state.config.users)

    async def _renew_hash(user: User, password: str) -> None:
        """A local account's password hash from an older version (a lower scrypt cost) is
        made again with today's cost once its password has been checked. Best effort:
        the sign-in works even when this can't be saved (it is tried again at the next
        one), and the account's sessions stay (pwd_version is unchanged)."""
        if state.password_work.busy():
            return
        old = user.password_hash
        new = await state.password_work.run(auth.hash_password, password)
        async with state.config_lock:
            if state.config.get_user(user.username) is not user or user.password_hash != old:
                return  # changed meanwhile
            user.password_hash = new
            try:  # only this hash changes: an unrelated setting must not stop it
                await asyncio.to_thread(state.store.save, state.config, validate=False)
            except OSError as e:
                user.password_hash = old
                state.log(f"could not save the renewed password hash of {user.username!r}: {e}")
                return
        state.log(f"the password hash of {user.username!r} was renewed with today's scrypt cost")

    def _too_many(request, typed: str, nxt: str):
        return render(request, "login.html", status_code=429,
                      error=_("Too many attempts. Wait a few minutes and try again."),
                      oidc_enabled=state.config.oidc.enabled, username=typed, next=nxt)

    def _login_failed(request, ip: str, username: str, typed: str, nxt: str):
        """Log, record and notify a failed sign-in (login_post counted it already); the
        sign-in page again, with the name typed (`typed`) and the page to go back to
        (`nxt`). A name that is no account's is recorded as auth.UNKNOWN_USER: what was
        typed may well be a password."""
        label = username if _is_account_name(username) else auth.UNKNOWN_USER
        state.log(f"failed login for {label!r} from {ip}")
        _record_failure(ip, label)
        _note_name_paused(label, username)
        return render(request, "login.html", status_code=401,
                      error=_("Invalid username or password."),
                      oidc_enabled=state.config.oidc.enabled, username=typed, next=nxt)

    async def login_post(request):
        ip = client_ip(request)
        form = await request.form(**FORM_LIMITS)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        typed = _typed(form)
        nxt = local_path(form.get("next"), "")   # the page to go back to (login_get)
        if state.rate_limiter.blocked(ip):
            return _too_many(request, typed, nxt)
        # username defaults to "admin" so single-user (password-only) logins keep working
        username = (form.get("username") or "admin").strip() or "admin"
        password = form.get("password", "")
        if len(username) > auth.LOGIN_NAME_MAX:  # no account has such a name: not tried
            state.rate_limiter.record_failure(ip)
            return _login_failed(request, ip, username, typed, nxt)
        if state.name_limiter.blocked(username):  # from any address: that name only
            return _too_many(request, typed, nxt)
        user = state.config.get_user(username)
        local = user is not None and user.source == "local"
        directory = not local and state.config.ldap.enabled
        work = state.directory_work if directory else state.password_work
        if work.busy():  # not tried, not counted
            busy_note(f"sign-in from {ip} not tried: {work.limit + work.queue} "
                      f"{'directory sign-ins' if directory else 'password checks'} are "
                      "running or waiting already")
            return render(request, "login.html", status_code=429,
                          error=_("Too many sign-ins at the same time. Try again in a "
                                  "moment."),
                          oidc_enabled=state.config.oidc.enabled, username=typed, next=nxt)
        # counted as failed before the password is checked, in the same step as the checks
        # above (no await between them): concurrent attempts can't all get past the limits
        # while the first ones are still being checked. A sign-in that works resets them.
        state.rate_limiter.record_failure(ip)
        state.name_limiter.record_failure(username)
        authed = None
        # local account: verify the scrypt hash off the event loop (it's slow on purpose)
        if local and user is not None:
            if await work.run(auth.verify_password, password, user.password_hash):
                authed = user
        # LDAP/AD: for unknown users (or LDAP shadow accounts) when LDAP is enabled
        elif directory:
            username = external_name(username)
            groups = await work.run(
                ldap_auth.authenticate, state.config.ldap, username, password, state.log,
                on_tls_problem=_note_tls_problem)
            if groups is not None:
                state.ldap_tls_problem = None   # the connection to the directory works
                if refusal := _name_refused(request, "ldap", username):
                    return _refusal_page(request, "ldap", refusal, typed, nxt)
                role = _directory_role("ldap", username, groups, state.config.ldap)
                if role:
                    authed = await _save_external("ldap", username, role)
                    if authed is None:   # a local account got the name meanwhile
                        return _refusal_page(request, "ldap",
                                             _name_refused(request, "ldap", username), typed,
                                             nxt)
                    refused.discard(_person("ldap", username))
                else:
                    await _end_refused_sessions("ldap", username)
        else:  # no such local account, no directory: the work of a wrong password
            await work.run(auth.verify_password, password, auth.DUMMY_HASH)
        if authed is not None and authed.totp_on:  # the password alone is not enough
            if local and auth.needs_rehash(authed.password_hash):
                await _renew_hash(authed, password)
            return totp.challenge(ctx, request, authed, nxt)
        if authed is not None:   # under its account's name (LDAP: in any letter case)
            state.rate_limiter.reset(ip)
            state.name_limiter.reset(authed.username)
            if local and auth.needs_rehash(authed.password_hash):
                await _renew_hash(authed, password)
            state.log(f"login: {authed.username} ({authed.role}, {authed.source}) from {ip}")
            ctx.audit(request, "login", f"{authed.role}, {authed.source}", user=authed.username)
            resp = proxy.redirect(request, nxt or "/")
            set_session(resp, request, authed)
            ctx.new_csrf_token(request)
            return resp
        return _login_failed(request, ip, username, typed, nxt)

    async def _sign_out_everywhere(username: str) -> None:
        """A logout the revocation list has no room for: end all of the account's
        sessions instead (pwd_version + 1), which ends the copies of its cookies too."""
        user = None
        try:
            async with state.edit_config(keep_on_failure=True):  # ended in memory anyway
                user = state.config.get_user(username)
                if user is not None:
                    user.pwd_version += 1
        except ConfigError as e:  # still ended in memory, until a restart
            state.log(f"could not save the sign-out of {username!r}: {e}")
        if user is None:
            return
        state.revoked_sessions.forget(username)
        state.log(f"{username!r} signed out everywhere: too many of its sessions were ended "
                  "by a logout to remember each one")

    async def logout_post(request):
        form = await request.form(**FORM_LIMITS)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        # the cookie is a signed token: end it on the server too, so a copy of it is
        # refused from now on (not just deleted in this browser)
        token = auth.cookie(request, auth.SESSION_COOKIE)
        full = state.revoked_sessions.add(state.config.secret_key, token)
        if full is not None and auth.session_user(state.config, token) is not None:
            await _sign_out_everywhere(full)
        if not ctx.is_anonymous(request):  # an open-access visitor had no session to end
            ctx.audit(request, "logout")
        resp = proxy.redirect(request, "/login")
        auth.delete_cookie(resp, request, state.config, auth.SESSION_COOKIE)
        ctx.new_csrf_token(request)
        return resp

    # ---------------- first-run setup ----------------
    # Only while no account exists (GuardMiddleware sends everything here then). Like
    # open access, it trusts a request only when every host name it claims is this
    # machine's (auth.claims_own_host, the DNS-rebinding guard). Through another name
    # (a reverse proxy, Docker's host name) the page asks for the one-time code that
    # AppState.prepare_first_run wrote to setup-code.txt. No redirect: every path leads here.
    def setup_page(request, status_code=200, error=None, username=""):
        # username: the admin name a refused form had (accounts can't be renamed, so one
        # that silently fell back to "admin" would stay so)
        return render(request, "setup.html", status_code=status_code, error=error,
                      ips=netinfo.list_ip_candidates(), admin=state.config.admin_ui,
                      locked=state.setup_locked, need_code=not auth.claims_own_host(request.headers),
                      host=auth.foreign_host(request.headers), username=username)

    async def setup_get(request):
        trusted = auth.claims_own_host(request.headers) and not state.setup_locked
        return setup_page(request, 200 if trusted else 403)

    async def setup_post(request):
        form = await request.form(**FORM_LIMITS)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        if state.setup_locked:
            return setup_page(request, status_code=403)
        ip = client_ip(request)
        typed = _typed(form)
        if not auth.claims_own_host(request.headers):
            if state.rate_limiter.blocked(ip):
                return setup_page(request, 429, _("Too many attempts. Wait a few minutes and try "
                                                  "again."), typed)
            if not state.setup_code_ok(form.get("setup_code") or ""):
                state.rate_limiter.record_failure(ip)
                state.log(f"first-run setup refused: wrong setup code from {ip} "
                          f"(host {auth.foreign_host(request.headers)!r})")
                return setup_page(request, 403, _("That setup code is not right. It is in "
                                                  "setup-code.txt in ser2net’s data folder."), typed)
        username = (form.get("username") or "admin").strip() or "admin"
        pw = form.get("password", "")
        pw2 = form.get("password2", "")
        err = _username_problem(username) or _password_problem(pw, pw2)
        if err:
            return setup_page(request, 400, err, typed)
        pw_hash = await state.password_work.run(auth.hash_password, pw)
        try:
            async with state.edit_config():
                # checked again here: another setup request may have finished while this one
                # was sending its form or hashing — it must not replace that admin
                claimed = state.config.password_set
                if not claimed:
                    state.config.users = [User(username=username, password_hash=pw_hash,
                                               role="admin", pwd_version=new_pwd_version())]
        except ConfigError as e:  # not saved: still no account
            return setup_page(request, 400, str(e), typed)
        if claimed:
            state.log(f"first-run setup from {ip} ignored: the admin account exists already")
            return proxy.redirect(request, "/login")
        state.first_run_done()
        if (user := state.config.get_user(username)) is None:  # (created just above)
            return RedirectResponse("/login", status_code=303)
        state.log(f"first-run setup complete; admin user '{username}' created")
        ctx.audit(request, "first_run_setup", username, user=username)
        resp = proxy.redirect(request, "/")
        set_session(resp, request, user)
        ctx.new_csrf_token(request)
        return resp

    return [
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"], max_body_size=FORM_MAX_BYTES),
        Route("/logout", logout_post, methods=["POST"], max_body_size=FORM_MAX_BYTES),
        Route("/auth/oidc/login", oidc_login, methods=["GET"]),
        Route("/auth/oidc/callback", oidc_callback, methods=["GET"]),
        Route("/setup", setup_get, methods=["GET"]),
        Route("/setup", setup_post, methods=["POST"], max_body_size=FORM_MAX_BYTES),
    ]
