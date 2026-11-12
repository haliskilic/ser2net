"""Modbus RTU<->TCP gateway runner.

A drop-in alternative to MappingRunner, selected when a mapping's network
protocol is ``modbus``. It listens as a Modbus/TCP server: each connected master
sends Modbus/TCP requests, which the gateway converts to Modbus/RTU, forwards on
the shared serial bus (one transaction at a time, since RTU is a single-master
bus), reads the slave's RTU reply, and returns it to that master as Modbus/TCP —
echoing the transaction id so concurrent masters don't get crossed wires. A request
whose slave doesn't answer in time is answered with exception 0x0B (gateway target
failed to respond); while the serial port is down, every request is answered with 0x0A
(gateway path unavailable) at once.

Shares the lifecycle shape (and the H3/H4 fixes) of MappingRunner: the same
supervised serial port (serial_link.SerialLink: reopened, and its USB identity
re-resolved, whenever it is lost) plus an asyncio TCP server, both torn down
cleanly on stop(). Masters are admitted by the same policy as bridge clients
(access.AccessPolicy: allow-list, max connections, priority IPs, kick_old_user);
a read-only gateway answers every request that is not known to be a read with
exception 01 and never puts it on the bus, and refuses MQTT/Sparkplug writes as well.
A banner, open/close strings and closeon don't apply: bytes outside Modbus frames
would break the framing.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..config import MappingConfig, ModbusPoint
from . import modbus, netguard, sockopts, tls
from .access import AccessPolicy, connection_order, server_ssl
from .bridge import (
    FROM_DEVICE, TO_DEVICE, RunnerStatus, Tracer, describe_disconnect, fmt_duration,
    log_ignored_settings, open_tracer,
)
from .mqtt_pub import shown
from .serial_link import SerialLink

if TYPE_CHECKING:
    from .mqtt_pub import MqttPublisher
    from .sparkplug_node import SparkplugNode

READ_CHUNK = 512


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
    seq: int = field(default_factory=connection_order)          # access.Client
    connected_at: float = field(default_factory=time.monotonic)
    close_reason: str = ""          # why the gateway dropped it (first cause wins)
    refused: int = 0                # requests refused because the mapping is read-only
    closing: bool = False           # kicked: listed until its handler is done

    def kick(self, reason: str = "kicked") -> None:
        self.closing = True
        self.close_reason = self.close_reason or reason
        with contextlib.suppress(Exception):
            self.writer.transport.abort()


class ModbusGatewayRunner:
    def __init__(self, mapping: MappingConfig, logger=None, trace_dir: Optional[str] = None):
        self.mapping = mapping
        self.trace_dir = trace_dir
        self._tracer: Optional[Tracer] = None
        self.status = RunnerStatus(state="stopped")
        self.log = logger
        self._serial_task: Optional[asyncio.Task] = None
        self._server: Optional[netguard.Listener] = None
        self._stop = asyncio.Event()
        # the RTU bus; is_open / ready say whether requests can reach it right now
        self._link = SerialLink(mapping.serial, self.status, self._stop, self._log,
                                owner=mapping.name)
        self._bus_lock = asyncio.Lock()      # RTU is single-master: one txn at a time
        self._clients: set[_Master] = set()
        self._monitors: set = set()           # browser console observers (read the bus)
        self._traffic: set = set()            # live hex view observers
        self._mqtt: Optional[MqttPublisher] = None  # optional MQTT publisher for register polling
        self._sparkplug: Optional[SparkplugNode] = None  # payload format sparkplug_b
        self._write_tasks: set = set()
        self._poll_task: Optional[asyncio.Task] = None
        self._publish_error = ""   # the last publishing failure logged by the poll loop
        # plain MQTT: the command topic of each writable point, matched exactly (a point
        # name is never read back out of a topic)
        base = mapping.mqtt.base_topic.rstrip("/")
        self._set_points: dict[str, ModbusPoint] = {
            f"{base}/{p.name}/set": p for p in mapping.modbus_poll.points if p.writable}
        self._access = AccessPolicy(mapping.network)
        self._refusal_log = netguard.RefusalLog(self._log)  # refused connections
        # inter-frame gap used to detect the end of an RTU reply (timing-based framing)
        baud = max(1, mapping.serial.baudrate)
        self._gap = max(0.01, 3.5 * 11 / baud)
        self._resp_timeout = mapping.options.modbus_response_timeout_s or 1.0

    def _log(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    @property
    def tracer(self) -> Optional[Tracer]:
        """The trace writer while the mapping runs with tracing on."""
        return self._tracer

    @property
    def listener(self) -> Optional[netguard.Listener]:
        """The Modbus TCP listener while the mapping runs, else None."""
        return self._server

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

    def wake_serial(self) -> None:
        """A serial device was plugged in (PortWatcher): try the port now if it is waiting
        to be opened again."""
        self._link.wake()

    async def serial_write(self, data: bytes) -> bool:
        # the modbus console is observe-only (injecting raw bytes would corrupt
        # the request/response framing on the shared bus)
        return False

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._stop.clear()
        net = self.mapping.network
        # the listener first (as MappingRunner.start): a start that fails there has opened
        # neither the serial port nor a broker connection, so it can be tried again
        self._server = await netguard.start_server(
            self._on_client, netguard.listen_hosts(net), net.port, ssl=server_ssl(net),
            allowed=self._access.allowed, log=self._log)
        self._log(f"modbus gateway listening on {netguard.listen_where(net, self._server.sockets)}"
                  f" ({'TLS, ' if net.tls else ''}{sockopts.describe(net)})")
        if self._stop.is_set():  # stopped while the listener came up: it found none to close,
            await self.stop()    # and no serial port, MQTT client or polling is started
            return
        try:
            self._tracer = open_tracer(self.mapping, self.trace_dir, self._log, self.status)
            self.status.state = "running"  # the serial side reports its own from here on
            self.status.last_error = ""
            # the port is read per transaction; the link only watches for it going away
            self._serial_task = asyncio.create_task(
                self._link.run(self._link.hold_open), name=f"modbus-serial:{self.mapping.name}")
            self._log_policy()
            log_ignored_settings(self.mapping, self._log)
            # optional edge mode: poll Modbus registers off the bus and publish to MQTT
            # (plain topics, or as a Sparkplug B edge node); writable points take values back
            if self.mapping.mqtt.enabled and self.mapping.modbus_poll.points:
                mq = self.mapping.mqtt
                if mq.payload_format == "sparkplug_b":
                    from .sparkplug_node import SparkplugNode
                    self._sparkplug = SparkplugNode(self.mapping, self._log, functools.partial(
                        self._write_soon, source="Sparkplug DCMD"))
                    self._mqtt = self._sparkplug.publisher()
                else:
                    from .mqtt_pub import MqttPublisher
                    self._mqtt = MqttPublisher(
                        mq, logger=self._log,
                        subscriptions=dict.fromkeys(self._set_points, self._on_set_message),
                        accept_retained=bool(mq.apply_retained_set))
                self._mqtt.connect()
                self._poll_task = asyncio.create_task(
                    self._poll_loop(), name=f"modbus-poll:{self.mapping.name}")
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
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
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # it had died: say why, and still send NDEATH etc. below
                self._log(f"polling had stopped: {e!r}")
            self._poll_task = None
        for t in list(self._write_tasks):
            t.cancel()
        if self._sparkplug is not None:
            sparkplug, self._sparkplug = self._sparkplug, None
            self._mqtt = None
            await sparkplug.aclose()  # NDEATH, then disconnect
        elif self._mqtt is not None:
            mqtt, self._mqtt = self._mqtt, None
            await mqtt.aclose()
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
        self._log("modbus gateway stopped")

    # ---------------- one RTU transaction on the shared bus ----------------
    async def _transaction(self, unit: int, pdu: bytes) -> bytes:
        function = pdu[0] if pdu else 0
        if self._link.down:  # the port is gone (reconnecting): answer now, don't queue
            return modbus.exception_pdu(function, modbus.GATEWAY_PATH_UNAVAILABLE)
        if not self._link.is_open:
            # the first open at start-up is still under way: wait a little for it, outside
            # the bus lock, so queued requests wait together and not one timeout each
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._link.ready.wait(), timeout=self._resp_timeout)
        async with self._bus_lock:
            reader, writer = self._link.reader, self._link.writer
            if reader is None or writer is None:  # not open (yet), or lost meanwhile
                return modbus.exception_pdu(function, modbus.GATEWAY_PATH_UNAVAILABLE)
            # discard any stale bytes left on the bus, then send the RTU request
            with contextlib.suppress(Exception):
                while True:
                    try:
                        junk = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=0.001)
                    except asyncio.TimeoutError:
                        break
                    if not junk:
                        break
            rtu_req = modbus.rtu_wrap(unit, pdu)
            self._feed_monitors(b"\xbb tx " + rtu_req)
            if self._tracer:
                self._tracer.write("net>ser", rtu_req)
            self._feed_traffic(TO_DEVICE, rtu_req)
            try:
                writer.write(rtu_req)
                await writer.drain()
                self.status.bytes_out += len(rtu_req)
                rtu_resp = await self._read_rtu_response(reader)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            self._feed_monitors(b"\xaa rx " + rtu_resp)
            if self._tracer:
                self._tracer.write("ser>net", rtu_resp)
            self._feed_traffic(FROM_DEVICE, rtu_resp)
            self.status.bytes_in += len(rtu_resp)
            try:
                runit, rpdu = modbus.rtu_unwrap(rtu_resp)
            except ValueError:
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            if runit != unit:  # reply from a different slave id -> treat as no answer
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            return rpdu

    async def _read_point(self, p):
        """One poll point's current value off the bus (bool for coils / discrete
        inputs, a number for registers). Raises ValueError / OSError."""
        if p.fn in (modbus.READ_COILS, modbus.READ_DISCRETE_INPUTS):
            rpdu = await self._transaction(p.unit, modbus.read_bits_pdu(p.fn, p.address, 1))
            return modbus.decode_bits(modbus.response_data(rpdu, p.fn), 1)[0]
        req = modbus.read_pdu(p.fn, p.address, modbus.dtype_registers(p.dtype))
        rpdu = await self._transaction(p.unit, req)
        return modbus.decode_value(modbus.response_data(rpdu, p.fn), p.dtype, p.scale)

    def _publish_values(self, values: dict) -> None:
        if self._sparkplug is not None:
            self._sparkplug.update(values)
        elif self._mqtt is not None:
            for name, v in values.items():
                text = ("1" if v else "0") if isinstance(v, bool) else str(v)
                self._mqtt.publish_value(name, text.encode())

    async def _poll_loop(self) -> None:
        """Edge mode: read the configured points off the RTU bus on an interval and
        publish the decoded values (to <base_topic>/<point name>, or as Sparkplug B)."""
        poll = self.mapping.modbus_poll
        while not self._stop.is_set():
            values = {}
            # while the port is gone there is nothing to read (the link logs the outage)
            points = poll.points if not self._link.down else []
            for p in points:
                try:
                    values[p.name] = await self._read_point(p)
                except Exception as e:  # one bad point must not stop the loop
                    self._log(f"poll '{p.name}': {e}")
            if values:
                try:
                    self._publish_values(values)
                    published = ""
                except Exception as e:  # nor a value the publisher can't take
                    published = f"poll: publishing failed: {e!r}"
                    if published != self._publish_error:  # once, not every interval
                        self._log(published)
                self._publish_error = published
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll.interval_s)
                break
            except asyncio.TimeoutError:
                pass

    # ---------------- writes from MQTT (plain <topic>/<point>/set, Sparkplug DCMD) ----
    async def write_point(self, point, value, source: str = "") -> None:
        """Write one value to one writable point, and nothing beyond it: a coil takes a
        bool (FC5), a holding register a number, encoded as the point's own type (FC6 for
        a 16-bit type, FC16 for the two registers of a 32-bit one). Check the slave's
        reply, then read the point back and publish it. A read-only mapping writes nothing
        (checked here, at write time, for every command path). The mapping's log names
        the write and where it came from (`source`)."""
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
            pdu = modbus.write_single_coil_pdu(point.address, value)
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{point.name!r} takes a number, not {shown(value)}")
            regs = modbus.encode_value(value, point.dtype, point.scale or 1.0)
            pdu = (modbus.write_single_register_pdu(point.address, regs[0]) if len(regs) == 1
                   else modbus.write_multiple_registers_pdu(point.address, regs))
        rpdu = await self._transaction(point.unit, pdu)
        modbus.check_write_response(rpdu, pdu)
        via = f", from {source}" if source else ""
        self._log(f"wrote '{point.name}' = {value!r} (unit {point.unit}, FC{pdu[0]}{via})")
        with contextlib.suppress(Exception):
            self._publish_values({point.name: await self._read_point(point)})

    def _write_soon(self, point, value, done=None, source: str = "") -> None:
        """Schedule write_point from an MQTT callback (already on the loop); ``done``
        gets None or the error message."""
        async def run():
            err = None
            try:
                await self.write_point(point, value, source)
            except Exception as e:
                err = str(e) or type(e).__name__
                self._log(f"write '{point.name}' = {shown(value)} failed: {err}")
            if done is not None:
                done(err)
        task = asyncio.create_task(run(), name=f"modbus-write:{point.name}")
        self._write_tasks.add(task)
        task.add_done_callback(self._write_tasks.discard)

    def _on_set_message(self, topic: str, payload: bytes) -> None:
        """Plain MQTT: a value for one writable point on its <base_topic>/<point>/set; the
        outcome goes to .../set/result (ok, or error: …)."""
        point = self._set_points.get(topic)
        if point is None or self._mqtt is None:
            return
        result = topic + "/result"
        if self.mapping.network.read_only:
            self._log(f"write '{point.name}' from MQTT refused: the mapping is read-only")
            self._mqtt.publish_raw(result, b"error: mapping is read-only")
            return
        try:
            value = parse_write_payload(payload, point)
        except ValueError as e:
            self._mqtt.publish_raw(result, f"error: {e}".encode())
            return
        mqtt = self._mqtt
        self._write_soon(point, value, lambda err: mqtt.publish_raw(
            result, b"ok" if err is None else f"error: {err}".encode()), source=f"MQTT {topic}")

    async def _read_rtu_response(self, reader) -> bytes:
        # wait for the first byte(s) within the response timeout
        first = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=self._resp_timeout)
        if not first:
            raise ConnectionError("serial EOF")
        buf = bytearray(first)
        # then drain until an inter-frame gap of silence (RTU framing is timing-based)
        while True:
            try:
                more = await asyncio.wait_for(reader.read(READ_CHUNK), timeout=self._gap)
            except asyncio.TimeoutError:
                break
            if not more:
                break
            buf += more
        return bytes(buf)

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
        if self._stop.is_set():  # accepted while the gateway stops
            writer.close()
            return
        peer_ip, peer_port = netguard.peer_address(writer)
        peer = netguard.client_label(peer_ip, peer_port)
        net = self.mapping.network

        # allow-list, then max connections (a priority master / kick_old_user makes room)
        admission = self._access.admit(peer_ip, peer, self._clients, self._refusal_log)
        if not admission.admitted:
            with contextlib.suppress(Exception):
                writer.close()
            return

        if err := sockopts.apply_keepalive(writer, net):
            self._log(f"TCP keepalive not enabled for {peer}: {err}")
        master = _Master(writer, peer, priority=admission.priority)
        self._clients.add(master)
        self._refresh_clients()
        tag = (" [priority]" if master.priority else "") + tls.client_certificate(writer)
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
            held = fmt_duration(time.monotonic() - master.connected_at)
            refused = f", {master.refused} request(s) refused (read-only)" if master.refused else ""
            self._log(f"modbus master disconnected: {peer} after {held} — {reason}{refused} "
                      f"({len(self._clients)} total)")


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
