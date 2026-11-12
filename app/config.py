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
import ntpath
import os
import posixpath
import re
import secrets
import socket
import tempfile
import time
import unicodedata
import urllib.parse
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
# Strict-Transport-Security max-age with admin TLS (AdminUI.hsts_max_age): one year, as
# before it could be set; 0 sends max-age=0 (HSTS off, browsers drop what they stored)
HSTS_MAX_AGE_DEFAULT = 365 * 86400
HSTS_MAX_AGE_MAX = 2 * 365 * 86400
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


# The data directory (the folder of config.json, set by ConfigStore): where a relative
# TLS file path in the configuration points. Before 2.9 such a path was looked up from the
# working directory (/app in Docker, the service's folder, System32 for the Windows
# service), so a data directory that moved or was restored elsewhere lost its files.
_data_dir: Optional[str] = None


def set_data_dir(path: Optional[str]) -> None:
    global _data_dir
    _data_dir = os.path.abspath(path) if path else None


def data_path(path: Any, data_dir: Optional[str] = None) -> str:
    """The file a TLS path from the configuration names: an absolute path as it is, a
    relative one in the data directory (``data_dir``, else ConfigStore's). A relative path
    found only from the working directory, where releases before 2.9 looked for it, is
    still used from there."""
    path = str(path or "")
    base = data_dir or _data_dir
    if not path or os.path.isabs(path) or ntpath.isabs(path) or not base:
        return path  # (a Windows path is no file in the data directory elsewhere either)
    inside = os.path.join(base, path)
    if not os.path.exists(inside) and os.path.exists(path):
        return path
    return inside


def moved_tls_file(path: Any, data_dir: str) -> str:
    """Where a TLS file of a data directory that was moved, or restored on another machine,
    is now: for an absolute ``path`` that is not there and lies in a folder named "tls"
    (where Settings → Admin TLS generates its certificate: <data dir>/tls/), the file of
    that name in ``data_dir``'s tls folder, if it is there; else "". A path written on
    Windows is read on any OS, and one written elsewhere on Windows."""
    text = str(path or "")
    if not text or not (posixpath.isabs(text) or ntpath.isabs(text)) or os.path.exists(text):
        return ""
    folder, name = ntpath.split(text)  # (ntpath takes "/" and "\\" both)
    if not name or ntpath.basename(folder).lower() != "tls":
        return ""
    found = os.path.join(data_dir, "tls", name)
    return found if os.path.isfile(found) and found != text else ""


def normalize_cidr(value: str) -> str:
    """Treat a bare wildcard address as 'any'. Users typing 0.0.0.0 / :: in an
    allow/priority list mean 'all clients', not a single /32 or /128 host."""
    v = value.strip()
    if v == "0.0.0.0":
        return "0.0.0.0/0"
    if v == "::":
        return "::/0"
    return v


# the bind IPs of a listener on every interface: 0.0.0.0 takes IPv4 clients, :: IPv6 ones
# (IPV6_V6ONLY); NetworkSettings.dual_stack makes either take both
WILDCARDS = ("0.0.0.0", "::")


def canonical_ip(value: Any) -> Any:
    """An IP address as it is stored: in its canonical form ('0:0:0:0:0:0:0:1' is '::1'),
    an IPv4-mapped IPv6 address ('::ffff:10.0.0.5') as the IPv4 address it stands for
    (that is what a listener on it can take). Anything else is returned as it is, for
    validation to report."""
    if not isinstance(value, str):
        return value
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return value
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


@functools.lru_cache(maxsize=None)
def _ipv6_takes_ipv4() -> bool:
    """Whether an IPv6 socket on this system takes IPv4 too unless told otherwise
    (IPV6_V6ONLY off by default: Linux unless net.ipv6.bindv6only is set, not Windows).
    Before 2.9 a UDP mapping on :: kept that default."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            return not s.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
    except (OSError, AttributeError):  # no IPv6 here
        return False


def shown_host(host: Any) -> str:
    """A host as host:port writes it: an IPv6 address in brackets, the admin UI's ""
    (every interface) as *."""
    h = "" if host is None else str(host)
    if not h:
        return "*"
    return f"[{h}]" if ":" in h and not h.startswith("[") else h


def host_port(host: Any, port: Any) -> str:
    """host:port as the log and the UI write it ([::1]:4001; see shown_host)."""
    return f"{shown_host(host)}:{port}"


def bind_ip_problem(value: Any) -> str:
    """Why ``value`` can't be a listener's bind IP ("" when it can): it is not an IP
    address, or it is a multicast address or a reserved one other than the wildcard
    and loopback addresses (0.0.0.0, ::, ::1). An IPv4-mapped IPv6 address is judged by
    its IPv4 address (Python releases differ in how is_reserved reads one). The one rule
    for a mapping's bind IP and for the console's custom admin UI address."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return _("Invalid bind IP: {0}.").format(repr(value))
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or (ip.is_reserved and not (ip.is_loopback or ip.is_unspecified)):
        return _("Bind IP cannot be a multicast/reserved address.")
    return ""


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
    # keeping other programs off the port (POSIX; Windows opens a port for one program
    # only anyway). `exclusive` (flock) keeps out only programs that flock the port too:
    #   tiocexcl: while the port is open, other programs' opens fail (TIOCEXCL; not root's)
    #   respect_uucp_locks: don't open a port another program has a UUCP lock file on
    #     (minicom, cu: LCK..<tty> in /run/lock or /var/lock; read only, none is made)
    tiocexcl: bool = False
    respect_uucp_locks: bool = False


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

    @property
    def identity(self) -> dict[str, Any]:
        """The USB identity this port is bound to (usb_identity of ``match``), {} if none."""
        return usb_identity(self.match)


# the keys of a serial port's `match` (the USB identity it is bound to) the engine knows;
# any other key (a typo, "VID") is not used, so it can't make every port match
MATCH_KEYS = ("vid", "pid", "serial_number", "location")


def usb_identity(match: Any) -> dict[str, Any]:
    """The known keys of a serial port's ``match`` that are set (MATCH_KEYS); {} when none
    is: the port is opened by its name."""
    if not isinstance(match, dict):
        return {}
    return {k: match[k] for k in MATCH_KEYS if match.get(k)}


def identity_text(match: Any) -> str:
    """A USB identity as the UI shows it: VID:PID [serial number] (@ location)."""
    want = usb_identity(match)
    text = f"{want.get('vid', '*')}:{want.get('pid', '*')}"
    if want.get("serial_number"):
        text += f" [{want['serial_number']}]"
    if want.get("location"):
        text += f" @ {want['location']}"
    return text


_COM_NAME = re.compile(r"(?:\\\\\.\\|//\./)?(COM\d+)", re.I)


def canonical_device(name: Any) -> Any:
    """A serial port name in the form two names of one port have in common, for comparing
    them (never for storing: what the user set is kept as it is). Windows opens 'com3',
    'COM3', '\\\\.\\COM3' and '//./COM3' alike, so all are 'COM3'; the name's shape decides,
    not this machine's OS (a cluster peer's ports are compared here too). Any other name is
    returned unchanged ('\\\\.\\CNCA0' of com0com keeps its prefix, POSIX paths their case)."""
    m = _COM_NAME.fullmatch(name) if isinstance(name, str) else None
    return m.group(1).upper() if m else name


def _serial_clash(m: "MappingConfig", mappings: list["MappingConfig"]) -> Optional[tuple]:
    """The first serial port enabled mapping ``m`` shares with another enabled one of
    ``mappings``: (other mapping, m's side, the other's side), None when none. Shared means
    the same port in the canonical spelling (canonical_device) with the same USB identity
    binding (none, or the same one: both would open the same device)."""
    if not m.enabled:
        return None

    def uses(mapping):
        return [(canonical_device(s.port),
                 sorted((k, str(v).casefold()) for k, v in s.identity.items()), s)
                for s in mapping.serial_sides() if s.port]

    mine = uses(m)
    for other in mappings:
        if other.id == m.id or not other.enabled:
            continue
        for key, ident, side in mine:
            for okey, oident, oside in uses(other):
                if key == okey and ident == oident:
                    return other, side, oside
    return None


def serial_conflict(m: "MappingConfig", mappings: list["MappingConfig"]) -> str:
    """Why mapping ``m`` can't be enabled as it is next to ``mappings`` ("" when it can): an
    enabled one uses the same serial port, in any spelling Windows opens alike ('com3' is
    'COM3'), bound to no USB identity or to the same one. Checked where a mapping is created,
    changed or started (the form, the REST API, a cluster peer, an import), never by
    validate(): a stored pair must not stop a start or another save (from_dict notes it)."""
    clash = _serial_clash(m, mappings)
    if clash is None:
        return ""
    other, side, oside = clash
    if side.identity:
        return _("Mappings '{0}' and '{1}' are both bound to the USB identity {2} on serial "
                 "port {3}: they would open the same device, and a serial port can only be "
                 "opened by one mapping.").format(other.name, m.name, identity_text(side.match),
                                                  side.port)
    if side.port != oside.port:
        return _("Mappings '{0}' and '{1}' both use serial port {2} (written {3} in '{1}') — a "
                 "serial port can only be opened by one mapping.").format(
            other.name, m.name, oside.port, side.port)
    return _("Mappings '{0}' and '{1}' both use serial port {2} — a serial "
             "port can only be opened by one mapping.").format(other.name, m.name, side.port)


def url_port(port: Any) -> bool:
    """True for a serial port value that pyserial would open with a URL handler instead
    of as a device: anything with "://" (socket://, rfc2217://, spy://, loop://, alt://,
    hwgrep:// ...), which is what pyserial's serial_for_url dispatches on. A mapping only
    opens serial devices (COM3, \\\\.\\COM10, /dev/ttyUSB0, /dev/serial/by-id/...)."""
    return isinstance(port, str) and "://" in port


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def port_problem(port: Any) -> str:
    """Why ``port`` can't be set as a mapping's serial port ("" when it can). One permissive
    rule for every edit (a path typed in the form, the REST API, an import, a cluster peer):
    a URL is refused (url_port: ser2net opens serial devices only), and so is a name with a
    control character in it (a pasted line break); device paths (/dev/..., a link to one,
    any absolute path), Windows names (COM3, \\\\.\\CNCA0) and a placeholder for a port
    found by its USB identity ("auto") pass. Never checked by validate(): a stored port must
    keep loading (a URL one is refused when it is opened, see engine/serial_io.refuse_url)."""
    if url_port(port):
        return _("Serial port {0} is a URL, not a device: ser2net opens serial devices only "
                 "(URLs such as socket:// or spy:// are not supported).").format(port)
    if isinstance(port, str) and _CONTROL_CHARS.search(port):
        return _("Serial port {0} has a control character in it (a line break?): type the "
                 "device's path again.").format(repr(port))
    return ""


# ---------------------------------------------------------------------------
# Network side
# ---------------------------------------------------------------------------
NET_MODES = ("server", "client", "udp")


@dataclass
class NetworkSettings:
    mode: str = "server"  # server (listen) | client (connect-out) | udp
    protocol: str = "raw"  # raw | telnet | rfc2217
    # A listener on 0.0.0.0 takes IPv4 clients only, one on :: IPv6 clients only. With
    # dual_stack a wildcard bind IP (either of the two) takes both: the picker's "All
    # interfaces (IPv4 + IPv6)". It means nothing with another address (edits refuse
    # that); a release without it listens on the bind IP alone.
    bind_ip: str = "0.0.0.0"
    dual_stack: bool = False
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
    # UDP: more destinations that always get the device's output too (whoever sends, and
    # before anyone has), each "IP:port" ("[IPv6]:port"): e.g. an NMEA 0183 feed to a LAN's
    # broadcast address. Sending to a broadcast address needs udp_broadcast (SO_BROADCAST).
    udp_targets: list[str] = field(default_factory=list)
    udp_broadcast: bool = False
    max_connections: int = 1
    kick_old_user: bool = False
    # TCP (raw / telnet / rfc2217, server and connect-out): while the serial port is not
    # open (unplugged, reconnecting) clients are disconnected and new ones refused, after
    # the short wait for the port that an RFC 2217 client gets (options.
    # rfc2217_net_timeout_s); a connect-out mapping connects only while the port is open.
    # Off (default): clients stay connected and are taken, and what they send for the
    # device meanwhile is dropped (counted in bytes_dropped); RFC 2217 sessions follow a
    # reopened port.
    disconnect_without_device: bool = False
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
    # Per-mapping TLS for the data bridge (server/client TCP), see engine/tls.py. A listener
    # needs tls_cert and tls_key; with the CA file tls_client_ca it asks every client for a
    # certificate that chains to it, and ends the handshake of one without such a
    # certificate (the allow-list still applies). A connect-out link presents them (when
    # set) to a server that asks for a client certificate, and checks the server's
    # certificate: it must chain to a certificate in the CA file tls_ca and name
    # remote_host, and/or be one of the SHA-256 fingerprints in tls_fingerprint (with
    # neither, the link only encrypts). tls_key_password decrypts an encrypted key (never
    # asked for on a terminal); edits load the files with it (MappingConfig.check_tls_files).
    tls: bool = False
    tls_cert: str = ""
    tls_key: str = ""
    tls_key_password: str = ""
    tls_ca: str = ""
    tls_fingerprint: str = ""
    tls_client_ca: str = ""

    @property
    def listens(self) -> bool:
        return self.mode in ("server", "udp")

    @property
    def both_families(self) -> bool:
        """The listener takes IPv4 and IPv6 clients: dual_stack (true itself, not text
        that reads as true) with a wildcard bind IP."""
        return self.dual_stack is True and canonical_ip(self.bind_ip) in WILDCARDS

    @property
    def bind_shown(self) -> str:
        """The bind IP as the UI shows it before the port: * for every interface of both
        families, an IPv6 address in brackets."""
        return "*" if self.both_families else shown_host(self.bind_ip)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "NetworkSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(NetworkSettings)}
        net = NetworkSettings(**{k: v for k, v in d.items() if k in known})
        net.bind_ip = canonical_ip(net.bind_ip)  # (AppConfig.from_dict notes a change)
        return net

    def validate(self) -> None:
        if self.mode not in NET_MODES:
            raise ConfigError(_("Network mode must be one of {0}.").format(NET_MODES))
        if self.protocol not in PROTOCOLS:
            raise ConfigError(_("Protocol must be one of {0}.").format(PROTOCOLS))

        if self.listens:
            if problem := bind_ip_problem(self.bind_ip):
                raise ConfigError(problem)
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
            if self.listens and not (self.tls_cert and self.tls_key):
                raise ConfigError(_("Enable TLS requires both a certificate and a key."))
            if bool(self.tls_cert) != bool(self.tls_key):  # connect-out: both or neither
                raise ConfigError(_("Connect-out TLS: set both a certificate and a key (for a "
                                    "server that asks for a client certificate), or neither."))
            for p in (self.tls_cert, self.tls_key):
                if p and not os.path.isfile(data_path(p)):
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


UDP_TARGETS_MAX = 32


def udp_target(entry: Any) -> tuple[str, int]:
    """One UDP target, "IP:port" or "[IPv6]:port", as (IP, port). Raises ValueError when it
    is not a host's IP address (or a broadcast / multicast one) and a port."""
    text = str(entry).strip() if isinstance(entry, str) else ""
    host, sep, port = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    elif ":" in host:  # an IPv6 address needs its brackets ([fd00::1]:5000)
        sep = ""
    try:
        if not sep:
            raise ValueError
        ip = ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        raise ValueError(f"UDP target {entry!r} is not an IP address and a port "
                         "(IP:port, [IPv6]:port)") from None
    if ip.is_unspecified:
        raise ValueError(f"UDP target {entry!r} is not a host's address")
    if not 1 <= number <= 65535:
        raise ValueError(f"UDP target {entry!r}: the port is not between 1 and 65535")
    return str(ip), number


def udp_targets(net: NetworkSettings) -> list[tuple[str, int]]:
    """A UDP mapping's targets (udp_targets), each once. Raises ValueError for a stored one
    that is malformed (edits refuse it: MappingConfig.validate_input), so the mapping fails
    to start rather than leaving a destination out without a word."""
    entries = net.udp_targets
    if not isinstance(entries, list):
        raise ValueError("udp_targets is not a list of IP:port entries")
    return list(dict.fromkeys(udp_target(e) for e in entries))


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
RFC2217_SIGNATURE_MAX = 128  # characters (an edit's rule)


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
    # RFC2217: what a client's SIGNATURE request is answered with (blank: "ser2net")
    rfc2217_signature: str = ""
    # Modbus gateway: how long to wait for an RTU slave's reply before returning a
    # Modbus exception (0x0B gateway-target-failed) to the TCP master.
    modbus_response_timeout_s: float = 1.0
    # Serial-to-network mappings (not a Modbus gateway): send poll_string to the device
    # every poll_interval_s seconds while its port is open (0 = off), e.g. a scale's or an
    # analyzer's query, so its answers reach MQTT, a trace or a UDP target without a
    # program polling it. \r \n \t \\ and \xHH stand for those bytes (poll_bytes).
    # Skipped while a TCP client is connected (it does the asking then), unless
    # poll_with_clients.
    poll_string: str = ""
    poll_interval_s: float = 0.0
    poll_with_clients: bool = False

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
# what ends a serial line published to MQTT (MqttSettings.line_end)
MQTT_LINE_ENDS = ("lf", "cr", "any")
MQTT_TX_MAX_BYTES = 64 * 1024  # an MQTT write (<base_topic>/tx) at most, and all waiting


# What a read-only REST API token gets instead of a stored secret (the MQTT password).
# Sent back (a PUT of what such a token read, a form), it keeps the stored password, so it
# is never stored as one.
REDACTED = "********"


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
    # Serial lines (not a Modbus gateway): what ends a line. "lf" (default): \n, a \r just
    # before it is left out (as before); "cr": \r, a \n next to it left out; "any": \r or
    # \n (\r\n is one line end). A stored value that is none of these reads as "lf".
    line_end: str = "lf"
    # Serial lines (not a Modbus gateway): write each message on <base_topic>/tx to the
    # serial port as it is, and answer on <base_topic>/tx/result ("ok" / "error: ...").
    # Read-only refuses them; a retained one (the broker's copy, sent again after each
    # (re)connect) is ignored. Off: MQTT only publishes.
    tx_enabled: bool = False

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
        m = MappingConfig(
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
        net = d.get("network")
        if (isinstance(net, dict) and "dual_stack" not in net and m.kind == "net"
                and m.network.mode == "udp" and m.network.bind_ip == "::"
                and _ipv6_takes_ipv4()):
            # Written by (or for) a release without dual_stack, whose UDP socket on :: kept
            # the OS default: on Linux it took IPv4 datagrams too. IPV6_V6ONLY is set now
            # (NetworkSettings.dual_stack), so such a mapping keeps doing what it did here.
            m.network.dual_stack = True
        return m

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

    @property
    def accepts_mqtt_tx(self) -> bool:
        """MQTT messages on <base_topic>/tx are written to the serial port: a serial-to-network
        mapping (not a Modbus gateway) with MQTT and tx_enabled on and Read-only off. Who may
        send them is up to the broker's access rules."""
        return (self.kind == "net" and self.network.protocol != "modbus" and self.mqtt.enabled
                and self.mqtt.tx_enabled is True and not self.network.read_only)

    def validate_input(self) -> None:
        """The rules for a mapping that is being created or changed (web form, REST API,
        import, cluster), checked there in addition to validate(). validate() must not
        refuse them: a stored config has to keep loading and saving (Docker re-saves it on
        every start), so a stored value they forbid is refused where it is used instead
        (a URL serial port: when the mapping starts, see engine/serial_io.refuse_url)."""
        for side in self.serial_sides():
            if problem := port_problem(side.port):
                raise ConfigError(problem)
        if (self.kind == "serialbridge" and self.serial.port
                and canonical_device(self.serial.port) == canonical_device(self.serial_b.port)):
            raise ConfigError(_("Serial-to-serial bridge needs two different ports."))
        net = self.network
        if self.kind == "net" and net.listens and net.dual_stack is True and not net.both_families:
            raise ConfigError(_("Listening for IPv4 and IPv6 clients (dual_stack) needs the bind "
                                "IP 0.0.0.0 or ::, not {0}.").format(net.bind_ip))
        if self.kind == "net" and net.mode == "udp":
            self._validate_udp_peer()
        sig = self.options.rfc2217_signature
        if not isinstance(sig, str) or len(sig) > RFC2217_SIGNATURE_MAX or _CONTROL_CHARS.search(sig):
            raise ConfigError(_("The RFC 2217 signature must be text of at most {0} characters, "
                                "without control characters.").format(RFC2217_SIGNATURE_MAX))
        if self.kind == "net" and self.network.protocol != "modbus":
            self._validate_lines_and_poll()
        if self.kind == "net" and net.tls and net.mode == "client":
            from .engine.tls import parse_pins
            try:
                parse_pins(net.tls_fingerprint)
            except ValueError as e:
                raise ConfigError(_("TLS server fingerprint: {0} is not a SHA-256 fingerprint "
                                    "(64 hex digits, with or without colons).").format(
                    repr(str(e)))) from None

    def check_tls_files(self) -> None:
        """An edit's check (mapping form, so also a cluster peer's save on the node it is
        for; REST API; an import, as a warning), not part of validate()/validate_input(),
        which can't load files: a TLS mapping's certificate, key and CA file must load as
        the mapping will load them (engine/tls.py), its key decrypted with tls_key_password,
        so a key that needs a password, a wrong password, a key that is not the
        certificate's or a CA file without a certificate is refused here rather than found
        out when the mapping starts. Raises ConfigError."""
        net = self.network
        if not (self.kind == "net" and net.tls and net.mode != "udp"):
            return
        from .engine.tls import TlsFileError, mapping_context
        try:
            mapping_context(net)
        except TlsFileError as e:
            raise ConfigError(tls_file_message(e)) from None

    def _validate_lines_and_poll(self) -> None:
        """A serial-to-network mapping's MQTT line end and writes, and its poll string (an
        edit's rules: a stored value they refuse reads as off / "lf")."""
        mq = self.mqtt
        if mq.enabled and mq.line_end not in MQTT_LINE_ENDS:
            raise ConfigError(_("The MQTT line end must be one of {0}.").format(MQTT_LINE_ENDS))
        if mq.enabled and mq.tx_enabled is True and mqtt_topic_problem(mq.base_topic):
            raise ConfigError(_("The MQTT base topic can't contain '+', '#' or the null "
                                "character: no message can be published to such a topic."))
        o = self.options
        interval = o.poll_interval_s
        if not _is_number(interval) or not (interval == 0 or 0.1 <= interval <= 86400):
            raise ConfigError(_("The poll interval must be 0 (off) or between 0.1 and 86400 "
                                "seconds."))
        try:
            data = poll_bytes(o.poll_string)
        except ValueError:
            raise ConfigError(_(r"The poll string can have only \r \n \t \\ or \xHH after a "
                                "backslash.")) from None
        if len(data) > POLL_STRING_MAX:
            raise ConfigError(_("The poll string can be at most {0} bytes.").format(
                POLL_STRING_MAX))
        if interval and not data:
            raise ConfigError(_("A poll interval needs a poll string to send."))

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
            if ip.version == 6 and bind_v4 and not net.both_families:
                raise ConfigError(_("The UDP reply address {0} is IPv6, but the mapping listens "
                                    "on an IPv4 address ({1}).").format(host, net.bind_ip))
            if ip.version == 4 and not bind_v4 and not net.both_families:
                raise ConfigError(_("The UDP reply address {0} is IPv4, but the mapping listens "
                                    "on an IPv6 address ({1}). To reach IPv4 addresses from "
                                    ":: pick “All interfaces (IPv4 + IPv6)”.").format(
                    host, net.bind_ip))
        timeout = net.udp_peer_timeout_s
        if not _is_number(timeout) or not (0 <= timeout <= MAX_TIMER_S):
            raise ConfigError(_("{0} must be between 0 (off) and {1} seconds.").format(
                _("Peer timeout"), MAX_TIMER_S))
        self._validate_udp_targets()

    def _validate_udp_targets(self) -> None:
        """A UDP mapping's targets: IP:port entries of the family the mapping listens on, at
        most UDP_TARGETS_MAX; 255.255.255.255 needs udp_broadcast (an edit's rules: a stored
        malformed one makes the mapping fail to start, see udp_targets)."""
        net = self.network
        if not isinstance(net.udp_targets, list):
            raise ConfigError(_("The UDP targets must be a list of IP:port entries."))
        if len(net.udp_targets) > UDP_TARGETS_MAX:
            raise ConfigError(_("At most {0} UDP targets can be set.").format(UDP_TARGETS_MAX))
        try:
            bind_v4 = ipaddress.ip_address(net.bind_ip).version == 4
        except ValueError:
            bind_v4 = False  # validate() reports a bad bind IP
        for entry in net.udp_targets:
            try:
                host, _port = udp_target(entry)
            except ValueError:
                raise ConfigError(_("UDP target {0} is not an IP address and a port (IP:port, "
                                    "[IPv6]:port).").format(repr(entry))) from None
            ip = ipaddress.ip_address(host)
            if not net.both_families and (ip.version == 6) == bind_v4:
                raise ConfigError(_("UDP target {0} is IPv{1}, but the mapping listens on {2}.")
                                  .format(entry, ip.version, net.bind_ip))
            if host == "255.255.255.255" and net.udp_broadcast is not True:
                raise ConfigError(_("UDP target {0} is the broadcast address: tick “Allow "
                                    "broadcast”.").format(entry))


POLL_STRING_MAX = 1024  # bytes (an edit's rule)
_POLL_ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|[rnt\\])|\\")


def poll_bytes(text: Any) -> bytes:
    r"""options.poll_string as the bytes sent: UTF-8, with \r \n \t \\ and \xHH for those
    bytes. Raises ValueError for another backslash sequence (an edit refuses it; a stored
    one: the poll is off)."""
    if not isinstance(text, str):
        raise ValueError("not text")
    out = bytearray()
    pos = 0
    for m in _POLL_ESCAPE.finditer(text):
        out += text[pos:m.start()].encode("utf-8")
        esc = m.group(1)
        if esc is None:
            raise ValueError(f"unknown escape at character {m.start() + 1} (use \\r \\n "
                             "\\t \\\\ or \\xHH)")
        out += (bytes([int(esc[1:], 16)]) if esc[0] == "x"
                else {"r": b"\r", "n": b"\n", "t": b"\t", "\\": b"\\"}[esc])
        pos = m.end()
    out += text[pos:].encode("utf-8")
    return bytes(out)


def mqtt_topic_problem(base_topic: Any) -> bool:
    """'+' / '#' (wildcards: a message can't be published to them, and a subscription would
    take every matching topic) or U+0000 in a base topic."""
    return any(c in str(base_topic) for c in "+#\x00")


def mqtt_tx_topics(m: "MappingConfig") -> list[tuple[str, str, Any]]:
    """The topics a serial-to-network mapping's MQTT writes use, as (topic, use, None): use
    "tx" (the command topic it takes writes from) and "tx_result" (its answers); none when
    it takes no writes (tx_enabled off, MQTT off, a Modbus gateway). The plain-format topic
    rules of the Modbus gateway (no wildcard, the status topic reserved) apply to them."""
    mq = m.mqtt
    if not (m.kind == "net" and m.network.protocol != "modbus" and mq.enabled
            and mq.tx_enabled is True):
        return []
    base = str(mq.base_topic).rstrip("/")
    return [(f"{base}/tx", "tx", None), (f"{base}/tx/result", "tx_result", None)]


def tls_file_message(e: Any) -> str:
    """The UI's text for an engine.tls.TlsFileError."""
    if e.kind == "unreadable":
        return _("The TLS file {0} can't be read: {1}").format(e.path, e.detail)
    if e.kind == "password":
        return _("The TLS key {0} is password-protected: enter its password (TLS key "
                 "password).").format(e.path)
    if e.kind == "unusable":
        return _("The TLS certificate {0} can't be used with its key ({1}). Is the key "
                 "password right, and is it the certificate's key?").format(e.path, e.detail)
    if e.kind == "no_cert":
        return _("Enable TLS requires both a certificate and a key.")
    if e.kind == "ca_unreadable":
        return _("The CA file {0} can't be read: {1}").format(e.path, e.detail)
    if e.kind == "ca_empty":
        return _("The CA file {0} holds no certificate (PEM or DER).").format(e.path)
    return str(e)


# ---------------------------------------------------------------------------
# Global application config
# ---------------------------------------------------------------------------
@dataclass
class AdminUI:
    bind_ip: str = "127.0.0.1"
    port: int = 8080
    tls_cert: str = ""
    tls_key: str = ""
    # decrypts an encrypted tls_key (engine/tls.py: never asked for on a terminal)
    tls_key_password: str = ""
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
    # With admin TLS, responses tell the browser to use only https:// for this host name
    # - on every port - for this many seconds after each visit (Strict-Transport-Security);
    # 0 sends max-age=0, which turns that off and makes browsers forget it. Settings ->
    # Admin TLS.
    hsts_max_age: int = HSTS_MAX_AGE_DEFAULT
    # Behind a reverse proxy (Settings -> Reverse proxy; app/web/proxy.py). public_url: the
    # address users open, such as https://ser2net.example.com, or with a path the UI is then
    # also served under (https://gw.example.com/ser2net); '' = none. trusted_proxies: the
    # addresses / networks whose X-Forwarded-For and X-Forwarded-Proto are believed; none by
    # default. proxy_hsts: HSTS (hsts_max_age) also on HTTPS requests through a proxy.
    public_url: str = ""
    trusted_proxies: list[str] = field(default_factory=list)
    proxy_hsts: bool = False
    # A name for this instance (Settings -> Admin interface; SER2NET_NODE_NAME for one run
    # instead), for instances that share a host name: notifications, the cluster's fleet
    # view, the browser tab and the top bar use it. '' = the host name, as before.
    node_name: str = ""
    # Which web UI requests all.log gets an "HTTP <ip> <method> <path> -> <status>" line
    # for (Settings -> Audit log): "all" (every page and change but the static files and
    # the automatic refreshes, as before), "changes" (only requests that change something,
    # and refused or failed ones: status 400 and up) or "off". Sign-ins, failed sign-ins
    # and the audit records are logged whatever it says.
    request_log: str = "all"
    # A notice the sign-in and first-run setup pages show before anyone signs in (Settings
    # -> Web UI access): who runs this system, what is recorded, a privacy notice or a
    # system-use warning. Plain text (line breaks kept, never HTML), at most
    # LOGIN_NOTICE_MAX characters; login_notice_url: an http(s) link to the full text.
    # '' = none, as before.
    login_notice: str = ""
    login_notice_url: str = ""

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)


NODE_NAME_MAX = 64
NODE_NAME_ENV = "SER2NET_NODE_NAME"


def node_name_problem(value: Any) -> str | None:
    """Why `value` can't be admin_ui.node_name, or None when it can (also when blank): at
    most NODE_NAME_MAX characters and no control characters or line breaks (it goes into
    e-mail subjects, where one would stop every message)."""
    if not isinstance(value, str):
        return _("The instance name must be text.")
    text = value.strip()
    if len(text) > NODE_NAME_MAX:
        return _("The instance name can have at most {0} characters.").format(NODE_NAME_MAX)
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in text):
        return _("The instance name can’t have control characters or line breaks.")
    return None


def node_name_from_env(env=None) -> str:
    """SER2NET_NODE_NAME when it is set and usable, else ''."""
    raw = ((os.environ if env is None else env).get(NODE_NAME_ENV) or "").strip()
    return raw if raw and node_name_problem(raw) is None else ""


def node_name(admin: "AdminUI", env=None) -> str:
    """This instance's name ('' = none: the host name is used): SER2NET_NODE_NAME when it
    is set and usable, else admin_ui.node_name."""
    return node_name_from_env(env) or admin.node_name


def node_label(admin: "AdminUI") -> str:
    """What names this instance where the host name did up to 2.8: node_name(), else the
    host name (notifications, the cluster's beacon and fleet view)."""
    return node_name(admin) or socket.gethostname()


REQUEST_LOG_MODES = ("all", "changes", "off")
LOGIN_NOTICE_MAX = 2000
LOGIN_NOTICE_URL_MAX = 500
# characters a sign-in notice keeps out: control characters but tab and line feed
_NOTICE_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_login_notice(text: str) -> str:
    """A sign-in notice as it is kept: line breaks as line feeds, no other control
    characters, no white space at its ends."""
    return _NOTICE_CONTROL.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n")).strip()


def login_notice_problem(text: Any) -> str | None:
    """Why `text` can't be admin_ui.login_notice, or None when it can (also when blank)."""
    if not isinstance(text, str):
        return _("The sign-in notice must be text.")
    if len(clean_login_notice(text)) > LOGIN_NOTICE_MAX:
        return _("The sign-in notice can have at most {0} characters.").format(LOGIN_NOTICE_MAX)
    return None


def login_notice_url_problem(url: Any) -> str | None:
    """Why `url` can't be admin_ui.login_notice_url, or None when it can (also when blank):
    an http:// or https:// address with a host name, without a user name or password."""
    bad = _("The notice’s link must be an http:// or https:// address, such as "
            "https://example.com/privacy.")
    if not isinstance(url, str):
        return bad
    text = url.strip()
    if not text:
        return None
    if len(text) > LOGIN_NOTICE_URL_MAX:
        return _("The notice’s link can have at most {0} characters.").format(
            LOGIN_NOTICE_URL_MAX)
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text):
        return bad
    try:
        parts = urllib.parse.urlsplit(text)
        parts.port  # noqa: B018 (raises ValueError for a port that is no number)
    except ValueError:
        return bad
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return bad
    return None


def _normalize_login_notice(admin: "AdminUI", notes: list[str]) -> None:
    """A hand-edited sign-in notice or link that can't be used is left out (a notice too
    long is cut), with a load note."""
    text = admin.login_notice
    if not isinstance(text, str):
        notes.append(f"admin_ui.login_notice is not text ({type(text).__name__}); no sign-in "
                     "notice is shown")
        admin.login_notice = ""
    else:
        admin.login_notice = clean_login_notice(text)
        if len(admin.login_notice) > LOGIN_NOTICE_MAX:
            notes.append(f"admin_ui.login_notice has {len(admin.login_notice)} characters; "
                         f"the first {LOGIN_NOTICE_MAX} are shown")
            admin.login_notice = admin.login_notice[:LOGIN_NOTICE_MAX].rstrip()
    url = admin.login_notice_url
    if login_notice_url_problem(url) is not None:
        notes.append(f"admin_ui.login_notice_url {str(url)[:80]!r} is not an http(s) address; "
                     "the sign-in notice has no link")
        admin.login_notice_url = ""
    else:
        admin.login_notice_url = url.strip()


def _normalize_request_log(admin: "AdminUI", notes: list[str]) -> None:
    """A hand-edited admin_ui.request_log other than all / changes / off logs every request,
    as before the setting existed, with a load note."""
    if admin.request_log not in REQUEST_LOG_MODES:
        notes.append(f"admin_ui.request_log {str(admin.request_log)[:40]!r} is not one of "
                     f"{', '.join(REQUEST_LOG_MODES)}; using all")
        admin.request_log = "all"


def _normalize_node_name(admin: "AdminUI", notes: list[str]) -> None:
    """A hand-edited admin_ui.node_name that can't be used is left out, with a load note."""
    value = admin.node_name
    if value == "":
        return
    if node_name_problem(value) is not None:
        notes.append(f"admin_ui.node_name {str(value)[:80]!r} is not usable "
                     f"({node_name_problem(value)}); the host name is used")
        admin.node_name = ""
    else:
        admin.node_name = value.strip()


# The first path segments of the UI's own pages: a public URL's path can't start with one,
# or a page asked for without the path would be taken for one under it (app/web/proxy.py).
RESERVED_PATHS = frozenset({"api", "auth", "favicon.ico", "healthz", "lang", "login", "logout",
                            "metrics", "settings", "setup", "static"})
_URL_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")
_HOST_NAME = re.compile(r"(?!-)[a-z0-9_-]{1,63}(?<!-)(\.(?!-)[a-z0-9_-]{1,63}(?<!-))*\.?")


def _public_url_parts(value: str):
    """(scheme, host as written in a URL, port or None, path without the trailing '/') of
    a public URL, or the problem (a str) with it."""
    text = value.strip()
    bad = _("The public URL must be an http:// or https:// address with a host name, such "
            "as https://ser2net.example.com.")
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in text) or not text.isascii():
        return bad
    if "?" in text or "#" in text:
        return _("The public URL can’t have a query (?…) or a fragment (#…).")
    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        return _("The public URL’s port must be a number from 1 to 65535.")
    scheme, host = parts.scheme.lower(), parts.hostname or ""
    if scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return bad
    if port == 0:
        return _("The public URL’s port must be a number from 1 to 65535.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_NAME.fullmatch(host):
            return bad
        written = host
    else:
        written = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    path = parts.path.rstrip("/")
    segments = path.split("/")[1:] if path else []
    if any(not _URL_PATH_SEGMENT.fullmatch(s) or s in (".", "..") for s in segments):
        return _("The public URL’s path may only have letters, digits and . _ ~ - between "
                 "its slashes, such as /ser2net.")
    if segments and segments[0].lower() in RESERVED_PATHS:
        return _("The public URL’s path can’t start with /{0}: that is a page of this UI.") \
            .format(segments[0])
    return scheme, written, port, path


def public_url_problem(value: Any) -> str | None:
    """Why `value` can't be admin_ui.public_url, or None when it can (also when blank): an
    http:// or https:// address with a host name, optionally a port and a path of plain
    segments that doesn't start like one of the UI's own pages (RESERVED_PATHS); no user
    name, query or fragment."""
    if not isinstance(value, str):
        return _("The public URL must be an http:// or https:// address with a host name, "
                 "such as https://ser2net.example.com.")
    if not value.strip():
        return None
    parts = _public_url_parts(value)
    return parts if isinstance(parts, str) else None


def normalize_public_url(value: str) -> str:
    """A usable public URL as it is kept: scheme and host in lower case, the scheme's own
    port and a trailing '/' left out ('' for a blank or unusable one)."""
    parts = _public_url_parts(value) if isinstance(value, str) and value.strip() else ""
    if isinstance(parts, str):
        return ""
    scheme, host, port, path = parts
    shown = "" if port is None or port == {"http": 80, "https": 443}[scheme] else f":{port}"
    return f"{scheme}://{host}{shown}{path}"


def proxy_entry_problem(entry: Any) -> str | None:
    """Why `entry` can't be one of admin_ui.trusted_proxies (an IP address or a network
    such as 172.18.0.0/16), or None when it can."""
    text = entry.strip() if isinstance(entry, str) else ""
    try:
        if not text or not text.isascii():
            raise ValueError
        ipaddress.ip_network(text, strict=False)
    except ValueError:
        return _("“{0}” is not an IP address or network (such as 172.18.0.0/16).").format(
            str(entry)[:100])
    return None


def split_proxy_entries(text: str) -> list[str]:
    """The entries of a list of trusted proxies written on one line (spaces or commas)."""
    return [e for e in re.split(r"[\s,]+", text or "") if e]


def _normalize_proxy_settings(admin: "AdminUI", notes: list[str]) -> None:
    """A hand-edited public_url, trusted_proxies or proxy_hsts that can't be used is left
    out, with a load note, rather than refused: config.json must not keep the UI from
    starting."""
    value = admin.trusted_proxies
    entries = split_proxy_entries(value) if isinstance(value, str) else value
    if not isinstance(entries, list):
        notes.append(f"admin_ui.trusted_proxies {value!r} is not a list of IP addresses or "
                     "networks; no proxy is trusted")
        entries = []
    kept = [e.strip() for e in entries if proxy_entry_problem(e) is None]
    if len(kept) != len(entries):
        dropped = [e for e in entries if proxy_entry_problem(e) is not None]
        notes.append(f"admin_ui.trusted_proxies: {dropped!r} left out (not IP addresses or "
                     "networks)")
    admin.trusted_proxies = kept
    url = admin.public_url
    if public_url_problem(url) is not None:
        notes.append(f"admin_ui.public_url {url!r} is not usable ({public_url_problem(url)}); "
                     "none is used")
        admin.public_url = ""
    else:
        admin.public_url = normalize_public_url(url)
    if not isinstance(admin.proxy_hsts, bool):
        notes.append(f"admin_ui.proxy_hsts {admin.proxy_hsts!r} is not true or false; using false")
        admin.proxy_hsts = False


# admin_ui connection limits: (default, lowest value other than 0, highest)
_ADMIN_LIMITS = {"max_connections": (256, 16, 65535),
                 "max_connections_per_ip": (64, 8, 65535),
                 "request_timeout_s": (10, 1, 3600)}


def _normalize_admin_limits(admin: "AdminUI", notes: list[str]) -> None:
    """Put an admin_ui connection limit that is not a usable whole number back to its
    default (0, no limit, stays 0; a value below the lowest goes up to it), and so an
    unusable hsts_max_age, with a load note: a hand-edited config.json must not keep the
    UI from starting, nor make it unusable."""
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
    value = admin.hsts_max_age
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= HSTS_MAX_AGE_MAX:
        admin.hsts_max_age = HSTS_MAX_AGE_DEFAULT
        notes.append(f"admin_ui.hsts_max_age {value!r} is not a whole number of seconds from 0 "
                     f"to {HSTS_MAX_AGE_MAX}; using {HSTS_MAX_AGE_DEFAULT} (one year)")


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


NOTIFY_EVENTS = ("device_lost", "device_back", "start_failed", "link_lost", "link_back",
                 "client_connected", "client_disconnected", "login_failed")
# (a saved events list is kept as it is: these are what a config without one gets)
NOTIFY_DEFAULT_EVENTS = ("device_lost", "device_back", "start_failed", "link_lost", "link_back")
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
    # Personal data in what is sent (client_connected / client_disconnected: the client's
    # IP address; login_failed: the account name and the IP address): with
    # mask_personal_data, an address only as its network (IPv4 /24, IPv6 /48) and no
    # account name. email_separately: each recipient gets a message of their own, so the
    # recipients don't see each other's addresses. Both off by default (as up to 2.8).
    mask_personal_data: bool = False
    email_separately: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "NotificationSettings":
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(NotificationSettings)}
        n = NotificationSettings(**{k: v for k, v in d.items() if k in known})
        n.events = [e for e in (n.events or []) if e in NOTIFY_EVENTS]
        # a hand-edited value other than true is off, as before these settings existed
        n.mask_personal_data = n.mask_personal_data is True
        n.email_separately = n.email_separately is True
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
        _normalize_proxy_settings(cfg.admin_ui, cfg.load_notes)
        _normalize_node_name(cfg.admin_ui, cfg.load_notes)
        _normalize_request_log(cfg.admin_ui, cfg.load_notes)
        _normalize_login_notice(cfg.admin_ui, cfg.load_notes)
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
            raw_net = (raw or {}).get("network") or {}
            if m.network.dual_stack is True and "dual_stack" not in raw_net:  # (from_dict)
                cfg.load_notes.append(
                    f"mapping '{m.name}': a UDP mapping on :: now takes IPv6 datagrams only, "
                    "unless it listens for IPv4 and IPv6 (dual_stack); this one took IPv4 "
                    "datagrams too on this system, so it is set to do so")
            raw_bind = raw_net.get("bind_ip")
            if isinstance(raw_bind, str) and raw_bind != m.network.bind_ip:
                cfg.load_notes.append(
                    f"mapping '{m.name}': its bind IP {raw_bind!r} is now written "
                    f"{m.network.bind_ip!r}"
                    + (" (an IPv4-mapped IPv6 address: the listener takes that IPv4 address)"
                       if ":" in raw_bind and ":" not in m.network.bind_ip else ""))
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
                odd = sorted(str(k) for k in side.match if k not in MATCH_KEYS) \
                    if isinstance(side.match, dict) else []
                if odd:
                    cfg.load_notes.append(
                        f"mapping '{m.name}': the USB identity of serial port {side.port!r} "
                        f"(match) has keys ser2net does not know ({', '.join(odd)}), which are "
                        f"not used; it knows {', '.join(MATCH_KEYS)}"
                        + ("" if side.identity else ", and none of them is set, so the port "
                           "is opened by its name"))
        enabled: list[MappingConfig] = []
        for m in cfg.mappings:
            clash = _serial_clash(m, enabled)
            if clash is not None:
                other, side, oside = clash
                if side.identity:
                    cfg.load_notes.append(
                        f"mappings '{other.name}' and '{m.name}' are both bound to the USB "
                        f"identity {identity_text(side.match)} on serial port {side.port!r}: "
                        "only one of them can open it")
                elif side.port != oside.port:  # (the same name is a problem validate() reports)
                    cfg.load_notes.append(
                        f"mappings '{other.name}' and '{m.name}' both use serial port "
                        f"{oside.port!r} (written {side.port!r} in '{m.name}'): only one of "
                        "them can open it")
            if m.enabled:
                enabled.append(m)
        return cfg

    def serial_conflict(self, m: MappingConfig) -> str:
        """serial_conflict of ``m`` next to this config's mappings."""
        return serial_conflict(m, self.mappings)

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

    def find_moved_tls_files(self, data_dir: Optional[str] = None) -> list[str]:
        """TLS files that are not where the config says, because the data directory was
        moved or restored elsewhere (a native install moved to Docker's /data, a Windows
        service given another --data-dir, a backup restored on another machine), taken from
        the tls folder of ``data_dir`` (else ConfigStore's; moved_tls_file): the admin UI's
        certificate and key (the pair Settings → Admin TLS generates is always there), and
        a mapping's. A pair is taken only when both of its files are found. Changes the
        config (to absolute paths, which older releases read the same) and returns what it
        did, one log line each."""
        base = data_dir or _data_dir
        notes: list[str] = []
        if not base:
            return notes

        def take(owner: Any, label: str, attrs: tuple[str, ...]) -> None:
            paths = [getattr(owner, a) for a in attrs]
            if not all(paths):
                return
            found = [moved_tls_file(p, base) for p in paths]
            if not all(found):
                return
            for attr, old, new in zip(attrs, paths, found, strict=True):
                setattr(owner, attr, new)
                notes.append(f"{label}{attr}: {old!r} is not there; {new!r} of this data "
                             "directory is used (the data directory was moved or restored)")

        take(self.admin_ui, "admin_ui.", ("tls_cert", "tls_key"))
        for m in self.mappings:
            label = f"mapping '{m.name}': "
            take(m.network, label, ("tls_cert", "tls_key"))
            for attr in ("tls_ca", "tls_client_ca"):
                take(m.network, label, (attr,))
        return notes

    def admin_key_reuse(self, m: MappingConfig) -> str:
        """Why mapping ``m`` can't be saved with its TLS key ("" when it can): the key is the
        admin UI's own key file, so the mapping would present the admin UI's certificate on
        another port of this host, and browsers send a site's cookies to every port of it.
        Checked where a mapping is created or changed (the form, so also a cluster peer's
        save on its own node, and the REST API), never by validate(): a stored one keeps
        loading and saving."""
        net, admin = m.network, self.admin_ui
        if not (m.kind == "net" and net.tls and net.tls_key and admin.tls_key):
            return ""
        try:
            same = os.path.samefile(data_path(net.tls_key), data_path(admin.tls_key))
        except (OSError, TypeError, ValueError):  # one of them is missing: not the same file
            return ""
        if not same:
            return ""
        return _("The TLS key {0} is the admin UI's own key. Give the mapping a certificate "
                 "and key of its own.").format(net.tls_key)

    def own_port_conflict(self, m: MappingConfig) -> str:
        """Why mapping ``m`` can't listen where it is set to next to ser2net's own
        listeners ("" when it can): a TCP listener on the admin UI's address and port, or a
        UDP one on the LAN cluster's discovery port while discovery is on (it would get the
        other nodes' beacons). Checked where a mapping is created or changed (the form, the
        REST API, a cluster peer, an import, a copy), never by validate(): a stored clash
        must not stop a start or another save, and the admin UI wins at start-up anyway
        (it listens before the mappings start), so such a mapping only fails to start."""
        if m.kind != "net" or not m.network.listens:
            return ""
        net = m.network
        if net.mode == "server":
            admin = self.admin_ui
            # "" binds every interface of both families (as asyncio and uvicorn read it),
            # "localhost" both loopback addresses
            ips = {"": ["0.0.0.0", "::"], "localhost": ["127.0.0.1", "::1"]}.get(
                admin.bind_ip, [admin.bind_ip])
            if net.port == admin.port and any(
                    _ip_overlaps(ip, net.bind_ip, b_both=net.both_families) for ip in ips):
                return _("Mapping '{0}': TCP port {1} on {2} is the admin UI's port ({3}:{4}), "
                         "so the mapping could not listen there. Pick another port.").format(
                    m.name, net.port, net.bind_shown, shown_host(admin.bind_ip), admin.port)
            return ""
        cluster = self.cluster
        try:
            discovery = int(cluster.discovery_port)
        except (TypeError, ValueError):
            return ""
        # discovery listens on IPv4 (0.0.0.0): an IPv6-only listener doesn't get its beacons
        if (cluster.active and cluster.discovery and net.port == discovery and _ip_overlaps(
                "0.0.0.0", net.bind_ip, b_both=net.both_families)):
            return _("Mapping '{0}': UDP port {1} is the LAN cluster's discovery port, so the "
                     "mapping would get the other nodes' beacons. Pick another port (or "
                     "another discovery port under Settings → LAN cluster).").format(
                m.name, net.port)
        return ""

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
            if not os.path.isfile(data_path(path)):
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
        listeners: list[tuple[str, int, MappingConfig]] = []   # (proto, port, mapping)
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
            # A wildcard collides with every address of its family on that port (of both,
            # when it takes both: see _ip_overlaps); treat any overlap as a clash.
            net = m.network
            for sproto, bport, other in listeners:
                onet = other.network
                if sproto == proto and bport == net.port and _ip_overlaps(
                        onet.bind_ip, net.bind_ip, onet.both_families, net.both_families):
                    found.append((f"{proto} {net.port} {other.id} {m.id}", ConfigError(
                        _("Mappings '{0}' and '{1}' both use {2} port {3} on overlapping "
                          "addresses ({4} / {5}).").format(other.name, m.name, proto.upper(), net.port, onet.bind_ip, net.bind_ip))))
            listeners.append((proto, net.port, m))
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


def _ip_overlaps(a: str, b: str, a_both: bool = False, b_both: bool = False) -> bool:
    """Whether listeners on bind IPs ``a`` and ``b`` (on one port) need the same address:
    the same address however it is written ('::' and '0:0:0:0:0:0:0:0'), or a wildcard and
    an address of its family. A listener on :: takes IPv6 only (IPV6_V6ONLY, TCP and UDP),
    so it can share a port with one on 0.0.0.0 or another IPv4 address; one that takes both
    families (``a_both`` / ``b_both``: NetworkSettings.both_families) overlaps either."""
    xs = list(WILDCARDS) if a_both else [a]
    ys = list(WILDCARDS) if b_both else [b]
    return any(_address_overlaps(x, y) for x in xs for y in ys)


def _address_overlaps(a: str, b: str) -> bool:
    x, y = canonical_ip(a), canonical_ip(b)
    if x == y:
        return True
    try:
        ax, ay = ipaddress.ip_address(x), ipaddress.ip_address(y)
    except ValueError:
        return False  # not an address (validate() says so): only the same text overlaps
    return ax.version == ay.version and (ax.is_unspecified or ay.is_unspecified)


# ---------------------------------------------------------------------------
# Atomic store
# ---------------------------------------------------------------------------
BACKUPS_KEPT = 50
_BACKUP_NAME = re.compile(r"^config-\d{8}T\d{6}Z(-\d+)?\.json$")


class ConfigStore:
    """config.json with atomic writes, plus automatic backups of earlier versions.

    Every save that changes config.json first copies the version it replaces to
    <data>/backups/config-<UTC time>.json (identical content is not copied twice; the
    newest BACKUPS_KEPT are kept). Backups are never read back on their own: restoring one
    is an explicit admin action (deleting config.json alone leaves them, and the next admin
    sees them in Settings). An admin can delete backups, or remove a deleted account from
    every backup (remove_account), in Settings -> Configuration backups."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        set_data_dir(directory)  # (relative TLS paths: data_path)
        os.makedirs(directory, exist_ok=True)
        # config.json holds the password hash + secret_key; keep the dir private on
        # BOTH platforms (POSIX chmod 0700 / Windows icacls owner-only inheritance).
        lock_down_dir(directory)
        self.backup_dir = os.path.join(directory, "backups")

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
        """[{name, time (UTC, 'YYYY-MM-DD HH:MM:SS'), size, mappings, users, accounts:
        [{username, role, source}]}], newest first."""
        out = []
        for name in self.backup_names():
            path = os.path.join(self.backup_dir, name)
            info: dict[str, Any] = {"name": name, "size": 0, "mappings": None, "users": None,
                                    "accounts": [], "time": backup_time(name)}
            try:
                info["size"] = os.path.getsize(path)
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
                info["mappings"] = len(doc.get("mappings") or [])
                info["accounts"] = _backup_accounts(doc)
                info["users"] = len(info["accounts"])
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            out.append(info)
        return out

    def _known_backup(self, name: str) -> str:
        """The path of the backup `name`; ConfigError for an unknown / invalid name."""
        if not _BACKUP_NAME.match(name or "") or name not in self.backup_names():
            raise ConfigError(_("No such backup."))
        return os.path.join(self.backup_dir, name)

    def delete_backup(self, name: str) -> None:
        """Delete one backup (ConfigError for an unknown name)."""
        os.remove(self._known_backup(name))

    def delete_backups(self) -> int:
        """Delete every backup; how many were deleted."""
        gone = 0
        for name in self.backup_names():
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(self.backup_dir, name))
                gone += 1
        return gone

    def remove_account(self, username: str) -> int:
        """Take the account named exactly `username` out of every backup that has it (in
        a backup from before accounts existed, the top-level password_hash is the 'admin'
        account), rewriting each one atomically under its name with everything else as
        it was: restoring those backups can't bring the account back. Returns how many
        backups were changed."""
        changed = 0
        for name in self.backup_names():
            path = os.path.join(self.backup_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and _drop_backup_account(doc, username):
                _replace_file(self.backup_dir, path, json.dumps(doc, indent=2, ensure_ascii=False))
                changed += 1
        return changed

    def read_backup(self, name: str) -> str:
        """The text of one backup. Raises ConfigError for an unknown / invalid name
        (only names this store generated are accepted: no path tricks)."""
        with open(self._known_backup(name), encoding="utf-8") as fh:
            return fh.read()


def _backup_accounts(doc: dict) -> list[dict]:
    """The accounts in a backup's JSON: [{username, role, source}] (a config from before
    accounts existed has one, 'admin', as its top-level password_hash)."""
    users = doc.get("users")
    if isinstance(users, list) and users:
        return [{"username": str(u.get("username", "")), "role": str(u.get("role", "")),
                 "source": str(u.get("source") or "local")} for u in users if isinstance(u, dict)]
    if doc.get("password_hash"):
        return [{"username": "admin", "role": "admin", "source": "local"}]
    return []


def _drop_backup_account(doc: dict, username: str) -> bool:
    """Take the account named `username` out of a backup's JSON; True if it was there."""
    users = doc.get("users")
    if isinstance(users, list) and users:
        kept = [u for u in users if not (isinstance(u, dict) and u.get("username") == username)]
        if len(kept) == len(users):
            return False
        doc["users"] = kept
        return True
    if username == "admin" and doc.get("password_hash"):
        doc.pop("password_hash", None)
        doc.pop("pwd_version", None)
        return True
    return False


def _replace_file(directory: str, path: str, text: str) -> None:
    """Write `text` to `path` atomically (a temp file in `directory`, fsync, rename)."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bak.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
