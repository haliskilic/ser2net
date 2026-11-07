"""Mapping operations shared by several entry points (web UI, cluster peers, REST API):
validate + persist + apply, without any HTTP concerns."""
from __future__ import annotations

from ...config import ConfigError, MappingConfig, valid_mapping_id
from ...engine import portlist
from ...i18n import _
from .forms import _checkbox, _mapping_from_form, _preserve_unmanaged_fields


def _gone() -> str:
    return _("This mapping no longer exists (it may have been deleted in the meantime). "
             "Close the form and add it again.")


def apply_usb_match(mapping: MappingConfig, form, ports: list, existing=None) -> None:
    """Set each serial side's stable USB identity (serial.match) from the form's
    "find this device by its USB identity" box: the selected port's VID/PID (+ serial
    number when it has one), so the engine re-finds the adapter after COMx / ttyUSBx
    renumbering. A device that is not plugged in right now keeps its stored identity.
    Raises ValueError when the box is set for a port without a USB identity."""
    for prefix, settings, old in (("serial_", mapping.serial, existing.serial if existing else None),
                                  ("serial_b_", mapping.serial_b, existing.serial_b if existing else None)):
        if f"{prefix}usb_match_shown" not in form or (prefix == "serial_b_" and mapping.kind != "serialbridge"):
            continue
        if not _checkbox(form, prefix + "usb_match"):
            settings.match = {}
            continue
        port = portlist.find(ports, settings.port)
        if port is not None and port.get("vid") and port.get("pid"):
            settings.match = {k: port[k] for k in ("vid", "pid", "serial_number") if port.get(k)}
        elif port is None and old is not None and old.match:
            settings.match = dict(old.match)  # unplugged now: keep what it was bound to
        else:
            raise ValueError(_("{0} has no USB identity (VID/PID) to find it by; untick “Find "
                               "this device by its USB identity”.").format(settings.port or _("This port")))


async def save_mapping_from_form(state, form) -> tuple[bool, str]:
    """Parse a mapping form, validate, persist and (re)apply. Returns (ok, name)
    on success or (False, error). Shared by the local UI save and the cluster
    remote-edit save (which runs the same form on the target node).

    The server owns mapping ids: a form without an id creates a mapping with a new
    one, a form with an id edits that mapping and is refused when there is none."""
    posted_id = form.get("id") or ""
    try:
        mapping = MappingConfig.from_dict(_mapping_from_form(form))
        # The form doesn't expose (or always post) every field; when editing, carry
        # those over so a save never silently wipes them (get_mapping is a sync
        # read — safe outside the config lock).
        existing = state.config.get_mapping(posted_id) if valid_mapping_id(posted_id) else None
        if posted_id and existing is None:
            return False, _gone()
        _preserve_unmanaged_fields(mapping, existing, form)
        apply_usb_match(mapping, form, state.ports.get(), existing)
        mapping.validate()
    except (ValueError, ConfigError) as e:
        return False, str(e)
    # Hold the lock only to mutate + persist config; release it BEFORE the
    # (potentially slow) supervisor call so other admin requests aren't blocked
    # while a serial port is being (re)opened.
    async with state.config_lock:
        backup = list(state.config.mappings)
        if posted_id:
            existing = state.config.get_mapping(mapping.id)
            if existing is None:  # deleted while this request was being checked
                return False, _gone()
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
    """Remove a mapping from the config, stop it and drop its log and traces. Returns the removed
    MappingConfig, or None when no mapping has that id."""
    removed = None
    async with state.config_lock:
        m = state.config.get_mapping(mid)
        if m:
            state.config.mappings.remove(m)
            await state.asave()
            removed = m
    if removed is not None:  # by the stored id, never the id from the request
        await state.supervisor.remove_mapping(removed.id)
        state.delete_mapping_log(removed.id)
        state.delete_mapping_traces(removed.id)
    return removed
