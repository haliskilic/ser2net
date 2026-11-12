"""Mapping operations shared by several entry points (web UI, cluster peers, REST API):
validate + persist + apply, without any HTTP concerns."""
from __future__ import annotations

from typing import NamedTuple, Optional

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
        mapping.validate_input()
    # ConfigError is a ValueError; values of the wrong type are read as the right one
    # (config._read_fields), so the others only guard a check that still trips over one
    except (ValueError, TypeError, AttributeError, OverflowError) as e:
        return False, str(e)
    # Hold the lock only to mutate + persist config; release it BEFORE the
    # (potentially slow) supervisor call so other admin requests aren't blocked
    # while a serial port is being (re)opened.
    try:
        async with state.edit_config():
            if posted_id:
                existing = state.config.get_mapping(mapping.id)
                if existing is None:  # deleted while this request was being checked
                    raise ConfigError(_gone())
                state.config.mappings[state.config.mappings.index(existing)] = mapping
            else:
                state.config.mappings.append(mapping)
    except ConfigError as e:  # not saved, nothing changed (config.json unwritable too)
        return False, str(e)
    await state.supervisor.apply_mapping(mapping)
    return True, mapping.name


class ActionResult(NamedTuple):
    """What apply_action did with one mapping."""
    found: bool                          # a mapping has this id
    name: str = ""
    error: Optional[ConfigError] = None  # not done: its change can't be saved
    warning: str = ""                    # done, but config.json could not keep it
    changed: bool = False                # False: it ran (or was stopped) already


async def apply_action(state, mid, action) -> ActionResult:
    """Start/stop/restart a local mapping by id. Shared by the UI action endpoint, the
    cluster remote-control endpoint and the REST API: apply_actions for one mapping."""
    return (await apply_actions(state, [mid], action))[0]


async def apply_actions(state, ids, action) -> list[ActionResult]:
    """Start/stop/restart the mappings with these ids (once each, in this order; the
    dashboard's bulk action) with one save.

    The enabled flags are saved first (when one changes; a start or stop takes no backup
    of its own, see ConfigStore.save), then the supervisor brings each runner in line (it
    reads the config when the mapping's turn comes). Start and stop are idempotent: a
    start leaves a mapping that runs with its settings as it is, clients and serial port
    included (changed=False); restart always restarts. A start or restart that can't be
    saved (e.g. the mapping shares a serial port with a running one, or config.json can't
    be written) leaves that mapping as it was and gives `error`; the others go on. A stop
    always stops: when config.json can't be written, the mappings stay stopped (in memory
    too) and `warning` says they start again when ser2net restarts."""
    stop = action == "stop"
    ids = list(dict.fromkeys(ids))
    found: dict[str, MappingConfig] = {}
    refused: dict[str, ConfigError] = {}
    warning = ""
    try:
        async with state.edit_config(keep_on_failure=stop, backup=False):
            found = {mid: m for mid in ids if (m := state.config.get_mapping(mid)) is not None}
            flipped = [m for m in found.values() if m.enabled == stop]
            for m in flipped:
                m.enabled = not stop
            if flipped and not stop:  # starting can make a clash: find who can't
                refused = _refuse_clashes(state.config, flipped)
    except ConfigError as e:  # the save itself failed (only a change is saved)
        if not stop:
            return [ActionResult(mid in found, found[mid].name if mid in found else "",
                                 error=e) for mid in ids]
        state.log(f"WARNING: stopped {', '.join(repr(m.name) for m in found.values())}, but "
                  f"config.json could not be saved ({e}): they start again when ser2net "
                  "restarts, unless a later change is saved")
        warning = _("Stopped, but config.json could not be saved ({0}): it starts again when "
                    "ser2net restarts.").format(e)
    results = []
    for mid in ids:
        m = found.get(mid)
        if m is None:
            results.append(ActionResult(False))
            continue
        if mid in refused:
            results.append(ActionResult(True, m.name, error=refused[mid]))
            continue
        if stop:
            changed = await state.supervisor.stop_mapping(mid)
        elif action == "restart":
            await state.supervisor.restart_mapping(m)
            changed = True
        else:  # start
            _ok, _msg, changed = await state.supervisor.ensure_running(m)
        results.append(ActionResult(True, m.name, warning=warning, changed=changed))
    return results


def _refuse_clashes(config, started: list) -> dict[str, ConfigError]:
    """Mappings just switched on whose start the save would refuse (a serial port it would
    share, ...): each is switched off again, by id with the reason. One check when none
    is refused; otherwise they are switched on again one by one, in order, so the first of
    two that share a port starts."""
    try:
        config.validate_for_save()
        return {}
    except ConfigError:
        pass
    for m in started:
        m.enabled = False
    refused = {}
    for m in started:
        m.enabled = True
        try:
            config.validate_for_save()
        except ConfigError as e:
            m.enabled = False
            refused[m.id] = e
    return refused


async def delete_mapping(state, mid):
    """Remove a mapping from the config, stop it and drop its log and traces. Returns the removed
    MappingConfig, or None when no mapping has that id. Raises ConfigError when the deletion
    can't be saved (then nothing is deleted)."""
    removed = None
    async with state.edit_config():
        m = state.config.get_mapping(mid)
        if m:
            state.config.mappings.remove(m)
            removed = m
    if removed is not None:  # by the stored id, never the id from the request
        await state.supervisor.remove_mapping(removed.id)
        state.delete_mapping_log(removed.id)
        state.delete_mapping_traces(removed.id)
    return removed
