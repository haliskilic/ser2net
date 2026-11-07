"""When each web-UI account last signed in, and which LDAP/OIDC accounts are unused.

The times come from the audit log — its login / oidc_login / first_run_setup records,
read once at start-up (AppState.load_sign_ins) — and from every session issued since
(routes.common.Ctx.set_session). They are kept in memory only: writing them to
config.json would save it, and push a backup out of the 50 kept, at every sign-in.

An LDAP/OIDC account is only a shadow of the directory's or identity provider's: it
comes back at the next sign-in while that still gives the person a role. One not signed
in for AppConfig.external_users_expire_days is removed by the hourly maintenance
(AppState.expire_external_accounts), so people who left don't stay in config.json and
its backups for ever. Never removed: local accounts, admins, and the last accounts
(an empty account list would open first-run setup again). An account without any
sign-in in the audit log is only removed when the log goes back that far for its kind
of sign-in — older versions did not record every LDAP sign-in.
"""
from __future__ import annotations

import time
from typing import Optional

from .config import User, fold

_TIME = "%Y-%m-%d %H:%M:%S"     # how the audit log writes times (local time)


def _when(text: str) -> Optional[float]:
    try:
        return time.mktime(time.strptime(text[:19], _TIME))
    except (ValueError, OverflowError):
        return None


def sign_in_of(record: dict) -> Optional[tuple[str, str]]:
    """(account name, source) of an audit record that is a sign-in, else None:
      login            user: the name (before this version: as typed), detail "role, source";
      oidc_login       user and detail: the name (v2.7: four fields, the name in detail);
      first_run_setup  the setup page's admin, signed in at once (the command line's
                       first admin, by "(command line)", is not a sign-in)."""
    action, user, detail = record.get("action"), record.get("user", ""), record.get("detail", "")
    if action == "login" and user and "," in detail:
        return user, detail.rsplit(",", 1)[1].strip()
    if action == "oidc_login" and (user or detail):
        return user or detail, "oidc"
    if action == "first_run_setup" and user and user == detail:
        return user, "local"
    return None


class SignIns:
    """The last sign-in of each account name (LDAP names also in any letter case, as
    they sign in), and for each source the oldest sign-in on record: how far back the
    audit log knows that kind of sign-in."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}       # account name -> time
        self._folded: dict[str, float] = {}     # fold(name) -> time (LDAP accounts)
        self._since: dict[str, float] = {}      # source -> its oldest sign-in on record
        self.loaded = False                     # the audit log has been read

    def note(self, name: str, source: str, when: Optional[float] = None) -> None:
        """A sign-in (a session issued) for this account, now or at `when`."""
        when = time.time() if when is None else when
        for table, key in ((self._last, name), (self._folded, fold(name))):
            if when > table.get(key, 0.0):
                table[key] = when
        if when < self._since.get(source, float("inf")):
            self._since[source] = when

    def load(self, records) -> None:
        """Add the sign-ins of these audit records (AppState.read_audit)."""
        for record in records:
            found = sign_in_of(record)
            when = _when(record.get("time", "")) if found else None
            if found and when is not None:
                self.note(found[0], found[1], when)
        self.loaded = True

    def last(self, user: User) -> Optional[float]:
        """When this account last signed in (an LDAP account: in any letter case), or
        None when the audit log doesn't know."""
        if user.source == "ldap":
            return self._folded.get(fold(user.username))
        return self._last.get(user.username)

    def expired(self, users: list[User], days: int, now: Optional[float] = None) -> list[User]:
        """The LDAP/OIDC accounts not signed in for `days` (see the module docstring)."""
        if days <= 0 or not self.loaded:
            return []
        cutoff = (time.time() if now is None else now) - days * 86400
        gone = []
        for u in users:
            if u.source not in ("ldap", "oidc") or u.role == "admin":
                continue
            last = self.last(u)
            if last is not None and last < cutoff:
                gone.append(u)
            elif last is None and self._since.get(u.source, float("inf")) <= cutoff:
                gone.append(u)
        return [] if len(gone) == len(users) else gone
