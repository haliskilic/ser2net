"""Event notifications: tell someone when a serial device disappears or comes back,
a mapping fails to start, clients come and go, a sign-in fails, the data disk is almost
full, a device sends nothing, a cluster node goes offline, a Modbus unit that a gateway
polls stops answering or answers again, or ser2netweb has started.

The Notifier watches the supervisor's status snapshot (every POLL_S seconds) and
turns state changes into events; the login route reports failed sign-ins itself. Every
DISK_CHECK_S it also looks at the data folder's free space: below storage.floor() (where
traces pause) it logs a warning and sends disk_low, once until there is room again.
A mapping with a no-data alarm (MappingOptions.no_data_alarm_s) whose open port has had
nothing from its device that long is logged and sends no_data, data_back once it talks
again (silence_events). Cluster nodes heard from in this run that are not heard from for
cluster.PEER_TTL, and configured peers that stop answering, are logged and send
peer_offline, peer_back when they return (PeerWatch). SERVICE_STARTED_S after the start
service_started sums up which mappings run (and whether the previous run was ended by
the event loop watchdog).
Each enabled event is sent to the configured webhook (generic JSON, Slack, Microsoft
Teams Workflows, Discord or Telegram) and/or by e-mail (SMTP), from a worker task so
a slow endpoint never holds anything up. The same event for the same mapping (client
events: and client address; failed sign-ins: per address) is sent at most once per
``repeat_s``, and at most MAX_PER_MINUTE messages go out per minute.
The client events send the client's IP address, a failed sign-in the account name and the
IP address; with ``mask_personal_data`` an address goes out only as its network and no
account name at all (mask_address, sign_in_detail).
Standard library only (urllib, smtplib).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import ipaddress
import json
import smtplib
import socket
import ssl
import time
import urllib.parse
import urllib.request
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Optional

from . import __version__, i18n, storage, user_agent
from .config import node_name as configured_name
from .engine.cluster import PEER_TTL, peer_addr
from .explain import explain
from .i18n import _
from .logtext import one_line
from .web import proxy

POLL_S = 2.0
DISK_CHECK_S = 60.0
MAX_PER_MINUTE = 20
TIMEOUT_S = 10.0
MAX_KEYS = 4096  # events remembered for the repeat interval (the oldest go first)
# the events whose detail is a client's address (bridge: "ip:port", IPv6 without brackets)
CLIENT_EVENTS = ("client_connected", "client_disconnected")
SERVICE_STARTED_S = 15.0  # after the start: the devices that open at all have opened
PEER_PROBE_S = 60.0       # a configured peer (no beacons) is asked this often ...
PEER_PROBE_MISSES = 2     # ... and is offline after this many answers missed in a row
DRAIN_S = 3.0             # at a stop, what is queued gets this long to be sent

def title(event: str) -> str:
    """The event's name in the notification language (see Notifier.deliver)."""
    return {
        "device_lost": _("serial device lost"),
        "device_back": _("serial device back"),
        "start_failed": _("mapping failed to start"),
        "link_lost": _("connection to the remote server lost"),
        "link_back": _("connection to the remote server back"),
        "client_connected": _("client connected"),
        "client_disconnected": _("client disconnected"),
        "login_failed": _("failed sign-in"),
        "disk_low": _("data disk almost full"),
        "no_data": _("no data from the serial device"),
        "data_back": _("data from the serial device again"),
        "peer_offline": _("cluster node offline"),
        "peer_back": _("cluster node back"),
        "service_started": _("ser2netweb started"),
        "unit_offline": _("Modbus unit not answering"),
        "unit_back": _("Modbus unit answering again"),
        "safe_values": _("safe values written"),
        "test": _("test notification"),
    }.get(event, event)


_LOST = ("reconnecting", "device-missing")


_SILENT = ("offline", "no reply")  # a polled unit that stopped answering (not: the port down)


def status_events(prev: dict, statuses: dict, names: dict) -> list[tuple[str, str, str, str]]:
    """Events between two status snapshots: [(event, key, mapping name, detail)].
    A mapping seen for the first time produces none, unless it is in trouble: one that
    failed at start-up (see baseline) or a new one that failed to start says so once, and
    so does a connect-out link that is down.

    A connect-out link (``link``) is lost when an attempt to connect fails while it was
    up or connecting, and back when it is up again after that: a link that is closed and
    at once connected again (a max connection time or idle timeout, the remote closing
    it) goes through "connecting", not "down", so it is not reported.

    A Modbus unit is reported when it stops answering, and again once it answers (not
    while its serial port is down: device_lost says that). A Modbus gateway that wrote
    points to their safe value (the broker, or the Sparkplug primary host, was away for
    their safe_after_s) says so once per point and outage. An older snapshot (2-tuple:
    no link, no units; 4-tuple: no safe values) still compares."""
    events = []
    for mid, st in statuses.items():
        name = names.get(mid)
        if name is None:
            continue
        old = prev.get(mid)
        state, peers = st.get("state"), {c.get("peer") for c in st.get("clients") or []}
        link = st.get("link") or ""
        if old is None:
            if state == "error":
                events.append(("start_failed", mid, name, st.get("last_error") or ""))
            if link == "down":
                events.append(("link_lost", mid, name, st.get("link_error") or ""))
            continue
        old_state, old_peers = old[0], old[1]
        old_link = old[2] if len(old) > 2 else ""
        old_units = old[3] if len(old) > 3 else {}
        device = st.get("device") or ""
        if old_state == "running" and state in _LOST:
            events.append(("device_lost", mid, name, st.get("last_error") or device))
        elif old_state in _LOST and state == "running":
            events.append(("device_back", mid, name, device))
        if state == "error" and old_state != "error":
            events.append(("start_failed", mid, name, st.get("last_error") or ""))
        if link == "down" and old_link in ("up", "connecting"):
            events.append(("link_lost", mid, name, st.get("link_error") or ""))
        elif link == "up" and old_link == "down":
            events.append(("link_back", mid, name, ", ".join(sorted(peers))))
        for p in sorted(peers - old_peers):
            events.append(("client_connected", f"{mid}:{_client_host(p)}", name, p))
        for p in sorted(old_peers - peers):
            events.append(("client_disconnected", f"{mid}:{_client_host(p)}", name, p))
        for unit, now in _units(st).items():
            was = old_units.get(unit)
            if now == _SILENT and was != _SILENT:
                events.append(("unit_offline", f"{mid}:unit{unit}", name, f"unit {unit}"))
            elif was == _SILENT and now[0] == "online":
                events.append(("unit_back", f"{mid}:unit{unit}", name, f"unit {unit}"))
        new = [p for p in _safe_written(st) if p not in (old[4] if len(old) > 4 else ())]
        if new:
            safe = st.get("safe") or {}
            rows = {p.get("name"): p for p in safe.get("points") or []}
            longest = max((rows[n].get("after_s") or 0 for n, _t in new), default=0)
            events.append(("safe_values", f"{mid}:safe", name, ", ".join(
                f"{n} = {_safe_text(rows[n].get('value'))}" for n, _t in new)
                + f" (no {safe.get('watching') or 'broker'} for {longest:g} s)"))
    return events


def _safe_written(st: dict) -> tuple:
    """The points a gateway wrote to their safe value in this outage: (name, when)."""
    return tuple((p.get("name"), p.get("written"))
                 for p in (st.get("safe") or {}).get("points") or [] if p.get("written"))


def _safe_text(value) -> str:
    return str(value).lower() if isinstance(value, bool) else f"{value:g}" if isinstance(
        value, float) else str(value)


def _client_host(peer: str) -> str:
    """A client's address without its port ("ip:port" -> "ip"): a client that reconnects
    comes from another port, and is still the same one for the repeat interval."""
    host, sep, port = str(peer).rpartition(":")
    return host if sep and host and port.isdigit() else str(peer)


def mask_address(text: str) -> str:
    """A client's address with personal data left out: only its network, IPv4 as /24
    ("10.0.5.x"), IPv6 as /48 ("2001:db8:5::/48"), without the port. The whole text is
    tried as an address before the part before its last ':' (a client is "[ipv6]:port"; a
    peer of an older version writes it without the brackets). Text that is no IP address
    (a host name set as a UDP mapping's reply address) is returned as it is."""
    text = str(text).strip()
    for candidate in (text, text.rpartition(":")[0]):
        try:
            ip = ipaddress.ip_address(candidate.strip("[]"))
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if isinstance(ip, ipaddress.IPv4Address):
            return ".".join(str(ip).split(".")[:3]) + ".x"
        return str(ipaddress.IPv6Network((int(ip) >> 80 << 80, 48)))
    return text


def sign_in_detail(settings, user: str, ip: str, source: str = "", reason: str = "") -> str:
    """The detail of a login_failed notification: "[LDAP |OIDC ]user '<name>' from <ip>"
    (an account's name, or auth.UNKNOWN_USER; "sign-in" for one that failed before it had a
    name, an SSO answer that doesn't match), then ": <reason>" when there is one. With
    settings.mask_personal_data the name is left out and the address is its network."""
    if settings.mask_personal_data:
        who, ip = "user (name left out)", mask_address(ip)
    else:
        who = f"user {user!r}" if user else "sign-in"
    text = f"{source} {who} from {ip}" if source else f"{who} from {ip}"
    return f"{text}: {reason}" if reason else text
def no_data_for(mapping, status: Optional[dict]) -> int:
    """Seconds the mapping's device has been silent, when that is past its no-data alarm
    (MappingOptions.no_data_alarm_s; reply-only: what was sent to it had no answer that
    long); 0 when it isn't (or the alarm is off, or the mapping doesn't run)."""
    limit = mapping.options.no_data_alarm_s
    if not limit or not status or status.get("state") != "running":
        return 0
    key = "reply_wait_s" if mapping.options.no_data_reply_only else "rx_idle_s"
    quiet = status.get(key)
    return int(quiet) if quiet is not None and quiet >= limit else 0


def silence_events(silent: set, statuses: dict, mappings) -> tuple[list, set]:
    """no_data / data_back between two looks: ([(event, key, mapping name, detail,
    mapping)], the ids silent now)."""
    events, now = [], set()
    for m in mappings:
        st = statuses.get(m.id)
        quiet = no_data_for(m, st)
        device = (st or {}).get("device") or m.serial.port
        if quiet:
            now.add(m.id)
            if m.id not in silent:
                what = ("no answer from" if m.options.no_data_reply_only else "no data from")
                events.append(("no_data", m.id, m.name, f"{what} {device} for {quiet} s", m))
        elif m.id in silent and st and st.get("state") == "running":
            events.append(("data_back", m.id, m.name, device, m))
    return events, now


def service_summary(mappings, statuses: dict) -> str:
    """One line on which enabled mappings run, naming (at most 10 of) those that don't."""
    enabled = [m for m in mappings if m.enabled]
    down = [m for m in enabled if (statuses.get(m.id) or {}).get("state") != "running"]
    text = (f"ser2netweb {__version__}: {len(enabled) - len(down)} of {len(enabled)} enabled "
            f"mapping(s) running")
    if down:
        parts = []
        for m in down[:10]:
            st = statuses.get(m.id) or {}
            why = st.get("last_error") or ""
            parts.append(f"{m.name} ({st.get('state') or 'stopped'}{': ' + why if why else ''})")
        text += "; not running: " + ", ".join(parts) + (
            f" and {len(down) - 10} more" if len(down) > 10 else "")
    return text


class PeerWatch:
    """The cluster nodes this node hears from. A node heard from in this run (its beacons)
    that is not heard from for cluster.PEER_TTL is offline. A configured peer that sends
    no beacons here (another network) is asked every PEER_PROBE_S while peer events are
    chosen, and is offline after PEER_PROBE_MISSES answers missed in a row (also one that
    never answered since the start). Each going offline and coming back is an event."""

    def __init__(self) -> None:
        self.heard: dict[str, tuple[float, str, str]] = {}  # id -> (when, name, address)
        self.offline: set[str] = set()
        self.misses: dict[str, int] = {}                     # configured address -> misses
        self._probed: Optional[float] = None

    def beacons(self, peers: list[dict], now: float) -> list[tuple[str, str, str, str]]:
        events = []
        for p in peers:
            pid, addr = str(p.get("id")), peer_addr(p.get("ip"), p.get("port"))
            name = str(p.get("name") or pid[:8])
            if pid in self.offline:
                self.offline.discard(pid)
                events.append(("peer_back", f"peer:{pid}", name, addr))
            self.heard[pid] = (now, name, addr)
        for pid, (when, name, addr) in self.heard.items():
            if pid not in self.offline and now - when > PEER_TTL:
                self.offline.add(pid)
                events.append(("peer_offline", f"peer:{pid}", name,
                               f"{addr}: not heard from for {PEER_TTL:g} s"))
        return events

    async def probe(self, cluster, now: float) -> list[tuple[str, str, str, str]]:
        if self._probed is not None and now - self._probed < PEER_PROBE_S:
            return []
        self._probed = now
        beaconing = {(p.get("ip"), p.get("port")) for p in cluster.peers()}
        targets = [t for t in cluster.manual_targets()
                   if (t["ip"], t["port"]) not in beaconing]
        answers = await asyncio.gather(*(cluster.fetch_peer(t) for t in targets),
                                       return_exceptions=True)
        events = []
        for t, answer in zip(targets, answers, strict=True):
            addr = peer_addr(t["ip"], t["port"])
            missed = self.misses.get(addr, 0)
            if isinstance(answer, dict):
                if missed >= PEER_PROBE_MISSES:
                    events.append(("peer_back", f"peer:{addr}", addr, addr))
                self.misses[addr] = 0
            else:
                self.misses[addr] = missed + 1
                if missed + 1 == PEER_PROBE_MISSES:
                    events.append(("peer_offline", f"peer:{addr}", addr,
                                   f"{addr}: no answer to {PEER_PROBE_MISSES} requests "
                                   f"{PEER_PROBE_S:g} s apart"))
        return events


def _units(st: dict) -> dict:
    return {u.get("unit"): (u.get("state"), u.get("cause") or "") for u in st.get("units") or []}


def snapshot(statuses: dict, prev: Optional[dict] = None) -> dict:
    """What status_events compares: each mapping's state, clients, connect-out link,
    polled units and the safe values written. Given the previous snapshot, a unit that had stopped answering stays so
    while it is "unknown" again (its mapping restarted, e.g. on a save), so its return is
    still reported."""
    snap = {}
    for mid, st in statuses.items():
        units = _units(st)
        before = (prev or {}).get(mid)
        if before is not None and len(before) > 3:
            for unit, was in before[3].items():
                if was == _SILENT and units.get(unit, ("",))[0] == "unknown":
                    units[unit] = _SILENT
        snap[mid] = (st.get("state"), {c.get("peer") for c in st.get("clients") or []},
                     st.get("link") or "", units, _safe_written(st))
    return snap


def baseline(statuses: dict) -> dict:
    """The snapshot the watcher starts from (right after the mappings started): one that
    failed to start, or whose connect-out link is down already, is left out, so that this
    is reported (once) like a later failure."""
    return {mid: snap for mid, snap in snapshot(statuses).items()
            if snap[0] != "error" and snap[2] != "down"}


def message(event: str, mapping: str, detail: str, url: str = "",
            name: str = "") -> tuple[str, str]:
    """(title, text) of a notification; `url`: the web UI's public URL (a last line);
    `name`: this instance's name (Settings -> Admin interface), else the host name. The
    title is one line (logtext.one_line): it is the e-mail's Subject, which a line break in
    a mapping name would make invalid."""
    host = name or socket.gethostname()
    head = one_line(f"ser2netweb {host}: {mapping + ' — ' if mapping else ''}{title(event)}")
    return head, head + (f"\n{detail}" if detail else "") + (f"\n{url}" if url else "")


def webhook_body(fmt: str, event: str, mapping: str, detail: str, chat_id: str = "",
                 url: str = "", name: str = "", instance_id: str = "") -> dict:
    head, text = message(event, mapping, detail, url, name)
    if fmt == "slack":
        return {"text": text}
    if fmt == "discord":  # shown as written: an @everyone or a mention in it pings no one
        return {"content": text[:2000], "allowed_mentions": {"parse": []}}
    if fmt == "telegram":
        return {"chat_id": chat_id, "text": text[:4096]}
    if fmt == "teams":  # Teams "Workflows" incoming webhook: an Adaptive Card message
        return {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "version": "1.4",
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "body": [{"type": "TextBlock", "text": head, "weight": "Bolder", "wrap": True},
                                 {"type": "TextBlock", "text": detail or " ", "wrap": True}]
                        + ([{"type": "TextBlock", "text": url, "wrap": True}] if url else [])}}]}
    # "node": the instance's name or the host name; "hostname" and "instance_id" (the
    # cluster id) tell instances on one host apart for a program
    body = {"event": event, "mapping": mapping, "detail": detail, "text": text,
            "node": name or socket.gethostname(), "hostname": socket.gethostname(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "version": __version__}
    if instance_id:
        body["instance_id"] = instance_id
    if url:  # only with a public URL (Settings -> Reverse proxy)
        body["url"] = url
    return body


# what quote() leaves of a URL's path and query: RFC 3986's delimiters and "%", so an
# already percent-encoded URL stays as it is (quote() never touches letters, digits, _.-~)
_URL_SAFE = "/%:@!$&'()*+,;="


def ascii_url(url: str) -> str:
    """`url` as a request can carry it, what a browser makes of one typed with letters
    outside ASCII: the host IDNA-encoded (xn--...; DNS and TLS do that themselves, but
    the Host header would go out as Latin-1 or fail), the path and query percent-encoded
    as UTF-8 (http.client sends the request line as ASCII). An ASCII URL stays as it is.
    Done when sending: the stored URL stays as it was typed."""
    if url.isascii():
        return url
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if not host.isascii():
        host = host.encode("idna").decode("ascii")   # UnicodeError: not a host name
    netloc = f"[{host}]" if ":" in host else host     # (IPv6)
    if parts.port is not None:
        netloc += f":{parts.port}"
    if parts.username is not None:
        user = urllib.parse.quote(parts.username, safe=_URL_SAFE)
        if parts.password is not None:
            user += ":" + urllib.parse.quote(parts.password, safe=_URL_SAFE)
        netloc = f"{user}@{netloc}"
    return urllib.parse.urlunsplit((parts.scheme, netloc,
                                    urllib.parse.quote(parts.path, safe=_URL_SAFE),
                                    urllib.parse.quote(parts.query, safe=_URL_SAFE + "?"), ""))


def send_webhook(settings, event: str, mapping: str, detail: str, url: str = "",
                 name: str = "", instance_id: str = "") -> str:
    """POST the message to the webhook. Returns a warning, or "". A redirect (301/302/303)
    is followed, as a GET without the message: some receivers answer a POST they handled
    that way, others moved (http:// to https://, a new path) and never got the message, so
    the 2xx at the end of it doesn't tell whether it arrived. (``url``: the link to this
    ser2netweb that the message carries, not the webhook's address.)"""
    target = settings.webhook_url.strip()
    # http(s) only, also when a config.json with another URL was tolerated on load
    # (NotificationSettings.validate refuses it when settings are saved)
    if not target.lower().startswith(("https://", "http://")):
        raise OSError("the webhook URL must start with https:// (or http://)")
    body = json.dumps(webhook_body(settings.webhook_format, event, mapping, detail,
                                   str(settings.telegram_chat_id), url, name,
                                   instance_id)).encode()
    req = urllib.request.Request(  # noqa: S310 (http(s) only)
        ascii_url(target), data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 (http(s) only)
        if r.status >= 300:
            raise OSError(f"webhook answered HTTP {r.status}")
        final = r.geturl()
    if final == req.full_url:
        return ""
    return (f"webhook ({settings.webhook_format}) was redirected to {_origin(final)}: if the "
            f"message didn't arrive, set the address it redirects to")


def _origin(url: str) -> str:
    """scheme://host[:port] of *url* - not its path or query, which may hold a token."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


def sender(settings, name: str = "") -> str:
    """The From of a notification e-mail: the address set (ser2net@<host name> without one),
    with "ser2netweb <name>" as its display name when the instance has a name and the address
    set has none (the name is never put into the address itself)."""
    host = socket.gethostname()
    address = settings.email_from.strip() or f"ser2net@{host}"
    shown, _addr = parseaddr(address)
    return formataddr((f"ser2netweb {name}", address)) if name and not shown else address


def send_email(settings, event: str, mapping: str, detail: str, url: str = "",
               name: str = "") -> None:
    """One message to every recipient (all of them in To:), or with
    settings.email_separately one message per recipient in the same SMTP session (only that
    recipient in To:)."""
    head, text = message(event, mapping, detail, url, name)
    host = socket.gethostname()
    msg = EmailMessage()
    msg["Subject"] = head
    msg["From"] = sender(settings, name)
    msg["To"] = ", ".join(settings.recipients)
    # quoted-printable keeps the message 7-bit, so Turkish text and non-ASCII mapping names
    # also pass servers without 8BITMIME
    where = f"{name} ({host})" if name and name != host else host
    msg.set_content(text + "\n\n-- \n" + _("ser2netweb {0} on {1}").format(__version__, where)
                    + "\n", cte="quoted-printable")
    ctx = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                             timeout=TIMEOUT_S, context=ctx)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_S)
    with smtp:
        if settings.smtp_security == "starttls":
            smtp.starttls(context=ctx)
        if settings.smtp_username:
            login(smtp, settings.smtp_username, settings.smtp_password)
        if not getattr(settings, "email_separately", False):  # (settings made before it)
            smtp.send_message(msg)
            return
        for rcpt in settings.recipients:
            del msg["To"]
            msg["To"] = rcpt
            smtp.send_message(msg, to_addrs=[rcpt])


def login(smtp: smtplib.SMTP, user: str, password: str) -> None:
    """smtp.login(user, password), also for a user name or password with letters outside
    ASCII: smtplib encodes those as ASCII and fails before it sends anything, so they go
    UTF-8 encoded here — AUTH PLAIN (RFC 4616), else LOGIN, else CRAM-MD5, as the server
    offers them. ASCII credentials take smtplib's own way, as before."""
    if user.isascii() and password.isascii():
        smtp.login(user, password)
        return
    smtp.ehlo_or_helo_if_needed()
    if not smtp.has_extn("auth"):
        raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")
    offered = smtp.esmtp_features["auth"].upper().split()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")
    error: Optional[Exception] = None
    for mech in ("PLAIN", "LOGIN", "CRAM-MD5"):
        if mech not in offered:
            continue
        if mech == "PLAIN":
            code, resp = smtp.docmd("AUTH", "PLAIN " + b64(f"\0{user}\0{password}".encode()))
        elif mech == "LOGIN":
            code, resp = smtp.docmd("AUTH", "LOGIN " + b64(user.encode()))
            if code == 334:
                code, resp = smtp.docmd(b64(password.encode()))
        else:
            code, resp = smtp.docmd("AUTH", "CRAM-MD5")
            if code == 334:
                digest = hmac.HMAC(password.encode(), base64.b64decode(resp), "md5").hexdigest()
                code, resp = smtp.docmd(b64(f"{user} {digest}".encode()))
        if code in (235, 503):   # 503: already signed in
            return
        error = smtplib.SMTPAuthenticationError(code, resp)
    raise error or smtplib.SMTPException("No suitable authentication method found.")


class Notifier:
    def __init__(self, state) -> None:
        self.state = state
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._last: dict[tuple[str, str], float] = {}
        self._sent: deque = deque()
        self._prev: dict = {}
        self._tasks: list[asyncio.Task] = []
        self.disk_low = False      # the data disk is below storage.floor() (until room again)
        self._disk_checked: Optional[float] = None  # time.monotonic() of the last look
        self.silent: set[str] = set()  # mappings past their no-data alarm (silence_events)
        self.peers = PeerWatch()
        self._started: Optional[float] = None  # time.monotonic() of start(); None once sent

    @property
    def settings(self):
        return self.state.config.notifications

    async def start(self) -> None:
        self._prev = baseline(self.state.supervisor.all_status())
        self._started = time.monotonic()
        self._tasks = [asyncio.create_task(self._watch(), name="notify-watch"),
                       asyncio.create_task(self._worker(), name="notify-send")]

    async def stop(self) -> None:
        """Stop watching; what is queued (or being sent) gets DRAIN_S more to go out."""
        if self._tasks:
            watcher = self._tasks[0]
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._queue.join(), DRAIN_S)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    def emit(self, event: str, key: str, mapping: str, detail: str = "") -> bool:
        """Queue an event if it is enabled and not a repeat; True when queued. A client
        event's address is masked here (mask_personal_data); its repeat key stays the
        address itself."""
        s = self.settings
        if not s.enabled or event not in s.events:
            return False
        now = time.monotonic()
        last = self._last.get((event, key))
        if last is not None and now - last < s.repeat_s:
            return False
        self._last.pop((event, key), None)  # added at the end: in the order of their times
        self._last[(event, key)] = now
        while len(self._last) > MAX_KEYS:
            del self._last[next(iter(self._last))]
        if s.mask_personal_data and event in CLIENT_EVENTS:
            detail = mask_address(detail)
        try:
            self._queue.put_nowait((event, mapping, detail))
        except asyncio.QueueFull:
            return False
        return True

    def _forget_expired(self) -> None:
        """Drop the events sent longer than repeat_s ago: they can't hold a repeat back
        any more (a client event is kept per address, a failed sign-in per IP)."""
        cutoff = time.monotonic() - self.settings.repeat_s
        while self._last:
            key, when = next(iter(self._last.items()))
            if when > cutoff:
                break
            del self._last[key]

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            try:
                self._forget_expired()
                statuses = self.state.supervisor.all_status()
                names = {m.id: m.name for m in self.state.config.mappings}
                for event, key, name, detail in status_events(self._prev, statuses, names):
                    self.emit(event, key, name, detail)
                self._prev = snapshot(statuses, self._prev)
                self.check_silence(statuses)
                now = time.monotonic()
                if self._disk_checked is None or now - self._disk_checked >= DISK_CHECK_S:
                    self._disk_checked = now
                    await self.check_disk()
                await self.check_peers()
                self.check_started(statuses)
            except Exception as e:  # never let the watcher die
                self.state.log(f"notifications: watcher error: {e}")

    def check_silence(self, statuses: dict) -> None:
        """A mapping's device past its no-data alarm, and talking again: logged (all.log
        and the mapping's log) and sent (no_data / data_back)."""
        events, self.silent = silence_events(self.silent, statuses,
                                             self.state.config.mappings)
        for event, key, name, detail, m in events:
            line = (f"{detail} (no-data alarm: {m.options.no_data_alarm_s} s)"
                    if event == "no_data" else f"data from {detail} again")
            maker = getattr(self.state, "make_mapping_logger", None)
            if maker is not None:
                maker(m)(line)
            else:
                self.state.log(f"[{name}] {line}")
            self.emit(event, key, name, detail)

    async def check_peers(self) -> None:
        """Cluster nodes going offline and coming back (PeerWatch): logged and sent."""
        cluster = getattr(self.state, "cluster", None)
        if cluster is None or not cluster.cluster.active:
            return
        events = self.peers.beacons(cluster.peers(), time.monotonic())
        s = self.settings
        if s.enabled and {"peer_offline", "peer_back"} & set(s.events):
            events += await self.peers.probe(cluster, time.monotonic())
        for event, key, name, detail in events:
            self.state.log(f"cluster: node {name!r} "
                           f"{'offline' if event == 'peer_offline' else 'back'}: {detail}")
            self.emit(event, key, name, detail)

    def check_started(self, statuses: dict) -> None:
        """SERVICE_STARTED_S after the start: which mappings run, logged and sent once."""
        if self._started is None or time.monotonic() - self._started < SERVICE_STARTED_S:
            return
        self._started = None
        detail = service_summary(self.state.config.mappings, statuses)
        hang = getattr(self.state, "previous_hang", "")
        self.state.log(f"start-up: {detail}")
        if hang:
            detail += "\n" + hang.removeprefix("WARNING: ")
        self.emit("service_started", "service", "", detail)

    async def check_disk(self) -> None:
        """Look at the data folder's free space: going below storage.floor() is logged and
        sends disk_low; having FREE_RESUME times the floor again is logged."""
        found = await asyncio.to_thread(storage.space, self.state.data_dir)
        if found is None:
            return
        free, total = found
        need = storage.floor(total)
        text = (f"{storage.mb(free)} free of {storage.mb(total)} on the disk of "
                f"{self.state.data_dir}; traces pause below {storage.mb(need)}")
        if not self.disk_low and free < need:
            self.disk_low = True
            self.state.log(f"WARNING: the data disk is almost full: {text}")
            self.emit("disk_low", "data", "", text)
        elif self.disk_low and free >= need * storage.FREE_RESUME:
            self.disk_low = False
            self.state.log(f"the data disk has room again: {text}")

    async def _worker(self) -> None:
        while True:
            event, mapping, detail = await self._queue.get()
            try:
                await self._send(event, mapping, detail)
            finally:
                self._queue.task_done()

    async def _send(self, event: str, mapping: str, detail: str) -> None:
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= MAX_PER_MINUTE:
            self.state.log(f"notifications: rate limit ({MAX_PER_MINUTE}/min), "
                           f"not sent: {event} {mapping}")
            return
        self._sent.append(now)
        warnings: list[str] = []
        errors = await self.deliver(event, mapping, detail, warnings=warnings)
        what = f"{event}, mapping {mapping!r}" if mapping else event
        for err in [str(e) for e in errors] + warnings:
            self.state.log(f"notifications: {err} ({what})")

    async def deliver(self, event: str, mapping: str, detail: str,
                      settings=None, warnings: Optional[list] = None) -> list["Failure"]:
        """Send to every configured channel now; returns the channels that failed (empty
        = all sent), and adds to *warnings* what was sent but may not have arrived (a
        redirected webhook). Messages are in the UI's default language (Settings -> Web UI
        access), or English when that is "the browser's" (the test message too: an empty
        detail gives its text in that language), name the instance by its name (Settings ->
        Admin interface) or else the host name, and end with the UI's public URL when one is
        set (Settings -> Reverse proxy)."""
        s = settings or self.settings
        token = i18n.set_language(self.state.config.admin_ui.language)
        try:
            if event == "test" and not detail:
                detail = _("If you can read this, ser2netweb can reach you.")
            return await self._deliver(s, event, mapping, detail,
                                       [] if warnings is None else warnings)
        finally:
            i18n.reset(token)

    async def _deliver(self, s, event: str, mapping: str, detail: str,
                       warnings: list) -> list["Failure"]:
        errors = []
        cfg = self.state.config
        url = proxy.public_url(cfg.admin_ui)
        name = configured_name(cfg.admin_ui)   # '' = the host name
        if s.webhook_url.strip():
            try:
                if warning := await asyncio.to_thread(send_webhook, s, event, mapping, detail,
                                                      url, name=name,
                                                      instance_id=cfg.instance_id):
                    warnings.append(warning)
            except Exception as e:
                errors.append(Failure("webhook", s.webhook_format, e, s.webhook_url.strip()))
        if s.recipients:
            try:
                await asyncio.to_thread(send_email, s, event, mapping, detail, url, name=name)
            except Exception as e:
                errors.append(Failure("e-mail", f"{s.smtp_host}:{s.smtp_port}", e,
                                      s.smtp_username + s.smtp_password))
        return errors


class Failure:
    """A channel a notification did not reach: str() is the English line for the log,
    shown() the message for the page, in its language."""

    def __init__(self, channel: str, where: str, error: BaseException, sent: str = ""):
        self.channel, self.where, self.error = channel, where, error
        self.non_ascii = not sent.isascii()   # in the URL / the user name or password

    def __str__(self) -> str:
        if self.channel == "webhook":
            return f"webhook ({self.where}) failed: {_reason(self.error)}"
        return f"e-mail via {self.where} failed: {_reason(self.error)}"

    def shown(self) -> str:
        e = self.error
        if isinstance(e, UnicodeError):   # (what is still left: a host name IDNA refuses ...)
            why = (_("the webhook URL has characters that can't be sent") if self.channel == "webhook"
                   else _("the user name or password has characters that can't be sent"))
        elif isinstance(e, smtplib.SMTPAuthenticationError):
            why = _("the server did not accept the user name or password (SMTP {0})").format(
                e.smtp_code)
            if self.non_ascii:
                why += " " + _("— they have letters outside ASCII, which some servers don't take")
        elif isinstance(e, smtplib.SMTPNotSupportedError) and "AUTH" in str(e):
            why = _("the server offers no sign-in (SMTP AUTH): leave the user name empty")
        elif isinstance(e, smtplib.SMTPNotSupportedError) and "STARTTLS" in str(e):
            why = _("the server does not offer STARTTLS: choose another security setting")
        else:
            why = explain(e, "net") or _reason(e)
        if self.channel == "webhook":
            return _("Webhook ({0}) failed: {1}").format(self.where, why)
        return _("E-mail via {0} failed: {1}").format(self.where, why)


def _reason(e: BaseException) -> str:
    code = getattr(e, "code", None)
    reason: Optional[object] = getattr(e, "reason", None)
    text = str(reason or e) or type(e).__name__
    return f"HTTP {code} {text}" if isinstance(code, int) else text
