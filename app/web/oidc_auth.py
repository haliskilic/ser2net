"""Optional OpenID Connect (OIDC) single sign-on — authorization-code flow.

Endpoints are discovered from the issuer's /.well-known/openid-configuration. The
returned id_token is validated — signature via the issuer's JWKS, plus iss / aud /
exp / nonce — using authlib.jose (do NOT hand-roll JWT validation). A claim
(groups_claim) maps to a ser2net role, reusing the LDAP group→role logic.

authlib is an OPTIONAL dependency (lazy import): absent => OIDC login is disabled
and local accounts keep working. HTTP uses stdlib urllib; the fetch/post calls are
injectable so the full flow can be tested without a real provider.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .ldap_auth import role_for_groups  # shared group/claim -> role mapping

_DISCOVERY_CACHE: dict[str, tuple[dict, float]] = {}
_DISCOVERY_TTL = 3600.0


def _get_json(url: str, timeout: float = 8):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https issuer URLs)
        return json.loads(r.read())


def _post_form(url: str, data: dict, timeout: float = 8):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read())


def discover(issuer: str, fetch=_get_json) -> dict:
    issuer = issuer.rstrip("/")
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached and (time.time() - cached[1] < _DISCOVERY_TTL):
        return cached[0]
    doc = fetch(issuer + "/.well-known/openid-configuration")
    _DISCOVERY_CACHE[issuer] = (doc, time.time())
    return doc


def build_authorize_url(settings, redirect_uri: str, state: str, nonce: str, fetch=_get_json) -> str:
    doc = discover(settings.issuer, fetch=fetch)
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.scopes or "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    return doc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def complete(settings, redirect_uri: str, code: str, nonce: str, logger=None,
             fetch=_get_json, post=_post_form) -> dict | None:
    """Exchange the auth code for tokens and validate the id_token. Returns the
    verified claims, or None on any failure (never raises)."""
    log = logger or (lambda _m: None)
    try:
        from authlib.jose import JsonWebKey, jwt
    except ImportError:
        log("OIDC enabled but authlib is not installed; SSO disabled (pip install authlib)")
        return None
    try:
        doc = discover(settings.issuer, fetch=fetch)
        token = post(doc["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": settings.client_id, "client_secret": settings.client_secret,
        })
        id_token = token.get("id_token")
        if not id_token:
            log("OIDC: token response had no id_token")
            return None
        jwks = JsonWebKey.import_key_set(fetch(doc["jwks_uri"]))
        claims = jwt.decode(id_token, jwks, claims_options={
            "iss": {"essential": True, "value": doc.get("issuer", settings.issuer.rstrip("/"))},
            "aud": {"essential": True, "value": settings.client_id},
        })
        claims.validate()                      # exp / iat / nbf
        if nonce and claims.get("nonce") != nonce:
            log("OIDC: nonce mismatch (possible replay)")
            return None
        return dict(claims)
    except Exception as e:  # discovery/token/JWKS/JWT failure -> denied
        log(f"OIDC authentication failed: {type(e).__name__}: {e}")
        return None


def username_from_claims(claims: dict, settings) -> str:
    for key in (settings.username_claim, "preferred_username", "email", "sub"):
        value = claims.get(key)
        if value:
            return str(value)
    return ""


def role_from_claims(claims: dict, settings):
    groups = claims.get(settings.groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    return role_for_groups(groups, settings)
