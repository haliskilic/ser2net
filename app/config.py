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

import calendar
import contextlib
import dataclasses
import functools
import hashlib
import hmac
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
import typing
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional, Union
from . import __version__, i18n
from .i18n import LANGUAGES, _
from .textfold import fold  # how names compare; still importable as app.config.fold

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
# a mapping's no-data alarm (MappingOptions.no_data_alarm_s): looked at every 2 s
NO_DATA_MIN_S = 10
NO_DATA_MAX_S = 7 * 86400
# Modbus gateway response timeout: the default, and what an edit may set
MODBUS_TIMEOUT_DEFAULT_S = 1.0
MODBUS_TIMEOUT_RANGE_S = (0.01, 60.0)
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
# trace files not written for this many days are deleted by the hourly maintenance
# (AppConfig.trace_max_age_days, AppState.run_trace_maintenance); 0 = kept, the default
TRACE_DAYS_MAX = 3650
# log and audit records and configuration backups older than this many days are deleted by
# the hourly maintenance (AppConfig.log_max_age_days, AppState.run_retention); 0 = only
# their size and count limits, the default
LOG_DAYS_MAX = 3650


class ConfigError(ValueError):
    """Raised when a config object fails validation. Message is user-facing."""


class ConfigWriteError(ConfigError):
    """A change that validates but can't be written to config.json (a full or read-only
    disk, a file another program holds): AppState.asave raises it for the OSError, so
    whoever shows a refused save shows this one too. Message is user-facing."""


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


def _choices(values) -> str:
    """Allowed values for a message: "5, 6, 7, 8", not the tuple's "(5, 6, 7, 8)"."""
    return ", ".join(str(v) for v in values)


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


# ---------------------------------------------------------------------------
# Reading JSON (config.json, the REST API, an import, a form) into the typed model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldNote:
    """What reading a JSON object into the model did with one of its values (_read_fields),
    by its path in that object ("serial.baudrate", "modbus_poll.points[0].scale"):
      unknown   - no setting of this version: left out (an edit warns about it);
      converted - another JSON type, read as the field's type ("false" as false, "9600" as
                  9600): an edit takes it as meant, loading config.json notes it;
      invalid   - can't be read as the field's type: `used` instead, and an edit refuses
                  it (`expected`: what it must be, a key of _EXPECTED);
      replaced  - not supported together with the other settings: `used` instead, with a
                  warning (`why` in English for the log, `warning` in the language of the
                  request that sent it);
      switch_text - a protective switch (_PROTECTIVE) given as text: loading reads it as
                  before 2.9.0 (`used`: any text but "" is on), an edit refuses it.
    A switch that is on only for JSON true (_ONLY_TRUE) given anything else is invalid
    (`used`: false)."""
    path: str
    kind: str
    value: Any = None
    used: Any = None
    expected: str = ""
    why: str = ""
    warning: str = ""

    def text(self) -> str:
        """The note in English, for the log (AppConfig.load_notes)."""
        if self.kind == "unknown":
            return f"{self.path} is not a setting of this version and is ignored"
        if self.kind == "invalid":
            return (f"{self.path} {_shown(self.value)} is not {_EXPECTED[self.expected]}: "
                    f"{_shown(self.used)} is used")
        if self.kind == "converted":
            text = f"{self.path} {_shown(self.value)} is read as {_shown(self.used)}"
            if self.used is False and isinstance(self.value, str) and self.value.strip():
                text += " (versions before 2.9.0 took any text but \"\" as true)"
            return text
        return self.why

    def error(self) -> str:
        """An edit's refusal of an invalid value, in the UI's language."""
        if self.kind == "switch_text":
            return _("{0}: use true or false (JSON booleans), not a string.").format(self.path)
        return _("{0}: {1} is not {2}.").format(self.path, _shown(self.value),
                                                _expected(self.expected))


# what a field of each kind must be (FieldNote.expected), for the log ...
_EXPECTED = {"bool": "true or false", "int": "a whole number", "number": "a finite number",
             "str": "text", "object": "a JSON object"}


def _expected(kind: str) -> str:
    """... and in the UI's language."""
    return {"bool": _("true or false"), "int": _("a whole number"),
            "number": _("a finite number"), "str": _("text"),
            "object": _("a JSON object")}[kind]


def _shown(value: Any, limit: int = 60) -> str:
    """A value as JSON writes it, cut to `limit` characters, for messages (escaped where
    it holds text UTF-8 can't carry, an unpaired surrogate: the message must reach a page,
    a JSON answer and the log)."""
    try:
        text = json.dumps(value, ensure_ascii=False)
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(value)
    except (TypeError, ValueError):
        text = repr(value).encode("utf-8", "backslashreplace").decode("utf-8")
    return text if len(text) <= limit else text[:limit - 1] + "…"


_TRUE_WORDS = ("true", "1", "on", "yes")
_FALSE_WORDS = ("false", "0", "off", "no", "")
# Field metadata of a switch that protects something (login, TLS, certificate checks,
# read-only). Loading reads text in it as before 2.9.0 — on unless empty — so an upgrade
# never weakens a hand-edited config whose "false" in quotes kept it on; an edit refuses
# text in it, so a client sending "false" learns it was not taken (_read_fields)
_PROTECTIVE = {"protective": True}
# Field metadata of a switch that is on only for JSON true: what this release added and
# checked that way from the start (`is True`: a listener that takes both families, MQTT
# writes to the device, HSTS through a proxy ...). Any other value reads as false, with a
# note, and an edit refuses it, so no text or number turns one on
_ONLY_TRUE = {"only_true": True}


def _text_number(text: str) -> Any:
    """The number a text spells (an int when it is one), else None."""
    t = text.strip()
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return None


def _coerce(value: Any, hint: Any) -> tuple[bool, Any]:
    """`value` read as a field of type `hint`: (True, the value) — converted from another
    JSON type when that needs no guess — or (False, None) when it can't be. null is an
    unset value: false, "", [] or {} (a number: _read_fields takes the field's default).
    A list of text takes any value: a text is a one-entry list, anything else becomes
    text, so no entry of an allow-list is ever dropped (unusable ones are reported where
    they are used: client_networks)."""
    if hint is bool:
        if value is None:
            return True, False
        if isinstance(value, bool):
            return True, value
        if _is_int(value) and value in (0, 1):
            return True, bool(value)
        if isinstance(value, str):
            word = value.strip().lower()
            if word in _TRUE_WORDS:
                return True, True
            if word in _FALSE_WORDS:
                return True, False
        return False, None
    if hint is int or hint is float:
        if isinstance(value, str):
            value = _text_number(value)
        if isinstance(value, bool) or not _is_number(value):
            return False, None
        if isinstance(value, float):
            if not math.isfinite(value):
                return False, None
            if hint is int and value.is_integer():
                return True, int(value)
        elif hint is float:
            try:
                float(value)
            except OverflowError:  # an int too large for any float
                return False, None
        return True, value
    if hint is str:
        if isinstance(value, str):
            return True, value
        if value is None:
            return True, ""
        if _is_number(value):
            return True, str(value)
        return False, None
    origin = typing.get_origin(hint)
    if origin is list and typing.get_args(hint) == (str,):
        if value is None:
            return True, []
        if isinstance(value, str):
            return True, [value] if value.strip() else []
        items = value if isinstance(value, list) else [value]
        return True, [v if isinstance(v, str) else _shown(v, 200) for v in items]
    if origin is dict:
        if value is None:
            return True, {}
        return (True, value) if isinstance(value, dict) else (False, None)
    return True, value


def _kind_of(hint: Any) -> str:
    return {bool: "bool", int: "int", float: "number", str: "str"}.get(hint, "object")


def _same(value: Any, read: Any) -> bool:
    """True when reading `value` changed nothing worth a note (null for an empty value,
    4001.0 for 4001)."""
    if value is None:
        return read is False or (not isinstance(read, bool) and read in ("", [], {}))
    if _is_number(value) and _is_number(read):
        return value == read
    return type(value) is type(read) and value == read


@functools.cache
def _hints(cls) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _field_default(f: dataclasses.Field) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    return f.default_factory()  # type: ignore[operator]


def _read_fields(cls, d: dict[str, Any], notes: list[FieldNote], path: str = "", *,
                 skip: tuple = (), keep: tuple = (),
                 fallback: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Keyword arguments for cls() from the JSON object `d`: each field `d` has, read as the
    field's type (_coerce), except `skip` (the caller reads those). A value that can't be
    takes the field's default — a true/false field the value's truth instead, as earlier
    versions took it, or the field's `fallback`. Keys that are no field (or `keep`, read
    elsewhere) are left out. What it did is added to `notes`, paths prefixed with `path`."""
    hints = _hints(cls)
    out: dict[str, Any] = {}
    names = set()
    for f in dataclasses.fields(cls):
        names.add(f.name)
        if f.name in skip or f.name not in d:
            continue
        value, hint = d[f.name], hints[f.name]
        if value is None and hint in (int, float):  # a number left unset
            out[f.name] = _field_default(f)
            continue
        if f.metadata.get("only_true") and value is not None and not isinstance(value, bool):
            notes.append(FieldNote(path + f.name, "invalid", value, False, "bool"))
            out[f.name] = False
            continue
        if isinstance(value, str) and f.metadata.get("protective"):
            used = bool(value)
            notes.append(FieldNote(
                path + f.name, "switch_text", value, used,
                why=f"{path}{f.name} {_shown(value)} is text, so it is read as "
                    f"{_shown(used)}, as before: this protective switch takes true or false "
                    "(without quotes)"))
            out[f.name] = used
            continue
        ok, read = _coerce(value, hint)
        if not ok:
            if fallback and f.name in fallback:
                read = fallback[f.name]
            else:
                read = bool(value) if hint is bool else _field_default(f)
            notes.append(FieldNote(path + f.name, "invalid", value, read, _kind_of(hint)))
        elif not _same(value, read):
            notes.append(FieldNote(path + f.name, "converted", value, read))
        out[f.name] = read
    for key, value in d.items():
        if key not in names and key not in keep:
            notes.append(FieldNote(f"{path}{key}", "unknown", value))
    return out


def _section(d: dict[str, Any], key: str, path: str = "") -> dict[str, Any]:
    """The JSON object d[key]; an absent or empty one is {} (as before). Anything else
    raises ConfigError: config.json then fails to load, as it did before, rather than
    lose that part of it."""
    value = d.get(key)
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(_("{0} must be a JSON object, not {1}.").format(path + key,
                                                                          _shown(value)))
    return value


def _objects(d: dict[str, Any], key: str, path: str = "") -> list[dict[str, Any]]:
    """The JSON list of objects d[key]; an absent or empty one is []. Raises ConfigError
    for anything else (see _section)."""
    value = d.get(key)
    if not value:
        return []
    if not isinstance(value, list):
        raise ConfigError(_("{0} must be a JSON list, not {1}.").format(path + key,
                                                                       _shown(value)))
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(_("{0} must be a JSON object, not {1}.").format(
                f"{path}{key}[{i}]", _shown(item)))
    return value


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
    if os.name == "nt":  # "tls/cert.pem" -> <data>\tls\cert.pem (the way Windows reads it)
        inside = os.path.normpath(inside)
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
ClientNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def client_networks(entries: Iterable[Any]) -> tuple[list[ClientNetwork], list[str]]:
    """The networks of an allowed / priority client list (an address is a one-host network;
    0.0.0.0 and :: are everyone, see normalize_cidr), and the entries that are none: a host
    name, a range, a comma-joined list ... Edits refuse those (NetworkSettings.validate), so
    only a hand-edited config.json has them; where a list is used, they are left out (an
    allow-list none of whose entries is usable admits nobody: engine/access.AccessPolicy)."""
    nets: list[ClientNetwork] = []
    ignored: list[str] = []
    for entry in entries or []:
        text = entry if isinstance(entry, str) else str(entry)
        try:
            nets.append(ipaddress.ip_network(normalize_cidr(text), strict=False))
        except ValueError:
            ignored.append(text)
    return nets, ignored


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
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "SerialSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        fields = _read_fields(SerialSettings, d, notes, path, skip=("advanced",))
        adv = _section(d, "advanced", path)
        advanced = SerialAdvanced(**_read_fields(SerialAdvanced, adv, notes, path + "advanced."))
        return SerialSettings(advanced=advanced, **fields)

    def validate(self) -> None:
        if not self.port:
            raise ConfigError(_("Serial port is required."))
        if not isinstance(self.baudrate, int) or self.baudrate <= 0:
            raise ConfigError(_("Baud rate must be a positive integer."))
        if self.bytesize not in BYTESIZES:
            raise ConfigError(_("Data bits must be one of {0}.").format(_choices(BYTESIZES)))
        if self.parity not in PARITIES:
            raise ConfigError(_("Parity must be one of {0}.").format(_choices(PARITIES)))
        if self.stopbits not in STOPBITS:
            raise ConfigError(_("Stop bits must be one of {0}.").format(_choices(STOPBITS)))
        if self.flowcontrol not in FLOWCONTROLS:
            raise ConfigError(_("Flow control must be one of {0}.").format(_choices(FLOWCONTROLS)))
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
    dual_stack: bool = field(default=False, metadata=_ONLY_TRUE)
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
    udp_broadcast: bool = field(default=False, metadata=_ONLY_TRUE)
    max_connections: int = 1
    kick_old_user: bool = False
    # TCP (raw / telnet / rfc2217, server and connect-out): while the serial port is not
    # open (unplugged, reconnecting) clients are disconnected and new ones refused, after
    # the short wait for the port that an RFC 2217 client gets (options.
    # rfc2217_net_timeout_s); a connect-out mapping connects only while the port is open.
    # Off (default): clients stay connected and are taken, and what they send for the
    # device meanwhile is dropped (counted in bytes_dropped); RFC 2217 sessions follow a
    # reopened port.
    disconnect_without_device: bool = field(default=False, metadata=_ONLY_TRUE)
    allowed_client_ips: list[str] = field(default_factory=list)
    # High-priority clients: when the port is at capacity, a connection from one of
    # these IPs/CIDRs is admitted by kicking an existing client (oldest non-priority
    # first), regardless of kick_old_user.
    priority_client_ips: list[str] = field(default_factory=list)
    read_only: bool = field(default=False, metadata=_PROTECTIVE)
    # Per-client outbound (serial->network) buffer, in chunks. A client slower than
    # the serial source is dropped once this fills. A chunk is one serial read, at most
    # 4 KB (8 KB with telnet / RFC 2217, which double 0xFF bytes), so the worst case per
    # client is ~client_queue_max * 4 KB (8 KB); raise for bursty high-throughput links.
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
    tls: bool = field(default=False, metadata=_PROTECTIVE)
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
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "NetworkSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        net = NetworkSettings(**_read_fields(NetworkSettings, d, notes, path))
        net.bind_ip = canonical_ip(net.bind_ip)  # (AppConfig.from_dict notes a change)
        # a UDP mapping has no connection to negotiate telnet on, so it always ran raw: say
        # so (validate() refuses RFC 2217 and Modbus over UDP, but older releases took telnet)
        if net.mode == "udp" and net.protocol == "telnet":
            notes.append(FieldNote(
                path + "protocol", "replaced", "telnet", "raw",
                why=f"{path}protocol telnet is not supported over UDP: raw is used (as it "
                    "always was: no telnet negotiation or escaping over UDP)",
                warning=_("Telnet is not supported over UDP, so the protocol is raw (as it "
                          "always was in effect).")))
            net.protocol = "raw"
        return net

    def validate(self) -> None:
        if self.mode not in NET_MODES:
            raise ConfigError(_("Network mode must be one of {0}.").format(_choices(NET_MODES)))
        if self.protocol not in PROTOCOLS:
            raise ConfigError(_("Protocol must be one of {0}.").format(_choices(PROTOCOLS)))

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
        for label, lst in ((_("allowed"), self.allowed_client_ips),
                           (_("priority"), self.priority_client_ips)):
            _nets, bad = client_networks(lst)
            if bad:
                raise ConfigError(_("Invalid {0} client IP/CIDR: {1}.").format(label, repr(bad[0])))


def tls_files_missing(net: NetworkSettings) -> list[str]:
    """A TLS listener's certificate / key files that don't exist, one message each. Not a
    rule of validate(): a file that goes missing (a mount, a renewal) must not block every
    other change. An edit refuses them (MappingConfig.validate_input), start-up names them
    (AppConfig.problems) and the mapping then fails to start, saying so."""
    if not net.tls or net.mode == "udp":
        return []
    return [_("TLS file not found: {0}").format(p)
            for p in dict.fromkeys((net.tls_cert, net.tls_key))
            if p and not os.path.isfile(data_path(p))]


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

# The text encoding of a mapping's web console and hex view (MappingOptions.
# console_encoding): what the device's bytes are shown as, and what is typed is sent as.
# The bridge itself passes bytes unchanged. UTF-8, or one of these single-byte code pages.
CONSOLE_ENCODINGS = ("utf-8", "iso-8859-9", "cp1254", "cp857", "iso-8859-1", "cp1252", "cp437")


def console_encoding(options: "MappingOptions") -> str:
    """A mapping's console encoding: a stored value that is not one of CONSOLE_ENCODINGS
    (hand-edited, or from a newer version) reads as UTF-8, today's behaviour (edits
    refuse one: MappingConfig.validate_input)."""
    value = options.console_encoding
    return value if value in CONSOLE_ENCODINGS else "utf-8"


@functools.lru_cache(maxsize=None)
def console_charset(encoding: str) -> str:
    """What the web console shows for the bytes 0x80-0xFF in `encoding` (app.js decodes
    the device's bytes with it, and encodes what is typed with it, in the browser: it has
    no CP857 and encodes nothing but UTF-8): 128 characters, "" for UTF-8 (xterm.js decodes
    that itself). A C1 control code shows as '.', so no byte becomes a terminal command; a
    byte the code page leaves undefined as U+FFFD."""
    if encoding not in CONSOLE_ENCODINGS or encoding == "utf-8":
        return ""
    chars = (bytes([b]).decode(encoding, "replace") for b in range(0x80, 0x100))
    return "".join("." if "\x80" <= c <= "\x9f" else c for c in chars)


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
    # the web console's and hex view's text encoding (CONSOLE_ENCODINGS); display and typing
    # only: what the bridge passes stays the same bytes
    console_encoding: str = "utf-8"
    # RFC2217 interop knobs for non-compliant peers:
    rfc2217_poll_modem_interval_s: float = 1.0
    rfc2217_net_timeout_s: float = 3.0
    # RFC2217: once the client has left, put the serial port back to this mapping's baud
    # rate, data bits, parity, stop bits and flow control (off: what the client set stays
    # until the port is reopened). RTS/DTR are left alone: toggling them can reset a device.
    rfc2217_restore_on_disconnect: bool = False
    # RFC2217: what a client's SIGNATURE request is answered with (blank: "ser2net")
    rfc2217_signature: str = ""
    # Modbus gateway: how long an RTU slave gets to begin its reply once the request is out
    # on the line, before the gateway answers exception 0x0B (gateway target failed to
    # respond). Read through modbus_response_timeout().
    modbus_response_timeout_s: float = MODBUS_TIMEOUT_DEFAULT_S
    # Modbus gateway: unit ids masters send that go to another RTU unit, as {"<id>": unit}
    # ({"255": 1}: a request for unit 255 goes to unit 1 and is answered as unit 255). Empty
    # (the default): each request goes to the unit it names, as before. Read through
    # modbus_unit_map().
    modbus_unit_map: dict = field(default_factory=dict)
    # Serial-to-network mappings (not a Modbus gateway): send poll_string to the device
    # every poll_interval_s seconds while its port is open (0 = off), e.g. a scale's or an
    # analyzer's query, so its answers reach MQTT, a trace or a UDP target without a
    # program polling it. \r \n \t \\ and \xHH stand for those bytes (poll_bytes).
    # Skipped while a TCP client is connected (it does the asking then), unless
    # poll_with_clients.
    poll_string: str = ""
    poll_interval_s: float = 0.0
    poll_with_clients: bool = field(default=False, metadata=_ONLY_TRUE)
    # No-data alarm (notify.py: no_data / data_back, the dashboard, /metrics): the serial
    # port is open but the device has sent nothing for this many seconds (0 = off,
    # else NO_DATA_MIN_S..NO_DATA_MAX_S); with no_data_reply_only, only while something
    # sent to it gets no answer (a device that speaks when asked, e.g. Modbus RTU).
    no_data_alarm_s: int = 0
    no_data_reply_only: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "MappingOptions":
        d = dict(d or {})
        notes = [] if notes is None else notes
        opts = MappingOptions(**_read_fields(MappingOptions, d, notes, path))
        opts.idle_timeout_s = _legacy_seconds(opts.idle_timeout_s)
        opts.trace_both = trace_file_name(opts.trace_both)
        opts._limit_no_data_alarm(notes, path)
        if isinstance(opts.modbus_unit_map, dict):  # (JSON keys are text)
            opts.modbus_unit_map = {str(k): v for k, v in opts.modbus_unit_map.items()}
        return opts

    def _limit_no_data_alarm(self, notes: list[FieldNote], path: str) -> None:
        """A no-data alarm outside 0 / NO_DATA_MIN_S..NO_DATA_MAX_S takes the nearest value
        it can have, with a note (loading notes it, an edit warns)."""
        value = self.no_data_alarm_s
        if value == 0 or NO_DATA_MIN_S <= value <= NO_DATA_MAX_S:
            return
        used = 0 if value < 0 else min(max(value, NO_DATA_MIN_S), NO_DATA_MAX_S)
        notes.append(FieldNote(
            path + "no_data_alarm_s", "replaced", value, used,
            why=f"{path}no_data_alarm_s {value} is not 0 (off) or {NO_DATA_MIN_S}.."
                f"{NO_DATA_MAX_S} seconds: {used} is used",
            warning=_("The no-data alarm must be 0 (off) or {0} to {1} seconds: {2} is "
                      "used.").format(NO_DATA_MIN_S, NO_DATA_MAX_S, used)))
        self.no_data_alarm_s = used

    @property
    def modbus_unit_map_text(self) -> str:
        """The unit map as the mapping form shows it: "255=1, 0=1"."""
        umap = self.modbus_unit_map
        return ", ".join(f"{k}={v}" for k, v in umap.items()) if isinstance(umap, dict) else ""

    def validate(self) -> None:
        for label, value in ((_("Idle timeout"), self.idle_timeout_s),
                             (_("Max connection time"), self.max_connection_time_s)):
            if not _is_number(value) or not (0 <= value <= MAX_TIMER_S):
                raise ConfigError(_("{0} must be between 0 (off) and {1} seconds.").format(label, MAX_TIMER_S))
        if not _is_number(self.trace_max_mb) or not (0.1 <= self.trace_max_mb <= 1024):
            raise ConfigError(_("Trace file size must be between 0.1 and 1024 MB."))
        if not _is_int(self.trace_keep) or not (0 <= self.trace_keep <= 50):
            raise ConfigError(_("Rotated trace files kept must be between 0 and 50."))


def modbus_response_timeout(opts: MappingOptions) -> float:
    """A Modbus gateway's response timeout in seconds. A stored value that is not a
    positive number of seconds (before 2.9 it was in config.json only, unchecked) reads as
    the default 1 s, as 0 always did; a numeric string counts as its number. Edits refuse
    anything outside MODBUS_TIMEOUT_RANGE_S (validate_input)."""
    t = opts.modbus_response_timeout_s
    if isinstance(t, str):
        try:
            t = float(t.strip())
        except ValueError:
            return MODBUS_TIMEOUT_DEFAULT_S
    if _is_number(t) and math.isfinite(t) and t > 0:
        return float(min(t, MAX_TIMER_S))
    return MODBUS_TIMEOUT_DEFAULT_S


def modbus_unit_map(opts: MappingOptions) -> dict[int, int]:
    """A Modbus gateway's unit map as it is used: {unit id a master sends: the RTU unit
    the request goes to}. Entries the edits refuse (not ids 0-255 to units 1-255) are left
    out, with a warning in the gateway's log at start."""
    out: dict[int, int] = {}
    umap = opts.modbus_unit_map
    for key, unit in (umap.items() if isinstance(umap, dict) else ()):
        sent = _loose_number(key, whole=True)
        if _is_int(sent) and 0 <= sent <= 255 and _is_int(unit) and 1 <= unit <= 255:
            out[sent] = unit
    return out


# ---------------------------------------------------------------------------
# Per-mapping MQTT publishing (optional northbound to an IIoT broker)
# ---------------------------------------------------------------------------
MQTT_VALUE_FORMATS = ("text", "json")  # plain-format Modbus values (mqtt.value_format)
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
    tls: bool = field(default=False, metadata=_PROTECTIVE)
    username: str = ""
    password: str = ""
    client_id: str = ""         # blank => auto
    # Modbus polling only: "plain" topics per point, or Eclipse Sparkplug B (this mapping
    # is an edge node spBv1.0/<group>/N*/<node>; blank node id = the mapping's name)
    payload_format: str = "plain"
    sparkplug_group_id: str = "ser2net"
    sparkplug_node_id: str = ""
    # Sparkplug B: the primary host application (its host id) whose STATE the node follows,
    # publishing its births only while that host is online; blank = none (birth at once)
    sparkplug_primary_host_id: str = ""
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
    tx_enabled: bool = field(default=False, metadata=_ONLY_TRUE)
    # Plain-format Modbus polling: also publish each polled unit's state, retained, to
    # <base_topic>/unit<N>/status ("online" / "offline"), on every change and after each
    # (re)connect. Off: nothing beyond the points' values and <base_topic>/status.
    unit_status: bool = False
    # TLS (tls on): the broker's certificate must chain to a CA the system trusts, or with
    # ca_file to one in that file only (PEM or DER); client_cert (PEM) is presented to a
    # broker that asks for a certificate (mutual TLS), with its key in client_key or in the
    # same file, encrypted with client_key_password if at all; tls_check_hostname off: the
    # certificate need not name the broker host (its chain is still checked). See
    # engine/mqtt_pub.tls_context; edits check the files (check_mqtt_tls_files).
    ca_file: str = ""
    client_cert: str = ""
    client_key: str = ""
    client_key_password: str = ""
    tls_check_hostname: bool = field(default=True, metadata=_PROTECTIVE)
    # Plain format: publish the data (serial lines, polled Modbus values) retained, so a
    # subscriber that (re)connects gets the last one at once. Sparkplug B never retains
    # data (the specification asks so); command results are never retained.
    retain: bool = False
    # Plain-format Modbus values: "text" (1 / 0 for a bit), or "json": {"value", "ts" (read
    # time, ms since the epoch), "quality"}, with "bad" (value null) once for a point whose
    # read fails or whose unit stops answering. Edits refuse other values; a stored one
    # (a later version's) is published as text.
    value_format: str = "text"

    @staticmethod
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "MqttSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        return MqttSettings(**_read_fields(MqttSettings, d, notes, path))

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
            raise ConfigError(_("MQTT payload format must be one of {0}.").format(_choices(MQTT_FORMATS)))
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
MODBUS_DTYPES = ("uint16", "int16", "uint32", "int32", "float32", "bool",
                 "int64", "uint64", "float64", "string")
# how a device lays out a value's bytes in its registers (engine/modbus.WORD_ORDERS): ABCD
# as Modbus sends a register, high register first; CDAB low register first ("word swap"),
# BADC each register's bytes swapped, DCBA both
MODBUS_WORD_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")
MODBUS_STRING_MAX_REGS = 125  # a "string" point: at most one read's worth of registers
MODBUS_BLOCK_GAP_MAX = 124    # block reads: unused registers between two points of a block
# a point's whole-number keys (the typed reader takes "1" and 1.0 as 1, and leaves 1.5 for
# an edit to refuse: validate_input)
_POINT_INTS = ("unit", "fn", "address")
# the keys of a point and of the poll that 2.9 added: typed from the start, so read as they
# are (not through _read_fields), for an edit to refuse what it can't use (validate_input)
_POINT_STRICT = ("min", "max", "allowed", "poll", "word_order", "bit", "length")
_POLL_STRICT = ("block_reads", "block_gap")


def _loose_number(value: Any, whole: bool) -> Any:
    """A number as stored before its type was checked (a unit map's key): a numeric string
    counts as its number, and where a whole number belongs (``whole``) an integral float as
    an int. Anything else is returned as it is (edits refuse it)."""
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d{1,30}", text):
            return int(text)
        try:
            number = float(text)
        except ValueError:
            return value
        if not math.isfinite(number):
            return value
        value = number
    if whole and isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


@dataclass
class ModbusPoint:
    name: str = ""
    unit: int = 1               # RTU slave id
    fn: int = 3                 # 1 = coils, 2 = discrete inputs, 3 = holding, 4 = input registers
    address: int = 0            # 0-based coil / register address
    # registers: uint16 | int16 (one), uint32 | int32 | float32 (two), int64 | uint64 |
    # float64 (four), string ("length" registers), bool (one of its bits: "bit");
    # coils / discrete inputs: bool
    dtype: str = "uint16"
    scale: float = 1.0          # published value = raw * scale (0 = no scaling, as before)
    # MQTT may write it: <base_topic>/<name>/set, or a Sparkplug B DCMD to its unit's
    # device (a coil: FC5, a holding register: FC6 / FC16 for its own registers). Only
    # coils and holding registers; not a string, nor a bit of a register.
    writable: bool = False
    # What MQTT may write to it (optional, writable points; None = no limit, as before):
    # the lowest and highest value, in the point's own units (as published, before the
    # scale is undone), and the values it may take (a list; true / false for a coil). A
    # write outside them is refused. Modbus/TCP masters are not limited by them.
    min: Optional[float] = None
    max: Optional[float] = None
    allowed: Optional[list] = None
    # False: write-only (a writable point that can't be read, such as a command register):
    # never polled, and a write is not read back
    poll: bool = True
    # How the device lays out the value's bytes in its registers (MODBUS_WORD_ORDERS), for
    # reads and writes; ABCD (the default) as before. A string: ABCD (first character in the
    # high byte) or BADC. Read through point_order().
    word_order: str = "ABCD"
    # dtype "bool" on a register (fn 3, 4): which of its bits it is, 0 (least significant)
    # to 15
    bit: Optional[int] = None
    # dtype "string": how many registers it takes (two characters each), 1-125
    length: Optional[int] = None

    @staticmethod
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "ModbusPoint":
        d = dict(d or {})
        notes = [] if notes is None else notes
        p = ModbusPoint(**_read_fields(ModbusPoint, d, notes, path, skip=_POINT_STRICT))
        # the keys 2.9 added are typed from the start: kept as they are, for an edit to
        # refuse what it can't use (ModbusPoll.validate_input); a stored one the gateway
        # can't use is ignored, with a warning in its log at start
        for key in _POINT_STRICT:
            if key in d:
                setattr(p, key, d[key])
        return p


def point_scale(p: ModbusPoint) -> Any:
    """The scale a point's values are read and written with: its own when it is a number
    (0 and 1: not scaled), else 1. A stored scale the edits refuse (text, NaN: see
    ModbusPoll.validate_input) is ignored rather than multiplied in, with a warning in the
    gateway's log at start."""
    s = p.scale
    return s if _is_number(s) and math.isfinite(s) else 1.0


def point_order(p: ModbusPoint) -> str:
    """The word order a point is read and written with: its own, or ABCD for a stored one
    the edits refuse (with a warning in the gateway's log at start)."""
    return p.word_order if p.word_order in MODBUS_WORD_ORDERS else "ABCD"


def poll_block_gap(poll: "ModbusPoll") -> Optional[int]:
    """Block reads (ModbusPoll.block_reads): how many unused registers (coils) may lie
    between two points read with one request, or None when block reads are off. A stored
    gap the edits refuse reads as 0, and anything but true as off."""
    if poll.block_reads is not True:
        return None
    gap = poll.block_gap
    return gap if _is_int(gap) and 0 <= gap <= MODBUS_BLOCK_GAP_MAX else 0


def _register_bit(value: Any) -> bool:
    return _is_int(value) and 0 <= value <= 15


def _string_length(value: Any) -> bool:
    return _is_int(value) and 1 <= value <= MODBUS_STRING_MAX_REGS


MODBUS_POLL_DEFAULT_S = 5.0


@dataclass
class ModbusPoll:
    interval_s: float = MODBUS_POLL_DEFAULT_S
    points: list[ModbusPoint] = field(default_factory=list)
    # Block reads (off: one request per point, as before): a unit's points of one function
    # that lie close together are read with one request, at most 125 registers (2000 coils
    # or inputs), with up to block_gap unused registers (coils) between two of them. A
    # block the slave refuses (exception 02 or 03) has its points read one at a time from
    # then on. Read through poll_block_gap().
    block_reads: bool = False
    block_gap: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "ModbusPoll":
        d = dict(d or {})
        notes = [] if notes is None else notes
        poll = ModbusPoll(
            points=[ModbusPoint.from_dict(p, notes, f"{path}points[{i}].")
                    for i, p in enumerate(_objects(d, "points", path))],
            **_read_fields(ModbusPoll, d, notes, path, skip=("points", *_POLL_STRICT)),
            **{key: d[key] for key in _POLL_STRICT if key in d})
        # 0 or less has always meant the default interval (an <input min> keeps the form
        # from sending it); said now, where it matters
        if poll.interval_s <= 0:
            if poll.points:
                was, now = _shown(poll.interval_s), f"{MODBUS_POLL_DEFAULT_S:g}"
                notes.append(FieldNote(
                    path + "interval_s", "replaced", poll.interval_s, MODBUS_POLL_DEFAULT_S,
                    why=f"{path}interval_s {was} is not a poll interval (more than 0 "
                        f"seconds): {now} s is used",
                    warning=_("{0}interval_s {1} is not a poll interval (more than 0 seconds): "
                              "{2} s is used.").format(path, was, now)))
            poll.interval_s = MODBUS_POLL_DEFAULT_S
        return poll

    @property
    def broadcast_points(self) -> list[str]:
        """The names of the points on unit 0, the broadcast address: never read (no slave
        answers a broadcast), and a write to one goes to every slave. Allowed, with a
        warning in the log and the mapping form."""
        return [p.name for p in self.points if p.unit == 0]

    @property
    def reserved_unit_points(self) -> list[str]:
        """The names of the points on units 248-255, which a Modbus serial line reserves
        (slave addresses are 1-247). Allowed, with a warning like broadcast_points."""
        return [p.name for p in self.points if _is_int(p.unit) and 248 <= p.unit <= 255]

    def validate(self) -> None:
        if not self.points:
            return
        if not _is_number(self.interval_s) or not (0 < self.interval_s < math.inf):
            raise ConfigError(_("Modbus poll interval must be > 0 seconds."))
        seen = set()
        for p in self.points:
            if not isinstance(p.name, str) and p.name is not None:  # (a model built in code)
                raise ConfigError(_("Modbus poll point names are text: {0} is not.").format(
                    repr(p.name)[:40]))
            if not isinstance(p.name, str) or not p.name.strip():
                raise ConfigError(_("Each Modbus poll point needs a name."))
            if p.name in seen:
                raise ConfigError(_("Duplicate Modbus poll point name: {0}.").format(repr(p.name)))
            seen.add(p.name)
            # (a number of any type compares as it did; anything else failed before too)
            if not isinstance(p.unit, (int, float)) or not (0 <= p.unit <= 255):
                raise ConfigError(_("Point {0}: unit must be 0-255.").format(repr(p.name)))
            if p.fn not in (1, 2, 3, 4) or not _is_int(p.fn):
                raise ConfigError(_("Point {0}: function must be 1 (coils), 2 (discrete "
                                    "inputs), 3 (holding registers) or 4 (input registers).").format(repr(p.name)))
            if not isinstance(p.address, (int, float)) or not (0 <= p.address <= 65535):
                raise ConfigError(_("Point {0}: address must be 0-65535.").format(repr(p.name)))
            # from_dict reads both as numbers / true or false; a model built in code is
            # refused here rather than fail at every poll
            if not _is_number(p.scale) or not (isinstance(p.scale, int) or math.isfinite(p.scale)):
                raise ConfigError(_("Point {0}: scale must be a number.").format(repr(p.name)))
            if not isinstance(p.writable, bool):
                raise ConfigError(_("Point {0}: writable must be true or false.").format(repr(p.name)))
            if p.dtype not in MODBUS_DTYPES:
                raise ConfigError(_("Point {0}: dtype must be one of {1}.").format(repr(p.name), _choices(MODBUS_DTYPES)))
            # (bool on a register and string are new in 2.9: what was refused stays refused
            # without the bit / length they need)
            if (p.fn in (1, 2)) != (p.dtype == "bool") and not (
                    p.dtype == "bool" and _register_bit(p.bit)):
                raise ConfigError(_("Point {0}: coils / discrete inputs (fn 1, 2) use dtype "
                                    "'bool'; registers (fn 3, 4) a numeric dtype or 'string', "
                                    "or 'bool' with the \"bit\" (0-15) it reads.").format(
                                        repr(p.name)))
            if p.dtype == "string" and not _string_length(p.length):
                raise ConfigError(_("Point {0}: a string needs its \"length\": how many "
                                    "registers it takes (1-{1}, two characters each).").format(
                                        repr(p.name), MODBUS_STRING_MAX_REGS))
            if p.writable and p.fn not in (1, 3):
                raise ConfigError(_("Point {0}: only coils (fn 1) and holding registers (fn 3) "
                                    "can be written.").format(repr(p.name)))

    def validate_input(self) -> None:
        """The point options an edit may set (min, max, allowed, poll, word_order) and the
        types of the rest. validate() leaves them alone (a stored config has to keep
        loading); one stored that can't be used is ignored, with a warning in the gateway's
        log at start."""
        if not isinstance(self.block_reads, bool):
            raise ConfigError(_("Block reads must be on or off (true or false)."))
        if not (_is_int(self.block_gap) and 0 <= self.block_gap <= MODBUS_BLOCK_GAP_MAX):
            raise ConfigError(_("The unused registers a block read may span must be a whole "
                                "number from 0 to {0}.").format(MODBUS_BLOCK_GAP_MAX))
        for p in self.points:
            name = repr(p.name)
            # the types validate() leaves alone: a stored point loads as it was
            for key in _POINT_INTS:
                if not _is_int(getattr(p, key)):
                    raise ConfigError(_("Point {0}: {1} must be a whole number.").format(
                        name, key))
            if not (_is_number(p.scale) and math.isfinite(p.scale)):
                raise ConfigError(_("Point {0}: scale must be a number (0 or 1: not "
                                    "scaled).").format(name))
            if not isinstance(p.writable, bool):
                raise ConfigError(_("Point {0}: writable must be true or false.").format(name))
            register = p.fn in (3, 4)
            if p.word_order not in MODBUS_WORD_ORDERS:
                raise ConfigError(_("Point {0}: word_order must be one of {1}.").format(
                    name, ", ".join(MODBUS_WORD_ORDERS)))
            if p.dtype == "bool" and p.word_order != "ABCD":
                raise ConfigError(_("Point {0}: word_order is for register values, not for a "
                                    "bit.").format(name))
            if p.dtype == "string" and p.word_order not in ("ABCD", "BADC"):
                raise ConfigError(_("Point {0}: a string's characters are in the high byte "
                                    "first (\"ABCD\") or the low byte first (\"BADC\").").format(
                                        name))
            if p.bit is not None and not (p.dtype == "bool" and register):
                raise ConfigError(_("Point {0}: \"bit\" reads one bit of a register: it needs "
                                    "\"dtype\": \"bool\" and fn 3 or 4.").format(name))
            if p.length is not None and p.dtype != "string":
                raise ConfigError(_("Point {0}: \"length\" is for a \"string\" point.").format(
                    name))
            if p.dtype == "string" and p.scale not in (0, 1):
                raise ConfigError(_("Point {0}: a string can't be scaled.").format(name))
            if p.writable and (p.dtype == "string" or (p.dtype == "bool" and register)):
                raise ConfigError(_("Point {0}: MQTT can't write a string or a bit of a "
                                    "register; make it read-only.").format(name))
            if not p.writable and any(v is not None for v in (p.min, p.max, p.allowed)):
                raise ConfigError(_("Point {0}: min, max and allowed limit what MQTT may "
                                    "write, so they need \"writable\": true.").format(name))
            coil = p.fn in (1, 2)
            if coil and (p.min is not None or p.max is not None):
                raise ConfigError(_("Point {0}: a coil is true or false, so min and max don't "
                                    "apply to it (use allowed).").format(name))
            for key, value in (("min", p.min), ("max", p.max)):
                if value is not None and not (_is_number(value) and math.isfinite(value)):
                    raise ConfigError(_("Point {0}: {1} must be a number.").format(name, key))
            if p.min is not None and p.max is not None and p.min > p.max:
                raise ConfigError(_("Point {0}: min is greater than max.").format(name))
            if p.allowed is not None and not (
                    isinstance(p.allowed, list) and p.allowed
                    and all(isinstance(v, bool) if coil else
                            (_is_number(v) and math.isfinite(v)) for v in p.allowed)):
                raise ConfigError(_("Point {0}: allowed must be a list of the values MQTT may "
                                    "write: true / false for a coil, numbers for a "
                                    "register.").format(name))
            if not isinstance(p.poll, bool):
                raise ConfigError(_("Point {0}: poll must be true or false.").format(name))
            if not p.poll and not p.writable:
                raise ConfigError(_("Point {0}: a point that is not polled (\"poll\": false) "
                                    "is write-only, so it needs \"writable\": true.").format(
                                        name))


def unstorable_text(value: Any, where: str = "") -> Optional[str]:
    """Where in ``value`` (a to_dict() tree: dicts, lists, strings) there is text that can't
    be written as UTF-8 — an unpaired surrogate (U+D800-U+DFFF), which only a JSON escape
    such as \\ud800 can make — as a path like "modbus_poll.points[0].name"; None when there
    is none. config.json could not be saved with it."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return where or "?"
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k).encode("utf-8", "backslashreplace").decode("utf-8")
            spot = f"{where}.{key}" if where else key
            if (bad := unstorable_text(k, spot)) or (bad := unstorable_text(v, spot)):
                return bad
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            if bad := unstorable_text(v, f"{where}[{i}]"):
                return bad
    return None


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
    # What from_dict did with the data it was given (FieldNote): loading config.json logs
    # it, an edit refuses an invalid value (validate_input) and warns about the rest.
    # Never persisted.
    input_notes: list[FieldNote] = field(default_factory=list, repr=False, compare=False)

    # what a mapping read back from the REST API has besides its settings (never stored)
    _READ_ONLY_KEYS = ("status", "warnings")

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MappingConfig":
        # every way into the config (file, import, form, REST, cluster) passes here, so an
        # id that is not a valid mapping id never gets further (see _mapping_id_from), nor a
        # value of the wrong type (see _read_fields)
        d = dict(d or {})
        notes: list[FieldNote] = []
        readers: dict[str, Any] = {
            "serial": SerialSettings.from_dict, "serial_b": SerialSettings.from_dict,
            "network": NetworkSettings.from_dict, "options": MappingOptions.from_dict,
            "mqtt": MqttSettings.from_dict, "modbus_poll": ModbusPoll.from_dict}
        fields = _read_fields(MappingConfig, d, notes, skip=("id", "input_notes", *readers),
                              keep=MappingConfig._READ_ONLY_KEYS)
        sections = {key: read(_section(d, key), notes, key + ".") for key, read in readers.items()}
        m = MappingConfig(id=_mapping_id_from(d.get("id")), input_notes=notes,
                          **sections, **fields)
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
        d = asdict(self)
        d.pop("input_notes", None)
        return d

    def input_warnings(self) -> list[str]:
        """What an edit (REST API, import) is told about the data it sent: keys that are no
        setting, settings replaced by supported ones (in the request's language; the REST
        API's is English)."""
        unknown = [n.path for n in self.input_notes if n.kind == "unknown"]
        out = ([_("Not settings of this version, so they are ignored: {0}.").format(
            ", ".join(unknown))] if unknown else [])
        return out + [n.warning for n in self.input_notes if n.kind == "replaced"]

    def validate(self) -> None:
        if not self.name.strip():
            raise ConfigError(_("Mapping name is required."))
        if self.kind not in MAPPING_KINDS:
            raise ConfigError(_("Mapping kind must be one of {0}.").format(_choices(MAPPING_KINDS)))
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

    def ignored_settings(self) -> list[str]:
        """Settings this mapping has that do nothing, in the current language: the runner
        logs them when it starts (in English), the edit form and the REST API show them.
        validate() accepts them, as earlier versions did."""
        out = []
        if self.mqtt.enabled and self.kind == "serialbridge":
            out.append(_("MQTT is on, but a serial-to-serial bridge does not publish to MQTT: "
                         "its MQTT settings are not used."))
        elif (self.mqtt.enabled and self.kind == "net" and self.network.protocol == "modbus"
              and not self.modbus_poll.points):
            out.append(_("MQTT is on, but a Modbus gateway publishes only its poll points, and "
                         "it has none: nothing is published. Add poll points, or turn MQTT off."))
        if self.kind == "net" and self.network.mode == "udp" and self.options.closeon:
            out.append(_("A close-on string is set, but a UDP mapping has no connections to "
                         "close: it is not used."))
        if self.kind == "net":  # a list an edit would refuse: only in a hand-edited file
            nets, bad = client_networks(self.network.allowed_client_ips)
            if bad:
                out.append(_("Allowed client IPs: these entries are not IP addresses or "
                             "networks, so they are left out: {0}.").format(", ".join(bad))
                           + ("" if nets else " " + _("No entry is usable, so every client "
                                                      "is refused.")))
            _nets, bad = client_networks(self.network.priority_client_ips)
            if bad:
                out.append(_("High-priority client IPs: these entries are not IP addresses or "
                             "networks, so they are left out: {0}.").format(", ".join(bad)))
        return out

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
        (a URL serial port: when the mapping starts, see engine/serial_io.refuse_url), or
        normalized when it is loaded (a value of the wrong type: from_dict)."""
        if (spot := unstorable_text(self.to_dict())) is not None:
            raise ConfigError(_("{0} has a character that can't be stored: an unpaired "
                                "surrogate escape (\\ud800 to \\udfff).").format(spot))
        for note in self.input_notes:
            if note.kind in ("invalid", "switch_text"):
                raise ConfigError(note.error())
        if self.kind == "net" and (missing := tls_files_missing(self.network)):
            raise ConfigError(missing[0])
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
        if self.options.console_encoding not in CONSOLE_ENCODINGS:
            raise ConfigError(_("Unknown console text encoding {0}: use one of {1}.").format(
                repr(self.options.console_encoding), ", ".join(CONSOLE_ENCODINGS)))
        if self.kind == "net" and self.network.protocol == "modbus":
            low, high = MODBUS_TIMEOUT_RANGE_S
            timeout = self.options.modbus_response_timeout_s
            if not _is_number(timeout) or not (low <= timeout <= high):
                raise ConfigError(_("The Modbus response timeout must be between {0} and {1} "
                                    "seconds.").format(f"{low:g}", f"{high:g}"))
            if self.mqtt.value_format not in MQTT_VALUE_FORMATS:
                raise ConfigError(_("MQTT value format must be one of {0}.").format(
                    MQTT_VALUE_FORMATS))
            umap = self.options.modbus_unit_map
            if not isinstance(umap, dict) or len(modbus_unit_map(self.options)) != len(umap):
                raise ConfigError(_("The unit map sends unit ids 0-255 to RTU units 1-255, "
                                    "each id once, such as 255=1."))
            self.modbus_poll.validate_input()
            host = self.mqtt.sparkplug_primary_host_id
            if self.mqtt.payload_format == "sparkplug_b" and host != "":
                if not isinstance(host, str) or any(c in host for c in "/+#"):
                    raise ConfigError(_("Sparkplug {0} must not contain '/', '+' or '#'.").format(
                        _("primary host ID")))
        if self.kind == "net" and self.mqtt.enabled and _plain_mqtt(self):
            # (serial lines and their /tx writes too: a subscription to such a topic would
            # take every matching one)
            if mqtt_topic_problem(self.mqtt.base_topic):
                raise ConfigError(_("The MQTT base topic can't contain '+', '#' or the null "
                                    "character: no message can be published to such a topic."))
            self.check_mqtt_topics()

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
        """A serial-to-network mapping's MQTT line end and its poll string (an edit's
        rules: a stored value they refuse reads as off / "lf"). Its base topic, also for
        MQTT writes, is checked with every plain-format mapping's (validate_input)."""
        mq = self.mqtt
        if mq.enabled and mq.line_end not in MQTT_LINE_ENDS:
            raise ConfigError(_("The MQTT line end must be one of {0}.").format(MQTT_LINE_ENDS))
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

    def check_mqtt_topics(self) -> None:
        """Plain MQTT on a Modbus gateway (an edit's rules, see validate_input): a point's
        name is part of its topics (<base_topic>/<name>, .../set, .../set/result), so it
        can't have '+', '#' or the null character, and a point can't be published where the
        mapping already publishes or takes something else: its status (<base_topic>/status),
        a unit's status (unit<N>/status, with unit_status on) or another point's command or
        result topic. A stored mapping that breaks them keeps loading and running; start-up
        and each start of it log a warning (mqtt_warnings). Raises ConfigError."""
        if not (self.kind == "net" and self.network.protocol == "modbus"):
            return
        for p in self.modbus_poll.points:
            if mqtt_topic_problem(p.name):
                raise ConfigError(_("Point {0}: with the plain MQTT format its name is part "
                                    "of its topic, which can't contain '+', '#' or the null "
                                    "character.").format(repr(p.name)))
        uses: dict[str, tuple[str, Any]] = {}
        for topic, use, of in sorted(_mqtt_topics(self), key=lambda t: t[1] == "value"):
            if topic not in uses:
                uses[topic] = (use, of)
                continue
            # (an overlap is always a point's value topic: the others can't meet)
            raise ConfigError(_("Point {0}: its MQTT topic {1} is already {2}. Rename the "
                                "point.").format(repr(of), repr(topic), _topic_use(*uses[topic])))

    def check_mqtt_tls_files(self) -> None:
        """An edit on the node that runs the mapping (its form, the REST API): with MQTT
        over TLS, the CA file and the client certificate and key must be usable here.
        Neither validate() nor validate_input() checks them: a stored config whose files
        are gone has to keep loading (the mapping then runs without MQTT, logged), a fleet
        edit is checked by the node it is for (its own form save), and an import only warns.
        Raises ConfigError."""
        mq = self.mqtt
        if not (self.kind == "net" and mq.enabled and mq.tls):
            return
        from .engine.mqtt_pub import TlsFileError, tls_context
        try:
            tls_context(mq)
        except TlsFileError as e:
            text = {
                "ca_unreadable": _("The MQTT CA file {0} can't be read: {1}"),
                "ca_empty": _("The MQTT CA file {0} holds no certificate (PEM or DER)."),
                "key_without_cert": _("An MQTT client key needs its client certificate."),
                "cert_unreadable": _("The MQTT client certificate or key ({0}) can't be read: "
                                     "{1}"),
                "cert_unusable": _("The MQTT client certificate {0} can't be used with its key "
                                   "({1}). Is the key password right, and is it the "
                                   "certificate's key?"),
            }[e.kind]
            raise ConfigError(text.format(e.path, e.detail)) from None

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


def mqtt_topic_problem(text: Any) -> bool:
    """'+' / '#' (wildcards: a message can't be published to them, and a subscription would
    take every matching topic) or U+0000 (which no MQTT string may have) in a topic, or in
    the part of one a name makes (a base topic, a point's name)."""
    return any(c in str(text) for c in "+#\x00")


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
# MQTT: the topics and identity a mapping uses on its broker, and clashes between
# mappings. Checked where a mapping is created or changed (check_mqtt_clashes) and
# warned about at start-up and at each start (mqtt_warnings); never by validate(): a
# stored config with a clash keeps loading and running.
# ---------------------------------------------------------------------------
def _plain_mqtt(m: MappingConfig) -> bool:
    """The mapping publishes plain MQTT topics under its base topic (serial lines, or
    Modbus points), not Sparkplug B (which a Modbus gateway alone can speak)."""
    return m.network.protocol != "modbus" or m.mqtt.payload_format != "sparkplug_b"


def _mqtt_topics(m: MappingConfig) -> list[tuple[str, str, Any]]:
    """The topics a plain-format mapping uses while it runs, as (topic, use, of): use
    "status" (its retained online/offline and its Last Will) and "lines" (serial lines,
    with "tx" and "tx_result" for its MQTT writes: mqtt_tx_topics), or on a Modbus gateway
    "value", "set" and "result" (a point's value, its command topic and the answers to its
    commands; of = the point's name) and "unit" (a unit's status, with unit_status on;
    of = the unit). As engine/mqtt_pub, engine/bridge and engine/modbus_gateway build
    them."""
    base = str(m.mqtt.base_topic).rstrip("/")
    out: list[tuple[str, str, Any]] = [(f"{base}/status", "status", None)]
    if m.network.protocol != "modbus":
        return out + [(base, "lines", None)] + mqtt_tx_topics(m)
    units: list[Any] = []
    for p in m.modbus_poll.points:
        if p.poll is not False:  # (a write-only point is never published)
            out.append((f"{base}/{p.name}", "value", p.name))
            if p.unit != 0 and p.unit not in units:
                units.append(p.unit)
        if p.writable:
            out.append((f"{base}/{p.name}/set", "set", p.name))
            out.append((f"{base}/{p.name}/set/result", "result", p.name))
    if m.mqtt.unit_status:
        out += [(f"{base}/unit{u}/status", "unit", u) for u in units]
    return out


def _topic_use(use: str, of: Any) -> str:
    if use == "status":
        return _("the mapping's online/offline status topic")
    if use == "unit":
        return _("the status topic of unit {0}").format(of)
    if use == "set":
        return _("the command topic of point {0}").format(repr(of))
    if use == "result":
        return _("the topic of the answers to point {0}'s commands").format(repr(of))
    return _("the topic of point {0}").format(repr(of))


def sparkplug_identity(m: MappingConfig) -> tuple[str, str]:
    """The Sparkplug B group and edge node ID a mapping's node uses (as
    engine/sparkplug_node: a blank node ID is made from the mapping's name)."""
    from .engine.sparkplug import sanitize_id
    mq = m.mqtt
    return (str(mq.sparkplug_group_id or "").strip() or "ser2net",
            str(mq.sparkplug_node_id or "").strip() or sanitize_id(str(m.name)))


@dataclass
class _MqttUse:
    """What a mapping that runs does on its broker (see _mqtt_use)."""
    broker: str              # host:port (the host in lower case)
    client_id: str           # "" = the client library makes one up
    node: str                # Sparkplug B: "<group>/<edge node>"; plain: ""
    commands: set[str]       # plain: the topics it takes writes from (<point>/set, /tx)
    published: set[str]      # plain: the topics it publishes to


def _mqtt_use(m: MappingConfig) -> Optional[_MqttUse]:
    """None when the mapping starts no MQTT client (not a network mapping, MQTT off, a
    Modbus gateway without points, a plain base topic with a wildcard: see
    engine/mqtt_pub)."""
    mq = m.mqtt
    if m.kind != "net" or not mq.enabled:
        return None
    if m.network.protocol == "modbus" and not m.modbus_poll.points:
        return None
    broker = f"{str(mq.host).strip().lower()}:{mq.port}"
    client_id = mq.client_id if isinstance(mq.client_id, str) else ""
    if not _plain_mqtt(m):
        return _MqttUse(broker, client_id, "/".join(sparkplug_identity(m)), set(), set())
    if any(c in str(mq.base_topic) for c in "+#"):
        return None
    topics = _mqtt_topics(m)
    commands = {"set", "tx"}
    return _MqttUse(broker, client_id, "",
                    {t for t, use, _of in topics if use in commands},
                    {t for t, use, _of in topics if use not in commands})


@dataclass(frozen=True)
class MqttClash:
    """Two enabled mappings that get in each other's way on one MQTT broker: kind
    "client_id" (the same client ID: the broker keeps one connection per ID),
    "sparkplug" (the same Sparkplug B edge node), "command" (a topic one of them takes
    writes from that the other uses too) — refused where a mapping is created or changed
    — or "shared" (both publish to a topic, such as one base topic's status: only
    warned about, since it can be meant)."""
    kind: str
    other: str    # the other mapping's name
    broker: str
    what: str     # the client ID, "<group>/<edge node>" or the topic

    @property
    def refused(self) -> bool:
        return self.kind != "shared"

    def text(self) -> str:
        if self.kind == "client_id":
            return _("Mapping '{0}' already connects to the MQTT broker {1} with the client ID "
                     "{2}. A broker keeps one connection per client ID, so the two mappings "
                     "would keep disconnecting each other: leave the client ID blank "
                     "(automatic) or use another one.").format(
                         self.other, self.broker, repr(self.what))
        if self.kind == "sparkplug":
            return _("Mapping '{0}' is already the Sparkplug B edge node {1} (group/edge node "
                     "ID) on the MQTT broker {2}. Two edge nodes can't share it: a host takes "
                     "them for one and sends its commands to both. Set another edge node ID "
                     "(a blank one is made from the mapping's name).").format(
                         self.other, repr(self.what), self.broker)
        if self.kind == "command":
            return _("Mapping '{0}' also uses the MQTT topic {1} on the broker {2}, and one of "
                     "the two takes writes from it (a point's /set, a serial mapping's /tx): "
                     "one MQTT command would be written by both, or the data of one taken as a "
                     "command by the other. Give one of them another base topic.").format(
                         self.other, repr(self.what), self.broker)
        # only logged (never refused, so never shown in the UI)
        return (f"mapping '{self.other}' publishes to the MQTT topic {self.what!r} on the "
                f"broker {self.broker} too, so the messages of the two mix there (a shared "
                ".../status shows the state of whichever connected last)")


class _MqttIndex:
    """The MQTT identity and topics of some mappings, by broker, to find what another
    mapping would share with them."""

    def __init__(self) -> None:
        # (broker, client ID / "<group>/<node>" / topic) -> the mappings with it
        self._ids: dict[tuple[str, str], list[MappingConfig]] = {}
        self._nodes: dict[tuple[str, str], list[MappingConfig]] = {}
        self._commands: dict[tuple[str, str], list[MappingConfig]] = {}
        self._published: dict[tuple[str, str], list[MappingConfig]] = {}

    def add(self, m: MappingConfig, use: _MqttUse) -> None:
        b = use.broker
        if use.client_id:
            self._ids.setdefault((b, use.client_id), []).append(m)
        if use.node:
            self._nodes.setdefault((b, use.node), []).append(m)
        for t in use.commands:
            self._commands.setdefault((b, t), []).append(m)
        for t in use.published:
            self._published.setdefault((b, t), []).append(m)

    def clashes(self, use: _MqttUse) -> list[MqttClash]:
        """What a mapping with ``use`` shares with these: one clash per other mapping, the
        most serious one; of the clashes only warned about ("shared"), just the first,
        when there is nothing worse (mappings that share a topic on purpose would
        otherwise say so to each other over and over)."""
        b = use.broker
        found: dict[str, MqttClash] = {}

        def note(kind: str, others: list[MappingConfig], what: str) -> None:
            for other in others:
                if other.id not in found:
                    found[other.id] = MqttClash(kind, str(other.name), b, what)

        if use.client_id:
            note("client_id", self._ids.get((b, use.client_id), []), use.client_id)
        if use.node:
            note("sparkplug", self._nodes.get((b, use.node), []), use.node)
        for t in sorted(use.commands):
            note("command", self._commands.get((b, t), []) + self._published.get((b, t), []), t)
        for t in sorted(use.published):
            note("command", self._commands.get((b, t), []), t)
        if not found:  # a shared base topic shows as its status topic
            topic = min((t for t in use.published if (b, t) in self._published),
                        key=lambda t: (not t.endswith("/status"), t), default=None)
            if topic is not None:
                note("shared", self._published[(b, topic)][:1], topic)
        return list(found.values())


def mqtt_clashes(mapping: MappingConfig, mappings) -> list[MqttClash]:
    """What ``mapping`` would share on its MQTT broker with the other enabled ones of
    ``mappings`` (itself, by id, left out) while both run; nothing when it is disabled."""
    if not mapping.enabled or (use := _mqtt_use(mapping)) is None:
        return []
    index = _MqttIndex()
    for m in mappings:
        if m.id == mapping.id or not m.enabled:
            continue
        try:
            other = _mqtt_use(m)
        except Exception:  # a hand-edited value of the wrong type: nothing to compare
            continue
        if other is not None and other.broker == use.broker:
            index.add(m, other)
    return index.clashes(use)


def check_mqtt_clashes(mapping: MappingConfig, mappings) -> None:
    """An edit's rule (the mapping form, also a cluster node's save of its own mapping;
    the REST API; an import): an enabled mapping can't share its MQTT client ID, its
    Sparkplug B edge node or a command topic with another enabled mapping on the same
    broker. Raises ConfigError. Not in validate(): a stored config with such a clash keeps
    loading and running (with a warning, see mqtt_warnings); only an edit has to fix it."""
    for clash in mqtt_clashes(mapping, mappings):
        if clash.refused:
            raise ConfigError(clash.text())


@contextlib.contextmanager
def _in_english():
    """Messages built meanwhile are in English (log lines, whatever the request's
    language)."""
    token = i18n.set_language(i18n.DEFAULT)
    try:
        yield
    finally:
        i18n.reset(token)


def _topic_warnings(m: MappingConfig) -> list[str]:
    if m.kind == "net" and m.mqtt.enabled and _plain_mqtt(m):
        try:
            m.check_mqtt_topics()
        except ConfigError as e:
            return [f"MQTT: {e}"]
    return []


def mqtt_warnings(mapping: MappingConfig, mappings) -> list[str]:
    """In English, for the log: what the edits refuse in ``mapping``'s MQTT topics (see
    check_mqtt_topics; a wildcard base topic is logged by the MQTT client itself), and
    what it shares on its broker with the other enabled ones of ``mappings``. Empty when
    it is disabled; never raises."""
    if not mapping.enabled:
        return []
    with _in_english():
        try:
            return _topic_warnings(mapping) + [f"MQTT: {c.text()}"
                                               for c in mqtt_clashes(mapping, mappings)]
        except Exception:  # a hand-edited value of the wrong type: validate() reports it
            return []


def mqtt_start_warnings(mappings: list[MappingConfig]) -> list[list[str]]:
    """mqtt_warnings for each of ``mappings``, in order, against the enabled ones before
    it, in one pass (a clash is said once, by the later mapping of the two): for the
    start-up's load notes and starting them all. In English; never raises."""
    out: list[list[str]] = []
    index = _MqttIndex()
    with _in_english():
        for m in mappings:
            lines: list[str] = []
            if m.enabled:
                try:
                    lines = _topic_warnings(m)
                    if (use := _mqtt_use(m)) is not None:
                        lines += [f"MQTT: {c.text()}" for c in index.clashes(use)]
                        index.add(m, use)
                except Exception:  # a hand-edited value of the wrong type: validate() says
                    lines = []
            out.append(lines)
    return out


def _mqtt_load_notes(mappings: list[MappingConfig]) -> list[str]:
    return [f"mapping '{m.name}': {line}" for m, lines in
            zip(mappings, mqtt_start_warnings(mappings), strict=True) for line in lines]


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
    require_login: bool = field(default=True, metadata=_PROTECTIVE)
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
    proxy_hsts: bool = field(default=False, metadata=_ONLY_TRUE)
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

    def check_tls_files(self) -> None:
        """The certificate and key, when set, must be files that exist: checked when they
        are set in Settings, and at start-up (runtime.require_admin_tls), not by every save
        — a file that goes missing later must not block every other change."""
        for path in (p for p in (self.tls_cert, self.tls_key) if p):
            if not os.path.isfile(data_path(path)):
                raise ConfigError(_("TLS file not found or not readable: {0}").format(path))


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


# the languages the UI offers are listed once, in app/i18n.py ("auto": the browser's)
UI_LANGUAGES = ("auto", *LANGUAGES)
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
    start_tls: bool = field(default=False, metadata=_PROTECTIVE)
    # check the server's certificate (LDAPS / StartTLS)
    tls_verify: bool = field(default=True, metadata=_PROTECTIVE)
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
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "LdapSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        return LdapSettings(**_read_fields(LdapSettings, d, notes, path))

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
            raise ConfigError(_("LDAP default role must be one of {0} or blank.").format(_choices(ROLES)))


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
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "OidcSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        return OidcSettings(**_read_fields(OidcSettings, d, notes, path))

    def validate(self) -> None:
        if not self.enabled:
            return
        if not (self.issuer.strip() and self.client_id.strip() and self.client_secret.strip()):
            raise ConfigError(_("OIDC needs an issuer, client ID and client secret."))
        if not self.issuer.strip().lower().startswith("https://"):
            raise ConfigError(_("OIDC issuer must be an https:// URL."))
        if self.default_role and self.default_role not in ROLES:
            raise ConfigError(_("OIDC default role must be one of {0} or blank.").format(_choices(ROLES)))


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
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "ClusterSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        # the fields added after 2.8.0 take their default for a hand-edited value of another
        # type, as in 2.8.1: no text turns off the link to older nodes or discovery, and the
        # previous key is never a number's text
        for name, default in (("legacy_auth", True), ("discovery", True), ("previous_key", "")):
            value = d.get(name)
            if name in d and not isinstance(value, type(default)):
                if value is not None:
                    notes.append(FieldNote(path + name, "invalid", value, default,
                                           _kind_of(type(default))))
                del d[name]
        # a hand-edited key of null or a number reads as "" (cluster off, which start-up
        # reports) or the number as text, instead of failing wherever the key is used
        c = ClusterSettings(**_read_fields(ClusterSettings, d, notes, path))
        c.peers = [p.strip() for p in c.peers if p.strip()]
        c.previous_until = _epoch(c.previous_until)
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
                 "client_connected", "client_disconnected", "login_failed", "disk_low",
                 "no_data", "data_back", "peer_offline", "peer_back", "service_started",
                 "unit_offline", "unit_back")
# (a saved events list is kept as it is: these are what a config without one gets)
NOTIFY_DEFAULT_EVENTS = ("device_lost", "device_back", "start_failed", "link_lost", "link_back")
WEBHOOK_FORMATS = ("json", "slack", "teams", "discord", "telegram")
SMTP_SECURITY = ("starttls", "ssl", "none")


@dataclass
class NotificationSettings:
    """Event notifications (app/notify.py): a webhook and/or e-mail when a serial
    device disappears or comes back, a mapping fails to start, clients connect or
    disconnect, a sign-in fails, the data disk is almost full, a device sends nothing
    (a mapping's no-data alarm), a cluster node goes offline, a polled Modbus unit stops or
    starts answering again, or ser2net has started. Off by default; the same event for the
    same mapping is sent at most once per repeat_s."""
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
    # mask_personal_data protects: text in a hand-edited one keeps it on (_PROTECTIVE)
    mask_personal_data: bool = field(default=False, metadata=_PROTECTIVE)
    email_separately: bool = field(default=False, metadata=_ONLY_TRUE)

    @staticmethod
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "NotificationSettings":
        d = dict(d or {})
        notes = [] if notes is None else notes
        n = NotificationSettings(**_read_fields(NotificationSettings, d, notes, path))
        n.events = [e for e in (n.events or []) if e in NOTIFY_EVENTS]
        # a hand-edited value other than true is off, as before the setting existed
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
                raise ConfigError(_("Webhook format must be one of {0}.").format(_choices(WEBHOOK_FORMATS)))
            if self.webhook_format == "telegram" and not str(self.telegram_chat_id).strip():
                raise ConfigError(_("Telegram needs a chat ID."))
        if self.recipients:
            if not self.smtp_host.strip():
                raise ConfigError(_("E-mail notifications need an SMTP server."))
            if not _is_int(self.smtp_port) or not (1 <= self.smtp_port <= 65535):
                raise ConfigError(_("SMTP port must be between 1 and 65535."))
            if self.smtp_security not in SMTP_SECURITY:
                raise ConfigError(_("SMTP security must be one of {0}.").format(_choices(SMTP_SECURITY)))
            if any("@" not in a for a in self.recipients):
                raise ConfigError(_("Each e-mail recipient needs an @."))
        if not _is_int(self.repeat_s) or not (0 <= self.repeat_s <= 86400):
            raise ConfigError(_("The repeat interval must be between 0 and 86400 seconds."))


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
    role is derived from LDAP groups on each login; no local password).
    `totp_secret` (base32, app/web/totp.py): a local account that set up two-factor
    sign-in also gives a code from its authenticator app; "" = off (the default). Written
    to config.json only when set."""
    username: str = ""
    password_hash: str = ""
    role: str = "admin"
    pwd_version: int = 0
    source: str = "local"
    totp_secret: str = ""

    @property
    def totp_on(self) -> bool:
        """Does signing in to this account take a code too? (Local accounts only: LDAP
        and OIDC sign-ins get a second factor from the directory or identity provider.)"""
        return self.source == "local" and bool(self.totp_secret)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["totp_secret"]:  # a config.json without two-factor sign-in stays as it was
            del d["totp_secret"]
        return d

    @property
    def kind(self) -> str:
        """How the account signs in, for messages: 'local', 'LDAP' or 'OIDC'."""
        return self.source if self.source == "local" else self.source.upper()

    @staticmethod
    def from_dict(d: dict[str, Any], notes: Optional[list[FieldNote]] = None,
                  path: str = "") -> "User":
        d = dict(d or {})
        notes = [] if notes is None else notes
        # a hand-edited totp_secret that is no text stays on (and matches no code), as text
        u = User(**_read_fields(User, d, notes, path,
                                fallback={"totp_secret": str(d.get("totp_secret") or "")}))
        if u.role not in ROLES:
            u.role = "viewer"
        if u.source not in ("local", "ldap", "oidc"):
            u.source = "local"
        if not isinstance(u.totp_secret, str):  # hand-edited: null is off, anything else
            u.totp_secret = str(u.totp_secret or "")  # stays on (and matches no code)
        # as an app may show it: in groups, lower case, padded
        u.totp_secret = re.sub(r"[\s=]", "", u.totp_secret).upper()
        return u


# REST API tokens besides the one of AppConfig.api_token_hash (Settings → REST API token)
API_TOKENS_MAX = 50
API_TOKEN_DAYS_MAX = 3650
API_TOKEN_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
API_TOKEN_ROLES = ("viewer", "operator")  # what Settings offers ("admin" acts as operator)
_API_TOKEN_ID = re.compile(r"[0-9a-f]{16}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass
class ApiToken:
    """A named REST API bearer token, next to the one of AppConfig.api_token_hash: each
    client can have its own, with its own role and an optional expiry, and is named in the
    audit log (api-token:<name>). Only the token's SHA-256 is stored. When it was last used
    is kept in memory only (AppState.api_token_used): every save writes a backup.
    The token of api_token_hash is handled as an ApiToken without an id (auth.find_api_token)
    and keeps its audit name 'api-token'."""
    id: str = ""
    name: str = ""
    sha256: str = ""
    role: str = "viewer"
    created: float = 0.0
    expires: float = 0.0   # epoch seconds; 0 = never

    @property
    def audit_label(self) -> str:
        return f"api-token:{self.name}" if self.id else "api-token"

    def expired(self, now: Optional[float] = None) -> bool:
        return bool(self.expires) and (time.time() if now is None else now) >= self.expires


def new_api_token_id() -> str:
    return secrets.token_hex(8)


def _api_tokens(raw: Any) -> tuple[list[ApiToken], list[str]]:
    """api_tokens from a config file, and notes on what had to be changed. An entry without
    a valid SHA-256 can't match any token and is left out; a role that is not one makes the
    token read-only (viewer), never more; a missing or doubled id or an unusable name gets
    a new one (the token itself keeps working)."""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["REST API: api_tokens is not a list, so it was ignored: its tokens no "
                    "longer work"]
    tokens: list[ApiToken] = []
    notes: list[str] = []
    for i, item in enumerate(raw, 1):
        digest = item.get("sha256") if isinstance(item, dict) else None
        if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest.strip().lower()):
            notes.append(f"REST API: api_tokens entry #{i} has no valid sha256, so it was "
                         "ignored: that token no longer works")
            continue
        tid = item.get("id")
        if not (isinstance(tid, str) and _API_TOKEN_ID.fullmatch(tid)) or any(
                t.id == tid for t in tokens):
            tid = new_api_token_id()
        name = item.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not API_TOKEN_NAME.fullmatch(name) or any(t.name.casefold() == name.casefold()
                                                     for t in tokens):
            new_name = f"token-{tid[:6]}"
            notes.append(f"REST API: api_tokens entry #{i} has the name {name!r}, which is not "
                         f"1-64 letters, digits, '.', '_' or '-' or is taken: it is now named "
                         f"{new_name!r}")
            name = new_name
        role_raw = item.get("role")
        role = role_raw.strip().lower() if isinstance(role_raw, str) else ""
        if role not in ROLES:
            notes.append(f"REST API: the role {role_raw!r} of the API token {name!r} is not one "
                         f"of {', '.join(ROLES)}, so the token is read-only (viewer)")
            role = "viewer"
        tokens.append(ApiToken(id=tid, name=name, sha256=digest.strip().lower(), role=role,
                               created=_epoch(item.get("created")),
                               expires=_epoch(item.get("expires"))))
    return tokens, notes


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
    # named API tokens besides that one (optional; written only when there are any)
    api_tokens: list[ApiToken] = field(default_factory=list)
    # lifetime of a web-UI sign-in (cookie + signed token); Settings -> Web UI access
    session_timeout_s: int = SESSION_TIMEOUT_DEFAULT_S
    # LDAP/OIDC accounts not signed in for this many days are removed (0: never, the
    # default); Settings -> Users
    external_users_expire_days: int = EXPIRE_DAYS_DEFAULT
    # trace files (data/traces) not written for this many days are deleted every hour (0:
    # kept until deleted by hand, the default); Settings -> Retention
    trace_max_age_days: int = 0
    # all.log, audit.log and backups/ keep nothing older than this many days, the mapping
    # logs at most that many of their 15 (0: only their size and count limits, the
    # default); Settings -> Retention
    log_max_age_days: int = 0
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
    # What the config.json this was loaded from had that this version doesn't use, for the
    # Settings page (a load note each, too): the paths of the keys that are no setting, and
    # the version that wrote it when that is newer than this one. Never persisted (every
    # save writes this version's "written_by").
    unknown_keys: list[str] = field(default_factory=list, repr=False, compare=False)
    newer_writer: str = field(default="", repr=False, compare=False)

    # ----- (de)serialization -----
    @staticmethod
    def from_dict(d: dict[str, Any]) -> "AppConfig":
        """The config in a config.json (a backup, a restore). Values of the wrong type are
        normalized, with a load note each (_read_fields), never refused: a config that an
        older release wrote or accepted keeps loading. A part that isn't shaped like one
        (a section that is no JSON object, a list that is no list) raises ConfigError."""
        d = dict(d or {})
        notes: list[FieldNote] = []
        # read elsewhere: the sections, the values with their own rules, what is never stored
        own = ("admin_ui", "api_token_role", "api_tokens", "session_timeout_s",
               "external_users_expire_days", "users", "ldap", "oidc", "cluster",
               "notifications", "mappings", "load_notes", "ids_created", "tolerated",
               "unknown_keys", "newer_writer")
        top = _read_fields(AppConfig, d, notes, skip=own,
                           keep=("password_hash", "pwd_version", "written_by"))
        admin = dict(_section(d, "admin_ui"))
        if isinstance(admin.get("trusted_proxies"), str):  # one line (spaces or commas)
            admin["trusted_proxies"] = split_proxy_entries(admin["trusted_proxies"])
        admin_ui = AdminUI(**_read_fields(AdminUI, admin, notes, "admin_ui."))
        if not isinstance(d.get("users", []), list):  # never read as "no accounts yet"
            raise ConfigError(_("{0} must be a JSON list, not {1}.").format(
                "users", _shown(d["users"])))
        users = [User.from_dict(u, notes, f"users[{i}].")
                 for i, u in enumerate(_objects(d, "users"))]
        if not users and d.get("password_hash"):  # migrate legacy single-password config
            users = [User(username="admin", password_hash=d["password_hash"],
                          role="admin", pwd_version=int(d.get("pwd_version", 0)))]
        token_role, token_note = _api_token_role(d)
        api_tokens, token_notes = _api_tokens(d.get("api_tokens"))
        cfg = AppConfig(
            admin_ui=admin_ui,
            api_token_role=token_role,
            api_tokens=api_tokens,
            session_timeout_s=_session_timeout(d.get("session_timeout_s")),
            external_users_expire_days=_expire_days(d.get("external_users_expire_days")),
            users=users,
            ldap=LdapSettings.from_dict(_section(d, "ldap"), notes, "ldap."),
            oidc=OidcSettings.from_dict(_section(d, "oidc"), notes, "oidc."),
            cluster=ClusterSettings.from_dict(_section(d, "cluster"), notes, "cluster."),
            notifications=NotificationSettings.from_dict(_section(d, "notifications"), notes,
                                                         "notifications."),
            mappings=[MappingConfig.from_dict(m) for m in _objects(d, "mappings")],
            **top,
        )
        cfg.secret_key = cfg.secret_key or secrets.token_hex(32)
        cfg.instance_id = cfg.instance_id or secrets.token_hex(8)
        writer = d.get("written_by")
        if isinstance(writer, str) and _newer_version(writer, __version__):
            cfg.newer_writer = writer[:40]
            cfg.load_notes.append(
                f"config.json was written by ser2net {cfg.newer_writer}, a newer version than "
                f"this one ({__version__}): what only {cfg.newer_writer} knows is not used here "
                "and is left out when this version saves config.json. backups/ keeps the file "
                f"as {cfg.newer_writer} left it: restore it there after upgrading again")
        cfg._note_fields("config.json", notes)
        for i, m in enumerate(cfg.mappings):
            cfg._note_fields(f"mapping '{m.name}'", m.input_notes)
            cfg.unknown_keys += [f"mappings[{i}].{n.path}" for n in m.input_notes
                                 if n.kind == "unknown"]
        cfg.unknown_keys[:0] = [n.path for n in notes if n.kind == "unknown"]
        _normalize_admin_limits(cfg.admin_ui, cfg.load_notes)
        _normalize_proxy_settings(cfg.admin_ui, cfg.load_notes)
        _normalize_node_name(cfg.admin_ui, cfg.load_notes)
        _normalize_request_log(cfg.admin_ui, cfg.load_notes)
        _normalize_login_notice(cfg.admin_ui, cfg.load_notes)
        cfg.ids_created = not (d.get("secret_key") and d.get("instance_id"))
        days = cfg.trace_max_age_days
        if not 0 <= days <= TRACE_DAYS_MAX:  # a hand-edited value must not stop a start
            cfg.trace_max_age_days = 0 if days < 0 else TRACE_DAYS_MAX
            cfg.load_notes.append(f"trace_max_age_days {days} is not 0 (keep) or 1..{TRACE_DAYS_MAX} "
                                  f"days: {cfg.trace_max_age_days} is used")
        days = cfg.log_max_age_days
        if not 0 <= days <= LOG_DAYS_MAX:  # (likewise)
            cfg.log_max_age_days = 0 if days < 0 else LOG_DAYS_MAX
            cfg.load_notes.append(f"log_max_age_days {days} is not 0 (size limits only) or "
                                  f"1..{LOG_DAYS_MAX} days: {cfg.log_max_age_days} is used")
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
        from .web import totp  # stdlib only; the web package imports this module
        for u in cfg.users:
            if u.totp_on and not totp.is_secret(u.totp_secret):
                cfg.load_notes.append(
                    f"user {u.username!r}: totp_secret is not a base32 secret of at least 16 "
                    "characters, so no two-factor code matches it and the account can't sign "
                    f"in. `ser2net --set-password {u.username}` sets a new password and turns "
                    "two-factor sign-in off")
        for names in (n for n in spellings.values() if len(n) > 1):
            cfg.load_notes.append(
                f"LDAP accounts {', '.join(map(repr, names))} are one directory user, in "
                "different letter case: they become one account at that user's next "
                "sign-in, which ends the sessions of the other spellings (deleting one in "
                "Settings → Users deletes them all)")
        if token_note:
            cfg.load_notes.append(token_note)
        cfg.load_notes += token_notes
        if cfg.ldap.enabled and cfg.ldap.uses_tls and "tls_verify" not in (d.get("ldap") or {}):
            cfg.load_notes.append(
                "LDAP: the directory server's certificate is now checked (LDAPS / StartTLS): "
                "it must come from a CA this system trusts and name the host in the server "
                "URI. If LDAP sign-in fails with a certificate error, all.log says why; for a "
                "private CA (Active Directory Certificate Services) or a self-signed "
                "certificate, set Settings → LDAP / Active Directory → CA certificate file "
                "(ldap.ca_file). Local accounts are not affected.")
        for raw, m in zip(_objects(d, "mappings"), cfg.mappings, strict=True):
            old_id = (raw or {}).get("id")
            if old_id and old_id != m.id:
                cfg.load_notes.append(
                    f"mapping '{m.name}': its id {old_id!r} is not a valid mapping id (1-64 "
                    f"letters, digits or '_'), so it is now {m.id}: the REST API and /metrics "
                    "use the new id, and its log and traces start afresh under it")
            raw_net = (raw or {}).get("network")
            raw_net = raw_net if isinstance(raw_net, dict) else {}
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
            raw_options = (raw or {}).get("options")
            raw_options = raw_options if isinstance(raw_options, dict) else {}
            old = str(raw_options.get("trace_both") or "").strip()
            # before 2.8 (written without trace_max_mb) a trace was a path: a bare name was a
            # file in ser2net's working directory, so it moved too
            if old and (old != m.options.trace_both or "trace_max_mb" not in raw_options):
                cfg.load_notes.append(
                    f"mapping '{m.name}': its trace file is now traces/{m.id}-{m.options.trace_both} "
                    f"in the data directory (configured: {old!r}; traces are no longer written "
                    "outside it)")
            for key in ("allowed_client_ips", "priority_client_ips"):
                nets, bad = client_networks(getattr(m.network, key))
                if bad and m.kind == "net":
                    cfg.load_notes.append(
                        f"mapping '{m.name}': network.{key} entries that are not IP addresses "
                        f"or networks are left out: {', '.join(map(repr, bad))}"
                        + ("; none is usable, so every client is refused until the list is "
                           "fixed" if key == "allowed_client_ips" and not nets else ""))
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
        # MQTT clashes the edits refuse now (or warn about): kept, and said at start-up
        cfg.load_notes += _mqtt_load_notes(cfg.mappings)
        return cfg

    def serial_conflict(self, m: MappingConfig) -> str:
        """serial_conflict of ``m`` next to this config's mappings."""
        return serial_conflict(m, self.mappings)
    def _note_fields(self, where: str, notes: list[FieldNote]) -> None:
        """Add what reading `where` did (FieldNote) to the load notes."""
        self.load_notes.extend(f"{where}: {n.text()}" for n in notes if n.kind != "unknown")
        unknown = [n.path for n in notes if n.kind == "unknown"]
        if unknown:
            shown = ", ".join(unknown[:20]) + (f" and {len(unknown) - 20} more"
                                               if len(unknown) > 20 else "")
            self.load_notes.append(
                f"{where}: not settings of this version, so they are not used and are left "
                f"out when config.json is next saved (backups/ keeps the file as it was): {shown}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            # which release wrote the file (older ones drop it): a later load by an older
            # release notes what it can't keep, and the backups list shows it
            "written_by": __version__,
            "admin_ui": asdict(self.admin_ui),
            "secret_key": self.secret_key,
            "api_token_hash": self.api_token_hash,
            "api_token_role": self.api_token_role,
            # only when there are any: a config.json without named tokens stays as it was
            **({"api_tokens": [asdict(t) for t in self.api_tokens]} if self.api_tokens else {}),
            "session_timeout_s": self.session_timeout_s,
            "external_users_expire_days": self.external_users_expire_days,
            "trace_max_age_days": self.trace_max_age_days,
            "log_max_age_days": self.log_max_age_days,
            "users": [u.to_dict() for u in self.users],
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
        # TLS: both files required together (that they exist: AdminUI.check_tls_files)
        cert, key = self.admin_ui.tls_cert, self.admin_ui.tls_key
        if bool(cert) != bool(key):
            raise ConfigError(_("Set both TLS certificate and key, or neither."))

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
            raise ConfigError(_("The default language must be one of {0}.").format(_choices(UI_LANGUAGES)))
        if self.admin_ui.anonymous_role not in ROLES:
            raise ConfigError(_("Role for visitors without login must be one of {0}.").format(_choices(ROLES)))
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
        """(key, problem, content) for every part validate() rejects and every clash, and
        each TLS file a mapping names that doesn't exist (tls_files_missing)."""
        found = []
        missing = {f"mapping {m.id}": tls_files_missing(m.network)
                   for m in self.mappings if m.kind == "net"}
        for key, label, content, check in self._parts():
            try:
                check()
            except Exception as e:  # a hand-edited value of the wrong type raises others too
                found.append((key, f"{label}: {e}", content()))
            try:
                found += [(key, f"{label}: {problem}", content())
                          for problem in missing.get(key, ())]
            except Exception:  # its content can't be read: named above already
                pass
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


def mapping_revision(secret_key: str, m: "MappingConfig") -> str:
    """An opaque revision of a mapping's settings (never stored): what an edit form or a
    REST client saw, to tell whether the mapping changed since. An HMAC with this node's
    secret key, so it tells nothing about the settings (the MQTT password a viewer token
    doesn't get), and another node's revision is its own."""
    key = hmac.new(secret_key.encode(), b"ser2net mapping revision", hashlib.sha256).digest()
    body = json.dumps(m.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()[:24]


def _mapping_content(m: "MappingConfig") -> dict[str, Any]:
    content = m.to_dict()
    content.pop("enabled", None)
    return content


def _version_parts(text: str) -> Optional[tuple[int, ...]]:
    """(2, 9, 0) for "2.9.0" (or "2.9.0rc1"), None for anything else."""
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", text.strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _newer_version(text: str, than: str) -> bool:
    a, b = _version_parts(text), _version_parts(than)
    return a is not None and b is not None and a > b


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
# with log_max_age_days set, backups older than that go (ConfigStore.prune_backups), but
# never the newest BACKUPS_MIN_KEPT: a clock far ahead must not take every way back
BACKUPS_MIN_KEPT = 5
_BACKUP_NAME = re.compile(r"^config-\d{8}T\d{6}Z(-\d+)?\.json$")


def now() -> float:
    """The wall clock that names backups (tests set another)."""
    return time.time()


class _Seen(typing.NamedTuple):
    """config.json as a store last read or wrote it."""
    key: Optional[tuple[int, int]]   # (mtime_ns, size); None: unknown
    digest: str                      # sha256 of its text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stat_key(st: os.stat_result) -> tuple[int, int]:
    return st.st_mtime_ns, st.st_size


class ConfigStore:
    """config.json with atomic writes, plus automatic backups.

    Every save keeps the file it writes as the newest backup,
    <data>/backups/config-<UTC time>.json, and first the version it replaces when that is
    not the newest backup already (an older release wrote it, or it was edited by hand).
    So backups/ always holds the current config.json too: going back to a release that
    keeps no backups (2.7) and drops what it doesn't know at its first save loses nothing.
    Identical content is not copied twice; the newest BACKUPS_KEPT are kept. Starting and
    stopping mappings takes no backup of its own (save(backup=False)), so it doesn't push
    the backups of real changes out. A new name always sorts first, also when the clock
    went back (it then takes the newest name's time, with the next number). Backups are
    never read back on their own: restoring one is an explicit admin action (deleting
    config.json alone leaves them, and the next admin sees them in Settings). An admin can
    delete backups, or remove a deleted account from every backup (remove_account), in
    Settings -> Configuration backups.

    The store remembers what config.json held when it last read or wrote it. A save that
    finds something else there replaces an edit made outside ser2net (by hand, a tool)
    that was not reloaded: the save goes ahead (sign-ins, starts and stops must not fail
    for it), the edited file is kept in backups/ first as any replaced version is, and
    the replacement is reported (replaced_edit, a WARNING via log, on_replaced).
    changed_on_disk() tells of such an edit before a save does."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        directory = os.path.dirname(self.path)
        set_data_dir(directory)  # (relative TLS paths: data_path)
        os.makedirs(directory, exist_ok=True)
        # config.json holds the password hash + secret_key; keep the dir private on
        # BOTH platforms (POSIX chmod 0700 / Windows icacls owner-only inheritance).
        lock_down_dir(directory)
        self.backup_dir = os.path.join(directory, "backups")
        # what each backup holds, by name: ((size, mtime_ns), _backup_summary()) - see backups()
        self._backup_summaries: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        # where a backup that fails, or a clock behind the backups, is reported (AppState
        # sets its log; a save itself never fails because of a backup)
        self.log: Any = lambda _msg: None
        self._clock_noted = False
        self._seen: Optional[_Seen] = None
        # the last edit made outside ser2net that a save replaced: {"edited": its mtime,
        # "saved": when, "backup": the backup's name ("" when none could be written)};
        # on_replaced(that) is called too (AppState: an audit record)
        self.replaced_edit: Optional[dict] = None
        self.on_replaced: Any = None

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> AppConfig:
        if not self.exists():
            return AppConfig()
        text, seen = self.read_current()
        config = AppConfig.from_dict(json.loads(text))
        self.note_read(seen)
        return config

    def read_current(self) -> tuple[str, _Seen]:
        """config.json's text, and what note_read() remembers of it once it is used."""
        with open(self.path, "r", encoding="utf-8") as fh:
            st = os.fstat(fh.fileno())
            text = fh.read()
        return text, _Seen(_stat_key(st), _digest(text))

    def note_read(self, seen: _Seen) -> None:
        """The configuration in use is the one read_current() gave (a load, a reload)."""
        self._seen = seen

    def changed_on_disk(self) -> Optional[float]:
        """When config.json holds something other than what this store last read or wrote
        (an edit made outside ser2net, which is not in use until it is reloaded): when it
        was changed (its mtime; 0.0 when the file is gone). Else None. Blocking: a stat, and
        a read only when the stat differs from what was seen."""
        seen = self._seen
        if seen is None:
            return None
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return 0.0
        except OSError:
            return None
        if _stat_key(st) == seen.key:
            return None
        try:
            text, now_seen = self.read_current()
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError):
            return st.st_mtime
        if now_seen.digest == seen.digest:  # only touched
            self._seen = now_seen
            return None
        return st.st_mtime

    def matches(self, config: AppConfig) -> bool:
        """True when config.json already holds exactly this configuration, so saving it
        would change nothing (start-up then leaves the file alone). Which release wrote it
        doesn't count: an upgrade alone doesn't rewrite it."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
        except (OSError, ValueError):
            return False
        mine = json.loads(json.dumps(config.to_dict()))
        if isinstance(on_disk, dict):
            on_disk.pop("written_by", None)
        mine.pop("written_by", None)
        return on_disk == mine

    def save(self, config: AppConfig, *, validate: bool = True, backup: bool = True) -> bool:
        """Atomically persist config to a single file (validates first: what start-up
        accepted while broken may stay as it was, see AppConfig.validate_for_save).

        Uses a temp file + os.replace so config.json is never partially written; the
        version being replaced is kept in backups/ first, the new one after (see the class
        docstring). Content config.json holds already is not written again.
        validate=False is for a change the caller checked itself and that must not
        fail on an unrelated setting (account recovery, app/accounts.py; start-up right
        after tolerate_problems(), runtime.prepare_config).
        backup=False: a change only of which mappings run (start / stop). It takes no backup
        of its own: when the newest backup is the file it replaces, the new file takes its
        place (as the newest backup, under a new name); otherwise it is kept as usual.
        Returns whether the configuration config.json held before is kept in backups/
        (False: that backup could not be written; logged)."""
        still_accepted = config.validate_for_save() if validate else config.tolerated
        data = json.dumps(config.to_dict(), indent=2, ensure_ascii=False)
        directory = os.path.dirname(self.path)
        try:
            current, current_seen = self.read_current()
        except OSError:
            current, current_seen = None, None  # first save: nothing to keep
        if current == data:  # config.json holds this already: nothing to write
            config.tolerated = still_accepted
            self._add_backup(data)  # (newest already, unless backups/ was emptied)
            self._seen = current_seen
            return True
        # an edit made outside ser2net, which this save replaces (kept, and reported)
        outside = (current_seen is not None and self._seen is not None
                   and current_seen.digest != self._seen.digest)
        kept, replaces, kept_as = True, None, ""
        if current is not None:
            if not backup and (newest := self._newest()) is not None and newest[1] == current:
                replaces = newest[0]  # the newest backup is the file replaced: it goes
            else:
                kept_as = self._add_backup(current)
                kept = bool(kept_as)

        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cfg.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
                written = _stat_key(os.fstat(fh.fileno()))
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._seen = _Seen(written, _digest(data))
        # written: only now does a part this save fixed stop being accepted while broken
        config.tolerated = still_accepted
        _fsync_dir(directory)
        if self._add_backup(data, prune=replaces is None) and replaces is not None:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.backup_dir, replaces))
        if outside and current_seen is not None:
            self._replaced(current_seen, kept_as)
        return kept

    def _replaced(self, edited: _Seen, backup: str) -> None:
        """Report that a save replaced an edit made outside ser2net (see the class doc)."""
        mtime = edited.key[0] / 1e9 if edited.key else 0.0
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else "?"
        self.replaced_edit = {"edited": mtime, "saved": time.time(), "backup": backup}
        where = (os.path.join("backups", backup) if backup
                 else "nowhere: its backup could not be written (see above)")
        self.log(f"WARNING: config.json had been changed outside ser2net ({when}) and was not "
                 "reloaded; this save replaced it with the configuration in use. The edited "
                 f"file is kept as {where}: restore it under Settings → Configuration backups, "
                 "or edit config.json again and reload it")
        if self.on_replaced is not None:
            self.on_replaced(self.replaced_edit)

    # ----- backups -----
    def _newest(self) -> Optional[tuple[str, str]]:
        """(name, text) of the newest backup, or None."""
        for name in self.backup_names()[:1]:
            try:
                with open(os.path.join(self.backup_dir, name), encoding="utf-8") as fh:
                    return name, fh.read()
            except OSError:
                return None
        return None

    def _add_backup(self, current: str, *, prune: bool = True) -> str:
        """Make `current` (a config.json's text) the newest backup, unless it is that
        already; its name when it is now, "" when not. Best effort: a failing backup is
        logged, and never stops the save itself. The name sorts first: when the clock is
        behind the newest backup, the new one takes that one's time with the next number."""
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            lock_down_dir(self.backup_dir)
            newest = self._newest()
            if newest is not None and newest[1] == current:
                return newest[0]  # this version is already the newest backup
            clock = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now()))
            stamp = clock
            if newest is not None and (top := _backup_order(newest[0])[0]) > clock:
                stamp = top
                if not self._clock_noted:
                    self._clock_noted = True
                    self.log(f"WARNING: the system clock ({backup_time(f'config-{clock}')} "
                             f"UTC) is behind the newest configuration backup "
                             f"({backup_time(newest[0])} UTC): new backups are named after "
                             f"that one, so that they stay the newest")
            # several saves in one second: -2, -3 ... (never reuse a pruned lower number,
            # or the newest backup would sort as the oldest)
            same = [_backup_order(n)[1] for n in self.backup_names()
                    if _backup_order(n)[0] == stamp]
            n = max(same) + 1 if same else 1
            name = f"config-{stamp}Z.json" if n == 1 else f"config-{stamp}Z-{n}.json"
            fd, tmp = tempfile.mkstemp(dir=self.backup_dir, prefix=".bak.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(current)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(self.backup_dir, name))
            for old in self.backup_names()[BACKUPS_KEPT:] if prune else ():
                if old != name:  # (it sorts first; never the one just written)
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self.backup_dir, old))
            return name
        except OSError as e:
            self.log(f"WARNING: could not keep a backup of config.json in {self.backup_dir}: "
                     f"{e}")
            return ""

    def prune_backups(self, max_age_s: float, now: float) -> list[str]:
        """Delete the backups named for a time more than max_age_s before `now` (UTC, as
        their names say: a backup taken while the clock was behind is named later, so it
        stays longer), except the newest BACKUPS_MIN_KEPT; the newest one is what
        config.json holds. Returns the names deleted. Blocking."""
        gone = []
        for name in self.backup_names()[BACKUPS_MIN_KEPT:]:
            try:
                named = calendar.timegm(time.strptime(_backup_order(name)[0], "%Y%m%dT%H%M%S"))
            except ValueError:
                continue
            if named < now - max_age_s:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(self.backup_dir, name))
                    gone.append(name)
        return gone

    def backup_names(self) -> list[str]:
        """Backup file names, newest first."""
        try:
            names = [n for n in os.listdir(self.backup_dir) if _BACKUP_NAME.match(n)]
        except OSError:
            return []
        return sorted(names, key=_backup_order, reverse=True)

    def backups(self) -> list[dict]:
        """[{name, time (UTC, 'YYYY-MM-DD HH:MM:SS'), size, mappings, users, accounts:
        [{username, role, source}], written_by (the release that wrote it; None: 2.8.1 or
        older), current (it is what config.json holds now), file_time (when the file was
        written, UTC, if that is more than a minute before the time in its name: the clock
        was behind the newest backup then, so it is named after that one; else "")}],
        newest first.

        The Settings page lists them at every visit: a file's contents are read once and
        kept by its name, size and modification time (a backup is written once; one that
        remove_account rewrites gets a new modification time), so only a new or changed one
        is parsed again. In memory only: the files and their names stay as they are."""
        try:
            with open(self.path, "rb") as fh:
                current: Optional[str] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            current = None
        known, kept, out = self._backup_summaries, {}, []
        for name in self.backup_names():
            path = os.path.join(self.backup_dir, name)
            info: dict[str, Any] = {"name": name, "size": 0, "mappings": None, "users": None,
                                    "accounts": [], "time": backup_time(name),
                                    "written_by": None, "current": False, "file_time": ""}
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
                info.update({k: v for k, v in summary.items() if k != "digest"})
                info["current"] = current is not None and summary["digest"] == current
            info["size"] = st.st_size
            try:
                named = calendar.timegm(time.strptime(_backup_order(name)[0], "%Y%m%dT%H%M%S"))
            except ValueError:
                named = 0
            if named - st.st_mtime > 60:  # (a copy of data/ makes it later: not shown)
                info["file_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st.st_mtime))
            out.append(info)
        self._backup_summaries = kept  # pruned to the files there are
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


def _backup_summary(path: str) -> dict[str, Any] | None:
    """What the backups list shows of one backup's contents: mappings, users, accounts and
    the release that wrote it (no counts and no accounts for a file that isn't a
    configuration; None itself when it can't be read now), and the digest of its text (is
    it what config.json holds: ConfigStore.backups)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    empty: dict[str, Any] = {"mappings": None, "users": None, "accounts": [], "written_by": None,
                             "digest": hashlib.sha256(data).hexdigest()}
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError:
        return empty
    try:
        accounts = _backup_accounts(doc)
        writer = doc.get("written_by")
        return {**empty, "mappings": len(doc.get("mappings") or []), "users": len(accounts),
                "accounts": accounts,
                "written_by": writer[:40] if isinstance(writer, str) else None}
    except (AttributeError, TypeError):
        return empty


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
