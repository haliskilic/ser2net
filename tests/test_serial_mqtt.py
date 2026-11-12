"""MQTT for serial-to-network mappings (not a Modbus gateway): line ends, writes from
<base_topic>/tx, and the poll string (no broker, no sockets, no serial hardware).

  - mqtt.line_end: "lf" (the default, as before: a CR before the LF is left out), "cr"
    (CR-terminated records, an LF next to the CR left out), "any" (CR or LF); a CR-only
    device publishes nothing with "lf", every record with "cr"; past 64 KiB without a line
    end the oldest bytes are dropped and the log says so once; a stored unknown value
    reads as "lf"; edits refuse it;
  - mqtt.tx_enabled: <base_topic>/tx is subscribed; each message goes to the serial port in
    one write (never mixed into a TCP client's data), "ok" on .../tx/result; refused with
    "error: ..." when read-only, too long or while the queue holds 64 KiB, or the port is
    not open; a retained copy is never written; off: nothing subscribed; a wildcard base
    topic is refused at edit time and takes no writes when stored; mqtt_tx_topics names
    the two topics for the topic checks (the dashboard's CMD tag: test_ui_markup);
  - options.poll_string: sent every poll_interval_s while the port is open, not while a TCP
    client is connected (unless poll_with_clients), not while the port is closed (nothing
    dropped or counted); \\r \\n \\t \\\\ \\xHH escapes; edits refuse a bad escape, an interval
    out of range or one without a string; a stored bad one is not sent (logged);
  - the form: the line end, the tx checkbox and the poll fields carry presence markers.

Run: python3 tests/test_serial_mqtt.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import ConfigError, MappingConfig, MqttSettings, mqtt_tx_topics, poll_bytes
from app.engine import bridge
from app.engine.bridge import MappingRunner
from app.engine.mqtt_pub import MAX_LINE_BUFFER, MqttPublisher
from app.web.routes.forms import _build_mapping_dict, _preserve_unmanaged_fields


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []

    def will_set(self, *a, **k):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, bytes(payload)))

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def lines(fake, topic="dev"):
    return [p for t, p in fake.published if t == topic]


def publisher(line_end, logs=None):
    fake = FakeClient()
    pub = MqttPublisher(MqttSettings(enabled=True, host="h", base_topic="dev", line_end=line_end),
                        logger=(logs.append if logs is not None else None), client=fake)
    pub.connect()
    return pub, fake


# ---- line ends -------------------------------------------------------------------------

def test_line_ends():
    records = b"ST,GS,+0001.25kg\r" * 3
    pub, fake = publisher("lf")
    pub.feed(records)
    assert lines(fake) == [], "LF (default, as before): CR-only records publish nothing"
    pub, fake = publisher("cr")
    pub.feed(records[:10])
    pub.feed(records[10:])
    assert lines(fake) == [b"ST,GS,+0001.25kg"] * 3
    pub.feed(b"a\r\nb\r")  # CRLF in CR mode: the LF is not part of the next line
    assert lines(fake)[-2:] == [b"a", b"b"]
    pub, fake = publisher("cr")
    pub.feed(b"\x02\x01\x0a\x7f\x03\r")  # a binary frame with 0x0A in it stays whole
    assert lines(fake) == [b"\x02\x01\x0a\x7f\x03"]
    pub, fake = publisher("any")
    pub.feed(b"one\rtwo\nthree\r\nfour\r")
    pub.feed(b"\nfive\n")  # a CRLF split across reads: no empty line published
    assert lines(fake) == [b"one", b"two", b"three", b"four", b"five"], lines(fake)
    pub, fake = publisher("lf")
    pub.feed(b"x\r\ny\n")
    assert lines(fake) == [b"x", b"y"], "LF: a CR before it is left out, as before"
    pub, fake = publisher("bogus")  # a stored unknown value: as before
    pub.feed(b"a\rb\n")
    assert lines(fake) == [b"a\rb"]
    print("line end: LF (default), CR, CR or LF; binary frames and split CRLFs  OK")


def test_cut_logged_once():
    logs = []
    pub, fake = publisher("lf", logs)
    for _ in range(3):
        pub.feed(b"x" * (MAX_LINE_BUFFER + 1))
    assert lines(fake) == []
    cut = [ln for ln in logs if "without a line end (LF)" in ln]
    assert len(cut) == 1 and "oldest bytes were dropped" in cut[0], logs
    print("a buffer cut without a line end is logged once  OK")


def test_line_end_validation():
    def m(**mqtt):
        return MappingConfig.from_dict({"name": "s", "serial": {"port": "COM3"},
                                        "network": {"port": 4001},
                                        "mqtt": {"enabled": True, "host": "h",
                                                 "base_topic": "dev", **mqtt}})
    assert MappingConfig().mqtt.line_end == "lf" and MappingConfig().mqtt.tx_enabled is False
    for end in ("lf", "cr", "any"):
        m(line_end=end).validate_input()
    bad = m(line_end="crlf")
    bad.validate()  # (a stored one keeps loading)
    try:
        bad.validate_input()
    except ConfigError as e:
        assert "line end" in str(e), e
    else:
        raise AssertionError("an unknown line end was taken")
    print("line end: edits refuse an unknown one, a stored one loads  OK")


# ---- writes from <base_topic>/tx -------------------------------------------------------

class PortWriter:
    def __init__(self):
        self.writes = []
        self.hold = None

    def write(self, data):
        self.writes.append(bytes(data))

    def is_closing(self):
        return False

    async def drain(self):
        if self.hold is not None:
            await self.hold.wait()


def tx_runner(logs, port_open=True, read_only=False, **mqtt):
    m = MappingConfig.from_dict({
        "name": "scale", "serial": {"port": "/dev/ttyX"},
        "network": {"bind_ip": "127.0.0.1", "port": 4001, "read_only": read_only},
        "mqtt": {"enabled": True, "host": "h", "base_topic": "lab/scale/", "tx_enabled": True,
                 **mqtt}})
    runner = MappingRunner(m, logger=logs.append)
    if port_open:
        runner._link.writer = PortWriter()
    fake = FakeClient()
    runner._mqtt = MqttPublisher(m.mqtt, logger=logs.append, client=fake,
                                 subscriptions=runner._mqtt_subscriptions())
    runner._mqtt.connect()
    runner._mqtt.handle_connect()
    return runner, fake


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def _tx():
    logs = []
    runner, fake = tx_runner(logs)
    assert fake.subscribed == ["lab/scale/tx"], fake.subscribed
    assert runner.mapping.accepts_mqtt_tx
    task = asyncio.create_task(runner._mqtt_tx_loop())
    try:
        pub = runner._mqtt
        pub.handle_message("lab/scale/tx", b"P\r\n")
        pub.handle_message("lab/scale/tx", b"TARE\r\n", retained=True)  # the broker's copy
        pub.handle_message("lab/scale/tx", b"Z\r\n")
        await settle()
        assert runner._link.writer.writes == [b"P\r\n", b"Z\r\n"], "each in one write, in order"
        assert lines(fake, "lab/scale/tx/result") == [b"ok", b"ok"]
        assert runner.status.bytes_out == 6
        assert any("ignored the retained message on 'lab/scale/tx'" in ln for ln in logs), logs
        # too long; busy while 64 KiB wait for a port that takes nothing
        pub.handle_message("lab/scale/tx", b"x" * (64 * 1024 + 1))
        hold = runner._link.writer.hold = asyncio.Event()
        pub.handle_message("lab/scale/tx", b"y" * (64 * 1024 - 10))
        await settle()
        pub.handle_message("lab/scale/tx", b"z" * 20)
        hold.set()
        await settle()
        results = lines(fake, "lab/scale/tx/result")
        assert results[2].startswith(b"error: message too long (65537 bytes"), results
        assert results[3].startswith(b"error: busy ("), results
        assert results[4] == b"ok", results
    finally:
        task.cancel()
    # read-only: refused; the port not open: an error
    logs = []
    runner, fake = tx_runner(logs, read_only=True)
    runner._mqtt.handle_message("lab/scale/tx", b"P\r\n")
    assert lines(fake, "lab/scale/tx/result") == [b"error: mapping is read-only"]
    assert not runner.mapping.accepts_mqtt_tx
    runner, fake = tx_runner(logs, port_open=False)
    task = asyncio.create_task(runner._mqtt_tx_loop())
    try:
        runner._mqtt.handle_message("lab/scale/tx", b"P\r\n")
        await settle()
        assert lines(fake, "lab/scale/tx/result") == [b"error: the serial port is not open"]
        assert runner.status.bytes_dropped == 3
    finally:
        task.cancel()
    # off: nothing subscribed; a stored wildcard base topic: no writes taken
    logs = []
    runner, fake = tx_runner(logs, tx_enabled=False)
    assert fake.subscribed == [] and runner._mqtt_subscriptions() == {}
    runner, fake = tx_runner(logs, base_topic="lab/+/scale")
    assert fake.subscribed == []
    assert any("MQTT writes not taken" in ln for ln in logs), logs


def test_tx():
    asyncio.run(_tx())
    print("tx: each message in one write and answered; retained, too long, busy, read-only, "
          "port closed, off and a wildcard base topic handled  OK")


def test_tx_config():
    def m(protocol="raw", **mqtt):
        return MappingConfig.from_dict({
            "name": "s", "serial": {"port": "COM3"}, "network": {"port": 4001,
                                                                "protocol": protocol},
            "mqtt": {"enabled": True, "host": "h", "base_topic": "lab/scale", **mqtt}})
    assert mqtt_tx_topics(m(tx_enabled=True)) == [("lab/scale/tx", "tx", None),
                                                  ("lab/scale/tx/result", "tx_result", None)]
    assert mqtt_tx_topics(m()) == [] and mqtt_tx_topics(m("modbus", tx_enabled=True)) == []
    wild = m(base_topic="lab/#", tx_enabled=True)
    wild.validate()
    try:
        wild.validate_input()
    except ConfigError as e:
        assert "can't contain '+', '#'" in str(e), e
    else:
        raise AssertionError("a wildcard base topic took writes")
    try:  # publishing only: refused as well (every plain-format MQTT mapping)
        m(base_topic="lab/#").validate_input()
    except ConfigError as e:
        assert "can't contain '+', '#'" in str(e), e
    else:
        raise AssertionError("a wildcard base topic was taken")
    print("tx: edits refuse a wildcard base topic; mqtt_tx_topics names the two topics  OK")


# ---- the poll string -------------------------------------------------------------------

def poll_runner(logs, **options):
    m = MappingConfig.from_dict({
        "name": "an", "serial": {"port": "/dev/ttyX"},
        "network": {"bind_ip": "127.0.0.1", "port": 4001},
        "options": {"poll_string": "Q\\r\\n", "poll_interval_s": 0.1, **options}})
    runner = MappingRunner(m, logger=logs.append)
    runner._link.writer = PortWriter()
    runner._link.ready.set()
    return runner


async def _poll():
    logs = []
    runner = poll_runner(logs)
    data, interval = runner._poll_settings()
    assert (data, interval) == (b"Q\r\n", 0.1)
    task = asyncio.create_task(runner._poll_loop(data, interval))
    try:
        await asyncio.sleep(0.35)
        sent = len(runner._link.writer.writes)
        assert sent >= 2 and set(runner._link.writer.writes) == {b"Q\r\n"}, sent
        # a TCP client is connected: it does the asking
        client = bridge._ClientConn.__new__(bridge._ClientConn)
        runner._clients.add(client)
        await asyncio.sleep(0.3)
        assert len(runner._link.writer.writes) <= sent + 1
        runner._clients.discard(client)
        # the port closed: nothing sent, nothing counted as dropped
        runner._link.ready.clear()
        before = len(runner._link.writer.writes)
        await asyncio.sleep(0.3)
        assert len(runner._link.writer.writes) <= before + 1
        assert runner.status.bytes_dropped == 0
    finally:
        task.cancel()
    assert any("polling the device every 0.1 s with b'Q\\r\\n' while no TCP client" in ln
               for ln in logs), logs
    # with poll_with_clients it goes on while a client is connected
    runner = poll_runner(logs, poll_with_clients=True)
    runner._clients.add(bridge._ClientConn.__new__(bridge._ClientConn))
    task = asyncio.create_task(runner._poll_loop(*runner._poll_settings()))
    try:
        await asyncio.sleep(0.35)
        assert len(runner._link.writer.writes) >= 2
    finally:
        task.cancel()
    # off, and stored values edits refuse: not sent
    logs.clear()
    assert poll_runner(logs, poll_interval_s=0)._poll_settings() is None
    assert poll_runner(logs, poll_string="")._poll_settings() is None
    assert poll_runner(logs, poll_string="\\q")._poll_settings() is None
    assert poll_runner(logs, poll_interval_s=0.01)._poll_settings() is None
    assert sum("poll string not sent" in ln for ln in logs) == 2, logs


def test_poll():
    asyncio.run(_poll())
    print("poll: sent on its interval while the port is open and no TCP client is connected "
          "(or with poll_with_clients); off and bad stored values not sent  OK")


def test_poll_validation():
    def m(**options):
        return MappingConfig.from_dict({"name": "s", "serial": {"port": "COM3"},
                                        "network": {"port": 4001}, "options": options})
    assert poll_bytes("\\x02R\\x03\\t\\\\") == b"\x02R\x03\t\\"
    for ok in ({}, {"poll_string": "P\\r\\n", "poll_interval_s": 1},
               {"poll_string": "P", "poll_interval_s": 0.1}, {"poll_string": "kept, off"}):
        m(**ok).validate_input()
    for bad, text in (({"poll_string": "\\q", "poll_interval_s": 1}, "after a backslash"),
                      ({"poll_string": "P", "poll_interval_s": 0.05}, "poll interval"),
                      # (not a number at all: the typed reader names the setting)
                      ({"poll_string": "P", "poll_interval_s": "soon"},
                       'options.poll_interval_s: "soon" is not a finite number'),
                      ({"poll_interval_s": 5}, "needs a poll string"),
                      ({"poll_string": "x" * 1025, "poll_interval_s": 1}, "at most 1024")):
        stored = m(**bad)
        stored.validate()
        try:
            stored.validate_input()
        except ConfigError as e:
            assert text in str(e), (bad, e)
        else:
            raise AssertionError(f"accepted: {bad}")
    print("poll: edits refuse a bad escape, an interval out of range or without a string  OK")


# ---- the form --------------------------------------------------------------------------

def test_form():
    existing = MappingConfig.from_dict({
        "name": "s", "serial": {"port": "COM3"}, "network": {"port": 4001},
        "mqtt": {"enabled": True, "host": "h", "base_topic": "dev", "line_end": "cr",
                 "tx_enabled": True},
        "options": {"poll_string": "P\\r\\n", "poll_interval_s": 2, "poll_with_clients": True}})
    base = {"name": "s", "kind": "net", "enabled": "on", "serial_port": "COM3",
            "serial_baudrate": "9600", "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "0.0.0.0", "network_port": "4001", "mqtt_enabled": "on",
            "mqtt_host": "h", "mqtt_base_topic": "dev", "id": existing.id}

    def parsed(form):
        m = MappingConfig.from_dict(_build_mapping_dict(form, strict=True))
        _preserve_unmanaged_fields(m, existing, form)
        o = m.options
        return (m.mqtt.line_end, m.mqtt.tx_enabled, o.poll_string, o.poll_interval_s,
                o.poll_with_clients)

    assert parsed(base) == ("cr", True, "P\\r\\n", 2, True), "absent from the form: kept"
    shown = dict(base, mqtt_line_end="any", mqtt_tx_shown="1", opt_poll_interval_s="0")
    assert parsed(shown) == ("any", False, "", 0.0, False)
    assert parsed(dict(shown, mqtt_tx_enabled="on", opt_poll_string="Q", opt_poll_interval_s="5",
                       opt_poll_with_clients="on")) == ("any", True, "Q", 5.0, True)
    print("form: line end, tx and poll fields kept when the form leaves them out  OK")


def main():
    test_line_ends()
    test_cut_logged_once()
    test_line_end_validation()
    test_tx()
    test_tx_config()
    test_poll()
    test_poll_validation()
    test_form()
    print("\nPASS: MQTT line ends and writes, and the poll string, for serial mappings")


if __name__ == "__main__":
    main()
