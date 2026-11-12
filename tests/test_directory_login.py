"""Signing in to the web UI with LDAP / Active Directory and with OIDC, end to end: a
ser2net server against a small LDAP server (_ldap_server.py) and a small HTTPS OpenID
provider in this process. The LDAP part needs ldap3, the OIDC part authlib and
cryptography; each is skipped without them (a SKIP line, which tests/run_all.py
repeats), or fails with SER2NET_OPTIONAL_TESTS=1.

  * a group setting that is only part of a group's name ("admins" for ser2net-admins)
    no longer gives that role: the sign-in is refused with the usual message, all.log
    names the setting and the group and says how to fix it (and how to get back in
    with `ser2net --set-password`), and Settings shows it until that section is saved;
  * an existing LDAP/OIDC account whose groups give no role any more is signed out
    everywhere at its next sign-in — once: further refused tries don't save again;
  * an LDAP name in any letter case is one account: earlier versions' accounts for
    other spellings are merged at the next sign-in (their sessions end; a refused
    sign-in ends them too), and the log and audit name the account, not what was typed;
  * the AD direct-bind template {username}@corp.local reads the user's groups; a
    Turkish group name typed in another letter case gives its role; a sign-in that
    gets the default role logs the user's groups;
  * the nested-groups box is saved, and kept when a form without it is posted;
  * the OIDC callback: a missing or altered state is refused (400), a failed token
    exchange too (401, the token endpoint's answer in all.log), with nothing saved; the
    code request carries PKCE (S256), a state cookie from before it still works; the
    provider's error answers are logged and shown by their code (403 / 503 / 400); an
    e-mail address the provider has not verified names nobody; each of these failed
    sign-ins, and one refused for its groups, is audited (oidc_login_failed, with a
    short reason);
  * a directory or provider account named like a local account (letter case aside) is
    refused (403, logged and audited): the local admin keeps its source, role,
    password and sessions; so is a name with invisible characters, an over-long one or
    one in parentheses, while DOMAIN\\name with / and # works.

Run: python3 tests/test_directory_login.py
"""
import datetime
import http.cookiejar
import http.server
import ipaddress
import json
import os
import shutil
import ssl
import tempfile
import threading
import time
import urllib.parse

from _optional import need
from _server import Browser, Server, ui_cookie

from app.web import auth, oidc_auth
from app.web.auth import hash_password

PW = "dir-login-pass-1"
BASE = "DC=corp,DC=local"
ADMINS = "CN=ser2net-admins,OU=Groups," + BASE
TEMPLATE = "{username}@corp.local"   # direct bind with the AD sign-in name Settings suggests


def local_admin():
    return {"username": "admin", "password_hash": hash_password(PW), "role": "admin",
            "pwd_version": 1}


def shadow(name, source, version):
    return {"username": name, "password_hash": "", "role": "admin", "pwd_version": version,
            "source": source}


def session_for(srv, name, version):
    return auth.issue_session(srv.saved_config()["secret_key"], 600, name, version)


def signed_in(srv, token) -> bool:
    """Does a browser with only this session cookie get the settings page?"""
    b = Browser(srv.base)
    b.jar.set_cookie(http.cookiejar.Cookie(
        0, ui_cookie("ser2net_session", srv.port), token, None, False, "127.0.0.1", False, False,
        "/", True, False, None, False, None, None, {}))
    st, headers, _ = b.get("/settings")
    assert st == 200 or (st == 303 and headers.get("Location") == "/login?next=%2Fsettings"), st
    return st == 200


def user_of(srv, name) -> dict:
    return next(u for u in srv.saved_config()["users"] if u["username"] == name)


def all_log(srv) -> str:
    with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
        return fh.read()


def audit_log(srv) -> str:
    try:
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def backups(srv) -> int:
    folder = os.path.join(srv.data, "backups")
    return len(os.listdir(folder)) if os.path.isdir(folder) else 0


# ---------------- LDAP / Active Directory ----------------

def ad_directory():
    from _ldap_server import FakeDirectory
    users = {"jdoe": [ADMINS], "ayse": ["CN=BİLGİ İŞLEM,OU=Gruplar," + BASE],
             "mehmet": ["CN=Domain Users,CN=Users," + BASE, "CN=Kalite Kontrol,OU=Gruplar," + BASE],
             "Admin": [ADMINS],   # a directory account named like the local admin
             "alice": [ADMINS],   # ... and one named like an OIDC account
             "zeynep": ["CN=Bilgi İşlem,OU=Gruplar," + BASE]}
    entries, passwords = {}, {}
    for account, groups in users.items():
        dn = f"CN={account},OU=People,{BASE}"
        entries[dn] = {"objectClass": ["user"], "sAMAccountName": [account],
                       "userPrincipalName": [f"{account}@corp.local"], "memberOf": groups}
        passwords[dn] = f"pw-{account}"
    return FakeDirectory(entries, passwords)


def local_name_refused(srv):
    """The directory's "Admin" signs in with its directory password: "admin" is the local
    admin's name (letter case aside), so the sign-in is refused, said so and recorded,
    and the local account keeps its source, role, password and sessions."""
    before = user_of(srv, "admin")
    for typed in ("ADMIN", "Admin"):
        st, _, page = Browser(srv.base).post("/login", {"username": typed, "password": "pw-Admin"})
        assert st == 403, (typed, st)
        assert "There is a local account named admin here" in page, page[-1500:]
    # the local name itself is checked against the local password only (never LDAP)
    st, _, page = Browser(srv.base).post("/login", {"username": "admin", "password": "pw-Admin"})
    assert st == 401 and "Invalid username or password." in page, st
    assert user_of(srv, "admin") == before
    assert not [u for u in srv.saved_config()["users"] if u["source"] != "local"
                and u["username"].lower() == "admin"], srv.saved_config()["users"]
    assert ("LDAP login denied for 'ADMIN': the local account 'admin' has this name, and an "
            "LDAP sign-in never takes over a local account") in all_log(srv), all_log(srv)[-1500:]
    assert "\tldap_login_denied\tlocal account admin\tADMIN" in audit_log(srv), audit_log(srv)
    Browser(srv.base).login("admin", PW)


def other_method_refused(srv):
    """The directory's "alice" signs in, but "alice" is an OIDC account here (another
    person, perhaps): refused like a local name, and the OIDC account stays as it was."""
    before = user_of(srv, "alice")
    for typed in ("alice", "Alice"):
        st, _, page = Browser(srv.base).post("/login", {"username": typed, "password": "pw-alice"})
        assert st == 403, (typed, st)
        assert "An account named alice here signs in another way" in page, page[-1500:]
    assert user_of(srv, "alice") == before
    assert ("LDAP login denied for 'alice': the OIDC account 'alice' has this name, and an "
            "LDAP sign-in never takes over an account of another sign-in method") in all_log(srv)
    assert "\tldap_login_denied\tOIDC account alice\talice" in audit_log(srv), audit_log(srv)


def ldap_form(d, **fields):
    form = {"ldap_enabled": "on", "ldap_server_uri": d.uri,
            "ldap_user_dn_template": TEMPLATE, "ldap_admin_group": "admins",
            "ldap_operator_group": "bilgi işlem", "ldap_viewer_group": "",
            "ldap_default_role": "", "ldap_nested_groups_shown": "1"}
    form.update(fields)
    return {k: v for k, v in form.items() if v is not None}


def test_ldap():
    with ad_directory() as d:
        # JDoe: what an earlier version made of a sign-in typed with a capital J
        config = {"users": [local_admin(), shadow("jdoe", "ldap", 5), shadow("JDoe", "ldap", 11)],
                  "ldap": {"enabled": True, "server_uri": d.uri,
                           "user_dn_template": TEMPLATE, "admin_group": "admins",
                           "operator_group": "bilgi işlem", "default_role": ""}}
        with Server(dict(config, users=[local_admin(), shadow("alice", "oidc", 21)])) as srv:
            other_method_refused(srv)
            print("LDAP: a sign-in under an OIDC account's name is refused; that account stays "
                  "as it was  OK")
        with Server(config) as srv:
            assert "LDAP accounts 'jdoe', 'JDoe' are one directory user" in all_log(srv)
            local_name_refused(srv)
            print("LDAP: a sign-in under a local account's name (any letter case) is refused; "
                  "the local admin stays as it was  OK")

            old, capital = session_for(srv, "jdoe", 5), session_for(srv, "JDoe", 11)
            assert signed_in(srv, old) and signed_in(srv, capital)

            # "admins" is only part of the group name ser2net-admins: refused
            st, _, page = Browser(srv.base).post("/login", {"username": "jdoe",
                                                             "password": "pw-jdoe"})
            assert st == 401 and "Invalid username or password." in page, st
            log = all_log(srv)
            assert (f"WARNING: LDAP admin_group 'admins' is only part of the name of the group "
                    f"'{ADMINS}', which no longer counts as a match: 'jdoe' gets no access "
                    f"instead of admin") in log, log[-2000:]
            assert "set it to 'ser2net-admins' or its full DN" in log, log[-2000:]
            assert "`ser2net --set-password NAME` with a new NAME creates a local admin" in log
            assert ("LDAP login denied for 'jdoe': no group setting matches their groups: "
                    "'ser2net-admins'") in log, log[-2000:]
            assert "LDAP: 'jdoe' signed out everywhere" in log, log[-2000:]
            print("LDAP: a setting that is only part of a group name is refused and logged  OK")

            # ...and the sessions the account still had are over (in any spelling), saved once
            assert not signed_in(srv, old), "the refused account kept its session"
            assert not signed_in(srv, capital), "another spelling kept its session"
            assert user_of(srv, "jdoe")["pwd_version"] == 6 and user_of(srv, "JDoe")["pwd_version"] == 12
            saved = backups(srv)
            st, _, _ = Browser(srv.base).post("/login", {"username": "jdoe", "password": "pw-jdoe"})
            assert st == 401 and user_of(srv, "jdoe")["pwd_version"] == 6
            assert backups(srv) == saved, "a refused sign-in saved the config again"
            print("LDAP: a refused re-login signs the account out everywhere (one save)  OK")

            admin = Browser(srv.base)
            admin.login("admin", PW)
            _, _, page = admin.get("/settings")
            assert (f"the Admin group “admins” matched the group “{ADMINS}” of jdoe only as "
                    f"part of its name") in page, "no warning in Settings"
            assert "could not sign in (before, they got admin)" in page
            assert "such as “ser2net-admins”" in page
            print("LDAP: Settings shows which setting no longer matches  OK")

            # a Turkish group name in another letter case: BİLGİ İŞLEM = "bilgi işlem"
            Browser(srv.base).login("ayse", "pw-ayse")
            assert user_of(srv, "ayse")["role"] == "operator"
            print("LDAP: UPN direct bind finds the groups; Turkish İ/ı names match  OK")

            # the fix: the whole name; saving drops the warning
            st, _, page = admin.post("/settings/ldap", ldap_form(
                d, ldap_admin_group="ser2net-admins", ldap_default_role="viewer"))
            assert "LDAP settings saved." in page and "only as part of its name" not in page
            Browser(srv.base).login("jdoe", "pw-jdoe")
            assert user_of(srv, "jdoe")["role"] == "admin"
            # one account in any letter case: JDoe went at that sign-in, JDOE is jdoe
            assert [u["username"] for u in srv.saved_config()["users"]
                    if u["username"].lower() == "jdoe"] == ["jdoe"], srv.saved_config()["users"]
            logins = all_log(srv).count("login: jdoe (admin, ldap) from")
            phone = Browser(srv.base)
            phone.login("JDOE", "pw-jdoe")
            assert len(srv.saved_config()["users"]) == 3, srv.saved_config()["users"]
            assert signed_in(srv, phone.cookie("ser2net_session").value)
            assert all_log(srv).count("login: jdoe (admin, ldap) from") == logins + 1
            assert "login: JDOE" not in all_log(srv) and "\tJDOE\n" not in audit_log(srv)
            assert audit_log(srv).count("\tlogin\tadmin, ldap\tjdoe\n") == 2, audit_log(srv)
            print("LDAP: a name in any letter case signs in to the one account (earlier "
                  "spellings merged, logged under its name)  OK")
            Browser(srv.base).login("mehmet", "pw-mehmet")
            assert user_of(srv, "mehmet")["role"] == "viewer"
            assert ("LDAP login for 'mehmet': no group setting matches their groups: "
                    "'Domain Users', 'Kalite Kontrol'; default role viewer") in all_log(srv)
            print("LDAP: the group's whole name works; the default role logs the groups  OK")

            # nested groups: ticked, kept by a form without the box, unticked
            for fields, expect in (({"ldap_nested_groups": "on"}, True),
                                   ({"ldap_nested_groups_shown": None}, True),
                                   ({}, False)):
                st, _, page = admin.post("/settings/ldap", ldap_form(
                    d, ldap_admin_group="ser2net-admins", **fields))
                assert "LDAP settings saved." in page, st
                assert srv.saved_config()["ldap"]["nested_groups"] is expect, fields
            print("LDAP: the nested-groups box is saved; an older form keeps it  OK")

            # a sign-in that changes no account writes nothing; one whose new account
            # can't be saved still signs in (the account is used from memory)
            def config_file():
                with open(srv.config_path, "rb") as fh:
                    return os.stat(srv.config_path).st_mtime_ns, fh.read()
            before = config_file()
            Browser(srv.base).login("ayse", "pw-ayse")
            assert config_file() == before, "an unchanged account was saved again"
            if os.name == "posix" and os.geteuid() != 0:
                os.chmod(srv.data, 0o500)  # config.json can't be replaced
                try:
                    z = Browser(srv.base)
                    z.login("zeynep", "pw-zeynep")
                    assert signed_in(srv, z.cookie("ser2net_session").value)
                finally:
                    os.chmod(srv.data, 0o700)
                assert config_file() == before
                assert ("WARNING: LDAP: could not save the account of 'zeynep' (Could not save "
                        "config.json: Permission denied); they are signed in") in all_log(srv)
                print("LDAP: an unchanged account is not saved again; an account that can't be "
                      "saved still signs in  OK")
            else:
                print("SKIP: the unwritable config.json part needs a POSIX non-root run")


# ---------------- OIDC ----------------

def make_pki(d):
    """A test CA and a 127.0.0.1 certificate it signed, for the provider."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def build(subject, issuer, pub, signer, ca, sans=()):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(pub)
             .serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(hours=1))
             .not_valid_after(now + datetime.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             .add_extension(x509.KeyUsage(digital_signature=not ca, key_encipherment=False,
                                          content_commitment=False, data_encipherment=False,
                                          key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                          encipher_only=False, decipher_only=False), critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()),
                            critical=False))
        if sans:
            b = b.add_extension(x509.SubjectAlternativeName(list(sans)), critical=False)
        return b.sign(signer, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ser2net test CA")])
    ca = build(ca_name, ca_name, ca_key.public_key(), ca_key, True)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = build(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]), ca_name,
                 key.public_key(), ca_key, False,
                 [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    paths = {n: os.path.join(d, n) for n in ("ca.pem", "server.pem", "server.key")}
    for name, data in (("ca.pem", ca.public_bytes(serialization.Encoding.PEM)),
                       ("server.pem", cert.public_bytes(serialization.Encoding.PEM)),
                       ("server.key", key.private_bytes(serialization.Encoding.PEM,
                                                        serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))):
        with open(paths[name], "wb") as fh:
            fh.write(data)
    return paths


class _Provider(http.server.BaseHTTPRequestHandler):
    """Discovery, JWKS and a token endpoint that hands out an id_token with
    server.claims (set by the test before each sign-in)."""

    def _json(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # http.server API
        iss = self.server.issuer
        if self.path == "/.well-known/openid-configuration":
            return self._json({"issuer": iss, "authorization_endpoint": iss + "/authorize",
                               "token_endpoint": iss + "/token", "jwks_uri": iss + "/jwks"})
        if self.path == "/jwks":
            return self._json({"keys": [self.server.key.as_dict()]})
        self.send_error(404)

    def do_POST(self):
        from authlib.jose import jwt
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        self.server.token_requests += 1
        self.server.token_bodies.append(dict(urllib.parse.parse_qsl(body)))
        if self.server.refuse:   # the code was not accepted
            data = json.dumps({"error": "invalid_grant"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        now = int(time.time())
        claims = dict(self.server.claims, iss=self.server.issuer, aud="ser2net", sub="s-1",
                      iat=now, exp=now + 300)
        token = jwt.encode({"alg": "RS256"}, claims, self.server.key).decode()
        self._json({"id_token": token, "token_type": "Bearer", "access_token": "a"})

    def log_message(self, *args):
        pass


def start_provider(pki):
    from authlib.jose import JsonWebKey
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["server.pem"], pki["server.key"])
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.issuer = f"https://127.0.0.1:{srv.server_address[1]}"
    srv.key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    srv.claims = {}
    srv.refuse, srv.token_requests, srv.token_bodies = False, 0, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def oidc_start(srv, provider, query=""):
    """/auth/oidc/login in a new browser: (browser, the state and nonce it sends)."""
    b = Browser(srv.base)
    st, headers, _ = b.get("/auth/oidc/login" + query)
    location = headers.get("Location", "")
    assert st == 303 and location.startswith(provider.issuer), (st, all_log(srv)[-1500:])
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["code_challenge_method"] == ["S256"], query    # PKCE, always
    provider.challenge = query["code_challenge"][0]
    return b, query["state"][0], query["nonce"][0]


def oidc_provider_answers(srv, provider):
    """PKCE: the token request carries the verifier of the challenge the browser was
    sent with; a state cookie from before (no verifier) still works. The provider's
    error answers are logged and shown by their code, never as its text."""
    st, _ = oidc_sign_in(srv, provider, {"preferred_username": "pkce.user",
                                         "groups": ["ser2net-viewers"]})
    assert st == 303, st
    verifier = provider.token_bodies[-1]["code_verifier"]
    assert oidc_auth.code_challenge(verifier) == provider.challenge
    # a sign-in started by the previous version: its state cookie has no verifier
    b, state, nonce = oidc_start(srv, provider)
    redirect = f"{srv.base}/auth/oidc/callback"
    old = auth.sign_payload(srv.saved_config()["secret_key"],
                            {"s": state, "n": nonce, "r": redirect}, ttl_seconds=600)
    b.jar.clear()
    b.jar.set_cookie(http.cookiejar.Cookie(
        0, ui_cookie("ser2net_oidc", srv.port, path="/auth/oidc"), old, None, False, "127.0.0.1",
        False, False, "/auth/oidc", True, False, None, False, None, None, {}))
    provider.claims = {"preferred_username": "pkce.user", "groups": ["ser2net-viewers"],
                       "nonce": nonce}
    st, h, _ = b.get("/auth/oidc/callback?" + urllib.parse.urlencode({"code": "C", "state": state}))
    assert st == 303 and "code_verifier" not in provider.token_bodies[-1], st
    assert h.get("Location") == "/", "no page to go back to in an older cookie: the dashboard"

    b, state, _nonce = oidc_start(srv, provider)
    requests = provider.token_requests
    for query, status, text in (
            ({"error": "access_denied", "error_description": "User is not assigned to the "
              "client application.", "state": state}, 403, "didn’t let you sign in to ser2net"),
            ({"error": "temporarily_unavailable"}, 503, "can’t sign you in right now"),
            ({"error": "invalid_scope", "state": state}, 400,
             "refused the sign-in request (invalid_scope)"),
            ({"error": "<b>x</b>", "state": state}, 400, "refused the sign-in request (?)")):
        st, _, page = b.get("/auth/oidc/callback?" + urllib.parse.urlencode(query))
        assert st == status and text in page, (query, st)
        assert "User is not assigned" not in page and "<b>x</b>" not in page
    log = all_log(srv)
    assert ("OIDC: the identity provider refused the sign-in: 'access_denied' ('User is not "
            "assigned to the client application.')") in log, log[-1500:]
    assert "'temporarily_unavailable' (not for a sign-in started in this browser)" in log
    assert provider.token_requests == requests
    audit = audit_log(srv)
    for reason in ("(access_denied)", "(invalid_scope)", "(?)",
                   "(temporarily_unavailable), not for a sign-in started in this browser"):
        assert f"\toidc_login_failed\tthe identity provider refused it {reason}\t\n" in audit, \
            (reason, audit[-1500:])

    # an e-mail address the provider hasn't verified names nobody (sub doesn't stand in)
    st, page = oidc_sign_in(srv, provider, {"email": "eve@example.com", "email_verified": False,
                                            "groups": ["ser2net-viewers"]})
    assert st == 403, st
    assert ("OIDC login denied: the ID token names the user by the e-mail address "
            "'eve@example.com', which the provider has not verified") in all_log(srv)
    assert not [u for u in srv.saved_config()["users"] if u["username"] in ("eve@example.com", "s-1")]
    assert "\toidc_login_failed\tno user name in the ID token (all.log says why)\t\n" \
        in audit_log(srv), audit_log(srv)[-800:]


def oidc_next(srv, provider):
    """A sign-in started from the sign-in page of a page whose session ended goes back to
    that page (?next=, carried in the signed state cookie); one that is not a page here
    goes to the dashboard."""
    claims = {"preferred_username": "pkce.user", "groups": ["ser2net-viewers"]}
    for nxt, want in (("/settings/audit?q=x", "/settings/audit?q=x"),
                      ("//evil.example/", "/"), ("https://evil.example/", "/")):
        b, state, nonce = oidc_start(srv, provider, "?" + urllib.parse.urlencode({"next": nxt}))
        provider.claims = dict(claims, nonce=nonce)
        st, h, _ = b.get("/auth/oidc/callback?" + urllib.parse.urlencode(
            {"code": "CODE", "state": state}))
        assert st == 303 and h.get("Location") == want, (nxt, st, h.get("Location"))


def oidc_sign_in(srv, provider, claims):
    """Through the provider with these claims: (status, page) of the callback."""
    b, state, nonce = oidc_start(srv, provider)
    provider.claims = dict(claims, nonce=nonce)
    st, _, page = b.get("/auth/oidc/callback?" + urllib.parse.urlencode(
        {"code": "CODE", "state": state}))
    return st, page


def oidc_refused_callbacks(srv, provider):
    """A callback without the state cookie, with another state or without a code is
    refused (400) before the code is used; a code the provider doesn't accept gives 401.
    Nothing is saved."""
    before, requests = srv.saved_config(), provider.token_requests
    b, state, nonce = oidc_start(srv, provider)
    provider.claims = {"preferred_username": "mallory", "groups": ["/plant/ser2net-admins"],
                       "nonce": nonce}
    stranger = Browser(srv.base)            # no state cookie
    for browser, query in ((stranger, {"code": "CODE", "state": state}),
                           (b, {"code": "CODE", "state": state + "x"}),
                           (b, {"code": "CODE", "state": ""}),
                           (b, {"code": "CODE", "state": "çalış"}),     # not ASCII: no 500
                           (b, {"state": state})):
        st, _, page = browser.get("/auth/oidc/callback?" + urllib.parse.urlencode(query))
        assert st == 400 and "SSO state mismatch" in page, (st, query)
    assert provider.token_requests == requests, "the code was used despite a bad state"
    provider.refuse = True
    try:
        st, _, page = b.get("/auth/oidc/callback?" + urllib.parse.urlencode(
            {"code": "CODE", "state": state}))
    finally:
        provider.refuse = False
    assert st == 401 and "SSO sign-in failed." in page, st
    assert provider.token_requests == requests + 1
    assert ("OIDC sign-in failed: the token endpoint refused the code: HTTP 400 "
            "'invalid_grant'") in all_log(srv), all_log(srv)[-1500:]
    assert srv.saved_config()["users"] == before["users"]
    assert not any(u["username"] == "mallory" for u in srv.saved_config()["users"])
    audit = audit_log(srv)
    mismatch = "\toidc_login_failed\tthe answer doesn't match a sign-in started in this browser"
    assert audit.count(mismatch) == 5 and \
        f"{mismatch} (none, or it took over 10 minutes)\t\n" in audit, audit
    assert "\toidc_login_failed\tthe code or the ID token was refused (all.log says why)\t\n" \
        in audit, audit


def test_oidc():
    tmp = tempfile.mkdtemp(prefix="s2n_oidc_")
    provider = start_provider(make_pki(tmp))
    try:
        config = {"users": [local_admin(), shadow("sso.user", "oidc", 7)],
                  "oidc": {"enabled": True, "issuer": provider.issuer, "client_id": "ser2net",
                           "client_secret": "s3cret", "admin_group": "admins",
                           "operator_group": "Bakım", "viewer_group": "ser2net-viewers"}}
        with Server(config, env={"SSL_CERT_FILE": os.path.join(tmp, "ca.pem")}) as srv:
            oidc_refused_callbacks(srv, provider)
            print("OIDC: callback without/with a wrong state 400, a refused code 401 (the "
                  "token endpoint's answer logged)  OK")
            oidc_provider_answers(srv, provider)
            print("OIDC: PKCE S256 (an older state cookie still works); the provider's errors "
                  "logged and shown by code; an unverified e-mail names nobody  OK")
            oidc_next(srv, provider)
            print("OIDC: the sign-in goes back to the page it was started for (a local one)  OK")

            # the provider's "admin" (or "Admin") is not the local admin
            before = user_of(srv, "admin")
            for name in ("admin", "Admin"):
                st, page = oidc_sign_in(srv, provider, {"preferred_username": name,
                                                        "groups": ["ser2net-viewers"]})
                assert st == 403 and "There is a local account named admin here" in page, st
            assert user_of(srv, "admin") == before, "the local admin was changed"
            assert ("OIDC login denied for 'admin': the local account 'admin' has this name, "
                    "and an OIDC sign-in never takes over a local account") in all_log(srv)
            assert "\toidc_login_denied\tlocal account admin\tAdmin" in audit_log(srv)
            Browser(srv.base).login("admin", PW)
            print("OIDC: a sign-in under a local account's name is refused; the local admin "
                  "stays as it was  OK")

            # names no account can have: refused, with the reason in all.log
            refused_names = ("(anonymous)", "ad\u202emin", "x" * 256)
            for name in refused_names:
                st, page = oidc_sign_in(srv, provider, {"preferred_username": name,
                                                        "groups": ["ser2net-viewers"]})
                assert st == 403 and "can’t be used here" in page, (name, st)
            log = all_log(srv)
            assert ("OIDC login denied for '(anonymous)': no account can have this name here: "
                    "a name in parentheses") in log, log[-1500:]
            assert "it contains control or invisible characters (U+202E)" in log
            assert "Username claim can name another claim" in log
            assert not [u for u in srv.saved_config()["users"] if u["username"] in refused_names]
            st, _ = oidc_sign_in(srv, provider, {"preferred_username": " CORP\\op/1#x ",
                                                 "groups": ["ser2net-viewers"]})
            assert st == 303 and user_of(srv, "CORP\\op/1#x")["role"] == "viewer", st
            print("OIDC: names with invisible characters, over-long or in parentheses are "
                  "refused; DOMAIN\\name with / and # works  OK")

            old = session_for(srv, "sso.user", 7)
            assert signed_in(srv, old)
            st, page = oidc_sign_in(srv, provider, {"preferred_username": "sso.user",
                                                    "groups": ["/plant/ser2net-admins"]})
            assert st == 403 and "Your account isn&#39;t mapped to a role." in page, st
            log = all_log(srv)
            assert ("WARNING: OIDC admin_group 'admins' is only part of the name of the group "
                    "'/plant/ser2net-admins'") in log, log[-2000:]
            assert "set it to 'ser2net-admins' or the whole value" in log
            assert ("OIDC login denied for 'sso.user': no group setting matches their groups: "
                    "'ser2net-admins'") in log, log[-2000:]
            assert not signed_in(srv, old) and user_of(srv, "sso.user")["pwd_version"] == 8
            assert "\toidc_login_failed\ttheir groups give no role\tsso.user\n" in audit_log(srv)
            print("OIDC: part of a group name is refused, logged, and signs the account out  OK")

            # Keycloak path, Turkish name in capitals: /Fabrika/BAKIM = "Bakım"
            st, _ = oidc_sign_in(srv, provider, {"preferred_username": "tekniker",
                                                 "groups": ["/Fabrika/BAKIM"]})
            assert st == 303 and user_of(srv, "tekniker")["role"] == "operator", st
            print("OIDC: a path's last part and Turkish İ/ı names match  OK")

        # the provider takes the only admin's role away: it decides, and all.log warns
        config["users"] = [shadow("boss", "oidc", 3)]
        with Server(config, env={"SSL_CERT_FILE": os.path.join(tmp, "ca.pem")}) as srv:
            st, _ = oidc_sign_in(srv, provider, {"preferred_username": "boss",
                                                 "groups": ["ser2net-viewers"]})
            assert st == 303 and user_of(srv, "boss")["role"] == "viewer", st
            assert ("WARNING: OIDC gives 'boss' the role viewer, so no admin account can sign "
                    "in any more. `ser2net --set-password NAME`") in all_log(srv), all_log(srv)[-800:]
            print("OIDC: the provider demoting the only admin is logged as a warning  OK")
    finally:
        provider.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if need("ldap3", what="LDAP sign-in through the web UI"):
        test_ldap()
    if need("authlib", "cryptography", what="OIDC sign-in through the web UI"):
        test_oidc()
    print("\nPASS: LDAP / OIDC sign-in through the web UI")


if __name__ == "__main__":
    main()
