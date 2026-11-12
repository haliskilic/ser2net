"""A minimal MQTT 3.1.1 broker for tests, in threads on 127.0.0.1 (an OS-assigned port).

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
        self.subs: dict[str, int] = {}   # topic filter -> granted QoS
        self.send_lock = threading.Lock()
        self.next_id = 0

    def send(self, data: bytes) -> None:
        with self.send_lock:
            try:
                self.conn.sendall(data)
            except OSError:
                pass  # it is going away; its own thread notices

    def deliver(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        body = len(topic.encode()).to_bytes(2, "big") + topic.encode()
        if qos:
            with self.send_lock:
                self.next_id = self.next_id % 0xFFFF + 1
                body += self.next_id.to_bytes(2, "big")
        self.send(_packet(0x30 | (qos << 1) | int(retain), body + payload))


class MqttBroker:
    __test__ = False   # not a test class

    def __init__(self, ssl: _ssl.SSLContext | None = None, connack: int = 0,
                 refuse=()):
        self.ssl = ssl
        self.connack = connack
        self.refuse = set(refuse)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.connects: list[dict] = []
        self.messages: list[tuple] = []
        self.subscriptions: list[str] = []
        self.tls_errors: list[str] = []
        self.retained: dict[str, tuple[bytes, int]] = {}  # topic -> (payload, qos)
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

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        """Publish as another client would: recorded, kept if retained, and routed."""
        self._published(topic, bytes(payload), qos, retain)

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

    def _published(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        with self._lock:
            self.messages.append((topic, payload, qos, retain))
            if retain:
                if payload:
                    self.retained[topic] = (payload, qos)
                else:
                    self.retained.pop(topic, None)
            targets = [(c, max(q for f, q in c.subs.items() if matches(f, topic)))
                       for c in self._clients if any(matches(f, topic) for f in c.subs)]
            self._lock.notify_all()
        for client, granted in targets:  # live: without the retain flag (3.3.1.3)
            client.deliver(topic, payload, min(qos, granted), False)

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
        will, clean, joined = None, False, False
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
                    self._record("connects", info)
                    client.send(bytes((0x20, 2, 0, self.connack)))
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
                    if qos:
                        mid, pos = body[pos:pos + 2], pos + 2
                        client.send(bytes((0x40 if qos == 1 else 0x50, 2)) + mid)
                    self._published(topic, bytes(body[pos:]), qos, retain)
                elif kind == 6:  # PUBREL
                    client.send(b"\x70\x02" + body[:2])
                elif kind == 8:  # SUBSCRIBE
                    mid, pos, codes, taken = body[:2], 2, bytearray(), []
                    while pos < len(body):
                        tlen = int.from_bytes(body[pos:pos + 2], "big")
                        topic = body[pos + 2:pos + 2 + tlen].decode()
                        qos = body[pos + 2 + tlen]
                        pos += 3 + tlen
                        self._record("subscriptions", topic)
                        if topic in self.refuse:
                            codes.append(0x80)
                        else:
                            codes.append(min(qos, 1))
                            taken.append((topic, min(qos, 1)))
                    with self._lock:
                        client.subs.update(taken)
                        kept = [(t, p, q, g) for f, g in taken
                                for t, (p, q) in self.retained.items() if matches(f, t)]
                    client.send(_packet(0x90, mid + bytes(codes)))
                    for t, p, q, g in kept:  # what the broker keeps, flagged retained
                        client.deliver(t, p, min(q, g), True)
                elif kind == 10:  # UNSUBSCRIBE
                    pos = 2
                    with self._lock:
                        while pos < len(body):
                            tlen = int.from_bytes(body[pos:pos + 2], "big")
                            client.subs.pop(body[pos + 2:pos + 2 + tlen].decode(), None)
                            pos += 2 + tlen
                    client.send(b"\xb0\x02" + body[:2])
                elif kind == 12:  # PINGREQ
                    client.send(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    clean = True
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
                self._published(*will)


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


def _parse_connect(body: bytes) -> dict:
    def text(pos: int) -> tuple[bytes, int]:
        n = int.from_bytes(body[pos:pos + 2], "big")
        return bytes(body[pos + 2:pos + 2 + n]), pos + 2 + n

    _name, pos = text(0)
    flags = body[pos + 1]
    keepalive = int.from_bytes(body[pos + 2:pos + 4], "big")
    client_id, pos = text(pos + 4)
    will = None
    if flags & 0x04:
        wtopic, pos = text(pos)
        wpayload, pos = text(pos)
        will = (wtopic.decode(), wpayload, (flags >> 3) & 3, bool(flags & 0x20))
    username = password = None
    if flags & 0x80:
        username, pos = text(pos)
    if flags & 0x40:
        password, pos = text(pos)
    return {"client_id": client_id.decode(), "keepalive": keepalive, "will": will,
            "username": username.decode() if username is not None else None,
            "password": password.decode() if password is not None else None}
