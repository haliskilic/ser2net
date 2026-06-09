"""Configuration data model + atomic JSON persistence for ser2net.

The model is deliberately split into explicit typed fields (borrowed from
ser2net's connection model) rather than gensio stack-strings, so the web UI can
present every option directly and validate it.

Persistence is plain stdlib JSON with an atomic write (tempfile -> fsync ->
os.replace -> parent-dir fsync) so a crash mid-write can never truncate dozens of
mappings. config.json is the single source of truth (no .bak); deleting it resets
all config.
"""
from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import json
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Allowed value sets (mirror pyserial; kept here so config validation has no
# hard import dependency on pyserial).
# ---------------------------------------------------------------------------
BYTESIZES = (5, 6, 7, 8)
PARITIES = ("N", "E", "O", "M", "S")
PARITY_LABELS = {"N": "None", "E": "Even", "O": "Odd", "M": "Mark", "S": "Space"}
STOPBITS = (1, 1.5, 2)
FLOWCONTROLS = ("none", "rtscts", "xonxoff", "dsrdtr")
LINE_STATES = ("on", "off", "keep")
PROTOCOLS = ("raw", "telnet", "rfc2217", "modbus")
COMMON_BAUDRATES = (
    300, 1200, 2400, 4800, 9600, 19200, 38400, 57600,
    115200, 230400, 460800, 921600,
)


class ConfigError(ValueError):
    """Raised when a config object fails validation. Message is user-facing."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def lock_down_dir(directory: str) -> None:
    """Restrict a state directory to its owner so the secrets it holds (secret_key,
    password hash in config.json) and captured serial logs are not readable by other
    local users.

    POSIX: chmod 0700. Windows: ``icacls`` removes inherited ACEs and grants Full
    only to the current user, SYSTEM and the Administrators group, with inheritance
    so child files (config.json, the atomic temp files, per-mapping logs) pick up the
    same restriction. Best-effort: silently degrades if it cannot be applied (the
    posix path was the only one protected before — Windows was left world-readable
    despite the docs claiming 0600).
    """
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        return
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        import subprocess

        # Set the ACL on the directory only (NOT /T): /inheritance:r drops inherited
        # ACEs, and the (OI)(CI) grants both protect the dir and propagate to child
        # files/dirs, so config.json + the atomic temp files + per-mapping logs are
        # created owner-private. Applying (OI)(CI) ACEs to existing *files* via /T
        # corrupts their ACL (inheritance flags are meaningless on a leaf and left the
        # owner unable to read config.json), so we rely on inheritance instead.
        # SIDs are locale-independent: *S-1-5-18 = SYSTEM, *S-1-5-32-544 = Administrators.
        grants = [f"{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
        cmd = ["icacls", directory, "/inheritance:r"]
        for g in grants:
            cmd += ["/grant:r", g]
        cmd += ["/C", "/Q"]
        with contextlib.suppress(Exception):
            subprocess.run(cmd, capture_output=True, timeout=15, check=False)


def normalize_cidr(value: str) -> str:
    """Treat a bare wildcard address as 'any'. Users typing 0.0.0.0 / :: in an
    allow/priority list mean 'all clients', not a single /32 or /128 host."""
    v = value.strip()
    if v == "0.0.0.0":
        return "0.0.0.0/0"
    if v == "::":
        return "::/0"
    return v


# ---------------------------------------------------------------------------
# Serial side
# ---------------------------------------------------------------------------
@dataclass
class SerialAdvanced:
    rs485_enabled: bool = False
    rs485_delay_before_tx_ms: float = 0.0
    rs485_delay_after_tx_ms: float = 0.0
    rs485_rts_level_for_tx: bool = True
    hangup_when_done: bool = False
    nobreak: bool = False


@dataclass
class SerialSettings:
    port: str = ""
    # Stable hardware identity; when set, used to (re)resolve the device path on
    # (re)connect so COMx / ttyUSB* renumbering doesn't break a mapping.
    match: dict[str, Any] = field(default_factory=dict)
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    flowcontrol: str = "none"  # none | rtscts | xonxoff | dsrdtr
    rts_on_open: str = "keep"  # on | off | keep
    dtr_on_open: str = "keep"
    exclusive: bool = True
    advanced: SerialAdvanced = field(default_factory=SerialAdvanced)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "SerialSettings":
        d = dict(d or {})
        adv = d.pop("advanced", {}) or {}
        known = {f.name for f in dataclasses.fields(SerialAdvanced)}
        advanced = SerialAdvanced(**{k: v for k, v in adv.items() if k in known})
        known = {f.name for f in dataclasses.fields(SerialSettings)} - {"advanced"}
        return SerialSettings(advanced=advanced, **{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        if not self.port:
            raise ConfigError("Serial port is required.")
        if not isinstance(self.baudrate, int) or self.baudrate <= 0:
            raise ConfigError("Baud rate must be a positive integer.")
        if self.bytesize not in BYTESIZES:
            raise ConfigError(f"Data bits must be one of {BYTESIZES}.")
        if self.parity not in PARITIES:
            raise ConfigError(f"Parity must be one of {PARITIES}.")
        if self.stopbits not in STOPBITS:
            raise ConfigError(f"Stop bits must be one of {STOPBITS}.")
        if self.flowcontrol not in FLOWCONTROLS:
            raise ConfigError(f"Flow control must be one of {FLOWCONTROLS}.")
        if self.rts_on_open not in LINE_STATES or self.dtr_on_open not in LINE_STATES:
            raise ConfigError("RTS/DTR on open must be on|off|keep.")
        # rtscts (hardware) flow control conflicts with RS-485 RTS direction toggling.
        if self.advanced.rs485_enabled and self.flowcontrol == "rtscts":
            raise ConfigError("RS-485 RTS toggling conflicts with RTS/CTS flow control.")

    def compact(self) -> str:
        """ser2net-style compact descriptor, e.g. '9600N81'."""
        return f"{self.baudrate}{self.parity}{self.bytesize}{int(self.stopbits)}"


# ---------------------------------------------------------------------------
# Network side
# ---------------------------------------------------------------------------
NET_MODES = ("server", "client", "udp")


@dataclass
class NetworkSettings:
    mode: str = "server"  # server (listen) | client (connect-out) | udp
    protocol: str = "raw"  # raw | telnet | rfc2217
    bind_ip: str = "0.0.0.0"
    port: int = 4001
    # client/udp connect-out target
    remote_host: str = ""
    remote_port: int = 0
    max_connections: int = 1
    kick_old_user: bool = False
    allowed_client_ips: list[str] = field(default_factory=list)
    # High-priority clients: when the port is at capacity, a connection from one of
    # these IPs/CIDRs is admitted by kicking an existing client (oldest non-priority
    # first), regardless of kick_old_user.
    priority_client_ips: list[str] = field(default_factory=list)
    read_only: bool = False
    # Per-client outbound (serial->network) buffer, in chunks. A client slower than
    # the serial source is dropped once this fills. Worst-case memory per client is
    # ~client_queue_max * 64KB; raise for bursty high-throughput links.
    client_queue_max: int = 2048
    # Per-mapping TLS for the data bridge (server/client TCP). Both files required.
    tls: bool = False
    tls_cert: str = ""
    tls_key: str = ""

    @property
    def listens(self) -> bool:
        return self.mode in ("server", "udp")

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "NetworkSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(NetworkSettings)}
        return NetworkSettings(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        if self.mode not in NET_MODES:
            raise ConfigError(f"Network mode must be one of {NET_MODES}.")
        if self.protocol not in PROTOCOLS:
            raise ConfigError(f"Protocol must be one of {PROTOCOLS}.")

        if self.listens:
            try:
                ip = ipaddress.ip_address(self.bind_ip)
            except ValueError:
                raise ConfigError(f"Invalid bind IP: {self.bind_ip!r}.") from None
            # 0.0.0.0 and :: are the "all interfaces" wildcards and must be allowed
            # even though :: reports is_reserved=True.
            if ip.is_multicast or (ip.is_reserved and str(ip) not in ("0.0.0.0", "::")):
                raise ConfigError("Bind IP cannot be a multicast/reserved address.")
            if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
                raise ConfigError("TCP/UDP port must be between 1 and 65535.")
        else:  # client (connect-out)
            if not self.remote_host.strip():
                raise ConfigError("Connect-out mode requires a remote host.")
            if not isinstance(self.remote_port, int) or not (1 <= self.remote_port <= 65535):
                raise ConfigError("Remote port must be between 1 and 65535.")

        if not isinstance(self.max_connections, int) or self.max_connections < 1:
            raise ConfigError("Max connections must be >= 1.")
        if not isinstance(self.client_queue_max, int) or self.client_queue_max < 16:
            raise ConfigError("Client queue size must be an integer >= 16.")
        if self.protocol == "rfc2217" and self.mode == "udp":
            raise ConfigError("RFC2217 requires a TCP (server/client) connection, not UDP.")
        if self.protocol == "modbus" and self.mode != "server":
            raise ConfigError("Modbus gateway requires TCP server mode (masters connect in).")
        if self.protocol == "rfc2217" and self.max_connections > 1:
            raise ConfigError(
                "RFC2217 supports a single connection (clients share one serial "
                "port's settings). Set Max connections = 1."
            )
        if self.tls:
            if self.mode == "udp":
                raise ConfigError("TLS is not applicable to UDP mappings.")
            if not (self.tls_cert and self.tls_key):
                raise ConfigError("Enable TLS requires both a certificate and a key.")
            for p in (self.tls_cert, self.tls_key):
                if not os.path.isfile(p):
                    raise ConfigError(f"TLS file not found: {p}")
        for label, lst in (("allowed", self.allowed_client_ips),
                           ("priority", self.priority_client_ips)):
            for cidr in lst:
                try:
                    ipaddress.ip_network(normalize_cidr(cidr), strict=False)
                except ValueError:
                    raise ConfigError(f"Invalid {label} client IP/CIDR: {cidr!r}.") from None


# ---------------------------------------------------------------------------
# Per-mapping options
# ---------------------------------------------------------------------------
@dataclass
class MappingOptions:
    banner: str = ""
    openstr: str = ""
    closestr: str = ""
    closeon: str = ""
    idle_timeout_s: int = 0
    trace_both: str = ""  # file path; empty = disabled
    trace_hexdump: bool = False
    trace_timestamp: bool = True
    # RFC2217 interop knobs for non-compliant peers:
    rfc2217_poll_modem_interval_s: float = 1.0
    rfc2217_net_timeout_s: float = 3.0
    # Modbus gateway: how long to wait for an RTU slave's reply before returning a
    # Modbus exception (0x0B gateway-target-failed) to the TCP master.
    modbus_response_timeout_s: float = 1.0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingOptions":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(MappingOptions)}
        return MappingOptions(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Per-mapping MQTT publishing (optional northbound to an IIoT broker)
# ---------------------------------------------------------------------------
@dataclass
class MqttSettings:
    enabled: bool = False
    host: str = ""
    port: int = 1883
    base_topic: str = ""        # e.g. "ser2net/plc1"; serial lines publish to <base_topic>
    qos: int = 0                # 0 | 1 | 2
    tls: bool = False
    username: str = ""
    password: str = ""
    client_id: str = ""         # blank => auto

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MqttSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(MqttSettings)}
        return MqttSettings(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host.strip():
            raise ConfigError("MQTT is enabled but no broker host is set.")
        if not self.base_topic.strip():
            raise ConfigError("MQTT is enabled but no base topic is set.")
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ConfigError("MQTT port must be between 1 and 65535.")
        if self.qos not in (0, 1, 2):
            raise ConfigError("MQTT QoS must be 0, 1 or 2.")


# ---------------------------------------------------------------------------
# A single serial<->TCP mapping
# ---------------------------------------------------------------------------
MAPPING_KINDS = ("net", "serialbridge")


@dataclass
class MappingConfig:
    id: str = field(default_factory=_new_id)
    name: str = ""
    enabled: bool = True
    kind: str = "net"  # net (serial<->network) | serialbridge (serial<->serial)
    serial: SerialSettings = field(default_factory=SerialSettings)
    serial_b: SerialSettings = field(default_factory=SerialSettings)  # serialbridge only
    network: NetworkSettings = field(default_factory=NetworkSettings)
    options: MappingOptions = field(default_factory=MappingOptions)
    mqtt: MqttSettings = field(default_factory=MqttSettings)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingConfig":
        d = dict(d or {})
        return MappingConfig(
            id=d.get("id") or _new_id(),
            name=d.get("name", ""),
            enabled=bool(d.get("enabled", True)),
            kind=d.get("kind", "net"),
            serial=SerialSettings.from_dict(d.get("serial", {})),
            serial_b=SerialSettings.from_dict(d.get("serial_b", {})),
            network=NetworkSettings.from_dict(d.get("network", {})),
            options=MappingOptions.from_dict(d.get("options", {})),
            mqtt=MqttSettings.from_dict(d.get("mqtt", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError("Mapping name is required.")
        if self.kind not in MAPPING_KINDS:
            raise ConfigError(f"Mapping kind must be one of {MAPPING_KINDS}.")
        self.serial.validate()
        if self.kind == "serialbridge":
            self.serial_b.validate()
            if self.serial_b.port == self.serial.port:
                raise ConfigError("Serial-to-serial bridge needs two different ports.")
        else:
            self.network.validate()
        self.mqtt.validate()


# ---------------------------------------------------------------------------
# Global application config
# ---------------------------------------------------------------------------
@dataclass
class AdminUI:
    bind_ip: str = "127.0.0.1"
    port: int = 8080
    tls_cert: str = ""
    tls_key: str = ""

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)


ROLES = ("admin", "operator", "viewer")
# higher rank = more privilege; a route requires >= some rank
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass
class User:
    """A web-UI account. `role` gates what the user may do; `pwd_version` is bumped
    on that user's password change to revoke only their existing sessions."""
    username: str = ""
    password_hash: str = ""
    role: str = "admin"
    pwd_version: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "User":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(User)}
        u = User(**{k: v for k, v in d.items() if k in known})
        if u.role not in ROLES:
            u.role = "viewer"
        return u


@dataclass
class AppConfig:
    version: int = 1
    admin_ui: AdminUI = field(default_factory=AdminUI)
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    api_token_hash: str = ""  # sha256 of the REST API bearer token (empty => API disabled)
    session_timeout_s: int = 8 * 3600
    # Web-UI accounts. Empty => first-run setup pending. A legacy single-password
    # config (top-level password_hash) is migrated to one 'admin' user on load.
    users: list[User] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)  # serial defaults
    mappings: list[MappingConfig] = field(default_factory=list)

    # ----- (de)serialization -----
    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AppConfig":
        d = dict(d or {})
        admin = d.get("admin_ui", {}) or {}
        known = {f.name for f in dataclasses.fields(AdminUI)}
        admin_ui = AdminUI(**{k: v for k, v in admin.items() if k in known})
        users = [User.from_dict(u) for u in d.get("users", [])]
        if not users and d.get("password_hash"):  # migrate legacy single-password config
            users = [User(username="admin", password_hash=d["password_hash"],
                          role="admin", pwd_version=int(d.get("pwd_version", 0)))]
        cfg = AppConfig(
            version=int(d.get("version", 1)),
            admin_ui=admin_ui,
            secret_key=d.get("secret_key") or secrets.token_hex(32),
            api_token_hash=d.get("api_token_hash", ""),
            session_timeout_s=int(d.get("session_timeout_s", 8 * 3600)),
            users=users,
            defaults=d.get("defaults", {}) or {},
            mappings=[MappingConfig.from_dict(m) for m in d.get("mappings", [])],
        )
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "admin_ui": asdict(self.admin_ui),
            "secret_key": self.secret_key,
            "api_token_hash": self.api_token_hash,
            "session_timeout_s": self.session_timeout_s,
            "users": [asdict(u) for u in self.users],
            "defaults": self.defaults,
            "mappings": [m.to_dict() for m in self.mappings],
        }

    # ----- helpers -----
    @property
    def password_set(self) -> bool:
        return bool(self.users)

    def get_user(self, username: str) -> Optional[User]:
        return next((u for u in self.users if u.username == username), None)

    def admin_count(self) -> int:
        return sum(1 for u in self.users if u.role == "admin")

    def get_mapping(self, mapping_id: str) -> Optional[MappingConfig]:
        return next((m for m in self.mappings if m.id == mapping_id), None)

    def validate(self) -> None:
        """Validate every mapping and cross-mapping invariants (unique IP:port)."""
        # TLS: both files required together, and they must exist/be readable so a
        # bad path fails fast at save time instead of crashing uvicorn at startup.
        cert, key = self.admin_ui.tls_cert, self.admin_ui.tls_key
        if bool(cert) != bool(key):
            raise ConfigError("Set both TLS certificate and key, or neither.")
        for path in (p for p in (cert, key) if p):
            if not os.path.isfile(path):
                raise ConfigError(f"TLS file not found or not readable: {path}")

        seen: dict[tuple[str, str, int], str] = {}   # (proto, bind_ip, port) -> name
        serial_owner: dict[str, tuple[str, str]] = {}  # device -> (mapping_id, name)
        for m in self.mappings:
            m.validate()

            # A serial port can only be opened by one mapping. Flag two ENABLED
            # mappings that target the same literal device (skip dynamic VID/PID
            # `match` entries, whose device path is resolved at open time).
            if m.enabled:
                devices = []
                if m.serial.port and not m.serial.match:
                    devices.append(m.serial.port)
                if m.kind == "serialbridge" and m.serial_b.port and not m.serial_b.match:
                    devices.append(m.serial_b.port)
                for dev in devices:
                    prev = serial_owner.get(dev)
                    if prev is not None and prev[0] != m.id:
                        raise ConfigError(
                            f"Mappings '{prev[1]}' and '{m.name}' both use serial port "
                            f"{dev} — a serial port can only be opened by one mapping."
                        )
                    serial_owner[dev] = (m.id, m.name)

            # only listening mappings (server/udp) bind a port; client/serialbridge don't
            if m.kind != "net" or not m.network.listens:
                continue
            # A TCP listener and a UDP listener CAN share the same port number (they
            # bind different transports), so the clash key includes the transport.
            proto = "udp" if m.network.mode == "udp" else "tcp"
            # 0.0.0.0 collides with everything on that port; treat any overlap as a clash.
            for (sproto, bip, bport), other in seen.items():
                if sproto == proto and bport == m.network.port and _ip_overlaps(bip, m.network.bind_ip):
                    raise ConfigError(
                        f"Mappings '{other}' and '{m.name}' both use {proto.upper()} port "
                        f"{m.network.port} on overlapping addresses ({bip} / {m.network.bind_ip})."
                    )
            seen[(proto, m.network.bind_ip, m.network.port)] = m.name


def _ip_overlaps(a: str, b: str) -> bool:
    if a == b:
        return True
    wildcards = {"0.0.0.0", "::"}
    return a in wildcards or b in wildcards


# ---------------------------------------------------------------------------
# Atomic store
# ---------------------------------------------------------------------------
class ConfigStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        # config.json holds the password hash + secret_key; keep the dir private on
        # BOTH platforms (POSIX chmod 0700 / Windows icacls owner-only inheritance).
        lock_down_dir(directory)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> AppConfig:
        if not self.exists():
            return AppConfig()
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return AppConfig.from_dict(data)

    def save(self, config: AppConfig) -> None:
        """Atomically persist config to a single file (validates first).

        Uses a temp file + os.replace so config.json is never partially written.
        No .bak is kept: config.json (and all.log) are the only state files, so
        deleting them fully resets the system.
        """
        config.validate()
        data = json.dumps(config.to_dict(), indent=2, ensure_ascii=False)
        directory = os.path.dirname(self.path)

        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cfg.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        # fsync the directory so the rename is durable
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except (OSError, AttributeError):
            pass  # not supported on some platforms (e.g. Windows dirs)
