"""TLS certificates about to end: the admin UI's and each enabled TLS mapping's.

Once a day (and once soon after the start) the notifier reads the certificate files in
use with web/certinfo.py. A certificate that has fewer than 30, 7 or 1 days left, or has
ended, is logged with a WARNING and — with the optional cert_expiring event on — sent as a
notification: once per certificate and threshold and channel, also across restarts
(what was sent is kept in cert-notices.json in the data folder once a channel took it,
with the channels that did not, which get it at the next check, and only they; what was
sent for a certificate is kept while it can't be read, while its mapping is disabled or
the admin UI's TLS is off, and is forgotten when the certificate is renewed (another end
date at its path) or its mapping is deleted). What was read last is also what /metrics
(ser2net_tls_certificate_expiry_seconds) and GET /api/v1/status (certificates) answer.

Only reads the files; renewing one is the operator's (or certbot's), followed by a reload
of the configuration or a new upload in Settings.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time
from typing import Any, Optional

from .web import certinfo

THRESHOLD_DAYS = (30, 7, 1)   # a notification when fewer days than each are left ...
EXPIRED = 0                   # ... and one once the certificate has ended
CHECK_S = 86400.0             # how often the notifier looks
FRESH_S = 300.0               # /metrics and the status read the files again after this
NOTICES_FILE = "cert-notices.json"
_lock = threading.Lock()


@dataclasses.dataclass
class Certificate:
    """A certificate file in use. `key`: "admin" or the mapping's id; `mapping`: the
    mapping's name ("" for the admin UI); `info`: what the file says (None: `problem`
    says why it could not be read)."""
    key: str
    mapping: str
    path: str
    info: Optional[certinfo.CertInfo] = None
    problem: str = ""

    @property
    def label(self) -> str:
        """Whose it is, for the log and the message (English)."""
        return f"mapping {self.mapping!r}" if self.mapping else "admin UI"

    def view(self, now: Optional[float] = None) -> dict[str, Any]:
        """As GET /api/v1/status lists it."""
        out: dict[str, Any] = {"use": "mapping" if self.mapping else "admin", "path": self.path}
        if self.mapping:
            out.update(id=self.key, mapping=self.mapping)
        if self.info is None:
            return {**out, "state": "unreadable", "detail": self.problem}
        return {**out, "subject": self.info.subject, "not_after": self.info.not_after,
                "days_left": self.info.days_left(now), "state": self.info.state(now)}


def in_use(config) -> list[Certificate]:
    """The certificate files the configuration uses now: the admin UI's (when its TLS is
    on) and each enabled TLS mapping's (a listener's certificate, or the client
    certificate of a connect-out mapping)."""
    out = []
    admin = config.admin_ui
    if admin.tls_enabled:
        out.append(Certificate("admin", "", admin.tls_cert))
    for m in config.mappings:
        net = m.network
        if m.enabled and m.kind == "net" and net.tls and net.mode != "udp" and net.tls_cert:
            out.append(Certificate(m.id, m.name, net.tls_cert))
    return out


def read_all(certificates: list[Certificate]) -> list[Certificate]:
    """Read each certificate's file (blocking)."""
    for c in certificates:
        try:
            c.info = certinfo.read(c.path)
        except certinfo.CertReadError as e:
            c.problem = "no PEM certificate in it" if e.code == "not_pem" else str(e)
    return certificates


def threshold(info: certinfo.CertInfo, now: float) -> Optional[int]:
    """The tightest threshold the certificate is past: EXPIRED, else the smallest of
    THRESHOLD_DAYS it has fewer days left than; None while it has more than them all."""
    left = info.not_after - now
    if left < 0:
        return EXPIRED
    reached = [d for d in THRESHOLD_DAYS if left < d * 86400]
    return min(reached) if reached else None


def message(c: Certificate, info: certinfo.CertInfo, now: float) -> str:
    """The notification's detail and the log line: whose certificate, which file, what it
    says."""
    return f"{c.label} certificate {c.path}: {info.describe(now)}"


def load_notices(path: str) -> dict[str, list]:
    """What was sent already: "<key>|<path>" -> [not_after, threshold], and a third item,
    the channels that did not take that threshold's notice yet (notify.CHANNELS), when
    there are any. {} when the file is missing or makes no sense (everything is then
    sent once more); an entry that makes no sense is left out."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if _record(v)}


def _record(v: Any) -> bool:
    return (isinstance(v, list) and len(v) in (2, 3)
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v[:2])
            and (len(v) == 2 or isinstance(v[2], list)
                 and all(isinstance(c, str) for c in v[2])))


def kept_unused(notices: dict[str, list], used: set[str], config) -> dict[str, list]:
    """The records ("<key>|<path>") of certificates not in use now that stay: a disabled
    mapping's, the admin UI's with its TLS off, one a mapping no longer names. Forgotten
    are those of a deleted mapping and those whose file now holds another certificate
    (renewed: another end date); one that can't be read stays. Blocking (reads them)."""
    out = {}
    for name, record in notices.items():
        if name in used:
            continue
        key, _sep, path = name.partition("|")
        if key != "admin" and config.get_mapping(key) is None:
            continue
        try:
            if certinfo.read(path).not_after != record[0]:
                continue
        except certinfo.CertReadError:
            pass
        out[name] = record
    return out


def save_notices(path: str, notices: dict[str, list]) -> None:
    """Rewrite the file whole (a temporary file renamed over it). Raises OSError."""
    with _lock:
        _write_notices(path, notices)


def note_sent(path: str, name: str, record: list) -> None:
    """Remember one notice that went out ("<key>|<path>" -> [not_after, threshold]), next
    to what the file holds. Raises OSError."""
    with _lock:
        notices = load_notices(path)
        notices[name] = record
        _write_notices(path, notices)


def _write_notices(path: str, notices: dict[str, list]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(notices, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class CertWatch:
    """The certificates in use as last read, and which thresholds were said already."""

    def __init__(self, state) -> None:
        self.state = state
        self._read: list[Certificate] = []
        self._read_at: Optional[float] = None    # time.monotonic() of the last read
        self._logged: dict[str, tuple] = {}      # key -> (not_after, threshold) logged
        self._save_failed = False                # logged once

    @property
    def notices_path(self) -> str:
        return os.path.join(self.state.data_dir, NOTICES_FILE)

    async def current(self, max_age: float = FRESH_S) -> list[Certificate]:
        """The certificates in use, read again when the last read is older than
        `max_age` seconds."""
        now = time.monotonic()
        if self._read_at is None or now - self._read_at >= max_age:
            self._read = await asyncio.to_thread(read_all, in_use(self.state.config))
            self._read_at = now
        return self._read

    async def check(self, emit, now: Optional[float] = None) -> int:
        """Read the certificates and say which are past a threshold they were not said to
        be past yet: a WARNING in the log (once per run and threshold), and
        emit("cert_expiring", key, mapping name, detail, sent=..., channels=...) -> bool for
        a notification, to every channel (channels None) or to those that did not take
        that threshold's notice before; remembered in cert-notices.json once a channel
        took it, with those that did not (Notifier.emit's sent). Returns how many were
        queued."""
        now = time.time() if now is None else now
        certificates = await self.current(max_age=0)
        notices = await asyncio.to_thread(load_notices, self.notices_path)
        used = {f"{c.key}|{c.path}" for c in certificates}
        kept = await asyncio.to_thread(kept_unused, notices, used, self.state.config)
        due: list[tuple[Certificate, str, list, str, Optional[list[str]]]] = []
        for c in certificates:
            name = f"{c.key}|{c.path}"
            if c.info is None:  # (being replaced, say): what was sent for it stays said
                if name in notices:
                    kept[name] = notices[name]
                continue
            if name in notices and notices[name][0] == c.info.not_after:
                kept[name] = notices[name]
            reached = threshold(c.info, now)
            if reached is None:
                continue
            text = message(c, c.info, now)
            if self._logged.get(name) != (c.info.not_after, reached):
                self._logged[name] = (c.info.not_after, reached)
                self.state.log(f"WARNING: TLS certificate: {text}")
            before = kept.get(name)
            if before is None or before[1] > reached:  # (else said for it, or a tighter one)
                due.append((c, name, [c.info.not_after, reached], text, None))
            elif before[1] == reached and len(before) > 2 and before[2]:
                # said, but not on every channel: to those it did not reach, and only them
                due.append((c, name, [c.info.not_after, reached], text, list(before[2])))
        # (first: a notice that is sent meanwhile is added to what this keeps)
        if kept != notices:  # (renewed certificates, deleted mappings: forgotten)
            await self._save(save_notices, kept)
        sent = 0
        for c, name, record, text, channels in due:
            if emit("cert_expiring", f"{name}|{record[1]}", c.mapping, text,
                    sent=self._remember(name, record), channels=channels):
                sent += 1
        return sent

    def _remember(self, name: str, record: list):
        """What Notifier.emit awaits once the notice was tried: once a channel took it
        (or there was none to try), keep it as sent, with the channels that did not."""
        async def sent(tried: list[str], failed: list[str]) -> None:
            if tried and set(failed) >= set(tried):
                return  # nobody got it: the next check tries again (what was kept stays)
            missed = sorted(set(failed))
            await self._save(note_sent, name, record + ([missed] if missed else []))
        return sent

    async def _save(self, write, *args) -> None:
        try:
            await asyncio.to_thread(write, self.notices_path, *args)
            self._save_failed = False
        except OSError as e:
            if not self._save_failed:
                self._save_failed = True
                self.state.log(f"TLS certificate notices: {NOTICES_FILE} could not be "
                               f"written ({e.strerror or e}): a notification may be sent "
                               "again after a restart")
