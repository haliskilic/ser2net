"""Mapping operations shared by several entry points (web UI, cluster peers, REST API):
validate + persist + apply, without any HTTP concerns."""
from __future__ import annotations

from ...config import ConfigError, MappingConfig
from .forms import _mapping_from_form, _preserve_unmanaged_fields


async def save_mapping_from_form(state, form) -> tuple[bool, str]:
    """Parse a mapping form, validate, persist and (re)apply. Returns (ok, name)
    on success or (False, error). Shared by the local UI save and the cluster
    remote-edit save (which runs the same form on the target node)."""
    try:
        mapping = MappingConfig.from_dict(_mapping_from_form(form))
        # The form doesn't expose (or always post) every field; when editing, carry
        # those over so a save never silently wipes them (get_mapping is a sync
        # read — safe outside the config lock).
        _preserve_unmanaged_fields(mapping, state.config.get_mapping(mapping.id), form)
        mapping.validate()
    except (ValueError, ConfigError) as e:
        return False, str(e)
    # Hold the lock only to mutate + persist config; release it BEFORE the
    # (potentially slow) supervisor call so other admin requests aren't blocked
    # while a serial port is being (re)opened.
    async with state.config_lock:
        backup = list(state.config.mappings)
        existing = state.config.get_mapping(mapping.id)
        if existing:
            state.config.mappings[state.config.mappings.index(existing)] = mapping
        else:
            state.config.mappings.append(mapping)
        try:
            await state.asave()
        except ConfigError as e:
            state.config.mappings = backup
            return False, str(e)
    await state.supervisor.apply_mapping(mapping)
    return True, mapping.name


async def apply_action(state, mid, action) -> tuple[bool, str]:
    """Start/stop/restart a local mapping by id. Returns (ok, name-or-error).
    Shared by the UI action endpoint, the cluster remote-control endpoint and the
    REST API."""
    async with state.config_lock:
        m = state.config.get_mapping(mid)
        if not m:
            return False, "Mapping not found"
        m.enabled = action != "stop"
        await state.asave()
    if action == "stop":
        await state.supervisor.stop_mapping(mid)
    elif action == "restart":
        await state.supervisor.restart_mapping(m)
    else:  # start
        await state.supervisor.apply_mapping(m)
    return True, m.name


async def delete_mapping(state, mid):
    """Remove a mapping from the config, stop it and drop its log. Returns the removed
    MappingConfig, or None when no mapping has that id."""
    removed = None
    async with state.config_lock:
        m = state.config.get_mapping(mid)
        if m:
            state.config.mappings.remove(m)
            await state.asave()
            removed = m
    if removed is not None:
        await state.supervisor.remove_mapping(mid)
        state.delete_mapping_log(mid)
    return removed
