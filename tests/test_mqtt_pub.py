"""MQTT publisher: line buffering + birth/death (Phase 2).

Uses a fake MQTT client injected into MqttPublisher, so this runs cross-platform
with no broker and without paho-mqtt installed. Verifies that complete
newline-delimited serial lines are published to the base topic, partial lines are
buffered, CR is stripped, and online/offline status is published with retain; that the
"online" birth goes out after every CONNACK (a client that drops QoS 0 messages until it
is connected, as paho does, gets it at QoS 0 too), and with a blank client ID once more
after STALE_WILL_S; that while the broker is away at QoS 1/2 polled values keep only the
latest one per topic (sent after the next connect, after the birth) and paho's queue is
bounded (MAX_QUEUED_MESSAGES; what it refuses is counted and logged once per outage);
and that
a command subscription handles a command when it is published, not the broker's retained
replay of it (unless accept_retained), and never an empty message. Also that paho's
network thread waits with poll(), not select() (which refuses descriptors >= 1024), and
with paho-mqtt installed: a publisher whose broker socket is above 1024 connects and
publishes (to a minimal broker on 127.0.0.1), and at QoS 0 the retained status reads
"online" after the first connect and again after the broker lost the connection (and
published the Will). aclose(), what a mapping's stop uses, sends the last message and stops
paho's thread without holding up the event loop, and gives up waiting after its timeout
when the broker takes nothing (also through the runners' stop() and a Sparkplug node);
paho callbacks that come after it change nothing.

Run: python3 tests/test_mqtt_pub.py
"""
import asyncio
import inspect
import os
import select
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _mqtt_broker import MqttBroker
from _optional import need

from app.config import MqttSettings
from app.engine import mqtt_pub
from app.engine.mqtt_pub import MqttPublisher, PollSelect, use_poll


class FakeClient:
    def __init__(self):
        self.published = []   # (topic, payload, qos, retain)
        self.will = None
        self.loop_started = False
        self.disconnected = False

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, host, port, keepalive=30):
        pass

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload), qos, retain))


class PahoLikeClient(FakeClient):
    """As paho does: a QoS 0 message published while not connected is dropped (paho has
    no socket for it yet; QoS 1/2 ones wait in its queue)."""

    def __init__(self):
        super().__init__()
        self.up = False

    def publish(self, topic, payload, qos=0, retain=False):
        if self.up or qos:
            super().publish(topic, payload, qos, retain)


def _pub(fake, topic):
    return [p for (t, p, _q, _r) in fake.published if t == topic]


def test_lines_published_and_buffered():
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="ser2net/dev", qos=1),
                        logger=lambda _m: None, client=fake)
    assert pub.connect() is True
    pub.handle_connect()                      # the broker accepted the connection
    # birth message: online, retained, on <base>/status
    assert ("ser2net/dev/status", b"online", 1, True) in fake.published

    pub.feed(b"line-1\r\nline-2\n")          # two complete lines (CRLF + LF)
    pub.feed(b"partial")                      # buffered, not yet published
    assert _pub(fake, "ser2net/dev") == [b"line-1", b"line-2"], fake.published
    pub.feed(b"-rest\nlast")                  # completes 'partial-rest', 'last' buffered
    assert _pub(fake, "ser2net/dev") == [b"line-1", b"line-2", b"partial-rest"]
    print("complete lines published (CR stripped), partial line buffered  OK")

    # QoS is carried through
    assert all(q == 1 for (t, _p, q, _r) in fake.published if t == "ser2net/dev")
    print("data published with the configured QoS  OK")

    pub.close()
    assert ("ser2net/dev/status", b"offline", 1, True) in fake.published
    assert fake.disconnected and not fake.loop_started
    print("close() publishes retained 'offline' and tears down the client  OK")


def test_disabled_until_connected():
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t"),
                        logger=lambda _m: None, client=fake)
    pub.feed(b"before-connect\n")             # not connected yet -> dropped
    assert _pub(fake, "t") == []
    print("feed() before connect() publishes nothing  OK")


def test_birth_after_each_connect():
    """At the default QoS 0 the birth must wait for the CONNACK (paho drops it before),
    and every later connect needs it again: the broker may have published the Will, and a
    clean stop leaves "offline" retained."""
    fake = PahoLikeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t"),
                        logger=lambda _m: None, client=fake)
    pub.connect()
    assert fake.published == [], "nothing is sent before the broker took the connection"
    fake.up = True
    pub.handle_connect()                      # CONNACK
    assert fake.published == [("t/status", b"online", 0, True)], fake.published
    fake.up = False
    pub._paho_on_disconnect(fake, None)       # lost: the broker publishes the Will
    pub.handle_connect()                      # (a CONNACK can't come without a link)
    fake.up = True
    pub.handle_connect()                      # reconnected
    assert _pub(fake, "t/status") == [b"online", b"online"], fake.published
    pub.close()
    assert fake.published[-1] == ("t/status", b"offline", 0, True)
    pub.handle_connect()                      # a late callback after close: nothing
    assert _pub(fake, "t/status") == [b"online", b"online", b"offline"]
    print("birth sent after every CONNACK, also at QoS 0; none after close()  OK")

    async def through_paho():
        fake = PahoLikeClient()
        pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t"),
                            logger=lambda _m: None, client=fake)
        pub.connect()
        fake.up = True
        pub._paho_on_connect(fake, None, {}, 5)   # refused (not authorised): no birth
        pub._paho_on_connect(fake, None, {}, 0)   # paho's thread: accepted
        await asyncio.sleep(0.05)
        assert fake.published == [("t/status", b"online", 0, True)], fake.published
        pub.close()
    asyncio.run(through_paho())
    print("paho's on_connect leads to the birth on the event loop (not on a refusal)  OK")


def test_birth_again_after_a_stale_will():
    """With a blank client ID the broker can't tie a new connection to the old one, whose
    Will it may publish up to 1.5 x keepalive after the link broke: after the new birth.
    So the birth goes out once more STALE_WILL_S later (with a client ID the broker ends
    the old session when the new one connects, before the CONNACK)."""
    saved = mqtt_pub.STALE_WILL_S
    mqtt_pub.STALE_WILL_S = 0.1
    assert saved > 1.5 * mqtt_pub.KEEPALIVE_S, saved

    async def run():
        def publisher(**kw):
            fake = PahoLikeClient()
            fake.up = True
            pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t", **kw),
                                logger=lambda _m: None, client=fake)
            pub.connect()
            pub.handle_connect()
            return fake, pub

        fake, pub = publisher()
        await asyncio.sleep(0.25)
        assert _pub(fake, "t/status") == [b"online", b"online"], fake.published
        pub.handle_connect()                  # a new session: one more, after the delay
        pub.handle_connect()                  # and another: the earlier timer is dropped
        await asyncio.sleep(0.25)
        assert _pub(fake, "t/status") == [b"online"] * 5, fake.published

        fake, pub = publisher(client_id="plc-1")
        await asyncio.sleep(0.25)
        assert _pub(fake, "t/status") == [b"online"], fake.published

        fake, pub = publisher()
        pub.close()                           # stopped before the delay: nothing more
        await asyncio.sleep(0.25)
        assert _pub(fake, "t/status") == [b"online", b"offline"], fake.published
    try:
        asyncio.run(run())
    finally:
        mqtt_pub.STALE_WILL_S = saved
    print("blank client ID: birth again after STALE_WILL_S, once per session; not with a "
          "client ID or after close()  OK")


class Info:
    def __init__(self, rc):
        self.rc = rc


class QueueClient(PahoLikeClient):
    """paho's QoS 1/2 queue with room for ``room`` messages while not connected."""

    def __init__(self, room):
        super().__init__()
        self.room = room

    def publish(self, topic, payload, qos=0, retain=False):
        if qos and not self.up:
            if self.room <= 0:
                return Info(15)  # MQTT_ERR_QUEUE_SIZE
            self.room -= 1
        super().publish(topic, payload, qos, retain)
        return Info(0 if self.up else 4)


def test_offline_queue():
    """QoS 1/2 while the broker is away: polled values keep the latest one per topic and
    go out after the next connect (after the birth); serial lines and other messages wait
    in paho's queue, bounded, and what it refuses is counted and logged once per outage.
    QoS 0 messages are lost meanwhile (counted)."""
    logs = []
    fake = QueueClient(room=2)
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="p", qos=1),
                        logger=logs.append, client=fake)
    pub.connect()
    pub.publish_value("a", b"1")
    pub.publish_value("b", b"5")
    pub.publish_value("a", b"2")              # replaces a=1
    assert fake.published == [], fake.published
    fake.up = True
    pub.handle_connect()
    assert fake.published == [("p/status", b"online", 1, True), ("p/b", b"5", 1, False),
                              ("p/a", b"2", 1, False)], fake.published
    pub.publish_value("a", b"3")              # connected: straight to paho
    assert fake.published[-1] == ("p/a", b"3", 1, False)

    fake.up = False
    pub.handle_disconnect("Keep alive timeout", True)  # lost
    logs.clear()
    fake.published.clear()
    pub.publish_value("a", b"4")
    for line in (b"x1\n", b"x2\n", b"x3\n", b"x4\n"):  # serial lines: paho's queue
        pub.feed(line)
    assert fake.published == [("p", b"x1", 1, False), ("p", b"x2", 1, False)], fake.published
    assert pub.state["dropped"] == 2, pub.state
    assert logs == ["MQTT: 10000 QoS 1 messages are waiting for h:1883, the most kept; newer "
                    "ones are dropped until it takes them"], logs
    fake.up = True
    pub.handle_connect()
    assert fake.published[-1] == ("p/a", b"4", 1, False), fake.published
    fake.up, fake.room = False, 0
    pub.handle_disconnect("Keep alive timeout", True)
    pub.feed(b"x5\n")
    assert pub.state["dropped"] == 3 and len([m for m in logs if "waiting" in m]) == 2, logs
    print("QoS 1 while away: latest value per point after the connect; paho's queue full: "
          "counted, logged once per outage  OK")

    fake = QueueClient(room=0)
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="p"),
                        logger=logs.append, client=fake)
    pub.connect()
    pub.publish_value("a", b"1")              # QoS 0 while away: lost, as before
    pub.feed(b"l\n")
    fake.up = True
    pub.handle_connect()
    assert fake.published == [("p/status", b"online", 0, True)], fake.published
    assert pub.state["dropped"] == 2, pub.state
    print("QoS 0 while away: nothing kept, counted as dropped  OK")


def test_paho_queue_limit():
    """paho itself: the publisher sets its queue limit before connecting, and paho refuses
    QoS 1 messages beyond it while the broker can't be reached."""
    if not need("paho.mqtt", what="paho-mqtt's QoS 1 queue limit"):
        return
    logs = []
    saved = mqtt_pub.MAX_QUEUED_MESSAGES
    mqtt_pub.MAX_QUEUED_MESSAGES = 5
    with socket.socket() as s:   # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pub = MqttPublisher(MqttSettings(enabled=True, host="127.0.0.1", port=port,
                                     base_topic="plant/meter", qos=1), logger=logs.append)
    try:
        assert pub.connect() is True
        assert pub._client.max_queued_messages == 5
        for n in range(8):
            pub.feed(b"%d kWh\n" % n)
        assert len(pub._client._out_messages) == 5, len(pub._client._out_messages)
        assert pub.state["dropped"] == 3, pub.state
        assert sum("5 QoS 1 messages are waiting" in m for m in logs) == 1, logs
    finally:
        mqtt_pub.MAX_QUEUED_MESSAGES = saved
        pub.close()
    print("paho: queue limit set before connecting; QoS 1 beyond it refused, counted, "
          "logged once  OK")


def test_will_is_set_for_lwt():
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="x", qos=2),
                        logger=lambda _m: None, client=fake)
    pub.connect()
    assert fake.will == ("x/status", b"offline", 2, True), fake.will
    print("Last-Will set to retained 'offline' on the status topic  OK")


class Msg:
    """What paho hands to on_message."""

    def __init__(self, topic, payload, retain):
        self.topic, self.payload, self.retain = topic, payload, retain


def test_retained_commands():
    """Subscriptions are command topics: a command is handled when it is published. The
    copy a broker sends from its retained store on (re)subscribe is dropped (and logged)
    unless accept_retained; an empty message (what clears a retained one) never reaches a
    handler; paho's msg.retain is what tells the two apart."""
    got, logs = [], []

    def handler(topic, payload):
        got.append((topic, payload))

    subs = {"plant/a/pump/set": handler, "spBv1.0/g/DCMD/n/+": handler}
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="plant/a"),
                        logger=lambda _m: None, client=FakeClient(), subscriptions=subs)
    pub.connect()
    pub._log = logs.append
    pub.handle_message("plant/a/pump/set", b"on", retained=True)
    pub.handle_message("spBv1.0/g/DCMD/n/unit1", b"\x0a\x00", retained=True)
    assert got == [] and sum("ignored the retained message" in m for m in logs) == 2, logs
    pub.handle_message("plant/a/pump/set", b"on")
    pub.handle_message("spBv1.0/g/DCMD/n/unit1", b"\x0a\x00")
    assert got == [("plant/a/pump/set", b"on"), ("spBv1.0/g/DCMD/n/unit1", b"\x0a\x00")], got
    for retained in (False, True):
        pub.handle_message("plant/a/pump/set", b"", retained=retained)
    assert len(got) == 2 and len(logs) == 2, (got, logs)
    pub.handle_message("plant/a/other/set", b"on", retained=True)   # not subscribed: no log
    assert len(logs) == 2, logs
    long_topic = "spBv1.0/g/DCMD/n/" + "d" * 5000
    pub.handle_message(long_topic, b"\x0a\x00", retained=True)
    assert len(logs) == 3 and len(logs[-1]) < 400, len(logs[-1])
    print("retained command replays dropped and logged; live ones handled; empty ones never  OK")

    got.clear()
    logs.clear()
    opted = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="plant/a"),
                          logger=lambda _m: None, client=FakeClient(), subscriptions=subs,
                          accept_retained=True)
    opted.connect()
    opted._log = logs.append
    opted.handle_message("plant/a/pump/set", b"on", retained=True)
    opted.handle_message("plant/a/pump/set", b"", retained=True)
    assert got == [("plant/a/pump/set", b"on")], got
    assert logs == ["MQTT: applying the retained message on 'plant/a/pump/set'"], logs
    print("accept_retained: a retained command is handled, and logged as such  OK")

    async def through_paho():   # paho's thread -> the event loop, with msg.retain
        got.clear()
        pub.connect()
        pub._paho_on_message(None, None, Msg("plant/a/pump/set", b"1", True))
        pub._paho_on_message(None, None, Msg("plant/a/pump/set", b"0", False))
        await asyncio.sleep(0.05)
        assert got == [("plant/a/pump/set", b"0")], got
    asyncio.run(through_paho())
    print("paho on_message passes msg.retain through to the event loop  OK")


def test_poll_select_answers_like_select():
    a, b = socket.socketpair()
    try:
        ps = PollSelect()
        assert ps.select([a], [a], [], 0) == select.select([a], [a], [], 0) == ([], [a], [])
        b.send(b"x")
        assert ps.select([a, b], [], [], 1) == select.select([a, b], [], [], 1) == ([a], [], [])
        a.recv(10)
        t0 = time.monotonic()
        assert ps.select([a], [], [], 0.2) == ([], [], [])
        assert 0.15 <= time.monotonic() - t0 < 2, "the timeout is honoured"
        for bad, exc in ((None, TypeError), ("x", TypeError), (-1, ValueError)):
            try:
                ps.select([bad], [], [], 0)
            except exc:
                pass
            else:
                raise AssertionError(f"{bad!r} must raise {exc.__name__}, as select() does")
        gone = socket.socket()
        fd = gone.fileno()
        gone.close()
        try:
            ps.select([fd], [], [], 0)
        except OSError:
            pass
        else:
            raise AssertionError("a closed descriptor must raise OSError, as select() does")
        assert ps.error is select.error, "the rest is the real select module"
    finally:
        a.close()
        b.close()
    print("PollSelect: same answers, timeout and errors as select.select()  OK")


def test_poll_select_above_fd_setsize():
    from _fds import high_fds

    with high_fds():
        a, b = socket.socketpair()
    try:
        assert a.fileno() >= 1024, a.fileno()
        try:
            select.select([a], [], [], 0)
            print("note: this select() takes descriptors >= 1024")
        except ValueError:
            pass  # what paho's thread ran into
        b.send(b"x")
        assert PollSelect().select([a], [a], [], 1) == ([a], [a], [])
    finally:
        a.close()
        b.close()
    print("PollSelect: a socket at descriptor >= 1024 is waited on  OK")


class MiniBroker(threading.Thread):
    """Just enough MQTT 3.1.1 for one publisher: CONNACK, PINGRESP, the topics it publishes."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.connected = threading.Event()
        self.topics = []

    def run(self):
        self.sock.settimeout(20)
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(20)
        f = conn.makefile("rb")
        with conn, f:
            while True:
                head = f.read(1)
                if not head:
                    return
                n, shift = 0, 0
                while True:
                    byte = f.read(1)[0]
                    n |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                body = f.read(n)
                kind = head[0] >> 4
                if kind == 1:  # CONNECT
                    conn.sendall(b"\x20\x02\x00\x00")
                    self.connected.set()
                elif kind == 3:  # PUBLISH (QoS 0)
                    self.topics.append(body[2:2 + int.from_bytes(body[:2], "big")].decode())
                elif kind == 12:  # PINGREQ
                    conn.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return


def test_paho_publisher_above_fd_setsize():
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("SKIP paho-mqtt end to end: paho-mqtt is not installed")
        return
    from _fds import high_fds

    assert "select.select(" in inspect.getsource(mqtt.Client._loop), \
        "paho's network loop no longer calls select.select(): check use_poll()"
    broker = MiniBroker()
    broker.start()
    pub = MqttPublisher(MqttSettings(enabled=True, host="127.0.0.1", port=broker.port,
                                     base_topic="plant/meter", qos=0), logger=lambda _m: None)
    try:
        with high_fds():  # paho's sockets (broker connection, wake-up pair) land >= 1024
            assert pub.connect() is True
            assert isinstance(mqtt.select, PollSelect) and use_poll(mqtt), \
                "the publisher's paho client must wait with poll()"
            assert broker.connected.wait(10), "paho did not connect"
            assert pub._client.socket().fileno() >= 1024, pub._client.socket().fileno()
        pub.feed(b"42.5 kWh\n")
        end = time.monotonic() + 10
        while "plant/meter" not in broker.topics and time.monotonic() < end:
            time.sleep(0.05)
        assert "plant/meter" in broker.topics, broker.topics
    finally:
        pub.close()
    print("paho publisher with its broker socket at descriptor >= 1024 connects and "
          "publishes  OK")


def test_paho_birth_at_qos0():
    """Real paho against a broker: at QoS 0 the retained status reads "online" after the
    first connect, and again after the broker lost the connection (and published the
    Will "offline")."""
    if not need("paho.mqtt", what="paho-mqtt birth at QoS 0 against a test broker"):
        return
    with MqttBroker() as broker:
        pub = MqttPublisher(MqttSettings(enabled=True, host="127.0.0.1", port=broker.port,
                                         base_topic="plant/meter", qos=0),
                            logger=lambda _m: None)

        async def run():
            assert pub.connect() is True
            for _ in range(200):
                if broker.topics("plant/meter/status"):
                    break
                await asyncio.sleep(0.05)
            assert broker.topics("plant/meter/status") == [(b"online", 0, True)], \
                broker.messages
            assert broker.connects[0]["will"] == ("plant/meter/status", b"offline", 0, True)
            broker.drop()                     # the link breaks: the Will goes out
            for _ in range(200):              # paho reconnects (1 s backoff)
                if len(broker.topics("plant/meter/status")) >= 3:
                    break
                await asyncio.sleep(0.05)
            assert broker.topics("plant/meter/status") == [
                (b"online", 0, True), (b"offline", 0, True), (b"online", 0, True)], \
                broker.messages
            pub.close()
        asyncio.run(run())
        assert broker.wait(lambda: len(broker.topics("plant/meter/status")) == 4)
        assert broker.topics("plant/meter/status")[-1] == (b"offline", 0, True)
    print("paho, QoS 0: retained status 'online' after the first connect and after a "
          "reconnect (over the Will), 'offline' at close  OK")


class StuckClient(FakeClient):
    """paho with a broker that takes nothing: loop_stop() waits for the queued messages."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def loop_stop(self):
        self.release.wait(10)
        super().loop_stop()


async def _aclose_checks():
    lines = []
    ticks = 0

    async def ticker():  # the event loop must keep running meanwhile
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tick = asyncio.ensure_future(ticker())
    try:
        fake = FakeClient()
        got = []
        pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t", qos=1),
                            logger=lines.append, client=fake,
                            subscriptions={"t/x/set": lambda t, p: got.append(p)})
        pub.connect()
        lines.clear()
        await pub.aclose()
        assert ("t/status", b"offline", 1, True) in fake.published
        assert fake.disconnected and not lines, lines
        pub.feed(b"after close\n")  # closed: nothing more is published
        assert not _pub(fake, "t")
        published = list(fake.published)
        pub._paho_on_message(None, None, Msg("t/x/set", b"1", False))  # late callbacks
        pub._paho_on_connect(None, None, {}, 0)
        await asyncio.sleep(0.05)
        pub.handle_message("t/x/set", b"1")
        pub.handle_connect()
        assert got == [] and fake.published == published and not lines, (got, lines)

        stuck = StuckClient()
        pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="t", qos=1),
                            logger=lines.append, client=stuck)
        pub.connect()
        lines.clear()
        t0, before = time.monotonic(), ticks
        await pub.aclose(final=("spBv1.0/g/NDEATH/n", b"death", 1, False), timeout=0.3)
        took = time.monotonic() - t0
        assert ("spBv1.0/g/NDEATH/n", b"death", 1, False) in stuck.published
        assert ("t/status", b"offline", 1, True) not in stuck.published
        assert 0.25 < took < 1.0, f"aclose() took {took:.2f} s"
        assert ticks - before >= 10, "aclose() held up the event loop"
        assert lines == ["MQTT: the broker did not take the last messages within 0.3 s; "
                         "closing without waiting for it"], lines
        stuck.release.set()
        await pub.aclose()  # closed already: nothing
    finally:
        tick.cancel()


def test_aclose_does_not_hold_up_the_loop():
    asyncio.run(_aclose_checks())
    print("aclose(): last message sent, paho stopped off the event loop; a broker that takes "
          "nothing is given up on after the timeout; late callbacks do nothing  OK")


async def _runner_stop_checks():
    from app.config import MappingConfig
    from app.engine.bridge import MappingRunner
    from app.engine.modbus_gateway import ModbusGatewayRunner
    from app.engine.sparkplug_node import SparkplugNode

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tick = asyncio.ensure_future(ticker())
    mq = {"enabled": True, "host": "h", "base_topic": "t", "qos": 1}
    points = {"interval_s": 1, "points": [{"name": "temp", "unit": 1, "fn": 3}]}
    runners, clients = [], []
    for kind in ("serial lines", "Modbus plain", "Sparkplug B"):
        stuck = StuckClient()
        if kind == "serial lines":
            m = MappingConfig.from_dict({"name": "m", "serial": {"port": "x"}, "mqtt": mq})
            r = MappingRunner(m, logger=lambda _m: None)
        else:
            m = MappingConfig.from_dict({
                "name": "gw", "serial": {"port": "x"}, "modbus_poll": points,
                "network": {"protocol": "modbus", "port": 1502},
                "mqtt": dict(mq, payload_format="sparkplug_b" if "Sparkplug" in kind
                             else "plain")})
            r = ModbusGatewayRunner(m, logger=lambda _m: None)
        if kind == "Sparkplug B":
            r._sparkplug = SparkplugNode(m, lambda _m: None, lambda p, v: None)
            r._mqtt = r._sparkplug.publisher()
        else:
            r._mqtt = MqttPublisher(m.mqtt, logger=lambda _m: None)
        r._mqtt._client = stuck
        r._mqtt.connect()
        runners.append(r)
        clients.append(stuck)
    try:
        t0, before = time.monotonic(), ticks
        await asyncio.gather(*(r.stop() for r in runners))  # each waits MQTT_CLOSE_TIMEOUT_S
        took = time.monotonic() - t0
        assert mqtt_pub.MQTT_CLOSE_TIMEOUT_S - 0.1 < took < mqtt_pub.MQTT_CLOSE_TIMEOUT_S + 1.5, \
            took
        assert ticks - before >= 100, f"stop() held up the event loop ({ticks - before} ticks)"
        for r, stuck in zip(runners, clients):
            assert r._mqtt is None and stuck.published, stuck.published
            assert r.status.as_dict()["mqtt"] == {}
    finally:
        for stuck in clients:
            stuck.release.set()
        tick.cancel()


def test_runner_stop_does_not_wait_for_paho():
    asyncio.run(_runner_stop_checks())
    print("stop() of a serial-line mapping, a Modbus gateway (plain, Sparkplug B): paho "
          "stopped off the event loop, given up on after the timeout  OK")


def main():
    test_lines_published_and_buffered()
    test_disabled_until_connected()
    test_birth_after_each_connect()
    test_birth_again_after_a_stale_will()
    test_offline_queue()
    test_will_is_set_for_lwt()
    test_retained_commands()
    test_paho_birth_at_qos0()
    test_paho_queue_limit()
    test_aclose_does_not_hold_up_the_loop()
    test_runner_stop_does_not_wait_for_paho()
    if os.name == "posix":
        test_poll_select_answers_like_select()
        test_poll_select_above_fd_setsize()
        test_paho_publisher_above_fd_setsize()
    else:
        print("SKIP PollSelect: POSIX only (Windows' select() limits the count, not the value)")
    print("\nPASS: MQTT publisher (line buffering + birth/death, retained commands, poll())")


if __name__ == "__main__":
    main()
