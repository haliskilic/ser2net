"""A minimal MQTT 3.1.1 / 5.0 broker for tests, in threads on 127.0.0.1 (an OS-assigned port).

    from _mqtt_broker import MqttBroker
    with MqttBroker() as broker:              # ssl=<server SSLContext> for TLS
        ...  connect a client to 127.0.0.1:broker.port
        broker.wait(lambda: broker.topics("plant/a/status"))
        broker.publish("plant/a/x/set", b"1")  # as another client would
        broker.drop()                          # cut every connection (their Wills go out)

It answers CONNECT (CONNACK with ``connack``, 0 = accepted; any other code closes the
connection), PUBLISH at QoS 0/1/2 (PUBACK; PUBREC, then PUBCOMP), SUBSCRIBE (SUBACK; a
filter in ``refuse`` gets 0x80), UNSUBSCRIBE, PINGREQ and DISCONNECT, and routes what is
published to the subscriptions it matches ('+' one level, '#' the rest), at the lower of
the two QoS (at most 1), without the retain flag. A retained message is kept per topic (an
empty one clears it) and sent to each new subscription it matches, flagged retained, as a
broker does after every (re)subscribe. ``messages`` holds what was published, in order:
(topic, payload, qos, retain), a Will too once the broker publishes it (the connection
ended without a DISCONNECT), and what ``publish()`` injected. ``connects``: each CONNECT, as
a dict (client id, username, password, will, keepalive). ``subscriptions``: each filter
subscribed to. ``tls_errors``: handshakes that failed.

MQTT 5 (a CONNECT with protocol level 5; each connection speaks the version it asked for):
``connects`` also has "protocol" (4 or 5), "clean" (clean session / clean start) and
"properties" ({"session_expiry": seconds, ...}, by the names in PROPERTIES; others by their
number); ``properties`` holds each published message's properties, in step with
``messages``. A retained message with a message expiry is not handed out once that has
passed (and with what is left of it before). ``connack`` and a refused filter's SUBACK are
reason codes then (give ``connack`` as the version's own code; a refused filter gets 0x87,
not authorized); ``puback``: the reason code of every PUBACK / PUBREC to an MQTT 5 client
(0x87: the publication is refused, and neither recorded nor routed); ``disconnect(reason)``
sends every MQTT 5 client a DISCONNECT with that reason code and closes the connections.
"""
from __future__ import annotations

import socket
import ssl as _ssl
import threading
import time


class _Client:
    """A client connection: what it subscribed to, and a lock for writing to it (packets
    for it come from its own thread and from those of the clients that publish)."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.v5 = False                  # it connected with protocol level 5
        self.subs: dict[str, int] = {}   # topic filter -> granted QoS
        self.send_lock = threading.Lock()
        self.next_id = 0

    def send(self, data: bytes) -> None:
        with self.send_lock:
            try:
                self.conn.sendall(data)
            except OSError:
                pass  # it is going away; its own thread notices

    def deliver(self, topic: str, payload: bytes, qos: int, retain: bool,
                expiry: int | None = None) -> None:
        body = len(topic.encode()).to_bytes(2, "big") + topic.encode()
        if qos:
            with self.send_lock:
                self.next_id = self.next_id % 0xFFFF + 1
                body += self.next_id.to_bytes(2, "big")
        if self.v5:  # its properties: the message expiry left, if it has one
            body += b"\x00" if expiry is None else b"\x05\x02" + expiry.to_bytes(4, "big")
        self.send(_packet(0x30 | (qos << 1) | int(retain), body + payload))


class MqttBroker:
    __test__ = False   # not a test class

    def __init__(self, ssl: _ssl.SSLContext | None = None, connack: int = 0,
                 refuse=(), puback: int = 0):
        self.ssl = ssl
        self.connack = connack
        self.refuse = set(refuse)
        self.puback = puback
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.connects: list[dict] = []
        self.messages: list[tuple] = []
        self.properties: list[dict] = []   # of each of ``messages`` (MQTT 5; else {})
        self.subscriptions: list[str] = []
        self.tls_errors: list[str] = []
        # topic -> (payload, qos, when its message expiry ends (time.monotonic()) or None)
        self.retained: dict[str, tuple[bytes, int, float | None]] = {}
        self._conns: set = set()
        self._clients: list[_Client] = []
        self._lock = threading.Condition()
        self._closed = False

    def __enter__(self) -> "MqttBroker":
        threading.Thread(target=self._accept, daemon=True, name="mqtt-broker").start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self.sock.close()
        self.drop()

    def drop(self) -> None:
        """Cut every client connection, as a lost link would (their Wills go out)."""
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def disconnect(self, reason: int = 0x8B) -> None:
        """Send every MQTT 5 client a DISCONNECT with ``reason`` (0x8B: server shutting
        down), then cut every connection."""
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            if c.v5:
                c.send(bytes((0xE0, 2, reason, 0)))
        self.drop()

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False,
                properties: dict | None = None) -> None:
        """Publish as another client would: recorded, kept if retained, and routed."""
        self._published(topic, bytes(payload), qos, retain, properties)

    def topics(self, topic: str) -> list[tuple]:
        """(payload, qos, retain) of each message published to ``topic``."""
        with self._lock:
            return [(p, q, r) for (t, p, q, r) in self.messages if t == topic]

    def payloads(self, topic: str) -> list[bytes]:
        return [p for p, _q, _r in self.topics(topic)]

    def wait(self, predicate, timeout: float = 10.0) -> bool:
        end = time.monotonic() + timeout
        with self._lock:
            while not predicate():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._lock.wait(min(left, 0.1))
        return True

    # ---- the broker ----
    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _record(self, attr: str, value) -> None:
        with self._lock:
            getattr(self, attr).append(value)
            self._lock.notify_all()

    def _published(self, topic: str, payload: bytes, qos: int, retain: bool,
                   properties: dict | None = None) -> None:
        properties = dict(properties or {})
        expiry = properties.get("message_expiry")
        with self._lock:
            self.messages.append((topic, payload, qos, retain))
            self.properties.append(properties)
            if retain:
                if payload:
                    ends = None if expiry is None else time.monotonic() + expiry
                    self.retained[topic] = (payload, qos, ends)
                else:
                    self.retained.pop(topic, None)
            targets = [(c, max(q for f, q in c.subs.items() if matches(f, topic)))
                       for c in self._clients if any(matches(f, topic) for f in c.subs)]
            self._lock.notify_all()
        for client, granted in targets:  # live: without the retain flag (3.3.1.3)
            client.deliver(topic, payload, min(qos, granted), False, expiry)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        if self.ssl is not None:
            try:
                conn = self.ssl.wrap_socket(conn, server_side=True)
            except (OSError, ValueError) as e:
                self._record("tls_errors", str(e))
                conn.close()
                return
        client = _Client(conn)
        with self._lock:
            self._conns.add(conn)
        will, clean, joined, v5 = None, False, False, False
        info: dict = {}
        try:
            f = conn.makefile("rb")
            while True:
                head = f.read(1)
                if not head:
                    break
                n, shift = 0, 0
                while True:
                    byte = f.read(1)[0]
                    n |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                body = f.read(n)
                kind, flags = head[0] >> 4, head[0] & 0x0F
                if kind == 1:  # CONNECT
                    info = _parse_connect(body)
                    will = info["will"]
                    v5 = client.v5 = info["protocol"] == 5
                    self._record("connects", info)
                    client.send(bytes((0x20, 3, 0, self.connack, 0) if v5
                                      else (0x20, 2, 0, self.connack)))
                    if self.connack:
                        clean = True  # a refused connection publishes no Will
                        break
                    with self._lock:
                        self._clients.append(client)
                    joined = True
                elif kind == 3:  # PUBLISH
                    qos, retain = (flags >> 1) & 3, bool(flags & 1)
                    tlen = int.from_bytes(body[:2], "big")
                    topic, pos = body[2:2 + tlen].decode(), 2 + tlen
                    refused = bytes((self.puback, 0)) if v5 and self.puback else b""
                    if qos:
                        mid, pos = body[pos:pos + 2], pos + 2
                        client.send(_packet(0x40 if qos == 1 else 0x50, mid + refused))
                    props: dict = {}
                    if v5:
                        props, pos = _properties(body, pos)
                    if not refused:
                        self._published(topic, bytes(body[pos:]), qos, retain, props)
                elif kind == 6:  # PUBREL
                    client.send(b"\x70\x02" + body[:2])
                elif kind == 8:  # SUBSCRIBE
                    mid, pos, codes, taken = body[:2], 2, bytearray(), []
                    if v5:
                        _props, pos = _properties(body, pos)
                    while pos < len(body):
                        tlen = int.from_bytes(body[pos:pos + 2], "big")
                        topic = body[pos + 2:pos + 2 + tlen].decode()
                        qos = body[pos + 2 + tlen] & 3  # (MQTT 5: the options' low bits)
                        pos += 3 + tlen
                        self._record("subscriptions", topic)
                        if topic in self.refuse:
                            codes.append(0x87 if v5 else 0x80)
                        else:
                            codes.append(min(qos, 1))
                            taken.append((topic, min(qos, 1)))
                    now = time.monotonic()
                    with self._lock:
                        client.subs.update(taken)
                        kept = [(t, p, q, g, None if ends is None else max(1, round(ends - now)))
                                for f, g in taken
                                for t, (p, q, ends) in self.retained.items()
                                if matches(f, t) and (ends is None or ends > now)]
                    client.send(_packet(0x90, mid + (b"\x00" if v5 else b"") + bytes(codes)))
                    for t, p, q, g, left in kept:  # what the broker keeps, flagged retained
                        client.deliver(t, p, min(q, g), True, left)
                elif kind == 10:  # UNSUBSCRIBE
                    pos, count = 2, 0
                    if v5:
                        _props, pos = _properties(body, pos)
                    with self._lock:
                        while pos < len(body):
                            tlen = int.from_bytes(body[pos:pos + 2], "big")
                            client.subs.pop(body[pos + 2:pos + 2 + tlen].decode(), None)
                            pos += 2 + tlen
                            count += 1
                    client.send(_packet(0xB0, body[:2] + (bytes(1 + count) if v5 else b"")))
                elif kind == 12:  # PINGREQ
                    client.send(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT (MQTT 5, reason 4: with the Will)
                    clean = not (v5 and body[:1] == b"\x04")
                    break
                # PUBACK (4) for what was delivered at QoS 1: nothing to do
        except (OSError, IndexError, ValueError):
            pass
        finally:
            with self._lock:
                self._conns.discard(conn)
                if joined:
                    self._clients.remove(client)
            conn.close()
            if will is not None and not clean:
                self._published(*will, info.get("will_properties"))


def matches(pattern: str, topic: str) -> bool:
    """MQTT topic filter match ('+' one level, '#' the rest)."""
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


def _packet(header: int, body: bytes) -> bytes:
    n, size = len(body), bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        size.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes((header,)) + bytes(size) + body


# MQTT 5 properties, by their identifier: (name, how the value is encoded)
PROPERTIES = {
    0x01: ("payload_format", 1), 0x02: ("message_expiry", 4), 0x03: ("content_type", "text"),
    0x08: ("response_topic", "text"), 0x09: ("correlation_data", "bytes"),
    0x0B: ("subscription_id", "varint"), 0x11: ("session_expiry", 4),
    0x12: ("assigned_client_id", "text"), 0x13: ("server_keepalive", 2),
    0x15: ("auth_method", "text"), 0x16: ("auth_data", "bytes"),
    0x17: ("request_problem_info", 1), 0x18: ("will_delay", 4),
    0x19: ("request_response_info", 1), 0x1A: ("response_info", "text"),
    0x1C: ("server_reference", "text"), 0x1F: ("reason_string", "text"),
    0x21: ("receive_maximum", 2), 0x22: ("topic_alias_maximum", 2), 0x23: ("topic_alias", 2),
    0x24: ("maximum_qos", 1), 0x25: ("retain_available", 1), 0x26: ("user_property", "pair"),
    0x27: ("maximum_packet_size", 4), 0x28: ("wildcard_subscription_available", 1),
    0x29: ("subscription_id_available", 1), 0x2A: ("shared_subscription_available", 1),
}


def _varint(body: bytes, pos: int) -> tuple[int, int]:
    n, shift = 0, 0
    while True:
        byte = body[pos]
        pos += 1
        n |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return n, pos


def _properties(body: bytes, pos: int) -> tuple[dict, int]:
    """The MQTT 5 properties at ``pos`` ({name: value}; user properties as a list of
    pairs), and where they end."""
    size, pos = _varint(body, pos)
    end, out = pos + size, {}
    while pos < end:
        name, how = PROPERTIES.get(body[pos], (body[pos], None))
        pos += 1
        if isinstance(how, int):
            value, pos = int.from_bytes(body[pos:pos + how], "big"), pos + how
        elif how == "varint":
            value, pos = _varint(body, pos)
        elif how in ("text", "bytes"):
            n = int.from_bytes(body[pos:pos + 2], "big")
            raw, pos = bytes(body[pos + 2:pos + 2 + n]), pos + 2 + n
            value = raw.decode() if how == "text" else raw
        elif how == "pair":
            pair = []
            for _ in range(2):
                n = int.from_bytes(body[pos:pos + 2], "big")
                pair.append(body[pos + 2:pos + 2 + n].decode())
                pos += 2 + n
            out.setdefault(name, []).append(tuple(pair))
            continue
        else:
            raise ValueError(f"unknown property {name}")
        out[name] = value
    return out, end


def _parse_connect(body: bytes) -> dict:
    def text(pos: int) -> tuple[bytes, int]:
        n = int.from_bytes(body[pos:pos + 2], "big")
        return bytes(body[pos + 2:pos + 2 + n]), pos + 2 + n

    _name, pos = text(0)
    protocol, flags = body[pos], body[pos + 1]
    keepalive = int.from_bytes(body[pos + 2:pos + 4], "big")
    pos += 4
    properties: dict = {}
    will_properties: dict = {}
    if protocol == 5:
        properties, pos = _properties(body, pos)
    client_id, pos = text(pos)
    will = None
    if flags & 0x04:
        if protocol == 5:
            will_properties, pos = _properties(body, pos)
        wtopic, pos = text(pos)
        wpayload, pos = text(pos)
        will = (wtopic.decode(), wpayload, (flags >> 3) & 3, bool(flags & 0x20))
    username = password = None
    if flags & 0x80:
        username, pos = text(pos)
    if flags & 0x40:
        password, pos = text(pos)
    return {"client_id": client_id.decode(), "keepalive": keepalive, "will": will,
            "protocol": protocol, "clean": bool(flags & 0x02), "properties": properties,
            "will_properties": will_properties,
            "username": username.decode() if username is not None else None,
            "password": password.decode() if password is not None else None}
