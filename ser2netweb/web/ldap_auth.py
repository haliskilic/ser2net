"""Optional LDAP / Active Directory authentication, layered on the RBAC model.

A user authenticates by binding to the directory; their group membership maps to a
ser2netweb role. On success a 'shadow' local User (source='ldap', no password) is
upserted so the rest of the system — sessions, role checks, the Users panel — works
uniformly; the role is refreshed from LDAP groups on every login.

ldap3 is an OPTIONAL dependency, imported lazily: if it's absent or fails to import,
LDAP login is disabled with a log line saying which (ser2netweb.optional) and local accounts
keep working. The actual bind/search is isolated so the role-mapping and shadow-user
logic can be unit-tested without ldap3; the group -> role matching (OIDC claims use it
too) is pure Python.

LDAPS and StartTLS check the directory server's certificate and host name (tls_context:
the system's CAs or ldap.ca_file); ldap3's own default checks nothing. A refused
certificate is logged with what to set (TlsProblem); nothing is sent over that
connection.
"""
from __future__ import annotations

import contextlib
import re
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .. import optional
from ..config import ROLE_RANK, User, fold  # fold: how group names compare

if TYPE_CHECKING:
    from ..config import AppConfig, LdapSettings, OidcSettings

# LDAP_MATCHING_RULE_IN_CHAIN: Active Directory follows nested groups for this filter
IN_CHAIN_RULE = "1.2.840.113556.1.4.1941"

# ---------------- group -> role ----------------
# A group setting (admin_group / operator_group / viewer_group, for LDAP and OIDC alike)
# names one group, and only its whole name matches — never a part of it. Letter case and
# extra spaces don't count (fold). It matches a group the directory or the identity
# provider reports when it is
#   * the same text: a full DN, compared RDN by RDN (cn=ser2netweb-admins,ou=groups,
#     dc=corp,dc=local), or the value exactly as the provider sends it;
#   * the group's own name: the value of a DN's first RDN (ser2netweb-admins), the last
#     segment of a path (Keycloak sends /team/ser2netweb-admins) or the part after a
#     DOMAIN\ prefix (AD FS);
#   * the leading RDNs of the DN (cn=ser2netweb-admins,ou=groups) or the trailing segments
#     of a path (team/ser2netweb-admins); a setting starting with / is the whole path.
GROUP_SETTINGS = (("admin_group", "admin"), ("operator_group", "operator"),
                  ("viewer_group", "viewer"))

_DN_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9-]*|[0-9]+(?:\.[0-9]+)*")
_HEX = frozenset("0123456789abcdefABCDEF")


def _split_dn(text: object) -> list[list[tuple[str, str]]] | None:
    """The RDNs of an RFC 4514 DN, each a list of (attribute type, value) with the
    escapes (\\, \\2C, UTF-8 bytes such as \\C4\\B0) decoded, or None when `text` is not
    a DN."""
    s = str(text)
    rdns: list[list[tuple[str, str]]] = []
    avas: list[tuple[str, str]] = []
    i, n = 0, len(s)
    while True:
        eq = s.find("=", i)
        if eq < 0 or not _DN_TYPE.fullmatch(s[i:eq].strip()):
            return None
        typ, i, raw = s[i:eq].strip().lower(), eq + 1, bytearray()
        while i < n and s[i] not in ",+":
            if s[i] == "\\":
                pair = s[i + 1:i + 3]
                if len(pair) == 2 and set(pair) <= _HEX:
                    raw.append(int(pair, 16))
                    i += 3
                    continue
                if i + 1 >= n:
                    return None
                i += 1
            raw += s[i].encode("utf-8", "surrogatepass")
            i += 1
        avas.append((typ, raw.decode("utf-8", "replace").strip()))
        if i >= n or s[i] == ",":
            rdns.append(avas)
            avas = []
        if i >= n:
            return rdns
        i += 1


def _dn_key(text: object) -> tuple[frozenset[tuple[str, str]], ...] | None:
    rdns = _split_dn(text)
    return None if rdns is None else tuple(frozenset((t, fold(v)) for t, v in rdn)
                                           for rdn in rdns)


def group_matches(setting: str | None, group: str) -> bool:
    """Does this group setting name this group? (the rules above)"""
    want = fold(setting or "")
    if not want:
        return False
    if want == fold(group):
        return True
    have = _dn_key(group)
    if have:
        wanted = _dn_key(setting)
        if wanted:                                   # the full DN or its leading RDNs
            return have[:len(wanted)] == wanted
        return any(value == want for _type, value in have[0])   # the group's own name
    text, typed = str(group).strip(), str(setting).strip()
    if "\\" in text and "\\" not in typed:          # DOMAIN\group
        return fold(text.rsplit("\\", 1)[1]) == want
    if "/" in text or "/" in typed:                 # a path such as Keycloak's /team/group
        path = [fold(p) for p in text.strip("/").split("/")]
        tail = [fold(p) for p in typed.strip("/").split("/")]
        return path == tail if typed.startswith("/") else path[-len(tail):] == tail
    return False


def group_name(group: object) -> str:
    """A group's own name: a DN's first RDN value, a path's last segment, the part
    after DOMAIN\\ — what to enter as the group setting."""
    rdns = _split_dn(group)
    if rdns:
        return rdns[0][0][1]
    text = str(group).strip()
    if "\\" in text:
        return text.rsplit("\\", 1)[1]
    return text.rstrip("/").rsplit("/", 1)[-1] if "/" in text else text


def group_names(groups: Iterable[object], limit: int = 10) -> str:
    """The groups for a log line: their own names, at most `limit`, each cut at 64
    characters."""
    groups = list(groups)
    names = [group_name(g) for g in groups[:limit]]
    text = ", ".join(repr(n if len(n) <= 64 else n[:63] + "…") for n in names) or "none"
    return text + (f" (+{len(groups) - limit} more)" if len(groups) > limit else "")


@dataclass
class RoleDecision:
    """How a user's groups map to a role (role_decision)."""
    role: str | None          # the role they get; None: the sign-in is refused
    setting: str = ""         # the group setting that gave it ("": none, default_role)
    group: str = ""           # the group it matched
    # a group setting that is only part of the name of one of their groups, which counted
    # before group names had to match whole, and would give more than `role`:
    # (setting, its value, that group) and the role it gave
    partial: tuple[str, str, str] | None = None
    partial_role: str = ""


def role_decision(groups: Iterable[str],
                  settings: LdapSettings | OidcSettings) -> RoleDecision:
    """The highest role a user's groups give (LDAP group DNs or OIDC claim values):
    admin_group, then operator_group, then viewer_group, else default_role (blank:
    None, refused)."""
    groups = [str(g) for g in groups]
    decision = RoleDecision(settings.default_role or None)
    for setting, role in GROUP_SETTINGS:
        group = next((g for g in groups if group_matches(getattr(settings, setting), g)), None)
        if group is not None:
            decision = RoleDecision(role, setting, group)
            break
    for setting, role in GROUP_SETTINGS:  # the earlier rule: the text anywhere in a group
        text = (getattr(settings, setting) or "").strip().lower()
        group = next((g for g in groups if text and text in g.lower()), None)
        if group is not None:
            if ROLE_RANK[role] > ROLE_RANK.get(decision.role or "", 0):
                decision.partial = (setting, getattr(settings, setting), group)
                decision.partial_role = role
            break
    return decision


def role_for_groups(groups: Iterable[str],
                    settings: LdapSettings | OidcSettings) -> str | None:
    """Highest ser2netweb role granted by a user's groups, or None to deny (the user
    authenticated but is in no mapped group and no default_role is set)."""
    return role_decision(groups, settings).role


def upsert_ldap_user(config: AppConfig, username: str, role: str) -> User:
    """Create or refresh the shadow account for an LDAP user."""
    return config.upsert_external_user(username, role, "ldap")


# ---------------- TLS: LDAPS and StartTLS ----------------
class CaFileError(ValueError):
    """ldap.ca_file can't be used: unreadable, or no certificate in it."""

    def __init__(self, path: str, reason: str, unreadable: bool) -> None:
        super().__init__(f"{path!r}: {reason}")
        self.path, self.reason, self.unreadable = path, reason, unreadable


def tls_context(settings: LdapSettings) -> ssl.SSLContext:
    """How an LDAPS / StartTLS connection checks the directory server: its certificate
    must chain to a CA this system trusts (its CA bundle; Windows: its certificate
    store) — or, with ca_file set, to a certificate in that file only (PEM, or DER as
    Windows exports it) — and name the host of the server URI (OpenSSL's check, for DNS
    names and IP addresses alike). tls_verify off: no check at all (insecure). Raises
    CaFileError."""
    if not settings.tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    path = settings.ca_file.strip()
    if not path:
        return ssl.create_default_context()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise CaFileError(path, f"can't read it: {e.strerror or e}", True) from None
    trusted: ssl.SSLContext | None = None
    try:   # PEM through OpenSSL's reader (several certificates, comments), else DER
        if b"-----BEGIN" in data:
            trusted = ssl.create_default_context(cafile=path)
        elif data.strip():   # (empty cadata would mean the system's CAs)
            trusted = ssl.create_default_context(cadata=data)
    except (ssl.SSLError, ValueError, OSError):
        trusted = None
    if trusted is None or not trusted.cert_store_stats()["x509"]:
        raise CaFileError(path, "it holds no certificate (PEM or DER)", False)
    return trusted


def _tls(settings: LdapSettings, ldap3: ModuleType) -> object:
    """The ldap3 Tls for both LDAPS and StartTLS: the handshake runs with tls_context()
    and the host of the server URI, so Python's own certificate and host-name check
    applies (ldap3 2.9.1's default Tls checks nothing, its optional check rejects IP
    addresses on Python 3.12+) and SNI is sent."""
    context = tls_context(settings)
    base: Any = ldap3.Tls

    class VerifyingTls(base):
        def wrap_socket(self, connection: Any, do_handshake: bool = False) -> None:
            connection.socket = context.wrap_socket(
                connection.socket, server_side=False, do_handshake_on_connect=do_handshake,
                server_hostname=connection.server.host)

    return VerifyingTls(validate=ssl.CERT_REQUIRED if settings.tls_verify else ssl.CERT_NONE)


_VERIFY_FAILED = re.compile(r"certificate verify failed: (.+?)(?: \(_ssl\.c:\d+\)|$)")
_RECOVERY = ("Local accounts still sign in; if no admin can, `ser2netweb --set-password NAME` "
             "with a new NAME creates a local admin account.")
_INSECURE = ("Not checking the certificate (ldap.tls_verify: false) is insecure: anyone on "
             "the network between ser2netweb and the directory could read the passwords.")


def tls_failure(exc: object) -> str | None:
    """OpenSSL's reason for refusing the server's certificate, from what a failed
    connection raised, or None if it wasn't that. ldap3 re-raises the ssl error as its
    own exception whose text is the repr of the original's arguments (quotes escaped),
    for a host with several addresses one per address in its args."""
    texts, todo, seen = [], [exc], set()
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, (list, tuple)):
            todo.extend(e)
        elif isinstance(e, BaseException):
            texts.append(str(e))
            todo.extend([*e.args, e.__cause__, e.__context__])
    for text in sorted(texts, key=lambda t: t.count("\\")):   # the least escaped first
        if m := _VERIFY_FAILED.search(text):
            return m.group(1).replace("\\", "").strip()   # no backslash in these reasons
    return None


@dataclass
class TlsProblem:
    """Why an LDAPS / StartTLS connection to the directory failed before anything was
    sent over it: the certificate is not trusted ("certificate"), it was issued for
    another name ("name"), or ca_file can't be used ("ca_file")."""
    kind: str
    reason: str      # OpenSSL's reason, or what is wrong with the CA file
    uri: str
    ca_file: str = ""
    unreadable: bool = False   # kind ca_file: can't be read (else: no certificate in it)

    def __str__(self) -> str:
        """The all.log line: what failed and what to set."""
        if self.kind == "ca_file":
            return (f"LDAP: the CA certificate file {self.ca_file!r} (ldap.ca_file) can't be "
                    f"used: {self.reason}, so LDAP sign-in over LDAPS / StartTLS fails until "
                    f"it is corrected in Settings → LDAP / Active Directory. {_RECOVERY}")
        head = (f"LDAP: the certificate of the directory server {self.uri} was not accepted "
                f"({self.reason}), so LDAP sign-in fails until this is fixed (nothing was "
                f"sent over that connection)")
        if self.kind == "name":
            fix = (": it was issued for another name. Use a name it was issued for in the "
                   "server URI (an IP address works only if the certificate lists that "
                   "address).")
        elif self.ca_file:
            fix = (f". The CA certificate file {self.ca_file!r} (ldap.ca_file) has to hold the "
                   f"certificate of the CA that issued it (PEM or DER).")
        else:
            fix = (". If the directory's certificates come from your own CA (Active Directory "
                   "Certificate Services) or are self-signed, set Settings → LDAP / Active "
                   "Directory → CA certificate file (ldap.ca_file in config.json) to that CA's "
                   "certificate (PEM or DER).")
        return f"{head}{fix} {_INSECURE} {_RECOVERY}"


def _tls_problem(exc: BaseException, settings: LdapSettings) -> TlsProblem | None:
    if isinstance(exc, CaFileError):
        return TlsProblem("ca_file", exc.reason, settings.server_uri, exc.path, exc.unreadable)
    reason = tls_failure(exc) if settings.uses_tls else None
    if reason is None:
        return None
    name = reason.startswith(("Hostname mismatch", "IP address mismatch"))
    return TlsProblem("name" if name else "certificate", reason, settings.server_uri,
                      settings.ca_file.strip())


def _result_text(result: Mapping[str, Any]) -> str:
    """An LDAP error answer for a log line: description (code): message; referrals."""
    text = f"{result.get('description')} ({result['result']})"
    if result.get("message"):
        text += ": " + " ".join(str(result["message"]).split())
    if result.get("referrals"):
        # not followed over TLS (authenticate): that server's certificate would go unchecked
        text += "; it refers to " + ", ".join(str(r) for r in result["referrals"])
    return text


# ---------------- bind + group lookup (ldap3) ----------------
class DirectoryGroups(list[str]):
    """The group DNs read for a user whose bind succeeded (authenticate).
    lookup_failed tells "they are in no group" apart from "their groups could not be
    read" (the reason is logged)."""
    lookup_failed = False


def _values(entry: Any, attr: str) -> list[str]:
    try:
        return [str(v) for v in entry[attr].values]
    except Exception:
        return []


def _dns_domain(dn: str) -> str:
    """corp.local for DC=corp,DC=local or OU=Staff,DC=corp,DC=local."""
    return ".".join(v for rdn in _split_dn(dn) or [] for t, v in rdn if t == "dc")


class _GroupLookup:
    """Reads the groups of the user who signed in, over an ldap3 connection."""

    def __init__(self, conn: Any, settings: LdapSettings, username: str,
                 log: Callable[[str], None], ldap3: ModuleType,
                 escape: Callable[[str], str]) -> None:
        self.conn, self.settings, self.username, self.log = conn, settings, username, log
        self.ldap3, self.escape = ldap3, escape
        self._root: dict[str, str] | None = None

    def problem(self, what: str) -> None:
        """Log why the groups could not be read, with the server's error, if any."""
        r = getattr(self.conn, "result", None) or {}
        if r.get("result"):   # an error answer, not just "no such entry"
            what += f" — {_result_text(r)}"
        self.log(f"LDAP: bound as {self.username!r}, but their groups could not be read: "
                 f"{what}")

    def root(self, attr: str) -> str:
        """An attribute of the server's RootDSE: defaultNamingContext (Active Directory:
        the DN of the server's domain) or configurationNamingContext."""
        if self._root is None:
            names = ("defaultNamingContext", "configurationNamingContext")
            self.conn.search("", "(objectClass=*)", search_scope=self.ldap3.BASE,
                             attributes=list(names))
            entry = self.conn.entries[0] if self.conn.entries else None
            self._root = {n: (_values(entry, n) or [""])[0] for n in names} if entry else {}
        return self._root.get(attr, "")

    def netbios_domain(self, name: str) -> str:
        """The DN of the forest's domain with this NetBIOS name (its crossRef), or ""."""
        config = self.root("configurationNamingContext")
        if not config:
            return ""
        # under CN=Partitions only the domains' crossRef objects have a nETBIOSName
        self.conn.search("CN=Partitions," + config, f"(nETBIOSName={self.escape(name)})",
                         search_scope=self.ldap3.LEVEL, attributes=["nCName"])
        entries = self.conn.entries
        return (_values(entries[0], "nCName") or [""])[0] if len(entries) == 1 else ""

    def direct(self, bind_name: str) -> DirectoryGroups:
        """Groups of the account that bound as `bind_name` (direct bind)."""
        groups = DirectoryGroups()
        attr = self.settings.group_attr
        try:
            if _split_dn(bind_name):     # a DN: read that entry
                user_dn = bind_name
                self.conn.search(user_dn, "(objectClass=*)", search_scope=self.ldap3.BASE,
                                 attributes=[attr])
                entry = self.conn.entries[0] if self.conn.entries else None
                if entry is None:
                    self.problem(f"no entry {user_dn!r}")
            else:                        # an AD sign-in name; not a DN, so not a search base
                entry = self.find_account(bind_name)
                user_dn = entry.entry_dn if entry is not None else ""
            if entry is None:
                groups.lookup_failed = True
                return groups
            groups.extend(_values(entry, attr))
            if self.settings.nested_groups:
                self.add_nested(groups, user_dn)
        except Exception as e:
            self.problem(f"{type(e).__name__}: {e}")
            groups.lookup_failed = True
        return groups

    def find_account(self, bind_name: str) -> Any:
        """The entry of an AD sign-in name: user@domain by userPrincipalName (an account
        without one also by sAMAccountName, when the base is that domain) under the user
        search base, else the server's domain; DOMAIN\\user by sAMAccountName in that
        domain (found by its NetBIOS name), under the user search base if it is in it.
        None (logged) unless there is exactly one."""
        search_base = self.settings.user_search_base.strip()
        if "\\" in bind_name:
            netbios, _sep, account = bind_name.partition("\\")
            domain = self.netbios_domain(netbios)
            if not domain:
                self.problem(f"the directory names no domain {netbios!r} (crossRef)")
                return None
            base = search_base or domain
            # by its DC= part: a child domain's DN is below its parent's DN
            if fold(_dns_domain(base)) != fold(_dns_domain(domain)):
                self.problem(f"the user search base {base!r} is not in the domain {netbios!r}")
                return None
            tries = [("sAMAccountName", account)]
        elif "@" in bind_name:
            base = search_base or self.root("defaultNamingContext")
            if not base:
                self.problem("no user search base is set and the server names no "
                             "defaultNamingContext")
                return None
            account, _at, suffix = bind_name.rpartition("@")
            tries = [("userPrincipalName", bind_name)]
            if fold(suffix) == fold(_dns_domain(base)):   # the implicit UPN: account@domain
                tries.append(("sAMAccountName", account))
        else:
            self.problem(f"{bind_name!r} is no DN, user@domain or DOMAIN\\user name")
            return None
        for attr, value in tries:
            self.conn.search(base, f"({attr}={self.escape(value)})",
                             attributes=[self.settings.group_attr])
            entries = self.conn.entries
            if len(entries) > 1:
                self.problem(f"{len(entries)} entries have {attr}={value} under {base!r}")
                return None
            if entries:
                return entries[0]
        self.problem(" or ".join(f"{a}={v}" for a, v in tries) + f" is not under {base!r}")
        return None

    def add_nested(self, groups: DirectoryGroups, user_dn: str) -> None:
        """Add the groups the user is in through other groups (Active Directory's
        LDAP_MATCHING_RULE_IN_CHAIN, searched in the whole domain)."""
        try:
            base = self.root("defaultNamingContext") or self.settings.user_search_base.strip()
            if not base:
                self.problem("nested groups: no base to search them in")
                groups.lookup_failed = True
                return
            self.conn.search(base, f"(member:{IN_CHAIN_RULE}:={self.escape(user_dn)})",
                             attributes=[self.ldap3.NO_ATTRIBUTES])
            if (getattr(self.conn, "result", None) or {}).get("result"):
                self.problem(f"nested groups under {base!r}")
                groups.lookup_failed = True
            known = {fold(g) for g in groups}
            for entry in self.conn.entries:
                if fold(entry.entry_dn) not in known:
                    known.add(fold(entry.entry_dn))
                    groups.append(entry.entry_dn)
        except Exception as e:   # keep the direct groups
            self.problem(f"nested groups: {type(e).__name__}: {e}")
            groups.lookup_failed = True


def authenticate(settings: LdapSettings, username: str, password: str,
                 logger: Callable[[str], None] | None = None, server: Any = None,
                 conn_factory: Callable[[Any, str, str], Any] | None = None,
                 on_tls_problem: Callable[[TlsProblem], None] | None = None,
                 ) -> list[str] | None:
    """Bind to LDAP as `username`/`password`; return the user's group DNs (a
    DirectoryGroups) on success or None on failure. Never raises on a bad bind.
    `server`/`conn_factory` are injection points for tests (an ldap3 MOCK_SYNC
    server). Returns None if ldap3 is missing or fails to import (logged). A refused
    server certificate (or an unusable ca_file) is logged with what to set and passed
    to on_tls_problem(TlsProblem)."""
    log = logger or (lambda _m: None)
    if not username or not password:   # empty password must not become an anonymous bind
        return None
    try:
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
        from ldap3.utils.dn import escape_rdn
    except Exception as e:  # not only ImportError: never a 500 for the sign-in
        log(f"LDAP login disabled: {optional.problem(e, 'ldap3')}")
        return None

    def _default_factory(srv: Any, user: str, pw: str) -> Any:
        ab = ldap3.AUTO_BIND_TLS_BEFORE_BIND if settings.start_tls else ldap3.AUTO_BIND_NO_TLS
        # over TLS, ldap3 would follow a referral to another server (with these
        # credentials) without checking its certificate, or in plain text: it doesn't
        return ldap3.Connection(srv, user=user, password=pw, auto_bind=ab,
                                auto_referrals=not settings.uses_tls)

    def _unbind(conn: Any) -> None:
        with contextlib.suppress(Exception):
            conn.unbind()

    factory = conn_factory or _default_factory
    try:
        if server is None:
            # connect_timeout so a dead/unreachable directory fails the login fast
            # instead of hanging the worker thread on the OS socket timeout
            server = ldap3.Server(settings.server_uri, get_info=ldap3.NONE, connect_timeout=5,
                                  use_ssl=settings.server_uri.lower().startswith("ldaps"),
                                  tls=_tls(settings, ldap3) if settings.uses_tls else None)
        if settings.user_dn_template.strip():
            # direct bind: the supplied credentials bind as the templated DN (or AD
            # sign-in name: {username}@corp.local, CORP\{username})
            bind_name = settings.user_dn_template.format(username=escape_rdn(username))
            conn = factory(server, bind_name, password)
            try:
                return _GroupLookup(conn, settings, username, log, ldap3,
                                    escape_filter_chars).direct(bind_name)
            finally:
                _unbind(conn)
        # search+bind: bind as the service account, find the user, then bind as them
        svc = factory(server, settings.bind_dn, settings.bind_password)
        try:
            flt = settings.user_search_filter.format(username=escape_filter_chars(username))
            svc.search(settings.user_search_base, flt, attributes=[settings.group_attr])
            if not svc.entries:
                r = getattr(svc, "result", None) or {}
                if r.get("result"):   # an error answer, not just "no such user"
                    log(f"LDAP: the user search under {settings.user_search_base!r} failed — "
                        f"{_result_text(r)}")
                return None
            entry = svc.entries[0]
            user_dn = entry.entry_dn
            groups = DirectoryGroups(_values(entry, settings.group_attr))
            _unbind(factory(server, user_dn, password))   # verify the password
            if settings.nested_groups:
                _GroupLookup(svc, settings, username, log, ldap3,
                             escape_filter_chars).add_nested(groups, user_dn)
            return groups
        finally:
            _unbind(svc)
    except Exception as e:  # bad bind / server error -> denied, never crash the login
        problem = _tls_problem(e, settings)
        if problem is None:  # not the name typed, which may be a password (login_post logs it)
            log(f"LDAP authentication failed: {type(e).__name__}: {e}")
            return None
        log(str(problem))
        if on_tls_problem is not None:
            on_tls_problem(problem)
        return None
