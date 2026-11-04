"""Form parsing and validation helpers for the HTML forms (mapping form, settings).

The mapping form is also rendered for a cluster peer's mapping (remote edit), so the
parsing here must tolerate a form from an older/newer node: absent fields fall back
to defaults (new mapping) or the stored values (edit, see _preserve_unmanaged_fields).
"""
from __future__ import annotations

import json
import re

from ...config import LdapSettings, MappingConfig, OidcSettings
from ...engine import netinfo


def _password_problem(pw: str, pw2: str) -> str | None:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "Passwords do not match."
    return None


def _module_present(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _ldap_from_form(form, old) -> LdapSettings:
    pw = form.get("ldap_bind_password", "")
    return LdapSettings.from_dict({
        "enabled": _checkbox(form, "ldap_enabled"),
        "server_uri": form.get("ldap_server_uri", "").strip(),
        "start_tls": _checkbox(form, "ldap_start_tls"),
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


def _username_problem(username: str) -> str | None:
    if not username:
        return "Username is required."
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", username):
        return "Username must be 1-32 characters: letters, digits, '.', '_' or '-'."
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
}


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
        "client_queue_max": _num(form, "network_client_queue_max", 2048, strict=strict),
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
    options = {
        "banner": form.get("opt_banner", ""),
        "idle_timeout_s": _num(form, "opt_idle_timeout_s", 0, strict=strict),
        "closeon": form.get("opt_closeon", ""),
        "trace_both": form.get("opt_trace_both", "").strip(),
        "trace_hexdump": _checkbox(form, "opt_trace_hexdump"),
    }
    if "opt_max_connection_time_s" in form:
        options["max_connection_time_s"] = _num(form, "opt_max_connection_time_s", 0,
                                                strict=strict)
    if "opt_trace_max_mb" in form:
        options["trace_max_mb"] = _num(form, "opt_trace_max_mb", 10.0, strict=strict, cast=float)
        options["trace_keep"] = _num(form, "opt_trace_keep", 3, strict=strict)
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
        },
        "modbus_poll": {
            "interval_s": _num(form, "modbus_poll_interval_s", 5.0, strict=False, cast=float),
            "points": _parse_modbus_points(form.get("modbus_points", ""), strict),
        },
    }


def _modbus_points_json(mapping) -> str:
    pts = [{"name": p.name, "unit": p.unit, "fn": p.fn, "address": p.address,
            "dtype": p.dtype, "scale": p.scale} for p in mapping.modbus_poll.points]
    return json.dumps(pts, indent=2) if pts else ""


def _parse_modbus_points(raw: str, strict: bool) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        points = json.loads(raw)
    except (ValueError, TypeError):
        if strict:
            raise ValueError("Modbus poll points must be a valid JSON array of objects.") from None
        return []
    return points if isinstance(points, list) else []


def _mapping_from_form(form) -> dict:
    return _build_mapping_dict(form, strict=True)


def _preserve_unmanaged_fields(new_map, existing, form=None) -> None:
    """Carry over config fields the mapping form does not expose, so editing an
    existing mapping never silently discards them: the stable-id `match`, the two
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
    for cur, old in ((new_map.serial, existing.serial),
                     (new_map.serial_b, existing.serial_b)):
        cur.match = old.match
        cur.advanced.hangup_when_done = old.advanced.hangup_when_done
        cur.advanced.nobreak = old.advanced.nobreak
    o, prev = new_map.options, existing.options
    o.openstr = prev.openstr
    o.closestr = prev.closestr
    o.trace_timestamp = prev.trace_timestamp
    o.rfc2217_poll_modem_interval_s = prev.rfc2217_poll_modem_interval_s
    o.rfc2217_net_timeout_s = prev.rfc2217_net_timeout_s
    o.modbus_response_timeout_s = prev.modbus_response_timeout_s


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner."""
    try:
        mapping = MappingConfig.from_dict(_build_mapping_dict(form, strict=False))
        # keep stored values for fields this form didn't post (else the re-rendered,
        # hidden inputs would carry defaults into the next save)
        _preserve_unmanaged_fields(mapping, state.config.get_mapping(mapping.id), form)
    except Exception:
        mapping = MappingConfig(name=form.get("name", ""))
    is_new = not form.get("id")
    return render(request, "_mapping_form.html", status_code=400,
                  mapping=mapping, is_new=is_new,
                  modbus_points_json=_modbus_points_json(mapping),
                  ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                  error=message)
