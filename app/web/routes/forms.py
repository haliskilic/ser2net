"""Form parsing and validation helpers for the HTML forms (mapping form, settings).

The mapping form is also rendered for a cluster peer's mapping (remote edit), so the
parsing here must tolerate a form from an older/newer node: absent fields fall back
to defaults (new mapping) or the stored values (edit, see _preserve_unmanaged_fields).
"""
from __future__ import annotations

import copy
import dataclasses
import difflib
import json
import math
import re

from ...config import (
    MODBUS_TIMEOUT_DEFAULT_S, REDACTED, ConfigError, LdapSettings, MappingConfig, ModbusPoint,
    OidcSettings, SerialSettings, mapping_revision, unstorable_text, valid_mapping_id,
)
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


def _field_label(name: str) -> str:
    """The mapping form's label of the number field `name`, for a message that names it."""
    side_b = name.startswith("serial_b_")
    key = name[len("serial_b_"):] if side_b else name.removeprefix("serial_")
    label = {
        "custom_baud": _("Baud rate"), "baudrate": _("Baud rate"), "bytesize": _("Data bits"),
        "stopbits": _("Stop bits"),
        "network_port": _("Listen port"), "network_remote_port": _("Remote port"),
        "network_connect_timeout_s": _("Connect timeout (s)"),
        "network_max_connections": _("Max connections"),
        "network_client_queue_max": _("Client buffer (chunks)"),
        "network_udp_peer_port": _("Reply to port"), "network_udp_peer_timeout_s": _("Peer timeout (s)"),
        "network_keepalive_idle_s": _("Keepalive idle (s)"),
        "network_keepalive_interval_s": _("Probe interval (s)"),
        "network_keepalive_count": _("Probe count"),
        "opt_idle_timeout_s": _("Idle timeout (s)"),
        "opt_max_connection_time_s": _("Max connection time (s)"),
        "opt_trace_max_mb": _("Trace file size (MB)"), "opt_trace_keep": _("Rotated trace files kept"),
        "mqtt_port": _("MQTT broker port"), "mqtt_qos": "QoS",
    }.get(key, key)
    return _("{0} (serial port B)").format(label) if side_b else label


def _num(form, name, default, *, strict, cast=int):
    """The number in the form field `name` (`default` when the form doesn't have it).
    strict: one that isn't a number is refused with a message naming the field (not
    Python's "invalid literal for int() with base 10: ''"); else it reads as `default`."""
    v = form.get(name, default)
    try:
        n = cast(v)
        if isinstance(n, float) and not math.isfinite(n):
            raise ValueError(v)
        return n
    except (TypeError, ValueError):
        if not strict:
            return default
        raise ConfigError((_("{0}: enter a whole number.") if cast is int else
                           _("{0}: enter a number.")).format(_field_label(name))) from None


def _serial_dict(form, prefix: str, strict: bool) -> dict:
    baud_field = prefix + "baudrate"
    baud = form.get(baud_field, "9600")
    if baud == "custom":
        baud_field, baud = prefix + "custom_baud", form.get(prefix + "custom_baud", "").strip()
    return {
        "port": form.get(prefix + "port", "").strip(),
        "baudrate": _num({baud_field: baud}, baud_field, 9600, strict=strict),
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


_RS485 = ("rs485_enabled", "rs485_delay_before_tx_ms", "rs485_delay_after_tx_ms",
          "rs485_rts_level_for_tx")
_SERIAL = tuple(f.name for f in dataclasses.fields(SerialSettings))

# Form fields that are not always posted: data-show-when hides AND disables a block,
# so the keepalive block isn't sent for UDP and the connect timeout only in client
# mode — and an older cluster peer's form (remote edit) doesn't know them at all. An
# absent field is left out of the parsed dict (defaults for a new mapping) and
# _preserve_unmanaged_fields keeps the stored values on edit, so switching mode back
# and forth or a mixed-version remote edit never silently resets them.
#   presence-marker form field -> (config section, attributes it governs)
# A marker must be in every form that has any of its attributes (an older one too):
# a form with the marker sets them all.
_OPTIONAL_FORM_FIELDS = {
    # serial↔serial only: the whole B side (a Serial ↔ Network mapping doesn't use it)
    "serial_b_port": ("serial_b", _SERIAL),
    # the USB-identity binding (see apply_usb_match); forms before 2.8.0 don't have it
    "serial_usb_match_shown": ("serial", ("match",)),
    "serial_b_usb_match_shown": ("serial_b", ("match",)),
    # in every form since 2.6.3
    "serial_rs485_delay_before_tx_ms": ("serial.advanced", _RS485),
    "serial_b_rs485_delay_before_tx_ms": ("serial_b.advanced", _RS485),
    # TCP server and UDP: where it listens
    "network_port": ("network", ("bind_ip", "port")),
    # TCP client: where it connects to
    "network_remote_host": ("network", ("remote_host", "remote_port")),
    # TCP server: who may connect
    "network_max_connections": ("network", ("max_connections", "kick_old_user",
                                            "priority_client_ips")),
    "network_allowed_client_ips": ("network", ("allowed_client_ips",)),   # + UDP
    # TCP (server and client)
    "network_tls_cert": ("network", ("tls", "tls_cert", "tls_key")),
    "network_keepalive_idle_s": ("network", ("tcp_keepalive", "keepalive_idle_s",
                                             "keepalive_interval_s", "keepalive_count")),
    "network_connect_timeout_s": ("network", ("connect_timeout_s",)),
    # connect-out TLS: the server certificate's CA file and pin
    "network_tls_ca": ("network", ("tls_ca",)),
    "network_tls_fingerprint": ("network", ("tls_fingerprint",)),
    # TLS listener: the CA file the clients' certificates must come from
    "network_tls_client_ca": ("network", ("tls_client_ca",)),
    "opt_max_connection_time_s": ("options", ("max_connection_time_s",)),
    # forms before 2.9.0 don't have the no-data alarm
    "opt_no_data_alarm_s": ("options", ("no_data_alarm_s", "no_data_reply_only")),
    "opt_trace_max_mb": ("options", ("trace_max_mb", "trace_keep")),
    # forms before 2.9 don't have it
    "opt_console_encoding": ("options", ("console_encoding",)),
    "mqtt_payload_format": ("mqtt", ("payload_format", "sparkplug_group_id", "sparkplug_node_id")),
    # every node since 2.3.0 reads it, but forms before 2.9.0 have no input for it
    "mqtt_client_id": ("mqtt", ("client_id",)),
    # shown for Sparkplug B only (the field is its own marker)
    "mqtt_sparkplug_primary_host_id": ("mqtt", ("sparkplug_primary_host_id",)),
    # a checkbox (absent when unticked) shown for plain-format Modbus points only
    "mqtt_apply_retained_set_shown": ("mqtt", ("apply_retained_set",)),
    # serial lines (not a Modbus gateway): the line end, and writes from <base_topic>/tx
    "mqtt_line_end": ("mqtt", ("line_end",)),
    "mqtt_tx_shown": ("mqtt", ("tx_enabled",)),
    # not a Modbus gateway: the poll string (its checkbox is absent when unticked)
    "opt_poll_interval_s": ("options", ("poll_string", "poll_interval_s", "poll_with_clients")),
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
    # shown for RFC 2217 mappings only
    "opt_rfc2217_signature": ("options", ("rfc2217_signature",)),
    # a checkbox (absent when unticked) shown for TCP raw / telnet / RFC 2217 mappings only
    "network_disconnect_without_device_shown": ("network", ("disconnect_without_device",)),
    # Modbus gateways only (before 2.9 it was in config.json only, and every edit kept it)
    "opt_modbus_response_timeout_s": ("options", ("modbus_response_timeout_s",)),
    "opt_modbus_unit_map": ("options", ("modbus_unit_map",)),
    # UDP only: where the device's output goes
    "network_udp_peer_timeout_s": ("network", ("udp_peer_host", "udp_peer_port",
                                               "udp_peer_timeout_s")),
    "network_udp_targets_shown": ("network", ("udp_targets", "udp_broadcast")),
    # the bind IP choice "All interfaces (IPv4 + IPv6)": an older node's form doesn't
    # offer it (_preserve_unmanaged_fields keeps it only while the bind IP is a wildcard)
    "network_dual_stack_shown": ("network", ("dual_stack",)),
    # checkboxes (absent when unticked) an older node's form doesn't have; a section with a
    # dot is a section of a section
    "serial_port_lock_shown": ("serial.advanced", ("tiocexcl", "respect_uucp_locks")),
    "serial_b_port_lock_shown": ("serial_b.advanced", ("tiocexcl", "respect_uucp_locks")),
    # Modbus gateways only (the points: see _keep_point_fields)
    "modbus_poll_interval_s": ("modbus_poll", ("interval_s",)),
    # the points' min / max / allowed / poll, word_order, bit and length (2.9), inside the
    # points JSON: a form without this marker (an older peer's, whose JSON can't carry them)
    # keeps each point's stored ones, by point name (see _keep_point_options)
    "modbus_point_options_shown": ("modbus_poll", ()),
    # block reads (2.9): a checkbox and the gap, shown for Modbus gateways
    "modbus_block_reads_shown": ("modbus_poll", ("block_reads", "block_gap")),
}
# the point options an older form drops (see "modbus_point_options_shown"), and what they
# apply to: one is kept only for a point it still fits (its type may have been changed)
_POINT_OPTIONS = {
    "min": lambda p: True, "max": lambda p: True, "allowed": lambda p: True,
    "poll": lambda p: True,
    "word_order": lambda p: p.dtype != "bool",
    "bit": lambda p: p.dtype == "bool" and p.fn in (3, 4),
    "length": lambda p: p.dtype == "string",
}


# Settings the mapping form doesn't have: an edit always keeps the stored values.
#   config section -> attributes
_UNMANAGED_FIELDS = {
    "serial.advanced": ("hangup_when_done", "nobreak"),
    "serial_b.advanced": ("hangup_when_done", "nobreak"),
    "options": ("openstr", "closestr", "trace_timestamp", "rfc2217_poll_modem_interval_s",
                "rfc2217_net_timeout_s"),
}

# One-line text inputs that can hold a stored value they can't show: a browser drops
# its line breaks (CR, LF) and turns a NUL into U+FFFD, so posted back unchanged it
# differs (close-on strings imported from a ser2net configuration have them).
#   form field -> (config section, attribute)
_TEXT_INPUTS = {
    "opt_banner": ("options", "banner"),
    "opt_closeon": ("options", "closeon"),
}

# What a form from before 2.8.0 (an older cluster node's) leaves out of each Modbus
# point, kept from the stored point of the same name (see _keep_point_fields).
_POINT_FIELDS_SINCE_2_8 = ("writable",)


def _section(mapping, path: str):
    for name in path.split("."):
        mapping = getattr(mapping, name)
    return mapping


# The mapping form never shows the stored MQTT password: a form with this marker posts a
# blank field to keep it, and MQTT_PASSWORD_CLEAR to remove it. A form without the marker
# (an older node's remote-edit form, an open tab from before the upgrade, a script) sets
# the password it posts, as before.
MQTT_PASSWORD_WRITE_ONLY = "mqtt_password_write_only"
MQTT_PASSWORD_CLEAR = "mqtt_password_clear"


def mqtt_password_from_form(form, stored: str) -> str:
    """The MQTT password a mapping form sets, given the stored one: the posted value, except
    that a write-only form's blank field keeps the stored password (or, with the remove box,
    clears it), and so does the REST API's REDACTED mask."""
    typed = form.get("mqtt_password", "")
    if typed == REDACTED:
        return stored
    if not typed and MQTT_PASSWORD_WRITE_ONLY in form:
        return "" if _checkbox(form, MQTT_PASSWORD_CLEAR) else stored
    return typed


def mqtt_password_view(stored: str, form=None) -> dict:
    """What _mapping_form.html shows of the MQTT password: whether one is stored, never
    the password itself; re-drawn after an error, what was typed and the remove box."""
    return {"mqtt_password_stored": bool(stored),
            "mqtt_password_typed": form.get("mqtt_password", "") if form is not None else "",
            "mqtt_password_clear": form is not None and _checkbox(form, MQTT_PASSWORD_CLEAR)}


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
    if "network_disconnect_without_device_shown" in form:
        network["disconnect_without_device"] = _checkbox(form, "network_disconnect_without_device")
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
    if "network_udp_targets_shown" in form:
        network.update(udp_targets=_split(form.get("network_udp_targets", "")),
                       udp_broadcast=_checkbox(form, "network_udp_broadcast"))
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
    if "opt_console_encoding" in form:
        options["console_encoding"] = form.get("opt_console_encoding", "").strip()
    if "opt_no_data_alarm_s" in form:
        options["no_data_alarm_s"] = _num(form, "opt_no_data_alarm_s", 0, strict=strict)
        options["no_data_reply_only"] = _checkbox(form, "opt_no_data_reply_only")
    if "opt_rfc2217_restore_on_disconnect_shown" in form:
        options["rfc2217_restore_on_disconnect"] = _checkbox(
            form, "opt_rfc2217_restore_on_disconnect")
    if "opt_rfc2217_signature" in form:
        options["rfc2217_signature"] = form.get("opt_rfc2217_signature", "").strip()
    if "opt_poll_interval_s" in form:
        options.update(
            poll_string=form.get("opt_poll_string", ""),
            poll_interval_s=(_num(form, "opt_poll_interval_s", 0.0, strict=strict, cast=float)
                             if form.get("opt_poll_interval_s") else 0.0),
            poll_with_clients=_checkbox(form, "opt_poll_with_clients"))
    if "opt_modbus_unit_map" in form:
        options["modbus_unit_map"] = _parse_unit_map(form.get("opt_modbus_unit_map", ""), strict)
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
            **({"client_id": form.get("mqtt_client_id", "").strip()}
               if "mqtt_client_id" in form else {}),
            **({"payload_format": form.get("mqtt_payload_format", "plain"),
                "sparkplug_group_id": form.get("mqtt_sparkplug_group_id", "").strip() or "ser2net",
                "sparkplug_node_id": form.get("mqtt_sparkplug_node_id", "").strip()}
               if "mqtt_payload_format" in form else {}),
            **({"sparkplug_primary_host_id": form.get("mqtt_sparkplug_primary_host_id", "").strip()}
               if "mqtt_sparkplug_primary_host_id" in form else {}),
            **({"apply_retained_set": _checkbox(form, "mqtt_apply_retained_set")}
               if "mqtt_apply_retained_set_shown" in form else {}),
            **({"line_end": form.get("mqtt_line_end", "lf")} if "mqtt_line_end" in form else {}),
            **({"tx_enabled": _checkbox(form, "mqtt_tx_enabled")}
               if "mqtt_tx_shown" in form else {}),
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
            **({"block_reads": _checkbox(form, "modbus_block_reads"),
                "block_gap": (_num(form, "modbus_block_gap", 0, strict=strict)
                              if form.get("modbus_block_gap", "").strip() else 0)}
               if "modbus_block_reads_shown" in form else {}),
        },
    }


def _modbus_points_json(mapping) -> str:
    pts = [{"name": p.name, "unit": p.unit, "fn": p.fn, "address": p.address,
            "dtype": p.dtype, "scale": p.scale, **({"writable": True} if p.writable else {}),
            # the options only when set (their defaults keep a point as before)
            **{k: getattr(p, k) for k in ("min", "max", "allowed") if getattr(p, k) is not None},
            **({"poll": p.poll} if p.poll is not True else {}),
            **({"word_order": p.word_order} if p.word_order != "ABCD" else {}),
            **{k: getattr(p, k) for k in ("bit", "length") if getattr(p, k) is not None}}
           for p in mapping.modbus_poll.points]
    return json_text(pts, indent=2) if pts else ""


def json_text(value, **kwargs) -> str:
    """JSON with non-ASCII text as it is ("Sıcaklık", not S\\u0131cakl\\u0131k), so it can be
    read and edited; escaped only if it holds what UTF-8 can't carry (an unpaired surrogate,
    which an edit refuses: see config.unstorable_text)."""
    text = json.dumps(value, ensure_ascii=False, **kwargs)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value, **kwargs)
    return text


def _keep_point_options(new_map, existing) -> None:
    """A form that can't carry the points' options (an older peer's): each point keeps the
    stored ones of the point with its name."""
    stored = {p.name: p for p in existing.modbus_poll.points}
    for p in new_map.modbus_poll.points:
        if (old := stored.get(p.name)) is not None:
            for key, fits in _POINT_OPTIONS.items():
                if fits(p):
                    setattr(p, key, getattr(old, key))


def _parse_unit_map(raw: str, strict: bool) -> dict:
    """A Modbus gateway's unit map as the form takes it: pairs such as "255=1, 0=1" (the unit
    id a master sends = the RTU unit the request goes to), separated by commas or spaces.
    ``strict``: a pair that is not one raises ValueError; else it is left out."""
    out: dict = {}
    for item in re.split(r"[\s,;]+", raw or ""):
        if not item:
            continue
        sent, eq, unit = item.partition("=")
        if eq and re.fullmatch(r"[0-9]{1,5}", sent) and re.fullmatch(r"[0-9]{1,5}", unit):
            out[str(int(sent))] = int(unit)
        elif strict:
            raise ValueError(_("The unit map is a list of pairs such as 255=1 (the unit id a "
                               "master sends = the RTU unit it goes to); {0} is not one.").format(
                                   repr(item[:40])))
    return out


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
        if unstorable_text(p) is not None:
            raise ValueError(_("Modbus poll point #{0} has a character that can't be stored: "
                               "an unpaired surrogate escape (\\ud800 to \\udfff).").format(i))
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


def in_text_box(value: str) -> str:
    """`value` as a browser sends it back from <input type=text value=...>: a NUL read as
    U+FFFD, CR and LF taken out."""
    return value.replace("\0", "\ufffd").replace("\r", "").replace("\n", "")


def apply_usb_match(mapping: MappingConfig, form, ports: list, existing=None,
                    strict: bool = True) -> None:
    """Set each serial side's stable USB identity (serial.match) from the form's
    "find this device by its USB identity" box: the selected port's VID/PID (+ serial
    number when it has one), so the engine re-finds the adapter after COMx / ttyUSBx
    renumbering. A device that is not plugged in right now keeps its stored identity.
    Raises ValueError when the box is set for a port without a USB identity (not strict:
    that side keeps its stored identity instead)."""
    for prefix, settings, old in (("serial_", mapping.serial, existing.serial if existing else None),
                                  ("serial_b_", mapping.serial_b, existing.serial_b if existing else None)):
        if f"{prefix}usb_match_shown" not in form or (prefix == "serial_b_" and mapping.kind != "serialbridge"):
            continue
        if not _checkbox(form, prefix + "usb_match"):
            settings.match = {}
            continue
        port = portlist.find(ports, settings.port, resolve=True)  # (this node's ports)
        if port is not None and port.get("vid") and port.get("pid"):
            # a binding to a USB socket too (location, set through the REST API or by hand)
            # stays one, to the socket the adapter is in now
            keys = ("vid", "pid", "serial_number") + (
                ("location",) if old is not None and old.identity.get("location") else ())
            settings.match = {k: port[k] for k in keys if port.get(k)}
        elif port is None and old is not None and old.match:
            settings.match = dict(old.match)  # unplugged now: keep what it was bound to
        elif strict:
            raise ValueError(_("{0} has no USB identity (VID/PID) to find it by; untick “Find "
                               "this device by its USB identity”.").format(settings.port or _("This port")))
        else:
            settings.match = dict(old.match) if old is not None else {}


def mapping_from_form(form, existing, ports: list) -> MappingConfig:
    """The mapping a submitted mapping form describes: parsed (a value it can't read
    raises ValueError / TypeError ...), with the stored values of `existing` (None: a new
    mapping) kept for what the form doesn't post (_preserve_unmanaged_fields) and the USB
    identity set from the form (apply_usb_match, with this node's `ports`). The save of
    the local UI and of a cluster remote edit."""
    mapping = MappingConfig.from_dict(_mapping_from_form(form))
    _preserve_unmanaged_fields(mapping, existing, form)
    apply_usb_match(mapping, form, ports, existing)
    return mapping


def mapping_for_rerender(form, existing, ports: list) -> MappingConfig:
    """The mapping to show again in a form that came back with an error (local or a
    cluster peer's): what was submitted, read leniently, with what a save would keep —
    the stored values for fields the form didn't post, the USB identity — so saving the
    form shown again never silently resets them, and the posted id, so the next save still
    edits that mapping. Points that can't be read are left out: the form shows their text
    as it was posted (_reshown_points). Never raises."""
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
        apply_usb_match(mapping, form, ports, existing, strict=False)
    except Exception:  # (stored values that don't fit the posted ones: shown as posted)
        pass
    return mapping


# Blocks of the form a node reads only from this version on: the fleet view leaves them
# out of the form for a peer running an older version, which would not keep them.
#   block (as _mapping_form.html names it) -> the first version that reads it
FORM_BLOCKS_SINCE = {
    "usb_match": (2, 8, 0),         # "Find this device by its USB identity"
    "trace_rotation": (2, 8, 0),    # trace file size / rotated files kept
    "payload_format": (2, 8, 0),    # MQTT payload format (Sparkplug B)
    "retained_set": (2, 8, 1),      # re-apply a retained /set
    "rfc2217_restore": (2, 8, 1),   # restore the serial settings after an RFC 2217 client
    "udp_reply": (2, 8, 1),         # UDP reply address and peer timeout
}


def blocks_not_read(version) -> frozenset[str]:
    """The FORM_BLOCKS_SINCE blocks a node running *version* (a (major, minor, patch);
    None: unknown) doesn't read."""
    if version is None:
        return frozenset(FORM_BLOCKS_SINCE)
    return frozenset(b for b, since in FORM_BLOCKS_SINCE.items() if version < since)


def form_context(mapping: MappingConfig, ports: list, ips: list, *, is_new: bool,
                 error=None, remote=None, hide=frozenset(), rev: str = "", links=None,
                 mqtt_stored: str = "", tls_stored: str | None = None, form=None) -> dict:
    """What _mapping_form.html is rendered with (every place that shows the form);
    `hide`: blocks the node that saves it doesn't read (blocks_not_read); `rev`: the
    stored mapping's revision (config.mapping_revision) on the node that saves it;
    `links`: the other names of the serial ports (portlist.links). The stored MQTT
    password and TLS key password are never shown, only whether there is one
    (`mqtt_stored`, `tls_stored`; None: the form goes by the mapping it shows), and after
    an error what was typed and the remove boxes of the submitted `form`
    (mqtt_password_view, tls_password_view) and its points JSON as it was posted
    (_reshown_points)."""
    out = {"mapping": mapping, "is_new": is_new,
           "modbus_points_json": (_reshown_points(form, mapping) if form is not None
                                  else _modbus_points_json(mapping)),
           "ports": portlist.visible(ports), "ips": ips, "error": error, "remote": remote,
           "hide": hide, "rev": rev, "links": links or {},
           **mqtt_password_view(mqtt_stored, form)}
    if tls_stored is not None:
        out.update(tls_password_view(tls_stored, form))
    return out


def _preserve_unmanaged_fields(new_map, existing, form=None) -> None:
    """Carry over what an edit's form did not set, so editing an existing mapping never
    silently discards it: the settings the form doesn't have (_UNMANAGED_FIELDS), the
    not-always-posted fields the submitted `form` lacks (_OPTIONAL_FORM_FIELDS; no form
    = none posted), a one-line text field posted back unchanged although it can't show
    the stored value (_TEXT_INPUTS: a banner or close-on string with a NUL, CR or LF,
    in_text_box), the stored MQTT password and TLS key password, which the form never
    shows, when it keeps them (mqtt_password_from_form; a blank key password field), the
    dual-stack choice of a form that doesn't offer it, the per-point Modbus fields a
    form from before 2.8.0 leaves out and the points' options (min, max, ...) of a form
    whose points JSON can't carry them (_keep_point_options). No-op when creating."""
    if existing is None:
        return
    posted = form is not None
    form = {} if form is None else form
    keep = [(section, attrs) for section, attrs in _UNMANAGED_FIELDS.items()]
    keep += [(section, attrs) for marker, (section, attrs) in _OPTIONAL_FORM_FIELDS.items()
             if marker not in form]
    for section, attrs in keep:
        cur, old = _section(new_map, section), _section(existing, section)
        for attr in attrs:
            setattr(cur, attr, copy.deepcopy(getattr(old, attr)))
    for field, (section, attr) in _TEXT_INPUTS.items():
        cur, old = _section(new_map, section), _section(existing, section)
        if field in form and getattr(cur, attr) == in_text_box(getattr(old, attr)):
            setattr(cur, attr, getattr(old, attr))
    if "modbus_point_options_shown" not in form and "modbus_points" in form:
        _keep_point_options(new_map, existing)
    if posted:
        new_map.mqtt.password = mqtt_password_from_form(form, existing.mqtt.password)
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
    # every 2.8.0+ form has the A side's USB-identity marker (its block is never hidden)
    if "serial_usb_match_shown" not in form:
        _keep_point_fields(new_map, existing)


def _keep_point_fields(new_map, existing) -> None:
    """A form from before 2.8.0 (an older cluster node's) lists the Modbus points without
    "writable" (_POINT_FIELDS_SINCE_2_8): each point keeps the stored value of the point
    with the same name — "writable" only while it is still a coil or holding register."""
    stored = {p.name: p for p in existing.modbus_poll.points}
    for p in new_map.modbus_poll.points:
        old = stored.get(p.name)
        if old is None:
            continue
        for attr in _POINT_FIELDS_SINCE_2_8:
            setattr(p, attr, getattr(old, attr))
        if p.fn not in (1, 3):
            p.writable = False


def tls_password_view(stored: str, form=None) -> dict:
    """What _mapping_form.html shows of a mapping's TLS key password: whether one is stored,
    never the password itself; re-drawn after an error, what was typed and the remove box
    (without these, the form goes by the mapping it shows)."""
    return {"tls_pw_stored": bool(stored),
            "tls_pw_typed": form.get("network_tls_key_password", "") if form is not None else "",
            "tls_pw_clear": form is not None and _checkbox(form, "network_tls_key_password_clear")}


def _reshown_points(form, mapping) -> str:
    """The points JSON of a refused form, to show it again: the text as it was posted (a
    typo in it is then fixed, not lost, and Save never stores the points it could not read
    as none), or the mapping's points when the form did not post them."""
    if "modbus_points" in form:
        return str(form.get("modbus_points") or "")
    return _modbus_points_json(mapping)


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner. It
    carries the mapping's current revision: the save checks the revision before anything
    else, so the error is either that it changed (a second save then replaces the
    changes, as the banner says) or about the values. A mapping deleted meanwhile is
    shown as a new one: saving it adds it again."""
    ports = state.ports.get()
    stored = state.config.get_mapping(form.get("id") or "")
    mapping = mapping_for_rerender(form, stored, ports)
    rev = mapping_revision(state.config.secret_key, stored) if stored is not None else ""
    return render(request, "_mapping_form.html", status_code=400,
                  **form_context(mapping, ports, netinfo.list_ip_candidates(),
                                 is_new=stored is None, error=message, rev=rev,
                                 links=portlist.links(portlist.visible(ports),
                                                      (mapping.serial.port, mapping.serial_b.port)),
                                 mqtt_stored=stored.mqtt.password if stored else "",
                                 tls_stored=stored.network.tls_key_password if stored else "",
                                 form=form))
