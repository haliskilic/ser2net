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
``fetch_peer`` performs it off the event loop, on a small thread pool of its own, and
shares each answer between all open dashboards for a few seconds. The browser only
ever talks to the node it logged into — that node fans out to its peers server-side.

Peer requests don't carry the key itself: each one is signed (header
``X-Cluster-Auth``) with a key derived from the cluster key, over its method, path and
query, a digest of its body, the sending and the receiving node's ids, a time and a
random nonce. The receiver checks the time against its own clock - the sender corrects
for the receiver's clock, learned from its signed beacons and replies, so the clocks
need not agree - remembers every nonce while its request could still pass, and signs
its reply for that request. So a request seen on the network can't be sent again, to
this node or to another one, and a reply can't be forged or altered. Releases up to
2.8.0 only know ``X-Cluster-Key``, the raw key: while ``legacy_auth`` is on, a node
accepts it, and sends it only to such peers - one whose signed beacon says it is
older, at the address that beacon advertises (never at the address a beacon came from:
beacons can be replayed from anywhere), or a manual peer that refused a signed request
the way older releases do (never one that has answered a signed request since this
node started). Beacons carry the protocol version, a random id of the sending process
(two processes with one node id are copies of one installation) and the sender's own
addresses; a beacon not newer than the last one from its sender is dropped.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import functools
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
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
FETCH_CACHE_S = 4.0     # a peer's fleet-view answer is reused this long (by every tab)
HTTP_WORKERS = 8        # threads for peer requests: their own pool, so slow or
                        # unreachable peers never hold up sign-ins, saves or port scans
MAX_REPLY_BYTES = 8 * 1024 * 1024   # a larger peer reply is refused
_LOOPBACK = ("0.0.0.0", "::", "127.0.0.1", "localhost", "")

AUTH_HEADER = "X-Cluster-Auth"   # a signed peer request or reply (after 2.8.0)
KEY_HEADER = "X-Cluster-Key"     # the raw key: all that releases up to 2.8.0 send and check
AUTH_SCHEME = "v2"
PROTOCOL = 2                     # a beacon's "v": 2 = the node signs its requests (older: 1)
CLOCK_WINDOW_S = 60.0            # a signed request's time may differ this much from ours
NONCES_KEPT = 10000              # signed requests remembered against a replay, at most
KEY_OVERLAP_S = 7 * 86400        # a replaced key stays accepted this long (a planned change)
BOOT_SKEW_S = 300.0              # a new run of a node may be dated this much before its last beacon
NOTE_TTL = 30.0                  # how long a node seen with another key / this node's id is shown
LEGACY_SEEN_S = 600.0            # how long a node that sent the raw key counts as using it
_FIELD = re.compile(r"[A-Za-z0-9_.-]{0,128}")
_NODE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_TIME = re.compile(r"\d{1,12}(\.\d{1,6})?")
_NONCE = re.compile(r"[0-9a-f]{16,64}")

# Why a peer-facing request was refused (PeerAuth.reason), as the log puts it.
REFUSALS = {
    "off": "the cluster is off on this node",
    "missing": "no cluster credentials",
    "malformed": "a malformed X-Cluster-Auth header",
    "key": "a wrong cluster key",
    "signature": "a signature that doesn't match this node's cluster key (another key?)",
    "legacy": "the cluster key unsigned (a node running 2.8.0 or older), and legacy authentication is off",
    "replay": "a signed request that was already used",
}

# Why requests to a peer fail (the fleet view explains each code), as the log puts it.
_PROBLEMS = {
    "off": "the cluster is off on this node",
    "key": "it refused this node's cluster key",
    "cluster-off": "its cluster is off",
    "legacy-off": ("it is a node running 2.8.0 or older (or answers like one), which only "
                   "knows the unsigned key, and legacy authentication is off on this node"),
    "manual-only": ("it is a node running 2.8.0 or older that doesn't advertise its LAN "
                    "address; the unsigned key only goes to an address a node advertises or "
                    "to a manual peer, so add it as a manual peer to manage it from here"),
    "unsigned": "its reply is not signed with the cluster key",
    "clock": "its clock and this node's could not be matched",
}


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


def auth_key(key: str) -> bytes:
    """The key peer requests and their replies are signed with. Derived from key_bytes,
    so it is neither the key beacons are signed with nor what older releases send."""
    return hmac.new(key_bytes(key), b"ser2net cluster auth v2", hashlib.sha256).digest()


def wire_id(instance_id) -> str:
    """A node id as X-Cluster-Auth carries it: the id itself (a generated one is hex),
    or a digest of an odd hand-edited one."""
    s = str(instance_id or "")
    if _NODE_ID.fullmatch(s):
        return s
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def canonical_target(path: str, query: str) -> str:
    """A request's target as it is signed: the decoded path, plus the query decoded and
    encoded again - so what the sender built and what the receiving server sees match."""
    q = urllib.parse.urlencode(urllib.parse.parse_qsl(query or "", keep_blank_values=True))
    return f"{path}?{q}" if q else path


def _mac(key: str, *fields) -> str:
    msg = "\n".join(str(f) for f in fields).encode("utf-8", "surrogatepass")
    return hmac.new(auth_key(key), msg, hashlib.sha256).hexdigest()


def request_mac(key: str, method: str, target: str, body: bytes | None, sender: str,
                recipient: str, ts: str, nonce: str) -> str:
    """The signature ("sig") of a peer request."""
    return _mac(key, "ser2net-cluster-request", method.upper(), target,
                hashlib.sha256(body or b"").hexdigest(), sender, recipient, ts, nonce)


def reply_mac(key: str, nonce: str, status: int, body: bytes | None, responder: str,
              now: str) -> str:
    """The signature of the reply to the signed request *nonce*."""
    return _mac(key, "ser2net-cluster-reply", nonce, int(status),
                hashlib.sha256(body or b"").hexdigest(), responder, now)


def format_auth(params: dict) -> str:
    return f"{AUTH_SCHEME} " + ",".join(f"{k}={v}" for k, v in params.items())


def parse_auth(value) -> dict | None:
    """The parameters of an X-Cluster-Auth value ("v2 k=v,k=v"; a bare "v2" only says
    that its sender understands signed requests), or None."""
    scheme, _sep, rest = str(value or "").strip().partition(" ")
    if scheme != AUTH_SCHEME:
        return None
    out: dict[str, str] = {}
    for part in rest.split(",") if rest.strip() else ():
        k, eq, v = part.strip().partition("=")
        if not eq or k in out or not k or not _FIELD.fullmatch(k) or not _FIELD.fullmatch(v):
            return None
        out[k] = v
    return out


def _remember(d: dict, k, v, limit: int) -> None:
    """d[k] = v as d's newest entry; the oldest ones go past *limit*."""
    d.pop(k, None)
    d[k] = v
    while len(d) > limit:
        d.pop(next(iter(d)))


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
    point the URL (and the request sent with it) at another host. An IPv6 host is put
    in brackets and the query is encoded."""
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
        return None  # following it would send the request on to wherever it points


def _peer_opener() -> urllib.request.OpenerDirector:
    """The urllib opener for peer requests. Always a direct connection: a proxy from
    HTTP(S)_PROXY / ALL_PROXY (or the Windows proxy settings), set so that webhooks or
    SSO can reach the Internet, would see every peer request (and an older peer's raw
    key) - and NO_PROXY can't name a whole LAN. Redirects are not followed. TLS is not
    verified: cluster trust is the shared key (requests and replies are signed with
    it), not the certificate (self-signed is normal)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       urllib.request.HTTPSHandler(context=ctx), _NoRedirect())


class _Nonces:
    """The nonces of signed requests accepted recently, so none is accepted twice. A
    nonce is kept while its request could still pass the time check; when the store is
    full of such nonces, the oldest goes and no request dated at or before it is
    accepted any more (it fails closed, never open)."""

    def __init__(self, limit: int = NONCES_KEPT):
        self.limit = limit
        self.floor = 0.0
        self._seen: dict[str, float] = {}   # nonce -> request time, oldest first

    def add(self, nonce: str, ts: float, now: float) -> bool:
        """Record a request's nonce; False when it was seen before (or may have been)."""
        if ts <= self.floor or nonce in self._seen:
            return False
        if len(self._seen) >= self.limit:
            cutoff = now - CLOCK_WINDOW_S
            self._seen = {n: t for n, t in self._seen.items() if t >= cutoff}
            while len(self._seen) >= self.limit:
                self.floor = max(self.floor, self._seen.pop(next(iter(self._seen))))
            if ts <= self.floor:
                return False
        self._seen[nonce] = ts
        return True


@dataclasses.dataclass
class PeerAuth:
    """What ClusterDiscovery.check_request found out about a peer-facing request."""
    ok: bool = False
    reason: str = ""      # why it was refused: a REFUSALS key, or "recipient" / "stale" (a
                          # signed request for another node id, or dated too far from now)
    legacy: bool = False  # it carried the raw key (a node running 2.8.0 or older)
    key: str = ""         # the key it was signed with: the reply is signed with it too
    nonce: str = ""       # its nonce, which a signed reply names
    sender: str = ""      # the node id it says it comes from (signed)
    first: bool = False   # the first refusal of this kind (or raw-key use) from that address
    again: bool = False   # that address was refused before: accepted again


@dataclasses.dataclass
class _Link:
    """What this node knows about calling one peer address (a fetch target)."""
    id: str = ""                 # the peer's node id as X-Cluster-Auth names it, once known
    legacy: bool = False         # it refused a signed request the way releases up to 2.8.0 do
    key: int = 0                 # which of this node's keys it accepts (0 the key, 1 the previous)
    offset: float | None = None  # its clock minus this node's, from a signed reply
    auth: str = ""               # how its last answered request authenticated: "v2" / "legacy"
    problem: str = ""            # why its last request failed: a _PROBLEMS key or "offline"/"error"


@dataclasses.dataclass
class _Answer:
    """One peer request's outcome (made in a worker thread, interpreted on the loop)."""
    status: int = 0              # the HTTP status, 0 when nothing (usable) answered
    data: dict | None = None     # the reply's JSON object
    problem: str = ""            # what went wrong, for the log ("" = a cluster node answered)
    v2: bool = False             # the reply says the peer understands signed requests
    signed: bool = False         # the reply is signed for this request with this node's key
    reason: str = ""             # a refusal's reason (its JSON "reason")
    peer_id: str = ""            # the peer's node id, from a signed reply
    offset: float | None = None  # the peer's clock minus this node's, from a signed reply


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram):
        self._on_datagram = on_datagram

    def datagram_received(self, data, addr):
        self._on_datagram(data, addr)

    def error_received(self, exc):  # ICMP port-unreachable etc. — ignore
        pass


class ClusterDiscovery:
    """Owns the UDP discovery socket + beacon loop + live peer table, signs this node's
    peer requests and checks the ones it receives. Reads the cluster config live from
    AppConfig so a settings change is picked up on the next restart (start/stop are
    driven by AppState's engine lifecycle)."""

    def __init__(self, config, logger):
        self.config = config            # AppConfig (cluster + admin_ui + instance_id, read live)
        self.log = logger
        self.boot = secrets.token_hex(8)    # this process: tells a copy of this node from itself
        self.started = time.time()
        self._transport = None
        self._beacon_task: asyncio.Task | None = None
        self._peers: dict[tuple, dict] = {}  # (id, run) -> {id,name,ip,port,scheme,last_seen,...}
        self._ids: dict[str, dict] = {}      # peer id -> the runs (processes) heard with it
        self._opener = _peer_opener()
        self._problems: dict[str, str] = {}  # peer host:port -> why its last request failed
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._fetched: dict[tuple, tuple[float, dict | None]] = {}  # target -> (when, answer)
        self._fetching: dict[tuple, asyncio.Task] = {}               # target -> request running
        self._links: dict[tuple, _Link] = {}         # target -> what calling it taught us
        self._signed_ok: dict[tuple, float] = {}     # targets that answered a signed request:
                                                     # never sent the raw key (kept on restarts)
        self._nonces = _Nonces()                     # also kept across restarts of discovery
        self._other_key: dict[str, float] = {}       # address -> last beacon with another key
        self._copies: dict[str, dict] = {}           # address -> a node with this node's id
        self._legacy_in: dict[str, float] = {}       # address -> when it last sent the raw key
        self._refusals: dict[str, str] = {}          # address -> why its last request was refused
        self._running = False
        self.discovery_error = ""           # non-empty when UDP discovery couldn't start

    # ----- config helpers -----
    @property
    def cluster(self):
        return self.config.cluster

    def keys(self) -> list[str]:
        """The keys this node accepts and signs with: the cluster key, then the previous
        one while a planned key change still accepts it (ClusterSettings.previous_key)."""
        c = self.cluster
        out = [c.key] if c.key.strip() else []
        prev = c.previous_key
        if out and prev.strip() and key_bytes(prev) != key_bytes(c.key) and (
                not c.previous_until or time.time() < c.previous_until):
            out.append(prev)
        return out

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
                # no LAN address known (no default route): 127.0.0.1 tells receivers to use
                # the address the beacon came from - on each network the right one
                ip = netinfo.primary_lan_ip() or "127.0.0.1"
        scheme = "https" if ui.tls_enabled else "http"
        return ip, int(ui.port), scheme

    # ----- beacon (de)serialization -----
    def _beacon_body(self) -> str:
        ip, port, scheme = self.advertised()
        payload = {"v": PROTOCOL, "id": self.config.instance_id, "name": socket.gethostname(),
                   "ip": ip, "port": port, "scheme": scheme, "t": round(time.time(), 3),
                   "b": self.boot,                     # this process (see _accept / _same_id)
                   "ips": netinfo.own_ipv4()[:32]}     # where its packets may come from
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _make_beacon(self, key: str | None = None, body: str | None = None) -> bytes:
        body = self._beacon_body() if body is None else body
        sig = _sign(self.cluster.key if key is None else key, body)
        return json.dumps({"d": body, "s": sig}).encode("utf-8")

    def handle_datagram(self, data: bytes, addr) -> None:
        """Verify a received beacon and record/refresh the peer. Pure (no I/O) so it
        can be called directly from tests to inject a peer. Any host on the LAN can
        send here, so anything malformed is dropped without an error."""
        keys = self.keys()
        if not keys:
            return
        try:
            env = json.loads(data.decode("utf-8"))
            body, sig = env["d"], env["s"]
            if not (isinstance(body, str) and isinstance(sig, str) and sig.isascii()):
                return
            kidx = next((i for i, k in enumerate(keys)
                         if hmac.compare_digest(_sign(k, body), sig)), None)
            p = json.loads(body)
        except (ValueError, KeyError, AttributeError, TypeError, RecursionError):
            return  # (a UnicodeError is a ValueError)
        if not isinstance(p, dict) or not p.get("id"):
            return
        src = str(addr[0]) if addr else ""
        if kidx is None:  # a different key (or tampered): not part of our cluster - only shown
            if src and str(p.get("b") or "") != self.boot:
                _remember(self._other_key, src, time.time(), 16)
            return
        raw_t = p.get("t")   # every release since 2.5 dates its beacons
        if isinstance(raw_t, bool) or not isinstance(raw_t, (int, float)):
            return
        t = float(raw_t)
        v: int = p["v"] if type(p.get("v")) is int else 1
        boot = str(p.get("b") or "")[:64] if v >= PROTOCOL else ""
        if not math.isfinite(t) or (boot and boot == self.boot):
            return  # (our own beacon - also one sent before this node took a new id)
        pid = str(p["id"])
        if pid == self.config.instance_id:
            self._same_id(p, addr, t, v)
            return
        ip, from_packet = self._beacon_address(p, addr, v)
        port = peer_port(p.get("port"))
        if ip is None or port is None:
            return  # no web-UI address to reach it at
        row = self._accept(pid, boot, t)
        if row is None:
            return  # not newer than what this node has from that sender: a replay
        now = time.time()
        scheme = p.get("scheme") if p.get("scheme") in ("http", "https") else "http"
        self._peers[row] = {
            "id": pid, "name": str(p.get("name", "?")), "ip": ip,
            "port": port, "scheme": scheme, "last_seen": now,
            "v": v, "key": kidx, "offset": t - now, "from_packet": from_packet,
        }
        self._other_key.pop(src, None)

    @staticmethod
    def _beacon_address(p: dict, addr, v: int = 1) -> tuple[str | None, bool]:
        """Where a beacon's sender is reached, and whether that is the packet's source: the
        IP address it advertises or, when that is no usable address (loopback or wildcard:
        the node could not tell its LAN IP), the packet's source address - for a sender that
        lists its own addresses (newer than 2.8.0), only one of those. Never a host
        name from the beacon."""
        try:
            ip = ipaddress.ip_address(str(p.get("ip") or "").strip())
        except ValueError:
            ip = None
        if ip is not None and not (ip.is_loopback or ip.is_unspecified):
            return str(ip), False
        if not addr:
            return None, False
        src, own = str(addr[0]), p.get("ips") if v >= PROTOCOL else None
        if isinstance(own, list) and own and src not in {str(a) for a in own}:
            return None, False   # sent from an address that node doesn't have
        return src, True

    def _accept(self, pid: str, boot: str, t: float) -> tuple | None:
        """The peer-table row for a verified beacon of node *pid*, from its run (process)
        *boot*, dated *t* - or None when it is not newer than the last beacon of that run,
        or a run dated well before the latest (an earlier run, replayed). A new run (a
        restart) replaces the earlier run's row, unless earlier runs keep beaconing too:
        then several nodes share the id (copies of one installation) and each has a row.
        Releases up to 2.8.0 send no run id: their beacons count as one run."""
        now = time.time()
        rec = self._ids.get(pid)
        if rec is None:
            if len(self._ids) >= 512:   # ids come and go: keep the ones heard from last
                for gone in sorted(self._ids, key=lambda i: self._ids[i]["seen"])[:256]:
                    del self._ids[gone]
            rec = self._ids[pid] = {"runs": {}, "max_t": t, "cur": boot, "shared": 0.0,
                                    "seen": now}
        last = rec["runs"].get(boot)
        if last is not None and t <= last:
            return None
        if last is None and t <= rec["max_t"] - BOOT_SKEW_S:
            return None
        if last is not None and boot and rec["cur"] and boot != rec["cur"]:
            rec["shared"] = now   # an earlier run beacons again: two nodes use this id
        rec["runs"][boot] = t
        if len(rec["runs"]) > 4:
            del rec["runs"][min(rec["runs"], key=rec["runs"].get)]
        new_run = boot != rec["cur"]
        rec.update(max_t=max(rec["max_t"], t), cur=boot, seen=now)
        if new_run and now - rec["shared"] > PEER_TTL:
            for k in [k for k in self._peers if k[0] == pid and k[1] != boot]:
                del self._peers[k]
        return (pid, boot)

    def _same_id(self, p: dict, addr, t: float, v: int) -> None:
        """A verified beacon with this node's own id, from another process: a copy of this
        installation (its data folder, disk or VM image) runs elsewhere - unless it was sent
        before this node started (an earlier run of this node, replayed)."""
        if t < self.started - 5:
            return
        ip, _from_packet = self._beacon_address(p, addr, v)
        where = peer_addr(ip or (addr[0] if addr else "?"), peer_port(p.get("port")) or "?")
        name = str(p.get("name", "?"))[:255]
        known = where in self._copies
        _remember(self._copies, where, {"addr": where, "name": name, "last_seen": time.time()}, 16)
        if not known:
            self.log(f"cluster: another node uses this node's identity "
                     f"({self.config.instance_id[:8]}): '{name}' at {where} — a copy of this "
                     "installation (data folder, disk or VM image)? Give one of them a new "
                     "identity under Settings → LAN cluster")

    def peers(self) -> list[dict]:
        """Live peers (TTL-pruned), sorted by name then ip."""
        now = time.time()
        self._peers = {k: p for k, p in self._peers.items() if now - p["last_seen"] <= PEER_TTL}
        return sorted(self._peers.values(), key=lambda p: (p["name"].lower(), p["ip"]))

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
        peer is also auto-discovered the richer auto entry wins, marked "configured" (and
        of two auto entries at one address, e.g. a node that just took a new id, the one
        heard from last)."""
        live, manual = self.peers(), self.manual_targets()
        configured = {(m["ip"], m["port"]) for m in manual}
        newest: dict[tuple, dict] = {}
        for p in live:
            k = (p["ip"], p["port"])
            if k not in newest or p["last_seen"] > newest[k]["last_seen"]:
                newest[k] = p
        seen, targets = set(), []
        for p in live:
            k = (p["ip"], p["port"])
            if newest[k] is not p:
                continue
            seen.add(k)
            targets.append({**p, "source": "auto", "configured": k in configured})
        for m in manual:
            if (m["ip"], m["port"]) not in seen:
                seen.add((m["ip"], m["port"]))
                targets.append(m)
        return targets

    def find_target(self, host, port) -> dict | None:
        """The discovered or configured peer at host:port (as the fleet view shows it),
        else None. Browser-initiated peer calls are resolved through this, so their
        address and scheme never come from the request."""
        h, p = peer_host(host), peer_port(port)
        if h is None or p is None:
            return None
        return next((t for t in self.all_targets() if t["ip"] == h and t["port"] == p), None)

    # ----- what the fleet view and Settings show -----
    def peer_state(self, target: dict) -> dict:
        """How this node fares with a peer: "auth" ("v2" / "legacy" once a request was
        answered), "problem" (why the last one failed: a code the fleet view explains)
        and "previous_key" (it accepts only the previous key: not changed yet)."""
        link = self._links.get(_target_key(target)) or _Link()
        idx = (target.get("key") or 0) if target.get("source") == "auto" else link.key
        return {"auth": link.auth, "problem": link.problem,
                "previous_key": idx == 1 and len(self.keys()) > 1}

    def copies(self) -> list[dict]:
        """Nodes heard recently with this node's own id ({addr, name}): copies of it."""
        now = time.time()
        return [c for c in self._copies.values() if now - c["last_seen"] <= NOTE_TTL]

    def shared_ids(self) -> list[dict]:
        """Peer ids that several live nodes use (copies of one installation)."""
        rows: dict[str, list] = {}
        for p in self.peers():
            rows.setdefault(p["id"], []).append(p)
        return [{"id": pid, "name": r[0]["name"],
                 "addrs": sorted({peer_addr(x["ip"], x["port"]) for x in r})}
                for pid, r in rows.items() if len(r) > 1]

    def other_key_nodes(self) -> list[str]:
        """Addresses that recently broadcast beacons signed with another key (never
        contacted: there is nothing to trust them with)."""
        now = time.time()
        live = {p["ip"] for p in self.peers()}
        return sorted(ip for ip, when in self._other_key.items()
                      if now - when <= NOTE_TTL and ip not in live)

    def legacy_nodes(self) -> list[str]:
        """Nodes using the unsigned key (releases up to 2.8.0): those this node sends it to
        and those that sent it here recently - none while legacy authentication is off."""
        if not self.cluster.legacy_auth:
            return []
        now, out = time.time(), set()
        for ip, when in self._legacy_in.items():
            if now - when <= LEGACY_SEEN_S:
                out.add(ip)
        for t in self.all_targets():
            link = self._links.get(_target_key(t))
            if (t["source"] == "auto" and t.get("v", 1) < PROTOCOL) or (link and link.auth == "legacy"):
                out.add(peer_addr(t["ip"], t["port"]))
        return sorted(out)

    # ----- lifecycle -----
    async def start(self) -> None:
        if self._running or not self.cluster.active:
            return
        if not self.cluster.discovery:   # manual peers only: no beacons sent or read
            self.discovery_error = ""
            self._running = True
            self.log(f"cluster: on, auto-discovery off (manual peers only) as "
                     f"'{socket.gethostname()}' ({self.config.instance_id[:8]})")
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
        self._peers.clear()   # (not _ids: old beacons must stay old after a settings save)
        self._problems.clear()
        self._links.clear()        # the keys may have changed (what a peer signed up for has not)
        self._other_key.clear()
        self._copies.clear()
        self.forget()
        if self._executor is not None:  # requests still running finish on their own
            self._executor.shutdown(wait=False)
            self._executor = None

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
        body = self._beacon_body()
        # one beacon per key: during a planned key change the nodes that only have the
        # previous key still see this node
        msgs = [self._make_beacon(k, body) for k in self.keys()]
        port = int(self.cluster.discovery_port)
        for addr in self._broadcast_addrs():
            for msg in msgs:
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

    # ----- peer-facing requests this node receives -----
    def check_request(self, method: str, path: str, query: str, body: bytes, headers,
                      client: str) -> PeerAuth:
        """Whether a request to a peer-facing endpoint may proceed: a signed request (see
        the module docstring) for this node, or - while legacy_auth is on - the raw key of
        a node running 2.8.0 or older. *path* is the decoded path, *query* the raw query string,
        *client* the caller's address; refusals and raw-key use are tallied per address
        (PeerAuth.first / .again), so the caller logs each change once."""
        if not self.cluster.active:
            auth = PeerAuth(reason="off")
        elif headers.get(AUTH_HEADER) is not None:
            auth = self._check_signed(method, canonical_target(path, query), body,
                                      headers.get(AUTH_HEADER))
        elif headers.get(KEY_HEADER) is not None:
            key = next((k for k in self.keys() if key_header_matches(k, headers.get(KEY_HEADER))),
                       None)
            if key is None:
                auth = PeerAuth(reason="key")
            elif not self.cluster.legacy_auth:
                auth = PeerAuth(reason="legacy")
            else:
                auth = PeerAuth(ok=True, legacy=True, key=key)
        else:
            auth = PeerAuth(reason="missing")
        if auth.ok:
            auth.again = self._refusals.pop(client, None) is not None
            if auth.legacy:
                auth.first = client not in self._legacy_in
                _remember(self._legacy_in, client, time.time(), 64)
        elif auth.reason not in ("recipient", "stale"):   # steps of the exchange, not failures
            auth.first = self._refusals.get(client) != auth.reason
            _remember(self._refusals, client, auth.reason, 256)
        return auth

    def _check_signed(self, method: str, target: str, body: bytes, value) -> PeerAuth:
        p = parse_auth(value)
        if p is None or "to" not in p or not _TIME.fullmatch(p.get("ts", "")) \
                or not _NONCE.fullmatch(p.get("nonce", "")) or not p.get("sig"):
            return PeerAuth(reason="malformed")
        sender, to, ts, nonce = p.get("from", ""), p["to"], p["ts"], p["nonce"]
        key = next((k for k in self.keys() if hmac.compare_digest(
            request_mac(k, method, target, body, sender, to, ts, nonce), p["sig"])), None)
        if key is None:
            return PeerAuth(reason="signature")
        # from here on the request is from a key holder: a refusal is signed, so the sender
        # can trust what it says (this node's id, its clock) and send the request again
        auth = PeerAuth(key=key, nonce=nonce, sender=sender)
        now, when = time.time(), float(ts)
        if to != wire_id(self.config.instance_id):
            auth.reason = "recipient"   # for another node, or sent before our id was known
        elif abs(when - now) > CLOCK_WINDOW_S or when < self.started - 2:
            auth.reason = "stale"       # (a request dated before this process started may
                                        # have been accepted by the one before it)
        elif not self._nonces.add(nonce, when, now):
            return PeerAuth(reason="replay")
        else:
            auth.ok = True
        return auth

    def reply_auth(self, auth: PeerAuth | None, status: int, body: bytes) -> str:
        """The X-Cluster-Auth value of a peer-facing reply. Signed for a signed request
        (also a refusal of one whose signature was valid); else just "v2", which tells the
        caller that this node understands signed requests."""
        if auth is None or auth.legacy or not (auth.key and auth.nonce):
            return AUTH_SCHEME
        me, now = wire_id(self.config.instance_id), f"{time.time():.3f}"
        return format_auth({"id": me, "now": now,
                            "sig": reply_mac(auth.key, auth.nonce, status, body, me, now)})

    # ----- peer HTTP (signed; the raw key only for nodes running 2.8.0 or older) -----
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
        """A peer's fleet-view payload (/api/cluster/local), or None when it doesn't
        answer. Shared by every open dashboard: an answer (also "no answer") is reused
        for FETCH_CACHE_S seconds, and one request per peer at most is in flight,
        however many tabs and users refresh the fleet view."""
        k = _target_key(peer)
        hit = self._fetched.get(k)
        if hit is not None and time.monotonic() - hit[0] < FETCH_CACHE_S:
            return hit[1]
        task = self._fetching.get(k)
        if task is None:
            task = asyncio.create_task(self.get_peer(peer, "/api/cluster/local", timeout=timeout),
                                       name="cluster-fetch")
            self._fetching[k] = task
            task.add_done_callback(functools.partial(self._fetch_done, k))
        return await asyncio.shield(task)   # a closed tab doesn't cancel it for the others

    def _fetch_done(self, k: tuple, task: asyncio.Task) -> None:
        if self._fetching.get(k) is not task:
            return  # forgotten while it ran: its answer may predate a change
        del self._fetching[k]
        if not task.cancelled() and task.exception() is None:
            now = time.monotonic()
            self._fetched = {t: v for t, v in self._fetched.items() if now - v[0] < FETCH_CACHE_S}
            self._fetched[k] = (now, task.result())

    def forget(self, peer: dict | None = None) -> None:
        """Drop shared fleet-view answers - one peer's or all - so the next refresh asks
        again: after a remote start/stop/edit the table shows the change at once."""
        if peer is None:
            self._fetched.clear()
            self._fetching.clear()
        else:
            self._fetched.pop(_target_key(peer), None)
            self._fetching.pop(_target_key(peer), None)

    async def control_peer(self, target: dict, mapping_id: str, action: str,
                           timeout: float = 4.0) -> dict | None:
        return await self.post_peer(target, "/api/cluster/control",
                                    {"mapping_id": mapping_id, "action": action}, timeout)

    async def _request(self, target: dict, method: str, path: str, query: dict | None,
                       body: bytes | None, timeout: float) -> tuple[int, dict | None]:
        """One key-guarded request: (HTTP status, 0 when nothing usable answered; the
        reply's JSON object or None). Made only to a discovered or configured peer, at
        an address peer_url accepts. Signed, unless the peer runs 2.8.0 or older (see
        _how); sent again at most a few times when the answer teaches this node what a
        signed request needs (the peer's id or clock, the other key) or that the peer
        is an older node."""
        k = _target_key(target)
        t = next((x for x in self.all_targets() if _target_key(x) == k), None)
        if t is None:
            return 0, None
        try:
            url = peer_url(t["scheme"], t["ip"], t["port"], path, query)
        except ValueError:
            self._note(t, "not a valid peer address")
            return 0, None
        link = self._links.get(k)
        if link is None:
            link = _Link()
            _remember(self._links, k, link, 512)
        ans, steps = _Answer(), []
        for _ in range(4):
            how = self._how(t, link)
            if isinstance(how, str):   # nothing may be sent: why
                link.problem = how
                self._note(t, _PROBLEMS[how])
                return 0, None
            try:
                ans = await asyncio.get_running_loop().run_in_executor(
                    self._pool(), self._http, method, url, body, timeout, how)
            except RuntimeError:  # the interpreter is shutting down
                return 0, None
            step = self._learn(k, t, link, how, ans)
            if not step or step in steps:   # done - or the same lesson twice: give up
                break
            steps.append(step)
        self._note(t, ans.problem)
        return ans.status, ans.data

    def _how(self, t: dict, link: _Link) -> dict | str:
        """How to authenticate the next request to peer *t*: {"mode": "v2" (signed) or
        "legacy" (the raw key), "key", "idx" (which of this node's keys), "to" (its node
        id, "" while unknown), "offset" (its clock minus ours)} - or, when nothing may be
        sent, the reason (a _PROBLEMS key)."""
        keys = self.keys()
        if not keys or not self.cluster.active:
            return "off"
        if t.get("source") == "auto":    # its signed beacon says what it is
            idx, older, to = t.get("key") or 0, t.get("v", 1) < PROTOCOL, wire_id(t.get("id"))
        else:
            idx, to = link.key, link.id
            older = link.legacy and _target_key(t) not in self._signed_ok
        if older:
            if not self.cluster.legacy_auth:
                return "legacy-off"
            if t.get("from_packet") and not t.get("configured"):
                # its beacon names no address of its own (127.0.0.1: no default route), and
                # the address a beacon came from proves nothing - a beacon can be replayed
                # from anywhere: the raw key goes only where it says it is, or to a manual peer
                return "manual-only"
        idx = min(idx, len(keys) - 1)
        offset = link.offset if link.offset is not None else float(t.get("offset") or 0.0)
        return {"mode": "legacy" if older else "v2", "key": keys[idx], "idx": idx, "to": to,
                "offset": offset}

    def _learn(self, k: tuple, t: dict, link: _Link, how: dict, ans: _Answer) -> str:
        """Update what this node knows about peer *t* from one answer and say how it went
        (ans.problem for the log, link.problem for the fleet view). Returns why to send
        the request again ("" = done): the peer's id or clock is known now, the other key
        or the other way of authenticating may work."""
        manual, two_keys = t.get("source") != "auto", len(self.keys()) > 1
        refused = ans.status in (401, 403)
        if ans.status == 0:           # no answer at all (ans.problem says why)
            link.problem = "offline"
            link.legacy = False       # it may come back upgraded: sign first again
            return ""
        if ans.signed:                # a signing node with this node's key
            link.id, link.offset, link.legacy = ans.peer_id or link.id, ans.offset, False
            if manual:
                link.key = how["idx"]
            _remember(self._signed_ok, k, time.time(), 1024)
            if refused and ans.reason == "stale":
                return self._failed(link, ans, "clock", retry="stale")
            if refused and ans.reason == "recipient":
                return self._failed(link, ans, "error", retry="recipient",
                                    text="it took the request as meant for another node")
            link.auth = "v2"
            return self._answered(link, ans)
        if how["mode"] == "v2":
            if refused and ans.v2:    # a signing node refused it
                if ans.reason == "off":
                    return self._failed(link, ans, "cluster-off")
                if ans.reason == "replay":
                    return self._failed(link, ans, "error", retry="replay",
                                        text="it took a new request for a replayed one")
                if ans.reason == "signature" and manual and two_keys:
                    link.key = 1 - how["idx"]   # the other key (if that fails too: this one again)
                    return self._failed(link, ans, "key", retry="key" if how["idx"] == 0 else "")
                return self._failed(link, ans, "key")
            if refused:               # as releases up to 2.8.0 refuse a request without the raw key
                if manual and k not in self._signed_ok:
                    if not self.cluster.legacy_auth:
                        return self._failed(link, ans, "legacy-off")
                    link.legacy = True
                    return self._failed(link, ans, "key", retry="legacy")
                return self._failed(link, ans, "key")
            if not ans.problem and (200 <= ans.status < 300 or ans.status in (400, 404)):
                return self._failed(link, ans, "unsigned")
            return self._answered(link, ans)   # (a redirect, an HTTP error: no answer anyway)
        # it was sent the raw key (a node running 2.8.0 or older)
        if refused and ans.v2:        # a signing node after all
            if manual:
                link.legacy = False
                return self._failed(link, ans, "key", retry="upgraded")
            return self._failed(link, ans, "key")
        if refused:
            if manual and two_keys:
                link.key = 1 - how["idx"]
                return self._failed(link, ans, "key", retry="key" if how["idx"] == 0 else "")
            return self._failed(link, ans, "key")
        if ans.v2 and manual:         # answered, and says it understands signed requests now
            link.legacy = False
        link.auth = "legacy"
        return self._answered(link, ans)

    @staticmethod
    def _failed(link: _Link, ans: _Answer, problem: str, text: str = "", retry: str = "") -> str:
        """No usable answer: say why (link.problem a code, ans.problem the log text) and
        return *retry*."""
        if not text and problem == "key":
            why = REFUSALS.get(ans.reason) if ans.v2 else None
            text = f"HTTP {ans.status}: {_PROBLEMS['key']} " + (
                f"({why})" if why else "(or its cluster is off)")
        link.problem, ans.problem = problem, text or _PROBLEMS.get(problem, problem)
        ans.status, ans.data = 0, None
        return retry

    @staticmethod
    def _answered(link: _Link, ans: _Answer) -> str:
        """The peer answered: what (if anything) to log about it."""
        code = ans.status
        if 300 <= code < 400:
            ans.problem, ans.data = f"HTTP {code}: redirects are not followed", None
        elif code >= 400 and (ans.data is None or code >= 500):
            ans.problem = ans.problem or f"HTTP {code}"
        # (a 4xx with a JSON answer is a peer refusing one request, e.g. a bad edit)
        link.problem = "error" if ans.problem else ""
        return ""

    def _pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=HTTP_WORKERS, thread_name_prefix="cluster-http")
        return self._executor

    def _http(self, method: str, url: str, body: bytes | None, timeout: float,
              how: dict) -> _Answer:
        """Make one peer request (in a worker thread), authenticated as *how* says (see
        _how): signed - and then its reply checked - or with the raw key of a node running
        2.8.0 or older. Never raises."""
        headers, nonce = {}, ""
        if how["mode"] == "legacy":
            headers[KEY_HEADER] = key_header(how["key"])
        else:
            parts = urllib.parse.urlsplit(url)
            target = canonical_target(urllib.parse.unquote(parts.path), parts.query)
            me, nonce = wire_id(self.config.instance_id), secrets.token_hex(16)
            ts = f"{time.time() + how['offset']:.3f}"
            headers[AUTH_HEADER] = format_auth({
                "from": me, "to": how["to"], "ts": ts, "nonce": nonce,
                "sig": request_mac(how["key"], method, target, body, me, how["to"], ts, nonce)})
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                status, reply = r.status, r.headers.get(AUTH_HEADER)
                raw, problem = _read(r)
        except urllib.error.HTTPError as e:
            with contextlib.closing(e):
                status, reply = e.code, (e.headers.get(AUTH_HEADER) if e.headers else None)
                raw, problem = _read(e)
        except Exception as e:  # no connection, timeout, TLS ...
            return _Answer(problem=str(getattr(e, "reason", "") or e) or type(e).__name__)
        ans = _Answer(status=status, problem=problem)
        params = parse_auth(reply)
        ans.v2 = params is not None
        if params and nonce and raw is not None and _TIME.fullmatch(params.get("now", "")) \
                and params.get("sig") and hmac.compare_digest(
                    reply_mac(how["key"], nonce, status, raw, params.get("id", ""), params["now"]),
                    params["sig"]):
            ans.signed, ans.peer_id = True, params.get("id", "")
            ans.offset = float(params["now"]) - time.time()
        if raw is not None:
            ans.data = _json_object(raw)
            if ans.data is None and not problem and 200 <= status < 300:
                ans.problem = "not a ser2net cluster reply"
            elif ans.data is not None:
                ans.reason = str(ans.data.get("reason") or "")[:32]
        return ans

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


def _read(resp) -> tuple[bytes | None, str]:
    """A peer reply's body (at most MAX_REPLY_BYTES), or None and why not."""
    try:
        raw = resp.read(MAX_REPLY_BYTES + 1)
    except Exception as e:  # cut off, timed out
        return None, f"reply not read: {e}"
    if len(raw) > MAX_REPLY_BYTES:
        return None, f"reply larger than {MAX_REPLY_BYTES // 2**20} MB"
    return raw, ""


def _json_object(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None
