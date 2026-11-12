"""A Modbus-polling mapping as an Eclipse Sparkplug B edge node.

The mapping's MQTT connection is the edge node spBv1.0/<group>/…/<node>, and each
Modbus unit (slave address) it polls is one of the node's devices, unit<N>, whose
metrics are that unit's poll points:
  * NDEATH is registered as the MQTT Last-Will (QoS 1, not retained) with the session's
    bdSeq; after every (re)connect the node publishes NBIRTH (seq 0) with the same bdSeq
    and the "Node Control/Rebirth" metric, then a DBIRTH per device with every point
    (its datatype, last value or null);
  * every metric carries the time its value was read off the bus (a DBIRTH lists them
    oldest first; a null is stamped with the time of the birth);
  * each poll publishes a DDATA per device with the points whose value changed (report
    by exception); seq counts on across NBIRTH, DBIRTH and DDATA: 0…255, 0, …. Data is
    published only once the session's NBIRTH and DBIRTHs are out: a poll that ends
    between a (re)connect and the births only updates the values the births carry;
  * DCMD: a metric named like a writable point of that device is written to the bus
    (through the gateway's write_point), unless the mapping is read-only (then the
    command is logged and refused); NCMD: "Node Control/Rebirth" = true
    republishes NBIRTH and the DBIRTHs. Commands are never retained (Sparkplug 3.0), so
    a retained NCMD / DCMD the broker replays on subscribe is dropped (MqttPublisher);
  * stopping the mapping publishes NDEATH before disconnecting (a host then takes the
    node's devices offline too).
After a lost connection the next session gets bdSeq + 1 (the Will is renewed in
MqttPublisher's disconnect hook), as the specification asks. With a data folder
(``state_dir``), the bdSeq of every session is stored there per edge node (BDSEQ_FILE), and
the node's next run goes on from it: after a power loss the broker may publish the old
session's Will only after the new session's NBIRTH, and a host tells the two apart by
their bdSeq. Without a stored one the first run starts at 0; if the file can't be read or
written, at a random bdSeq (logged).
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from typing import Callable, Optional

from . import sparkplug
from .mqtt_pub import MqttPublisher, shown

INT64 = sparkplug.DataType.Int64  # the bdSeq metric's datatype (Sparkplug 3.0)
BOOLEAN = sparkplug.DataType.Boolean


# {"<group>/<node>": the bdSeq of that edge node's last session}, in the data folder
BDSEQ_FILE = "sparkplug-bdseq.json"
_bdseq_lock = threading.Lock()  # nodes renew their Will on paho's threads


def load_bdseq(path: str, key: str) -> Optional[int]:
    """The bdSeq stored for edge node ``key`` ("<group>/<node>"), or None if there is none
    (no file yet). Raises OSError / ValueError if the file can't be read or makes no sense."""
    with _bdseq_lock:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    value = data.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                              or not 0 <= value <= 255):
        raise ValueError(f"the bdSeq of {key} is {value!r}, not 0-255")
    return value


def save_bdseq(path: str, key: str, value: int) -> None:
    """Store edge node ``key``'s bdSeq: the file is rewritten whole (a temporary file,
    flushed to the disk, renamed over it), so a power loss leaves the old one or the new
    one. A file that can't be read is started afresh. Raises OSError."""
    with _bdseq_lock:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        data = dict(data) if isinstance(data, dict) else {}
        data[key] = value
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _changed(old, new) -> bool:
    """Whether a point's value differs from the one last reported: a NaN (a meter's
    "no data") that stays NaN is no change, although NaN != NaN."""
    if isinstance(old, float) and isinstance(new, float) and math.isnan(old) and math.isnan(new):
        return False
    return old != new


def _stamp(metric: sparkplug.Metric) -> int:
    return metric.timestamp or 0


def device_id(unit: int) -> str:
    """The Sparkplug device of a Modbus unit (slave address)."""
    return f"unit{unit}"


class SparkplugNode:
    def __init__(self, mapping, log: Callable[[str], None], write: Callable,
                 state_dir: Optional[str] = None) -> None:
        mq = mapping.mqtt
        self.mapping = mapping
        self.group = mq.sparkplug_group_id.strip() or "ser2net"
        self.node = mq.sparkplug_node_id.strip() or sparkplug.sanitize_id(mapping.name)
        self.points = {p.name: p for p in mapping.modbus_poll.points}
        self.devices: dict[str, list[str]] = {}   # device id -> its point names, in order
        for p in mapping.modbus_poll.points:
            self.devices.setdefault(device_id(p.unit), []).append(p.name)
        self.values: dict = {}       # last value sent per metric
        self.stamps: dict = {}       # when each of those was read (ms since the epoch)
        self._log = log
        self._write = write          # gateway._write_soon(point, value)
        self._pub: MqttPublisher | None = None
        self._bdseq_file = os.path.join(state_dir, BDSEQ_FILE) if state_dir else None
        self._bdseq_saved = True     # False once storing it failed (logged once)
        self.bdseq = self._first_bdseq()
        self.seq = 0
        self._sessions = 0           # connects seen (the first one keeps the first bdSeq)
        self._born = False           # this MQTT session's NBIRTH and DBIRTHs are out

    def topic(self, kind: str, device: str | None = None) -> str:
        return sparkplug.topic(self.group, kind, self.node, device)

    def _ndeath(self) -> tuple:
        payload = sparkplug.Payload(timestamp=_now_ms(), seq=None, metrics=[
            sparkplug.Metric(name=sparkplug.BDSEQ, datatype=INT64, value=self.bdseq)])
        return (self.topic("NDEATH"), sparkplug.encode_payload(payload), 1, False)

    @property
    def _bdseq_key(self) -> str:
        return f"{self.group}/{self.node}"

    def _first_bdseq(self) -> int:
        """The bdSeq of this run's first session: the one after the stored one (0 if none
        is stored), stored at once; random if the file can't be read or written."""
        if self._bdseq_file is None:
            return 0  # no data folder: every run starts at 0
        problem = ""
        try:
            last = load_bdseq(self._bdseq_file, self._bdseq_key)
        except (OSError, ValueError) as e:
            last, problem = None, f"can't be read ({e})"
        first = random.randrange(256) if problem else 0 if last is None else (last + 1) % 256
        try:
            save_bdseq(self._bdseq_file, self._bdseq_key, first)
        except OSError as e:
            if not problem:
                first = random.randrange(256)
            problem = problem or f"can't be stored ({e})"
            self._bdseq_saved = False
        if problem:
            self._log(f"Sparkplug: the bdSeq of {self._bdseq_key} {problem}, in "
                      f"{self._bdseq_file}; this run starts at a random bdSeq, {first}")
        return first

    def _save_bdseq(self) -> None:
        if self._bdseq_file is None or not self._bdseq_saved:
            return
        try:
            save_bdseq(self._bdseq_file, self._bdseq_key, self.bdseq)
        except OSError as e:
            self._bdseq_saved = False
            self._log(f"Sparkplug: bdSeq {self.bdseq} can't be stored in {self._bdseq_file} "
                      f"({e}); the next start may use it again")

    def _will(self) -> tuple:
        """Called before the first connect and after every disconnect (paho's thread):
        a new session gets the next bdSeq, stored for the next run, and the Will carries
        it. Nothing is published as data until that session's births are out (paho sends
        a publish as soon as it has a socket, before the broker has accepted the
        connection)."""
        self._born = False
        if self._sessions:
            self.bdseq = (self.bdseq + 1) % 256
            self._save_bdseq()
        self._sessions += 1
        return self._ndeath()

    def publisher(self) -> MqttPublisher:
        dcmd = "/".join((sparkplug.TOPIC_NAMESPACE, self.group, "DCMD", self.node, "+"))
        self._pub = MqttPublisher(self.mapping.mqtt, logger=self._log, will=self._will,
                                  birth=False, on_connect=self.birth, accept_retained=False,
                                  subscriptions={self.topic("NCMD"): self.on_node_command,
                                                 dcmd: self.on_device_command})
        self._log(f"Sparkplug B edge node {self.group}/{self.node}: "
                  f"{len(self.devices)} device(s), {len(self.points)} metrics")
        return self._pub

    def _metric(self, name: str, now: int) -> sparkplug.Metric:
        """A point's metric: its last value, stamped with when it was read; a null (never
        read) stamped ``now``."""
        p = self.points[name]
        dt = sparkplug.datatype_for(p.dtype, p.scale or 1.0)
        value = self.values.get(name)
        if value is None:
            return sparkplug.Metric(name=name, datatype=dt, is_null=True, timestamp=now)
        if dt in (sparkplug.DataType.Float, sparkplug.DataType.Double):
            value = float(value)
        return sparkplug.Metric(name=name, datatype=dt, value=value,
                                timestamp=self.stamps.get(name, now))

    def _publish(self, topic: str, metrics: list) -> None:
        if self._pub is None:
            return
        payload = sparkplug.Payload(timestamp=_now_ms(), seq=self.seq, metrics=metrics)
        self._pub.publish_raw(topic, sparkplug.encode_payload(payload), qos=0)

    def birth(self) -> None:
        """NBIRTH, then a DBIRTH per device (after each connect, and on a rebirth request)."""
        self.seq = 0
        self._publish(self.topic("NBIRTH"), [
            sparkplug.Metric(name=sparkplug.BDSEQ, datatype=INT64, value=self.bdseq),
            sparkplug.Metric(name=sparkplug.REBIRTH, datatype=BOOLEAN, value=False)])
        for dev, names in self.devices.items():
            self.seq = sparkplug.next_seq(self.seq)
            now = _now_ms()
            metrics = sorted((self._metric(n, now) for n in names), key=_stamp)
            self._publish(self.topic("DBIRTH", dev), metrics)
        self._born = True

    def update(self, values: dict, stamps: dict | None = None) -> None:
        """A DDATA per device with the metrics whose value changed since the last report;
        ``stamps``: when each value was read (ms since the epoch; now if not given). Before
        the session's births the values are only kept: the births carry them."""
        now = _now_ms()
        changed = set()
        for n, v in values.items():
            if n not in self.points:
                continue
            if _changed(self.values.get(n), v):
                changed.add(n)
                self.values[n] = v
            # read again, changed or not: a birth reports when the value was last seen
            self.stamps[n] = (stamps or {}).get(n, now)
        if not self._born:
            return
        for dev, names in self.devices.items():
            metrics = [self._metric(n, now) for n in names if n in changed]
            if metrics:
                self.seq = sparkplug.next_seq(self.seq)
                self._publish(self.topic("DDATA", dev), metrics)

    def _decode(self, kind: str, payload: bytes):
        try:
            return sparkplug.decode_payload(payload)
        except ValueError as e:
            self._log(f"Sparkplug {kind} ignored: {e}")
            return None

    def on_node_command(self, topic: str, payload: bytes) -> None:
        """NCMD: "Node Control/Rebirth" is the node's only metric; points are written
        with a DCMD to their device."""
        cmd = self._decode("NCMD", payload)
        if cmd is None:
            return
        for m in cmd.metrics:
            if m.name != sparkplug.REBIRTH:
                self._log(f"Sparkplug NCMD for {shown(m.name)} ignored (points are written with "
                          "a DCMD to their device)")
            elif m.value is True:  # a Boolean true, not just any truthy value
                self._log("Sparkplug rebirth requested")
                self.birth()

    def on_device_command(self, topic: str, payload: bytes) -> None:
        dev = topic.rsplit("/", 1)[-1]
        names = self.devices.get(dev)
        if names is None:
            self._log(f"Sparkplug DCMD for an unknown device {dev!r} ignored")
            return
        cmd = self._decode("DCMD", payload)
        if cmd is None:
            return
        for m in cmd.metrics:
            point = self.points.get(m.name) if m.name in names else None
            if point is None or not point.writable:
                self._log(f"Sparkplug DCMD for {dev}/{shown(m.name)} ignored (not a writable "
                          "point)")
                continue
            if m.is_null:
                continue
            if self.mapping.network.read_only:  # checked per command, not at subscribe time
                self._log(f"Sparkplug DCMD for {dev}/{shown(m.name)} refused: the mapping is "
                          "read-only")
                continue
            value = m.value
            # Sparkplug 3.0 lets a host leave the datatype off a command; the value then
            # arrives as the raw unsigned wire field — read it as the point's signed type
            if (not m.datatype and isinstance(value, int) and not isinstance(value, bool)
                    and point.dtype in ("int16", "int32") and value > 0x7FFFFFFF):
                value -= 1 << 32
            self._write(point, value)

    def close(self) -> None:
        if self._pub is not None:
            self._pub.close(final=self._ndeath())
            self._pub = None
