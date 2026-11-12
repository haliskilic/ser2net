"""Event notifications: tell someone when a serial device disappears or comes back,
a mapping fails to start, clients come and go, a sign-in fails, the data disk is almost
full, a device sends nothing, a cluster node goes offline, or ser2net has started.

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

from . import __version__, i18n, storage
from .engine.cluster import PEER_TTL, peer_addr
from .i18n import _

POLL_S = 2.0
DISK_CHECK_S = 60.0
MAX_PER_MINUTE = 20
TIMEOUT_S = 10.0
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
        "client_connected": _("client connected"),
        "client_disconnected": _("client disconnected"),
        "login_failed": _("failed sign-in"),
        "disk_low": _("data disk almost full"),
        "no_data": _("no data from the serial device"),
        "data_back": _("data from the serial device again"),
        "peer_offline": _("cluster node offline"),
        "peer_back": _("cluster node back"),
        "service_started": _("ser2net started"),
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
    text = (f"ser2net {__version__}: {len(enabled) - len(down)} of {len(enabled)} enabled "
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
        self.disk_low = False      # the data disk is below storage.floor() (until room again)
        self._disk_checked: Optional[float] = None  # time.monotonic() of the last look
        self.silent: set[str] = set()  # mappings past their no-data alarm (silence_events)
        self.peers = PeerWatch()
        self._started: Optional[float] = None  # time.monotonic() of start(); None once sent

    @property
    def settings(self):
        return self.state.config.notifications

    async def start(self) -> None:
        self._prev = snapshot(self.state.supervisor.all_status())
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
