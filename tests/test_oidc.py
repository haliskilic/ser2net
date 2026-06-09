"""OIDC single sign-on (Phase 2.3).

Pure parts (claim->role/username, the signed state cookie) always run in CI. The
full token-exchange + id_token validation flow uses a real RS256-signed token and
mocked discovery/token/JWKS HTTP, so it runs without a provider; it's skipped only
if authlib isn't importable. Run: python3 tests/test_oidc.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import OidcSettings
from app.web import auth, oidc_auth

ISSUER = "https://idp.example.com"
CLIENT_ID = "ser2net"


def _settings():
    return OidcSettings(enabled=True, issuer=ISSUER, client_id=CLIENT_ID, client_secret="sek",
                        username_claim="preferred_username", groups_claim="groups",
                        admin_group="ser2net-admins", operator_group="ser2net-operators",
                        viewer_group="ser2net-viewers", default_role="")


def test_claim_mapping():
    s = _settings()
    assert oidc_auth.role_from_claims({"groups": ["ser2net-operators"]}, s) == "operator"
    assert oidc_auth.role_from_claims({"groups": "ser2net-admins"}, s) == "admin"   # string ok
    assert oidc_auth.role_from_claims({"groups": ["other"]}, s) is None             # deny
    assert oidc_auth.username_from_claims({"preferred_username": "alice"}, s) == "alice"
    assert oidc_auth.username_from_claims({"email": "a@b.c"}, s) == "a@b.c"          # falls back
    assert oidc_auth.username_from_claims({"sub": "xyz"}, s) == "xyz"
    print("OIDC claim -> role/username mapping (+ fallbacks, deny)  OK")


def test_signed_state_cookie():
    secret = "s" * 64
    tok = auth.sign_payload(secret, {"s": "abc", "n": "xyz", "r": "https://h/cb"}, ttl_seconds=600)
    data = auth.read_payload(secret, tok)
    assert data and data["s"] == "abc" and data["n"] == "xyz" and data["r"] == "https://h/cb"
    assert auth.read_payload("other-secret", tok) is None          # tamper/wrong key
    assert auth.read_payload(secret, tok[:-3] + "AAA") is None      # bad signature
    expired = auth.sign_payload(secret, {"s": "x"}, ttl_seconds=-1)
    assert auth.read_payload(secret, expired) is None              # expired
    print("OIDC signed state cookie: round-trips, rejects tamper/expiry  OK")


def test_complete_flow():
    try:
        from authlib.jose import JsonWebKey, jwt
    except ImportError:
        print("skip: authlib not installed (pure logic above already covered)")
        return

    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    jwks = {"keys": [key.as_dict()]}                       # private dict still verifies (has public parts)
    disco = {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/auth",
             "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"}

    def make_token(aud=CLIENT_ID, nonce="N1", exp_delta=300, iss=ISSUER):
        payload = {"iss": iss, "aud": aud, "sub": "u1", "preferred_username": "alice",
                   "groups": ["ser2net-operators"], "nonce": nonce,
                   "iat": int(time.time()), "exp": int(time.time()) + exp_delta}
        return jwt.encode({"alg": "RS256"}, payload, key).decode()

    def fetch_for(token):
        def fetch(url):
            return jwks if url.endswith("/jwks") else disco
        return fetch

    def post_for(token):
        def post(url, data):
            assert data["grant_type"] == "authorization_code" and data["code"] == "CODE"
            return {"id_token": token, "token_type": "Bearer"}
        return post

    s = _settings()
    oidc_auth._DISCOVERY_CACHE.clear()

    # happy path
    tok = make_token()
    claims = oidc_auth.complete(s, "https://h/cb", "CODE", "N1", fetch=fetch_for(tok), post=post_for(tok))
    assert claims and claims["preferred_username"] == "alice"
    assert oidc_auth.role_from_claims(claims, s) == "operator"
    print("OIDC complete(): valid id_token -> claims; groups -> operator  OK")

    # nonce mismatch -> rejected
    assert oidc_auth.complete(s, "https://h/cb", "CODE", "WRONG", fetch=fetch_for(tok), post=post_for(tok)) is None
    # wrong audience -> rejected
    bad_aud = make_token(aud="someone-else")
    assert oidc_auth.complete(s, "https://h/cb", "CODE", "N1", fetch=fetch_for(bad_aud), post=post_for(bad_aud)) is None
    # expired -> rejected
    expired = make_token(exp_delta=-10)
    assert oidc_auth.complete(s, "https://h/cb", "CODE", "N1", fetch=fetch_for(expired), post=post_for(expired)) is None
    print("OIDC complete(): rejects bad nonce / audience / expiry  OK")


def main():
    test_claim_mapping()
    test_signed_state_cookie()
    test_complete_flow()
    print("\nPASS: OIDC single sign-on")


if __name__ == "__main__":
    main()
