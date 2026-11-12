"""Mapping operations shared by several entry points (web UI, cluster peers, REST API):
validate + persist + apply, without any HTTP concerns."""
from __future__ import annotations

from typing import NamedTuple, Optional

from ...config import ConfigError, MappingConfig, mapping_revision, valid_mapping_id
from ...i18n import _
from .forms import apply_usb_match as apply_usb_match  # where it was before 2.9.0
from .forms import mapping_from_form


def _gone() -> str:
    return _("This mapping no longer exists (it may have been deleted in the meantime). The "
             "form shows your values as a new mapping: save it to add the mapping again, or "
             "Cancel.")


def _changed() -> str:
    return _("This mapping was changed after this form was opened (by someone else, in another "
             "tab, or by a start or stop). The form shows your values: save again to replace "
             "the changes, or Cancel to keep them.")


class StaleRevision(Exception):
    """The mapping is no longer at the revision the change was made for."""


async def save_mapping_from_form(state, form) -> tuple[bool, str]:
    """Parse a mapping form, validate, persist and (re)apply. Returns (ok, name)
    on success or (False, error). Shared by the local UI save and the cluster
    remote-edit save (which runs the same form on the target node).

    The server owns mapping ids: a form without an id creates a mapping with a new
    one, a form with an id edits that mapping and is refused when there is none. A form
    with the revision it was opened at ("rev", see config.mapping_revision; forms of
    nodes before 2.9.0 have none) is refused when the mapping changed since: checked
    first, so any other refusal means it hadn't."""
    posted_id = form.get("id") or ""
    posted_rev = form.get("rev") or ""

    def stale(m) -> bool:
        return bool(posted_rev) and posted_rev != mapping_revision(state.config.secret_key, m)

    try:
        # The form doesn't expose (or always post) every field; when editing, carry
        # those over so a save never silently wipes them (get_mapping is a sync
        # read — safe outside the config lock).
        existing = state.config.get_mapping(posted_id) if valid_mapping_id(posted_id) else None
        if posted_id and existing is None:
            return False, _gone()
        if existing is not None and stale(existing):
            return False, _changed()
        mapping = mapping_from_form(form, existing, state.ports.get())
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
                if stale(existing):   # ... or changed
                    raise ConfigError(_changed())
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


async def delete_mapping(state, mid, expected=None):
    """Remove a mapping from the config, stop it and drop its log and traces. Returns the removed
    MappingConfig, or None when no mapping has that id. Raises ConfigError when the deletion
    can't be saved (then nothing is deleted), StaleRevision when `expected` (a test of the
    mapping, e.g. its revision) fails."""
    removed = None
    async with state.edit_config():
        m = state.config.get_mapping(mid)
        if m and expected is not None and not expected(m):
            raise StaleRevision(mid)
        if m:
            state.config.mappings.remove(m)
            removed = m
    if removed is not None:  # by the stored id, never the id from the request
        await state.supervisor.remove_mapping(removed.id)
        state.delete_mapping_log(removed.id)
        state.delete_mapping_traces(removed.id)
    return removed
