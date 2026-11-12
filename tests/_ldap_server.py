"""A small LDAP server for the tests: simple bind, search and unbind over plain TCP in
the real LDAPv3 wire format, so ser2netweb's ldap3 client code runs unchanged against it
(in the test process, or in a ser2netweb server started by the test).

    from _ldap_server import FakeDirectory
    with FakeDirectory(entries, passwords) as d:
        settings.server_uri = d.uri          # ldap://127.0.0.1:<port>

entries: {dn: {attribute: [values]}}; passwords: {dn: password}. With ad=True (the
default) it answers like Active Directory where the tests need it: a bind by
userPrincipalName (also the implicit sAMAccountName@<domain>) or DOMAIN\\sAMAccountName,
a RootDSE with defaultNamingContext and configurationNamingContext (the crossRef
entries of the domains go under its CN=Partitions), a search base that isn't a DN
refused with invalidDNSyntax, and the LDAP_MATCHING_RULE_IN_CHAIN filter on `member`
following nested groups. With ad=False it is a plain directory (OpenLDAP): DN binds
only, no defaultNamingContext, an unknown matching rule matches nothing. Filters are
single conditions (equality, presence, extensible match): pyasn1 can't decode ldap3's
definitions of &, | and !. Every bind name and search is recorded (`binds`, `searches`).

TLS: tls=<server ssl.SSLContext> answers LDAPS (uri ldaps://), with start_tls=True plain
LDAP that takes the StartTLS extended operation; `host` is the name put in the uri (the
socket listens on 127.0.0.1). Failed handshakes are recorded in `tls_errors`, binds over
TLS in `tls_binds`. referrals={base DN: URL}: a search at or below that base gets a
referral to the URL instead of entries (as AD answers for another domain).
Needs ldap3 (its ASN.1 definitions, pyasn1).
"""
from __future__ import annotations

import socket
import ssl
import threading

from ldap3.protocol.rfc4511 import (URI, LDAPDN, AttributeDescription, AttributeValue,
                                    BindResponse, ExtendedResponse, LDAPMessage, LDAPString,
                                    MessageID, PartialAttribute, PartialAttributeList,
                                    ProtocolOp, Referral, ResponseName, ResultCode,
                                    SearchResultDone, SearchResultEntry, Vals)
from pyasn1.codec.ber import decoder, encoder

IN_CHAIN = "1.2.840.113556.1.4.1941"
START_TLS = "1.3.6.1.4.1.1466.20037"


def norm_dn(dn: str) -> str:
    """Case- and space-insensitive form of a DN (the test data has no escapes)."""
    return ",".join("=".join(p.strip() for p in rdn.split("=", 1))
                    for rdn in str(dn).lower().split(","))


def is_dn(text: str) -> bool:
    return bool(text) and all("=" in rdn and rdn.split("=", 1)[0].strip()
                              for rdn in text.split(","))


class FakeDirectory:
    def __init__(self, entries: dict, passwords: dict, *, ad: bool = True,
                 base: str = "DC=corp,DC=local", netbios: str = "CORP",
                 tls: ssl.SSLContext | None = None, start_tls: bool = False,
                 host: str = "127.0.0.1", referrals: dict | None = None):
        self.entries = {dn: {k.lower(): list(v) for k, v in attrs.items()}
                        for dn, attrs in entries.items()}
        self.passwords = {norm_dn(dn): pw for dn, pw in passwords.items()}
        self.ad, self.base, self.netbios = ad, base, netbios
        self.domain = ".".join(rdn.split("=", 1)[1] for rdn in base.split(",")
                               if rdn.strip().lower().startswith("dc="))
        self.binds: list[str] = []
        self.searches: list[tuple[str, int, str]] = []   # (base, scope, filter)
        self.tls, self.start_tls = tls, start_tls
        self.referrals = {norm_dn(dn): url for dn, url in (referrals or {}).items()}
        self.tls_errors: list[str] = []   # server side of failed TLS handshakes
        self.tls_binds: list[str] = []    # bind names that came over TLS
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        scheme = "ldaps" if tls is not None and not start_tls else "ldap"
        self.uri = f"{scheme}://{host}:{self.port}"
        self._stop = False

    def __enter__(self) -> "FakeDirectory":
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop = True
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    # ---- the directory ----
    def _entry(self, dn: str):
        key = norm_dn(dn)
        return next((attrs for d, attrs in self.entries.items() if norm_dn(d) == key), None)

    def _account(self, name: str):
        return next((dn for dn, attrs in self.entries.items()
                     if any(v.lower() == name.lower() for v in attrs.get("samaccountname", []))),
                    None)

    def _bind_dn(self, name: str):
        """The entry a bind name stands for: a DN, a UPN, an implicit UPN or DOMAIN\\user."""
        if is_dn(name):
            return name
        if self.ad and "@" in name:
            for dn, attrs in self.entries.items():
                if any(v.lower() == name.lower() for v in attrs.get("userprincipalname", [])):
                    return dn
            account, _at, suffix = name.rpartition("@")
            return self._account(account) if suffix.lower() == self.domain.lower() else None
        if self.ad and "\\" in name:
            domain, _sep, account = name.partition("\\")
            return self._account(account) if domain.lower() == self.netbios.lower() else None
        return None

    def _bind(self, name: str, password: str) -> int:
        self.binds.append(name)
        dn = self._bind_dn(name)
        if dn is None or not password or self.passwords.get(norm_dn(dn)) != password:
            return 49  # invalidCredentials
        return 0

    def _in_chain(self, group_dn: str, member: str, seen: set) -> bool:
        attrs = self._entry(group_dn)
        if attrs is None or norm_dn(group_dn) in seen:
            return False
        seen.add(norm_dn(group_dn))
        members = attrs.get("member", [])
        return (any(norm_dn(m) == norm_dn(member) for m in members)
                or any(self._in_chain(m, member, seen) for m in members))

    def _match(self, flt, dn: str, attrs: dict) -> bool:
        kind = flt.getName()
        f = flt[kind]
        if kind == "present":
            return str(f).lower() == "objectclass" or str(f).lower() in attrs
        if kind == "equalityMatch":
            name, value = str(f["attributeDesc"]).lower(), bytes(f["assertionValue"]).decode()
            return any(v.lower() == value.lower() for v in attrs.get(name, []))
        if kind == "extensibleMatch":
            rule = str(f["matchingRule"]) if f["matchingRule"].isValue else ""
            name = str(f["type"]).lower() if f["type"].isValue else ""
            if self.ad and rule == IN_CHAIN and name == "member":
                return self._in_chain(dn, bytes(f["matchValue"]).decode(), set())
        return False  # anything else (an unknown matching rule) is Undefined

    def _text(self, flt) -> str:
        kind = flt.getName()
        f = flt[kind]
        if kind == "present":
            return f"({f}=*)"
        if kind == "equalityMatch":
            return f"({f['attributeDesc']}={bytes(f['assertionValue']).decode()})"
        if kind == "extensibleMatch":
            return f"({f['type']}:{f['matchingRule']}:={bytes(f['matchValue']).decode()})"
        return f"({kind})"

    def _search(self, req):
        base, scope = str(req["baseObject"]), int(req["scope"])
        self.searches.append((base, scope, self._text(req["filter"])))
        wanted = {str(a).lower() for a in req["attributes"]}
        if base == "" and scope == 0:   # the RootDSE
            root = {"namingcontexts": [self.base]}
            if self.ad:
                root["defaultnamingcontext"] = [self.base]
                root["configurationnamingcontext"] = ["CN=Configuration," + self.base]
            return 0, [("", {k: v for k, v in root.items() if not wanted or k in wanted})]
        if not is_dn(base):
            return 34, []  # invalidDNSyntax: what AD answers for a UPN used as the base
        nbase = norm_dn(base)
        for suffix, url in self.referrals.items():
            if nbase == suffix or nbase.endswith("," + suffix):
                return 10, [url]  # referral: that part of the tree is on another server
        if not any(norm_dn(dn) == nbase or norm_dn(dn).endswith("," + nbase)
                   for dn in [self.base, *self.entries]):
            return 32, []  # noSuchObject (the OUs above the entries exist)
        hits = []
        for dn, attrs in self.entries.items():
            ndn = norm_dn(dn)
            depth = ndn.count(",") - nbase.count(",")
            if not (ndn == nbase or ndn.endswith("," + nbase)):
                continue
            if (scope == 0 and depth) or (scope == 1 and depth != 1):
                continue
            if self._match(req["filter"], dn, attrs):
                hits.append((dn, {} if wanted == {"1.1"} else {
                    k: v for k, v in attrs.items() if not wanted or "*" in wanted or k in wanted}))
        return 0, hits

    # ---- the wire format ----
    def _wrap(self, conn: socket.socket):
        """The server side of a TLS handshake: the TLS socket, or None (recorded)."""
        try:
            return self.tls.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as e:
            self.tls_errors.append(f"{type(e).__name__}: {e}")
            conn.close()
            return None

    def _client(self, sock: socket.socket) -> None:
        buf, conn = b"", sock
        try:
            if self.tls is not None and not self.start_tls:   # LDAPS
                if (conn := self._wrap(sock)) is None:
                    return
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while (size := _message_size(buf)) is not None and len(buf) >= size:
                    msg, _rest = decoder.decode(buf[:size], asn1Spec=LDAPMessage())
                    buf = buf[size:]
                    answer = self._handle(conn, msg)
                    if answer == "start_tls":
                        if (conn := self._wrap(conn)) is None:
                            return
                    elif not answer:
                        return
        except OSError:
            return
        finally:
            (conn or sock).close()

    def _handle(self, conn: socket.socket, msg):
        mid, op = int(msg["messageID"]), msg["protocolOp"]
        kind = op.getName()
        if kind == "bindRequest":
            req = op["bindRequest"]
            auth = req["authentication"]
            pw = bytes(auth["simple"]).decode() if auth.getName() == "simple" else ""
            if isinstance(conn, ssl.SSLSocket):
                self.tls_binds.append(str(req["name"]))
            code = self._bind(str(req["name"]), pw)
            conn.sendall(_reply(mid, "bindResponse", _result(BindResponse(), code)))
            return True
        if kind == "searchRequest":
            code, hits = self._search(op["searchRequest"])
            if code == 10:   # a referral: hits are its URLs
                conn.sendall(_reply(mid, "searchResDone",
                                    _result(SearchResultDone(), code, referrals=hits)))
                return True
            for dn, attrs in hits:
                conn.sendall(_reply(mid, "searchResEntry", _entry(dn, attrs)))
            conn.sendall(_reply(mid, "searchResDone", _result(SearchResultDone(), code)))
            return True
        if kind == "extendedReq":
            name = str(op["extendedReq"]["requestName"])
            ok = (name == START_TLS and self.start_tls and self.tls is not None
                  and not isinstance(conn, ssl.SSLSocket))
            pdu = _result(ExtendedResponse(), 0 if ok else 2)   # 2: protocolError
            pdu["responseName"] = ResponseName(name)
            conn.sendall(_reply(mid, "extendedResp", pdu))
            return "start_tls" if ok else True
        return False  # unbind (or anything else): hang up


def _message_size(buf: bytes):
    """Length of the first BER element in buf, once its header is complete."""
    if len(buf) < 2:
        return None
    if buf[1] < 0x80:
        return 2 + buf[1]
    n = buf[1] & 0x7F
    return None if len(buf) < 2 + n else 2 + n + int.from_bytes(buf[2:2 + n], "big")


def _result(pdu, code: int, referrals=()):
    pdu["resultCode"] = ResultCode(code)
    pdu["matchedDN"] = LDAPDN("")
    pdu["diagnosticMessage"] = LDAPString("" if code == 0 else f"fake directory: error {code}")
    if referrals:
        ref = Referral()
        for i, url in enumerate(referrals):
            ref.setComponentByPosition(i, URI(url))
        pdu["referral"] = ref
    return pdu


def _entry(dn: str, attrs: dict):
    entry = SearchResultEntry()
    entry["object"] = LDAPDN(dn)
    plist = PartialAttributeList()
    for i, (name, values) in enumerate(attrs.items()):
        pa = PartialAttribute()
        pa["type"] = AttributeDescription(name)
        vals = Vals()
        for j, v in enumerate(values):
            vals.setComponentByPosition(j, AttributeValue(v))
        pa["vals"] = vals
        plist.setComponentByPosition(i, pa)
    entry["attributes"] = plist
    return entry


def _reply(mid: int, kind: str, pdu) -> bytes:
    msg = LDAPMessage()
    msg["messageID"] = MessageID(mid)
    op = ProtocolOp()
    op.setComponentByName(kind, pdu)
    msg["protocolOp"] = op
    return encoder.encode(msg)
