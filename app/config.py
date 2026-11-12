"""Configuration data model + atomic JSON persistence for ser2net.

The model is deliberately split into explicit typed fields (borrowed from
ser2net's connection model) rather than gensio stack-strings, so the web UI can
present every option directly and validate it.

Persistence is plain stdlib JSON with an atomic write (tempfile -> fsync ->
os.replace -> parent-dir fsync) so a crash mid-write can never truncate dozens of
mappings. config.json is the single source of truth; earlier versions are kept in
backups/ (ConfigStore) but never read back unless an admin restores one, so deleting
config.json still resets all config.
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from .i18n import _

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
MAX_TIMER_S = 365 * 86400  # upper bound for the per-connection timers (one year)
# web-UI sign-in session lifetime (AppConfig.session_timeout_s)
SESSION_TIMEOUT_DEFAULT_S = 8 * 3600
SESSION_TIMEOUT_MIN_S = 5 * 60
SESSION_TIMEOUT_MAX_S = 30 * 86400
# LDAP/OIDC accounts not signed in for this many days are removed (AppConfig
# .external_users_expire_days, app/signins.py); 0 = never, the default: an admin turns it
# on. At least the longest session lifetime, so no live session ends that way.
EXPIRE_DAYS_DEFAULT = 0
EXPIRE_DAYS_MIN = SESSION_TIMEOUT_MAX_S // 86400
EXPIRE_DAYS_MAX = 3650


class ConfigError(ValueError):
    """Raised when a config object fails validation. Message is user-facing."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# A mapping id names the mapping's own files (logs/<id>.log, traces/<id>-<name>) and is
# part of URLs, so it is 1-64 ASCII letters, digits or "_": never a path separator, ".",
# ":" or NUL, and no "-" either (a mapping's traces are found by their "<id>-" prefix,
# so an id "a" must not also match the traces of a mapping "a-b"). Generated ids
# (_new_id) are 12 hex digits.
_MAPPING_ID = re.compile(r"[A-Za-z0-9_]{1,64}")
# device names Windows reserves, with any extension too (logs/NUL.log is the NUL device)
_WINDOWS_DEVICE_NAME = re.compile(r"(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])")


def valid_mapping_id(value: Any) -> bool:
    """True for a mapping id that is safe as a file name and in a URL (see _MAPPING_ID)."""
    return (isinstance(value, str) and _MAPPING_ID.fullmatch(value) is not None
            and _WINDOWS_DEVICE_NAME.fullmatch(value) is None)


def _mapping_id_from(value: Any) -> str:
    """The id of a mapping read from a config file, an import or a form: the value when it
    is a valid id, a new one when there is none, else one derived from the value. The
    derived id is the same on every load, so the mapping keeps its log and traces even
    when the config is not saved again."""
    if valid_mapping_id(value):
        return value
    if not value:
        return _new_id()
    return hashlib.sha256(repr(value).encode("utf-8", "backslashreplace")).hexdigest()[:12]


def mapping_file(directory: str, mapping_id: Any, suffix: str = "") -> str:
    """<directory>/<mapping id><suffix>: a mapping's own file, its log (logs/<id>.log) or a
    trace (traces/<id>-<name>). Raises ValueError for an invalid mapping id, or when the
    result would not be a file directly in `directory`, so no id or name can point
    anywhere else."""
    if not valid_mapping_id(mapping_id):
        raise ValueError(f"not a valid mapping id: {mapping_id!r}")
    name = f"{mapping_id}{suffix}"
    path = os.path.join(directory, name)
    if (os.path.basename(name) != name
            or os.path.dirname(os.path.abspath(path)) != os.path.abspath(directory)):
        raise ValueError(f"not a file in {directory}: {name!r}")
    return path


def lock_down_dir(directory: str) -> None:
    """Restrict a state directory to its owner so the secrets it holds (secret_key,
    password hash in config.json) and captured serial logs are not readable by other
    local users.

    POSIX: chmod 0700. Windows: ``icacls`` removes inherited ACEs and grants Full
    only to the current user, SYSTEM and the Administrators group, with inheritance
    so child files (config.json, the atomic temp files, per-mapping logs) pick up the
    same restriction. Best-effort: silently degrades if it cannot be applied (the
    posix path was the only one protected before — Windows was left world-readable
    despite the docs claiming 0600). A service's data directory as --install-service
    leaves it (only SYSTEM and Administrators can use it, see winservice.service_folder)
    is left as it is: an elevated run (--set-password) must not give its own account's
    unelevated programs a way to change what the service runs. Any other folder gets
    the grant for the current user, also one an elevated administrator created private
    to itself, so that account can still use it unelevated.
    """
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        return
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if not user or _service_folder(directory):
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
        # by full path: Windows searches an unqualified name in the program's own
        # folder and the current directory before System32, so an icacls.exe planted
        # next to ser2net.exe (e.g. in Downloads) would otherwise run instead
        root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        icacls = os.path.join(root, "System32", "icacls.exe")
        cmd = [icacls if os.path.isfile(icacls) else "icacls", directory, "/inheritance:r"]
        for g in grants:
            cmd += ["/grant:r", g]
        cmd += ["/C", "/Q"]
        with contextlib.suppress(Exception):
            subprocess.run(cmd, capture_output=True, timeout=15, check=False)


def _service_folder(directory: str) -> bool:
    """Windows: `directory` is a service's data directory as --install-service leaves it,
    or inherits from one (False when that can't be told)."""
    from . import winservice  # the Windows API through ctypes

    try:
        sec = winservice.read_security(directory)
    except Exception:  # best effort, like the rest of lock_down_dir
        return False
    return sec is not None and winservice.service_folder(*sec)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _legacy_seconds(value: Any) -> Any:
    """Normalize a timer that predates validation (idle_timeout_s). Before v2.7 the
    REST API / import stored it unchecked and anything <= 0 meant "off", so an old
    config may hold -1 or a numeric string. Map those onto the validated range rather
    than refuse to start: a headless/Docker boot re-saves (= validates) the config."""
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return 0
        if value.is_integer():
            value = int(value)
    if not _is_number(value) or not math.isfinite(value) or value < 0:
        return 0
    return min(value, MAX_TIMER_S)


TRACE_DEFAULT_NAME = "trace.log"
# letters/digits of any script, "_", "." and "-"; path separators and the characters
# Windows forbids become "_" (the <id>- prefix also rules out reserved names like CON)
_TRACE_NAME_BAD = re.compile(r"[^\w.-]+")


def trace_file_name(value: Any) -> str:
    """The file name a mapping's trace is written to, inside the data directory's
    traces/ folder. Before v2.8 ``trace_both`` was a free path that any operator could
    point at any file the service could write (config.json, a startup script...);
    such a value now only contributes its last path component, sanitised. '' = off."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    name = re.split(r"[\\/]", raw)[-1]
    name = _TRACE_NAME_BAD.sub("_", name).strip(". ")[:80].rstrip(". ")
    return name or TRACE_DEFAULT_NAME


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
            raise ConfigError(_("Serial port is required."))
        if not isinstance(self.baudrate, int) or self.baudrate <= 0:
            raise ConfigError(_("Baud rate must be a positive integer."))
        if self.bytesize not in BYTESIZES:
            raise ConfigError(_("Data bits must be one of {0}.").format(BYTESIZES))
        if self.parity not in PARITIES:
            raise ConfigError(_("Parity must be one of {0}.").format(PARITIES))
        if self.stopbits not in STOPBITS:
            raise ConfigError(_("Stop bits must be one of {0}.").format(STOPBITS))
        if self.flowcontrol not in FLOWCONTROLS:
            raise ConfigError(_("Flow control must be one of {0}.").format(FLOWCONTROLS))
        if self.rts_on_open not in LINE_STATES or self.dtr_on_open not in LINE_STATES:
            raise ConfigError(_("RTS/DTR on open must be on|off|keep."))
        # rtscts (hardware) flow control conflicts with RS-485 RTS direction toggling.
        if self.advanced.rs485_enabled and self.flowcontrol == "rtscts":
            raise ConfigError(_("RS-485 RTS toggling conflicts with RTS/CTS flow control."))

    def compact(self) -> str:
        """ser2net-style compact descriptor, e.g. '9600N81'."""
        return f"{self.baudrate}{self.parity}{self.bytesize}{int(self.stopbits)}"


def url_port(port: Any) -> bool:
    """True for a serial port value that pyserial would open with a URL handler instead
    of as a device: anything with "://" (socket://, rfc2217://, spy://, loop://, alt://,
    hwgrep:// ...), which is what pyserial's serial_for_url dispatches on. A mapping only
    opens serial devices (COM3, \\\\.\\COM10, /dev/ttyUSB0, /dev/serial/by-id/...)."""
    return isinstance(port, str) and "://" in port


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
    # client mode: the connect-out target (UDP does not use these: see udp_peer_host)
    remote_host: str = ""
    remote_port: int = 0
    # UDP: where the device's output goes. Blank (default): to the sender of the last
    # datagram from an allowed IP, so a new sender takes it over. With a reply address
    # (an IP and a port) it always goes there, whoever sends.
    udp_peer_host: str = ""
    udp_peer_port: int = 0
    # UDP without a reply address: forget the last sender after this many seconds
    # without a datagram from it (the output then goes nowhere until someone sends);
    # 0 = never.
    udp_peer_timeout_s: int = 0
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
    # Dead-peer detection for TCP (server + client mode): after keepalive_idle_s of
    # silence the OS probes the peer every keepalive_interval_s and drops the link
    # after keepalive_count unanswered probes. A client that vanished without a
    # FIN/RST (power cut, cable pull, VPN/NAT drop) is then reaped in about
    # idle + interval * count seconds instead of holding its slot forever.
    tcp_keepalive: bool = True
    keepalive_idle_s: int = 60
    keepalive_interval_s: int = 10
    keepalive_count: int = 3
    # Client (connect-out) mode: abandon a TCP (+TLS) connect attempt after this many
    # seconds and retry. 0 = no limit of our own (the OS gives up after ~21 s on
    # Windows, ~2 min on Linux).
    connect_timeout_s: float = 10.0
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
            raise ConfigError(_("Network mode must be one of {0}.").format(NET_MODES))
        if self.protocol not in PROTOCOLS:
            raise ConfigError(_("Protocol must be one of {0}.").format(PROTOCOLS))

        if self.listens:
            try:
                ip = ipaddress.ip_address(self.bind_ip)
            except ValueError:
                raise ConfigError(_("Invalid bind IP: {0}.").format(repr(self.bind_ip))) from None
            # 0.0.0.0 and :: are the "all interfaces" wildcards and must be allowed
            # even though :: reports is_reserved=True.
            if ip.is_multicast or (ip.is_reserved and str(ip) not in ("0.0.0.0", "::")):
                raise ConfigError(_("Bind IP cannot be a multicast/reserved address."))
            if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
                raise ConfigError(_("TCP/UDP port must be between 1 and 65535."))
        else:  # client (connect-out)
            if not self.remote_host.strip():
                raise ConfigError(_("Connect-out mode requires a remote host."))
            if not isinstance(self.remote_port, int) or not (1 <= self.remote_port <= 65535):
                raise ConfigError(_("Remote port must be between 1 and 65535."))

        if not isinstance(self.max_connections, int) or self.max_connections < 1:
            raise ConfigError(_("Max connections must be >= 1."))
        if not isinstance(self.client_queue_max, int) or self.client_queue_max < 16:
            raise ConfigError(_("Client queue size must be an integer >= 16."))
        # OS limits: Linux caps TCP_KEEPIDLE/TCP_KEEPINTVL at 32767 s and TCP_KEEPCNT at 127.
        for label, value, high in ((_("Keepalive idle time"), self.keepalive_idle_s, 32767),
                                   (_("Keepalive probe interval"), self.keepalive_interval_s, 32767),
                                   (_("Keepalive probe count"), self.keepalive_count, 127)):
            if not _is_int(value) or not (1 <= value <= high):
                raise ConfigError(_("{0} must be an integer between 1 and {1}.").format(label, high))
        if not _is_number(self.connect_timeout_s) or not (0 <= self.connect_timeout_s <= 3600):
            raise ConfigError(_("Connect timeout must be between 0 and 3600 seconds (0 = OS "
                                "default)."))
        if self.protocol == "rfc2217" and self.mode == "udp":
            raise ConfigError(_("RFC2217 requires a TCP (server/client) connection, not UDP."))
        if self.protocol == "modbus" and self.mode != "server":
            raise ConfigError(_("Modbus gateway requires TCP server mode (masters connect in)."))
        if self.protocol == "rfc2217" and self.max_connections > 1:
            raise ConfigError(
                _("RFC2217 supports a single connection (clients share one serial port's "
                  "settings). Set Max connections = 1.")
            )
        if self.tls:
            if self.mode == "udp":
                raise ConfigError(_("TLS is not applicable to UDP mappings."))
            if not (self.tls_cert and self.tls_key):
                raise ConfigError(_("Enable TLS requires both a certificate and a key."))
            for p in (self.tls_cert, self.tls_key):
                if not os.path.isfile(p):
                    raise ConfigError(_("TLS file not found: {0}").format(p))
        for label, lst in ((_("allowed"), self.allowed_client_ips),
                           (_("priority"), self.priority_client_ips)):
            for cidr in lst:
                try:
                    ipaddress.ip_network(normalize_cidr(cidr), strict=False)
                except ValueError:
                    raise ConfigError(_("Invalid {0} client IP/CIDR: {1}.").format(label, repr(cidr))) from None


def udp_peer_address(net: NetworkSettings) -> Optional[tuple[str, int]]:
    """The reply address of a UDP mapping (udp_peer_host, udp_peer_port): where the
    device's output always goes, or None when none is set (the last sender gets it).
    Raises ValueError when it is not a host's IP address and a port; edits refuse that
    (MappingConfig.validate_input), so only a hand-edited config has it."""
    host = str(net.udp_peer_host or "").strip()
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"UDP reply address {host!r} is not an IP address") from None
    if ip.is_unspecified:
        raise ValueError(f"UDP reply address {host} is not a host's address")
    port = net.udp_peer_port
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ValueError(f"UDP reply port {port!r} is not between 1 and 65535")
    return host, port


def udp_peer_timeout(net: NetworkSettings) -> float:
    """A UDP mapping's peer timeout in seconds, 0 = never. A stored value that is not a
    number of seconds reads as 0, today's behaviour (edits refuse one: validate_input)."""
    t = net.udp_peer_timeout_s
    if _is_number(t) and math.isfinite(t) and t > 0:
        return float(min(t, MAX_TIMER_S))
    return 0.0


# ---------------------------------------------------------------------------
# Per-mapping options
# ---------------------------------------------------------------------------
@dataclass
class MappingOptions:
    banner: str = ""
    openstr: str = ""
    closestr: str = ""
    closeon: str = ""
    # Per-connection timers for TCP clients (0 = off): drop a client after this many
    # seconds without traffic either way / after this long connected, active or not
    # (a connect-out mapping then reconnects).
    idle_timeout_s: int = 0
    max_connection_time_s: int = 0
    # Traffic trace: a file NAME (empty = off), written as data/traces/<id>-<name>
    # (see trace_file_name). Rotated past trace_max_mb, keeping trace_keep old files.
    trace_both: str = ""
    trace_hexdump: bool = False
    trace_timestamp: bool = True
    trace_max_mb: float = 10.0
    trace_keep: int = 3
    # RFC2217 interop knobs for non-compliant peers:
    rfc2217_poll_modem_interval_s: float = 1.0
    rfc2217_net_timeout_s: float = 3.0
    # RFC2217: once the client has left, put the serial port back to this mapping's baud
    # rate, data bits, parity, stop bits and flow control (off: what the client set stays
    # until the port is reopened). RTS/DTR are left alone: toggling them can reset a device.
    rfc2217_restore_on_disconnect: bool = False
    # Modbus gateway: how long to wait for an RTU slave's reply before returning a
    # Modbus exception (0x0B gateway-target-failed) to the TCP master.
    modbus_response_timeout_s: float = 1.0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingOptions":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(MappingOptions)}
        opts = MappingOptions(**{k: v for k, v in d.items() if k in known})
        opts.idle_timeout_s = _legacy_seconds(opts.idle_timeout_s)
        opts.trace_both = trace_file_name(opts.trace_both)
        return opts

    def validate(self) -> None:
        for label, value in ((_("Idle timeout"), self.idle_timeout_s),
                             (_("Max connection time"), self.max_connection_time_s)):
            if not _is_number(value) or not (0 <= value <= MAX_TIMER_S):
                raise ConfigError(_("{0} must be between 0 (off) and {1} seconds.").format(label, MAX_TIMER_S))
        if not _is_number(self.trace_max_mb) or not (0.1 <= self.trace_max_mb <= 1024):
            raise ConfigError(_("Trace file size must be between 0.1 and 1024 MB."))
        if not _is_int(self.trace_keep) or not (0 <= self.trace_keep <= 50):
            raise ConfigError(_("Rotated trace files kept must be between 0 and 50."))


# ---------------------------------------------------------------------------
# Per-mapping MQTT publishing (optional northbound to an IIoT broker)
# ---------------------------------------------------------------------------
MQTT_FORMATS = ("plain", "sparkplug_b")


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
    # Modbus polling only: "plain" topics per point, or Eclipse Sparkplug B (this mapping
    # is an edge node spBv1.0/<group>/N*/<node>; blank node id = the mapping's name)
    payload_format: str = "plain"
    sparkplug_group_id: str = "ser2net"
    sparkplug_node_id: str = ""
    # Plain-format commands: also write a <base_topic>/<point>/set that the broker sends
    # from its retained store after each (re)connect. Off: a command is written once, when
    # it is published (Sparkplug NCMD / DCMD are never retained, so a retained one is
    # always ignored).
    apply_retained_set: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MqttSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(MqttSettings)}
        return MqttSettings(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.host.strip():
            raise ConfigError(_("MQTT is enabled but no broker host is set."))
        if not self.base_topic.strip():
            raise ConfigError(_("MQTT is enabled but no base topic is set."))
        if not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ConfigError(_("MQTT port must be between 1 and 65535."))
        if self.qos not in (0, 1, 2):
            raise ConfigError(_("MQTT QoS must be 0, 1 or 2."))
        if self.payload_format not in MQTT_FORMATS:
            raise ConfigError(_("MQTT payload format must be one of {0}.").format(MQTT_FORMATS))
        if self.payload_format == "sparkplug_b":
            for label, value in ((_("group ID"), self.sparkplug_group_id),
                                 (_("edge node ID"), self.sparkplug_node_id)):
                if any(c in value for c in "/+#"):
                    raise ConfigError(_("Sparkplug {0} must not contain '/', '+' or '#'.").format(label))
            if not self.sparkplug_group_id.strip():
                raise ConfigError(_("Sparkplug B needs a group ID."))


# ---------------------------------------------------------------------------
# Modbus register polling (gateway edge mode: read registers -> MQTT)
# ---------------------------------------------------------------------------
MODBUS_DTYPES = ("uint16", "int16", "uint32", "int32", "float32", "bool")


@dataclass
class ModbusPoint:
    name: str = ""
    unit: int = 1               # RTU slave id
    fn: int = 3                 # 1 = coils, 2 = discrete inputs, 3 = holding, 4 = input registers
    address: int = 0            # 0-based coil / register address
    dtype: str = "uint16"       # uint16 | int16 | uint32 | int32 | float32 (registers), bool (bits)
    scale: float = 1.0          # published value = raw * scale (0 = no scaling, as before)
    # MQTT may write it: <base_topic>/<name>/set, or a Sparkplug B DCMD to its unit's
    # device (a coil: FC5, a holding register: FC6 / FC16 for its own one or two
    # registers). Only coils and holding registers.
    writable: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ModbusPoint":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(ModbusPoint)}
        return ModbusPoint(**{k: v for k, v in d.items() if k in known})


@dataclass
class ModbusPoll:
    interval_s: float = 5.0
    points: list[ModbusPoint] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ModbusPoll":
        d = dict(d or {})
        return ModbusPoll(
            interval_s=float(d.get("interval_s", 5.0) or 5.0),
            points=[ModbusPoint.from_dict(p) for p in d.get("points", [])],
        )

    def validate(self) -> None:
        if not self.points:
            return
        if self.interval_s <= 0:
            raise ConfigError(_("Modbus poll interval must be > 0 seconds."))
        seen = set()
        for p in self.points:
            if not p.name.strip():
                raise ConfigError(_("Each Modbus poll point needs a name."))
            if p.name in seen:
                raise ConfigError(_("Duplicate Modbus poll point name: {0}.").format(repr(p.name)))
            seen.add(p.name)
            if not (0 <= p.unit <= 255):
                raise ConfigError(_("Point {0}: unit must be 0-255.").format(repr(p.name)))
            if p.fn not in (1, 2, 3, 4):
                raise ConfigError(_("Point {0}: function must be 1 (coils), 2 (discrete "
                                    "inputs), 3 (holding registers) or 4 (input registers).").format(repr(p.name)))
            if not (0 <= p.address <= 65535):
                raise ConfigError(_("Point {0}: address must be 0-65535.").format(repr(p.name)))
            if p.dtype not in MODBUS_DTYPES:
                raise ConfigError(_("Point {0}: dtype must be one of {1}.").format(repr(p.name), MODBUS_DTYPES))
            if (p.fn in (1, 2)) != (p.dtype == "bool"):
                raise ConfigError(_("Point {0}: coils / discrete inputs (fn 1, 2) use dtype "
                                    "'bool'; registers (fn 3, 4) a numeric dtype.").format(repr(p.name)))
            if p.writable and p.fn not in (1, 3):
                raise ConfigError(_("Point {0}: only coils (fn 1) and holding registers (fn 3) "
                                    "can be written.").format(repr(p.name)))


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
    modbus_poll: ModbusPoll = field(default_factory=ModbusPoll)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingConfig":
        # every way into the config (file, import, form, REST, cluster) passes here, so an
        # id that is not a valid mapping id never gets further (see _mapping_id_from)
        d = dict(d or {})
        return MappingConfig(
            id=_mapping_id_from(d.get("id")),
            name=d.get("name", ""),
            enabled=bool(d.get("enabled", True)),
            kind=d.get("kind", "net"),
            serial=SerialSettings.from_dict(d.get("serial", {})),
            serial_b=SerialSettings.from_dict(d.get("serial_b", {})),
            network=NetworkSettings.from_dict(d.get("network", {})),
            options=MappingOptions.from_dict(d.get("options", {})),
            mqtt=MqttSettings.from_dict(d.get("mqtt", {})),
            modbus_poll=ModbusPoll.from_dict(d.get("modbus_poll", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError(_("Mapping name is required."))
        if self.kind not in MAPPING_KINDS:
            raise ConfigError(_("Mapping kind must be one of {0}.").format(MAPPING_KINDS))
        self.serial.validate()
        if self.kind == "serialbridge":
            self.serial_b.validate()
            if self.serial_b.port == self.serial.port:
                raise ConfigError(_("Serial-to-serial bridge needs two different ports."))
        else:
            self.network.validate()
        self.options.validate()
        self.mqtt.validate()
        self.modbus_poll.validate()
        if self.modbus_poll.points and not (self.kind == "net" and self.network.protocol == "modbus"):
            raise ConfigError(_("Modbus register polling requires a Modbus-gateway mapping "
                                "(protocol = modbus)."))
        if (self.mqtt.enabled and self.mqtt.payload_format == "sparkplug_b"
                and not self.modbus_poll.points):
            raise ConfigError(_("Sparkplug B publishes Modbus poll points: add points (and use "
                                "protocol = modbus), or use the plain MQTT format."))

    def serial_sides(self) -> list[SerialSettings]:
        """The serial ports this mapping opens: A, and B for a serial-to-serial bridge."""
        return [self.serial, self.serial_b] if self.kind == "serialbridge" else [self.serial]

    @property
    def accepts_mqtt_writes(self) -> bool:
        """MQTT commands can write to the bus: a Modbus gateway with MQTT on, writable
        points and Read-only off. Who may send them (<base_topic>/<point>/set, or the
        Sparkplug NCMD / DCMD topics) is up to the broker's access rules."""
        return (self.kind == "net" and self.network.protocol == "modbus" and self.mqtt.enabled
                and not self.network.read_only
                and any(p.writable for p in self.modbus_poll.points))

    def validate_input(self) -> None:
        """The rules for a mapping that is being created or changed (web form, REST API,
        import, cluster), checked there in addition to validate(). validate() must not
        refuse them: a stored config has to keep loading and saving (Docker re-saves it on
        every start), so a stored value they forbid is refused where it is used instead
        (a URL serial port: when the mapping starts, see engine/serial_io.refuse_url)."""
        for side in self.serial_sides():
            if url_port(side.port):
                raise ConfigError(_("Serial port {0} is a URL, not a device: ser2net opens serial "
                                    "devices only (URLs such as socket:// or spy:// are not "
                                    "supported).").format(side.port))
        if self.kind == "net" and self.network.mode == "udp":
            self._validate_udp_peer()

    def _validate_udp_peer(self) -> None:
        """A UDP mapping's reply address and peer timeout (an edit's rules: a stored
        malformed reply address makes the mapping fail to start, see udp_peer_address)."""
        net = self.network
        host = str(net.udp_peer_host or "").strip()
        if host:
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                ip = None
            if ip is None or ip.is_unspecified:
                raise ConfigError(_("The UDP reply address must be a host's IP address, not "
                                    "{0}.").format(repr(host)))
            if not _is_int(net.udp_peer_port) or not (1 <= net.udp_peer_port <= 65535):
                raise ConfigError(_("The UDP reply port must be between 1 and 65535."))
            try:
                bind_v4 = ipaddress.ip_address(net.bind_ip).version == 4
            except ValueError:
                bind_v4 = False  # validate() reports a bad bind IP
            if ip.version == 6 and bind_v4:
                raise ConfigError(_("The UDP reply address {0} is IPv6, but the mapping listens "
                                    "on an IPv4 address ({1}).").format(host, net.bind_ip))
        timeout = net.udp_peer_timeout_s
        if not _is_number(timeout) or not (0 <= timeout <= MAX_TIMER_S):
            raise ConfigError(_("{0} must be between 0 (off) and {1} seconds.").format(
                _("Peer timeout"), MAX_TIMER_S))


# ---------------------------------------------------------------------------
# Global application config
# ---------------------------------------------------------------------------
@dataclass
class AdminUI:
    bind_ip: str = "127.0.0.1"
    port: int = 8080
    tls_cert: str = ""
    tls_key: str = ""
    # Web-UI login can be switched off from Settings (trusted networks only): visitors
    # without a session then act with anonymous_role. Signing in still works, and the
    # REST API keeps its bearer token.
    require_login: bool = True
    anonymous_role: str = "viewer"
    # web UI language when a visitor hasn't picked one: auto (the browser's), en, tr
    language: str = "auto"
    # Connections to the UI's listener: at most this many open at once in all, and from
    # one source address (0 = no limit; config.json only). A connection must send a
    # complete request head within request_timeout_s of connecting, and of the end of the
    # previous response (0 = no limit), then its body, which gets that time plus a second
    # per KiB of it that comes in; a client that takes nothing of a response for
    # request_timeout_s is dropped too. A WebSocket (the console) is not timed.
    max_connections: int = 256
    max_connections_per_ip: int = 64
    request_timeout_s: int = 10

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)


# admin_ui connection limits: (default, lowest value other than 0, highest)
_ADMIN_LIMITS = {"max_connections": (256, 16, 65535),
                 "max_connections_per_ip": (64, 8, 65535),
                 "request_timeout_s": (10, 1, 3600)}


def _normalize_admin_limits(admin: "AdminUI", notes: list[str]) -> None:
    """Put an admin_ui connection limit that is not a usable whole number back to its
    default (0, no limit, stays 0; a value below the lowest goes up to it), with a load
    note: a hand-edited config.json must not keep the UI from starting, nor make it unusable."""
    for name, (default, low, high) in _ADMIN_LIMITS.items():
        value = getattr(admin, name)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
            fixed = default
        elif 0 < value < low:
            fixed = low
        else:
            continue
        setattr(admin, name, fixed)
        notes.append(f"admin_ui.{name} {value!r} is not usable (0 = no limit, else "
                     f"{low}-{high}); using {fixed}")


UI_LANGUAGES = ("auto", "en", "tr")
ROLES = ("admin", "operator", "viewer")
# higher rank = more privilege; a route requires >= some rank
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass
class LdapSettings:
    """LDAP / Active Directory authentication. Users authenticate by binding to the
    directory; their group membership maps to a ser2net role. Two bind modes:
      - direct: user_dn_template (e.g. 'uid={username},ou=people,dc=ex,dc=com' or the
        AD UPN '{username}@corp.local'); the credentials bind as that DN. An AD sign-in
        name (user@domain, DOMAIN\\user) isn't a DN: the account is then found by
        userPrincipalName / sAMAccountName in its domain, under user_search_base if set.
      - search+bind: bind as the service account (bind_dn/bind_password), find the user
        with user_search_filter under user_search_base, then re-bind as that DN.
    Group->role: a user in admin_group gets admin, operator_group -> operator, etc.;
    default_role applies when authenticated but in no mapped group (empty => deny). A
    group setting is a group's whole name (its CN) or DN, compared without regard to
    letter case (ldap_auth.group_matches). nested_groups (Active Directory) also
    counts the groups a user is in through other groups.
    TLS (ldaps:// or StartTLS): the server's certificate must chain to a CA the system
    trusts — or, with ca_file set, to one in that file only (PEM or DER) — and name the
    host in server_uri (ldap_auth.tls_context). tls_verify=False skips the check
    (insecure)."""
    enabled: bool = False
    server_uri: str = ""             # ldap://host:389 or ldaps://host:636
    start_tls: bool = False
    tls_verify: bool = True          # check the server's certificate (LDAPS / StartTLS)
    ca_file: str = ""                # CA certificate(s) to trust instead of the system's
    user_dn_template: str = ""       # direct-bind template (mutually exclusive with search)
    bind_dn: str = ""                # service account for search+bind
    bind_password: str = ""
    user_search_base: str = ""
    user_search_filter: str = "(uid={username})"   # AD: (sAMAccountName={username})
    group_attr: str = "memberOf"     # attribute on the user entry listing group DNs
    admin_group: str = ""
    operator_group: str = ""
    viewer_group: str = ""
    default_role: str = ""           # role when no group matched; "" => deny login
    nested_groups: bool = False      # AD: LDAP_MATCHING_RULE_IN_CHAIN group search

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "LdapSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(LdapSettings)}
        return LdapSettings(**{k: v for k, v in d.items() if k in known})

    @property
    def uses_tls(self) -> bool:
        """ldaps://, or StartTLS: the bind passwords travel encrypted."""
        return self.server_uri.strip().lower().startswith("ldaps://") or bool(self.start_tls)

    @property
    def sends_plaintext(self) -> bool:
        """Passwords over the network unencrypted: neither ldaps:// nor StartTLS, and no
        ldapi:// (a local socket)."""
        return not (self.uses_tls or self.server_uri.strip().lower().startswith("ldapi://"))

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.server_uri.strip():
            raise ConfigError(_("LDAP is enabled but no server URI is set."))
        if not (self.user_dn_template.strip() or
                (self.bind_dn.strip() and self.user_search_base.strip())):
            raise ConfigError(_("LDAP needs either a user DN template (direct bind) or a bind "
                                "DN + user search base (search+bind)."))
        if self.default_role and self.default_role not in ROLES:
            raise ConfigError(_("LDAP default role must be one of {0} or blank.").format(ROLES))


@dataclass
class OidcSettings:
    """OpenID Connect single sign-on (authorization-code flow). Endpoints are
    discovered from the issuer's /.well-known/openid-configuration. A claim
    (groups_claim) maps to a ser2net role, like the LDAP group mapping: a group setting
    is the whole value, or its last part (/team/name, DOMAIN\\name)."""
    enabled: bool = False
    issuer: str = ""                 # e.g. https://accounts.google.com or a Keycloak realm URL
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""           # blank => derived from the request
    scopes: str = "openid email profile"
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    admin_group: str = ""
    operator_group: str = ""
    viewer_group: str = ""
    default_role: str = ""           # role when no group matched; "" => deny

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "OidcSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(OidcSettings)}
        return OidcSettings(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> None:
        if not self.enabled:
            return
        if not (self.issuer.strip() and self.client_id.strip() and self.client_secret.strip()):
            raise ConfigError(_("OIDC needs an issuer, client ID and client secret."))
        if not self.issuer.strip().lower().startswith("https://"):
            raise ConfigError(_("OIDC issuer must be an https:// URL."))
        if self.default_role and self.default_role not in ROLES:
            raise ConfigError(_("OIDC default role must be one of {0} or blank.").format(ROLES))


_PEER_ZONE = re.compile(r"[A-Za-z0-9_.-]+")


def peer_host(host: str) -> str | None:
    """A cluster peer's host in canonical form: an IP address (IPv6 without brackets,
    with an optional %zone) or a host name. None when it is neither — a value with
    anything in it that could change the URL built from it."""
    h = str(host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    addr, sep, zone = h.partition("%")
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        ip = None
    if ip is not None:
        if sep and not (ip.version == 6 and _PEER_ZONE.fullmatch(zone)):
            return None
        return f"{ip}%{zone}" if sep else str(ip)
    labels = (h[:-1] if h.endswith(".") else h).split(".")
    if len(h) <= 253 and all(
            0 < len(lb) <= 63 and lb[0].isalnum() and lb[-1].isalnum()
            and all(c.isalnum() or c in "-_" for c in lb) for lb in labels):
        return h
    return None


def parse_cluster_peer(entry: str, strict: bool = False) -> tuple[str, str, int]:
    """Parse a manual cluster-peer entry into (scheme, host, port). Accepts
    'host:port' (defaults to http) or 'http(s)://host:port'. Raises ConfigError.
    strict (when an entry is edited) also requires the host to be an IP address or a
    host name; validate() stays lenient, so an entry an older release accepted still
    loads (one that is no address is just never contacted)."""
    s = (entry or "").strip()
    scheme = "http"
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError(_("Cluster peer scheme must be http or https: {0}").format(entry))
    host, sep, port_s = s.rpartition(":")
    if not sep or not host or (strict and peer_host(host) is None):
        raise ConfigError(_("Cluster peer must be host:port — got: {0}").format(entry))
    try:
        port = int(port_s)
    except ValueError:
        raise ConfigError(_("Cluster peer port is not a number: {0}").format(entry)) from None
    if not (1 <= port <= 65535):
        raise ConfigError(_("Cluster peer port must be 1..65535: {0}").format(entry))
    return scheme, host, port


@dataclass
class ClusterSettings:
    """LAN cluster: instances discover each other via signed UDP broadcast beacons
    and one node aggregates every node's mappings into a single read-only view.
    Opt-in: disabled until `enabled` is set AND a shared `key` is configured. The
    key both signs beacons (so only same-key nodes trust each other) and guards the
    peer-facing status endpoint. `advertise_ip` overrides the auto-detected LAN IP
    that peers use to reach this node's web UI (blank => auto). `peers` is a list of
    manual 'host:port' entries for routed/L3 networks where UDP broadcast doesn't
    reach — they're aggregated alongside auto-discovered peers.

    Peer requests are signed with (a key derived from) the key; `legacy_auth` also
    accepts the raw key from, and sends it to, nodes running 2.8.0 or older, which know
    nothing else. During a planned key change `previous_key` (the key replaced last) is still
    accepted until `previous_until`, so the nodes not changed yet keep working."""
    enabled: bool = False
    key: str = ""                    # shared secret (PSK); empty => cluster off
    discovery_port: int = 41750      # UDP port for broadcast beacons
    advertise_ip: str = ""           # override advertised web-UI IP (blank => auto-detect)
    peers: list[str] = field(default_factory=list)  # manual 'host:port' peer entries
    previous_key: str = ""           # still accepted during a key change (blank => none)
    previous_until: float = 0.0      # ... until this time (epoch seconds; 0 => no end)
    legacy_auth: bool = True         # raw key with nodes up to 2.8.0 (off once all upgraded)
    discovery: bool = True           # UDP beacons; off => manual peers only

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ClusterSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(ClusterSettings)}
        c = ClusterSettings(**{k: v for k, v in d.items() if k in known})
        c.peers = [str(p).strip() for p in (c.peers or []) if str(p).strip()]
        # a hand-edited key of null or a number: "" (cluster off, which start-up reports) or
        # the number as text, instead of failing wherever the key is used
        c.key = "" if c.key is None else str(c.key)
        # the fields added after 2.8.0: a hand-edited value of the wrong type takes the default
        c.previous_key = c.previous_key if isinstance(c.previous_key, str) else ""
        c.previous_until = _epoch(c.previous_until)
        for name in ("legacy_auth", "discovery"):
            if not isinstance(getattr(c, name), bool):
                setattr(c, name, True)
        return c

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.key.strip())

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.key.strip():
            raise ConfigError(_("Cluster is enabled but no shared key is set."))
        try:
            port = int(self.discovery_port)
        except (TypeError, ValueError):
            raise ConfigError(_("Cluster discovery port must be a number.")) from None
        if not (1 <= port <= 65535):
            raise ConfigError(_("Cluster discovery port must be 1..65535."))
        if self.advertise_ip.strip():
            try:
                ipaddress.ip_address(self.advertise_ip.strip())
            except ValueError:
                raise ConfigError(
                    _("Cluster advertise IP is not a valid address: {0}").format(self.advertise_ip)) from None
        for entry in self.peers:
            parse_cluster_peer(entry)  # raises ConfigError on a bad entry


NOTIFY_EVENTS = ("device_lost", "device_back", "start_failed",
                 "client_connected", "client_disconnected", "login_failed")
NOTIFY_DEFAULT_EVENTS = ("device_lost", "device_back", "start_failed")
WEBHOOK_FORMATS = ("json", "slack", "teams", "discord", "telegram")
SMTP_SECURITY = ("starttls", "ssl", "none")


@dataclass
class NotificationSettings:
    """Event notifications (app/notify.py): a webhook and/or e-mail when a serial
    device disappears or comes back, a mapping fails to start, clients connect or
    disconnect, or a sign-in fails. Off by default; the same event for the same
    mapping is sent at most once per repeat_s."""
    enabled: bool = False
    events: list[str] = field(default_factory=lambda: list(NOTIFY_DEFAULT_EVENTS))
    repeat_s: int = 300
    webhook_url: str = ""            # may carry a token (Slack/Teams/Discord/Telegram)
    webhook_format: str = "json"     # json | slack | teams | discord | telegram
    telegram_chat_id: str = ""
    email_to: str = ""               # comma-separated recipients
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"  # starttls | ssl | none
    smtp_username: str = ""
    smtp_password: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "NotificationSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(NotificationSettings)}
        n = NotificationSettings(**{k: v for k, v in d.items() if k in known})
        n.events = [e for e in (n.events or []) if e in NOTIFY_EVENTS]
        return n

    @property
    def recipients(self) -> list[str]:
        return [a.strip() for a in self.email_to.replace(";", ",").split(",") if a.strip()]

    def validate(self) -> None:
        if not self.enabled:
            return
        if not (self.webhook_url.strip() or self.recipients):
            raise ConfigError(_("Notifications need a webhook URL or e-mail recipients."))
        if self.webhook_url.strip():
            if not self.webhook_url.strip().lower().startswith(("https://", "http://")):
                raise ConfigError(_("The webhook URL must start with https:// (or http://)."))
            if self.webhook_format not in WEBHOOK_FORMATS:
                raise ConfigError(_("Webhook format must be one of {0}.").format(WEBHOOK_FORMATS))
            if self.webhook_format == "telegram" and not str(self.telegram_chat_id).strip():
                raise ConfigError(_("Telegram needs a chat ID."))
        if self.recipients:
            if not self.smtp_host.strip():
                raise ConfigError(_("E-mail notifications need an SMTP server."))
            if not _is_int(self.smtp_port) or not (1 <= self.smtp_port <= 65535):
                raise ConfigError(_("SMTP port must be between 1 and 65535."))
            if self.smtp_security not in SMTP_SECURITY:
                raise ConfigError(_("SMTP security must be one of {0}.").format(SMTP_SECURITY))
            if any("@" not in a for a in self.recipients):
                raise ConfigError(_("Each e-mail recipient needs an @."))
        if not _is_int(self.repeat_s) or not (0 <= self.repeat_s <= 86400):
            raise ConfigError(_("The repeat interval must be between 0 and 86400 seconds."))


def fold(text) -> str:
    """How names compare where letter case doesn't count (LDAP and OIDC group names, LDAP
    user names, an LDAP/OIDC sign-in name or a new account's name against the existing
    ones): NFC, every run of white space (no-break spaces too) as one space, and letter
    case folded so that Turkish İ, I, ı and i are one letter — str.lower() turns 'İ'
    into 'i' + U+0307 and leaves 'ı' as it is, so KALİTE and Kalite, or BAKIM and Bakım,
    would differ."""
    s = " ".join(unicodedata.normalize("NFC", str(text)).split())
    return s.replace("\u0130", "i").replace("\u0131", "i").casefold().replace("i\u0307", "i")


class AccountNameError(ValueError):
    """An LDAP/OIDC sign-in can't have an account under its name (upsert_external_user).
    The text says why, for the log."""


EXTERNAL_NAME_MAX = 255   # an LDAP/OIDC account name (UPNs and e-mail addresses fit)


def external_name(name) -> str:
    """The one spelling an LDAP/OIDC sign-in name is kept in: NFC (the same name sent
    decomposed is not another account), without white space around it."""
    return unicodedata.normalize("NFC", str(name)).strip()


def external_name_problem(name: str) -> Optional[str]:
    """Why `name` (as external_name() spells it) can't name an LDAP/OIDC account, for the
    log; None if it can. Any printable text works — DOMAIN\\user, user@domain, names with
    / # ? % — because the Users panel sends names in form fields, not in URLs. But every
    character must be visible: a control, format (such as a right-to-left override or a
    zero-width space) or line-separator character would hide the real name in the Users
    panel, the logs and the audit log. A name in parentheses is how the audit log marks
    who acted without an account ("(no login)", "(command line)")."""
    if not name:
        return "it is empty"
    if len(name) > EXTERNAL_NAME_MAX:
        return f"it is longer than {EXTERNAL_NAME_MAX} characters"
    hidden = sorted({c for c in name if unicodedata.category(c)[0] == "C"
                     or unicodedata.category(c) in ("Zl", "Zp")})
    if hidden:
        return ("it contains control or invisible characters ("
                + ", ".join(f"U+{ord(c):04X}" for c in hidden) + ")")
    if name.startswith("(") and name.endswith(")"):
        return ("a name in parentheses is how the audit log marks who acted without an "
                "account, such as '(no login)'")
    return None


@dataclass
class User:
    """A web-UI account. `role` gates what the user may do; `pwd_version` is bumped
    on that user's password/role change to revoke only their existing sessions (a new
    account starts at a random value, see new_pwd_version).
    `source` is 'local' (password in password_hash) or 'ldap' (a shadow account whose
    role is derived from LDAP groups on each login; no local password)."""
    username: str = ""
    password_hash: str = ""
    role: str = "admin"
    pwd_version: int = 0
    source: str = "local"

    @property
    def kind(self) -> str:
        """How the account signs in, for messages: 'local', 'LDAP' or 'OIDC'."""
        return self.source if self.source == "local" else self.source.upper()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "User":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(User)}
        u = User(**{k: v for k, v in d.items() if k in known})
        if u.role not in ROLES:
            u.role = "viewer"
        if u.source not in ("local", "ldap", "oidc"):
            u.source = "local"
        return u


def new_pwd_version() -> int:
    """The first pwd_version of a new account. A session cookie is checked against the
    user name and this number only, so it is random rather than 1: an account deleted
    and created again under the same name (or an LDAP/OIDC user signing in again after
    being deleted) must not match the cookies the earlier account was given."""
    return secrets.randbelow(2 ** 31)


@dataclass
class AppConfig:
    version: int = 1
    admin_ui: AdminUI = field(default_factory=AdminUI)
    secret_key: str = field(default_factory=lambda: secrets.token_hex(32))
    api_token_hash: str = ""  # sha256 of the REST API bearer token (empty => API disabled)
    api_token_role: str = "admin"  # role granted by the API token (viewer = read-only)
    # lifetime of a web-UI sign-in (cookie + signed token); Settings -> Web UI access
    session_timeout_s: int = SESSION_TIMEOUT_DEFAULT_S
    # LDAP/OIDC accounts not signed in for this many days are removed (0: never, the
    # default); Settings -> Users
    external_users_expire_days: int = EXPIRE_DAYS_DEFAULT
    # Web-UI accounts. Empty => first-run setup pending. A legacy single-password
    # config (top-level password_hash) is migrated to one 'admin' user on load.
    users: list[User] = field(default_factory=list)
    ldap: LdapSettings = field(default_factory=LdapSettings)
    oidc: OidcSettings = field(default_factory=OidcSettings)
    cluster: ClusterSettings = field(default_factory=ClusterSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    # Stable per-node identity for the LAN cluster (generated once, persisted).
    instance_id: str = field(default_factory=lambda: secrets.token_hex(8))
    defaults: dict[str, Any] = field(default_factory=dict)  # serial defaults
    mappings: list[MappingConfig] = field(default_factory=list)
    # What loading changed in an older config (logged at startup; never persisted).
    load_notes: list[str] = field(default_factory=list, repr=False, compare=False)
    # Loading had to create secret_key / instance_id (config.json lacked them); the
    # start-up saves them once (runtime.prepare_config). Never persisted.
    ids_created: bool = field(default=False, repr=False, compare=False)
    # Parts start-up accepted although validate() rejects them (tolerate_problems): part
    # key -> its content then. Never persisted.
    tolerated: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
        token_role, token_note = _api_token_role(d)
        cfg = AppConfig(
            version=int(d.get("version", 1)),
            admin_ui=admin_ui,
            secret_key=d.get("secret_key") or secrets.token_hex(32),
            api_token_hash=d.get("api_token_hash", ""),
            api_token_role=token_role,
            session_timeout_s=_session_timeout(d.get("session_timeout_s")),
            external_users_expire_days=_expire_days(d.get("external_users_expire_days")),
            users=users,
            ldap=LdapSettings.from_dict(d.get("ldap", {})),
            oidc=OidcSettings.from_dict(d.get("oidc", {})),
            cluster=ClusterSettings.from_dict(d.get("cluster", {})),
            notifications=NotificationSettings.from_dict(d.get("notifications", {})),
            instance_id=d.get("instance_id") or secrets.token_hex(8),
            defaults=d.get("defaults", {}) or {},
            mappings=[MappingConfig.from_dict(m) for m in d.get("mappings", [])],
        )
        _normalize_admin_limits(cfg.admin_ui, cfg.load_notes)
        cfg.ids_created = not (d.get("secret_key") and d.get("instance_id"))
        raw_days = d.get("external_users_expire_days")
        if raw_days is not None and raw_days != cfg.external_users_expire_days:
            cfg.load_notes.append(
                f"external_users_expire_days {raw_days!r} is not 0 (never) or "
                f"{EXPIRE_DAYS_MIN}..{EXPIRE_DAYS_MAX} days: {cfg.external_users_expire_days} "
                "is used")
        spellings: dict[str, list[str]] = {}
        for u in cfg.users:
            if u.source == "ldap":
                spellings.setdefault(fold(u.username), []).append(u.username)
        for names in (n for n in spellings.values() if len(n) > 1):
            cfg.load_notes.append(
                f"LDAP accounts {', '.join(map(repr, names))} are one directory user, in "
                "different letter case: they become one account at that user's next "
                "sign-in, which ends the sessions of the other spellings (deleting one in "
                "Settings → Users deletes them all)")
        if token_note:
            cfg.load_notes.append(token_note)
        if cfg.ldap.enabled and cfg.ldap.uses_tls and "tls_verify" not in (d.get("ldap") or {}):
            cfg.load_notes.append(
                "LDAP: the directory server's certificate is now checked (LDAPS / StartTLS): "
                "it must come from a CA this system trusts and name the host in the server "
                "URI. If LDAP sign-in fails with a certificate error, all.log says why; for a "
                "private CA (Active Directory Certificate Services) or a self-signed "
                "certificate, set Settings → LDAP / Active Directory → CA certificate file "
                "(ldap.ca_file). Local accounts are not affected.")
        for raw, m in zip(d.get("mappings", []), cfg.mappings, strict=False):
            old_id = (raw or {}).get("id")
            if old_id and old_id != m.id:
                cfg.load_notes.append(
                    f"mapping '{m.name}': its id {old_id!r} is not a valid mapping id (1-64 "
                    f"letters, digits or '_'), so it is now {m.id}: the REST API and /metrics "
                    "use the new id, and its log and traces start afresh under it")
            old = str(((raw or {}).get("options") or {}).get("trace_both") or "").strip()
            if old and old != m.options.trace_both:
                cfg.load_notes.append(
                    f"mapping '{m.name}': its trace file is now traces/{m.id}-{m.options.trace_both} "
                    f"in the data directory (configured: {old!r}; traces are no longer written "
                    "outside it)")
            for side in m.serial_sides():
                if url_port(side.port):
                    cfg.load_notes.append(
                        f"mapping '{m.name}': its serial port {side.port!r} is a URL, not a "
                        "device; URL ports are no longer opened, so the mapping does not start "
                        "until a serial device is set")
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "admin_ui": asdict(self.admin_ui),
            "secret_key": self.secret_key,
            "api_token_hash": self.api_token_hash,
            "api_token_role": self.api_token_role,
            "session_timeout_s": self.session_timeout_s,
            "external_users_expire_days": self.external_users_expire_days,
            "users": [asdict(u) for u in self.users],
            "ldap": asdict(self.ldap),
            "oidc": asdict(self.oidc),
            "cluster": asdict(self.cluster),
            "notifications": asdict(self.notifications),
            "instance_id": self.instance_id,
            "defaults": self.defaults,
            "mappings": [m.to_dict() for m in self.mappings],
        }

    # ----- helpers -----
    @property
    def password_set(self) -> bool:
        return bool(self.users)

    def get_user(self, username: str) -> Optional[User]:
        return next((u for u in self.users if u.username == username), None)

    def named(self, name: str, source: Optional[str] = None) -> list[User]:
        """The accounts (of `source`, if given) whose name is `name` apart from letter
        case (fold: Turkish İ/ı too), the one spelled exactly so first."""
        key = fold(name)
        found = [u for u in self.users
                 if (source is None or u.source == source) and fold(u.username) == key]
        return sorted(found, key=lambda u: u.username != name)

    def admin_count(self) -> int:
        return sum(1 for u in self.users if u.role == "admin")

    def source_enabled(self, user: User) -> bool:
        """False for an LDAP/OIDC account while that sign-in method is switched off:
        its sessions end and it can't sign in until the method is on again."""
        if user.source == "ldap":
            return self.ldap.enabled
        if user.source == "oidc":
            return self.oidc.enabled
        return True

    def usable_admin_count(self) -> int:
        """Admins who can sign in with the current settings (local accounts, and
        LDAP/OIDC ones while their sign-in method is on)."""
        return sum(1 for u in self.users if u.role == "admin" and self.source_enabled(u))

    def external_accounts(self, name: str, source: str) -> list[User]:
        """The accounts of the LDAP/OIDC user who signs in as `name`. An LDAP name is the
        same name in any letter case (directories compare it that way, and a phone may
        send "Jdoe" for jdoe): every LDAP account named so, the exact spelling first. An
        OIDC name is compared exactly (a provider's "sub" is case-sensitive)."""
        if source == "ldap":
            return self.named(name, "ldap")
        return [u for u in self.users if u.source == source and u.username == name]

    def taken_name(self, username: str, source: str) -> Optional[User]:
        """The account, letter case aside, that keeps an LDAP/OIDC sign-in (`source`)
        from having one under this name: a local account, else one of the other sign-in
        method. None when there is none."""
        other = "oidc" if source == "ldap" else "ldap"
        found = self.named(username, "local") or self.named(username, other)
        return found[0] if found else None

    def upsert_external_user(self, username: str, role: str, source: str) -> User:
        """Create or refresh a shadow account for an externally-authenticated user
        (LDAP/OIDC). Bumps pwd_version when the role or source changes.
        A sign-in through a directory or an identity provider never takes over, converts
        or demotes another kind of account — a local one, or one of the other sign-in
        method (an LDAP and an OIDC user of one name may be two people): raises
        AccountNameError when one has this name, letter case aside, or when the name
        can't be an account's (external_name_problem; `username` is spelled as
        external_name() spells it).
        LDAP: the account is the one spelled like `username`, else the first in another
        letter case (external_accounts); the person's other LDAP accounts — made by
        earlier versions for each spelling — are removed, which ends their sessions."""
        if taken := self.taken_name(username, source):
            raise AccountNameError(f"the {taken.kind} account {taken.username!r} has this name")
        if (why := external_name_problem(username)) is not None:
            raise AccountNameError(why)
        same = self.external_accounts(username, source)
        if len(same) > 1:
            self.users = [x for x in self.users if not any(x is y for y in same[1:])]
        u = same[0] if same else self.get_user(username)
        if u is None:
            u = User(username=username, password_hash="", role=role, source=source,
                     pwd_version=new_pwd_version())
            self.users.append(u)
        elif u.role != role or u.source != source:
            u.role, u.source = role, source
            u.pwd_version += 1
        return u

    def get_mapping(self, mapping_id: str) -> Optional[MappingConfig]:
        return next((m for m in self.mappings if m.id == mapping_id), None)

    def validate(self) -> None:
        """Validate every part (the admin UI settings, each section, each mapping) and the
        cross-mapping invariants (unique IP:port, one mapping per serial port); raises
        the first problem as ConfigError."""
        for _key, _label, _content, check in self._parts():
            check()
        for _key, error in self._clashes():
            raise error

    def _validate_admin_ui(self) -> None:
        # TLS: both files required together, and they must exist/be readable so a
        # bad path fails fast at save time instead of crashing uvicorn at startup.
        cert, key = self.admin_ui.tls_cert, self.admin_ui.tls_key
        if bool(cert) != bool(key):
            raise ConfigError(_("Set both TLS certificate and key, or neither."))
        for path in (p for p in (cert, key) if p):
            if not os.path.isfile(path):
                raise ConfigError(_("TLS file not found or not readable: {0}").format(path))

        # Admin UI bind IP (set via the console / --reconfigure, or a hand-edited
        # config). Catch a bad value here with a clear error instead of letting
        # uvicorn crash at bind time.
        if self.admin_ui.bind_ip:
            try:
                ipaddress.ip_address(self.admin_ui.bind_ip)
            except ValueError:
                raise ConfigError(
                    _("Invalid admin UI bind IP: {0}").format(self.admin_ui.bind_ip)) from None
        if self.admin_ui.language not in UI_LANGUAGES:
            raise ConfigError(_("The default language must be one of {0}.").format(UI_LANGUAGES))
        if self.admin_ui.anonymous_role not in ROLES:
            raise ConfigError(_("Role for visitors without login must be one of {0}.").format(ROLES))
        if not _is_int(self.session_timeout_s) or not (
                SESSION_TIMEOUT_MIN_S <= self.session_timeout_s <= SESSION_TIMEOUT_MAX_S):
            raise ConfigError(_("Session lifetime must be between {0} minutes and {1} days.").format(SESSION_TIMEOUT_MIN_S // 60, SESSION_TIMEOUT_MAX_S // 86400))

    def _parts(self):
        """validate()'s checks, one per part of the config, in order: (key, label,
        content, check). content() is what tells whether a save changed the part; a
        mapping's enabled flag is left out of it, since starting or stopping one changes
        nothing its own check looks at (a serial port it then shares is a clash)."""
        yield ("admin_ui", "admin UI", lambda: (asdict(self.admin_ui), self.session_timeout_s),
               self._validate_admin_ui)
        for key, label in (("ldap", "LDAP"), ("oidc", "OIDC"), ("cluster", "LAN cluster"),
                           ("notifications", "notifications")):
            section = getattr(self, key)
            yield key, label, functools.partial(asdict, section), section.validate
        for m in self.mappings:
            yield (f"mapping {m.id}", f"mapping '{m.name or m.id}'",
                   functools.partial(_mapping_content, m), m.validate)

    def _clashes(self) -> list[tuple[str, ConfigError]]:
        """Every pair of mappings that would share a serial port or a listening port, keyed
        by the resource and both mapping ids."""
        found = []
        serial_users: list[tuple[str, MappingConfig]] = []          # (device, mapping)
        listeners: list[tuple[str, str, int, MappingConfig]] = []   # (proto, bind_ip, port, mapping)
        for m in self.mappings:
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
                    for used, other in serial_users:
                        if used == dev and other.id != m.id:
                            found.append((f"serial {dev} {other.id} {m.id}", ConfigError(
                                _("Mappings '{0}' and '{1}' both use serial port {2} — a serial "
                                  "port can only be opened by one mapping.").format(other.name, m.name, dev))))
                    serial_users.append((dev, m))

            # only listening mappings (server/udp) bind a port; client/serialbridge don't
            if m.kind != "net" or not m.network.listens:
                continue
            # A TCP listener and a UDP listener CAN share the same port number (they
            # bind different transports), so the clash key includes the transport.
            proto = "udp" if m.network.mode == "udp" else "tcp"
            # 0.0.0.0 collides with everything on that port; treat any overlap as a clash.
            for sproto, bip, bport, other in listeners:
                if sproto == proto and bport == m.network.port and _ip_overlaps(bip, m.network.bind_ip):
                    found.append((f"{proto} {m.network.port} {other.id} {m.id}", ConfigError(
                        _("Mappings '{0}' and '{1}' both use {2} port {3} on overlapping "
                          "addresses ({4} / {5}).").format(other.name, m.name, proto.upper(), m.network.port, bip, m.network.bind_ip))))
            listeners.append((proto, m.network.bind_ip, m.network.port, m))
        return found

    def _broken(self) -> list[tuple[str, str, Any]]:
        """(key, problem, content) for every part validate() rejects and every clash."""
        found = []
        for key, label, content, check in self._parts():
            try:
                check()
            except Exception as e:  # a hand-edited value of the wrong type raises others too
                found.append((key, f"{label}: {e}", content()))
        try:
            found += [(key, str(error), None) for key, error in self._clashes()]
        except Exception as e:  # mappings too broken to compare
            found.append(("clashes", f"mappings: {e}", None))
        return found

    def problems(self) -> list[str]:
        """Everything validate() rejects in this config, one line each, naming the part
        (validate() stops at the first)."""
        return [problem for _key, problem, _content in self._broken()]

    def tolerate_problems(self) -> list[str]:
        """Accept what this config breaks now and return it, one line each: later saves
        may leave those parts as they are (validate_for_save). Start-up does this, since
        a config.json that loads is never refused: an older release may have accepted
        it, or a file it names may be missing on this machine (runtime.prepare_config)."""
        broken = self._broken()
        self.tolerated = {key: content for key, _problem, content in broken}
        return [problem for _key, problem, _content in broken]

    def validate_for_save(self) -> dict[str, Any]:
        """What a save enforces: validate(), except that a part start-up accepted while
        broken (tolerate_problems) may stay broken as long as it is left as it was, and
        so may a clash it accepted. What a save changes or adds must validate, so one bad
        value can't block every other change (signing in with LDAP/OIDC, stopping a
        mapping, fixing another broken part).

        Returns what is still accepted after this save (a part it fixed is held to the
        rules again). ConfigStore.save commits that only once the file is written, so a
        save that fails to write — and is rolled back by its caller — keeps the
        acceptance it had."""
        if not self.tolerated:
            self.validate()
            return {}
        still: dict[str, Any] = {}
        for key, _label, content, check in self._parts():
            try:
                check()
            except Exception as e:
                if key not in self.tolerated or content() != self.tolerated[key]:
                    if isinstance(e, ConfigError):
                        raise
                    raise ConfigError(str(e)) from e
                still[key] = self.tolerated[key]
        try:
            clashes = self._clashes()
        except Exception as e:
            if "clashes" not in self.tolerated:
                raise ConfigError(str(e)) from e
            clashes, still["clashes"] = [], None
        for key, error in clashes:
            if key not in self.tolerated:
                raise error
            still[key] = None
        return still


def _mapping_content(m: "MappingConfig") -> dict[str, Any]:
    content = m.to_dict()
    content.pop("enabled", None)
    return content


def _api_token_role(d: dict[str, Any]) -> tuple[str, str]:
    """api_token_role from a config file, and a note when it had to be changed. No key
    means a token from before token roles (v2.3), which had full access: admin. A role
    in other letter case or with spaces is that role. Anything else is not a role, and
    the token gets the least access (viewer, read-only) rather than a guess."""
    if "api_token_role" not in d:
        return "admin", ""
    raw = d["api_token_role"]
    role = raw.strip().lower() if isinstance(raw, str) else ""
    if role in ROLES:
        return role, ""
    if not d.get("api_token_hash"):  # no token: generating one picks its role anyway
        return "viewer", ""
    return "viewer", (f"REST API: api_token_role {raw!r} is not one of {', '.join(ROLES)}, so the "
                      "API token is read-only (viewer). For more, generate a new token with the "
                      "role it needs under Settings → REST API token.")


def _session_timeout(value: Any) -> int:
    """session_timeout_s from a config file. Before v2.8 it was stored but never used
    (sessions always lasted 8 h), so an odd hand-edited value must not stop a start:
    unreadable -> the default, anything else clamped into the supported range."""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return SESSION_TIMEOUT_DEFAULT_S
    return min(max(seconds, SESSION_TIMEOUT_MIN_S), SESSION_TIMEOUT_MAX_S)


def _epoch(value: Any) -> float:
    """A stored point in time (epoch seconds); anything unreadable is 0 (none)."""
    if isinstance(value, bool):
        return 0.0
    try:
        t = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return t if math.isfinite(t) and t > 0 else 0.0


def _expire_days(value: Any) -> int:
    """external_users_expire_days from a config file: missing or unreadable -> the
    default (0: never), 0 or less -> 0, anything else clamped into the supported range (a
    hand-edited value must not stop a start; from_dict notes the change)."""
    if value is None or isinstance(value, bool):
        return EXPIRE_DAYS_DEFAULT
    try:
        days = int(value)
    except (TypeError, ValueError, OverflowError):
        return EXPIRE_DAYS_DEFAULT
    return 0 if days <= 0 else min(max(days, EXPIRE_DAYS_MIN), EXPIRE_DAYS_MAX)


def _ip_overlaps(a: str, b: str) -> bool:
    if a == b:
        return True
    wildcards = {"0.0.0.0", "::"}
    return a in wildcards or b in wildcards


# ---------------------------------------------------------------------------
# Atomic store
# ---------------------------------------------------------------------------
BACKUPS_KEPT = 50
_BACKUP_NAME = re.compile(r"^config-\d{8}T\d{6}Z(-\d+)?\.json$")


class ConfigStore:
    """config.json with atomic writes, plus automatic backups of earlier versions.

    Every save that changes config.json first copies the version it replaces to
    <data>/backups/config-<UTC time>.json (identical content is not copied twice; the
    newest BACKUPS_KEPT are kept). Backups are never read back on their own: deleting
    config.json still resets the system; restoring one is an explicit admin action."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        # config.json holds the password hash + secret_key; keep the dir private on
        # BOTH platforms (POSIX chmod 0700 / Windows icacls owner-only inheritance).
        lock_down_dir(directory)
        self.backup_dir = os.path.join(directory, "backups")
        # what each backup holds, by name: ((size, mtime_ns), _backup_summary()) - see backups()
        self._backup_summaries: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> AppConfig:
        if not self.exists():
            return AppConfig()
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return AppConfig.from_dict(data)

    def matches(self, config: AppConfig) -> bool:
        """True when config.json already holds exactly this configuration, so saving it
        would change nothing (start-up then leaves the file alone)."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh) == json.loads(json.dumps(config.to_dict()))
        except (OSError, ValueError):
            return False

    def save(self, config: AppConfig, *, validate: bool = True) -> None:
        """Atomically persist config to a single file (validates first: what start-up
        accepted while broken may stay as it was, see AppConfig.validate_for_save).

        Uses a temp file + os.replace so config.json is never partially written; the
        version being replaced is kept in backups/ first (see the class docstring).
        validate=False is for a change the caller checked itself and that must not
        fail on an unrelated setting (account recovery, app/accounts.py; start-up right
        after tolerate_problems(), runtime.prepare_config).
        """
        still_accepted = config.validate_for_save() if validate else config.tolerated
        data = json.dumps(config.to_dict(), indent=2, ensure_ascii=False)
        directory = os.path.dirname(self.path)
        self._backup_current(data)

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
        # written: only now does a part this save fixed stop being accepted while broken
        config.tolerated = still_accepted
        _fsync_dir(directory)

    # ----- backups -----
    def _backup_current(self, new_data: str) -> None:
        """Copy config.json into backups/ before it is replaced by new_data. Best
        effort: a failing backup must never stop the save itself."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            return  # first save: nothing to keep
        if current == new_data:
            return
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            lock_down_dir(self.backup_dir)
            names = self.backup_names()
            if names:
                with open(os.path.join(self.backup_dir, names[0]), encoding="utf-8") as fh:
                    if fh.read() == current:
                        return  # this version is already the newest backup
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            # several saves in one second: -2, -3 ... (never reuse a pruned lower number,
            # or the newest backup would sort as the oldest)
            same = [_backup_order(n)[1] for n in names if n.startswith(f"config-{stamp}")]
            n = max(same) + 1 if same else 1
            name = f"config-{stamp}.json" if n == 1 else f"config-{stamp}-{n}.json"
            fd, tmp = tempfile.mkstemp(dir=self.backup_dir, prefix=".bak.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(current)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(self.backup_dir, name))
            for old in self.backup_names()[BACKUPS_KEPT:]:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(self.backup_dir, old))
        except OSError:
            pass

    def backup_names(self) -> list[str]:
        """Backup file names, newest first."""
        try:
            names = [n for n in os.listdir(self.backup_dir) if _BACKUP_NAME.match(n)]
        except OSError:
            return []
        return sorted(names, key=_backup_order, reverse=True)

    def backups(self) -> list[dict]:
        """[{name, time (UTC, 'YYYY-MM-DD HH:MM:SS'), size, mappings, users}], newest first.

        The Settings page lists them at every visit: a file's contents are read once and
        kept by its name, size and modification time (a backup is written once and never
        changed in place), so only a new or changed one is parsed again. In memory only:
        the files and their names stay as they are."""
        known, kept, out = self._backup_summaries, {}, []
        for name in self.backup_names():
            path = os.path.join(self.backup_dir, name)
            info: dict[str, Any] = {"name": name, "size": 0, "mappings": None, "users": None,
                                    "time": backup_time(name)}
            try:
                st = os.stat(path)
            except OSError:
                out.append(info)
                continue
            stamp = (st.st_size, st.st_mtime_ns)
            hit = known.get(name)
            summary = hit[1] if hit is not None and hit[0] == stamp else _backup_summary(path)
            if summary is not None:
                kept[name] = (stamp, summary)
                info.update(summary)
            info["size"] = st.st_size
            out.append(info)
        self._backup_summaries = kept  # pruned to the files there are
        return out

    def read_backup(self, name: str) -> str:
        """The text of one backup. Raises ConfigError for an unknown / invalid name
        (only names this store generated are accepted: no path tricks)."""
        if not _BACKUP_NAME.match(name or "") or name not in self.backup_names():
            raise ConfigError(_("No such backup."))
        with open(os.path.join(self.backup_dir, name), encoding="utf-8") as fh:
            return fh.read()


def _backup_summary(path: str) -> dict[str, Any] | None:
    """What the backups list shows of one backup's contents (None for the counts of a file
    that isn't a configuration; None itself when it can't be read now)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError:
        return None
    except ValueError:
        return {"mappings": None, "users": None}
    try:
        return {"mappings": len(doc.get("mappings") or []), "users": len(doc.get("users") or [])}
    except (AttributeError, TypeError):
        return {"mappings": None, "users": None}


def backup_time(name: str) -> str:
    """'config-20260101T120000Z.json' -> '2026-01-01 12:00:00' (UTC)."""
    stamp = name[len("config-"):len("config-") + 15]
    return (f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} "
            f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}")


def _backup_order(name: str) -> tuple:
    m = re.match(r"^config-(\d{8}T\d{6})Z(?:-(\d+))?\.json$", name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def _fsync_dir(directory: str) -> None:
    """fsync a directory so a rename into it is durable (not on Windows)."""
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except (OSError, AttributeError):
        pass  # not supported on some platforms (e.g. Windows dirs)
