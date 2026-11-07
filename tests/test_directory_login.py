"""Signing in to the web UI with LDAP / Active Directory and with OIDC, end to end: a
ser2net server against a small LDAP server (_ldap_server.py) and a small HTTPS OpenID
provider in this process. The LDAP part needs ldap3, the OIDC part authlib and
cryptography; each is skipped without them.

  * a group setting that is only part of a group's name ("admins" for ser2net-admins)
    no longer gives that role: the sign-in is refused with the usual message, all.log
    names the setting and the group and says how to fix it (and how to get back in
    with `ser2net --set-password`), and Settings shows it until that section is saved;
  * an existing LDAP/OIDC account whose groups give no role any more is signed out
    everywhere at its next sign-in — once: further refused tries don't save again;
  * the AD direct-bind template {username}@corp.local reads the user's groups; a
    Turkish group name typed in another letter case gives its role; a sign-in that
    gets the default role logs the user's groups;
  * the nested-groups box is saved, and kept when a form without it is posted.

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

from _server import Browser, Server

from app.web import auth
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
        0, "ser2net_session", token, None, False, "127.0.0.1", False, False, "/", True,
        False, None, False, None, None, {}))
    st, headers, _ = b.get("/settings")
    assert st == 200 or (st == 303 and headers.get("Location") == "/login"), st
    return st == 200


def user_of(srv, name) -> dict:
    return next(u for u in srv.saved_config()["users"] if u["username"] == name)


def all_log(srv) -> str:
    with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
        return fh.read()


def backups(srv) -> int:
    folder = os.path.join(srv.data, "backups")
    return len(os.listdir(folder)) if os.path.isdir(folder) else 0


# ---------------- LDAP / Active Directory ----------------

def ad_directory():
    from _ldap_server import FakeDirectory
    users = {"jdoe": [ADMINS], "ayse": ["CN=BİLGİ İŞLEM,OU=Gruplar," + BASE],
             "mehmet": ["CN=Domain Users,CN=Users," + BASE, "CN=Kalite Kontrol,OU=Gruplar," + BASE]}
    entries, passwords = {}, {}
    for account, groups in users.items():
        dn = f"CN={account},OU=People,{BASE}"
        entries[dn] = {"objectClass": ["user"], "sAMAccountName": [account],
                       "userPrincipalName": [f"{account}@corp.local"], "memberOf": groups}
        passwords[dn] = f"pw-{account}"
    return FakeDirectory(entries, passwords)


def ldap_form(d, **fields):
    form = {"ldap_enabled": "on", "ldap_server_uri": d.uri,
            "ldap_user_dn_template": TEMPLATE, "ldap_admin_group": "admins",
            "ldap_operator_group": "bilgi işlem", "ldap_viewer_group": "",
            "ldap_default_role": "", "ldap_nested_groups_shown": "1"}
    form.update(fields)
    return {k: v for k, v in form.items() if v is not None}


def test_ldap():
    try:
        import ldap3  # noqa: F401
    except ImportError:
        print("skip: ldap3 not installed")
        return
    with ad_directory() as d:
        config = {"users": [local_admin(), shadow("jdoe", "ldap", 5)],
                  "ldap": {"enabled": True, "server_uri": d.uri,
                           "user_dn_template": TEMPLATE, "admin_group": "admins",
                           "operator_group": "bilgi işlem", "default_role": ""}}
        with Server(config) as srv:
            old = session_for(srv, "jdoe", 5)
            assert signed_in(srv, old)

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

            # ...and the sessions the account still had are over, saved once
            assert not signed_in(srv, old), "the refused account kept its session"
            assert user_of(srv, "jdoe")["pwd_version"] == 6
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

    def do_GET(self):  # noqa: N802 (http.server API)
        iss = self.server.issuer
        if self.path == "/.well-known/openid-configuration":
            return self._json({"issuer": iss, "authorization_endpoint": iss + "/authorize",
                               "token_endpoint": iss + "/token", "jwks_uri": iss + "/jwks"})
        if self.path == "/jwks":
            return self._json({"keys": [self.server.key.as_dict()]})
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        from authlib.jose import jwt
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
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
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def oidc_sign_in(srv, provider, claims):
    """Through the provider with these claims: (status, page) of the callback."""
    b = Browser(srv.base)
    st, headers, _ = b.get("/auth/oidc/login")
    location = headers.get("Location", "")
    assert st == 303 and location.startswith(provider.issuer), (st, all_log(srv)[-1500:])
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    provider.claims = dict(claims, nonce=query["nonce"][0])
    st, _, page = b.get("/auth/oidc/callback?" + urllib.parse.urlencode(
        {"code": "CODE", "state": query["state"][0]}))
    return st, page


def test_oidc():
    try:
        import authlib  # noqa: F401
        import cryptography  # noqa: F401
    except ImportError:
        print("skip: authlib or cryptography not installed")
        return
    tmp = tempfile.mkdtemp(prefix="s2n_oidc_")
    provider = start_provider(make_pki(tmp))
    try:
        config = {"users": [local_admin(), shadow("sso.user", "oidc", 7)],
                  "oidc": {"enabled": True, "issuer": provider.issuer, "client_id": "ser2net",
                           "client_secret": "s3cret", "admin_group": "admins",
                           "operator_group": "Bakım"}}
        with Server(config, env={"SSL_CERT_FILE": os.path.join(tmp, "ca.pem")}) as srv:
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
            print("OIDC: part of a group name is refused, logged, and signs the account out  OK")

            # Keycloak path, Turkish name in capitals: /Fabrika/BAKIM = "Bakım"
            st, _ = oidc_sign_in(srv, provider, {"preferred_username": "tekniker",
                                                 "groups": ["/Fabrika/BAKIM"]})
            assert st == 303 and user_of(srv, "tekniker")["role"] == "operator", st
            print("OIDC: a path's last part and Turkish İ/ı names match  OK")
    finally:
        provider.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_ldap()
    test_oidc()
    print("\nPASS: LDAP / OIDC sign-in through the web UI")


if __name__ == "__main__":
    main()
