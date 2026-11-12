"""A minimal MQTT 3.1.1 broker for tests, in threads on 127.0.0.1 (an OS-assigned port).

    from _mqtt_broker import MqttBroker
    with MqttBroker() as broker:              # ssl=<server SSLContext> for TLS
        ...  connect a client to 127.0.0.1:broker.port
        broker.wait(lambda: broker.topics("plant/a/status"))
        broker.drop()                          # cut every connection (their Wills go out)

It answers CONNECT (CONNACK with ``connack``, 0 = accepted; any other code closes the
connection), PUBLISH at QoS 0/1/2 (PUBACK; PUBREC, then PUBCOMP), SUBSCRIBE (SUBACK; a
filter in ``refuse`` gets 0x80), PINGREQ and DISCONNECT. ``messages`` holds what was
published, in order: (topic, payload, qos, retain), a Will too once the broker publishes it
(the connection ended without a DISCONNECT). ``connects``: each CONNECT, as a dict (client
id, username, password, will, keepalive). ``tls_errors``: handshakes that failed. There is
no routing to subscribers and no retained store.
"""
from __future__ import annotations

import socket
import ssl as _ssl
import threading
import time


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
        self._conns: set[socket.socket] = set()
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

    def topics(self, topic: str) -> list[tuple]:
        """(payload, qos, retain) of each message published to ``topic``."""
        with self._lock:
            return [(p, q, r) for (t, p, q, r) in self.messages if t == topic]

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

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        if self.ssl is not None:
            try:
                conn = self.ssl.wrap_socket(conn, server_side=True)
            except (OSError, ValueError) as e:
                self._record("tls_errors", str(e))
                conn.close()
                return
        with self._lock:
            self._conns.add(conn)
        will, clean = None, False
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
                    conn.sendall(bytes((0x20, 2, 0, self.connack)))
                    if self.connack:
                        clean = True  # a refused connection publishes no Will
                        break
                elif kind == 3:  # PUBLISH
                    qos, retain = (flags >> 1) & 3, bool(flags & 1)
                    tlen = int.from_bytes(body[:2], "big")
                    topic, pos = body[2:2 + tlen].decode(), 2 + tlen
                    if qos:
                        mid, pos = body[pos:pos + 2], pos + 2
                        conn.sendall(bytes((0x40 if qos == 1 else 0x50, 2)) + mid)
                    self._record("messages", (topic, bytes(body[pos:]), qos, retain))
                elif kind == 6:  # PUBREL
                    conn.sendall(b"\x70\x02" + body[:2])
                elif kind == 8:  # SUBSCRIBE
                    mid, pos, codes = body[:2], 2, bytearray()
                    while pos < len(body):
                        tlen = int.from_bytes(body[pos:pos + 2], "big")
                        topic = body[pos + 2:pos + 2 + tlen].decode()
                        qos = body[pos + 2 + tlen]
                        pos += 3 + tlen
                        self._record("subscriptions", topic)
                        codes.append(0x80 if topic in self.refuse else min(qos, 1))
                    conn.sendall(bytes((0x90, 2 + len(codes))) + mid + bytes(codes))
                elif kind == 12:  # PINGREQ
                    conn.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    clean = True
                    break
        except (OSError, IndexError, ValueError):
            pass
        finally:
            with self._lock:
                self._conns.discard(conn)
            conn.close()
            if will is not None and not clean:
                self._record("messages", will)


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
