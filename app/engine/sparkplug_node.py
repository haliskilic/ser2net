"""A Modbus-polling mapping as an Eclipse Sparkplug B edge node.

The mapping's MQTT connection is the edge node spBv1.0/<group>/…/<node>:
  * NDEATH is registered as the MQTT Last-Will (QoS 1, not retained) with the session's
    bdSeq; after every (re)connect the node publishes NBIRTH (seq 0) with the same bdSeq,
    the "Node Control/Rebirth" metric and every poll point (its datatype, last value or
    null);
  * each poll publishes NDATA with the points whose value changed (report by
    exception), seq counting 1…255, 0, …;
  * NCMD: "Node Control/Rebirth" = true republishes NBIRTH; a metric named like a
    writable point is written to the bus (through the gateway's write_point);
  * stopping the mapping publishes NDEATH before disconnecting.
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


class SparkplugNode:
    def __init__(self, mapping, log: Callable[[str], None], write: Callable) -> None:
        mq = mapping.mqtt
        self.mapping = mapping
        self.group = mq.sparkplug_group_id.strip() or "ser2net"
        self.node = mq.sparkplug_node_id.strip() or sparkplug.sanitize_id(mapping.name)
        self.points = {p.name: p for p in mapping.modbus_poll.points}
        self.values: dict = {}       # last value sent per metric
        self.bdseq = 0
        self.seq = 0
        self._sessions = 0           # connects seen (the first one keeps bdSeq 0)
        self._log = log
        self._write = write          # gateway._write_soon(point, value)
        self._pub: MqttPublisher | None = None

    def topic(self, kind: str) -> str:
        return sparkplug.topic(self.group, kind, self.node)

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
        self._pub = MqttPublisher(self.mapping.mqtt, logger=self._log, will=self._will,
                                  birth=False, on_connect=self.birth,
                                  subscriptions={self.topic("NCMD"): self.on_command})
        self._log(f"Sparkplug B edge node {self.group}/{self.node} "
                  f"({len(self.points)} metrics)")
        return self._pub

    def _metric(self, name: str, value) -> sparkplug.Metric:
        p = self.points[name]
        dt = sparkplug.datatype_for(p.dtype, p.scale or 1.0)
        if value is None:
            return sparkplug.Metric(name=name, datatype=dt, is_null=True, timestamp=_now_ms())
        if dt in (sparkplug.DataType.Float, sparkplug.DataType.Double):
            value = float(value)
        return sparkplug.Metric(name=name, datatype=dt, value=value, timestamp=_now_ms())

    def _publish(self, kind: str, metrics: list, seq: int) -> None:
        if self._pub is None:
            return
        payload = sparkplug.Payload(timestamp=_now_ms(), seq=seq, metrics=metrics)
        self._pub.publish_raw(self.topic(kind), sparkplug.encode_payload(payload), qos=0)

    def birth(self) -> None:
        """NBIRTH (after each connect, and on a rebirth request)."""
        self.seq = 0
        metrics = [sparkplug.Metric(name=sparkplug.BDSEQ, datatype=UINT64, value=self.bdseq),
                   sparkplug.Metric(name=sparkplug.REBIRTH, datatype=BOOLEAN, value=False)]
        metrics += [self._metric(n, self.values.get(n)) for n in self.points]
        self._publish("NBIRTH", metrics, self.seq)

    def update(self, values: dict) -> None:
        """NDATA with the metrics whose value changed since the last report."""
        changed = [n for n, v in values.items() if n in self.points and self.values.get(n) != v]
        for n in changed:
            self.values[n] = values[n]
        if changed:
            self.seq = sparkplug.next_seq(self.seq)
            self._publish("NDATA", [self._metric(n, self.values[n]) for n in changed], self.seq)

    def on_command(self, topic: str, payload: bytes) -> None:
        try:
            cmd = sparkplug.decode_payload(payload)
        except ValueError as e:
            self._log(f"Sparkplug NCMD ignored: {e}")
            return
        for m in cmd.metrics:
            if m.name == sparkplug.REBIRTH:
                if m.value:
                    self._log("Sparkplug rebirth requested")
                    self.birth()
                continue
            point = self.points.get(m.name)
            if point is None or not point.writable:
                self._log(f"Sparkplug NCMD for {m.name!r} ignored (not a writable point)")
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
