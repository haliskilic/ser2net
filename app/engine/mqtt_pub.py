"""Optional per-mapping MQTT publisher (serial -> MQTT northbound).

Publishes newline-delimited serial lines to an MQTT topic so meters, sensors and
other line-oriented serial devices can feed an IIoT broker / Unified Namespace.
A retained ``<base_topic>/status`` birth/death (online/offline, with an MQTT
Last-Will) lets subscribers see link state.

paho-mqtt is an OPTIONAL dependency (not in the offline bootstrap): if it isn't
installed, publishing is disabled with a log line and the bridge runs normally.
The paho client runs its own network thread (loop_start) and ``publish()`` is
thread-safe, so feeding it from the asyncio serial loop never blocks the loop.
"""
from __future__ import annotations

import contextlib

MAX_LINE_BUFFER = 65536  # drop the oldest bytes if a newline never arrives (no unbounded growth)


class MqttPublisher:
    def __init__(self, settings, logger=None, client=None) -> None:
        self._s = settings
        self._log = logger or (lambda _m: None)
        self._client = client            # injected in tests; otherwise built in connect()
        self._buf = bytearray()
        self._connected = False
        base = settings.base_topic.rstrip("/")
        self._data_topic = base
        self._status_topic = base + "/status"

    def connect(self) -> bool:
        owned = self._client is None  # an injected client (tests) is driven externally
        if owned:
            try:
                import paho.mqtt.client as mqtt
            except ImportError:
                self._log("MQTT enabled but paho-mqtt is not installed; publishing disabled "
                          "(pip install paho-mqtt)")
                return False
            self._client = mqtt.Client(client_id=self._s.client_id or None)
            if self._s.username:
                self._client.username_pw_set(self._s.username, self._s.password)
            if self._s.tls:
                with contextlib.suppress(Exception):
                    self._client.tls_set()
        # register the Last-Will (retained offline) before connecting
        with contextlib.suppress(Exception):
            self._client.will_set(self._status_topic, b"offline", qos=self._s.qos, retain=True)
        if owned:
            with contextlib.suppress(Exception):
                self._client.connect_async(self._s.host, self._s.port, keepalive=30)
                self._client.loop_start()
        self._connected = True
        self._publish(self._status_topic, b"online", retain=True)
        self._log(f"MQTT publishing serial lines to {self._s.host}:{self._s.port} "
                  f"topic '{self._data_topic}'")
        return True

    def _publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            self._client.publish(topic, payload, qos=self._s.qos, retain=retain)

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

    def close(self) -> None:
        if self._client is None:
            return
        self._connected = False
        self._publish(self._status_topic, b"offline", retain=True)
        with contextlib.suppress(Exception):
            self._client.loop_stop()
            self._client.disconnect()
        self._client = None
