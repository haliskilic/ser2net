"""Event notifications: tell someone when a serial device disappears or comes back,
a mapping fails to start, clients come and go, or a sign-in fails.

The Notifier watches the supervisor's status snapshot (every POLL_S seconds) and
turns state changes into events; the login route reports failed sign-ins itself.
Each enabled event is sent to the configured webhook (generic JSON, Slack, Microsoft
Teams Workflows, Discord or Telegram) and/or by e-mail (SMTP), from a worker task so
a slow endpoint never holds anything up. The same event for the same mapping is sent
at most once per ``repeat_s``, and at most MAX_PER_MINUTE messages go out per minute.
Standard library only (urllib, smtplib).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import smtplib
import socket
import ssl
import time
import urllib.parse
import urllib.request
from collections import deque
from email.message import EmailMessage
from typing import Optional

from . import __version__, i18n
from .explain import explain
from .i18n import _

POLL_S = 2.0
MAX_PER_MINUTE = 20
TIMEOUT_S = 10.0

def title(event: str) -> str:
    """The event's name in the notification language (see Notifier.deliver)."""
    return {
        "device_lost": _("serial device lost"),
        "device_back": _("serial device back"),
        "start_failed": _("mapping failed to start"),
        "client_connected": _("client connected"),
        "client_disconnected": _("client disconnected"),
        "login_failed": _("failed sign-in"),
        "test": _("test notification"),
    }.get(event, event)
_LOST = ("reconnecting", "device-missing")


def status_events(prev: dict, statuses: dict, names: dict) -> list[tuple[str, str, str, str]]:
    """Events between two status snapshots: [(event, key, mapping name, detail)].
    A mapping seen for the first time produces none (start-up)."""
    events = []
    for mid, st in statuses.items():
        name = names.get(mid)
        if name is None:
            continue
        old = prev.get(mid)
        state, peers = st.get("state"), {c.get("peer") for c in st.get("clients") or []}
        if old is None:
            continue
        old_state, old_peers = old
        device = st.get("device") or ""
        if old_state == "running" and state in _LOST:
            events.append(("device_lost", mid, name, st.get("last_error") or device))
        elif old_state in _LOST and state == "running":
            events.append(("device_back", mid, name, device))
        if state == "error" and old_state != "error":
            events.append(("start_failed", mid, name, st.get("last_error") or ""))
        for p in sorted(peers - old_peers):
            events.append(("client_connected", f"{mid}:{p}", name, p))
        for p in sorted(old_peers - peers):
            events.append(("client_disconnected", f"{mid}:{p}", name, p))
    return events


def snapshot(statuses: dict) -> dict:
    return {mid: (st.get("state"), {c.get("peer") for c in st.get("clients") or []})
            for mid, st in statuses.items()}


def message(event: str, mapping: str, detail: str) -> tuple[str, str]:
    """(title, text) of a notification."""
    host = socket.gethostname()
    head = f"ser2net {host}: {mapping + ' — ' if mapping else ''}{title(event)}"
    return head, head + (f"\n{detail}" if detail else "")


def webhook_body(fmt: str, event: str, mapping: str, detail: str, chat_id: str = "") -> dict:
    head, text = message(event, mapping, detail)
    if fmt == "slack":
        return {"text": text}
    if fmt == "discord":
        return {"content": text[:2000]}
    if fmt == "telegram":
        return {"chat_id": chat_id, "text": text[:4096]}
    if fmt == "teams":  # Teams "Workflows" incoming webhook: an Adaptive Card message
        return {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "version": "1.4",
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "body": [{"type": "TextBlock", "text": head, "weight": "Bolder", "wrap": True},
                                 {"type": "TextBlock", "text": detail or " ", "wrap": True}]}}]}
    return {"event": event, "mapping": mapping, "detail": detail, "text": text,
            "node": socket.gethostname(), "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "version": __version__}


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


def send_webhook(settings, event: str, mapping: str, detail: str) -> None:
    body = json.dumps(webhook_body(settings.webhook_format, event, mapping, detail,
                                   str(settings.telegram_chat_id))).encode()
    req = urllib.request.Request(ascii_url(settings.webhook_url.strip()), data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": f"ser2net/{__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        if r.status >= 300:
            raise OSError(f"webhook answered HTTP {r.status}")


def send_email(settings, event: str, mapping: str, detail: str) -> None:
    head, text = message(event, mapping, detail)
    msg = EmailMessage()
    msg["Subject"] = head
    msg["From"] = settings.email_from.strip() or f"ser2net@{socket.gethostname()}"
    msg["To"] = ", ".join(settings.recipients)
    # quoted-printable keeps the message 7-bit, so Turkish text and non-ASCII mapping names
    # also pass servers without 8BITMIME
    msg.set_content(text + "\n\n-- \n" + _("ser2net {0} on {1}").format(__version__, socket.gethostname())
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
        smtp.send_message(msg)


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

    @property
    def settings(self):
        return self.state.config.notifications

    async def start(self) -> None:
        self._prev = snapshot(self.state.supervisor.all_status())
        self._tasks = [asyncio.create_task(self._watch(), name="notify-watch"),
                       asyncio.create_task(self._worker(), name="notify-send")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    def emit(self, event: str, key: str, mapping: str, detail: str = "") -> bool:
        """Queue an event if it is enabled and not a repeat; True when queued."""
        s = self.settings
        if not s.enabled or event not in s.events:
            return False
        now = time.monotonic()
        last = self._last.get((event, key))
        if last is not None and now - last < s.repeat_s:
            return False
        self._last[(event, key)] = now
        try:
            self._queue.put_nowait((event, mapping, detail))
        except asyncio.QueueFull:
            return False
        return True

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            try:
                statuses = self.state.supervisor.all_status()
                names = {m.id: m.name for m in self.state.config.mappings}
                for event, key, name, detail in status_events(self._prev, statuses, names):
                    self.emit(event, key, name, detail)
                self._prev = snapshot(statuses)
            except Exception as e:  # never let the watcher die
                self.state.log(f"notifications: watcher error: {e}")

    async def _worker(self) -> None:
        while True:
            event, mapping, detail = await self._queue.get()
            now = time.monotonic()
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            if len(self._sent) >= MAX_PER_MINUTE:
                self.state.log(f"notifications: rate limit ({MAX_PER_MINUTE}/min), "
                               f"not sent: {event} {mapping}")
                continue
            self._sent.append(now)
            errors = await self.deliver(event, mapping, detail)
            for err in errors:
                self.state.log(f"notifications: {err}")

    async def deliver(self, event: str, mapping: str, detail: str,
                      settings=None) -> list["Failure"]:
        """Send to every configured channel now; returns the channels that failed (empty
        = all sent). Messages are in the UI's default language (Settings -> Web UI
        access), or English when that is "the browser's" — the test message too (an
        empty detail: its text in that language)."""
        s = settings or self.settings
        token = i18n.set_language(self.state.config.admin_ui.language)
        try:
            if event == "test" and not detail:
                detail = _("If you can read this, ser2net can reach you.")
            return await self._deliver(s, event, mapping, detail)
        finally:
            i18n.reset(token)

    async def _deliver(self, s, event: str, mapping: str, detail: str) -> list["Failure"]:
        errors = []
        if s.webhook_url.strip():
            try:
                await asyncio.to_thread(send_webhook, s, event, mapping, detail)
            except Exception as e:
                errors.append(Failure("webhook", s.webhook_format, e, s.webhook_url.strip()))
        if s.recipients:
            try:
                await asyncio.to_thread(send_email, s, event, mapping, detail)
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
