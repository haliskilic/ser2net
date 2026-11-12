"""Modbus RTU<->TCP gateway runner.

A drop-in alternative to MappingRunner, selected when a mapping's network
protocol is ``modbus``. It listens as a Modbus/TCP server: each connected master
sends Modbus/TCP requests, which the gateway converts to Modbus/RTU, forwards on
the shared serial bus (one transaction at a time, since RTU is a single-master
bus), reads the slave's RTU reply, and returns it to that master as Modbus/TCP —
echoing the transaction id so concurrent masters don't get crossed wires. A request
whose slave doesn't answer in time is answered with exception 0x0B (gateway target
failed to respond); while the serial port is down, every request is answered with 0x0A
(gateway path unavailable) at once, and so is one that was on the bus when the port went
away. A broadcast (unit 0) write goes on the bus and, after
the turnaround delay, gets the echo a slave would have sent; a read of unit 0 can't get a
reply and is answered 0x0B at once, without touching the bus.

Shares the lifecycle shape (and the H3/H4 fixes) of MappingRunner: the same
supervised serial port (serial_link.SerialLink: reopened, and its USB identity
re-resolved, whenever it is lost) plus an asyncio TCP server, both torn down
cleanly on stop(). Masters are admitted by the same policy as bridge clients
(access.AccessPolicy: allow-list, max connections, priority IPs, kick_old_user);
a read-only gateway answers every request that is not known to be a read with
exception 01 and never puts it on the bus, and refuses MQTT/Sparkplug writes as well.
A banner, open/close strings and closeon don't apply: bytes outside Modbus frames
would break the framing.

Edge mode (MQTT enabled, poll points) reads the points off the bus on an interval, unit by
unit, and keeps track of each unit's health (_poll_unit): a unit that gives no reply to any
of its points in UNIT_OFFLINE_AFTER cycles in a row, or whose serial port stays gone that
long, is offline (logged once; a Sparkplug B node sends its DDEATH) and is polled with one
point per cycle until it answers; then all its points are read again (Sparkplug B: a DBIRTH
with those values). Requests from Modbus/TCP masters are not affected. Plain MQTT publishes
each value as text (1 / 0 for a bit), or with mqtt.value_format "json" as {"value", "ts"
(the read time, ms since the epoch), "quality": "good"}; then a point whose read fails, or
whose unit goes offline, is published once as {"value": null, "quality": "bad"} until it
reads again (encode_value).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from ..config import (MappingConfig, ModbusPoint, modbus_response_timeout, point_order,
                      point_scale)
from . import modbus, netguard, sockopts
from .access import AccessPolicy, server_ssl
from .bridge import (
    FROM_DEVICE, TO_DEVICE, RunnerStatus, Tracer, describe_disconnect, fmt_duration, open_tracer,
)
from .mqtt_pub import shown
from .serial_link import SerialLink

if TYPE_CHECKING:
    from .mqtt_pub import MqttPublisher
    from .sparkplug_node import SparkplugNode

READ_CHUNK = 512
# An RTU reply whose length its header does not give (FC8, FC43, user-defined codes), or
# that fails its CRC at that length, ends once the line has been quiet this long (or for
# 3.5 characters, if that is longer): well above the latency of USB serial adapters (16 ms
# by default on FTDI chips) and the 15.6 ms clock of the event loop on Windows.
SILENCE_S = 0.05
# After a request that got no reply in time (or bytes after its reply), the line has to
# be quiet this long (at most the response timeout) before the next request goes out: a
# reply that comes late is dropped then, not taken for the next request's.
GUARD_S = 0.25
# After a broadcast (unit 0), which no slave answers: the time the slaves get to carry it
# out before the next request goes out (the Modbus serial line specification's turnaround
# delay, 100-200 ms; at most the response timeout).
TURNAROUND_S = 0.2
# Log lines about one slave's replies (cut short, not matching): at most one per this many
# seconds per kind and unit; the ones in between are counted into the next.
NOTE_EVERY_S = 60.0
# Edge mode: a unit none of whose points has answered in this many poll cycles in a row (or
# while the serial port is gone) is offline, and polled with one point per cycle from then on.
UNIT_OFFLINE_AFTER = 3
# Writes from MQTT / Sparkplug B: one task per mapping writes them in the order they came,
# one at a time (a write and its read-back take their turns on the bus with the poll and
# the masters, whatever the number of commands). At most this many wait for the bus; a
# command beyond them is refused (busy).
WRITE_QUEUE_MAX = 32
# A write that waited this long for its turn is not written any more (expired): the bus is
# too slow for the commands coming in, and whoever sent it has likely given up on it.
WRITE_EXPIRE_S = 10.0
# A plain /set larger than this is refused unread (a backstop: a value is a few bytes).
SET_MAX_BYTES = 64 * 1024
# stop(): the write on the bus gets this long at most (and no longer than its reply can
# take) to finish before it is cancelled; the ones still waiting are answered at once.
WRITE_STOP_WAIT_S = 2.0


def _next_timer(idle_s: float, deadline: Optional[float]) -> Optional[float]:
    """Seconds until a master's idle / max-connection-time timer fires (None = none)."""
    waits = [idle_s] if idle_s > 0 else []
    if deadline is not None:
        waits.append(max(0.0, deadline - time.monotonic()))
    return min(waits) if waits else None


@dataclass(eq=False)
class _Master:
    """A connected Modbus/TCP master (identity-hashed, so it can live in a set)."""
    writer: asyncio.StreamWriter
    peer: str
    priority: bool = False          # admitted from priority_client_ips
    connected_at: float = field(default_factory=time.time)
    close_reason: str = ""          # why the gateway dropped it (first cause wins)
    refused: int = 0                # requests refused because the mapping is read-only

    def kick(self, reason: str = "kicked") -> None:
        self.close_reason = self.close_reason or reason
        with contextlib.suppress(Exception):
            self.writer.transport.abort()


@dataclass(eq=False)
class _Write:
    """A write from MQTT / Sparkplug B, waiting for the bus (or on it)."""
    point: ModbusPoint
    value: Any
    done: Optional[Callable[[Optional[str], Any], None]] = None  # (error, value read back)
    refused: Optional[str] = None   # refused when it came: answered in its turn, not written
    queued: float = field(default_factory=time.monotonic)
    stage: str = "queued"           # sent: its request is on the line; written: acknowledged


class NoReply(ValueError):
    """A poll point got no reply: its unit did not answer in time (or not usably), or the
    serial port is down (exception 0x0B / 0x0A, which the gateway makes up then)."""


@dataclass(eq=False)
class _Unit:
    """A polled Modbus unit (slave address) and how it is doing (see _poll_unit)."""
    unit: int
    points: list[ModbusPoint] = field(default_factory=list)
    state: str = "unknown"      # unknown (not polled yet) | online | offline
    cause: str = ""             # offline: "no reply" (the unit) or "port down" (the port)
    misses: int = 0             # poll cycles in a row in which none of its points answered,
    miss_cause: str = ""        # ... all for this cause (another one starts the count over)
    since: float = 0.0          # time.time() when it went online / offline
    errors: int = 0             # point reads that failed, in this run
    probe: int = 0              # offline: which point the next probe reads (each in turn)

    def as_dict(self) -> dict:
        """For RunnerStatus.units (the dashboard, the REST API, /metrics, notifications)."""
        return {"unit": self.unit, "state": self.state, "cause": self.cause,
                "since": int(self.since) if self.since else None, "errors": self.errors}


@dataclass
class _Reads:
    """What one round of point reads gave."""
    values: dict = field(default_factory=dict)
    stamps: dict = field(default_factory=dict)  # when each value was read (ms since the epoch)
    failed: list = field(default_factory=list)  # (point, error)
    answered: bool = False      # the unit answered at least once (a value or an exception)

    def drop(self, name: str) -> None:
        """A point written after it was read here: what was read before the write is out
        of date (the write's read-back has reported the point already)."""
        self.values.pop(name, None)
        self.stamps.pop(name, None)
        self.failed = [(p, e) for p, e in self.failed if p.name != name]


class ModbusGatewayRunner:
    def __init__(self, mapping: MappingConfig, logger=None, trace_dir: Optional[str] = None,
                 state_dir: Optional[str] = None):
        self.mapping = mapping
        self.trace_dir = trace_dir
        self.state_dir = state_dir  # the data folder: what a Sparkplug node keeps across runs
        self._tracer: Optional[Tracer] = None
        self.status = RunnerStatus(state="stopped")
        self.log = logger
        self._serial_task: Optional[asyncio.Task] = None
        self._server: Optional[netguard.Listener] = None
        self._stop = asyncio.Event()
        # the RTU bus; is_open / ready say whether requests can reach it right now
        self._link = SerialLink(mapping.serial, self.status, self._stop, self._log)
        self._bus_lock = asyncio.Lock()      # RTU is single-master: one txn at a time
        self._clients: set[_Master] = set()
        self._monitors: set = set()           # browser console observers (read the bus)
        self._traffic: set = set()            # live hex view observers
        self._mqtt: Optional[MqttPublisher] = None  # optional MQTT publisher for register polling
        self._sparkplug: Optional[SparkplugNode] = None  # payload format sparkplug_b
        self._writes: deque[_Write] = deque()  # MQTT writes waiting for the bus, in order
        self._writer: Optional[asyncio.Task] = None  # writes them, one at a time
        self._closing = False  # stop() has begun: no more MQTT writes are taken
        self._poll_task: Optional[asyncio.Task] = None
        # plain MQTT: the command topic of each writable point, matched exactly (a point
        # name is never read back out of a topic)
        base = mapping.mqtt.base_topic.rstrip("/")
        self._set_points: dict[str, ModbusPoint] = {
            f"{base}/{p.name}/set": p for p in mapping.modbus_poll.points if p.writable}
        self._access = AccessPolicy(mapping.network)
        # one RTU character on the line (start bit, 8 data bits, parity or a 2nd stop bit,
        # stop bit), and the silence that ends a reply its header does not size
        baud = max(1, mapping.serial.baudrate)
        self._char_s = 11 / baud
        self._gap = max(SILENCE_S, 3.5 * self._char_s)
        self._resp_timeout = modbus_response_timeout(mapping.options)
        self._notes: dict[tuple[str, int], tuple[float, int]] = {}  # see _note
        # the line may still carry a reply to an earlier request (see _settle): since when
        # (loop time; None: it does not), and the last request that got none in time, as
        # (unit, PDU)
        self._dirty: Optional[float] = None
        self._unanswered: Optional[tuple[int, bytes]] = None
        # edge mode: the polled units, in the order their first point is listed (a point on
        # unit 0, the broadcast address, is never read: no slave answers a broadcast), and the
        # points whose last read failed (True: that was logged)
        self._units: dict[int, _Unit] = {}
        for p in mapping.modbus_poll.points:
            if p.unit != modbus.BROADCAST and _polled(p):
                self._units.setdefault(p.unit, _Unit(p.unit)).points.append(p)
        self._failing: dict[str, bool] = {}
        self._bad: set[str] = set()  # plain JSON values: points last published as "bad"
        self._cycle: Optional[_Reads] = None  # the reads of the unit being polled (see _Reads.drop)

    def _log(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    # ---- console monitors (watch the raw bus) ----
    def add_monitor(self, mon) -> None:
        self._monitors.add(mon)

    def discard_monitor(self, mon) -> None:
        self._monitors.discard(mon)

    def _feed_monitors(self, data: bytes) -> None:
        for mon in list(self._monitors):
            mon.feed(data)

    def add_traffic_monitor(self, mon) -> None:
        self._traffic.add(mon)

    def discard_traffic_monitor(self, mon) -> None:
        self._traffic.discard(mon)

    def _feed_traffic(self, direction: int, data: bytes) -> None:
        for mon in list(self._traffic):
            mon.feed_traffic(direction, data)

    async def serial_write(self, data: bytes) -> bool:
        # the modbus console is observe-only (injecting raw bytes would corrupt
        # the request/response framing on the shared bus)
        return False

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._stop.clear()
        self._closing = False
        self._tracer = open_tracer(self.mapping, self.trace_dir, self._log)
        net = self.mapping.network
        # the port is read per transaction; the link only watches for it going away
        self._serial_task = asyncio.create_task(
            self._link.run(self._link.hold_open), name=f"modbus-serial:{self.mapping.name}")
        try:
            self._server = await netguard.start_server(
                self._on_client, net.bind_ip, net.port, ssl=server_ssl(net),
                allowed=self._access.allowed, log=self._log)
            self._log(f"modbus gateway listening on {net.bind_ip}:{net.port}"
                      f" ({'TLS, ' if net.tls else ''}{sockopts.describe(net)})")
        except BaseException:
            await self.stop()
            raise
        self._log_policy()
        # optional edge mode: poll Modbus registers off the bus and publish to MQTT
        # (plain topics, or as a Sparkplug B edge node); writable points take values back
        if self.mapping.mqtt.enabled and self.mapping.modbus_poll.points:
            mq = self.mapping.mqtt
            if mq.payload_format == "sparkplug_b":
                from .sparkplug_node import SparkplugNode
                self._sparkplug = SparkplugNode(self.mapping, self._log, self._write_soon,
                                                state_dir=self.state_dir)
                self._mqtt = self._sparkplug.publisher()
            else:
                from .mqtt_pub import MqttPublisher
                self._mqtt = MqttPublisher(
                    mq, logger=self._log,
                    subscriptions=dict.fromkeys(self._set_points, self._on_set_message),
                    accept_retained=bool(mq.apply_retained_set),
                    on_connect=self._publish_unit_states if mq.unit_status else None)
            self.status.mqtt = self._mqtt.state
            self._mqtt.connect()
            self._report_units()
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name=f"modbus-poll:{self.mapping.name}")
        if self.status.state == "stopped":  # the serial side has not reported yet
            self.status.state = "running"

    async def stop(self) -> None:
        self._closing = True  # MQTT / Sparkplug writes are refused from now on
        self._stop.set()
        if self._server is not None:
            self._server.close()
        for c in list(self._clients):
            c.kick("gateway stopped")
        for mon in list(self._monitors) + list(self._traffic):
            mon.close()
        self._monitors.clear()
        self._traffic.clear()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        await self._end_writes()  # (their answers go out before the MQTT connection closes)
        if self._sparkplug is not None:
            sparkplug, self._sparkplug = self._sparkplug, None
            self._mqtt = None
            await sparkplug.aclose()  # NDEATH, then disconnect
        elif self._mqtt is not None:
            mqtt, self._mqtt = self._mqtt, None
            await mqtt.aclose()
        self.status.mqtt = {}
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            self._server = None
        if self._serial_task:
            self._serial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._serial_task
            self._serial_task = None
        await self._link.close()
        if self._tracer:
            await self._tracer.aclose()
            self._tracer = None
        self.status.state = "stopped"
        self.status.client_count = 0
        self.status.clients = []
        self.status.units = []
        self._log("modbus gateway stopped")

    # ---------------- one RTU transaction on the shared bus ----------------
    async def _transaction(self, unit: int, pdu: bytes, job: Optional[_Write] = None) -> bytes:
        """One request on the RTU bus and its reply (or the exception the gateway answers in
        its place); ``job``: the MQTT write it is for (marked sent once it is on the line)."""
        function = pdu[0] if pdu else 0
        if self._link.down:  # the port is gone (reconnecting): answer now, don't queue
            return modbus.exception_pdu(function, modbus.GATEWAY_PATH_UNAVAILABLE)
        if unit == modbus.BROADCAST and modbus.is_read_request(pdu):
            # no slave answers a broadcast, so a read of unit 0 can't get a reply: nothing
            # goes on the bus, and the master gets at once what it got after the timeout
            return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
        if not self._link.is_open:
            # the first open at start-up is still under way: wait a little for it, outside
            # the bus lock, so queued requests wait together and not one timeout each
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._link.ready.wait(), timeout=self._resp_timeout)
        async with self._bus_lock:
            reader, writer = self._link.reader, self._link.writer
            if reader is None or writer is None:  # not open (yet), or lost meanwhile
                return modbus.exception_pdu(function, modbus.GATEWAY_PATH_UNAVAILABLE)
            rtu_req = modbus.rtu_wrap(unit, pdu)
            loop = asyncio.get_running_loop()
            try:
                await self._settle(reader)  # drop what the line holds, then send
                self._feed_monitors(b"\xbb tx " + rtu_req)
                if self._tracer:
                    self._tracer.write("net>ser", rtu_req)
                self._feed_traffic(TO_DEVICE, rtu_req)
                writer.write(rtu_req)
                if job is not None:
                    job.stage = "sent"
                await writer.drain()
                self.status.bytes_out += len(rtu_req)
                # drain() returns once the OS has the request, which then still takes
                # its time on the line: the response timeout starts after that
                sent = loop.time() + len(rtu_req) * self._char_s
                if unit == modbus.BROADCAST:
                    await self._turnaround(reader, sent)
                    reply = modbus.broadcast_reply(pdu)
                    if reply is not None:  # a write: what each slave would have answered
                        return reply
                    return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
                rpdu = await self._read_reply(reader, unit, pdu, sent)
            except asyncio.TimeoutError:  # (an OSError too, since Python 3.11)
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            except (ConnectionError, OSError):  # the port went away meanwhile
                return modbus.exception_pdu(function, modbus.GATEWAY_PATH_UNAVAILABLE)
            if rpdu is None:  # no reply from that slave in time
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            return rpdu

    async def _read_point(self, p):
        """One poll point's current value off the bus (bool for coils / discrete inputs and
        a register's bit, text for a string, a number for the other registers). Raises
        NoReply if the unit did not answer (or the port is down), ValueError for an answer
        that is not a value (an exception, ...)."""
        if p.fn in (modbus.READ_COILS, modbus.READ_DISCRETE_INPUTS):
            rpdu = await self._transaction(p.unit, modbus.read_bits_pdu(p.fn, p.address, 1))
            self._check_answered(rpdu, p)
            return modbus.decode_bits(modbus.response_data(rpdu, p.fn), 1)[0]
        rpdu = await self._transaction(p.unit, modbus.read_pdu(p.fn, p.address, _registers(p)))
        self._check_answered(rpdu, p)
        return decode_point(p, modbus.response_data(rpdu, p.fn))

    def _check_answered(self, rpdu: bytes, p) -> None:
        """Raise NoReply for the exceptions the gateway answers itself when no reply came:
        0x0B (none in time, or none that could be used) and 0x0A (the port is down)."""
        if len(rpdu) == 2 and rpdu[0] & 0x80:
            if rpdu[1] == modbus.GATEWAY_TARGET_FAILED:
                raise NoReply(f"no reply from unit {p.unit} within {self._resp_timeout:g} s")
            if rpdu[1] == modbus.GATEWAY_PATH_UNAVAILABLE:
                raise NoReply("the serial port is down")

    def _publish_values(self, values: dict, stamps: Optional[dict] = None,
                        force: bool = False) -> None:
        """Publish values read off the bus; ``stamps``: when each was read (ms since the
        epoch), which Sparkplug B reports as the metric's timestamp; ``force``: Sparkplug B
        sends them in a DDATA even if they did not change (after a DCMD)."""
        if self._sparkplug is not None:
            self._sparkplug.update(values, stamps, force=values.keys() if force else ())
        elif self._mqtt is not None:
            fmt = self.mapping.mqtt.value_format
            for name, v in values.items():
                self._bad.discard(name)
                self._mqtt.publish_value(name, encode_value(
                    v, fmt, (stamps or {}).get(name) or _now_ms()))

    def _publish_bad(self, names) -> None:
        """Plain MQTT, JSON values: points whose read failed, or whose unit stopped
        answering, are published once as quality "bad" (value null), until they read
        again (text values: nothing is published for them)."""
        if (self._sparkplug is not None or self._mqtt is None
                or self.mapping.mqtt.value_format != "json"):
            return
        now = _now_ms()
        for name in names:
            if name not in self._bad:
                self._bad.add(name)
                self._mqtt.publish_value(name, encode_value(None, "json", now, good=False))

    async def _poll_loop(self) -> None:
        """Edge mode: read the configured points off the RTU bus on an interval, unit by
        unit, and publish the decoded values (to <base_topic>/<point name>, or as Sparkplug
        B); see _poll_unit."""
        poll = self.mapping.modbus_poll
        while not self._stop.is_set():
            lost = []  # units that went offline in this cycle because the port is gone
            for u in list(self._units.values()):
                if self._stop.is_set():
                    break
                if await self._poll_unit(u) == "port down":
                    lost.append(u.unit)
                self._report_units()
            if lost:
                self._log(f"{_units_text(lost)}: offline, the serial port has been down for "
                          f"{UNIT_OFFLINE_AFTER} polls")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll.interval_s)
                break
            except asyncio.TimeoutError:
                pass

    async def _poll_unit(self, u: _Unit) -> Optional[str]:
        """Read one unit's points and publish the values. A unit that gave no reply to any of
        them in UNIT_OFFLINE_AFTER cycles in a row, or whose port stayed down that long, goes
        offline: a Sparkplug B node sends its DDEATH and drops its values, and from then on
        each cycle reads one of its points (each in turn), so a dead unit holds the bus for
        one response timeout per cycle, not one per point. When it answers, its other points
        are read too and it is online again (Sparkplug B: a DBIRTH with those values). A slave
        exception is an answer: the unit is there. Only changes of state are logged; a point
        that keeps failing on a unit that answers is logged when it starts failing (at most
        once a minute) and when it reads again. Returns the cause if the unit went offline
        in this cycle."""
        if self._link.down:  # nothing to read (the link logs the outage)
            return self._unit_missed(u, "port down")
        # what is read here is published once all of the unit's points are read; a write in
        # between drops the value of its point that was read before it (write_point)
        reads = self._cycle = _Reads()
        try:
            if u.state == "offline":
                probe = u.points[u.probe % len(u.points)]
                u.probe += 1
                await self._read_points([probe], reads)
                if reads.answered:
                    await self._read_points([p for p in u.points if p is not probe], reads)
            else:
                await self._read_points(u.points, reads)
        finally:
            self._cycle = None
        u.errors += len(reads.failed)
        self._forget([p.name for p, _e in reads.failed])
        if not reads.answered:
            return self._unit_missed(u, "port down" if self._link.down else "no reply")
        back = u.state == "offline"
        u.misses, u.miss_cause = 0, ""
        if u.state != "online":
            if back:
                self._log(f"unit {u.unit} answers again (offline for "
                          f"{fmt_duration(time.time() - u.since)})")
            u.state, u.cause, u.since = "online", "", time.time()
            self._unit_state_changed(u)
        for p, e in reads.failed:
            if p.name not in self._failing:
                self._failing[p.name] = self._note(f"poll {p.name}", p.unit,
                                                   f"poll '{p.name}' (unit {p.unit}): {e}")
        for name in reads.values:
            if self._failing.pop(name, False):
                self._log(f"poll '{name}' reads again")
        self._publish_bad([p.name for p, _e in reads.failed])
        if back and self._sparkplug is not None:
            self._sparkplug.device_online(u.unit, reads.values, reads.stamps)
        elif reads.values:
            self._publish_values(reads.values, reads.stamps)
        return None

    async def _read_points(self, points, reads: _Reads) -> None:
        for p in points:
            if self._stop.is_set():
                break
            try:
                reads.values[p.name] = await self._read_point(p)
                reads.stamps[p.name] = _now_ms()
                reads.answered = True
            except NoReply as e:
                reads.failed.append((p, e))
            except Exception as e:  # an answer, but not a value; one bad point must not stop the loop
                reads.failed.append((p, e))
                reads.answered = True

    def _report_units(self) -> None:
        self.status.units = [u.as_dict() for u in self._units.values()]

    def _unit_state_changed(self, u: _Unit) -> None:
        if self._sparkplug is None and self.mapping.mqtt.unit_status:
            self._publish_unit_state(u)

    def _publish_unit_state(self, u: _Unit) -> None:
        """Plain MQTT with unit status on: <base_topic>/unit<N>/status, retained."""
        if self._mqtt is not None and u.state != "unknown":
            base = self.mapping.mqtt.base_topic.rstrip("/")
            self._mqtt.publish_raw(f"{base}/unit{u.unit}/status", u.state.encode(), retain=True)

    def _publish_unit_states(self) -> None:
        """After each (re)connect: the units' states again (a change while the broker was
        away was not delivered)."""
        for u in self._units.values():
            self._publish_unit_state(u)

    def _unit_missed(self, u: _Unit, cause: str) -> Optional[str]:
        """A poll cycle in which ``u`` gave no reply, because of ``cause``; it goes offline
        after UNIT_OFFLINE_AFTER such cycles in a row. A unit that already stopped answering
        stays offline for that reason while the port is down."""
        if cause != u.miss_cause:
            u.misses, u.miss_cause = 0, cause
        u.misses += 1
        if u.misses < UNIT_OFFLINE_AFTER or (u.state == "offline"
                                              and (u.cause == cause or cause == "port down")):
            return None
        went = u.state != "offline"
        u.state, u.cause = "offline", cause
        if went:
            u.since = time.time()
            if self._sparkplug is not None:
                self._sparkplug.device_offline(u.unit)
            self._publish_bad([p.name for p in u.points])
            self._unit_state_changed(u)
        if cause == "no reply":
            n = len(u.points)
            self._log(f"unit {u.unit}: offline, no reply to "
                      f"{'its point' if n == 1 else f'any of its {n} points'} in "
                      f"{UNIT_OFFLINE_AFTER} polls in a row; polling one point per cycle until "
                      "it answers")
        return cause if went else None

    def _forget(self, names: list) -> None:
        """Points whose read failed: their last value is not reported as current again."""
        if names and self._sparkplug is not None:
            self._sparkplug.forget(names)

    # ---------------- writes from MQTT (plain <topic>/<point>/set, Sparkplug DCMD) ----
    async def write_point(self, point, value, job: Optional[_Write] = None) -> Any:
        """Write one value to one writable point, and nothing beyond it: a coil takes a
        bool (FC5), a holding register a number, encoded as the point's own type (FC6 for
        a 16-bit type, FC16 for the two registers of a 32-bit one). Check the slave's
        reply, then read the point back and publish it; returns that value (None if it was
        not read back). A point on unit 0 is a broadcast, carried out by every slave and
        answered by none, so it is not read back, nor is a write-only point ("poll":
        false). A read-only mapping writes nothing, nor does a value outside the point's
        min / max / allowed (checked here, at write time, for every command path)."""
        if self.mapping.network.read_only:
            raise ValueError("mapping is read-only")
        if not point.writable:
            raise ValueError(f"point {point.name!r} is not writable")
        if point.fn == modbus.READ_COILS:
            # only a bool: never a list (several coils), a number (a Sparkplug Int 2) or
            # text (a Sparkplug String "false")
            if not isinstance(value, bool):
                raise ValueError(f"{point.name!r} is a coil: it takes true or false, "
                                 f"not {shown(value)}")
            check_limits(point, value)
            pdu = modbus.write_single_coil_pdu(point.address, value)
            raw = ""
        else:
            if point.dtype not in modbus.DTYPES:  # (a stored point the edits refuse)
                what = "a bit of a register" if point.dtype == "bool" else shown(point.dtype)
                raise ValueError(f"{point.name!r} is {what}: MQTT can't write it")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{point.name!r} takes a number, not {shown(value)}")
            check_limits(point, value)
            regs = modbus.encode_value(value, point.dtype, point_scale(point) or 1.0,
                                       point_order(point))
            pdu = (modbus.write_single_register_pdu(point.address, regs[0]) if len(regs) == 1
                   else modbus.write_multiple_registers_pdu(point.address, regs))
            raw = _raw_text(value, regs, point)
        rpdu = await self._transaction(point.unit, pdu, job)
        self._check_answered(rpdu, point)  # (NoReply: the unit did not answer)
        modbus.check_write_response(rpdu, pdu)
        if job is not None:
            job.stage = "written"
        if self._cycle is not None:  # a poll that read the point before the write is under way
            self._cycle.drop(point.name)
        if point.unit == modbus.BROADCAST:
            self._log(f"wrote '{point.name}' = {value!r} (broadcast to unit 0, FC{pdu[0]}{raw}; "
                      "not read back)")
            return None
        if not _polled(point):
            self._log(f"wrote '{point.name}' = {value!r} (unit {point.unit}, FC{pdu[0]}{raw}; "
                      "write-only, not read back)")
            return None
        self._log(f"wrote '{point.name}' = {value!r} (unit {point.unit}, FC{pdu[0]}{raw})")
        if self._closing:  # the mapping is stopping: no read-back
            return None
        return await self._read_back(point)

    async def _read_back(self, point) -> Any:
        """After a write: the point's value, read and published (Sparkplug B: in a DDATA
        even if it did not change, so the host learns how the DCMD went); None if it can't
        be read."""
        try:
            value = await self._read_point(point)
        except Exception:
            return None
        self._publish_values({point.name: value}, {point.name: _now_ms()}, force=True)
        return value

    def _write_soon(self, point, value, done=None, refused: Optional[str] = None) -> None:
        """Queue a write from MQTT (called on the loop): the writes are made one at a time,
        in the order they came (see WRITE_QUEUE_MAX). ``done`` gets (None or the error,
        the value read back or None) once it is over; a write ``refused`` when it came (the
        error) is answered in its turn as well, so the answers keep the commands' order."""
        w = _Write(point, value, done, refused)
        if self._closing:
            self._note("write stopping", point.unit, f"write to '{point.name}' refused: the "
                       "mapping is stopping")
            self._finish(w, "mapping stopped")
            return
        if len(self._writes) >= WRITE_QUEUE_MAX:
            self._note("write busy", point.unit, f"write to '{point.name}' refused: "
                       f"{WRITE_QUEUE_MAX} writes are waiting for the bus already (busy)")
            self._finish(w, "busy")
            return
        self._writes.append(w)
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._run_writes(),
                                               name=f"modbus-write:{self.mapping.name}")

    async def _run_writes(self) -> None:
        """The mapping's writer: the queued writes, one at a time, in the order they came
        (until the mapping stops: _end_writes answers the rest)."""
        while self._writes and not self._closing:
            w = self._writes.popleft()
            try:
                err, value = await self._run_write(w)
            except asyncio.CancelledError:  # stop() did not wait any longer for it
                if w.stage == "written":    # (only its read-back was cut short)
                    self._finish(w, None)
                    raise
                outcome = "sent, outcome unknown" if w.stage == "sent" else "not sent"
                self._log(f"write '{w.point.name}' = {shown(w.value)} cancelled (mapping "
                          f"stopped): {outcome}")
                self._finish(w, f"cancelled: mapping stopped ({outcome})")
                raise
            self._finish(w, err, value)

    async def _end_writes(self) -> None:
        """stop(): the write on the bus may finish (for as long as its reply can take, at
        most WRITE_STOP_WAIT_S), else it is cancelled, and its answer says whether its
        request had gone out; then the writes still waiting are answered "cancelled:
        mapping stopped" (none of them was sent). In that order, so the answers keep the
        order of the commands."""
        task, self._writer = self._writer, None
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=min(self._resp_timeout + GUARD_S,
                                                   WRITE_STOP_WAIT_S))
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        waiting, self._writes = list(self._writes), deque()
        for w in waiting:
            self._finish(w, w.refused or "cancelled: mapping stopped")
        dropped = [w.point.name for w in waiting if w.refused is None]
        if dropped:
            more = f" and {len(dropped) - 5} more" if len(dropped) > 5 else ""
            self._log(f"{len(dropped)} MQTT write(s) cancelled before they were sent, the "
                      f"mapping stopped: {_names(dropped[:5])}{more}")

    async def _run_write(self, w: _Write) -> tuple[Optional[str], Any]:
        if w.refused is not None:
            await self._report(w.point)
            return w.refused, None
        waited = time.monotonic() - w.queued
        if waited > WRITE_EXPIRE_S:
            self._note("write expired", w.point.unit, f"write '{w.point.name}' = "
                       f"{shown(w.value)} not made: it waited {waited:.1f} s for the bus "
                       "(expired)")
            return f"expired: waited {waited:.1f} s for the bus", None
        try:
            return None, await self.write_point(w.point, w.value, w)
        except Exception as e:
            err = str(e) or type(e).__name__
            self._log(f"write '{w.point.name}' = {shown(w.value)} failed: {err}")
            if not isinstance(e, NoReply):
                await self._report(w.point)
            return err, None

    async def _report(self, point) -> None:
        """Sparkplug B, a DCMD that wrote nothing (refused, or the slave's exception): the
        point's current value in a DDATA all the same, as after a write, so the host sees
        it was not taken. Not for a unit that did not answer (it goes offline: DDEATH)."""
        if (self._sparkplug is not None and not self._closing
                and point.unit != modbus.BROADCAST and _polled(point)):
            await self._read_back(point)

    def _finish(self, w: _Write, err: Optional[str], value: Any = None) -> None:
        if w.done is not None:
            try:
                w.done(err, value)
            except Exception as e:  # (an answer that can't be sent must not stop the writer)
                self._log(f"write '{w.point.name}': the answer was not sent: {e}")

    def _on_set_message(self, topic: str, payload: bytes) -> None:
        """Plain MQTT: a value for one writable point on its <base_topic>/<point>/set; the
        outcome goes to .../set/result (ok, or error: …), at QoS 1 or the mapping's QoS. A
        JSON request {"value": …, "id": …} gets a JSON answer {"id": …, "ok": true,
        "value": <read back>} / {"id": …, "ok": false, "error": "…"} (see
        parse_write_request)."""
        point = self._set_points.get(topic)
        if point is None or self._mqtt is None:
            return
        mqtt, result = self._mqtt, topic + "/result"
        # at QoS 1 at least, as the /set subscription: paho keeps it over a reconnect
        qos = max(self.mapping.mqtt.qos if isinstance(self.mapping.mqtt.qos, int) else 0, 1)
        request: Optional[dict] = {} if payload[:64].lstrip()[:1] == b"{" else None

        def answer(err: Optional[str], value: Any = None) -> None:
            if request is None:  # plain text: ok, or error: …
                out = b"ok" if err is None else f"error: {err}".encode()
            else:  # a JSON request: its id (if it had one), and the value read back
                reply = dict(request, ok=err is None)
                if err is None:
                    reply["value"] = _json_value(value)
                else:
                    reply["error"] = err
                out = json.dumps(reply, separators=(",", ":")).encode()
            mqtt.publish_raw(result, out, qos=qos)
        if len(payload) > SET_MAX_BYTES:
            too_large = f"too large: {len(payload)} bytes (at most {SET_MAX_BYTES})"
            self._note("write too large", point.unit, f"write '{point.name}' from MQTT "
                       f"refused: {too_large}")
            self._write_soon(point, None, answer, refused=too_large)
            return
        try:
            if request is None:
                value = parse_write_payload(payload, point)
            else:
                value = parse_write_request(payload, point, request)
        except ValueError as e:
            self._write_soon(point, None, answer, refused=str(e))
            return
        if self.mapping.network.read_only:
            self._note("write read-only", point.unit,
                       f"write '{point.name}' from MQTT refused: the mapping is read-only")
            self._write_soon(point, None, answer, refused="mapping is read-only")
            return
        self._write_soon(point, value, answer)

    # ---------------- reading the RTU line ----------------
    async def _settle(self, reader) -> None:
        """Before a request goes out: drop what the line holds, bytes no request is waiting
        for. After a request that got no reply in time (or bytes after its reply), first
        make sure the line has been quiet for the guard time since then, so that a reply
        that comes late is dropped here and not taken for this request's (a read's reply
        carries no address), and say which slave answered late. A line that never goes
        quiet holds the request back by at most a whole frame's time more."""
        loop = asyncio.get_running_loop()
        since, self._dirty = self._dirty, None
        late, self._unanswered = self._unanswered, None
        guard = 0.0 if since is None else max(min(GUARD_S, self._resp_timeout),
                                              3.5 * self._char_s)
        last = loop.time() if since is None else since  # the last sign of life on the line
        end = loop.time() + guard + modbus.MAX_ADU * self._char_s
        got = bytearray()
        while (left := end - loop.time()) > 0:
            wait = max(0.001, last + guard - loop.time())  # 1 ms: take what is buffered
            try:
                chunk = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=min(wait, left))
            except asyncio.TimeoutError:
                break
            if not chunk:
                raise ConnectionError("serial EOF")
            got += chunk
            last = loop.time()
        if not got:
            return
        self._dropped(got)
        if late is not None and modbus.find_reply(got, *late, quiet=True).span is not None:
            unit, request = late
            self._note("late", unit, f"unit {unit} answered FC{request[0]} after the response "
                       f"timeout ({self._resp_timeout:g} s); the late reply was dropped. Raise "
                       "the mapping's response timeout if the slave needs longer")
        else:
            self._note("stray", -1, f"dropped {len(got)} byte(s) on the serial line that no "
                       "request was waiting for")

    async def _read_reply(self, reader, unit: int, request: bytes, sent: float) -> Optional[bytes]:
        """The PDU of the reply from ``unit`` to ``request``, which has gone out on the line
        at ``sent`` (loop time), or None if none came in time.

        The reply has to begin within the response timeout and may then take as long as a
        whole frame needs. It is complete as soon as it is as long as its header says (or
        when the line goes quiet, for a reply whose header does not say), not after a fixed
        pause, and has to answer the request (function, byte count, echo): see
        modbus.find_reply. Bytes around it, and frames that answer something else, are
        dropped; the next request then waits for a quiet line first (_settle)."""
        loop = asyncio.get_running_loop()
        deadline = sent + self._resp_timeout
        finish = deadline + modbus.MAX_ADU * self._char_s + self._gap
        buf = bytearray()
        dropped = 0  # bytes cut off the front of buf (no reply is that long)
        quiet = False
        while True:
            scan = modbus.find_reply(buf, unit, request, quiet=quiet)
            if scan.span is not None:
                start, end = scan.span
                self._dropped(buf[:start])
                self._delivered(bytes(buf[start:end]))
                self._dropped(buf[end:])
                if dropped or end < len(buf):
                    # bytes after the reply: more may be on its way (bytes only in front of
                    # it, such as an adapter's echo of the request, are no reason to wait)
                    self._dirty = loop.time()
                return bytes(buf[start + 1:end - 2])
            left = (finish if scan.pending else deadline) - loop.time()
            if left <= 0:
                break
            if buf and not quiet:
                left = min(left, self._gap)  # then try what came as a whole frame
            try:
                chunk = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=left)
            except asyncio.TimeoutError:
                quiet = bool(buf)
                continue
            if not chunk:
                raise ConnectionError("serial EOF")
            buf += chunk
            quiet = False
            if len(buf) > 2 * modbus.MAX_ADU:  # a reply can't begin before the last MAX_ADU
                self._dropped(buf[:-modbus.MAX_ADU])
                dropped += len(buf) - modbus.MAX_ADU
                del buf[:-modbus.MAX_ADU]
        self._dirty = loop.time()
        self._unanswered = (unit, request)
        if buf or dropped:
            self._dropped(buf)
            what = scan.rejected or ("a frame from it that is cut short or fails its CRC"
                                     if scan.started else "none of them answers it")
            self._note("bad", unit, f"unit {unit}: no usable reply to FC{request[0]} within "
                       f"{self._resp_timeout:g} s; dropped {dropped + len(buf)} byte(s) "
                       f"({what})")
        return None

    async def _turnaround(self, reader, sent: float) -> None:
        """After a broadcast, which no slave answers: keep the bus until the request is out
        (``sent``) and the slaves have had the turnaround time to carry it out. What comes
        meanwhile is dropped."""
        loop = asyncio.get_running_loop()
        end = sent + min(TURNAROUND_S, self._resp_timeout)
        got = bytearray()
        while (left := end - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=left)
            except asyncio.TimeoutError:
                break
            if not chunk:
                raise ConnectionError("serial EOF")
            got += chunk
        if got:
            self._dropped(got)
            self._dirty = loop.time()
            self._note("stray", modbus.BROADCAST, f"dropped {len(got)} byte(s) that came after "
                       "a broadcast to unit 0, which no slave may answer")

    def _delivered(self, frame: bytes) -> None:
        self.status.bytes_in += len(frame)
        self._feed_monitors(b"\xaa rx " + frame)
        if self._tracer:
            self._tracer.write("ser>net", frame)
        self._feed_traffic(FROM_DEVICE, frame)

    def _dropped(self, data) -> None:
        """Bytes read off the line that are not a reply: counted, and shown in the trace
        (as ser>drop), the console and the hex view like any others."""
        if not data:
            return
        data = bytes(data)
        self.status.bytes_in += len(data)
        self._feed_monitors(b"\xaa rx (dropped) " + data)
        if self._tracer:
            self._tracer.write("ser>drop", data)
        self._feed_traffic(FROM_DEVICE, data)

    def _note(self, kind: str, unit: int, message: str) -> bool:
        """Log a note about the replies on the line, at most once per NOTE_EVERY_S for one
        kind and unit: a slave that keeps failing every request must not flood the log.
        Returns whether it was logged (or only counted)."""
        now = time.monotonic()
        last, held = self._notes.get((kind, unit), (None, 0))
        if last is not None and now - last < NOTE_EVERY_S:
            self._notes[(kind, unit)] = (last, held + 1)
            return False
        self._notes[(kind, unit)] = (now, 0)
        more = f" (and {held} more since the last such line)" if held else ""
        self._log(message + more)
        return True

    # ---------------- network side ----------------
    def _log_policy(self) -> None:
        """Log at start which of the mapping's options a Modbus gateway treats differently:
        the ones it does not use, and what read-only means here."""
        opts = self.mapping.options
        unused = [name for name, value in (("banner", opts.banner), ("openstr", opts.openstr),
                                           ("closestr", opts.closestr), ("closeon", opts.closeon))
                  if value]
        if unused:
            self._log(f"not used by a Modbus gateway: {', '.join(unused)} "
                      "(bytes outside Modbus frames would break the framing)")
        if self.mapping.network.read_only:
            self._log("read-only: requests from Modbus/TCP masters that are not reads get "
                      "exception 01 (illegal function) and never reach the bus; MQTT/Sparkplug "
                      "writes to writable points are refused too")
        poll = self.mapping.modbus_poll
        if poll.broadcast_points:
            self._log(f"warning: poll point(s) {_names(poll.broadcast_points)} on unit 0, the "
                      "broadcast address: never read (no slave answers a broadcast); a write "
                      "to one goes to every slave on the line and is not read back")
        if poll.reserved_unit_points:
            self._log(f"warning: poll point(s) {_names(poll.reserved_unit_points)} on units "
                      "248-255, which a Modbus serial line reserves (slave addresses are 1-247)")
        for p in poll.points:
            unusable = [key for key in ("min", "max", "allowed")
                        if getattr(p, key) is not None and _limit(p, key) is None]
            if p.writable and unusable:
                self._log(f"warning: point {p.name!r}: {' and '.join(unusable)} ignored (not a "
                          "number, or for allowed not a list of values of the point's type)")
            if point_scale(p) is not p.scale:
                self._log(f"warning: point {p.name!r}: scale {shown(p.scale)} ignored (not a "
                          "number): its values are not scaled")
            if point_order(p) != p.word_order:
                self._log(f"warning: point {p.name!r}: word_order {shown(p.word_order)} ignored "
                          "(not ABCD, CDAB, BADC or DCBA): read as ABCD")
        mq = self.mapping.mqtt
        missing = [what for what, have in (("TLS", mq.tls), ("a username", mq.username))
                   if not have]
        if self.mapping.accepts_mqtt_writes and missing:
            self._log("warning: writable points take MQTT commands over a broker connection "
                      f"without {' or '.join(missing)}: any client the broker lets publish to "
                      "the command topics can write them; restrict those topics with the "
                      "broker's access rules (ACLs)")

    def _refuse(self, master: _Master, pdu: bytes) -> bytes:
        """Answer a request a read-only gateway does not forward: exception 01 (illegal
        function). The first one per master connection is logged, the rest are counted."""
        function = pdu[0] if pdu else 0
        master.refused += 1
        if master.refused == 1:
            self._log(f"read-only: refused FC{function} from {master.peer} with exception 01 "
                      "(further refusals for this master are only counted)")
        return modbus.exception_pdu(function, modbus.ILLEGAL_FUNCTION)

    def _refresh_clients(self) -> None:
        self.status.set_clients(self._clients)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("?", 0)
        peer_ip = str(peername[0]) if isinstance(peername, tuple) else str(peername)
        peer = f"{peer_ip}:{peername[1]}" if isinstance(peername, tuple) and len(peername) > 1 else peer_ip
        net = self.mapping.network

        # allow-list, then max connections (a priority master / kick_old_user makes room)
        admission = self._access.admit(peer_ip, peer, self._clients, self._log)
        if not admission.admitted:
            with contextlib.suppress(Exception):
                writer.close()
            return

        if err := sockopts.apply_keepalive(writer, net):
            self._log(f"TCP keepalive not enabled for {peer}: {err}")
        master = _Master(writer, peer, priority=admission.priority)
        self._clients.add(master)
        self._refresh_clients()
        tag = " [priority]" if master.priority else ""
        self._log(f"modbus master connected: {peer}{tag} ({len(self._clients)} total)")
        opts = self.mapping.options
        deadline = (time.monotonic() + opts.max_connection_time_s
                    if opts.max_connection_time_s > 0 else None)
        reason = "closed by master"
        buf = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    data = await asyncio.wait_for(
                        reader.read(READ_CHUNK), timeout=_next_timer(opts.idle_timeout_s, deadline))
                except asyncio.TimeoutError as e:
                    if getattr(e, "errno", None) is not None:
                        raise  # a socket error (e.g. keepalive ETIMEDOUT), not our timer
                    if deadline is not None and time.monotonic() >= deadline:
                        reason = ("max connection time reached "
                                  f"({fmt_duration(opts.max_connection_time_s)})")
                    else:
                        reason = f"idle timeout ({fmt_duration(opts.idle_timeout_s)} without traffic)"
                    break
                if not data:
                    break
                buf += data
                while True:
                    try:
                        frame = modbus.take_tcp_adu(buf)
                    except ValueError as e:
                        self._log(f"bad Modbus/TCP framing from {peer}: {e}")
                        reason = "bad Modbus/TCP framing"
                        return
                    if frame is None:
                        break
                    txn, unit, pdu = frame
                    if net.read_only and not modbus.is_read_request(pdu):
                        rpdu = self._refuse(master, pdu)
                    else:
                        rpdu = await self._transaction(unit, pdu)
                    writer.write(modbus.build_tcp_adu(txn, unit, rpdu))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as e:
            reason = describe_disconnect(e)
        finally:
            self._clients.discard(master)
            self._refresh_clients()
            with contextlib.suppress(Exception):
                writer.close()
            reason = master.close_reason or reason
            if self._stop.is_set():
                reason = "gateway stopped"
            held = fmt_duration(time.time() - master.connected_at)
            refused = f", {master.refused} request(s) refused (read-only)" if master.refused else ""
            self._log(f"modbus master disconnected: {peer} after {held} — {reason}{refused} "
                      f"({len(self._clients)} total)")


def _now_ms() -> int:
    return int(time.time() * 1000)


def encode_value(value, fmt: str, ts: int, good: bool = True) -> bytes:
    """A polled value as a plain MQTT payload: text (1 / 0 for a bit; also for a value_format
    this version doesn't know), or for "json" {"value", "ts" (ms since the epoch),
    "quality"}: a bit is true / false, a NaN or infinite reading null; ``good`` False: the
    read failed (value null, quality "bad")."""
    if fmt != "json":
        return (("1" if value else "0") if isinstance(value, bool) else str(value)).encode()
    if not good or (isinstance(value, float) and not math.isfinite(value)):
        value = None
    return json.dumps({"value": value, "ts": ts, "quality": "good" if good else "bad"},
                      separators=(",", ":")).encode()


def _polled(point) -> bool:
    """Whether a point is read (False: "poll": false, a write-only point)."""
    return point.poll is not False


def _limit(point, key: str):
    """A point's min / max / allowed as the gateway can use it, or None (none set, or a
    stored one that can't be used: the edits refuse such values, see
    ModbusPoll.validate_input)."""
    value = getattr(point, key)
    if key == "allowed":
        coil = point.fn in (modbus.READ_COILS, modbus.READ_DISCRETE_INPUTS)
        ok = isinstance(value, list) and value and all(
            isinstance(v, bool) if coil else _finite(v) for v in value)
        return value if ok else None
    return value if _finite(value) and point.fn not in (
        modbus.READ_COILS, modbus.READ_DISCRETE_INPUTS) else None


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def check_limits(point, value) -> None:
    """A value MQTT writes to ``point``, against the point's allowed / min / max (in its own
    units, before the scale is undone). Raises ValueError."""
    allowed = _limit(point, "allowed")
    if allowed is not None and not any(
            (value is v) if isinstance(v, bool) else (not isinstance(value, bool) and value == v)
            for v in allowed):
        listed = ", ".join(_num(v) if not isinstance(v, bool) else str(v).lower()
                           for v in allowed[:8]) + (", …" if len(allowed) > 8 else "")
        shown_value = str(value).lower() if isinstance(value, bool) else _num(value)
        raise ValueError(f"{shown_value} is not one of the values allowed for "
                         f"{point.name!r} ({listed})")
    if isinstance(value, bool):
        return
    low, high = _limit(point, "min"), _limit(point, "max")
    if low is not None and high is not None and not low <= value <= high:
        raise ValueError(f"{_num(value)} is outside the range {_num(low)}..{_num(high)} of "
                         f"{point.name!r}")
    if low is not None and value < low:
        raise ValueError(f"{_num(value)} is below the minimum {_num(low)} of {point.name!r}")
    if high is not None and value > high:
        raise ValueError(f"{_num(value)} is above the maximum {_num(high)} of {point.name!r}")


def _raw_text(value, regs: list[int], point) -> str:
    """For the log: the raw register value written, when it is not the value itself (an
    integer type rounds a fraction, a scale divides it)."""
    raw = modbus.decode_value(b"".join(r.to_bytes(2, "big") for r in regs), point.dtype,
                              order=point_order(point))
    scale = point_scale(point)
    scaled = bool(scale) and scale != 1.0
    if not scaled and (point.dtype in ("float32", "float64") or raw == value):
        return ""
    return f", raw value {_num(raw)}"


def _registers(p) -> int:
    """How many registers a register point (fn 3, 4) reads: its string's length, one for a
    bit of a register, else its type's. ValueError for a stored type the edits refuse."""
    if p.dtype == "string":
        return p.length
    if p.dtype == "bool":
        return 1
    return modbus.dtype_registers(p.dtype)


def decode_point(p, data: bytes, offset: int = 0):
    """A register point's value from read data (response_data), its registers starting at
    ``offset`` registers in: a bit of a register, a string, or a number as the point's type,
    word order and scale."""
    data = data[offset * 2:]
    if p.dtype == "bool":
        return modbus.register_bit(data, p.bit)
    if p.dtype == "string":
        if len(data) < p.length * 2:
            raise ValueError("not enough data for the string")
        return modbus.decode_string(data[:p.length * 2], point_order(p))
    return modbus.decode_value(data, p.dtype, point_scale(p), point_order(p))


def _units_text(units: list[int]) -> str:
    return (f"unit {units[0]}" if len(units) == 1
            else "units " + ", ".join(str(u) for u in units))


def _names(names: list[str]) -> str:
    return ", ".join(repr(n) for n in names)


# a holding register's value: a decimal integer, 0x hex, or a decimal fraction / exponent
_NUMBER = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_NUMBER_MAX_LEN = 64


def parse_write_payload(payload: bytes, point):
    """The value of a plain-MQTT write to ``point``: one coil state (1/0, true/false or
    on/off), or one number for a holding register (decimal, 0x hex, or a decimal fraction
    or exponent; write_point encodes it as the point's type). Anything else, a JSON list
    included, raises ValueError: a command writes the point it was sent to, nothing more."""
    text = payload.decode("utf-8", "replace").strip()
    if point.fn == modbus.READ_COILS:
        return _coil(text)
    if len(text) > _NUMBER_MAX_LEN or not _NUMBER.fullmatch(text):
        raise ValueError(f"not a number: {shown(text)}")
    if "x" in text or "X" in text:
        return int(text, 16)
    return float(text) if any(c in text for c in ".eE") else int(text)


def _coil(text: str) -> bool:
    t = text.lower()
    if t in ("1", "true", "on"):
        return True
    if t in ("0", "false", "off"):
        return False
    raise ValueError(f"a coil takes one state: 1/0, true/false or on/off, not {shown(text)}")


_ID_MAX_LEN = 64


def parse_write_request(payload: bytes, point, request: dict):
    """The value of a plain-MQTT write sent as JSON, {"value": …, "id": …}: one coil state
    (true / false, or 1 / 0) or one JSON number; "id" (optional: a string of at most 64
    characters or an integer) is put into ``request`` as soon as it is read, so that even
    the answer to a request refused here carries it. Raises ValueError."""
    def no_constant(name: str):
        raise ValueError(f"{name} is not a number")
    try:
        doc = json.loads(payload.decode("utf-8"), parse_constant=no_constant)
    except (ValueError, RecursionError):  # (UnicodeDecodeError too; and deep nesting)
        raise ValueError(f"not a JSON request: {shown(payload)}") from None
    if not isinstance(doc, dict):
        raise ValueError('a JSON request is an object: {"value": …, "id": …}')
    if "id" in doc:
        rid = doc["id"]
        if not ((isinstance(rid, str) and len(rid) <= _ID_MAX_LEN)
                or (isinstance(rid, int) and not isinstance(rid, bool)
                    and abs(rid) < 1 << 63)):
            raise ValueError(f"the id must be a string of at most {_ID_MAX_LEN} characters "
                             f"or an integer, not {shown(rid)}")
        request["id"] = rid
    unknown = sorted(str(k) for k in doc if k not in ("value", "id"))
    if unknown:
        raise ValueError(f"unknown key(s) in the request: {shown(', '.join(unknown))}")
    if "value" not in doc:
        raise ValueError('the request has no "value"')
    value = doc["value"]
    if point.fn == modbus.READ_COILS:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return value == 1
        raise ValueError(f"a coil takes true / false or 1 / 0, not {shown(value)}")
    return value  # write_point takes a number (and refuses anything else)


def _json_value(value):
    """A value read back, for a JSON answer: a NaN or infinite reading is null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
