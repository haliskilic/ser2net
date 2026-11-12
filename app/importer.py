"""Importing mappings: parse an uploaded file, plan the change, apply it.

The web UI first shows a preview (plan_rows) and only then applies the import, in
one of two modes:
  * merge   — a mapping with the same id, else the same name, is updated in place
              (keeping the existing id, so its log and traces stay attached; a setting
              the file doesn't have — an export of an older version — keeps its current
              value); the others are added; mappings not in the file stay as they are;
  * replace — the file's mappings replace all current ones (the pre-v2.8 behaviour).
Either way the previous config.json is kept in data/backups/ by ConfigStore.save.

A mapping from a ser2net export or config.json replaces the one it updates. One from a
classic ser2net file says only part of what a mapping has here, so a merge takes only
that part from it and keeps the rest (_overlay): TLS, the bind IP or the allowed clients
set after the first import, its USB identity, RS-485, MQTT, Modbus points. The preview
lists what an update changes and what it keeps, and how each mapping will be after the
import (enabled, TLS, which clients, read-only) with its own warnings.
"""
from __future__ import annotations

import contextvars
import copy
import dataclasses
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from . import i18n
from .config import (
    AppConfig, ConfigError, MappingConfig, ModbusPoint, _new_id, host_port, normalize_cidr,
)
from .i18n import _
from .ser2net_import import Note, looks_yaml, parse_ser2net_config

MODES = ("merge", "replace")
# An import file is read whole and its mappings are previewed and validated at once:
# limits far above any real export or config.json (and the classic parser has its own).
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_MAPPINGS = 1000


@dataclass
class ImportPlan:
    source: str                         # what the file was, for the preview
    mappings: list[MappingConfig]
    warnings: list[str] = field(default_factory=list)
    classic: bool = False               # a classic ser2net file: a merge overlays (_overlay)
    # the source and the warnings in English, for the log and the audit log (which stay
    # English: parse_for_preview); empty: as `source` / `warnings`
    log_source: str = ""
    log_warnings: list[str] = field(default_factory=list)
    # per mapping, the settings the file doesn't have (see absent_settings); a merge keeps
    # their current values. Empty for a classic ser2net configuration.
    absent: list[list[str]] = field(default_factory=list)

    def split_warnings(self) -> tuple[list[str], list[list[str]]]:
        """The warnings about the file as a whole, and those about each mapping (by its
        position in `mappings`; a classic file's ser2net_import.Note knows its mapping),
        which the preview shows in the mapping's row."""
        general: list[str] = []
        per: list[list[str]] = [[] for _m in self.mappings]
        for w in self.warnings:
            i = getattr(w, "mapping", None)
            (per[i] if i is not None and 0 <= i < len(per) else general).append(w)
        return general, per


def parse_for_preview(raw: bytes, filename: str = "") -> ImportPlan:
    """parse_mappings_file, with the source and the warnings in English as well (log_*):
    applying the import logs them, and the log and the audit log stay English whatever
    the language of the page."""
    plan = parse_mappings_file(raw, filename)
    if i18n.current() == i18n.DEFAULT:
        plan.log_source, plan.log_warnings = plan.source, list(plan.warnings)
    else:
        english = contextvars.copy_context().run(_in_english, raw, filename)
        plan.log_source, plan.log_warnings = english.source, english.warnings
    return plan


def _in_english(raw: bytes, filename: str) -> ImportPlan:
    i18n.set_language(i18n.DEFAULT)  # (in a copy of the context: for this call only)
    return parse_mappings_file(raw, filename)


def parse_mappings_file(raw: bytes, filename: str = "") -> ImportPlan:
    """A ser2net mappings export ({"mappings": [...]}), a whole config.json, or a
    classic ser2net configuration (ser2net.yaml of ser2net 4.x / ser2net.conf of 3.x).
    Raises ConfigError with a user-facing message."""
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigError(_("The file is larger than {0} MB.").format(MAX_FILE_BYTES // 2**20))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(_("The file is not UTF-8 text.")) from None
    if not text.lstrip().startswith(("{", "[")):
        return _classic_ser2net(text, filename)
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as e:
        # where, not the parser's English words ("Expecting ',' delimiter: ...")
        where = (_("line {0}, column {1}").format(e.lineno, e.colno)
                 if isinstance(e, json.JSONDecodeError) else
                 _("nested too deeply") if isinstance(e, RecursionError) else str(e))
        raise ConfigError(_("The file is not valid JSON ({0}).").format(where)) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("mappings"), list):
        raise ConfigError(_('Expected a ser2net export: a JSON object with a "mappings" list.'))
    if len(doc["mappings"]) > MAX_MAPPINGS:
        raise ConfigError(_("The file has {0} mappings; at most {1} can be imported at once.")
                          .format(len(doc["mappings"]), MAX_MAPPINGS))
    mappings: list[MappingConfig] = []
    warnings: list[str] = []
    absent: list[list[str]] = []
    for i, item in enumerate(doc["mappings"], 1):
        if not isinstance(item, dict):
            raise ConfigError(_("Mapping #{0} is not an object.").format(i))
        try:
            m = MappingConfig.from_dict(item)
            # TLS files of a data directory that moved are this one's (tls folder) before an
            # edit's rules ask whether they exist
            moved = AppConfig(mappings=[m]).find_moved_tls_files()
            m.validate_input()
        except (TypeError, ValueError) as e:  # ConfigError is a ValueError
            raise ConfigError(_("Mapping #{0} ({1}): {2}").format(i, item.get('name', '?'), e)) from None
        if moved:
            warnings.append(_("Mapping #{0} ({1}): its TLS files are not where the file says; "
                              "the files of that name in this data directory's tls folder are "
                              "used.").format(i, m.name))
        warnings.extend(_tls_warning(i, m))
        warnings.extend(_("Mapping #{0} ({1}): {2}").format(i, m.name, w)
                        for w in m.input_warnings() + m.ignored_settings())
        if item.get("id") and item["id"] != m.id:
            warnings.append(_("Mapping #{0} ({1}): {2} is not a valid mapping id (1-64 letters, "
                              "digits or _), so it gets a new one.").format(
                i, m.name, repr(item["id"])[:80]))
        absent.append(absent_settings(item))
        if absent[-1]:
            names = list(dict.fromkeys(re.sub(r"\[\d+\]", "[]", p) for p in absent[-1]))
            warnings.append(_("Mapping #{0} ({1}): the file has no {2} (an export of an older "
                              "version?). Merged into a mapping of the same name, they keep "
                              "their current values; otherwise they take the defaults.").format(
                i, m.name, ", ".join(names[:8]) + (" …" if len(names) > 8 else "")))
        mappings.append(m)
    whole_config = "users" in doc or "admin_ui" in doc
    source = _("ser2net config.json") if whole_config else _("ser2net export")
    if whole_config:
        warnings.append(_("Only the mappings are imported from a config.json; users, "
                          "passwords and settings stay as they are."))
    return ImportPlan(source=source, mappings=mappings, warnings=warnings, absent=absent)


_FULL_MAPPING = MappingConfig().to_dict()
_FULL_POINT = dataclasses.asdict(ModbusPoint())
_POINT = re.compile(r"modbus_poll\.points\[(\d+)\]")


def absent_settings(item: dict, full: dict = _FULL_MAPPING, path: str = "") -> list[str]:
    """The settings (paths like "options.trace_max_mb", "modbus_poll.points[2].writable")
    a mapping of an import file doesn't have, e.g. because an older version wrote it."""
    out = []
    for key, default in full.items():
        if key == "id":
            continue
        if not isinstance(item, dict) or key not in item:
            out.append(path + key)
        elif key == "points" and isinstance(item[key], list):
            for i, point in enumerate(item[key]):
                if isinstance(point, dict):
                    out += [f"{path}points[{i}].{k}" for k in _FULL_POINT if k not in point]
        elif isinstance(default, dict) and key != "match":  # a section (match is a value)
            out += absent_settings(item[key], default, f"{path}{key}.")
    return out


def _keep_absent(m: MappingConfig, target: MappingConfig, absent: list[str]) -> MappingConfig:
    """*m* (merged into *target*) with target's values for the settings the file didn't
    have; a Modbus point's by its name ("writable" only while it is a coil or holding
    register)."""
    if not absent:
        return m
    d, old = m.to_dict(), target.to_dict()
    old_points = {p["name"]: p for p in old["modbus_poll"]["points"]}
    for path in absent:
        head, _dot, key = path.rpartition(".")
        if pm := _POINT.fullmatch(head):
            points, n = d["modbus_poll"]["points"], int(pm.group(1))
            if n < len(points) and (stored := old_points.get(points[n]["name"])) is not None \
                    and (key != "writable" or points[n]["fn"] in (1, 3)):
                points[n][key] = copy.deepcopy(stored[key])
            continue
        src, dst = old, d
        for part in head.split(".") if head else ():
            src, dst = src[part], dst[part]
        dst[key] = copy.deepcopy(src[key])
    return MappingConfig.from_dict(d)


def _classic_ser2net(text: str, filename: str) -> ImportPlan:
    """A configuration file of the classic C ser2net, converted (app/ser2net_import).
    Nothing importable is an error, so a replace import cannot empty the mappings."""
    dicts, warnings = parse_ser2net_config(text, filename)
    if not dicts:
        raise ConfigError(" ".join(warnings) or _("The file has no ser2net connections to import."))
    source = _("ser2net.yaml (ser2net 4.x)") if looks_yaml(text) else _("ser2net.conf (ser2net 3.x)")
    mappings = [MappingConfig.from_dict(d) for d in dicts]
    for i, m in enumerate(mappings, 1):
        m.validate_input()  # the converter skips URL devices already; this backs it up
        for w in _tls_warning(i, m):
            note = Note(w)
            note.mapping = i - 1   # (shown in its row of the preview)
            warnings.append(note)
    return ImportPlan(source=source, mappings=mappings, warnings=warnings, classic=True)


def _tls_warning(i: int, m: MappingConfig) -> list[str]:
    """A TLS mapping whose certificate and key don't load here (a key that needs its
    password, files of another machine) is imported all the same, with a warning: it can't
    start until that is fixed, and never runs without TLS."""
    try:
        m.check_tls_files()
    except ConfigError as e:
        return [_("Mapping #{0} ({1}): {2} It can't start until that is fixed.").format(
            i, m.name, e)]
    return []


def _match(existing: list[MappingConfig], m: MappingConfig):
    return (next((e for e in existing if e.id == m.id), None)
            or next((e for e in existing if e.name == m.name), None))


def _bind_reach(ip: str) -> int:
    """How far a bind IP reaches: 0 the loopback, 1 one address, 2 every interface."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return 1
    return 0 if a.is_loopback else 2 if a.is_unspecified else 1


def _reach(net) -> int:
    """How far a listener (NetworkSettings) reaches: _bind_reach, and 3 for every
    interface of both families (dual_stack on 0.0.0.0 / ::)."""
    return 3 if net.both_families else _bind_reach(net.bind_ip)


def _within(inner: list[str], outer: list[str]) -> bool:
    """The clients `inner` allows are all allowed by `outer` too (an empty list: any)."""
    if not outer:
        return True
    if not inner:
        return False
    try:
        nets_in, nets_out = ([ipaddress.ip_network(normalize_cidr(x), strict=False) for x in xs]
                             for xs in (inner, outer))
    except ValueError:
        return sorted(inner) == sorted(outer)
    return all(any(n.version == w.version and n.subnet_of(w) for w in nets_out)  # type: ignore[arg-type]
               for n in nets_in)


def _overlay(old: MappingConfig, new: MappingConfig) -> MappingConfig:
    """`new`, from a classic ser2net file, merged into `old`, the mapping it updates. The
    file says only part of what a mapping has here (what ser2net_import writes), so only
    that is taken from it, and never so that the mapping gets more open: a disabled
    mapping stays disabled, the bind IP (with the families it takes, dual_stack), the
    allowed clients and read-only are not widened, TLS is not turned off and a TLS client
    CA is not dropped, a Modbus gateway stays one. What the file leaves at ser2net's
    default (no banner, no timeout, ...) keeps the mapping's own value, the port locking
    (flock / uucplock) too; the serial framing is always the file's (ser2net ran with it,
    9600N81 by default)."""
    r = MappingConfig.from_dict(old.to_dict())
    plain = MappingConfig()

    def given(dst, src, default, *attrs):  # the values the file sets (not the default)
        for a in attrs:
            if getattr(src, a) != getattr(default, a):
                setattr(dst, a, getattr(src, a))
    r.name, r.kind = new.name, new.kind
    r.enabled = old.enabled and new.enabled
    s, ns = r.serial, new.serial
    s.port, s.baudrate, s.bytesize, s.parity, s.stopbits, s.flowcontrol = (
        ns.port, ns.baudrate, ns.bytesize, ns.parity, ns.stopbits, ns.flowcontrol)
    given(s, ns, plain.serial, "rts_on_open", "dtr_on_open", "exclusive")
    given(s.advanced, ns.advanced, plain.serial.advanced, "hangup_when_done", "nobreak",
          "tiocexcl", "respect_uucp_locks")
    n, nn = r.network, new.network
    n.mode, n.port = nn.mode, nn.port
    if old.network.protocol != "modbus" or nn.mode != "server":  # (a gateway is a TCP server)
        n.protocol = nn.protocol
    if _reach(nn) <= _reach(n):
        n.bind_ip, n.dual_stack = nn.bind_ip, nn.dual_stack
    if nn.allowed_client_ips and _within(nn.allowed_client_ips, n.allowed_client_ips):
        n.allowed_client_ips = list(nn.allowed_client_ips)
    n.read_only = n.read_only or nn.read_only
    if nn.tls:
        n.tls, n.tls_cert, n.tls_key = True, nn.tls_cert, nn.tls_key
        if nn.tls_client_ca:   # (clientauth with its CA file here; a CA the mapping has stays)
            n.tls_client_ca = nn.tls_client_ca
    given(n, nn, plain.network, "kick_old_user", "max_connections")
    if n.protocol == "rfc2217":
        n.max_connections = 1  # (RFC 2217 takes a single client here: the file's is 1 too)
    o, no = r.options, new.options
    given(o, no, plain.options, "idle_timeout_s", "banner", "openstr", "closestr", "closeon")
    if no.trace_both:
        o.trace_both, o.trace_hexdump, o.trace_timestamp = (
            no.trace_both, no.trace_hexdump, no.trace_timestamp)
    return r


def _merged(target: MappingConfig, m: MappingConfig, classic: bool,
            absent: Optional[list[str]] = None) -> MappingConfig:
    """What `m` makes of the mapping `target` it updates (a copy; `m.id` is not used): a
    classic ser2net file's mapping keeps what such a file can't say (_overlay), any other
    target's values for the settings its file didn't have (`absent`, _keep_absent)."""
    if classic:
        return _overlay(target, m)
    return MappingConfig.from_dict({**_keep_absent(m, target, absent or []).to_dict(),
                                    "id": target.id})


def apply_import(current: list[MappingConfig], incoming: list[MappingConfig],
                 mode: str, classic: bool = False, absent: list[list[str]] | None = None,
                 ) -> tuple[list[MappingConfig], list[MappingConfig]]:
    """(new mapping list, mappings that were added or changed). Pure: the inputs
    are not modified. An update in merge mode keeps what the file doesn't say: `classic`
    (a classic ser2net file) what such a file can't say (_overlay); else `absent`, per
    incoming mapping the settings its file didn't have (ImportPlan.absent), keep their
    current values (_keep_absent)."""
    if mode not in MODES:
        raise ConfigError(_("Import mode must be one of {0}.").format(", ".join(MODES)))
    if mode == "replace":
        seen: set[str] = set()
        out = []
        for m in incoming:
            m = MappingConfig.from_dict(m.to_dict())
            if m.id in seen:
                m.id = _new_id()
            seen.add(m.id)
            out.append(m)
        return out, list(out)
    result = [MappingConfig.from_dict(m.to_dict()) for m in current]
    touched = []
    for i, m in enumerate(incoming):
        m = MappingConfig.from_dict(m.to_dict())
        target = _match(result, m)
        if target is not None:
            m = _merged(target, m, classic, absent[i] if absent and i < len(absent) else [])
            if m.to_dict() == target.to_dict():
                continue  # identical: nothing to restart
            result[result.index(target)] = m
        else:
            if any(e.id == m.id for e in result):
                m.id = _new_id()
            result.append(m)
        touched.append(m)
    return result, touched


def plan_rows(current: list[MappingConfig], incoming: list[MappingConfig],
              mode: str, classic: bool = False,
              notes: Optional[list[list[str]]] = None,
              absent: list[list[str]] | None = None) -> list[dict]:
    """Preview rows: {name, kind, action: add|update|same|remove, target, changes, after,
    notes}. For an update, `changes` lists what it changes and what it keeps of the
    mapping (_diff); `after` is how the mapping will be (_after); `notes` are its
    warnings (ImportPlan.split_warnings, in the order of `incoming`), those about its
    protection first. `classic` / `absent`: as for apply_import."""
    rows = []
    for i, m in enumerate(incoming):
        target = _match(current, m) if mode == "merge" else None
        changes: list[dict] = []
        result = m
        if target is None:
            action = "add"
        else:
            result = _merged(target, m, classic, absent[i] if absent and i < len(absent) else [])
            action = "same" if result.to_dict() == target.to_dict() else "update"
            changes = _diff(target, m, result)
        own = [{"text": getattr(n, "detail", "") or n, "security": getattr(n, "security", False)}
               for n in (notes[i] if notes and i < len(notes) else [])]
        rows.append({"name": m.name, "kind": _kind(m), "action": action,
                     "target": target.name if target is not None else "", "changes": changes,
                     "after": _after(result),
                     "notes": sorted(own, key=lambda n: not n["security"])})
    if mode == "replace":
        for e in current:
            rows.append({"name": e.name, "kind": _kind(e), "action": "remove", "target": "",
                         "changes": [], "after": None, "notes": []})
    return rows


def _after(m: MappingConfig) -> dict:
    """How a mapping will be after the import, for the preview (as the dashboard tags it):
    enabled, TLS, read-only, `clients` (entries of its allow-list; None: it doesn't
    listen) and `open` (enabled and taking any client beyond the loopback)."""
    n = m.network
    listens = m.kind == "net" and n.mode != "client"
    return {"enabled": m.enabled, "tls": listens and n.tls, "read_only": n.read_only,
            "clients": len(n.allowed_client_ips) if listens else None,
            "open": listens and m.enabled and not n.allowed_client_ips
            and _reach(n) > 0}


_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def log_lines(plan: ImportPlan) -> list[str]:
    """The import's warnings for all.log, in English, one line each: a line break or other
    control character from the file is written as its escape (it would start a log line
    of its own)."""
    warnings = plan.log_warnings if plan.log_source else plan.warnings
    return [_CONTROL.sub(lambda m: m.group().encode("unicode_escape").decode(), w)
            for w in warnings]


def audit_detail(plan: ImportPlan, most: int = 500) -> str:
    """The audit record of an import: how many mappings from what, and its warnings (in
    English), cut to `most` characters."""
    source, warnings = ((plan.log_source, plan.log_warnings) if plan.log_source else
                        (plan.source, plan.warnings))
    text = f"{len(plan.mappings)} mappings ({source})"
    if warnings:
        text += f"; {len(warnings)} warnings: " + "; ".join(warnings)
    return text if len(text) <= most else text[:most - 1] + "…"


def _shown(value: str, most: int = 60) -> str:
    """A string option in the preview: control characters escaped, cut at `most`, — for
    none."""
    v = value.encode("unicode_escape").decode("ascii") if not value.isprintable() else value
    return "—" if not v else v if len(v) <= most else v[:most - 1] + "…"


def _fields(m: MappingConfig) -> list[tuple[str, str]]:
    """What the preview compares of a mapping: (label, value as shown), in the order of
    the mapping form."""
    s, n, o = m.serial, m.network, m.options
    on, off = _("on"), _("off")
    framing = s.compact() + (f" · {s.flowcontrol}" if s.flowcontrol != "none" else "")
    return [
        (_("State"), _("enabled") if m.enabled else _("disabled")),
        (_("Serial port"), s.port),
        (_("Serial"), framing),
        (_("RTS on open"), s.rts_on_open),
        (_("DTR on open"), s.dtr_on_open),
        (_("Protocol"), f"{n.mode}/{n.protocol}"),
        (_("Bind IP"), _("All interfaces (IPv4 + IPv6)") if n.both_families else n.bind_ip),
        (_("Listen port"), str(n.port)),
        ("TLS", on if n.tls else off),
        (_("Allowed client IPs / CIDRs"), " ".join(n.allowed_client_ips) or _("any client")),
        (_("Read-only"), on if n.read_only else off),
        (_("Max connections"), str(n.max_connections)),
        (_("Kick old client"), on if n.kick_old_user else off),
        (_("Idle timeout (s)"), str(o.idle_timeout_s)),
        (_("Connect banner"), _shown(o.banner)),
        ("openstr", _shown(o.openstr)),
        ("closestr", _shown(o.closestr)),
        (_("Close-on string"), _shown(o.closeon)),
        (_("Trace file name"), _shown(o.trace_both)),
    ]


def _looser(old: MappingConfig, new: MappingConfig) -> set[str]:
    """The fields (labels of _fields) in which `new` is more open than `old`: started,
    TLS off, a wider bind IP or allow-list, writable."""
    a, b = old.network, new.network
    out = set()
    if new.enabled and not old.enabled:
        out.add(_("State"))
    if a.tls and not b.tls:
        out.add("TLS")
    if _reach(b) > _reach(a):
        out.add(_("Bind IP"))
    if not _within(b.allowed_client_ips, a.allowed_client_ips):
        out.add(_("Allowed client IPs / CIDRs"))
    if a.read_only and not b.read_only:
        out.add(_("Read-only"))
    return out


def _diff(old: MappingConfig, file: MappingConfig, result: MappingConfig) -> list[dict]:
    """What an update does to the mapping `old`: each field it changes ({what, before,
    after}) and, for a classic file, each one the file has otherwise that the mapping
    keeps ({what, before, file, kept: True}). `security`: the change opens the mapping
    up, or the file would have."""
    before, given, after = _fields(old), _fields(file), _fields(result)
    looser, would = _looser(old, result), _looser(old, file)
    out = []
    for (what, b), (_l, f), (_m, a) in zip(before, given, after, strict=True):
        if b != a:
            out.append({"what": what, "before": b, "after": a, "kept": False,
                        "security": what in looser})
        elif f != a:
            out.append({"what": what, "before": b, "file": f, "kept": True,
                        "security": what in would})
    return out


def _kind(m: MappingConfig) -> str:
    if m.kind == "serialbridge":
        return f"{m.serial.port} ↔ {m.serial_b.port}"
    net = m.network
    where = (f"→ {host_port(net.remote_host, net.remote_port)}" if net.mode == "client"
             else f"{net.bind_shown}:{net.port}")
    return f"{m.serial.port} · {net.mode}/{net.protocol} {where}"
