"""Optional OpenID Connect (OIDC) single sign-on — authorization-code flow.

Endpoints are discovered from the issuer's /.well-known/openid-configuration. The
returned id_token is validated — signature via the issuer's JWKS, plus iss / aud /
exp / nonce, its times with CLOCK_SKEW_S of tolerance — using authlib.jose (do NOT
hand-roll JWT validation). A claim (groups_claim) maps to a ser2net role, reusing the
LDAP group→role logic (whole group names only: ldap_auth.group_matches).

The code request carries PKCE (S256; the verifier travels in the signed state cookie).
Every address the discovery document names, and every redirect on the way, must be
https:// — http:// only to this machine's loopback address (a provider on the same host),
where nothing crosses a network — so the client secret, the code and the signing keys
never travel in clear text (InsecureEndpoint).

authlib is an OPTIONAL dependency (lazy import): absent or failing to import => OIDC
login is disabled with a log line saying which (app.optional), and local accounts keep
working. HTTP uses stdlib urllib; the fetch/post calls are
injectable so the full flow can be tested without a real provider.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import optional
from ..config import external_name
from .ldap_auth import role_for_groups  # shared group/claim -> role mapping

# issuer -> (its discovery document, time.monotonic() when it was fetched)
_DISCOVERY_CACHE: dict[str, tuple[dict, float]] = {}
_DISCOVERY_TTL = 3600.0
# How far this host's clock may be off the identity provider's when an ID token's times
# (iat, nbf, exp) are checked: two minutes, as authlib's own OIDC client allows. Without
# it, a host a second behind a provider that stamps iat with the current second (such as
# Keycloak) has its sign-ins refused as "issued in the future".
CLOCK_SKEW_S = 120
_ISSUER_WARNED: set[tuple[str, str]] = set()   # (configured, discovered): logged once


class InsecureEndpoint(ValueError):
    """An address from the provider is neither https:// nor loopback http://; the text
    says which, for the log."""


class TokenEndpointError(Exception):
    """The token endpoint refused the code (an HTTP error answer); the text is for the log."""


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


CALLBACK_PATH = "/auth/oidc/callback"


def redirect_uri(settings, request, public_url: str = "") -> str:
    """The redirect URI a sign-in sends to the identity provider, which must have it
    registered: oidc.redirect_uri when set; else, with a public URL (Settings -> Reverse
    proxy), that URL and CALLBACK_PATH; else made from the request as it has been since
    v2.3 - X-Forwarded-Proto or the request's scheme, X-Forwarded-Host or Host, and
    CALLBACK_PATH. Forwarded and X-Forwarded-Port are not read: a working install with a
    blank redirect_uri has this value registered, and reading them would change it. Of a
    list that proxies in a row made (a, b), the first value counts: the one the browser
    used (the whole list made no usable address). Settings shows the value for its own
    request (redirect_uri_is_plain_http)."""
    configured = settings.redirect_uri.strip()
    if configured:
        return configured
    if public_url:
        return public_url + CALLBACK_PATH
    scheme = _first(request.headers.get("x-forwarded-proto")) or request.url.scheme
    host = (_first(request.headers.get("x-forwarded-host")) or request.headers.get("host")
            or request.url.netloc)
    return f"{scheme}://{host}{CALLBACK_PATH}"


def _first(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def redirect_uri_is_plain_http(uri: str) -> bool:
    """`uri` is an http:// address of another host than this machine's loopback: most
    identity providers (Entra ID, Google, Okta) refuse to register such a redirect URI."""
    try:
        parts = urllib.parse.urlsplit(uri)
        host = parts.hostname or ""
    except ValueError:
        return False
    return parts.scheme.lower() == "http" and not _loopback(host)


def secure_url(url, what: str) -> str:
    """`url` if it is https://, or http:// to a loopback address (127.0.0.0/8, ::1,
    localhost: nothing leaves this machine — a provider on the same host). Raises
    InsecureEndpoint for anything else, such as the http:// addresses a Keycloak behind a
    TLS-terminating proxy advertises when its proxy settings are missing."""
    try:
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        host = parts.hostname
    except ValueError:   # such as an unclosed [IPv6 address
        host = None
    if host and (parts.scheme == "https" or (parts.scheme == "http" and _loopback(host))):
        return url
    raise InsecureEndpoint(
        f"the identity provider's {what} {url!r} is not an https:// address, so OIDC sign-in "
        "is refused (the client secret, the code or the signing keys would go in clear "
        "text). Have the provider announce https:// addresses (behind a TLS proxy: its "
        "proxy / frontend URL settings)")


class _SecureRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an address secure_url() allows."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        secure_url(newurl, "redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    """A new one for each request: urllib's HTTPS handler loads the CA certificates when
    it is made (Python 3.12+), and the standalone Linux binary points OpenSSL at the
    system's CA bundle (app/standalone.py: SSL_CERT_FILE) only after this module is
    imported. One made at import would trust no provider there."""
    return urllib.request.build_opener(_SecureRedirects)


def _get_json(url: str, timeout: float = 8):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _opener().open(req, timeout=timeout) as r:  # noqa: S310 (secure_url checked it)
        return json.loads(r.read())


def _post_form(url: str, data: dict, timeout: float = 8):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with _opener().open(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read())
    except urllib.error.HTTPError as e:   # an OAuth error answer: say what it was
        raise TokenEndpointError(f"the token endpoint refused the code: HTTP {e.code} "
                                 f"{_error_text(e.read(4096))}") from None


def _error_text(body: bytes) -> str:
    """An OAuth error answer (RFC 6749 5.2) for a log line: error (error_description)."""
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("error"):
        text = repr(str(doc["error"])[:64])
        if doc.get("error_description"):
            text += f" ({str(doc['error_description'])[:200]!r})"
        return text
    return repr(body[:200].decode("utf-8", "replace"))


def discover(issuer: str, fetch=_get_json) -> dict:
    issuer = issuer.rstrip("/")
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached and (time.monotonic() - cached[1] < _DISCOVERY_TTL):
        return cached[0]
    doc = fetch(secure_url(issuer + "/.well-known/openid-configuration", "discovery document"))
    if not isinstance(doc, dict):
        raise ValueError("the discovery document is not a JSON object")
    _DISCOVERY_CACHE[issuer] = (doc, time.monotonic())
    return doc


def new_code_verifier() -> str:
    """A PKCE code_verifier (RFC 7636: 43-128 unreserved characters; this is 64)."""
    return secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    """The S256 code_challenge of a code_verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_authorize_url(settings, redirect_uri: str, state: str, nonce: str, fetch=_get_json,
                        code_verifier: str = "") -> str:
    doc = discover(settings.issuer, fetch=fetch)
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.scopes or "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    if code_verifier:
        params.update(code_challenge=code_challenge(code_verifier), code_challenge_method="S256")
    endpoint = secure_url(doc.get("authorization_endpoint"), "authorization endpoint")
    return endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(params)


def _expected_issuer(settings, doc: dict, log) -> str:
    """The iss the ID token must carry: the discovery document's issuer. One that is not
    the configured issuer URL is logged once (reaching a provider through another URL than
    its public one is common, so it is no reason to refuse)."""
    configured = settings.issuer.rstrip("/")
    issuer = str(doc.get("issuer") or configured)
    if issuer.rstrip("/") != configured and (configured, issuer) not in _ISSUER_WARNED:
        _ISSUER_WARNED.add((configured, issuer))
        log(f"WARNING: OIDC: the discovery document of {configured!r} names the issuer "
            f"{issuer!r}; ID tokens must come from {issuer!r}. If that is not the same "
            f"provider under another address, correct the issuer URL in Settings → Single "
            f"sign-on (OIDC)")
    return issuer


def complete(settings, redirect_uri: str, code: str, nonce: str, logger=None,
             fetch=_get_json, post=_post_form, code_verifier: str = "") -> dict | None:
    """Exchange the auth code for tokens and validate the id_token. Returns the
    verified claims, or None on any failure (never raises). code_verifier: the PKCE
    verifier of this sign-in (none for a state cookie from before PKCE was sent)."""
    log = logger or (lambda _m: None)
    try:
        from authlib.jose import JsonWebKey, jwt
        from authlib.jose.errors import ExpiredTokenError, InvalidTokenError
    except Exception as e:  # not only ImportError (e.g. an authlib without authlib.jose)
        log(f"OIDC sign-in disabled: {optional.problem(e, 'authlib')}")
        return None
    try:
        doc = discover(settings.issuer, fetch=fetch)
        request = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": settings.client_id, "client_secret": settings.client_secret,
        }
        if code_verifier:
            request["code_verifier"] = code_verifier
        token = post(secure_url(doc.get("token_endpoint"), "token endpoint"), request)
        id_token = token.get("id_token")
        if not id_token:
            log("OIDC: token response had no id_token")
            return None
        jwks = JsonWebKey.import_key_set(fetch(secure_url(doc.get("jwks_uri"), "jwks_uri")))
        claims = jwt.decode(id_token, jwks, claims_options={   # checks the signature
            "iss": {"essential": True, "value": _expected_issuer(settings, doc, log)},
            "aud": {"essential": True, "value": settings.client_id},
            "exp": {"essential": True},        # OIDC Core: an ID token always expires
        })
        try:
            claims.validate(leeway=CLOCK_SKEW_S)   # exp / iat / nbf
        except (ExpiredTokenError, InvalidTokenError) as e:
            log(f"OIDC: the ID token was refused ({e}): {_clock_hint(claims)}")
            return None
        if nonce and claims.get("nonce") != nonce:
            log("OIDC: nonce mismatch (possible replay)")
            return None
        return dict(claims)
    except (InsecureEndpoint, TokenEndpointError) as e:
        log(f"OIDC sign-in failed: {e}")
        return None
    except Exception as e:  # discovery/token/JWKS/JWT failure -> denied
        log(f"OIDC authentication failed: {type(e).__name__}: {e}")
        return None


def _clock_hint(claims) -> str:
    """How far this host's clock seems off the provider's, for a token refused for its
    times. The provider issued it (iat, under a signature already checked) a moment ago
    by its own clock, so now - iat is about the difference between the two clocks."""
    iat = claims.get("iat")
    if isinstance(iat, bool) or not isinstance(iat, (int, float)):
        return "check that this server's clock is right (time synchronisation, NTP)"
    offset = round(time.time() - iat)
    where = f"{-offset} s behind" if offset < 0 else f"{offset} s ahead of"
    return (f"this server's clock is about {where} the identity provider's ({CLOCK_SKEW_S} s "
            f"are allowed): check the time synchronisation (NTP) of this server and of the "
            f"provider")


def _says_false(value) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def _username(claims: dict, settings) -> tuple[str, str]:
    """(the user's name, "") or ("", why the ID token gives none)."""
    for key in (settings.username_claim, "preferred_username", "email", "sub"):
        value = claims.get(key)
        if value:
            if key == "email" and _says_false(claims.get("email_verified")):
                # no fall-back to sub either: that would be another account
                return "", (f"the ID token names the user by the e-mail address {str(value)!r}, "
                            f"which the provider has not verified (email_verified: false)")
            return external_name(value), ""
    return "", "the ID token names no user"


def username_from_claims(claims: dict, settings) -> str:
    """The user's name: the username_claim, else preferred_username, email or sub,
    spelled as LDAP/OIDC account names are kept (config.external_name). An e-mail address
    the provider says it has not verified (email_verified: false; a missing claim is
    fine — Entra ID rarely sends it) names nobody: ""."""
    return _username(claims, settings)[0]


def no_username_reason(claims: dict, settings) -> str:
    """Why username_from_claims() gave "", for the log."""
    return _username(claims, settings)[1] or "the ID token names no user"


def groups_from_claims(claims: dict, settings) -> list[str]:
    """The values of the groups claim (a list, or one value)."""
    groups = claims.get(settings.groups_claim) or []
    if not isinstance(groups, (list, tuple)):
        groups = [groups]
    return [str(g) for g in groups]


def role_from_claims(claims: dict, settings):
    return role_for_groups(groups_from_claims(claims, settings), settings)
