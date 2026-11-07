"""LDAP/AD authentication on top of RBAC (Phase 2).

The role-mapping and shadow-user logic are pure (always run in CI): a group setting
matches whole group names only — the full DN, its leading RDNs or the group's own name
(the CN), in any letter case with Turkish İ/I/ı/i as one letter — never a part of one.
The bind/search flow is exercised with a fake connection factory and against a small
LDAP server speaking the real protocol (_ldap_server.py), which answers like Active
Directory where needed: sign-in names that are no DN (user@domain, DOMAIN\\user), the
RootDSE, nested groups. Those parts need ldap3 (the escape helpers the code uses live in
it): without it they are skipped with a SKIP line (tests/run_all.py repeats it), and with
SER2NET_OPTIONAL_TESTS=1 the test fails instead. Run: python3 tests/test_ldap.py
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _names import OTHER_GROUP, SAME_GROUP
from _optional import need

from app.config import AccountNameError, AppConfig, LdapSettings, User
from app.web import ldap_auth

OPER_GROUP = "cn=ser2net-operators,ou=groups,dc=ex,dc=com"
ADMIN_GROUP = "cn=ser2net-admins,ou=groups,dc=ex,dc=com"


def test_role_for_groups():
    s = LdapSettings(admin_group="ser2net-admins", operator_group="ser2net-operators",
                     viewer_group="ser2net-viewers", default_role="")
    assert ldap_auth.role_for_groups([ADMIN_GROUP, OPER_GROUP], s) == "admin"   # highest wins
    assert ldap_auth.role_for_groups([OPER_GROUP], s) == "operator"
    assert ldap_auth.role_for_groups(["cn=ser2net-viewers,ou=g,dc=ex"], s) == "viewer"
    assert ldap_auth.role_for_groups(["cn=other,ou=g"], s) is None             # no default -> deny
    s.default_role = "viewer"
    assert ldap_auth.role_for_groups(["cn=other"], s) == "viewer"              # falls back to default
    print("role_for_groups: highest role wins; default/deny handled  OK")


def test_whole_names_only():
    s = LdapSettings(admin_group="ser2net-admins", operator_group="ser2net-operators",
                     viewer_group="ser2net-viewers", default_role="")
    # groups whose DN merely contains the configured name are other groups
    for dn in ("cn=not-ser2net-admins,ou=groups,dc=ex,dc=com",
               "cn=ser2net-admins-readonly,ou=groups,dc=ex,dc=com",
               "cn=printers,ou=ser2net-admins,dc=ex,dc=com",
               "cn=ex-ser2net-admins,ou=groups,dc=ex,dc=com",
               "CN=ser2net-admins-approvers,OU=Groups,DC=corp,DC=local"):
        assert ldap_auth.role_for_groups([dn], s) is None, dn
    assert ldap_auth.role_for_groups(["cn=ex-ser2net-admins,ou=g,dc=ex",
                                      "cn=ser2net-viewers,ou=g,dc=ex"], s) == "viewer"
    short = LdapSettings(admin_group="admin")
    for dn in ("cn=sysadmin-trainees,ou=groups,dc=ex",
               "CN=Staff,OU=Groups,DC=administration,DC=local"):
        assert ldap_auth.role_for_groups([dn], short) is None, dn
    assert ldap_auth.role_for_groups(["CN=Domain Admins,CN=Users,DC=corp,DC=local"],
                                     LdapSettings(admin_group="admins")) is None
    fallback = LdapSettings(admin_group="ser2net-admins", default_role="viewer")
    assert ldap_auth.role_for_groups(["cn=not-ser2net-admins,ou=g"], fallback) == "viewer"
    print("group settings: a group that only contains the name is another group  OK")


def test_dn_forms():
    group = "CN=ser2net-admins,OU=Groups,DC=corp,DC=local"
    for setting in ("ser2net-admins", "SER2NET-ADMINS", "  ser2net-admins ", "cn=ser2net-admins",
                    "CN=ser2net-admins, OU=Groups", "cn=ser2net-admins,ou=groups,dc=corp,dc=local",
                    "CN = ser2net-admins , OU = Groups , DC = corp , DC = local"):
        assert ldap_auth.group_matches(setting, group), setting
    for setting in ("ou=groups,dc=corp,dc=local", "cn=ser2net-admins,ou=other",
                    "cn=ser2net-admins,ou=groups,dc=corp,dc=local,dc=x", "groups", "corp",
                    "ser2net", "admins", "", "   "):
        assert not ldap_auth.group_matches(setting, group), setting
    # escapes in a DN: a comma inside the CN, UTF-8 bytes as hex pairs
    smith = r"CN=Smith\, John,OU=Groups,DC=corp,DC=local"
    assert ldap_auth.group_matches("Smith, John", smith)
    assert not ldap_auth.group_matches("Smith", smith)
    assert ldap_auth.group_name(smith) == "Smith, John"
    hexed = r"CN=\C4\B0\C5\9Fletme,OU=Gruplar,DC=corp,DC=local"
    assert ldap_auth.group_matches("İşletme", hexed)
    assert ldap_auth.group_matches("cn=işletme,ou=GRUPLAR", hexed)
    # a group attribute with plain names instead of DNs: the whole name
    assert ldap_auth.group_matches("ser2net-admins", "ser2net-admins")
    assert not ldap_auth.group_matches("admins", "ser2net-admins")
    print("group settings: the CN, cn=..., leading RDNs or the full DN; escapes decoded  OK")


def test_turkish_names():
    for typed, stored in SAME_GROUP:
        dn = f"CN={stored},OU=Gruplar,DC=corp,DC=local"
        assert ldap_auth.group_matches(typed, dn), (typed, dn)
        assert ldap_auth.role_for_groups([dn], LdapSettings(operator_group=typed)) == "operator"
        assert ldap_auth.group_matches(f"cn={typed},ou=gruplar", dn), (typed, dn)
    for typed, stored in OTHER_GROUP:
        assert not ldap_auth.group_matches(typed, f"CN={stored},OU=Gruplar,DC=corp,DC=local"), \
            (typed, stored)
    assert ldap_auth.fold("KALİTE") == ldap_auth.fold("kalite") == ldap_auth.fold("Kalıte")
    print("group names: Turkish İ/I/ı/i, NFD text and no-break spaces compare as typed  OK")


def test_partial_match_noted():
    """The earlier rule (the text anywhere in the DN) is still computed, to warn about
    settings that only worked through it."""
    admins = "CN=ser2net-admins,OU=Groups,DC=corp,DC=local"
    s = LdapSettings(admin_group="admins", operator_group="ser2net-operators")
    d = ldap_auth.role_decision([admins], s)
    assert d.role is None and d.partial == ("admin_group", "admins", admins), d
    assert d.partial_role == "admin" and ldap_auth.group_name(admins) == "ser2net-admins"
    d = ldap_auth.role_decision([admins, "CN=ser2net-operators,OU=Groups,DC=corp,DC=local"], s)
    assert (d.role, d.setting, d.partial_role) == ("operator", "operator_group", "admin"), d
    s.default_role = "viewer"
    d = ldap_auth.role_decision([admins], s)
    assert d.role == "viewer" and not d.setting and d.partial_role == "admin", d
    # nothing to note while the whole-name rule gives as much
    whole = LdapSettings(admin_group="ser2net-admins", viewer_group="viewers")
    d = ldap_auth.role_decision([admins, "CN=all-viewers,OU=Groups,DC=corp,DC=local"], whole)
    assert d.role == "admin" and d.partial is None, d
    names = ldap_auth.group_names([f"CN=g{i},OU=G,DC=x" for i in range(12)])
    assert names.startswith("'g0', 'g1', ") and names.endswith("'g9' (+2 more)"), names
    assert ldap_auth.group_names([]) == "none"
    assert ldap_auth.group_names(["cn=" + "x" * 100]) == repr("x" * 63 + "…")
    print("role_decision: notes a setting that is only part of a group name  OK")


def test_upsert_ldap_user():
    cfg = AppConfig.from_dict({"users": [
        {"username": "admin", "password_hash": "x", "role": "admin", "source": "local"}]})
    u = ldap_auth.upsert_ldap_user(cfg, "alice", "operator")
    assert u.username == "alice" and u.role == "operator" and u.source == "ldap"
    assert u.password_hash == "" and isinstance(u.pwd_version, int) and len(cfg.users) == 2
    v0 = u.pwd_version
    same = ldap_auth.upsert_ldap_user(cfg, "alice", "operator")  # unchanged -> no bump
    assert same is u and same.pwd_version == v0
    promoted = ldap_auth.upsert_ldap_user(cfg, "alice", "admin")  # role change -> bump
    assert promoted.role == "admin" and promoted.pwd_version == v0 + 1
    # deleted, then back at the next sign-in: a new random version, not the old cookies' one
    cfg.users = [x for x in cfg.users if x.username != "alice"]
    again = ldap_auth.upsert_ldap_user(cfg, "alice", "viewer")
    assert again is not promoted and again.pwd_version != promoted.pwd_version
    print("upsert_ldap_user: creates shadow (random first version), refreshes role, bumps "
          "pwd_version on change  OK")


def test_upsert_never_takes_a_local_account():
    """An LDAP/OIDC sign-in under the name of a local account — in any letter case,
    Turkish İ/ı too — is refused, and the local account stays exactly as it was (no
    source change, no role from the directory, no session bump), so the only admin
    can't be lost."""
    cfg = AppConfig.from_dict({"users": [
        {"username": "admin", "password_hash": "h", "role": "admin", "pwd_version": 7,
         "source": "local"},
        {"username": "ismail", "password_hash": "h2", "role": "operator", "pwd_version": 2,
         "source": "local"},
        {"username": "jdoe", "password_hash": "", "role": "viewer", "pwd_version": 3,
         "source": "ldap"}]})
    before = [dataclasses.asdict(u) for u in cfg.users]
    for name, source, local in (("admin", "oidc", "admin"), ("admin", "ldap", "admin"),
                                ("ADMIN", "ldap", "admin"), ("Admin", "oidc", "admin"),
                                ("İSMAİL", "ldap", "ismail"), ("Ismail", "oidc", "ismail")):
        try:
            cfg.upsert_external_user(name, "viewer", source)
        except AccountNameError as e:
            assert f"the local account '{local}' has this name" in str(e), e
        else:
            raise AssertionError(f"{source} sign-in as {name!r} took over a local account")
    assert [dataclasses.asdict(u) for u in cfg.users] == before
    assert cfg.admin_count() == 1
    # named(): every spelling, the exact one first; of one source if asked
    cfg.users.append(User(username="JDoe", source="ldap", role="viewer"))
    assert [u.username for u in cfg.named("JDOE")] == ["jdoe", "JDoe"]
    assert [u.username for u in cfg.named("JDoe")] == ["JDoe", "jdoe"]
    assert cfg.named("jdoe", "local") == [] and cfg.named("ADMIN", "local")[0].username == "admin"
    assert [u.username for u in cfg.named("İsmail", "local")] == ["ismail"]
    print("upsert_external_user: never takes over a local account (any letter case)  OK")


# ---- authenticate() flow with a fake connection factory (needs ldap3 for escapes) ----

class _Attr:
    def __init__(self, values):
        self.values = values


class _Entry:
    def __init__(self, dn, groups, attr="memberOf"):
        self.entry_dn = dn
        self._a = {attr: _Attr(groups)}

    def __getitem__(self, key):
        return self._a.get(key, _Attr([]))


class _Conn:
    def __init__(self, entries=None):
        self.entries = entries or []

    def search(self, *a, **k):
        return True

    def unbind(self):
        pass


def _make_factory(directory, svc_dn, svc_pw):
    """directory: {dn: (password, [groups])}. Mimics ldap3 auto_bind: a bad password
    raises (as ldap3 would on AUTO_BIND), a good one returns a connection."""
    def factory(server, user, password):
        if user == svc_dn:
            if password != svc_pw:
                raise RuntimeError("service bind failed")
            # the service conn can search; pre-load all entries it might return
            entries = [_Entry(dn, groups) for dn, (_pw, groups) in directory.items()]
            return _Conn(entries=entries)
        rec = directory.get(user)
        if rec is None or rec[0] != password:
            raise RuntimeError("invalid credentials")
        return _Conn(entries=[_Entry(user, rec[1])])
    return factory


def test_empty_password_never_binds():
    """An empty password (or user name) is refused before anything is sent: a simple bind
    with an empty password is an anonymous bind, which many directories accept. Needs no
    ldap3."""
    binds = []

    def factory(server, user, password):
        binds.append(user)
        raise AssertionError("an empty password must not reach the directory")

    for s in (LdapSettings(enabled=True, server_uri="ldap://h",
                           user_dn_template="uid={username},ou=people,dc=ex,dc=com"),
              LdapSettings(enabled=True, server_uri="ldaps://h", bind_dn="cn=svc,dc=ex",
                           bind_password="svcpw", user_search_base="dc=ex")):
        for user, pw in (("alice", ""), ("", "secret"), ("", "")):
            assert ldap_auth.authenticate(s, user, pw, server=object(), conn_factory=factory) is None
    assert binds == [], binds
    print("authenticate: an empty password or user name never binds (no anonymous bind)  OK")


def test_authenticate_flows():
    user_dn = "uid=alice,ou=people,dc=ex,dc=com"
    directory = {user_dn: ("secret", [OPER_GROUP])}

    # direct-bind mode
    s = LdapSettings(enabled=True, server_uri="ldap://h",
                     user_dn_template="uid={username},ou=people,dc=ex,dc=com")
    f = _make_factory(directory, svc_dn="", svc_pw="")
    assert ldap_auth.authenticate(s, "alice", "secret", server=object(), conn_factory=f) == [OPER_GROUP]
    assert ldap_auth.authenticate(s, "alice", "WRONG", server=object(), conn_factory=f) is None
    print("authenticate direct-bind: groups on success, None on a bad password  OK")

    # search+bind mode
    s2 = LdapSettings(enabled=True, server_uri="ldap://h", bind_dn="cn=svc,dc=ex",
                      bind_password="svcpw", user_search_base="ou=people,dc=ex,dc=com",
                      user_search_filter="(uid={username})")
    f2 = _make_factory(directory, svc_dn="cn=svc,dc=ex", svc_pw="svcpw")
    assert ldap_auth.authenticate(s2, "alice", "secret", server=object(), conn_factory=f2) == [OPER_GROUP]
    assert ldap_auth.authenticate(s2, "alice", "WRONG", server=object(), conn_factory=f2) is None
    print("authenticate search+bind: finds user, verifies password, returns groups  OK")


# ---- authenticate() against a small LDAP server (real protocol, real ldap3 client) ----

BASE = "DC=corp,DC=local"
JDOE = "CN=John Doe,OU=People," + BASE
ASMITH = "CN=Ann Smith,OU=People," + BASE        # no userPrincipalName attribute
OPS = "CN=ser2net-operators,OU=Groups," + BASE
ADMINS = "CN=ser2net-admins,OU=Groups," + BASE
PLANT = "CN=Plant Staff,OU=Groups," + BASE        # has OPS as a member: jdoe is in it nested
SVC = "CN=svc-ser2net,OU=Service," + BASE
PARTITIONS = "CN=Partitions,CN=Configuration," + BASE   # a crossRef per domain of the forest
CHILD = "DC=child," + BASE                               # a child domain's naming context


def ad_directory(**kw):
    from _ldap_server import FakeDirectory
    entries = {
        JDOE: {"objectClass": ["user"], "sAMAccountName": ["jdoe"],
               "userPrincipalName": ["jdoe@corp.local"], "memberOf": [OPS]},
        ASMITH: {"objectClass": ["user"], "sAMAccountName": ["asmith"], "memberOf": [ADMINS]},
        OPS: {"objectClass": ["group"], "member": [JDOE]},
        ADMINS: {"objectClass": ["group"], "member": [ASMITH]},
        PLANT: {"objectClass": ["group"], "member": [OPS]},
        SVC: {"objectClass": ["user"], "sAMAccountName": ["svc-ser2net"]},
        "CN=CORP," + PARTITIONS: {"objectClass": ["crossRef"], "nETBIOSName": ["CORP"],
                                  "nCName": [BASE]},
        "CN=CHILD," + PARTITIONS: {"objectClass": ["crossRef"], "nETBIOSName": ["CHILD"],
                                   "nCName": [CHILD]},
    }
    return FakeDirectory(entries, {JDOE: "pw-jdoe", ASMITH: "pw-ann", SVC: "pw-svc"}, **kw)


def test_ad_sign_in_names():
    """Direct bind with an AD sign-in name that is no DN (the UI's {username}@corp.local
    or CORP\\{username}): the account's entry is looked up after the bind."""
    import ldap3
    logs: list[str] = []
    with ad_directory() as d:
        # what reading the groups used to do: the UPN as a search base, which AD refuses
        conn = ldap3.Connection(ldap3.Server(d.uri, get_info=ldap3.NONE), user="jdoe@corp.local",
                                password="pw-jdoe", auto_bind=ldap3.AUTO_BIND_NO_TLS)
        conn.search("jdoe@corp.local", "(objectClass=*)", search_scope=ldap3.BASE,
                    attributes=["memberOf"])
        assert conn.result["description"] == "invalidDNSyntax" and not conn.entries
        conn.unbind()
        d.searches.clear()

        upn = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="{username}@corp.local")
        groups = ldap_auth.authenticate(upn, "jdoe", "pw-jdoe", logs.append)
        assert groups == [OPS] and not groups.lookup_failed, (groups, logs)
        assert ("", 0, "(objectClass=*)") in d.searches               # the RootDSE
        assert (BASE, 2, "(userPrincipalName=jdoe@corp.local)") in d.searches, d.searches
        assert not any(b == "jdoe@corp.local" for b, _s, _f in d.searches)
        assert ldap_auth.role_for_groups(groups, LdapSettings(operator_group="ser2net-operators")) \
            == "operator"
        assert ldap_auth.authenticate(upn, "jdoe", "wrong", logs.append) is None
        # an account without userPrincipalName: the implicit UPN, by sAMAccountName
        assert ldap_auth.authenticate(upn, "asmith", "pw-ann", logs.append) == [ADMINS]
        assert (BASE, 2, "(sAMAccountName=asmith)") in d.searches
        # under the user search base when one is set
        d.searches.clear()
        upn.user_search_base = "OU=People," + BASE
        assert ldap_auth.authenticate(upn, "jdoe", "pw-jdoe", logs.append) == [OPS]
        assert d.searches == [(upn.user_search_base, 2, "(userPrincipalName=jdoe@corp.local)")]
        # DOMAIN\\user
        nt4 = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="CORP\\{username}")
        assert ldap_auth.authenticate(nt4, "jdoe", "pw-jdoe", logs.append) == [OPS]
        assert (PARTITIONS, 1, "(nETBIOSName=CORP)") in d.searches
        assert (BASE, 2, "(sAMAccountName=jdoe)") in d.searches
        assert not [m for m in logs if "could not be read" in m], logs
    print("authenticate: AD user@domain / DOMAIN\\user binds find the account's groups  OK")


def test_ad_lookup_problems():
    """A group lookup that fails is logged and flagged — the bind itself succeeded."""
    from _ldap_server import FakeDirectory

    child_asmith = "CN=asmith,OU=People," + CHILD
    child_staff = "CN=child-staff,OU=Groups," + CHILD

    class Forest(FakeDirectory):
        """Also takes sign-in names of other domains, as a forest (or a trust) would."""
        names = {"asmith@child.corp.local": ASMITH, "child\\asmith": child_asmith,
                 "other\\jdoe": JDOE, "jdoe": JDOE}

        def _bind_dn(self, name):
            return self.names.get(name.lower()) or super()._bind_dn(name)

    logs: list[str] = []
    with ad_directory() as d:
        s = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="{username}@corp.local",
                         user_search_base="OU=Nowhere," + BASE)
        groups = ldap_auth.authenticate(s, "jdoe", "pw-jdoe", logs.append)
        assert groups == [] and groups.lookup_failed, groups
        assert "'jdoe'" in logs[-1] and "noSuchObject (32)" in logs[-1], logs
    with ad_directory() as base:
        entries = {dn: dict(a) for dn, a in base.entries.items()}
    entries[child_asmith] = {"objectClass": ["user"], "sAMAccountName": ["asmith"],
                             "memberOf": [child_staff]}
    with Forest(entries, {JDOE: "pw-jdoe", ASMITH: "pw-ann", child_asmith: "pw-child"}) as d:
        # another domain's UPN: no sAMAccountName fallback in this domain (where
        # "asmith" could be someone else)
        child = LdapSettings(enabled=True, server_uri=d.uri,
                             user_dn_template="{username}@child.corp.local")
        groups = ldap_auth.authenticate(child, "asmith", "pw-ann", logs.append)
        assert groups == [] and groups.lookup_failed, groups
        assert not any("sAMAccountName" in f for _b, _s, f in d.searches), d.searches
        assert "userPrincipalName=asmith@child.corp.local is not under" in logs[-1], logs
        # DOMAIN\user: in that domain (its crossRef), not in the server's own domain
        nt4 = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="CHILD\\{username}")
        assert ldap_auth.authenticate(nt4, "asmith", "pw-child", logs.append) == [child_staff]
        assert (CHILD, 2, "(sAMAccountName=asmith)") in d.searches, d.searches
        nt4.user_search_base = "OU=People," + BASE     # a base in another domain
        groups = ldap_auth.authenticate(nt4, "asmith", "pw-child", logs.append)
        assert groups == [] and groups.lookup_failed, groups
        assert "is not in the domain 'CHILD'" in logs[-1], logs
        corp = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="CORP\\{username}",
                            user_search_base="OU=People," + CHILD)
        groups = ldap_auth.authenticate(corp, "asmith", "pw-ann", logs.append)
        assert groups == [] and "is not in the domain 'CORP'" in logs[-1], logs
        other = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="OTHER\\{username}")
        groups = ldap_auth.authenticate(other, "jdoe", "pw-jdoe", logs.append)
        assert groups == [] and "names no domain 'OTHER'" in logs[-1], logs
        # a bare name is none of the forms an account can be found by
        bare = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="{username}")
        groups = ldap_auth.authenticate(bare, "jdoe", "pw-jdoe", logs.append)
        assert groups == [] and "is no DN, user@domain or DOMAIN" in logs[-1], logs
    # two entries for one account name: no guessing
    with ad_directory() as d:
        d.entries["CN=jdoe2,OU=Other," + BASE] = {"samaccountname": ["jdoe"], "memberof": [ADMINS]}
        nt4 = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="CORP\\{username}")
        groups = ldap_auth.authenticate(nt4, "jdoe", "pw-jdoe", logs.append)
        assert groups == [] and groups.lookup_failed and "2 entries" in logs[-1], (groups, logs)
    print("authenticate: a failed group lookup is logged; DOMAIN\\user only in that domain  OK")


def test_nested_groups():
    from _ldap_server import FakeDirectory
    logs: list[str] = []
    with ad_directory() as d:
        upn = LdapSettings(enabled=True, server_uri=d.uri, user_dn_template="{username}@corp.local")
        assert ldap_auth.authenticate(upn, "jdoe", "pw-jdoe", logs.append) == [OPS]
        assert not any("1.2.840.113556.1.4.1941" in f for _b, _s, f in d.searches)
        upn.nested_groups = True
        groups = ldap_auth.authenticate(upn, "jdoe", "pw-jdoe", logs.append)
        assert groups == [OPS, PLANT] and not groups.lookup_failed, groups
        assert (BASE, 2, f"(member:1.2.840.113556.1.4.1941:={JDOE})") in d.searches, d.searches
        assert ldap_auth.role_for_groups(groups, LdapSettings(admin_group="plant staff")) == "admin"
        # search + bind: the service account searches
        sb = LdapSettings(enabled=True, server_uri=d.uri, bind_dn=SVC, bind_password="pw-svc",
                          user_search_base="OU=People," + BASE,
                          user_search_filter="(sAMAccountName={username})", nested_groups=True)
        assert ldap_auth.authenticate(sb, "jdoe", "pw-jdoe", logs.append) == [OPS, PLANT]
        assert ldap_auth.authenticate(sb, "jdoe", "wrong", logs.append) is None
        # a DN template on AD works as before, with nested groups too
        dn = LdapSettings(enabled=True, server_uri=d.uri, nested_groups=True,
                          user_dn_template="CN={username},OU=People," + BASE)
        assert ldap_auth.authenticate(dn, "John Doe", "pw-jdoe", logs.append) == [OPS, PLANT]
        assert not [m for m in logs if "could not be read" in m], logs
    # OpenLDAP: DN binds as before; the AD-only rule simply finds nothing more
    people = "ou=people,dc=ex,dc=com"
    alice = "uid=alice," + people
    entries = {alice: {"objectClass": ["inetOrgPerson"], "uid": ["alice"],
                       "memberOf": [OPER_GROUP]},
               "cn=svc,dc=ex,dc=com": {"objectClass": ["person"]}}
    with FakeDirectory(entries, {alice: "secret", "cn=svc,dc=ex,dc=com": "svcpw"}, ad=False,
                       base="dc=ex,dc=com") as d:
        plain = LdapSettings(enabled=True, server_uri=d.uri,
                             user_dn_template="uid={username}," + people)
        assert ldap_auth.authenticate(plain, "alice", "secret", logs.append) == [OPER_GROUP]
        assert d.searches == [(alice, 0, "(objectClass=*)")], d.searches
        sb = LdapSettings(enabled=True, server_uri=d.uri, bind_dn="cn=svc,dc=ex,dc=com",
                          bind_password="svcpw", user_search_base=people, nested_groups=True)
        groups = ldap_auth.authenticate(sb, "alice", "secret", logs.append)
        assert groups == [OPER_GROUP] and not groups.lookup_failed, groups
        assert not [m for m in logs if "could not be read" in m], logs
    print("authenticate: nested groups (AD, opt-in) in every bind mode; OpenLDAP unchanged  OK")


def main():
    test_role_for_groups()
    test_whole_names_only()
    test_dn_forms()
    test_turkish_names()
    test_partial_match_noted()
    test_upsert_ldap_user()
    test_upsert_never_takes_a_local_account()
    test_empty_password_never_binds()
    if need("ldap3", what="authenticate() with ldap3: bind, search, AD sign-in names, "
                          "nested groups over the LDAP protocol"):
        test_authenticate_flows()
        test_ad_sign_in_names()
        test_ad_lookup_problems()
        test_nested_groups()
    print("\nPASS: LDAP/AD authentication")


if __name__ == "__main__":
    main()
