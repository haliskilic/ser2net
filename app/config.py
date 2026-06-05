"""Configuration data model + atomic JSON persistence for pyser2net.

The model is deliberately split into explicit typed fields (borrowed from
ser2net's connection model) rather than gensio stack-strings, so the web UI can
present every option directly and validate it.

Persistence is plain stdlib JSON with an atomic write (tempfile -> fsync ->
os.replace -> parent-dir fsync) so a crash mid-write can never truncate dozens of
mappings. config.json is the single source of truth (no .bak); deleting it resets
all config.
"""
from __future__ import annotations

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
PROTOCOLS = ("raw", "telnet", "rfc2217")
COMMON_BAUDRATES = (
    300, 1200, 2400, 4800, 9600, 19200, 38400, 57600,
    115200, 230400, 460800, 921600,
)


class ConfigError(ValueError):
    """Raised when a config object fails validation. Message is user-facing."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


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
@dataclass
class NetworkSettings:
    protocol: str = "raw"  # raw | telnet | rfc2217
    bind_ip: str = "0.0.0.0"
    port: int = 4001
    max_connections: int = 1
    kick_old_user: bool = False
    allowed_client_ips: list[str] = field(default_factory=list)
    # High-priority clients: when the port is at capacity, a connection from one of
    # these IPs/CIDRs is admitted by kicking an existing client (oldest non-priority
    # first), regardless of kick_old_user.
    priority_client_ips: list[str] = field(default_factory=list)
    read_only: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "NetworkSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(NetworkSettings)}
        return NetworkSettings(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ConfigError(f"Protocol must be one of {PROTOCOLS}.")
        try:
            ip = ipaddress.ip_address(self.bind_ip)
        except ValueError:
            raise ConfigError(f"Invalid bind IP: {self.bind_ip!r}.")
        # 0.0.0.0 and :: are the "all interfaces" wildcards and must be allowed even
        # though :: reports is_reserved=True.
        if ip.is_multicast or (ip.is_reserved and str(ip) not in ("0.0.0.0", "::")):
            raise ConfigError("Bind IP cannot be a multicast/reserved address.")
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ConfigError("TCP port must be between 1 and 65535.")
        if not isinstance(self.max_connections, int) or self.max_connections < 1:
            raise ConfigError("Max connections must be >= 1.")
        if self.protocol == "rfc2217" and self.max_connections > 1:
            raise ConfigError(
                "RFC2217 supports a single connection (clients share one serial "
                "port's settings). Set Max connections = 1."
            )
        for label, lst in (("allowed", self.allowed_client_ips),
                           ("priority", self.priority_client_ips)):
            for cidr in lst:
                try:
                    ipaddress.ip_network(normalize_cidr(cidr), strict=False)
                except ValueError:
                    raise ConfigError(f"Invalid {label} client IP/CIDR: {cidr!r}.")


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

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingOptions":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(MappingOptions)}
        return MappingOptions(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# A single serial<->TCP mapping
# ---------------------------------------------------------------------------
@dataclass
class MappingConfig:
    id: str = field(default_factory=_new_id)
    name: str = ""
    enabled: bool = True
    serial: SerialSettings = field(default_factory=SerialSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    options: MappingOptions = field(default_factory=MappingOptions)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingConfig":
        d = dict(d or {})
        return MappingConfig(
            id=d.get("id") or _new_id(),
            name=d.get("name", ""),
            enabled=bool(d.get("enabled", True)),
            serial=SerialSettings.from_dict(d.get("serial", {})),
            network=NetworkSettings.from_dict(d.get("network", {})),
            options=MappingOptions.from_dict(d.get("options", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError("Mapping name is required.")
        self.serial.validate()
        self.network.validate()


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


@dataclass
class AppConfig:
    version: int = 1
    admin_ui: AdminUI = field(default_factory=AdminUI)
    password_hash: str = ""  # empty => not set yet (first-run setup pending)
    pwd_version: int = 0      # bumped on every password change to revoke old sessions
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    session_timeout_s: int = 8 * 3600
    defaults: dict[str, Any] = field(default_factory=dict)  # serial defaults
    mappings: list[MappingConfig] = field(default_factory=list)

    # ----- (de)serialization -----
    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AppConfig":
        d = dict(d or {})
        admin = d.get("admin_ui", {}) or {}
        known = {f.name for f in dataclasses.fields(AdminUI)}
        admin_ui = AdminUI(**{k: v for k, v in admin.items() if k in known})
        cfg = AppConfig(
            version=int(d.get("version", 1)),
            admin_ui=admin_ui,
            password_hash=d.get("password_hash", ""),
            pwd_version=int(d.get("pwd_version", 0)),
            secret_key=d.get("secret_key") or secrets.token_hex(32),
            session_timeout_s=int(d.get("session_timeout_s", 8 * 3600)),
            defaults=d.get("defaults", {}) or {},
            mappings=[MappingConfig.from_dict(m) for m in d.get("mappings", [])],
        )
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "admin_ui": asdict(self.admin_ui),
            "password_hash": self.password_hash,
            "pwd_version": self.pwd_version,
            "secret_key": self.secret_key,
            "session_timeout_s": self.session_timeout_s,
            "defaults": self.defaults,
            "mappings": [m.to_dict() for m in self.mappings],
        }

    # ----- helpers -----
    @property
    def password_set(self) -> bool:
        return bool(self.password_hash)

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

        seen: dict[tuple[str, int], str] = {}
        for m in self.mappings:
            m.validate()
            key = (m.network.bind_ip, m.network.port)
            # 0.0.0.0 collides with everything on that port; treat any overlap as a clash.
            for (bip, bport), other in seen.items():
                if bport == m.network.port and _ip_overlaps(bip, m.network.bind_ip):
                    raise ConfigError(
                        f"Mappings '{other}' and '{m.name}' both use port {m.network.port} "
                        f"on overlapping addresses ({bip} / {m.network.bind_ip})."
                    )
            seen[key] = m.name


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
        # config.json holds the password hash + secret_key; keep the dir private.
        # (config.json itself is written 0600 via tempfile.mkstemp + os.replace.)
        if os.name == "posix":
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

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
