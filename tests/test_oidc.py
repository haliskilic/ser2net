"""OIDC single sign-on (Phase 2.3).

Pure parts (claim->role/username, the signed state cookie) always run in CI: a group
setting matches a whole claim value in any letter case (Turkish İ/I/ı/i as one
letter), or the last part of a Keycloak path / DOMAIN\\name — never a part of a name.
An e-mail address the provider says is not verified names nobody (no fall-back to sub).
Discovery and the authorize URL (with PKCE S256) need no package either; every address
the provider gives, and every redirect, must be https:// (http:// only to loopback). The
full token-exchange + id_token validation flow uses a real RS256-signed token and mocked
discovery/token/JWKS HTTP, so it runs without a provider; it needs authlib and
cryptography: without them it is skipped with a SKIP line (tests/run_all.py repeats it),
and with SER2NET_OPTIONAL_TESTS=1 the test fails instead. Run: python3 tests/test_oidc.py
"""
import base64
import hashlib
import http.server
import os
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _names import OTHER_GROUP, SAME_GROUP
from _optional import need

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
    # one spelling: NFC, no white space around it
    assert oidc_auth.username_from_claims({"preferred_username": " Jose\u0301 "}, s) == "Jos\u00e9"
    # an e-mail address the provider has not verified names nobody — and sub doesn't stand in
    for flag in (False, "false", "False"):
        claims = {"email": "a@b.c", "email_verified": flag, "sub": "xyz"}
        assert oidc_auth.username_from_claims(claims, s) == "", flag
        assert "'a@b.c', which the provider has not verified" in oidc_auth.no_username_reason(
            claims, s)
    for flag in (True, "true", None):   # Entra ID often sends no email_verified at all
        assert oidc_auth.username_from_claims({"email": "a@b.c", "email_verified": flag}, s) \
            == "a@b.c"
    assert oidc_auth.username_from_claims({"preferred_username": "alice", "email": "a@b.c",
                                           "email_verified": False}, s) == "alice"
    by_mail = OidcSettings(username_claim="email")
    assert oidc_auth.username_from_claims({"email": "a@b.c", "email_verified": False,
                                           "preferred_username": "alice"}, by_mail) == ""
    assert oidc_auth.no_username_reason({}, s) == "the ID token names no user"
    print("OIDC claim -> role/username mapping (+ fallbacks, deny)  OK")


def test_claim_whole_names():
    s = OidcSettings(admin_group="admins", operator_group="ser2net-operators", default_role="")
    for value in ("sysadmins", "admins-readonly", "/team/not-admins", "ser2net-admins",
                  "/admins/printers", "CORP\\domain admins"):
        assert oidc_auth.role_from_claims({"groups": [value]}, s) is None, value
    # the forms providers send: the name, a Keycloak path, DOMAIN\\name
    for value in ("admins", "ADMINS", "/admins", "/plant/team/admins", "CORP\\Admins"):
        assert oidc_auth.role_from_claims({"groups": [value]}, s) == "admin", value
    assert oidc_auth.role_from_claims({"groups": ["/x/ser2net-operators", "sysadmins"]}, s) \
        == "operator"
    s.default_role = "viewer"
    assert oidc_auth.role_from_claims({"groups": ["sysadmins"]}, s) == "viewer"
    # a path setting: its trailing segments; one that starts with / is the whole path
    path = OidcSettings(admin_group="team/ser2net-admins", operator_group="/plant/ser2net-ops")
    assert oidc_auth.role_from_claims({"groups": ["/org/team/ser2net-admins"]}, path) == "admin"
    assert oidc_auth.role_from_claims({"groups": ["/org/other/ser2net-admins"]}, path) is None
    assert oidc_auth.role_from_claims({"groups": ["/plant/ser2net-ops"]}, path) == "operator"
    assert oidc_auth.role_from_claims({"groups": ["plant/ser2net-ops"]}, path) == "operator"
    assert oidc_auth.role_from_claims({"groups": ["/site/plant/ser2net-ops"]}, path) is None
    # a claim that is one value, or not a string at all
    assert oidc_auth.groups_from_claims({"groups": 42}, s) == ["42"]
    assert oidc_auth.groups_from_claims({}, s) == []
    print("OIDC groups: whole names, Keycloak paths and DOMAIN\\name; no part of a name  OK")


def test_claim_turkish_names():
    for typed, stored in SAME_GROUP:
        s = OidcSettings(operator_group=typed)
        assert oidc_auth.role_from_claims({"groups": [stored]}, s) == "operator", (typed, stored)
        assert oidc_auth.role_from_claims({"groups": f"/Fabrika/{stored}"}, s) == "operator"
    for typed, stored in OTHER_GROUP:
        s = OidcSettings(operator_group=typed)
        assert oidc_auth.role_from_claims({"groups": [stored]}, s) is None, (typed, stored)
    print("OIDC groups: Turkish İ/I/ı/i, NFD and no-break spaces compare as typed  OK")


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


def test_discovery_and_authorize_url():
    """The sign-in redirect: endpoints from the issuer's discovery document (cached), the
    authorization-code request with state and nonce. Needs no authlib."""
    fetched = []
    disco = {"issuer": ISSUER, "authorization_endpoint": ISSUER + "/auth",
             "token_endpoint": ISSUER + "/token", "jwks_uri": ISSUER + "/jwks"}

    def fetch(url):
        fetched.append(url)
        return disco

    oidc_auth._DISCOVERY_CACHE.clear()
    s = _settings()
    s.issuer = ISSUER + "/"          # a trailing slash is not part of the discovery URL
    url = oidc_auth.build_authorize_url(s, "https://h/auth/oidc/callback", "ST", "NO", fetch=fetch)
    assert fetched == [ISSUER + "/.well-known/openid-configuration"], fetched
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith(ISSUER + "/auth?"), url
    assert params == {"response_type": ["code"], "client_id": [CLIENT_ID],
                      "redirect_uri": ["https://h/auth/oidc/callback"],
                      "scope": ["openid email profile"], "state": ["ST"], "nonce": ["NO"]}, params
    # the discovery document is cached: a second sign-in fetches nothing
    oidc_auth.build_authorize_url(s, "https://h/cb", "S2", "N2", fetch=fetch)
    assert len(fetched) == 1, fetched
    assert oidc_auth.discover(ISSUER, fetch=fetch) is disco
    # PKCE: the S256 challenge of the verifier (RFC 7636 appendix B's example)
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert oidc_auth.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    url = oidc_auth.build_authorize_url(s, "https://h/cb", "S3", "N3", fetch=fetch,
                                        code_verifier=verifier)
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert params["code_challenge"] == ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"]
    assert params["code_challenge_method"] == ["S256"] and "code_verifier" not in params
    v = oidc_auth.new_code_verifier()
    assert 43 <= len(v) <= 128 and set(v) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"), v
    assert oidc_auth.code_challenge(v) == base64.urlsafe_b64encode(
        hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    oidc_auth._DISCOVERY_CACHE.clear()
    print("OIDC: discovery (cached) and the authorization-code redirect URL with PKCE S256  OK")


def test_https_only():
    """An address from the provider that is not https:// (http:// only to this machine's
    loopback address) stops the sign-in before anything goes to it."""
    for ok in ("https://idp.example.com/auth", "http://127.0.0.1:8080/realms/x/token",
               "http://[::1]/jwks", "http://localhost:8443/x", "http://127.9.9.9/x"):
        assert oidc_auth.secure_url(ok, "x") == ok, ok
    for bad in ("http://idp.example.com/token", "http://10.0.0.5/jwks", "ftp://idp/x",
                "https:///nohost", "", None, 42, "//idp.example.com/x"):
        try:
            oidc_auth.secure_url(bad, "token endpoint")
        except oidc_auth.InsecureEndpoint as e:
            assert "is not an https:// address" in str(e), e
        else:
            raise AssertionError(f"accepted {bad!r}")
    oidc_auth._DISCOVERY_CACHE.clear()
    disco = {"issuer": ISSUER, "authorization_endpoint": "http://idp.example.com/auth"}
    try:
        oidc_auth.build_authorize_url(_settings(), "https://h/cb", "S", "N", fetch=lambda u: disco)
    except oidc_auth.InsecureEndpoint as e:
        assert "authorization endpoint 'http://idp.example.com/auth'" in str(e), e
    else:
        raise AssertionError("an http:// authorization endpoint was used")
    oidc_auth._DISCOVERY_CACHE.clear()

    # a redirect to plain http is not followed (the loopback server itself may be http)
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            self.send_response(302)
            self.send_header("Location", "http://idp.example.com/.well-known/x")
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        oidc_auth._get_json(f"http://127.0.0.1:{srv.server_address[1]}/x", timeout=5)
    except oidc_auth.InsecureEndpoint as e:
        assert "redirect 'http://idp.example.com/.well-known/x'" in str(e), e
    else:
        raise AssertionError("followed a redirect to http://")
    finally:
        srv.shutdown()
        srv.server_close()
    print("OIDC: provider addresses and redirects must be https:// (or loopback http://)  OK")


def test_complete_flow():
    from authlib.jose import JsonWebKey, jwt

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

    sent = []

    def post_for(token):
        def post(url, data):
            assert data["grant_type"] == "authorization_code" and data["code"] == "CODE"
            sent.append(data)
            return {"id_token": token, "token_type": "Bearer"}
        return post

    s = _settings()
    oidc_auth._DISCOVERY_CACHE.clear()

    # happy path
    tok = make_token()
    claims = oidc_auth.complete(s, "https://h/cb", "CODE", "N1", fetch=fetch_for(tok), post=post_for(tok))
    assert claims and claims["preferred_username"] == "alice"
    assert oidc_auth.role_from_claims(claims, s) == "operator"
    assert "code_verifier" not in sent[-1]          # a state cookie from before PKCE
    assert oidc_auth.complete(s, "https://h/cb", "CODE", "N1", fetch=fetch_for(tok),
                              post=post_for(tok), code_verifier="V" * 43)
    assert sent[-1]["code_verifier"] == "V" * 43
    print("OIDC complete(): valid id_token -> claims; groups -> operator; PKCE verifier sent  OK")

    # a token endpoint or JWKS address that is not https:// is never used
    for name in ("token_endpoint", "jwks_uri"):
        oidc_auth._DISCOVERY_CACHE.clear()
        bad = dict(disco, **{name: "http://idp.example.com/" + name})
        logs, posts = [], len(sent)
        assert oidc_auth.complete(s, "https://h/cb", "CODE", "N1", logs.append,
                                  fetch=lambda u, d=bad: jwks if u.endswith("/jwks") else d,
                                  post=post_for(tok)) is None
        assert any("is not an https:// address" in m for m in logs), logs
        assert len(sent) == posts + (name == "jwks_uri"), name   # no code to an http endpoint
    oidc_auth._DISCOVERY_CACHE.clear()
    # a discovery document naming another issuer: logged once; tokens must come from it
    other = dict(disco, issuer="https://sso.example.com")
    logs = []
    for _ in range(2):
        assert oidc_auth.complete(s, "https://h/cb", "CODE", "N1", logs.append,
                                  fetch=lambda u: jwks if u.endswith("/jwks") else other,
                                  post=post_for(tok)) is None
    assert sum("names the issuer 'https://sso.example.com'" in m for m in logs) == 1, logs
    oidc_auth._DISCOVERY_CACHE.clear()
    print("OIDC complete(): http:// token/JWKS addresses refused; another issuer logged  OK")

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
    test_claim_whole_names()
    test_claim_turkish_names()
    test_signed_state_cookie()
    test_discovery_and_authorize_url()
    test_https_only()
    if need("authlib", "cryptography", what="OIDC token exchange and id_token checks "
                                            "(oidc_auth.complete)"):
        test_complete_flow()
    print("\nPASS: OIDC single sign-on")


if __name__ == "__main__":
    main()
