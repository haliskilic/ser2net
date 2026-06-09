"""Modbus RTU<->TCP gateway runner.

A drop-in alternative to MappingRunner, selected when a mapping's network
protocol is ``modbus``. It listens as a Modbus/TCP server: each connected master
sends Modbus/TCP requests, which the gateway converts to Modbus/RTU, forwards on
the shared serial bus (one transaction at a time, since RTU is a single-master
bus), reads the slave's RTU reply, and returns it to that master as Modbus/TCP —
echoing the transaction id so concurrent masters don't get crossed wires.

Shares the lifecycle shape (and the H3/H4 fixes) of MappingRunner: a supervised,
auto-reconnecting serial endpoint plus an asyncio TCP server, both torn down
cleanly on stop().
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from typing import Optional

from ..config import MappingConfig, normalize_cidr
from . import modbus, serial_io
from .bridge import RunnerStatus, fmt_duration

READ_CHUNK = 512


def _parse_nets(cidrs):
    nets = []
    for c in cidrs:
        with contextlib.suppress(ValueError):
            nets.append(ipaddress.ip_network(normalize_cidr(c), strict=False))
    return nets


def _ip_in_nets(ip: str, nets) -> bool:
    if not nets:
        return False
    with contextlib.suppress(ValueError):
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        return any(addr in net for net in nets)
    return False


class ModbusGatewayRunner:
    def __init__(self, mapping: MappingConfig, logger=None):
        self.mapping = mapping
        self.status = RunnerStatus(state="stopped")
        self.log = logger
        self.serial_instance = None
        self._sreader: Optional[asyncio.StreamReader] = None
        self._swriter: Optional[asyncio.StreamWriter] = None
        self._serial_task: Optional[asyncio.Task] = None
        self._server = None
        self._stop = asyncio.Event()
        self._serial_ready = asyncio.Event()
        self._bus_lock = asyncio.Lock()      # RTU is single-master: one txn at a time
        self._clients: set = set()
        self._monitors: set = set()           # browser console observers (read the bus)
        self._mqtt = None                      # optional MQTT publisher for register polling
        self._poll_task: Optional[asyncio.Task] = None
        self._allowed = _parse_nets(mapping.network.allowed_client_ips)
        # inter-frame gap used to detect the end of an RTU reply (timing-based framing)
        baud = max(1, mapping.serial.baudrate)
        self._gap = max(0.01, 3.5 * 11 / baud)
        self._resp_timeout = mapping.options.modbus_response_timeout_s or 1.0

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

    async def serial_write(self, data: bytes) -> None:
        # the modbus console is observe-only (injecting raw bytes would corrupt
        # the request/response framing on the shared bus)
        return

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self._stop.clear()
        net = self.mapping.network
        self._serial_task = asyncio.create_task(
            self._serial_supervisor(), name=f"modbus-serial:{self.mapping.name}")
        try:
            self._server = await asyncio.start_server(
                self._on_client, host=net.bind_ip, port=net.port, ssl=self._server_ssl())
            self._log(f"modbus gateway listening on {net.bind_ip}:{net.port}"
                      f"{' (TLS)' if net.tls else ''}")
        except BaseException:
            await self.stop()
            raise
        # optional edge mode: poll Modbus registers off the bus and publish to MQTT
        if self.mapping.mqtt.enabled and self.mapping.modbus_poll.points:
            from .mqtt_pub import MqttPublisher
            self._mqtt = MqttPublisher(self.mapping.mqtt, logger=self._log)
            self._mqtt.connect()
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name=f"modbus-poll:{self.mapping.name}")
        self.status.state = "running"

    async def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        for c in list(self._clients):
            with contextlib.suppress(Exception):
                c.transport.abort()
        for mon in list(self._monitors):
            mon.close()
        self._monitors.clear()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        if self._mqtt is not None:
            self._mqtt.close()
            self._mqtt = None
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
            self._server = None
        if self._serial_task:
            self._serial_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._serial_task
            self._serial_task = None
        await self._close_serial()
        self.status.state = "stopped"
        self.status.client_count = 0
        self.status.clients = []
        self._log("modbus gateway stopped")

    def _server_ssl(self):
        net = self.mapping.network
        if not net.tls:
            return None
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(net.tls_cert, net.tls_key)
        return ctx

    # ---------------- serial side ----------------
    async def _close_serial(self) -> None:
        self._serial_ready.clear()
        w = self._swriter
        self._sreader = self._swriter = None
        self.serial_instance = None
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()
                await w.wait_closed()

    async def _serial_supervisor(self) -> None:
        backoff, attempt = 0.5, 0
        while not self._stop.is_set():
            try:
                reader, writer, ser, device = await serial_io.open_serial(self.mapping.serial)
                self._sreader, self._swriter, self.serial_instance = reader, writer, ser
                self.status.device = device
                self.status.last_error = ""
                self.status.state = "running"
                self._serial_ready.set()
                backoff, attempt = 0.5, 0
                self._log(f"serial open: {device} @ {self.mapping.serial.compact()}")
                await self._stop.wait()       # hold the port open; reads happen per-transaction
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status.last_error = str(e)
                self.status.state = "device-missing" if attempt == 0 else "reconnecting"
                self._log(f"serial error: {e}")
            finally:
                await self._close_serial()
            if self._stop.is_set():
                break
            attempt += 1
            self.status.reconnects += 1
            self.status.state = "reconnecting"
            delay = min(backoff, 10.0) + (attempt % 5) * 0.1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 10.0)

    # ---------------- one RTU transaction on the shared bus ----------------
    async def _transaction(self, unit: int, pdu: bytes) -> bytes:
        function = pdu[0] if pdu else 0
        async with self._bus_lock:
            if not self._serial_ready.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._serial_ready.wait(), timeout=self._resp_timeout)
            reader, writer = self._sreader, self._swriter
            if reader is None or writer is None:
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
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
            try:
                writer.write(rtu_req)
                await writer.drain()
                self.status.bytes_out += len(rtu_req)
                rtu_resp = await self._read_rtu_response(reader)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            self._feed_monitors(b"\xaa rx " + rtu_resp)
            self.status.bytes_in += len(rtu_resp)
            try:
                runit, rpdu = modbus.rtu_unwrap(rtu_resp)
            except ValueError:
                return modbus.exception_pdu(function, modbus.GATEWAY_TARGET_FAILED)
            return rpdu

    async def _poll_loop(self) -> None:
        """Edge mode: read configured registers off the RTU bus on an interval and
        publish each decoded value to <base_topic>/<point name> over MQTT."""
        poll = self.mapping.modbus_poll
        while not self._stop.is_set():
            for p in poll.points:
                try:
                    req = modbus.read_pdu(p.fn, p.address, modbus.dtype_registers(p.dtype))
                    rpdu = await self._transaction(p.unit, req)
                    data = modbus.response_data(rpdu, p.fn)
                    value = modbus.decode_value(data, p.dtype, p.scale)
                except Exception as e:  # one bad point must not stop the loop
                    self._log(f"poll '{p.name}': {e}")
                    continue
                if self._mqtt is not None:
                    self._mqtt.publish_value(p.name, str(value).encode())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll.interval_s)
                break
            except asyncio.TimeoutError:
                pass

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
    def _refresh_clients(self) -> None:
        self.status.client_count = len(self._clients)
        self.status.clients = sorted(
            ({"peer": c.peer, "connected_at": c.connected_at, "priority": False}
             for c in self._clients),
            key=lambda d: d["connected_at"],
        )

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername") or ("?", 0)
        peer_ip = str(peername[0]) if isinstance(peername, tuple) else str(peername)
        peer = f"{peer_ip}:{peername[1]}" if isinstance(peername, tuple) and len(peername) > 1 else peer_ip
        net = self.mapping.network

        if self._allowed and not _ip_in_nets(peer_ip, self._allowed):
            self._log(f"rejected {peer}: not in allowed list")
            writer.close()
            return
        if len(self._clients) >= net.max_connections:
            self._log(f"rejected {peer}: max connections reached")
            with contextlib.suppress(Exception):
                writer.close()
            return

        writer.connected_at = time.time()
        writer.peer = peer
        self._clients.add(writer)
        self._refresh_clients()
        self._log(f"modbus master connected: {peer} ({len(self._clients)} total)")
        buf = bytearray()
        try:
            while not self._stop.is_set():
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                buf += data
                while True:
                    try:
                        frame = modbus.take_tcp_adu(buf)
                    except ValueError as e:
                        self._log(f"bad Modbus/TCP framing from {peer}: {e}")
                        return
                    if frame is None:
                        break
                    txn, unit, pdu = frame
                    rpdu = await self._transaction(unit, pdu)
                    writer.write(modbus.build_tcp_adu(txn, unit, rpdu))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            self._refresh_clients()
            with contextlib.suppress(Exception):
                writer.close()
            held = fmt_duration(time.time() - getattr(writer, "connected_at", time.time()))
            self._log(f"modbus master disconnected: {peer} after {held} ({len(self._clients)} total)")
