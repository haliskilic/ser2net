"""LAN cluster discovery — nodes find each other and share a read-only view.

Each ser2net instance periodically broadcasts a small UDP *beacon* (its node id,
hostname, and the IP:port + scheme of its web UI) signed with HMAC-SHA256 over
the shared cluster key. Every instance listens on the same UDP port and records
beacons whose signature verifies with its own key — so only nodes configured with
the *same* key trust each other. Peers not heard from within ``PEER_TTL`` seconds
expire.

Deliberately **not** mDNS/zeroconf and no extra dependencies: just an asyncio
DatagramProtocol + a periodic broadcast task. This module only does *discovery*
(who is out there and how to reach their web UI). Aggregating a peer's mappings
is an HTTP GET to that peer's ``/api/cluster/local`` (guarded by the shared key);
``fetch_peer`` performs it off the event loop. The browser only ever talks to the
node it logged into — that node fans out to its peers server-side.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import socket
import ssl
import time
import urllib.request

from . import netinfo

BEACON_INTERVAL = 5.0   # seconds between outbound beacons
PEER_TTL = 20.0         # a peer is dropped if not heard from within this window
_LOOPBACK = ("0.0.0.0", "::", "127.0.0.1", "localhost", "")


def _sign(key: str, body: str) -> str:
    return hmac.new(key.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram):
        self._on_datagram = on_datagram

    def datagram_received(self, data, addr):
        self._on_datagram(data, addr)

    def error_received(self, exc):  # ICMP port-unreachable etc. — ignore
        pass


class ClusterDiscovery:
    """Owns the UDP discovery socket + beacon loop + live peer table. Reads the
    cluster config live from AppConfig so a settings change is picked up on the
    next restart (start/stop are driven by AppState's engine lifecycle)."""

    def __init__(self, config, logger):
        self.config = config            # AppConfig (cluster + admin_ui + instance_id, read live)
        self.log = logger
        self._transport = None
        self._beacon_task: asyncio.Task | None = None
        self._peers: dict[str, dict] = {}   # id -> {id,name,ip,port,scheme,last_seen}
        self._running = False

    # ----- config helpers -----
    @property
    def cluster(self):
        return self.config.cluster

    def advertised(self) -> tuple[str, int, str]:
        """(ip, port, scheme) other nodes use to reach THIS node's web UI."""
        c = self.cluster
        ui = self.config.admin_ui
        ip = c.advertise_ip.strip()
        if not ip:
            bind = (ui.bind_ip or "").strip()
            if bind not in _LOOPBACK:
                ip = bind
            else:
                ip = netinfo.primary_lan_ip() or "127.0.0.1"
        scheme = "https" if ui.tls_enabled else "http"
        return ip, int(ui.port), scheme

    # ----- beacon (de)serialization -----
    def _make_beacon(self) -> bytes:
        ip, port, scheme = self.advertised()
        payload = {"v": 1, "id": self.config.instance_id, "name": socket.gethostname(),
                   "ip": ip, "port": port, "scheme": scheme, "t": int(time.time())}
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return json.dumps({"d": body, "s": _sign(self.cluster.key, body)}).encode("utf-8")

    def handle_datagram(self, data: bytes, addr) -> None:
        """Verify a received beacon and record/refresh the peer. Pure (no I/O) so it
        can be called directly from tests to inject a peer."""
        key = self.cluster.key
        if not key:
            return
        try:
            env = json.loads(data.decode("utf-8"))
            body, sig = env["d"], env["s"]
        except (ValueError, KeyError, AttributeError, TypeError):
            return
        if not hmac.compare_digest(_sign(key, body), str(sig)):
            return  # different key (or tampered) — not part of our cluster
        try:
            p = json.loads(body)
        except ValueError:
            return
        pid = str(p.get("id", ""))
        if not pid or pid == self.config.instance_id:
            return  # ignore our own beacon
        ip = str(p.get("ip", "")).strip()
        if ip in _LOOPBACK and addr:
            ip = addr[0]   # advertised IP unusable — fall back to the packet's source
        scheme = p.get("scheme") if p.get("scheme") in ("http", "https") else "http"
        self._peers[pid] = {
            "id": pid, "name": str(p.get("name", "?")), "ip": ip,
            "port": int(p.get("port", 0) or 0), "scheme": scheme, "last_seen": time.time(),
        }

    def peers(self) -> list[dict]:
        """Live peers (TTL-pruned), sorted by name then ip."""
        now = time.time()
        live = [p for p in self._peers.values() if now - p["last_seen"] <= PEER_TTL]
        self._peers = {p["id"]: p for p in live}
        return sorted(live, key=lambda p: (p["name"].lower(), p["ip"]))

    # ----- lifecycle -----
    async def start(self) -> None:
        if self._running or not self.cluster.active:
            return
        loop = asyncio.get_running_loop()
        port = int(self.cluster.discovery_port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(AttributeError, OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", port))
        except OSError as e:
            sock.close()
            self.log(f"cluster: could not bind UDP {port}: {e} — discovery disabled")
            return
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _BeaconProtocol(self.handle_datagram), sock=sock)
        self._running = True
        self._beacon_task = asyncio.create_task(self._beacon_loop(), name="cluster-beacon")
        self.log(f"cluster: discovery on UDP {port} as '{socket.gethostname()}' "
                 f"({self.config.instance_id[:8]})")

    async def stop(self) -> None:
        self._running = False
        if self._beacon_task is not None:
            self._beacon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beacon_task
            self._beacon_task = None
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
            self._transport = None
        self._peers.clear()

    async def _beacon_loop(self) -> None:
        while True:
            try:
                self._broadcast()
            except Exception as e:  # never let the loop die on a transient send error
                self.log(f"cluster: beacon send failed: {e}")
            await asyncio.sleep(BEACON_INTERVAL)

    def _broadcast(self) -> None:
        if self._transport is None:
            return
        msg = self._make_beacon()
        port = int(self.cluster.discovery_port)
        for addr in self._broadcast_addrs():
            with contextlib.suppress(OSError):
                self._transport.sendto(msg, (addr, port))

    @staticmethod
    def _broadcast_addrs() -> list[str]:
        """Global broadcast plus each NIC's directed broadcast (some networks only
        deliver the subnet-directed form). Best-effort; psutil is optional."""
        addrs = ["255.255.255.255"]
        try:
            import psutil
            for nic in psutil.net_if_addrs().values():
                for a in nic:
                    if getattr(a, "family", None) == socket.AF_INET and getattr(a, "broadcast", None):
                        addrs.append(a.broadcast)
        except Exception:
            pass
        seen, out = set(), []
        for a in addrs:
            if a and a not in seen:
                seen.add(a)
                out.append(a)
        return out

    # ----- peer aggregation (HTTP) -----
    async def fetch_peer(self, peer: dict, timeout: float = 2.5) -> dict | None:
        url = f"{peer['scheme']}://{peer['ip']}:{peer['port']}/api/cluster/local"
        return await asyncio.to_thread(self._http_get_json, url, timeout)

    def _http_get_json(self, url: str, timeout: float) -> dict | None:
        req = urllib.request.Request(url, headers={"X-Cluster-Key": self.cluster.key})
        ctx = None
        if url.startswith("https"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # cluster trust is the shared key, not the cert
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None
