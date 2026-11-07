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
import ipaddress
import json
import socket
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import netinfo
from ..config import parse_cluster_peer, peer_host

BEACON_INTERVAL = 5.0   # seconds between outbound beacons
PEER_TTL = 20.0         # a peer is dropped if not heard from within this window
_LOOPBACK = ("0.0.0.0", "::", "127.0.0.1", "localhost", "")


def key_bytes(key: str) -> bytes:
    """The cluster key as every check uses it - the beacon HMAC and the credential sent
    with peer requests: Unicode NFKC-normalised, then UTF-8. NFKC makes the same key
    typed or pasted on different systems match (a composed or decomposed letter, a
    no-break space); an ASCII key is unchanged, so its bytes are those every release
    has used."""
    return unicodedata.normalize("NFKC", key or "").encode("utf-8", "surrogatepass")


def key_header(key: str) -> str:
    """The X-Cluster-Key header value for *key*: key_bytes as latin-1 text, which
    http.client sends as exactly those bytes (an ASCII key goes out unchanged)."""
    return key_bytes(key).decode("latin-1")


def key_header_matches(key: str, header: str | None) -> bool:
    """Whether an X-Cluster-Key header carries *key*. The server decodes header bytes
    as latin-1, so encoding back gives the bytes that were sent; they are compared with
    key_bytes(key) as bytes (a text comparison fails on a non-ASCII key)."""
    want = key_bytes(key)
    try:
        got = (header or "").encode("latin-1")
    except UnicodeEncodeError:  # not text from a header
        return False
    return bool(want and got) and hmac.compare_digest(got, want)


def _sign(key: str, body: str) -> str:
    return hmac.new(key_bytes(key), body.encode("utf-8"), hashlib.sha256).hexdigest()


def peer_port(value) -> int | None:
    """value as a TCP port (1..65535), else None."""
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def peer_url(scheme: str, host: str, port, path: str = "/", query: dict | None = None) -> str:
    """The URL of *path* on a peer's web UI. ValueError unless the scheme is http or
    https, the host an IP address or host name and the port 1..65535, so no value can
    point the URL (and the key sent with it) at another host. An IPv6 host is put in
    brackets and the query is encoded."""
    h, p = peer_host(host), peer_port(port)
    if scheme not in ("http", "https") or h is None or p is None or not path.startswith("/"):
        raise ValueError(f"not a cluster peer address: {scheme!r}, {host!r}, {port!r}")
    url = f"{scheme}://[{h}]:{p}{path}" if ":" in h else f"{scheme}://{h}:{p}{path}"
    return f"{url}?{urllib.parse.urlencode(query)}" if query else url


def peer_addr(host, port) -> str:
    """host:port as the UI and the logs show it (an IPv6 address in brackets)."""
    return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"


def _target_key(t: dict) -> tuple:
    return t.get("scheme"), t.get("ip"), t.get("port")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # following it would send the key on to wherever it points


def _peer_opener() -> urllib.request.OpenerDirector:
    """The urllib opener for peer requests. Always a direct connection: a proxy from
    HTTP(S)_PROXY / ALL_PROXY (or the Windows proxy settings), set so that webhooks or
    SSO can reach the Internet, would be handed the key - and NO_PROXY can't name a
    whole LAN. Redirects are not followed. TLS is not verified: cluster trust is the
    shared key, not the certificate (self-signed is normal)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=ctx), _NoRedirect())


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
        self._opener = _peer_opener()
        self._problems: dict[str, str] = {}  # peer host:port -> why its last request failed
        self._running = False
        self.discovery_error = ""           # non-empty when UDP discovery couldn't start

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
        can be called directly from tests to inject a peer. Any host on the LAN can
        send here, so anything malformed is dropped without an error."""
        key = self.cluster.key
        if not key:
            return
        try:
            env = json.loads(data.decode("utf-8"))
            body, sig = env["d"], env["s"]
            if not (isinstance(body, str) and isinstance(sig, str) and sig.isascii()):
                return
            if not hmac.compare_digest(_sign(key, body), sig):
                return  # different key (or tampered) — not part of our cluster
            p = json.loads(body)
        except (ValueError, KeyError, AttributeError, TypeError, RecursionError):
            return  # (a UnicodeError is a ValueError)
        if not isinstance(p, dict):
            return
        pid = str(p.get("id", ""))
        if not pid or pid == self.config.instance_id:
            return  # ignore our own beacon
        ip, port = self._beacon_address(p.get("ip"), addr), peer_port(p.get("port"))
        if ip is None or port is None:
            return  # no web-UI address to reach it at
        scheme = p.get("scheme") if p.get("scheme") in ("http", "https") else "http"
        self._peers[pid] = {
            "id": pid, "name": str(p.get("name", "?")), "ip": ip,
            "port": port, "scheme": scheme, "last_seen": time.time(),
        }

    @staticmethod
    def _beacon_address(advertised, addr) -> str | None:
        """Where a beacon's sender is reached: the IP address it advertises or, when that
        is no usable address (loopback or wildcard: the node could not tell its LAN IP),
        the packet's source address. Never a host name from the beacon."""
        try:
            ip = ipaddress.ip_address(str(advertised or "").strip())
        except ValueError:
            ip = None
        if ip is not None and not (ip.is_loopback or ip.is_unspecified):
            return str(ip)
        return str(addr[0]) if addr else None   # fall back to the packet's source

    def peers(self) -> list[dict]:
        """Live peers (TTL-pruned), sorted by name then ip."""
        now = time.time()
        live = [p for p in self._peers.values() if now - p["last_seen"] <= PEER_TTL]
        self._peers = {p["id"]: p for p in live}
        return sorted(live, key=lambda p: (p["name"].lower(), p["ip"]))

    def manual_targets(self) -> list[dict]:
        """Configured manual peers (for routed/L3 networks broadcast can't reach),
        parsed into fetch targets. Bad entries are skipped (validate() rejects them
        at save time, so this only guards a hand-edited config)."""
        out = []
        for entry in self.cluster.peers:
            try:
                scheme, host, port = parse_cluster_peer(entry)
            except Exception:
                continue
            out.append({"scheme": scheme, "ip": peer_host(host) or host, "port": port,
                        "source": "manual"})
        return out

    def all_targets(self) -> list[dict]:
        """Auto-discovered peers + manual peers, deduped by (ip, port). When a manual
        peer is also auto-discovered the richer auto entry wins."""
        seen, targets = set(), []
        for p in self.peers():
            seen.add((p["ip"], p["port"]))
            targets.append({**p, "source": "auto"})
        for m in self.manual_targets():
            if (m["ip"], m["port"]) not in seen:
                seen.add((m["ip"], m["port"]))
                targets.append(m)
        return targets

    def find_target(self, host, port) -> dict | None:
        """The discovered or configured peer at host:port (as the fleet view shows it),
        else None. Browser-initiated peer calls are resolved through this, so their
        address and scheme — where the shared key goes — never come from the request."""
        h, p = peer_host(host), peer_port(port)
        if h is None or p is None:
            return None
        return next((t for t in self.all_targets() if t["ip"] == h and t["port"] == p), None)

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
            self.discovery_error = f"UDP discovery port {port} unavailable: {e}"
            self.log(f"cluster: could not bind UDP {port}: {e} — discovery disabled "
                     "(manual peers still work)")
            return
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _BeaconProtocol(self.handle_datagram), sock=sock)
        except OSError as e:
            sock.close()  # endpoint creation owns the socket only on success
            self.discovery_error = f"UDP listener on {port} failed: {e}"
            self.log(f"cluster: could not start UDP listener on {port}: {e} — discovery disabled")
            return
        self.discovery_error = ""
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
        self._problems.clear()

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

    # ----- peer HTTP (all key-guarded; trust is the shared key) -----
    def _auth_headers(self) -> dict[str, str]:
        """The credential sent with every peer request: the shared key itself, as every
        release since 2.6 sends and checks it (byte-identical for an ASCII key)."""
        return {"X-Cluster-Key": key_header(self.cluster.key)}

    async def get_peer(self, target: dict, path: str, query: dict | None = None,
                       timeout: float = 3.0) -> dict | None:
        """GET a key-guarded path of a known peer: its JSON answer, or None (no answer,
        refused, not a ser2net node)."""
        status, data = await self._request(target, "GET", path, query, None, timeout)
        return data if 200 <= status < 300 else None

    async def post_peer(self, target: dict, path: str, fields, timeout: float = 6.0) -> dict | None:
        """POST a form to a key-guarded path of a known peer: its JSON answer — also a
        400 refusal, whose "error" is the peer's own message (e.g. why it can't save
        a mapping) — or None."""
        body = urllib.parse.urlencode(fields).encode()
        status, data = await self._request(target, "POST", path, None, body, timeout)
        return data if 200 <= status < 300 or status == 400 else None

    async def fetch_peer(self, peer: dict, timeout: float = 2.5) -> dict | None:
        return await self.get_peer(peer, "/api/cluster/local", timeout=timeout)

    async def control_peer(self, target: dict, mapping_id: str, action: str,
                           timeout: float = 4.0) -> dict | None:
        return await self.post_peer(target, "/api/cluster/control",
                                    {"mapping_id": mapping_id, "action": action}, timeout)

    async def _request(self, target: dict, method: str, path: str, query: dict | None,
                       body: bytes | None, timeout: float) -> tuple[int, dict | None]:
        """One key-guarded request: (HTTP status, 0 when nothing answered; the reply's
        JSON object or None). Made only to a discovered or configured peer, at an
        address peer_url accepts, so the key never goes to an address from a request."""
        if not any(_target_key(t) == _target_key(target) for t in self.all_targets()):
            return 0, None
        try:
            url = peer_url(target["scheme"], target["ip"], target["port"], path, query)
        except ValueError:
            self._note(target, "not a valid peer address")
            return 0, None
        status, data, problem = await asyncio.to_thread(self._http, method, url, body, timeout)
        self._note(target, problem)
        return status, data

    def _http(self, method: str, url: str, body: bytes | None,
              timeout: float) -> tuple[int, dict | None, str]:
        """Make one peer request (in a worker thread): (status, JSON object or None, what
        went wrong - "" when the peer answered as a cluster node does). Never raises."""
        headers = self._auth_headers()
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                data = _json_object(r)
                return r.status, data, "" if data is not None else "not a ser2net cluster reply"
        except urllib.error.HTTPError as e:
            with contextlib.closing(e):
                data = _json_object(e)
            if e.code in (401, 403):
                return e.code, None, (f"HTTP {e.code}: it refused this node's cluster key "
                                      "(or its cluster is off)")
            if 300 <= e.code < 400:
                return e.code, None, f"HTTP {e.code}: redirects are not followed"
            # a 4xx with a JSON answer is a peer refusing one request (e.g. a bad edit)
            return e.code, data, "" if data is not None and e.code < 500 else f"HTTP {e.code}"
        except Exception as e:  # no connection, timeout, TLS ...
            return 0, None, str(getattr(e, "reason", "") or e) or type(e).__name__

    def _note(self, target: dict, problem: str) -> None:
        """Log why requests to a peer fail - once per change, not on every refresh: the
        first failure, a different one, and the first success after them."""
        addr = peer_addr(target["ip"], target["port"])
        if problem == self._problems.get(addr, ""):
            return
        if problem:
            if len(self._problems) >= 256:  # peers come and go; keep this bounded
                self._problems.clear()
            self._problems[addr] = problem
            self.log(f"cluster: request to peer {addr} failed: {problem}")
        else:
            del self._problems[addr]
            self.log(f"cluster: peer {addr} answers again")


def _json_object(resp) -> dict | None:
    """A peer reply's JSON object, or None."""
    try:
        data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None
