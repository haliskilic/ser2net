"""Optional per-mapping MQTT publisher (serial -> MQTT northbound).

Publishes newline-delimited serial lines to an MQTT topic so meters, sensors and
other line-oriented serial devices can feed an IIoT broker / Unified Namespace.
A retained ``<base_topic>/status`` birth/death (online/offline, with an MQTT
Last-Will) lets subscribers see link state.

paho-mqtt is an OPTIONAL dependency (not in the offline bootstrap): if it isn't
installed or fails to import, publishing is disabled with a log line saying which
(app.optional) and the bridge runs normally.
The paho client runs its own network thread (loop_start) and ``publish()`` is
thread-safe, so feeding it from the asyncio serial loop never blocks the loop. That
thread waits for its sockets with select.select(), which refuses descriptors at or above
FD_SETSIZE (1024); on POSIX the client waits with poll() instead (``PollSelect``).
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import math
import select
from typing import Any, Callable, Optional

from .. import optional

MAX_LINE_BUFFER = 65536  # drop the oldest bytes if a newline never arrives (no unbounded growth)


class MqttPublisher:
    """``will`` replaces the retained <base>/status "offline" Last-Will and ``birth``
    the "online" message (Sparkplug B sends NDEATH / NBIRTH instead); a callable
    ``will`` is asked again after every disconnect (in paho's thread), so the next
    connection carries a fresh one (the Sparkplug bdSeq). ``on_connect``
    runs on the event loop after every (re)connect; ``subscriptions`` maps topics to
    handlers ``(topic, payload)`` that also run on the event loop — paho's network
    thread never touches the bridges directly.

    The subscriptions are command topics, and a command is acted on once, when it arrives:
    the broker also sends the last retained message of a topic on every (re)subscribe
    (after each reconnect, restart or config save), flagged as retained, and such a replay
    is dropped and logged unless ``accept_retained``. An empty message is never a command:
    it is what clears a retained one (and reaches subscribers live, without the flag)."""

    def __init__(self, settings, logger=None, client=None, *, will=None,
                 birth: bool = True, on_connect: Optional[Callable[[], None]] = None,
                 subscriptions: Optional[dict] = None, accept_retained: bool = False) -> None:
        self._s = settings
        self._log = logger or (lambda _m: None)
        self._client = client            # injected in tests; otherwise built in connect()
        self._buf = bytearray()
        self._connected = False
        base = settings.base_topic.rstrip("/")
        self._data_topic = base
        self._status_topic = base + "/status"
        self._will = will
        self._birth = birth
        self._on_connect_cb = on_connect
        self._subs: dict[str, Callable[[str, bytes], None]] = dict(subscriptions or {})
        self._accept_retained = accept_retained
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def connect(self) -> bool:
        owned = self._client is None  # an injected client (tests) is driven externally
        if owned:
            try:
                import paho.mqtt.client as mqtt
            except Exception as e:  # missing, or installed but failing to import
                self._log("MQTT publishing disabled: "
                          f"{optional.problem(e, 'paho-mqtt', 'paho.mqtt')}")
                return False
            use_poll(mqtt)
            try:  # paho-mqtt 2.x: the current callback API (no deprecation warning)
                self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                           client_id=self._s.client_id or "")
            except AttributeError:  # paho-mqtt 1.x
                self._client = mqtt.Client(client_id=self._s.client_id or None)
            if self._s.username:
                self._client.username_pw_set(self._s.username, self._s.password)
            if self._s.tls:
                with contextlib.suppress(Exception):
                    self._client.tls_set()
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
        # register the Last-Will (retained offline) before connecting
        self._set_will()
        with contextlib.suppress(Exception):
            self._client.on_connect = self._paho_on_connect
            self._client.on_message = self._paho_on_message
            self._client.on_disconnect = self._paho_on_disconnect
        if owned:
            with contextlib.suppress(Exception):
                self._client.connect_async(self._s.host, self._s.port, keepalive=30)
                self._client.loop_start()
        self._connected = True
        if self._birth:
            self._publish(self._status_topic, b"online", retain=True)
        self._log(f"MQTT publishing serial lines to {self._s.host}:{self._s.port} "
                  f"topic '{self._data_topic}'")
        return True

    def _publish(self, topic: str, payload: bytes, retain: bool = False,
                 qos: Optional[int] = None) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            self._client.publish(topic, payload, qos=self._s.qos if qos is None else qos,
                                 retain=retain)

    def publish_raw(self, topic: str, payload: bytes, qos: Optional[int] = None,
                    retain: bool = False) -> None:
        """Publish to an absolute topic (e.g. Sparkplug B spBv1.0/...)."""
        if self._connected:
            self._publish(topic, payload, retain=retain, qos=qos)

    def _set_will(self) -> None:
        will = self._will() if callable(self._will) else self._will
        will = will or (self._status_topic, b"offline", self._s.qos, True)
        with contextlib.suppress(Exception):
            self._client.will_set(will[0], will[1], qos=will[2], retain=will[3])

    def _paho_on_disconnect(self, client, userdata, *args) -> None:
        if callable(self._will):  # paho reconnects with whatever will is set now
            self._set_will()

    # ---- paho callbacks (its network thread) -> the event loop ----
    def _call_soon(self, fn, *args) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # loop shutting down
            loop.call_soon_threadsafe(fn, *args)

    def _paho_on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
            return
        self._call_soon(self.handle_connect)

    def _paho_on_message(self, client, userdata, msg) -> None:
        self._call_soon(self.handle_message, msg.topic, bytes(msg.payload),
                        bool(getattr(msg, "retain", False)))

    def handle_connect(self) -> None:
        """After every (re)connect: subscribe again, then the owner's callback."""
        if self._client is None:
            return
        for topic in self._subs:
            with contextlib.suppress(Exception):
                self._client.subscribe(topic, qos=1)
        if self._on_connect_cb is not None:
            try:
                self._on_connect_cb()
            except Exception as e:
                self._log(f"MQTT on-connect handler failed: {e}")

    def handle_message(self, topic: str, payload: bytes, retained: bool = False) -> None:
        """A message on a subscribed topic (on the event loop); ``retained``: the broker
        sent it from its retained store (paho's ``msg.retain``), not live."""
        if not payload:  # clears a retained message; never a command
            return
        handler = self._subs.get(topic)
        if handler is None:  # a subscription with a wildcard
            handler = next((h for t, h in self._subs.items() if _matches(t, topic)), None)
        if handler is not None and retained:
            if not self._accept_retained:
                self._log(f"MQTT: ignored the retained message on {shown(topic, 120)}; a "
                          "command is applied when it is published, not again from the "
                          "broker's retained store (publish commands without retain)")
                return
            self._log(f"MQTT: applying the retained message on {shown(topic, 120)}")
        if handler is not None:
            try:
                handler(topic, payload)
            except Exception as e:
                self._log(f"MQTT message on {topic}: {e}")

    def publish_value(self, subtopic: str, payload: bytes) -> None:
        """Publish a payload to <base_topic>/<subtopic> (used by Modbus register polling)."""
        if not self._connected:
            return
        self._publish(self._data_topic + "/" + subtopic, payload)

    def feed(self, data: bytes) -> None:
        """Buffer serial bytes and publish each complete (newline-delimited) line."""
        if not self._connected or self._client is None:
            return
        self._buf += data
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl]).rstrip(b"\r")
            del self._buf[:nl + 1]
            if line:
                self._publish(self._data_topic, line)
        if len(self._buf) > MAX_LINE_BUFFER:  # no newline in sight — don't grow forever
            del self._buf[:-4096]

    def close(self, final: Optional[tuple] = None) -> None:
        """Disconnect; ``final`` = (topic, payload, qos, retain) to send first instead of
        the retained "offline" status (Sparkplug B: an NDEATH)."""
        if self._client is None:
            return
        self._connected = False
        if final is not None:
            self._publish(final[0], final[1], qos=final[2], retain=final[3])
        elif self._birth:
            self._publish(self._status_topic, b"offline", retain=True)
        with contextlib.suppress(Exception):
            self._client.loop_stop()
            self._client.disconnect()
        self._client = None


class PollSelect:
    """Stands in for the select module inside paho-mqtt's client module. Its network
    thread waits on the broker socket with select.select(), which raises ValueError for a
    descriptor >= FD_SETSIZE (1024): in a process with more files open than that, paho
    took every such wait for a lost connection and reconnected forever, without a word.
    select() here answers the same call with poll(), which has no such limit; everything
    else is the real select module."""

    def __getattr__(self, name: str) -> Any:
        return getattr(select, name)

    @staticmethod
    def select(rlist, wlist, xlist, timeout=None):
        """select.select(), with its errors for a bad argument: TypeError for something
        that is not a descriptor, ValueError for a negative one, OSError (EBADF) for one
        that is not open."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        lists = [[(_fd(o), o) for o in objs] for objs in (rlist, wlist, xlist)]
        poller = select.poll()
        wanted: dict[int, int] = {}
        for (entries, flag) in zip(lists, (select.POLLIN, select.POLLOUT, select.POLLPRI),
                                   strict=True):
            for fd, _o in entries:
                wanted[fd] = wanted.get(fd, 0) | flag
        for fd, flags in wanted.items():
            poller.register(fd, flags)
        ms = None if timeout is None else math.ceil(timeout * 1000)
        events = dict(poller.poll(ms))
        if any(ev & select.POLLNVAL for ev in events.values()):
            raise OSError(errno.EBADF, "Bad file descriptor")
        # what select() reports for each poll() result (as Linux maps them)
        readable = select.POLLIN | select.POLLHUP | select.POLLERR
        writable = select.POLLOUT | select.POLLERR
        return ([o for fd, o in lists[0] if events.get(fd, 0) & readable],
                [o for fd, o in lists[1] if events.get(fd, 0) & writable],
                [o for fd, o in lists[2] if events.get(fd, 0) & select.POLLPRI])


def _fd(obj) -> int:
    fd = obj if isinstance(obj, int) else getattr(obj, "fileno", None)
    if callable(fd):
        fd = fd()
    if not isinstance(fd, int):
        raise TypeError("argument must be an int, or have a fileno() method.")
    if fd < 0:
        raise ValueError(f"file descriptor cannot be a negative integer ({fd})")
    return fd


def use_poll(paho_client_module) -> bool:
    """Make paho's client wait with poll() (PollSelect), on POSIX; once, and only while that
    module still waits through the select module. Returns whether it does now."""
    current = getattr(paho_client_module, "select", None)
    if isinstance(current, PollSelect):
        return True
    if current is not select or not hasattr(select, "poll"):
        return False  # Windows (select() limits the count, not the value) or a changed paho
    paho_client_module.select = PollSelect()
    return True


def shown(value, limit: int = 40) -> str:
    """repr() of a value that came in over MQTT, cut to ``limit`` characters: what a log line
    or a .../set/result answer quotes of it (a payload can be as large as the broker allows)."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _matches(pattern: str, topic: str) -> bool:
    """MQTT topic filter match ('+' one level, '#' the rest)."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)
