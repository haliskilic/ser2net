"""Importing mappings: parse an uploaded file, plan the change, apply it.

The web UI first shows a preview (plan_rows) and only then applies the import, in
one of two modes:
  * merge   — a mapping with the same id, else the same name, is updated in place
              (keeping the existing id, so its log and traces stay attached; a setting
              the file doesn't have — an export of an older version — keeps its current
              value); the others are added; mappings not in the file stay as they are;
  * replace — the file's mappings replace all current ones (the pre-v2.8 behaviour).
Either way the previous config.json is kept in data/backups/ by ConfigStore.save.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
from dataclasses import dataclass, field

from .config import ConfigError, MappingConfig, ModbusPoint, _new_id
from .i18n import _
from .ser2net_import import looks_yaml, parse_ser2net_config

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
    # per mapping, the settings the file doesn't have (see absent_settings); a merge keeps
    # their current values. Empty for a classic ser2net configuration.
    absent: list[list[str]] = field(default_factory=list)


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
    except (ValueError, RecursionError) as e:  # RecursionError: nested too deeply
        raise ConfigError(_("The file is not valid JSON ({0}).").format(e)) from None
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
            m.validate_input()
        except (TypeError, ValueError) as e:  # ConfigError is a ValueError
            raise ConfigError(_("Mapping #{0} ({1}): {2}").format(i, item.get('name', '?'), e)) from None
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
    for m in mappings:
        m.validate_input()  # the converter skips URL devices already; this backs it up
    return ImportPlan(source=source, mappings=mappings, warnings=warnings)


def _match(existing: list[MappingConfig], m: MappingConfig):
    return (next((e for e in existing if e.id == m.id), None)
            or next((e for e in existing if e.name == m.name), None))


def apply_import(current: list[MappingConfig], incoming: list[MappingConfig],
                 mode: str, absent: list[list[str]] | None = None,
                 ) -> tuple[list[MappingConfig], list[MappingConfig]]:
    """(new mapping list, mappings that were added or changed). `absent`: per incoming
    mapping, the settings its file didn't have (ImportPlan.absent): a merge keeps the
    current values. Pure: the inputs are not modified."""
    if mode not in MODES:
        raise ConfigError(_("Import mode must be one of {0}.").format(MODES))
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
            m = _keep_absent(m, target, absent[i] if absent and i < len(absent) else [])
            m.id = target.id
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
              mode: str, absent: list[list[str]] | None = None) -> list[dict]:
    """Preview rows: {name, kind, action: add|update|same|remove, target}."""
    rows = []
    for i, m in enumerate(incoming):
        target = _match(current, m) if mode == "merge" else None
        if target is None:
            action = "add"
        else:
            merged = _keep_absent(m, target, absent[i] if absent and i < len(absent) else [])
            probe = MappingConfig.from_dict({**merged.to_dict(), "id": target.id})
            action = "same" if probe.to_dict() == target.to_dict() else "update"
        rows.append({"name": m.name, "kind": _kind(m), "action": action,
                     "target": target.name if target is not None else ""})
    if mode == "replace":
        for e in current:
            rows.append({"name": e.name, "kind": _kind(e), "action": "remove", "target": ""})
    return rows


def _kind(m: MappingConfig) -> str:
    if m.kind == "serialbridge":
        return f"{m.serial.port} ↔ {m.serial_b.port}"
    net = m.network
    where = (f"→ {net.remote_host}:{net.remote_port}" if net.mode == "client"
             else f"{net.bind_ip}:{net.port}")
    return f"{m.serial.port} · {net.mode}/{net.protocol} {where}"
