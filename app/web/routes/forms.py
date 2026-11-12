"""Form parsing and validation helpers for the HTML forms (mapping form, settings).

The mapping form is also rendered for a cluster peer's mapping (remote edit), so the
parsing here must tolerate a form from an older/newer node: absent fields fall back
to defaults (new mapping) or the stored values (edit, see _preserve_unmanaged_fields).
"""
from __future__ import annotations

import dataclasses
import difflib
import json
import re

from ...config import (MODBUS_TIMEOUT_DEFAULT_S, LdapSettings, MappingConfig, ModbusPoint,
                       OidcSettings, valid_mapping_id)
from ...engine import netinfo, portlist
from ...i18n import _


def _password_problem(pw: str, pw2: str) -> str | None:
    if len(pw) < 8:
        return _("Password must be at least 8 characters.")
    if pw != pw2:
        return _("Passwords do not match.")
    return None


def _ldap_from_form(form, old) -> LdapSettings:
    pw = form.get("ldap_bind_password", "")
    return LdapSettings.from_dict({
        "enabled": _checkbox(form, "ldap_enabled"),
        "server_uri": form.get("ldap_server_uri", "").strip(),
        "start_tls": _checkbox(form, "ldap_start_tls"),
        # a form without these (rendered before they existed) keeps the stored values
        "tls_verify": (not _checkbox(form, "ldap_tls_insecure")
                       if "ldap_tls_shown" in form else old.tls_verify),
        "ca_file": (form.get("ldap_ca_file", "").strip()
                    if "ldap_tls_shown" in form else old.ca_file),
        "user_dn_template": form.get("ldap_user_dn_template", "").strip(),
        "bind_dn": form.get("ldap_bind_dn", "").strip(),
        "bind_password": pw if pw else old.bind_password,   # blank => keep the stored one
        "user_search_base": form.get("ldap_user_search_base", "").strip(),
        "user_search_filter": form.get("ldap_user_search_filter", "").strip() or "(uid={username})",
        "group_attr": form.get("ldap_group_attr", "").strip() or "memberOf",
        "admin_group": form.get("ldap_admin_group", "").strip(),
        "operator_group": form.get("ldap_operator_group", "").strip(),
        "viewer_group": form.get("ldap_viewer_group", "").strip(),
        "default_role": form.get("ldap_default_role", "").strip(),
        # a form without the box (rendered before it existed) keeps the stored value
        "nested_groups": (_checkbox(form, "ldap_nested_groups")
                          if "ldap_nested_groups_shown" in form else old.nested_groups),
    })


def _oidc_from_form(form, old) -> OidcSettings:
    sec = form.get("oidc_client_secret", "")
    return OidcSettings.from_dict({
        "enabled": _checkbox(form, "oidc_enabled"),
        "issuer": form.get("oidc_issuer", "").strip(),
        "client_id": form.get("oidc_client_id", "").strip(),
        "client_secret": sec if sec else old.client_secret,   # blank => keep stored
        "redirect_uri": form.get("oidc_redirect_uri", "").strip(),
        "scopes": form.get("oidc_scopes", "").strip() or "openid email profile",
        "username_claim": form.get("oidc_username_claim", "").strip() or "preferred_username",
        "groups_claim": form.get("oidc_groups_claim", "").strip() or "groups",
        "admin_group": form.get("oidc_admin_group", "").strip(),
        "operator_group": form.get("oidc_operator_group", "").strip(),
        "viewer_group": form.get("oidc_viewer_group", "").strip(),
        "default_role": form.get("oidc_default_role", "").strip(),
    })


_USERNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _username_problem(username: str) -> str | None:
    """A local account's name: ASCII letters, digits, '.', '_' or '-' (the setup and Users
    forms check the same in the browser: pattern). Names the characters it refuses — a
    phone keyboard can put in a Turkish İ unnoticed."""
    if not username:
        return _("Username is required.")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", username):
        rule = _("Username must be 1-32 characters: English letters (a-z, A-Z), digits, '.', "
                 "'_' or '-'.")
        bad = [c for c in dict.fromkeys(username) if c not in _USERNAME_CHARS]
        if bad:
            rule += " " + _("Not allowed: {0}").format(", ".join(repr(c) for c in bad))
        return rule
    return None


def _checkbox(form, name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def _split(s: str) -> list:
    return [x for x in re.split(r"[\s,]+", s or "") if x]


def _num(form, name, default, *, strict, cast=int):
    v = form.get(name, default)
    if strict:
        return cast(v)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _serial_dict(form, prefix: str, strict: bool) -> dict:
    baud = form.get(prefix + "baudrate", "9600")
    if baud == "custom":
        baud = form.get(prefix + "custom_baud", "").strip()
    return {
        "port": form.get(prefix + "port", "").strip(),
        "baudrate": (int(baud) if strict else _num({"b": baud}, "b", 9600, strict=False)),
        "bytesize": _num(form, prefix + "bytesize", 8, strict=strict),
        "parity": form.get(prefix + "parity", "N"),
        "stopbits": _num(form, prefix + "stopbits", 1, strict=strict, cast=float),
        "flowcontrol": form.get(prefix + "flowcontrol", "none"),
        "rts_on_open": form.get(prefix + "rts_on_open", "keep"),
        "dtr_on_open": form.get(prefix + "dtr_on_open", "keep"),
        "exclusive": _checkbox(form, prefix + "exclusive"),
        "advanced": {
            "rs485_enabled": _checkbox(form, prefix + "rs485_enabled"),
            "rs485_delay_before_tx_ms": _num(form, prefix + "rs485_delay_before_tx_ms",
                                             0.0, strict=False, cast=float),
            "rs485_delay_after_tx_ms": _num(form, prefix + "rs485_delay_after_tx_ms",
                                            0.0, strict=False, cast=float),
            "rs485_rts_level_for_tx": _checkbox(form, prefix + "rs485_rts_level_for_tx"),
        },
    }


# Form fields that are not always posted: data-show-when hides AND disables a block,
# so the keepalive block isn't sent for UDP and the connect timeout only in client
# mode — and an older cluster peer's form (remote edit) doesn't know them at all. An
# absent field is left out of the parsed dict (defaults for a new mapping) and
# _preserve_unmanaged_fields keeps the stored values on edit, so switching mode back
# and forth or a mixed-version remote edit never silently resets them.
#   presence-marker form field -> (config section, attributes it governs)
_OPTIONAL_FORM_FIELDS = {
    "network_keepalive_idle_s": ("network", ("tcp_keepalive", "keepalive_idle_s",
                                             "keepalive_interval_s", "keepalive_count")),
    "network_connect_timeout_s": ("network", ("connect_timeout_s",)),
    "opt_max_connection_time_s": ("options", ("max_connection_time_s",)),
    "opt_trace_max_mb": ("options", ("trace_max_mb", "trace_keep")),
    "mqtt_payload_format": ("mqtt", ("payload_format", "sparkplug_group_id", "sparkplug_node_id")),
    # shown for Sparkplug B only (the field is its own marker)
    "mqtt_sparkplug_primary_host_id": ("mqtt", ("sparkplug_primary_host_id",)),
    # a checkbox (absent when unticked) shown for plain-format Modbus points only
    "mqtt_apply_retained_set_shown": ("mqtt", ("apply_retained_set",)),
    "mqtt_unit_status_shown": ("mqtt", ("unit_status",)),
    # plain format: retained data (a checkbox shown for plain-format mappings) and the
    # Modbus value format (shown for plain-format points; the select is its own marker)
    "mqtt_retain_shown": ("mqtt", ("retain",)),
    "mqtt_value_format": ("mqtt", ("value_format",)),
    # MQTT over TLS: CA file, client certificate (mutual TLS), host name check (2.9)
    "mqtt_ca_file": ("mqtt", ("ca_file", "client_cert", "client_key", "client_key_password",
                              "tls_check_hostname")),
    # not offered for a Modbus gateway, which has no per-client output buffer and can't
    # send a banner or act on a close-on string (bytes outside Modbus frames)
    "network_client_queue_max": ("network", ("client_queue_max",)),
    "opt_banner": ("options", ("banner",)),
    "opt_closeon": ("options", ("closeon",)),
    # a checkbox (absent when unticked) shown for RFC 2217 mappings only
    "opt_rfc2217_restore_on_disconnect_shown": ("options", ("rfc2217_restore_on_disconnect",)),
    # Modbus gateways only (before 2.9 it was in config.json only)
    "opt_modbus_response_timeout_s": ("options", ("modbus_response_timeout_s",)),
    # UDP only: where the device's output goes
    "network_udp_peer_timeout_s": ("network", ("udp_peer_host", "udp_peer_port",
                                               "udp_peer_timeout_s")),
    # the points' min / max / allowed / poll (2.9), inside the points JSON: a form without
    # this marker (an older peer's, whose JSON can't carry them) keeps each point's stored
    # ones, by point name (see _keep_point_options)
    "modbus_point_options_shown": ("modbus_poll", ()),
}
# the point options an older form drops (see "modbus_point_options_shown")
_POINT_OPTIONS = ("min", "max", "allowed", "poll")


def _build_mapping_dict(form, strict: bool) -> dict:
    bind = form.get("network_bind_ip", "0.0.0.0")
    if bind == "custom":
        bind = form.get("network_bind_ip_custom", "").strip()
    network = {
        "mode": form.get("network_mode", "server"),
        "protocol": form.get("network_protocol", "raw"),
        "bind_ip": bind or "0.0.0.0",
        "port": _num(form, "network_port", 4001, strict=strict),
        "remote_host": form.get("network_remote_host", "").strip(),
        "remote_port": (_num(form, "network_remote_port", 0, strict=strict)
                        if form.get("network_remote_port") else 0),
        "max_connections": _num(form, "network_max_connections", 1, strict=strict),
        "kick_old_user": _checkbox(form, "network_kick_old_user"),
        "read_only": _checkbox(form, "network_read_only"),
        "allowed_client_ips": _split(form.get("network_allowed_client_ips", "")),
        "priority_client_ips": _split(form.get("network_priority_client_ips", "")),
        "tls": _checkbox(form, "network_tls"),
        "tls_cert": form.get("network_tls_cert", "").strip(),
        "tls_key": form.get("network_tls_key", "").strip(),
    }
    if "network_keepalive_idle_s" in form:
        network.update(
            tcp_keepalive=_checkbox(form, "network_tcp_keepalive"),
            keepalive_idle_s=_num(form, "network_keepalive_idle_s", 60, strict=strict),
            keepalive_interval_s=_num(form, "network_keepalive_interval_s", 10, strict=strict),
            keepalive_count=_num(form, "network_keepalive_count", 3, strict=strict),
        )
    if "network_connect_timeout_s" in form:
        network["connect_timeout_s"] = _num(form, "network_connect_timeout_s", 10.0,
                                            strict=strict, cast=float)
    if "network_client_queue_max" in form:
        network["client_queue_max"] = _num(form, "network_client_queue_max", 2048, strict=strict)
    if "network_udp_peer_timeout_s" in form:
        network.update(
            udp_peer_host=form.get("network_udp_peer_host", "").strip(),
            udp_peer_port=(_num(form, "network_udp_peer_port", 0, strict=strict)
                           if form.get("network_udp_peer_port") else 0),
            udp_peer_timeout_s=(_num(form, "network_udp_peer_timeout_s", 0, strict=strict)
                                if form.get("network_udp_peer_timeout_s") else 0),
        )
    options = {
        "idle_timeout_s": _num(form, "opt_idle_timeout_s", 0, strict=strict),
        "trace_both": form.get("opt_trace_both", "").strip(),
        "trace_hexdump": _checkbox(form, "opt_trace_hexdump"),
    }
    for key in ("banner", "closeon"):
        if f"opt_{key}" in form:
            options[key] = form.get(f"opt_{key}", "")
    if "opt_max_connection_time_s" in form:
        options["max_connection_time_s"] = _num(form, "opt_max_connection_time_s", 0,
                                                strict=strict)
    if "opt_trace_max_mb" in form:
        options["trace_max_mb"] = _num(form, "opt_trace_max_mb", 10.0, strict=strict, cast=float)
        options["trace_keep"] = _num(form, "opt_trace_keep", 3, strict=strict)
    if "opt_rfc2217_restore_on_disconnect_shown" in form:
        options["rfc2217_restore_on_disconnect"] = _checkbox(
            form, "opt_rfc2217_restore_on_disconnect")
    if "opt_modbus_response_timeout_s" in form:  # blank: the default
        options["modbus_response_timeout_s"] = (
            _num(form, "opt_modbus_response_timeout_s", MODBUS_TIMEOUT_DEFAULT_S,
                 strict=strict, cast=float)
            if form.get("opt_modbus_response_timeout_s", "").strip() else MODBUS_TIMEOUT_DEFAULT_S)
    return {
        "id": form.get("id") or None,
        "name": (form.get("name", "").strip() if strict else form.get("name", "")),
        "enabled": _checkbox(form, "enabled"),
        "kind": form.get("kind", "net"),
        "serial": _serial_dict(form, "serial_", strict),
        "serial_b": _serial_dict(form, "serial_b_", strict),
        "network": network,
        "options": options,
        "mqtt": {
            "enabled": _checkbox(form, "mqtt_enabled"),
            "host": form.get("mqtt_host", "").strip(),
            "port": _num(form, "mqtt_port", 1883, strict=strict),
            "base_topic": form.get("mqtt_base_topic", "").strip(),
            "qos": _num(form, "mqtt_qos", 0, strict=strict),
            "tls": _checkbox(form, "mqtt_tls"),
            "username": form.get("mqtt_username", "").strip(),
            "password": form.get("mqtt_password", ""),
            "client_id": form.get("mqtt_client_id", "").strip(),
            **({"payload_format": form.get("mqtt_payload_format", "plain"),
                "sparkplug_group_id": form.get("mqtt_sparkplug_group_id", "").strip() or "ser2net",
                "sparkplug_node_id": form.get("mqtt_sparkplug_node_id", "").strip()}
               if "mqtt_payload_format" in form else {}),
            **({"sparkplug_primary_host_id": form.get("mqtt_sparkplug_primary_host_id", "").strip()}
               if "mqtt_sparkplug_primary_host_id" in form else {}),
            **({"apply_retained_set": _checkbox(form, "mqtt_apply_retained_set")}
               if "mqtt_apply_retained_set_shown" in form else {}),
            **({"unit_status": _checkbox(form, "mqtt_unit_status")}
               if "mqtt_unit_status_shown" in form else {}),
            **({"retain": _checkbox(form, "mqtt_retain")}
               if "mqtt_retain_shown" in form else {}),
            **({"value_format": form.get("mqtt_value_format", "text")}
               if "mqtt_value_format" in form else {}),
            **({"ca_file": form.get("mqtt_ca_file", "").strip(),
                "client_cert": form.get("mqtt_client_cert", "").strip(),
                "client_key": form.get("mqtt_client_key", "").strip(),
                "client_key_password": form.get("mqtt_client_key_password", ""),
                "tls_check_hostname": _checkbox(form, "mqtt_tls_check_hostname")}
               if "mqtt_ca_file" in form else {}),
        },
        "modbus_poll": {
            "interval_s": _num(form, "modbus_poll_interval_s", 5.0, strict=False, cast=float),
            "points": _parse_modbus_points(form.get("modbus_points", ""), strict),
        },
    }


def _modbus_points_json(mapping) -> str:
    pts = [{"name": p.name, "unit": p.unit, "fn": p.fn, "address": p.address,
            "dtype": p.dtype, "scale": p.scale, **({"writable": True} if p.writable else {}),
            # the options only when set (their defaults keep a point as before)
            **{k: getattr(p, k) for k in ("min", "max", "allowed") if getattr(p, k) is not None},
            **({"poll": p.poll} if p.poll is not True else {})}
           for p in mapping.modbus_poll.points]
    return json.dumps(pts, indent=2) if pts else ""


def _keep_point_options(new_map, existing) -> None:
    """A form that can't carry the points' options (an older peer's): each point keeps the
    stored ones of the point with its name."""
    stored = {p.name: p for p in existing.modbus_poll.points}
    for p in new_map.modbus_poll.points:
        if (old := stored.get(p.name)) is not None:
            for key in _POINT_OPTIONS:
                setattr(p, key, getattr(old, key))


_POINT_KEYS = tuple(f.name for f in dataclasses.fields(ModbusPoint))


def _parse_modbus_points(raw: str, strict: bool) -> list:
    """The points JSON of a mapping form: a list of point objects (one object alone is a list
    of one; blank: no points). ``strict`` (a save): anything else raises ValueError saying
    what is wrong — where the JSON breaks (line and column), which point is not an object,
    a key that looks like a misspelt one ("adress"; other unknown keys are left alone, as
    everywhere else: a newer node's). Not strict (showing a refused form again): what can't
    be read is left out."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        points = json.loads(raw)
    except json.JSONDecodeError as e:
        if strict:
            raise ValueError(_("Modbus poll points: the JSON is not valid at line {0}, column "
                               "{1}: {2}.").format(e.lineno, e.colno, e.msg)) from None
        return []
    except (ValueError, RecursionError):  # (nested too deeply)
        if strict:
            raise ValueError(_("Modbus poll points must be a valid JSON array of objects.")) from None
        return []
    if isinstance(points, dict):
        points = [points]
    if not isinstance(points, list):
        if strict:
            raise ValueError(_("Modbus poll points must be a valid JSON array of objects."))
        return []
    if not strict:
        return [p for p in points if isinstance(p, dict)]
    for i, p in enumerate(points, 1):
        if not isinstance(p, dict):
            raise ValueError(_("Modbus poll point #{0} is not an object.").format(i))
        for key in p:
            if key in _POINT_KEYS:
                continue
            near = difflib.get_close_matches(str(key), _POINT_KEYS, n=1, cutoff=0.7)
            if near:
                raise ValueError(_("Modbus poll point #{0} ({1}): unknown key {2}. Did you "
                                   "mean {3}?").format(i, repr(p.get("name"))[:40],
                                                      json.dumps(key), json.dumps(near[0])))
    return points


def _mapping_from_form(form) -> dict:
    return _build_mapping_dict(form, strict=True)


def _preserve_unmanaged_fields(new_map, existing, form=None) -> None:
    """Carry over config fields the mapping form does not expose, so editing an
    existing mapping never silently discards them: the stable-id `match` (when the
    form has no USB-identity field), the two
    advanced serial flags the form omits (`hangup_when_done`, `nobreak`), open/close
    strings, the trace-timestamp flag and the RFC2217 interop knobs. The RS-485 fields
    ARE in the form now, so they're taken from the submission. Given the submitted
    `form`, the not-always-posted fields (_OPTIONAL_FORM_FIELDS) are kept too when
    absent. No-op when creating."""
    if existing is None:
        return
    if form is not None:
        for marker, (section, attrs) in _OPTIONAL_FORM_FIELDS.items():
            if marker not in form:
                for attr in attrs:
                    setattr(getattr(new_map, section), attr,
                            getattr(getattr(existing, section), attr))
        if "modbus_point_options_shown" not in form and "modbus_points" in form:
            _keep_point_options(new_map, existing)
    for prefix, cur, old in (("serial_", new_map.serial, existing.serial),
                             ("serial_b_", new_map.serial_b, existing.serial_b)):
        # the USB-identity binding is in the form now (ops.apply_usb_match); a form
        # without it (an older cluster peer's) keeps the stored one
        if form is None or f"{prefix}usb_match_shown" not in form:
            cur.match = old.match
        cur.advanced.hangup_when_done = old.advanced.hangup_when_done
        cur.advanced.nobreak = old.advanced.nobreak
    o, prev = new_map.options, existing.options
    o.openstr = prev.openstr
    o.closestr = prev.closestr
    o.trace_timestamp = prev.trace_timestamp
    o.rfc2217_poll_modem_interval_s = prev.rfc2217_poll_modem_interval_s
    o.rfc2217_net_timeout_s = prev.rfc2217_net_timeout_s


def _reshown_mapping(form, existing) -> MappingConfig:
    """The mapping a refused form showed, to show it again with the error: the posted values
    (read leniently), the stored ones (``existing``: the stored mapping, or None) for the
    fields the form did not post — else the hidden inputs would carry defaults into the next
    save — and the posted id, so the next save still edits that mapping. Points that can't
    be read are left out: the form shows their text as it was posted (_reshown_points)."""
    posted_id = form.get("id") or ""
    try:
        d = _build_mapping_dict(form, strict=False)
        try:
            mapping = MappingConfig.from_dict(d)
        except Exception:  # (a point that is not one): the rest of the form still counts
            d["modbus_poll"]["points"] = []
            mapping = MappingConfig.from_dict(d)
    except Exception:
        mapping = MappingConfig(name=str(form.get("name", "")))
    if valid_mapping_id(posted_id):
        mapping.id = posted_id
    try:
        _preserve_unmanaged_fields(mapping, existing, form)
    except Exception:  # (stored values that don't fit the posted ones: shown as posted)
        pass
    return mapping


def _reshown_points(form, mapping) -> str:
    """The points JSON of a refused form, to show it again: the text as it was posted (a
    typo in it is then fixed, not lost, and Save never stores the points it could not read
    as none), or the mapping's points when the form did not post them."""
    if "modbus_points" in form:
        return str(form.get("modbus_points") or "")
    return _modbus_points_json(mapping)


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner."""
    posted_id = form.get("id") or ""
    existing = state.config.get_mapping(posted_id) if valid_mapping_id(posted_id) else None
    mapping = _reshown_mapping(form, existing)
    is_new = not posted_id
    return render(request, "_mapping_form.html", status_code=400,
                  mapping=mapping, is_new=is_new,
                  modbus_points_json=_reshown_points(form, mapping),
                  ports=portlist.visible(state.ports.get()), ips=netinfo.list_ip_candidates(),
                  error=message)
