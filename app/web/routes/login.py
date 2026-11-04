"""Sign-in pages: login/logout, first-run setup and OIDC single sign-on."""
from __future__ import annotations

import asyncio
import secrets

from starlette.responses import RedirectResponse
from starlette.routing import Route

from ...config import User
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
        if not data or not code or not secrets.compare_digest(st, data.get("s", "")):
            return render(request, "login.html", status_code=400, error=None, oidc_enabled=True,
                          oidc_error=_("SSO state mismatch — please try again."))
        claims = await asyncio.to_thread(oidc_auth.complete, oidc, data["r"], code, data["n"], state.log)
        if claims is None:
            return render(request, "login.html", status_code=401, error=None, oidc_enabled=True,
                          oidc_error=_("SSO sign-in failed."))
        username = oidc_auth.username_from_claims(claims, oidc)
        role = oidc_auth.role_from_claims(claims, oidc)
        if not username or not role:
            state.log(f"OIDC login denied: user={username!r} role={role!r} (no mapped group)")
            return render(request, "login.html", status_code=403, error=None, oidc_enabled=True,
                          oidc_error=_("Your account isn't mapped to a role."))
        async with state.config_lock:
            user = state.config.upsert_external_user(username, role, "oidc")
            await state.asave()
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
        ctx.audit(request, "logout")
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
            return csrf_failed()
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
