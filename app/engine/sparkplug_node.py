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
  * a unit that stops answering (the gateway's poll loop decides, see
    ModbusGatewayRunner._poll_unit) gets a DDEATH, its values are dropped and the births
    leave it out; when it answers again it gets a DBIRTH with the values just read. A point
    whose read fails is not reported with its old value again (a birth sends it as null
    until it reads again). Unit 0 (broadcast, write-only) is always born, with nulls;
  * DCMD: a metric named like a writable point of that device is written to the bus
    (through the gateway's write_point), unless the mapping is read-only (then the
    command is logged and refused). Either way the point is read back and sent in a DDATA,
    changed or not, so the host learns the outcome (not when the unit did not answer: it
    goes offline, with a DDEATH); NCMD: "Node Control/Rebirth" = true
    republishes NBIRTH and the DBIRTHs, at most once every REBIRTH_EVERY_S (a request
    that comes sooner is carried out then). Commands are never retained (Sparkplug 3.0), so
    a retained NCMD / DCMD the broker replays on subscribe is dropped (MqttPublisher). A
    command larger than CMD_MAX_BYTES, a DCMD with more metrics than its device has
    points and an NCMD with more than NCMD_MAX_METRICS are ignored before they are
    decoded further; what a command leaves out (metrics that are not writable points of
    the device, nulls) is logged in one line per command, at most once a minute per kind;
  * stopping the mapping publishes NDEATH before disconnecting (a host then takes the
    node's devices offline too);
  * with a primary host (mqtt.sparkplug_primary_host_id), the node also subscribes to
    that host application's STATE (Sparkplug 3.0: spBv1.0/STATE/<id>, JSON {"online",
    "timestamp"}; also the older STATE/<id>, ONLINE / OFFLINE) and publishes its births
    only while it says online: after each (re)connect it waits for it (the broker sends
    the retained STATE on subscribe), and when the host goes offline the node sends
    NDEATH, disconnects and connects again, to wait once more. A STATE older than the
    newest one taken is ignored. One broker only: no list to walk, and nothing is kept
    for a host that is away (no store and forward).
After a lost connection the next session gets bdSeq + 1 (the Will is renewed in
MqttPublisher's disconnect hook), as the specification asks. With a data folder
(``state_dir``), the bdSeq of every session is stored there per edge node (BDSEQ_FILE), and
the node's next run goes on from it: after a power loss the broker may publish the old
session's Will only after the new session's NBIRTH, and a host tells the two apart by
their bdSeq. Without a stored one the first run starts at 0; if the file can't be read or
written, at a random bdSeq (logged).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import threading
import time
from typing import Callable, Optional

from ..config import point_scale
from . import sparkplug
from .mqtt_pub import MqttPublisher, shown

INT64 = sparkplug.DataType.Int64  # the bdSeq metric's datatype (Sparkplug 3.0)
BOOLEAN = sparkplug.DataType.Boolean

# Inbound commands: a larger NCMD / DCMD is ignored unread (a generous backstop: what bounds
# a command is its metric count, see on_device_command), and so is an NCMD with more
# metrics than this (the node has only its Node Control metrics).
CMD_MAX_BYTES = 256 * 1024
NCMD_MAX_METRICS = 64
# A rebirth request that comes sooner than this after the last births is carried out then
# (once, for all such requests), not at once: a host asking again and again can't make the
# node publish its births back to back.
REBIRTH_EVERY_S = 2.0
# Log lines about the commands that come in: at most one per this many seconds per kind and
# device; the ones in between are counted into the next.
NOTE_EVERY_S = 60.0


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
        self.dead: set[str] = set()  # devices whose unit stopped answering (DDEATH)
        self._log = log
        self._write = write          # gateway._write_soon(point, value, refused=None)
        self._pub: MqttPublisher | None = None
        self._bdseq_file = os.path.join(state_dir, BDSEQ_FILE) if state_dir else None
        self._bdseq_saved = True     # False once storing it failed (logged once)
        self.bdseq = self._first_bdseq()
        self.seq = 0
        self._sessions = 0           # connects seen (the first one keeps the first bdSeq)
        self._born = False           # this MQTT session's NBIRTH and DBIRTHs are out
        # the primary host application whose STATE gates the births ("" = none)
        self.primary_host = str(mq.sparkplug_primary_host_id or "").strip()
        if self.primary_host:
            try:
                sparkplug.state_topic(self.primary_host)
            except ValueError as e:
                self._log(f"Sparkplug: primary host ID ignored, births don't wait for it: {e}")
                self.primary_host = ""
        self._host_online = False    # its last STATE in this session said online
        self._host_stamp: Optional[int] = None  # timestamp of the newest 3.0 STATE taken
        self._waiting_said = False   # "waiting for the primary host" logged this session
        self._restart: Optional[asyncio.Task] = None  # NDEATH + a new session, under way
        self._last_birth = 0.0       # time.monotonic() of the last births
        self._rebirth: Optional[asyncio.TimerHandle] = None  # a rebirth due (REBIRTH_EVERY_S)
        self._notes: dict[str, tuple[float, int]] = {}  # see _note

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
        # (S311: a sequence number a host application compares, not a secret)
        first = random.randrange(256) if problem else 0 if last is None else (last + 1) % 256  # noqa: S311
        try:
            save_bdseq(self._bdseq_file, self._bdseq_key, first)
        except OSError as e:
            if not problem:
                first = random.randrange(256)  # noqa: S311 - as above
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
        self._host_online = self._waiting_said = False  # wait for its STATE again
        if self._sessions:
            self.bdseq = (self.bdseq + 1) % 256
            self._save_bdseq()
        self._sessions += 1
        return self._ndeath()

    def publisher(self) -> MqttPublisher:
        dcmd = "/".join((sparkplug.TOPIC_NAMESPACE, self.group, "DCMD", self.node, "+"))
        subscriptions = {self.topic("NCMD"): self.on_node_command, dcmd: self.on_device_command}
        state_topics: tuple = ()
        if self.primary_host:  # its STATE is retained: the current state, not a command
            state_topics = (sparkplug.state_topic(self.primary_host),
                            f"STATE/{self.primary_host}")  # Sparkplug 2.2
            subscriptions.update(dict.fromkeys(state_topics, self.on_host_state))
        self._pub = MqttPublisher(self.mapping.mqtt, logger=self._log, will=self._will,
                                  birth=False, on_connect=self.birth, accept_retained=False,
                                  subscriptions=subscriptions, state_topics=state_topics)
        host = f"; births wait for primary host {self.primary_host!r}" if self.primary_host else ""
        self._log(f"Sparkplug B edge node {self.group}/{self.node}: "
                  f"{len(self.devices)} device(s), {len(self.points)} metrics{host}")
        return self._pub

    def _metric(self, name: str, now: int) -> sparkplug.Metric:
        """A point's metric: its last value, stamped with when it was read; a null (never
        read) stamped ``now``."""
        p = self.points[name]
        dt = sparkplug.datatype_for(p.dtype, point_scale(p) or 1.0)
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
        """NBIRTH, then a DBIRTH per device (after each connect, and on a rebirth request);
        with a primary host, only while it is online."""
        if self.primary_host and (not self._host_online or self._restart is not None):
            if not self._waiting_said:
                self._waiting_said = True
                self._log(f"Sparkplug: waiting for primary host {self.primary_host!r} to be "
                          f"online ({sparkplug.state_topic(self.primary_host)}) before NBIRTH")
            return
        if self._rebirth is not None:  # these births answer it
            self._rebirth.cancel()
            self._rebirth = None
        self._last_birth = time.monotonic()
        self.seq = 0
        self._publish(self.topic("NBIRTH"), [
            sparkplug.Metric(name=sparkplug.BDSEQ, datatype=INT64, value=self.bdseq),
            sparkplug.Metric(name=sparkplug.REBIRTH, datatype=BOOLEAN, value=False)])
        for dev in self.devices:
            if dev not in self.dead:
                self._dbirth(dev)
        self._born = True

    def _dbirth(self, dev: str) -> None:
        self.seq = sparkplug.next_seq(self.seq)
        now = _now_ms()
        metrics = sorted((self._metric(n, now) for n in self.devices[dev]), key=_stamp)
        self._publish(self.topic("DBIRTH", dev), metrics)

    def device_offline(self, unit: int) -> None:
        """The unit stopped answering: a DDEATH for its device (once the session is born),
        whose values are dropped; births leave it out until device_online()."""
        dev = device_id(unit)
        if dev not in self.devices or dev in self.dead:
            return
        self.dead.add(dev)
        self.forget(self.devices[dev])
        if self._born:
            self.seq = sparkplug.next_seq(self.seq)
            self._publish(self.topic("DDEATH", dev), [])

    def device_online(self, unit: int, values: dict, stamps: dict) -> None:
        """The unit answers again: a DBIRTH with the values just read (once the session is
        born; else the next birth carries them)."""
        dev = device_id(unit)
        if dev not in self.devices:
            return
        self.dead.discard(dev)
        self.forget(self.devices[dev])
        for n, v in values.items():
            if n in self.points:
                self.values[n], self.stamps[n] = v, stamps.get(n, _now_ms())
        if self._born:
            self._dbirth(dev)

    def forget(self, names) -> None:
        """Points whose read failed (or whose unit is offline): their last value is not
        reported again; a birth sends them as null until they are read again."""
        for n in names:
            self.values.pop(n, None)
            self.stamps.pop(n, None)

    def update(self, values: dict, stamps: dict | None = None, force=()) -> None:
        """A DDATA per device with the metrics whose value changed since the last report
        (a point read again after a failed read counts as changed), and those in ``force``
        whether they changed or not (the value read back after a DCMD); ``stamps``: when
        each value was read (ms since the epoch; now if not given). Before the session's
        births the values are only kept: the births carry them."""
        now = _now_ms()
        changed = set()
        for n, v in values.items():
            if n not in self.points or device_id(self.points[n].unit) in self.dead:
                continue  # an offline unit's values come with its next DBIRTH
            if _changed(self.values.get(n), v) or n in force:
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

    def _note(self, key: str, message: str) -> None:
        """Log a line about an inbound command at most once per NOTE_EVERY_S for one
        ``key`` (kind and device): a host that keeps sending such commands, or a flood of
        them, must not flood the log."""
        now = time.monotonic()
        last, held = self._notes.get(key, (None, 0))
        if last is not None and now - last < NOTE_EVERY_S:
            self._notes[key] = (last, held + 1)
            return
        self._notes[key] = (now, 0)
        self._log(message + (f" (and {held} more since the last such line)" if held else ""))

    def _decode(self, kind: str, where: str, payload: bytes, max_metrics: int):
        """A command's payload, or None (logged) if it is too large or can't be used."""
        if len(payload) > CMD_MAX_BYTES:
            self._note(f"{kind} size {where}", f"Sparkplug {kind} for {where} ignored: "
                       f"{len(payload)} bytes, more than the {CMD_MAX_BYTES} a command may "
                       "have")
            return None
        try:
            return sparkplug.decode_payload(payload, max_metrics=max_metrics)
        except ValueError as e:
            self._note(f"{kind} bad {where}", f"Sparkplug {kind} for {where} ignored: {e}")
            return None

    def on_node_command(self, topic: str, payload: bytes) -> None:
        """NCMD: "Node Control/Rebirth" is the node's only metric; points are written
        with a DCMD to their device. One NCMD gives one rebirth at most."""
        cmd = self._decode("NCMD", "the node", payload, NCMD_MAX_METRICS)
        if cmd is None:
            return
        others = [m.name for m in cmd.metrics if m.name != sparkplug.REBIRTH]
        if others:
            self._note("NCMD ignored", f"Sparkplug NCMD for {_listed(None, others)} ignored "
                       "(points are written with a DCMD to their device)")
        # a Boolean true, not just any truthy value
        if any(m.name == sparkplug.REBIRTH and m.value is True for m in cmd.metrics):
            self._rebirth_requested()

    def _rebirth_requested(self) -> None:
        """A host asks for the births: now, or REBIRTH_EVERY_S after the last ones (once
        for all the requests that come until then)."""
        wait = self._last_birth + REBIRTH_EVERY_S - time.monotonic()
        if wait <= 0:
            self._log("Sparkplug rebirth requested")
            self.birth()
        elif self._rebirth is None:
            self._log(f"Sparkplug rebirth requested; the births go out in {wait:.1f} s (at "
                      f"most one rebirth every {REBIRTH_EVERY_S:g} s)")
            self._rebirth = asyncio.get_running_loop().call_later(wait, self._rebirth_due)

    def _rebirth_due(self) -> None:
        self._rebirth = None
        if self._pub is not None:
            self.birth()

    def on_host_state(self, topic: str, payload: bytes) -> None:
        """The primary host's STATE: online -> births (if not born yet in this session);
        offline -> NDEATH and a new session, which waits for it again."""
        online = self._host_state(topic, payload)
        if online is None:
            return
        self._host_online = online
        if online and not self._born and self._restart is None:
            self._log(f"Sparkplug: primary host {self.primary_host!r} is online")
            self.birth()
        elif not online and self._born and self._restart is None and self._pub is not None:
            self._log(f"Sparkplug: primary host {self.primary_host!r} is offline: NDEATH, then "
                      "a new session that waits for it")
            self._born = False
            pub, final = self._pub, self._ndeath()

            async def restart() -> None:
                try:
                    await pub.restart(final)
                finally:
                    self._restart = None
                if self._host_online and not self._born and self._pub is pub:
                    self.birth()  # its STATE came back while the new session began
            self._restart = asyncio.get_running_loop().create_task(
                restart(), name=f"sparkplug-restart:{self.node}")

    def _host_state(self, topic: str, payload: bytes) -> Optional[bool]:
        """Whether a STATE message says online (None: not a usable one, or older than the
        newest taken). Sparkplug 3.0: JSON {"online": bool, "timestamp": ms}; 2.2 (topic
        STATE/<id>): the text ONLINE / OFFLINE."""
        what = f"Sparkplug STATE of {self.primary_host!r} ignored"
        if not topic.startswith(sparkplug.TOPIC_NAMESPACE + "/"):
            text = payload.decode("utf-8", "replace").strip().upper()
            if text in ("ONLINE", "OFFLINE"):
                return text == "ONLINE"
            self._log(f"{what}: {shown(payload)} is neither ONLINE nor OFFLINE")
            return None
        try:
            doc = json.loads(payload.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError too
            self._log(f"{what}: not JSON ({shown(payload)})")
            return None
        online = doc.get("online") if isinstance(doc, dict) else None
        if not isinstance(online, bool):
            self._log(f"{what}: no \"online\": true / false in {shown(payload)}")
            return None
        stamp = doc.get("timestamp")
        if isinstance(stamp, int) and not isinstance(stamp, bool):
            if self._host_stamp is not None and stamp < self._host_stamp:
                self._log(f"{what}: older ({stamp}) than the one taken ({self._host_stamp})")
                return None
            self._host_stamp = stamp
        return online

    def on_device_command(self, topic: str, payload: bytes) -> None:
        """DCMD: each metric that is a writable point of the device is written, in the
        order they come (a coil switched on and off again is switched twice); a DCMD can't
        have more metrics than its device has points."""
        dev = topic.rsplit("/", 1)[-1]
        names = self.devices.get(dev)
        if names is None:
            self._note("DCMD unknown", f"Sparkplug DCMD for an unknown device {shown(dev)} "
                       "ignored")
            return
        cmd = self._decode("DCMD", dev, payload, len(names))
        if cmd is None:
            return
        ignored, nulls, refused = [], [], []
        for m in cmd.metrics:
            point = self.points.get(m.name) if m.name in names else None
            if point is None or not point.writable:
                ignored.append(m.name)
                continue
            if m.is_null:
                nulls.append(m.name)
                continue
            if self.mapping.network.read_only:  # checked per command, not at subscribe time
                refused.append(m.name)
                self._write(point, m.value, refused="mapping is read-only")  # (a read-back)
                continue
            value = m.value
            # Sparkplug 3.0 lets a host leave the datatype off a command; the value then
            # arrives as the raw unsigned wire field — read it as the point's signed type
            if not m.datatype and isinstance(value, int) and not isinstance(value, bool):
                if point.dtype in ("int16", "int32") and value > 0x7FFFFFFF:
                    value -= 1 << 32
                elif point.dtype == "int64" and value > 0x7FFFFFFFFFFFFFFF:
                    value -= 1 << 64
            self._write(point, value)
        if ignored:
            what = "a writable point" if len(ignored) == 1 else "writable points"
            self._note(f"DCMD ignored {dev}", f"Sparkplug DCMD for {_listed(dev, ignored)} "
                       f"ignored (not {what})")
        if nulls:
            self._note(f"DCMD null {dev}", f"Sparkplug DCMD for {_listed(dev, nulls)} "
                       "ignored: null, no value to write")
        if refused:
            self._note(f"DCMD read-only {dev}", f"Sparkplug DCMD for {_listed(dev, refused)} "
                       "refused: the mapping is read-only")

    def close(self) -> None:
        self._cancel_rebirth()
        if self._restart is not None:  # its new session must not outlive the mapping
            self._restart.cancel()
        if self._pub is not None:
            self._pub.close(final=self._ndeath())
            self._pub = None

    async def aclose(self) -> None:
        """close() without holding up the event loop (MqttPublisher.aclose)."""
        self._cancel_rebirth()
        if self._restart is not None:  # its new session must not outlive the mapping
            self._restart.cancel()
        pub, self._pub = self._pub, None
        if pub is not None:
            await pub.aclose(final=self._ndeath())

    def _cancel_rebirth(self) -> None:
        if self._rebirth is not None:
            self._rebirth.cancel()
            self._rebirth = None


def _listed(dev: Optional[str], names: list) -> str:
    """Metric names (as they came, cut short) for a log line: the first three, then how
    many more."""
    shown_ = [f"{dev}/{shown(n)}" if dev else shown(n) for n in names[:3]]
    more = f" and {len(names) - 3} more" if len(names) > 3 else ""
    return ", ".join(shown_) + more
