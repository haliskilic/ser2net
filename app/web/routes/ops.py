"""Mapping operations shared by several entry points (web UI, cluster peers, REST API):
validate + persist + apply, without any HTTP concerns."""
from __future__ import annotations

import itertools

from ...config import (ConfigError, MappingConfig, _ip_overlaps, check_mqtt_clashes,
                       sparkplug_identity, valid_mapping_id)
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
        mapping.check_mqtt_tls_files()
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
        try:  # (against the mappings as they are now, under the lock)
            check_mqtt_clashes(mapping, state.config.mappings)
        except ConfigError as e:
            return False, str(e)
        if posted_id:
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


def copy_of(config, m: MappingConfig) -> MappingConfig:
    """The dashboard's Copy: ``m``'s settings under a new id, disabled, and nothing in it
    that clashes with ``m`` (or another mapping) when it is enabled next to it, once its
    serial port is changed: the name "<name> (copy)" ("(copy 2)", … when taken), the next
    free listening port for its transport and address (searched down from ``m``'s once
    65535 is reached), no MQTT client ID (a broker keeps one connection per ID), the base
    topic "<topic>-copy" ("-copy-2", …) so a /set is not written by both, and a Sparkplug
    B edge node ID made from the new name. Raises ConfigError when no port is free."""
    d = m.to_dict()
    d.pop("id", None)
    d["enabled"] = False  # don't auto-start a duplicate
    dup = MappingConfig.from_dict(d)
    names = {x.name for x in config.mappings}
    dup.name, k = f"{m.name} (copy)", 1
    while dup.name in names:
        k += 1
        dup.name = f"{m.name} (copy {k})"
    mq = dup.mqtt
    mq.client_id = ""
    base = str(mq.base_topic).rstrip("/")
    if base:
        topics = {str(x.mqtt.base_topic).rstrip("/") for x in config.mappings}
        mq.base_topic, k = f"{base}-copy", 1
        while mq.base_topic in topics:
            k += 1
            mq.base_topic = f"{base}-copy-{k}"
    mq.sparkplug_node_id = ""  # made from the name, unless another mapping has that one
    if mq.payload_format == "sparkplug_b":
        nodes = {sparkplug_identity(x) for x in config.mappings
                 if x.mqtt.payload_format == "sparkplug_b"}
        group, node = sparkplug_identity(dup)
        k = 1
        while (group, mq.sparkplug_node_id or node) in nodes:
            k += 1
            mq.sparkplug_node_id = f"{node}_{k}"
    net = dup.network
    if dup.kind == "net" and net.listens:
        proto = "udp" if net.mode == "udp" else "tcp"
        taken = {x.network.port for x in config.mappings
                 if x.kind == "net" and x.network.listens
                 and ("udp" if x.network.mode == "udp" else "tcp") == proto
                 and _ip_overlaps(x.network.bind_ip, net.bind_ip)}
        if net.port in taken:
            free = (p for p in itertools.chain(range(max(net.port + 1, 1), 65536),
                                               range(min(net.port - 1, 65535), 0, -1))
                    if p not in taken)
            port = next(free, None)
            if port is None:
                raise ConfigError(_("No free {0} port is left on {1} for the copy.").format(
                    proto.upper(), net.bind_ip))
            net.port = port
    return dup


async def apply_action(state, mid, action) -> tuple[bool, str]:
    """Start/stop/restart a local mapping by id. Returns (ok, name-or-error).
    Shared by the UI action endpoint, the cluster remote-control endpoint and the
    REST API."""
    async with state.config_lock:
        m = state.config.get_mapping(mid)
        if not m:
            return False, "Mapping not found"
        was = m.enabled
        m.enabled = action != "stop"
        try:
            await state.asave()
        except Exception:  # not saved, so nothing changed (e.g. starting it shares a serial port)
            m.enabled = was
            raise
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
            index = state.config.mappings.index(m)
            state.config.mappings.remove(m)
            try:
                await state.asave()
            except Exception:  # not saved, so not deleted
                state.config.mappings.insert(index, m)
                raise
            removed = m
    if removed is not None:  # by the stored id, never the id from the request
        await state.supervisor.remove_mapping(removed.id)
        state.delete_mapping_log(removed.id)
        state.delete_mapping_traces(removed.id)
    return removed
