"""Importing mappings: parse an uploaded file, plan the change, apply it.

The web UI first shows a preview (plan_rows) and only then applies the import, in
one of two modes:
  * merge   — a mapping with the same id, else the same name, is updated in place
              (keeping the existing id, so its log and traces stay attached); the
              others are added; mappings not in the file stay as they are;
  * replace — the file's mappings replace all current ones (the pre-v2.8 behaviour).
Either way the previous config.json is kept in data/backups/ by ConfigStore.save.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import ConfigError, MappingConfig, _new_id
from .ser2net_import import looks_yaml, parse_ser2net_config

MODES = ("merge", "replace")


@dataclass
class ImportPlan:
    source: str                         # what the file was, for the preview
    mappings: list[MappingConfig]
    warnings: list[str] = field(default_factory=list)


def parse_mappings_file(raw: bytes, filename: str = "") -> ImportPlan:
    """A ser2net mappings export ({"mappings": [...]}), a whole config.json, or a
    classic ser2net configuration (ser2net.yaml of ser2net 4.x / ser2net.conf of 3.x).
    Raises ConfigError with a user-facing message."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError("The file is not UTF-8 text.") from None
    if not text.lstrip().startswith(("{", "[")):
        return _classic_ser2net(text, filename)
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"The file is not valid JSON ({e}).") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("mappings"), list):
        raise ConfigError('Expected a ser2net export: a JSON object with a "mappings" list.')
    mappings, warnings = [], []
    for i, item in enumerate(doc["mappings"], 1):
        if not isinstance(item, dict):
            raise ConfigError(f"Mapping #{i} is not an object.")
        try:
            mappings.append(MappingConfig.from_dict(item))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"Mapping #{i} ({item.get('name', '?')}): {e}") from None
    source = "ser2net config.json" if "users" in doc or "admin_ui" in doc else "ser2net export"
    if source == "ser2net config.json":
        warnings.append("Only the mappings are imported from a config.json; users, "
                        "passwords and settings stay as they are.")
    return ImportPlan(source=source, mappings=mappings, warnings=warnings)


def _classic_ser2net(text: str, filename: str) -> ImportPlan:
    """A configuration file of the classic C ser2net, converted (app/ser2net_import).
    Nothing importable is an error, so a replace import cannot empty the mappings."""
    dicts, warnings = parse_ser2net_config(text, filename)
    if not dicts:
        raise ConfigError(" ".join(warnings) or "The file has no ser2net connections to import.")
    source = "ser2net.yaml (ser2net 4.x)" if looks_yaml(text) else "ser2net.conf (ser2net 3.x)"
    return ImportPlan(source=source, mappings=[MappingConfig.from_dict(d) for d in dicts],
                      warnings=warnings)


def _match(existing: list[MappingConfig], m: MappingConfig):
    return (next((e for e in existing if e.id == m.id), None)
            or next((e for e in existing if e.name == m.name), None))


def apply_import(current: list[MappingConfig], incoming: list[MappingConfig],
                 mode: str) -> tuple[list[MappingConfig], list[MappingConfig]]:
    """(new mapping list, mappings that were added or changed). Pure: the inputs
    are not modified."""
    if mode not in MODES:
        raise ConfigError(f"Import mode must be one of {MODES}.")
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
    for m in incoming:
        m = MappingConfig.from_dict(m.to_dict())
        target = _match(result, m)
        if target is not None:
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
              mode: str) -> list[dict]:
    """Preview rows: {name, kind, action: add|update|same|remove, target}."""
    rows = []
    for m in incoming:
        target = _match(current, m) if mode == "merge" else None
        if target is None:
            action = "add"
        else:
            probe = MappingConfig.from_dict({**m.to_dict(), "id": target.id})
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
