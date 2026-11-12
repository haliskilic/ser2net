"""Form parsing and validation helpers for the HTML forms (mapping form, settings).

The mapping form is also rendered for a cluster peer's mapping (remote edit), so the
parsing here must tolerate a form from an older/newer node: absent fields fall back
to defaults (new mapping) or the stored values (edit, see _preserve_unmanaged_fields).
"""
from __future__ import annotations

import json
import re

from ...config import LdapSettings, MappingConfig, OidcSettings
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
            **({"tiocexcl": _checkbox(form, prefix + "tiocexcl"),
                "respect_uucp_locks": _checkbox(form, prefix + "respect_uucp_locks")}
               if prefix + "port_lock_shown" in form else {}),
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
    # connect-out TLS: the server certificate's CA file and pin
    "network_tls_ca": ("network", ("tls_ca",)),
    "network_tls_fingerprint": ("network", ("tls_fingerprint",)),
    # TLS listener: the CA file the clients' certificates must come from
    "network_tls_client_ca": ("network", ("tls_client_ca",)),
    "opt_max_connection_time_s": ("options", ("max_connection_time_s",)),
    "opt_trace_max_mb": ("options", ("trace_max_mb", "trace_keep")),
    "mqtt_payload_format": ("mqtt", ("payload_format", "sparkplug_group_id", "sparkplug_node_id")),
    # a checkbox (absent when unticked) shown for plain-format Modbus points only
    "mqtt_apply_retained_set_shown": ("mqtt", ("apply_retained_set",)),
    # not offered for a Modbus gateway, which has no per-client output buffer and can't
    # send a banner or act on a close-on string (bytes outside Modbus frames)
    "network_client_queue_max": ("network", ("client_queue_max",)),
    "opt_banner": ("options", ("banner",)),
    "opt_closeon": ("options", ("closeon",)),
    # a checkbox (absent when unticked) shown for RFC 2217 mappings only
    "opt_rfc2217_restore_on_disconnect_shown": ("options", ("rfc2217_restore_on_disconnect",)),
    # UDP only: where the device's output goes
    "network_udp_peer_timeout_s": ("network", ("udp_peer_host", "udp_peer_port",
                                               "udp_peer_timeout_s")),
    # the bind IP choice "All interfaces (IPv4 + IPv6)": an older node's form doesn't
    # offer it (_preserve_unmanaged_fields keeps it only while the bind IP is a wildcard)
    "network_dual_stack_shown": ("network", ("dual_stack",)),
    # checkboxes (absent when unticked) an older node's form doesn't have; a section with a
    # dot is a section of a section
    "serial_port_lock_shown": ("serial.advanced", ("tiocexcl", "respect_uucp_locks")),
    "serial_b_port_lock_shown": ("serial_b.advanced", ("tiocexcl", "respect_uucp_locks")),
}


def _section(mapping, path: str):
    for name in path.split("."):
        mapping = getattr(mapping, name)
    return mapping


def _build_mapping_dict(form, strict: bool) -> dict:
    bind = form.get("network_bind_ip", "0.0.0.0")
    if bind == "custom":
        bind = form.get("network_bind_ip_custom", "").strip()
    both = bind == netinfo.BOTH_FAMILIES  # every interface, IPv4 and IPv6
    network = {
        "mode": form.get("network_mode", "server"),
        "protocol": form.get("network_protocol", "raw"),
        "bind_ip": "0.0.0.0" if both else bind or "0.0.0.0",
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
        # never shown in the form: blank keeps the stored one (_preserve_unmanaged_fields)
        "tls_key_password": form.get("network_tls_key_password", ""),
    }
    if "network_keepalive_idle_s" in form:
        network.update(
            tcp_keepalive=_checkbox(form, "network_tcp_keepalive"),
            keepalive_idle_s=_num(form, "network_keepalive_idle_s", 60, strict=strict),
            keepalive_interval_s=_num(form, "network_keepalive_interval_s", 10, strict=strict),
            keepalive_count=_num(form, "network_keepalive_count", 3, strict=strict),
        )
    if "network_dual_stack_shown" in form:
        network["dual_stack"] = both
    for key in ("tls_ca", "tls_fingerprint", "tls_client_ca"):
        if f"network_{key}" in form:
            network[key] = form.get(f"network_{key}", "").strip()
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
            **({"apply_retained_set": _checkbox(form, "mqtt_apply_retained_set")}
               if "mqtt_apply_retained_set_shown" in form else {}),
        },
        "modbus_poll": {
            "interval_s": _num(form, "modbus_poll_interval_s", 5.0, strict=False, cast=float),
            "points": _parse_modbus_points(form.get("modbus_points", ""), strict),
        },
    }


def _modbus_points_json(mapping) -> str:
    pts = [{"name": p.name, "unit": p.unit, "fn": p.fn, "address": p.address,
            "dtype": p.dtype, "scale": p.scale, **({"writable": True} if p.writable else {})}
           for p in mapping.modbus_poll.points]
    return json.dumps(pts, indent=2) if pts else ""


def _parse_modbus_points(raw: str, strict: bool) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        points = json.loads(raw)
    except (ValueError, TypeError):
        if strict:
            raise ValueError(_("Modbus poll points must be a valid JSON array of objects.")) from None
        return []
    return points if isinstance(points, list) else []


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
                    setattr(_section(new_map, section), attr,
                            getattr(_section(existing, section), attr))
        net = new_map.network
        if not form.get("network_tls_key_password", ""):
            # the form never shows the stored TLS key password: a blank field (or a form
            # without it: an older node's) keeps it, unless the remove box is ticked
            net.tls_key_password = ("" if _checkbox(form, "network_tls_key_password_clear")
                                    else existing.network.tls_key_password)
        if "network_dual_stack_shown" not in form:
            # an older node's form (which may list this node's choices all the same): the
            # stored setting stays while the bind IP is a wildcard
            if form.get("network_bind_ip") == netinfo.BOTH_FAMILIES:
                net.dual_stack = True
            elif not net.both_families:
                net.dual_stack = False
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
    o.modbus_response_timeout_s = prev.modbus_response_timeout_s


def tls_password_view(stored: str, form=None) -> dict:
    """What _mapping_form.html shows of a mapping's TLS key password: whether one is stored,
    never the password itself; re-drawn after an error, what was typed and the remove box
    (without these, the form goes by the mapping it shows)."""
    return {"tls_pw_stored": bool(stored),
            "tls_pw_typed": form.get("network_tls_key_password", "") if form is not None else "",
            "tls_pw_clear": form is not None and _checkbox(form, "network_tls_key_password_clear")}


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner."""
    existing = None
    try:
        mapping = MappingConfig.from_dict(_build_mapping_dict(form, strict=False))
        # keep stored values for fields this form didn't post (else the re-rendered,
        # hidden inputs would carry defaults into the next save)
        existing = state.config.get_mapping(mapping.id)
        _preserve_unmanaged_fields(mapping, existing, form)
    except Exception:
        mapping = MappingConfig(name=form.get("name", ""))
    is_new = not form.get("id")
    ports = portlist.visible(state.ports.get())
    return render(request, "_mapping_form.html", status_code=400,
                  mapping=mapping, is_new=is_new,
                  modbus_points_json=_modbus_points_json(mapping),
                  ports=ports, ips=netinfo.list_ip_candidates(),
                  links=portlist.links(ports, (mapping.serial.port, mapping.serial_b.port)),
                  error=message,
                  **tls_password_view(existing.network.tls_key_password if existing else "",
                                      form))
