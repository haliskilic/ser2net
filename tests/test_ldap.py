"""LDAP/AD authentication on top of RBAC (Phase 2).

The role-mapping and shadow-user logic are pure (always run in CI): a group setting
matches whole group names only — the full DN, its leading RDNs or the group's own name
(the CN), in any letter case with Turkish İ/I/ı/i as one letter — never a part of one.
The actual bind/search flow is exercised with a fake connection factory, so no real
LDAP server is needed; it is skipped only if ldap3 isn't importable (the escape helpers
the code uses live in ldap3). Run: python3 tests/test_ldap.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _names import OTHER_GROUP, SAME_GROUP

from app.config import AppConfig, LdapSettings
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


def test_authenticate_flows():
    try:
        import ldap3  # noqa: F401  (the code uses ldap3's escape helpers)
    except ImportError:
        print("skip: ldap3 not installed (pure logic above already covered)")
        return

    user_dn = "uid=alice,ou=people,dc=ex,dc=com"
    directory = {user_dn: ("secret", [OPER_GROUP])}

    # direct-bind mode
    s = LdapSettings(enabled=True, server_uri="ldap://h",
                     user_dn_template="uid={username},ou=people,dc=ex,dc=com")
    f = _make_factory(directory, svc_dn="", svc_pw="")
    assert ldap_auth.authenticate(s, "alice", "secret", server=object(), conn_factory=f) == [OPER_GROUP]
    assert ldap_auth.authenticate(s, "alice", "WRONG", server=object(), conn_factory=f) is None
    assert ldap_auth.authenticate(s, "alice", "", server=object(), conn_factory=f) is None  # empty pw
    print("authenticate direct-bind: groups on success, None on bad/empty password  OK")

    # search+bind mode
    s2 = LdapSettings(enabled=True, server_uri="ldap://h", bind_dn="cn=svc,dc=ex",
                      bind_password="svcpw", user_search_base="ou=people,dc=ex,dc=com",
                      user_search_filter="(uid={username})")
    f2 = _make_factory(directory, svc_dn="cn=svc,dc=ex", svc_pw="svcpw")
    assert ldap_auth.authenticate(s2, "alice", "secret", server=object(), conn_factory=f2) == [OPER_GROUP]
    assert ldap_auth.authenticate(s2, "alice", "WRONG", server=object(), conn_factory=f2) is None
    print("authenticate search+bind: finds user, verifies password, returns groups  OK")


def main():
    test_role_for_groups()
    test_whole_names_only()
    test_dn_forms()
    test_turkish_names()
    test_partial_match_noted()
    test_upsert_ldap_user()
    test_authenticate_flows()
    print("\nPASS: LDAP/AD authentication")


if __name__ == "__main__":
    main()
