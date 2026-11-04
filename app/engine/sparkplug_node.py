"""A Modbus-polling mapping as an Eclipse Sparkplug B edge node.

The mapping's MQTT connection is the edge node spBv1.0/<group>/…/<node>, and each
Modbus unit (slave address) it polls is one of the node's devices, unit<N>, whose
metrics are that unit's poll points:
  * NDEATH is registered as the MQTT Last-Will (QoS 1, not retained) with the session's
    bdSeq; after every (re)connect the node publishes NBIRTH (seq 0) with the same bdSeq
    and the "Node Control/Rebirth" metric, then a DBIRTH per device with every point
    (its datatype, last value or null);
  * each poll publishes a DDATA per device with the points whose value changed (report
    by exception); seq counts on across NBIRTH, DBIRTH and DDATA: 0…255, 0, …;
  * DCMD: a metric named like a writable point of that device is written to the bus
    (through the gateway's write_point); NCMD: "Node Control/Rebirth" = true
    republishes NBIRTH and the DBIRTHs;
  * stopping the mapping publishes NDEATH before disconnecting (a host then takes the
    node's devices offline too).
After a lost connection the next session gets bdSeq + 1 (the Will is renewed in
MqttPublisher's disconnect hook), as the specification asks.
"""
from __future__ import annotations

import time
from typing import Callable

from . import sparkplug
from .mqtt_pub import MqttPublisher

UINT64 = sparkplug.DataType.UInt64
BOOLEAN = sparkplug.DataType.Boolean


def _now_ms() -> int:
    return int(time.time() * 1000)


def device_id(unit: int) -> str:
    """The Sparkplug device of a Modbus unit (slave address)."""
    return f"unit{unit}"


class SparkplugNode:
    def __init__(self, mapping, log: Callable[[str], None], write: Callable) -> None:
        mq = mapping.mqtt
        self.mapping = mapping
        self.group = mq.sparkplug_group_id.strip() or "ser2net"
        self.node = mq.sparkplug_node_id.strip() or sparkplug.sanitize_id(mapping.name)
        self.points = {p.name: p for p in mapping.modbus_poll.points}
        self.devices: dict[str, list[str]] = {}   # device id -> its point names, in order
        for p in mapping.modbus_poll.points:
            self.devices.setdefault(device_id(p.unit), []).append(p.name)
        self.values: dict = {}       # last value sent per metric
        self.bdseq = 0
        self.seq = 0
        self._sessions = 0           # connects seen (the first one keeps bdSeq 0)
        self._log = log
        self._write = write          # gateway._write_soon(point, value)
        self._pub: MqttPublisher | None = None

    def topic(self, kind: str, device: str | None = None) -> str:
        return sparkplug.topic(self.group, kind, self.node, device)

    def _ndeath(self) -> tuple:
        payload = sparkplug.Payload(timestamp=_now_ms(), seq=None, metrics=[
            sparkplug.Metric(name=sparkplug.BDSEQ, datatype=UINT64, value=self.bdseq)])
        return (self.topic("NDEATH"), sparkplug.encode_payload(payload), 1, False)

    def _will(self) -> tuple:
        """Called before the first connect and after every disconnect (paho's thread):
        a new session gets the next bdSeq, and the Will carries it."""
        if self._sessions:
            self.bdseq = (self.bdseq + 1) % 256
        self._sessions += 1
        return self._ndeath()

    def publisher(self) -> MqttPublisher:
        dcmd = "/".join((sparkplug.TOPIC_NAMESPACE, self.group, "DCMD", self.node, "+"))
        self._pub = MqttPublisher(self.mapping.mqtt, logger=self._log, will=self._will,
                                  birth=False, on_connect=self.birth,
                                  subscriptions={self.topic("NCMD"): self.on_node_command,
                                                 dcmd: self.on_device_command})
        self._log(f"Sparkplug B edge node {self.group}/{self.node}: "
                  f"{len(self.devices)} device(s), {len(self.points)} metrics")
        return self._pub

    def _metric(self, name: str, value) -> sparkplug.Metric:
        p = self.points[name]
        dt = sparkplug.datatype_for(p.dtype, p.scale or 1.0)
        if value is None:
            return sparkplug.Metric(name=name, datatype=dt, is_null=True, timestamp=_now_ms())
        if dt in (sparkplug.DataType.Float, sparkplug.DataType.Double):
            value = float(value)
        return sparkplug.Metric(name=name, datatype=dt, value=value, timestamp=_now_ms())

    def _publish(self, topic: str, metrics: list) -> None:
        if self._pub is None:
            return
        payload = sparkplug.Payload(timestamp=_now_ms(), seq=self.seq, metrics=metrics)
        self._pub.publish_raw(topic, sparkplug.encode_payload(payload), qos=0)

    def birth(self) -> None:
        """NBIRTH, then a DBIRTH per device (after each connect, and on a rebirth request)."""
        self.seq = 0
        self._publish(self.topic("NBIRTH"), [
            sparkplug.Metric(name=sparkplug.BDSEQ, datatype=UINT64, value=self.bdseq),
            sparkplug.Metric(name=sparkplug.REBIRTH, datatype=BOOLEAN, value=False)])
        for dev, names in self.devices.items():
            self.seq = sparkplug.next_seq(self.seq)
            self._publish(self.topic("DBIRTH", dev),
                          [self._metric(n, self.values.get(n)) for n in names])

    def update(self, values: dict) -> None:
        """A DDATA per device with the metrics whose value changed since the last report."""
        changed = {n for n, v in values.items() if n in self.points and self.values.get(n) != v}
        for n in changed:
            self.values[n] = values[n]
        for dev, names in self.devices.items():
            metrics = [self._metric(n, self.values[n]) for n in names if n in changed]
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
                self._log(f"Sparkplug NCMD for {m.name!r} ignored (points are written with "
                          "a DCMD to their device)")
            elif m.value:
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
                self._log(f"Sparkplug DCMD for {dev}/{m.name} ignored (not a writable point)")
                continue
            if m.is_null:
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
