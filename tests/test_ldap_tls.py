"""LDAPS and StartTLS check the directory server's certificate and host name.

Against the test LDAP server (_ldap_server.py) over real TLS, with certificates from a
test CA made here (_pki.py):
  * accepted: a certificate from the CA in ldap.ca_file (PEM or DER), from a CA the
    system trusts (SSL_CERT_FILE stands in for the system store), a self-signed one in
    ca_file; an IP address in the URI that the certificate lists (ldap3's own check on
    Python 3.12+ rejects that), a DNS name; LDAPS and StartTLS, direct bind and search +
    bind;
  * refused before any password is sent: a certificate from another CA, none trusted
    by the system, one for another name — each logged with what to set
    (ldap.ca_file, the server URI, `ser2netweb --set-password`), the reason found also
    when the host has several addresses;
  * ldap.tls_verify false (the insecure opt-out) accepts any certificate;
  * an unreadable or certificate-less ca_file is reported, and nothing is sent;
  * a referral is not followed over TLS (the referred server would get the credentials
    unchecked).
Through the web UI: an upgraded LDAPS config logs that certificates are checked now; a
restore preview lists the check switching off or on; a refused certificate shows in
Settings → LDAP until the section is saved; the form saves
the CA file (refusing an unusable one) and the opt-out, keeps them when an older form
without the fields is posted; Settings and the start-up log warn while the check is off
and while passwords go unencrypted (ldap:// without StartTLS).

The pure parts (settings, the reason in an exception, the messages) always run; the
rest needs ldap3 and cryptography (SKIP without, or fail with SER2NETWEB_OPTIONAL_TESTS=1).
Run: python3 tests/test_ldap_tls.py
"""
import contextlib
import os
import shutil
import ssl
import tempfile

from _optional import need
from _server import Browser, Server

from ser2netweb.config import AppConfig, LdapSettings
from ser2netweb.web import ldap_auth
from ser2netweb.web.auth import hash_password
from ser2netweb.web.routes.settings import _setting_changes

PW = "ldap-tls-pass-1"
BASE = "DC=corp,DC=local"
JDOE = "CN=jdoe,OU=People," + BASE
OPS = "CN=ser2net-operators,OU=Groups," + BASE
SVC = "CN=svc-ser2net,OU=Service," + BASE
CHILD = "DC=child," + BASE


# ---------------- pure: settings, reasons, messages ----------------

def test_settings():
    assert LdapSettings().tls_verify is True and LdapSettings().ca_file == ""
    for uri, start_tls, tls, plain in (("ldaps://dc:636", False, True, False),
                                       (" LDAPS://dc ", False, True, False),
                                       ("ldap://dc", True, True, False),
                                       ("ldap://dc", False, False, True),
                                       ("dc.corp.local", False, False, True),
                                       ("ldapi:///run/slapd/ldapi", False, False, False)):
        s = LdapSettings(server_uri=uri, start_tls=start_tls)
        assert (s.uses_tls, s.sends_plaintext) == (tls, plain), uri
    # verification on by default, also for a config written before the setting existed
    upgraded = AppConfig.from_dict({"ldap": {"enabled": True, "server_uri": "ldaps://dc:636",
                                             "user_dn_template": "{username}@corp.local"}})
    assert upgraded.ldap.tls_verify is True
    assert any("certificate is now checked" in n and "ldap.ca_file" in n
               for n in upgraded.load_notes), upgraded.load_notes
    for quiet in ({"enabled": True, "server_uri": "ldaps://dc", "tls_verify": True},
                  {"enabled": True, "server_uri": "ldap://dc"},
                  {"enabled": False, "server_uri": "ldaps://dc"}):
        assert not AppConfig.from_dict({"ldap": quiet}).load_notes, quiet
    off = AppConfig.from_dict({"ldap": {"tls_verify": False, "ca_file": "/x/ca.pem"}})
    saved = off.to_dict()
    assert saved["ldap"]["tls_verify"] is False and saved["ldap"]["ca_file"] == "/x/ca.pem"
    # restoring a backup that turns the check off says so in its preview
    assert "LDAP · Server certificate check: enabled → disabled" in \
        _setting_changes(AppConfig(), off), _setting_changes(AppConfig(), off)
    assert not [c for c in _setting_changes(AppConfig(), AppConfig()) if "certificate" in c]
    # the contexts: system CAs, checked name; or nothing checked at all
    ctx = ldap_auth.tls_context(LdapSettings())
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    off = ldap_auth.tls_context(LdapSettings(tls_verify=False, ca_file="/does/not/exist"))
    assert off.verify_mode == ssl.CERT_NONE and not off.check_hostname
    print("settings: verification on by default (upgrade note); uses_tls / sends_plaintext  OK")


def test_failure_reasons():
    """The reason is found in what ldap3 raises: its own exception with the ssl error's
    text, for StartTLS too, and for a host with several addresses (a list per address)."""
    verify = ("socket ssl wrapping error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify "
              "failed: unable to get local issuer certificate (_ssl.c:1081)")
    assert ldap_auth.tls_failure(OSError(verify)) == "unable to get local issuer certificate"
    starttls = ("wrap socket error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "Hostname mismatch, certificate is not valid for 'dc.corp.local'. (_ssl.c:1081)")
    assert ldap_auth.tls_failure(RuntimeError(starttls)) == \
        "Hostname mismatch, certificate is not valid for 'dc.corp.local'."
    history = [(ConnectionRefusedError("[Errno 111] Connection refused"), ("::1", 636, 0, 0)),
               (OSError(verify), ("127.0.0.1", 636))]
    assert ldap_auth.tls_failure(OSError("unable to open socket", history)) == \
        "unable to get local issuer certificate"
    for other in (OSError("socket connection error while opening: [Errno 111] Connection "
                          "refused"), ValueError("invalidCredentials"),
                  OSError("[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1081)")):
        assert ldap_auth.tls_failure(other) is None, other
    print("the refused certificate's reason, from LDAPS, StartTLS and multi-address errors  OK")


def test_messages():
    s = LdapSettings(server_uri="ldaps://dc.corp.local")
    text = str(ldap_auth.TlsProblem("certificate", "self-signed certificate", s.server_uri))
    for part in ("the certificate of the directory server ldaps://dc.corp.local was not accepted "
                 "(self-signed certificate)", "nothing was sent",
                 "Settings → LDAP / Active Directory → CA certificate file (ldap.ca_file",
                 "ldap.tls_verify: false) is insecure", "`ser2netweb --set-password NAME`"):
        assert part in text, (part, text)
    text = str(ldap_auth.TlsProblem("certificate", "unable to get local issuer certificate",
                                    s.server_uri, "/etc/ser2net/ca.pem"))
    assert "The CA certificate file '/etc/ser2net/ca.pem' (ldap.ca_file) has to hold" in text
    text = str(ldap_auth.TlsProblem("name", "IP address mismatch, certificate is not valid for "
                                            "'10.0.0.5'.", "ldaps://10.0.0.5"))
    assert "issued for another name. Use a name it was issued for in the server URI" in text
    assert "--set-password" in text
    text = str(ldap_auth.TlsProblem("ca_file", "can't read it: No such file or directory",
                                    s.server_uri, "/x/ca.pem", True))
    assert text.startswith("LDAP: the CA certificate file '/x/ca.pem' (ldap.ca_file) can't be "
                           "used: can't read it"), text
    print("log lines: what was refused and what to set; the way back in  OK")


# ---------------- over TLS, against the test directory ----------------

def server_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def directory(cert_key, **kw):
    from _ldap_server import FakeDirectory
    entries = {JDOE: {"objectClass": ["user"], "sAMAccountName": ["jdoe"],
                      "userPrincipalName": ["jdoe@corp.local"], "memberOf": [OPS]},
               SVC: {"objectClass": ["user"], "sAMAccountName": ["svc-ser2net"]},
               "CN=CHILD,CN=Partitions,CN=Configuration," + BASE: {
                   "objectClass": ["crossRef"], "nETBIOSName": ["CHILD"], "nCName": [CHILD]}}
    tls = server_context(*cert_key) if cert_key else None
    return FakeDirectory(entries, {JDOE: "pw-jdoe", SVC: "pw-svc"}, tls=tls, **kw)


def direct(uri, **kw):
    return LdapSettings(enabled=True, server_uri=uri, user_dn_template="{username}@corp.local",
                        **kw)


@contextlib.contextmanager
def system_trusts(ca_path):
    """SSL_CERT_FILE: the CA bundle the system's trust store is read from (OpenSSL reads
    it when a context loads the default certificates; on Windows too, after its store)."""
    saved = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = ca_path
    try:
        yield
    finally:
        if saved is None:
            del os.environ["SSL_CERT_FILE"]
        else:
            os.environ["SSL_CERT_FILE"] = saved


class Pki:
    def __init__(self, folder):
        from _pki import TestCA
        self.ca, self.other = TestCA(folder), TestCA(folder, "other CA")
        self.ip = self.ca.issue(folder, "dc-ip", ["127.0.0.1", "localhost"])     # good
        self.wrong_name = self.ca.issue(folder, "dc-name", ["dc.corp.local"])    # another name
        self.self_signed = self.ca.issue(folder, "dc-self", ["127.0.0.1"], self_signed=True)


def sign_in(settings, logs, problems, user="jdoe", password="pw-jdoe"):
    return ldap_auth.authenticate(settings, user, password, logs.append,
                                  on_tls_problem=problems.append)


def test_ldaps(pki):
    logs, problems = [], []
    with directory(pki.ip) as d:
        # the CA in ca_file (PEM, then DER as Windows exports it); an IP address in the URI
        for ca_file in (pki.ca.path, pki.ca.der_path):
            assert sign_in(direct(d.uri, ca_file=ca_file), logs, problems) == [OPS], logs[-3:]
        assert d.tls_binds == ["jdoe@corp.local"] * 2 and not problems, (d.tls_binds, problems)
        # a DNS name the certificate lists
        named = direct(d.uri.replace("127.0.0.1", "localhost"), ca_file=pki.ca.path)
        assert sign_in(named, logs, problems) == [OPS], logs[-3:]
        # the system's trust store, when it has the CA
        with system_trusts(pki.ca.path):
            assert sign_in(direct(d.uri), logs, problems) == [OPS], logs[-3:]
        print("LDAPS: the CA from ca_file (PEM / DER) or the system; IP and DNS names  OK")

        # refused: another CA, the system's CAs (without the test CA); nothing is sent
        d.binds.clear()
        for settings in (direct(d.uri, ca_file=pki.other.path), direct(d.uri)):
            assert sign_in(settings, logs, problems) is None
            p = problems.pop()
            assert (p.kind, p.reason) == ("certificate", "unable to get local issuer certificate"), p
            assert logs[-1] == str(p), logs[-1]
        assert "The CA certificate file " in str(ldap_auth.TlsProblem(
            "certificate", "x", d.uri, pki.other.path))
        assert "set Settings → LDAP / Active Directory → CA certificate file" in logs[-1], logs[-1]
        assert d.binds == [], "a password was sent over an unchecked connection"
        assert d.tls_errors, "the handshake should have failed"
        # the same with a host name that also resolves to ::1 (several addresses)
        localhost = direct(d.uri.replace("127.0.0.1", "localhost"), ca_file=pki.other.path)
        assert sign_in(localhost, logs, problems) is None and problems.pop().kind == "certificate"
        assert d.binds == []
        print("LDAPS: a certificate from another CA, or none trusted: refused, nothing sent  OK")

        # the insecure opt-out accepts it
        insecure = direct(d.uri, ca_file=pki.other.path, tls_verify=False)
        assert sign_in(insecure, logs, problems) == [OPS] and not problems
        print("LDAPS: tls_verify off (insecure) accepts any certificate  OK")

    with directory(pki.wrong_name) as d:
        assert sign_in(direct(d.uri, ca_file=pki.ca.path), logs, problems) is None
        p = problems.pop()
        assert p.kind == "name" and p.reason == ("IP address mismatch, certificate is not valid "
                                                 "for '127.0.0.1'."), p
        assert "it was issued for another name" in logs[-1] and d.binds == [], logs[-1]
        print("LDAPS: a certificate for another name: refused, the log says to fix the URI  OK")

    with directory(pki.self_signed) as d:
        assert sign_in(direct(d.uri, ca_file=pki.self_signed[0]), logs, problems) == [OPS]
        assert sign_in(direct(d.uri, ca_file=pki.ca.path), logs, problems) is None
        assert problems.pop().kind == "certificate"
        print("LDAPS: a self-signed server certificate works as its own ca_file  OK")


def test_start_tls(pki):
    logs, problems = [], []
    with directory(pki.ip, start_tls=True) as d:
        assert d.uri.startswith("ldap://")
        ok = direct(d.uri, start_tls=True, ca_file=pki.ca.path)
        assert sign_in(ok, logs, problems) == [OPS], logs[-3:]
        assert d.tls_binds == ["jdoe@corp.local"] and d.binds == d.tls_binds
        d.binds.clear()
        assert sign_in(direct(d.uri, start_tls=True, ca_file=pki.other.path), logs, problems) is None
        p = problems.pop()
        assert p.kind == "certificate" and p.reason == "unable to get local issuer certificate", p
        assert d.binds == [] and "was not accepted" in logs[-1]
        assert sign_in(direct(d.uri, start_tls=True, tls_verify=False), logs, problems) == [OPS]
        # search + bind: the service account's password isn't sent either
        d.binds.clear()
        sb = LdapSettings(enabled=True, server_uri=d.uri, start_tls=True, bind_dn=SVC,
                          bind_password="pw-svc", user_search_base="OU=People," + BASE,
                          user_search_filter="(sAMAccountName={username})", ca_file=pki.ca.path)
        assert sign_in(sb, logs, problems) == [OPS], logs[-3:]
        assert d.binds == [SVC, JDOE] == d.tls_binds[-2:]
        d.binds.clear()
        sb.ca_file = pki.other.path
        assert sign_in(sb, logs, problems) is None and problems.pop().kind == "certificate"
        assert d.binds == []
    print("StartTLS: checked like LDAPS (direct and search + bind); refused: nothing sent  OK")


def test_ca_file_problems(pki, folder):
    logs, problems = [], []
    empty, garbage, key_only = (os.path.join(folder, n) for n in ("empty.pem", "x.bin", "k.pem"))
    with open(pki.ip[1], "rb") as fh:   # a private key, no certificate
        key = fh.read()
    for path, data in ((empty, b""), (garbage, b"not a certificate\x00"), (key_only, key)):
        with open(path, "wb") as fh:
            fh.write(data)
    missing = os.path.join(folder, "missing.pem")
    for path, unreadable in ((missing, True), (empty, False), (garbage, False), (key_only, False)):
        try:
            ldap_auth.tls_context(LdapSettings(ca_file=path))
        except ldap_auth.CaFileError as e:
            assert e.unreadable is unreadable and e.path == path, (path, e)
        else:
            raise AssertionError(f"{path} accepted as a CA file")
    with directory(pki.ip) as d:
        for path in (missing, garbage):
            assert sign_in(direct(d.uri, ca_file=path), logs, problems) is None
            p = problems.pop()
            assert p.kind == "ca_file" and p.ca_file == path, p
            assert logs[-1].startswith(f"LDAP: the CA certificate file {path!r} (ldap.ca_file) "
                                       "can't be used"), logs[-1]
        assert d.binds == [] and not d.tls_errors, "connected despite an unusable CA file"
    print("an unreadable or certificate-less CA file: reported, nothing connects  OK")


def test_referrals(pki):
    """A referral over TLS is logged, not followed: ldap3 would bind to the referred
    server with the same credentials, without checking its certificate (or in plain
    text)."""
    from _ldap_server import FakeDirectory
    logs, problems = [], []
    with FakeDirectory({}, {JDOE: "pw-jdoe", SVC: "pw-svc"}) as other:
        url = f"ldap://127.0.0.1:{other.port}/{CHILD}"
        # it takes CHILD\jdoe (the account is in the child domain, its entry is not here)
        with directory(pki.ip, referrals={CHILD: url}, netbios="CHILD") as d:
            sb = LdapSettings(enabled=True, server_uri=d.uri, bind_dn=SVC, bind_password="pw-svc",
                              user_search_base="OU=People," + CHILD, ca_file=pki.ca.path,
                              user_search_filter="(sAMAccountName={username})")
            assert sign_in(sb, logs, problems) is None
            assert d.binds == [SVC] and other.binds == [], (d.binds, other.binds)
            assert logs[-1] == (f"LDAP: the user search under {sb.user_search_base!r} failed — "
                                f"referral (10): fake directory: error 10; it refers to {url}"), logs
            # DOMAIN\user of another domain: its entry and groups are on another server
            nt4 = LdapSettings(enabled=True, server_uri=d.uri, ca_file=pki.ca.path,
                               user_dn_template="CHILD\\{username}")
            groups = sign_in(nt4, logs, problems)
            assert groups == [] and groups.lookup_failed, (groups, logs[-2:])
            assert (CHILD, 2, "(sAMAccountName=jdoe)") in d.searches, d.searches
            assert f"referral (10): fake directory: error 10; it refers to {url}" in logs[-1], logs
            assert other.binds == [] and not problems
    print("over TLS a referral is logged with its URL, not followed  OK")


# ---------------- through the web UI ----------------

def all_log(srv):
    with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
        return fh.read()


def ldap_form(uri, **fields):
    form = {"ldap_enabled": "on", "ldap_server_uri": uri,
            "ldap_user_dn_template": "{username}@corp.local",
            "ldap_operator_group": "ser2net-operators", "ldap_nested_groups_shown": "1",
            "ldap_tls_shown": "1", "ldap_ca_file": ""}
    form.update(fields)
    return {k: v for k, v in form.items() if v is not None}


def test_web(pki, folder):
    with directory(pki.ip) as d:
        config = {"users": [{"username": "admin", "password_hash": hash_password(PW),
                             "role": "admin", "pwd_version": 1}],
                  # as written by 2.8: no tls_verify / ca_file
                  "ldap": {"enabled": True, "server_uri": d.uri,
                           "user_dn_template": "{username}@corp.local",
                           "operator_group": "ser2net-operators"}}
        with Server(config) as srv:
            assert "LDAP: the directory server's certificate is now checked" in all_log(srv)
            st, _, page = Browser(srv.base).post("/login", {"username": "jdoe",
                                                             "password": "pw-jdoe"})
            assert st == 401 and "Invalid username or password." in page, st
            log = all_log(srv)
            assert f"LDAP: the certificate of the directory server {d.uri} was not accepted" in log
            assert "`ser2netweb --set-password NAME`" in log and d.binds == [], log[-1500:]
            admin = Browser(srv.base)
            admin.login("admin", PW)
            _, _, page = admin.get("/settings")
            assert (f"the certificate of the directory server {d.uri} was not accepted (unable to "
                    "get local issuer certificate), so LDAP sign-in failed") in page, \
                "no certificate banner in Settings"
            assert "as “CA certificate file” below and save" in page
            assert 'name="ldap_ca_file" value=""' in page and "ldap_tls_insecure" in page
            print("web: an upgraded LDAPS config logs the change; a refused certificate is "
                  "logged and shown in Settings  OK")

            # the form refuses an unusable CA file, then saves a good one
            garbage = os.path.join(folder, "garbage.pem")
            with open(garbage, "wb") as fh:
                fh.write(b"-----BEGIN NOTHING-----\n")
            for path, error in ((os.path.join(folder, "nope.pem"), "can&#39;t be read"),
                                (garbage, "holds no certificate (PEM or DER)")):
                _, _, page = admin.post("/settings/ldap", ldap_form(d.uri, ldap_ca_file=path))
                assert "Not saved: the CA certificate file" in page and error in page, path
                assert srv.saved_config()["ldap"].get("ca_file", "") == ""
            # ... but not when it switches LDAP off: then the file isn't used
            missing = os.path.join(folder, "nope.pem")
            _, _, page = admin.post("/settings/ldap", ldap_form(d.uri, ldap_enabled=None,
                                                                ldap_ca_file=missing))
            assert "LDAP settings saved." in page, page[page.find("banner"):][:300]
            saved = srv.saved_config()["ldap"]
            assert saved["enabled"] is False and saved["ca_file"] == missing, saved
            _, _, page = admin.post("/settings/ldap", ldap_form(d.uri, ldap_ca_file=pki.ca.path))
            assert "LDAP settings saved." in page and "was not accepted" not in page
            saved = srv.saved_config()["ldap"]
            assert saved["ca_file"] == pki.ca.path and saved["tls_verify"] is True, saved
            Browser(srv.base).login("jdoe", "pw-jdoe")
            assert d.tls_binds == ["jdoe@corp.local"]
            # a form from before these fields keeps them
            _, _, page = admin.post("/settings/ldap", ldap_form(d.uri, ldap_tls_shown=None,
                                                                ldap_ca_file=None))
            assert "LDAP settings saved." in page
            assert srv.saved_config()["ldap"]["ca_file"] == pki.ca.path
            print("web: the CA file is checked when LDAP is saved on (not when switched off); "
                  "with it the sign-in works; an older form keeps it  OK")

            # the insecure opt-out: saved, warned about, logged
            _, _, page = admin.post("/settings/ldap", ldap_form(d.uri, ldap_tls_insecure="on"))
            assert "LDAP settings saved." in page
            assert srv.saved_config()["ldap"]["tls_verify"] is False
            assert "The directory server’s certificate is not checked" in page
            assert "WARNING: LDAP: the directory server's certificate is no longer checked" in \
                all_log(srv)
            Browser(srv.base).login("jdoe", "pw-jdoe")      # any certificate now
            _, _, page = admin.post("/settings/ldap", ldap_form(d.uri.replace("ldaps", "ldap")))
            assert "Passwords go to the directory unencrypted" in page, "no plain-LDAP warning"
            assert "certificate is not checked" not in page
            print("web: the insecure opt-out and plain ldap:// are warned about  OK")
        config["ldap"].update(tls_verify=False, ca_file="")
        with Server(config) as srv:
            log = all_log(srv)
            assert "WARNING: LDAP: the directory server's certificate is not checked" in log
            assert "certificate is now checked" not in log
        print("start-up: a WARNING while the certificate is not checked  OK")


def main():
    test_settings()
    test_failure_reasons()
    test_messages()
    if not need("ldap3", "cryptography", what="LDAPS / StartTLS against a test directory "
                                              "over real TLS, and through the web UI"):
        print("\nPASS: LDAP TLS certificate checks (pure parts)")
        return
    folder = tempfile.mkdtemp(prefix="s2n_ldaptls_")
    try:
        pki = Pki(folder)
        test_ldaps(pki)
        test_start_tls(pki)
        test_ca_file_problems(pki, folder)
        test_referrals(pki)
        test_web(pki, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("\nPASS: LDAP TLS certificate checks")


if __name__ == "__main__":
    main()
