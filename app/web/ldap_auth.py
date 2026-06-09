"""Optional LDAP / Active Directory authentication, layered on the RBAC model.

A user authenticates by binding to the directory; their group membership maps to a
ser2net role. On success a 'shadow' local User (source='ldap', no password) is
upserted so the rest of the system — sessions, role checks, the Users panel — works
uniformly; the role is refreshed from LDAP groups on every login.

ldap3 is an OPTIONAL dependency, imported lazily: if it's absent, LDAP login is
disabled with a log line and local accounts keep working. The actual bind/search is
isolated so the role-mapping and shadow-user logic can be unit-tested without ldap3.
"""
from __future__ import annotations

import contextlib

from ..config import User


def role_for_groups(groups, settings) -> str | None:
    """Highest ser2net role granted by a user's group DNs, or None to deny (the user
    authenticated but is in no mapped group and no default_role is set)."""
    lowered = [str(g).lower() for g in groups]

    def member(group_name: str) -> bool:
        g = (group_name or "").strip().lower()
        return bool(g) and any(g in dn for dn in lowered)

    if member(settings.admin_group):
        return "admin"
    if member(settings.operator_group):
        return "operator"
    if member(settings.viewer_group):
        return "viewer"
    return settings.default_role or None


def upsert_ldap_user(config, username: str, role: str) -> User:
    """Create or refresh the shadow account for an LDAP user. Bumps pwd_version when
    the role or source changes so stale-role sessions are revoked."""
    u = config.get_user(username)
    if u is None:
        u = User(username=username, password_hash="", role=role, source="ldap", pwd_version=1)
        config.users.append(u)
    elif u.role != role or u.source != "ldap":
        u.role = role
        u.source = "ldap"
        u.pwd_version += 1
    return u


def _entry_groups(entry, attr) -> list[str]:
    try:
        return [str(v) for v in entry[attr].values]
    except Exception:
        return []


def authenticate(settings, username: str, password: str, logger=None,
                 server=None, conn_factory=None) -> list[str] | None:
    """Bind to LDAP as `username`/`password`; return the user's group DNs on success
    or None on failure. Never raises on a bad bind. `server`/`conn_factory` are
    injection points for tests (an ldap3 MOCK_SYNC server). Returns None if ldap3 is
    not installed."""
    log = logger or (lambda _m: None)
    if not username or not password:   # empty password must not become an anonymous bind
        return None
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
        from ldap3.utils.dn import escape_rdn
    except ImportError:
        log("LDAP enabled but ldap3 is not installed; LDAP login disabled (pip install ldap3)")
        return None

    def _default_factory(srv, user, pw):
        ab = ldap3.AUTO_BIND_TLS_BEFORE_BIND if settings.start_tls else ldap3.AUTO_BIND_NO_TLS
        return ldap3.Connection(srv, user=user, password=pw, auto_bind=ab)

    factory = conn_factory or _default_factory
    try:
        if server is None:
            # connect_timeout so a dead/unreachable directory fails the login fast
            # instead of hanging the worker thread on the OS socket timeout
            server = ldap3.Server(settings.server_uri, get_info=ldap3.NONE, connect_timeout=5,
                                  use_ssl=settings.server_uri.lower().startswith("ldaps"))
        if settings.user_dn_template.strip():
            # direct bind: the supplied credentials bind as the templated DN
            user_dn = settings.user_dn_template.format(username=escape_rdn(username))
            conn = factory(server, user_dn, password)
            groups = []
            with contextlib.suppress(Exception):
                conn.search(user_dn, "(objectClass=*)", search_scope=ldap3.BASE,
                            attributes=[settings.group_attr])
                if conn.entries:
                    groups = _entry_groups(conn.entries[0], settings.group_attr)
            conn.unbind()
            return groups
        # search+bind: bind as the service account, find the user, then bind as them
        svc = factory(server, settings.bind_dn, settings.bind_password)
        flt = settings.user_search_filter.format(username=escape_filter_chars(username))
        svc.search(settings.user_search_base, flt, attributes=[settings.group_attr])
        if not svc.entries:
            svc.unbind()
            return None
        entry = svc.entries[0]
        user_dn, groups = entry.entry_dn, _entry_groups(entry, settings.group_attr)
        svc.unbind()
        user_conn = factory(server, user_dn, password)   # verify the password
        user_conn.unbind()
        return groups
    except Exception as e:  # bad bind / server error -> denied, never crash the login
        log(f"LDAP authentication failed for {username!r}: {type(e).__name__}: {e}")
        return None
