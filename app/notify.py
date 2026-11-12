"""Event notifications: tell someone when a serial device disappears or comes back,
a mapping fails to start, clients come and go, a sign-in fails, or a Modbus unit that a
gateway polls stops answering or answers again.

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
import contextlib
import json
import smtplib
import socket
import ssl
import time
import urllib.request
from collections import deque
from email.message import EmailMessage
from typing import Optional

from . import __version__, i18n
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
        "unit_offline": _("Modbus unit not answering"),
        "unit_back": _("Modbus unit answering again"),
        "test": _("test notification"),
    }.get(event, event)
_LOST = ("reconnecting", "device-missing")


_SILENT = ("offline", "no reply")  # a polled unit that stopped answering (not: the port down)


def status_events(prev: dict, statuses: dict, names: dict) -> list[tuple[str, str, str, str]]:
    """Events between two status snapshots: [(event, key, mapping name, detail)].
    A mapping seen for the first time produces none (start-up). A Modbus unit is reported
    when it stops answering, and again once it answers (not while its serial port is down:
    device_lost says that)."""
    events = []
    for mid, st in statuses.items():
        name = names.get(mid)
        if name is None:
            continue
        old = prev.get(mid)
        state, peers = st.get("state"), {c.get("peer") for c in st.get("clients") or []}
        if old is None:
            continue
        old_state, old_peers, old_units = old[0], old[1], (old[2] if len(old) > 2 else {})
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
        for unit, now in _units(st).items():
            was = old_units.get(unit)
            if now == _SILENT and was != _SILENT:
                events.append(("unit_offline", f"{mid}:unit{unit}", name, f"unit {unit}"))
            elif was == _SILENT and now[0] == "online":
                events.append(("unit_back", f"{mid}:unit{unit}", name, f"unit {unit}"))
    return events


def _units(st: dict) -> dict:
    return {u.get("unit"): (u.get("state"), u.get("cause") or "") for u in st.get("units") or []}


def snapshot(statuses: dict, prev: Optional[dict] = None) -> dict:
    """What status_events compares: each mapping's state, clients and polled units. Given the
    previous snapshot, a unit that had stopped answering stays so while it is "unknown"
    again (its mapping restarted, e.g. on a save), so its return is still reported."""
    snap = {}
    for mid, st in statuses.items():
        units = _units(st)
        before = (prev or {}).get(mid)
        if before is not None and len(before) > 2:
            for unit, was in before[2].items():
                if was == _SILENT and units.get(unit, ("",))[0] == "unknown":
                    units[unit] = _SILENT
        snap[mid] = (st.get("state"), {c.get("peer") for c in st.get("clients") or []}, units)
    return snap


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


def send_webhook(settings, event: str, mapping: str, detail: str) -> None:
    body = json.dumps(webhook_body(settings.webhook_format, event, mapping, detail,
                                   str(settings.telegram_chat_id))).encode()
    req = urllib.request.Request(settings.webhook_url.strip(), data=body, method="POST",
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
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


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
                self._prev = snapshot(statuses, self._prev)
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
                      settings=None) -> list[str]:
        """Send to every configured channel now; returns the errors (empty = all sent).
        Messages are in the UI's default language (Settings -> Web UI access), or
        English when that is "the browser's"."""
        s = settings or self.settings
        token = i18n.set_language(self.state.config.admin_ui.language)
        try:
            return await self._deliver(s, event, mapping, detail)
        finally:
            i18n.reset(token)

    async def _deliver(self, s, event: str, mapping: str, detail: str) -> list[str]:
        errors = []
        if s.webhook_url.strip():
            try:
                await asyncio.to_thread(send_webhook, s, event, mapping, detail)
            except Exception as e:
                errors.append(f"webhook ({s.webhook_format}) failed: {_reason(e)}")
        if s.recipients:
            try:
                await asyncio.to_thread(send_email, s, event, mapping, detail)
            except Exception as e:
                errors.append(f"e-mail via {s.smtp_host}:{s.smtp_port} failed: {_reason(e)}")
        return errors


def _reason(e: BaseException) -> str:
    code = getattr(e, "code", None)
    reason: Optional[object] = getattr(e, "reason", None)
    text = str(reason or e) or type(e).__name__
    return f"HTTP {code} {text}" if isinstance(code, int) else text
