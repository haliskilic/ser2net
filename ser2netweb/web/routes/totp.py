"""Two-factor sign-in (TOTP, ser2netweb/web/totp.py) for local accounts: the code step of a
sign-in, and setting it up or turning it off (Settings → Two-factor sign-in).

Off unless an account sets it up. A local account with User.totp_secret signs in in two
steps: /login checks the password as always, then (challenge) sets a short-lived signed
cookie naming the account instead of a session and sends the browser to /auth/totp, which
asks for the code of the authenticator app and only then gives the session. A wrong code
counts against the address, together with wrong passwords (AppState.rate_limiter), and
against the account (CODE_ATTEMPTS in CODE_WINDOW_S); a code accepted once is not
accepted again (the replay guard is in memory: after a restart, a code used just before
it could be used once more within its 90 s window).

Lost authenticator: an admin turns it off in Settings → Users, or `ser2netweb --set-password
NAME` on the server (ser2netweb/accounts.py) sets a new password and turns it off — also for the
only admin, and with ser2netweb stopped.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ... import notify
from ...config import ConfigError
from ...i18n import _
from .. import auth, proxy, totp
from ..auth import LoginRateLimiter
from ..starlette_compat import Route
from .common import Ctx, csrf_failed, local_path, read_form
from .settings import _Flash

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import RedirectResponse, Response
    from starlette.routing import BaseRoute

    from ...config import User

PAGE = "/settings/totp"          # Settings → Two-factor sign-in
PENDING_COOKIE = "ser2net_2fa"   # its name for a request: auth.set_cookie (proxy.cookie_name)
PENDING_PATH = "/auth/totp"      # also its Path (under the public URL's path)
PENDING_TTL_S = 300          # from the password to the code
ENROL_TTL_S = 1800           # from showing a new key to confirming it with a code
CODE_ATTEMPTS = 5            # wrong codes for one account ...
CODE_WINDOW_S = 300          # ... in this many seconds, then it must wait
# the code forms are a few short fields (and no file: read_form)
FORM_MAX_BYTES = 64 * 1024
FORM_LIMITS = {"max_fields": 8, "max_part_size": 16 * 1024}
NO_STORE = {"Cache-Control": "no-store"}  # a page with a key or a code step


def challenge(ctx: Ctx, request: Request, user: User, nxt: str = "") -> RedirectResponse:
    """`user`'s password was right and their account takes a code too: no session yet,
    the pending cookie for the code step instead (/auth/totp). `nxt`: the page the
    sign-in goes on to (/login?next=, a path without the base path), carried in the
    cookie ("x") to the code step."""
    state = ctx.state
    resp = proxy.redirect(request, PENDING_PATH)
    payload = {"p": "totp", "u": user.username, "v": user.pwd_version}
    if nxt := local_path(nxt, ""):
        payload["x"] = nxt
    auth.set_cookie(resp, request, state.config, PENDING_COOKIE,
                    auth.sign_payload(state.config.secret_key, payload,
                                      ttl_seconds=PENDING_TTL_S),
                    max_age=PENDING_TTL_S, path=PENDING_PATH)
    state.log(f"login: password of {user.username} accepted from {ctx.client_ip(request)}; "
              "waiting for the two-factor code")
    return resp


def routes(ctx: Ctx) -> list[BaseRoute]:
    state, render, client_ip = ctx.state, ctx.render, ctx.client_ip
    current_user, set_session = ctx.current_user, ctx.set_session
    code_limiter = LoginRateLimiter(CODE_ATTEMPTS, CODE_WINDOW_S)   # per account
    # what turning it on or off said, for the page its redirect leads to (as Settings does:
    # Post/Redirect/Get), by the browser's CSRF token and the account
    flash = _Flash()
    last_step: dict[str, int] = {}   # account -> the time step of its last accepted code

    def _check(user: User, code: object) -> bool:
        """Is `code` a valid code of `user`'s secret now, and not one used before? Then it
        is used up."""
        step = totp.verify(user.totp_secret, code, last_step.get(user.username, -1))
        if step is None:
            return False
        last_step[user.username] = step
        return True

    # ---------------- the code step of a sign-in ----------------
    def _pending(request: Request) -> dict | None:
        data = auth.read_payload(state.config.secret_key,
                                 auth.cookie(request, PENDING_COOKIE, PENDING_PATH, legacy=False))
        return data if data and data.get("p") == "totp" else None

    def _pending_next(request: Request) -> str:
        """Where the sign-in goes on to once the code is right (challenge's `nxt`); "" for
        the dashboard (also a cookie from before it was carried)."""
        data = _pending(request)
        return local_path(data.get("x"), "") if data else ""

    def _pending_user(request: Request) -> User | None:
        """The account whose password the pending cookie vouches for, while it still takes
        a code: same password version (no password change or sign-out since) and two-factor
        sign-in still on. None otherwise."""
        data = _pending(request)
        if not data or not isinstance(data.get("u"), str):
            return None
        user = state.config.get_user(data["u"])
        if user is None or not user.totp_on or user.pwd_version != data.get("v"):
            return None
        return user

    def _code_page(request: Request, user: User, status_code: int = 200,
                   error: str | None = None) -> Response:
        return render(request, "login_totp.html", status_code=status_code, error=error,
                      username=user.username, headers=NO_STORE)

    def _start_again(request: Request, status_code: int, error: str) -> Response:
        resp = render(request, "login.html", status_code=status_code, error=error,
                      oidc_enabled=state.config.oidc.enabled, next=_pending_next(request))
        auth.delete_cookie(resp, request, state.config, PENDING_COOKIE, PENDING_PATH)
        return resp

    async def code_get(request: Request) -> Response:
        user = _pending_user(request)
        if user is None:
            return proxy.redirect(request, "/login")
        return _code_page(request, user)

    async def code_post(request: Request) -> Response:
        form = await read_form(request, **FORM_LIMITS)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        ip = client_ip(request)
        user = _pending_user(request)
        if user is None:
            return _start_again(request, 401, _("The sign-in took too long or was ended. "
                                                "Sign in again."))
        if state.rate_limiter.blocked(ip):  # wrong passwords and codes from this address
            return _start_again(request, 429, _("Too many attempts. Wait a few minutes and "
                                                "try again."))
        if code_limiter.blocked(user.username):
            return _start_again(request, 429, _("Too many wrong codes. Wait a few minutes "
                                                "and sign in again."))
        if not _check(user, form.get("code")):
            state.rate_limiter.record_failure(ip)
            code_limiter.record_failure(user.username)
            state.log(f"failed two-factor code for {user.username!r} from {ip}")
            state.failed_logins.add(ip, user.username, "login_failed", "two-factor code")
            state.notifier.emit("login_failed", ip, "", notify.sign_in_detail(
                state.config.notifications, user.username, ip, "", "wrong two-factor code"))
            return _code_page(request, user, 401, _(
                "That code is not right. Enter the code your app shows now; if it never "
                "works, the clock of this server or of your device is off."))
        state.rate_limiter.reset(ip)
        state.name_limiter.reset(user.username)  # as a sign-in with the password alone
        code_limiter.reset(user.username)
        state.log(f"login: {user.username} ({user.role}, local, two-factor) from {ip}")
        ctx.audit(request, "login", f"{user.role}, local, two-factor", user=user.username)
        resp = proxy.redirect(request, _pending_next(request) or "/")
        set_session(resp, request, user)
        ctx.new_csrf_token(request)
        auth.delete_cookie(resp, request, state.config, PENDING_COOKIE, PENDING_PATH)
        return resp

    # ---------------- setting it up / turning it off (Settings) ----------------
    def _browser(request: Request) -> str:
        return getattr(request.state, "csrf_token", None) or ""

    def _done(request: Request, me: User, ok: str) -> RedirectResponse:
        """A form that did its work: back to the page (a reload doesn't post it again, the
        TR / EN switch has a page to go back to), which says `ok`; the session renewed
        (it ended the account's other sessions)."""
        flash.put(_browser(request), me.username, ok=ok)
        resp = proxy.redirect(request, PAGE)
        set_session(resp, request, me)
        return resp

    def _page(request: Request, status_code: int = 200, ok: str | None = None,
              error: str | None = None, secret: str | None = None) -> Response:
        """Settings → Two-factor sign-in: a new key to set up (or `secret`, the one being
        set up), or, while it is on, the form to turn it off. A page answered to a form
        (refused) names the page's own address (page_url)."""
        me = current_user(request)
        if request.method == "GET" and me is not None and (
                done := flash.take(_browser(request), me.username)):
            ok = done["ok"]
        refusal = None
        if me is None or me.source == "anonymous":
            refusal = _("Sign in to set up two-factor sign-in.")
        elif me.source != "local":
            refusal = _("Your account signs in with {0}: a second factor for it is set up "
                        "there (in the directory or the identity provider).").format(
                            me.source.upper())
        ctx_vars: dict = {"ok": ok, "error": error, "refusal": refusal, "me": me,
                          "enabled": bool(me is not None and me.totp_on)}
        if me is not None and refusal is None and not ctx_vars["enabled"]:
            secret = secret or totp.new_secret()
            ctx_vars.update(secret_grouped=totp.grouped(secret),
                            uri=totp.otpauth_uri(secret, me.username),
                            enrol=auth.sign_payload(state.config.secret_key,
                                                    {"p": "totp-enrol", "u": me.username,
                                                     "s": secret}, ttl_seconds=ENROL_TTL_S))
        return render(request, "totp.html", status_code=status_code, headers=NO_STORE,
                      page_url=None if request.method == "GET" else PAGE, **ctx_vars)

    async def settings_page(request: Request) -> Response:
        return _page(request)

    async def _password_ok(me: User, password: object) -> bool:
        # (scrypt: with the sign-ins' limit on such work at a time, AppState.password_work)
        return await state.password_work.run(auth.verify_password, str(password or ""),
                                             me.password_hash)

    async def _store(request: Request, me: User, secret: str) -> Response | None:
        """Save `secret` ("" = off) for the signed-in account and end its other sessions
        (pwd_version + 1); None when saved, else the page with the error."""
        try:
            async with state.edit_config():
                user = state.config.get_user(me.username)
                if user is None or user is not me:  # deleted or replaced meanwhile
                    return _page(request, 409, error=_("Your account changed meanwhile. Sign "
                                                       "in again."))
                user.totp_secret, user.pwd_version = secret, user.pwd_version + 1
        except ConfigError as e:  # not saved: nothing changed
            return _page(request, 400, error=str(e))
        return None

    async def enable_post(request: Request) -> Response:
        form = await read_form(request, **FORM_LIMITS)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        me = current_user(request)
        if me is None or me.source != "local":
            return _page(request, 400)  # the refusal
        if me.totp_on:
            return proxy.redirect(request, PAGE)  # on already: the page says so
        data = auth.read_payload(state.config.secret_key, form.get("enrol"))
        if (not data or data.get("p") != "totp-enrol" or data.get("u") != me.username
                or not totp.is_secret(data.get("s"))):
            return _page(request, 400, error=_("This setup has expired. Add the new key below "
                                               "to your app and try again."))
        secret = data["s"]
        if not await _password_ok(me, form.get("password")):
            return _page(request, 400, error=_("Current password is incorrect."), secret=secret)
        step = totp.verify(secret, form.get("code"))
        if step is None:
            return _page(request, 400, secret=secret, error=_(
                "That code is not right. Enter the code your app shows now for this key; if "
                "it never works, the clock of this server or of your device is off."))
        if failed := await _store(request, me, secret):
            return failed
        last_step[me.username] = step
        state.log(f"two-factor sign-in turned on for {me.username}; their other sessions "
                  "were signed out")
        ctx.audit(request, "totp_enable", me.username)
        return _done(request, me, _("Two-factor sign-in is on: signing in now takes a code from "
                                    "your app too. Your other sessions were signed out."))

    async def disable_post(request: Request) -> Response:
        form = await read_form(request, **FORM_LIMITS)
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return csrf_failed()
        me = current_user(request)
        if me is None or me.source != "local":
            return _page(request, 400)  # the refusal
        if not me.totp_on:
            return proxy.redirect(request, PAGE)  # off already: the page says so
        if not await _password_ok(me, form.get("password")):
            return _page(request, 400, error=_("Current password is incorrect."))
        if code_limiter.blocked(me.username):
            return _page(request, 429, error=_("Too many wrong codes. Wait a few minutes and "
                                               "try again."))
        if not _check(me, form.get("code")):
            code_limiter.record_failure(me.username)
            return _page(request, 400, error=_("That code is not right. Enter the code your "
                                               "app shows now."))
        if failed := await _store(request, me, ""):
            return failed
        state.log(f"two-factor sign-in turned off for {me.username}; their other sessions "
                  "were signed out")
        ctx.audit(request, "totp_disable", me.username)
        return _done(request, me, _("Two-factor sign-in is off: your password alone signs you "
                                    "in. Your other sessions were signed out."))

    return [
        Route(PENDING_PATH, code_get, methods=["GET"]),
        Route(PENDING_PATH, code_post, methods=["POST"], max_body_size=FORM_MAX_BYTES),
        Route(PAGE, settings_page, methods=["GET"]),
        Route("/settings/totp/enable", enable_post, methods=["POST"],
              max_body_size=FORM_MAX_BYTES),
        Route("/settings/totp/disable", disable_post, methods=["POST"],
              max_body_size=FORM_MAX_BYTES),
    ]
