"""LDAP/AD authentication on top of RBAC (Phase 2).

The role-mapping and shadow-user logic are pure (always run in CI). The actual
bind/search flow is exercised with a fake connection factory, so no real LDAP
server is needed; it is skipped only if ldap3 isn't importable (the escape helpers
the code uses live in ldap3). Run: python3 tests/test_ldap.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


def test_upsert_ldap_user():
    cfg = AppConfig.from_dict({"users": [
        {"username": "admin", "password_hash": "x", "role": "admin", "source": "local"}]})
    u = ldap_auth.upsert_ldap_user(cfg, "alice", "operator")
    assert u.username == "alice" and u.role == "operator" and u.source == "ldap"
    assert u.password_hash == "" and u.pwd_version == 1 and len(cfg.users) == 2
    v0 = u.pwd_version
    same = ldap_auth.upsert_ldap_user(cfg, "alice", "operator")  # unchanged -> no bump
    assert same is u and same.pwd_version == v0
    promoted = ldap_auth.upsert_ldap_user(cfg, "alice", "admin")  # role change -> bump
    assert promoted.role == "admin" and promoted.pwd_version == v0 + 1
    print("upsert_ldap_user: creates shadow, refreshes role, bumps pwd_version on change  OK")


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
    test_upsert_ldap_user()
    test_authenticate_flows()
    print("\nPASS: LDAP/AD authentication")


if __name__ == "__main__":
    main()
