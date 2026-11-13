"""JSON REST API (``/api/v1``) for automation: Ansible, scripts, CI, fleet tools.

Separate from the HTMX endpoints under ``/api`` (which return HTML fragments for
the browser UI). Authentication is a bearer token enforced in GuardMiddleware
(``Authorization: Bearer <token>``); these handlers assume the request is already
authenticated. Every response is JSON, except ``/api/v1/metrics`` (the Prometheus text
format). The OpenAPI 3.0 description is served at ``/api/v1/openapi.json`` and
``/api/v1/health`` is unauthenticated for probes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import typing
from typing import TYPE_CHECKING, Optional

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .. import __version__, config, importer
from ..config import (REDACTED, ROLE_RANK, ConfigError, ConfigWriteError, MappingConfig,
                      mapping_revision, valid_mapping_id)
from . import proxy
from .starlette_compat import Route
from .routes import ops
from .routes.common import client_ip as _client_ip
from .routes.system import METRICS_TYPE, metrics_text

if TYPE_CHECKING:
    from ..state import AppState

API_PREFIX = "/api/v1"
# the values a true/false query parameter may have
_BOOLEANS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}

# The REST API's own version (OpenAPI info.version, GET /info) and what it offers beyond
# 1.0.0, so a client can tell what it may use without parsing the software version.
API_VERSION = "1.1.0"
API_FEATURES = ("warnings", "changed", "if-match")

def _caller(request) -> str:
    """Who calls, for the audit log: the token's name (api-token:<name>), 'api-token' for
    the token of api_token_hash."""
    token = getattr(request.state, "api_token", None)
    return token.audit_label if token is not None else "api-token"


def _mapping_view(mapping: MappingConfig, state: AppState,
                  request: Optional[Request] = None) -> dict:
    """A mapping's full config plus its live runtime status. A read-only (viewer)
    token doesn't get stored secrets (the MQTT password and client key password, the
    TLS key password) — the same rule as the web UI, where viewers can't open the edit
    form or the export. Operator/admin tokens see the real value, so a GET -> PUT round
    trip keeps working for them (a PUT that sends the mask back keeps the stored TLS key
    password). The role is the calling token's (GuardMiddleware puts it on the request)."""
    view = mapping.to_dict()
    token = getattr(request.state, "api_token", None) if request is not None else None
    role = token.role if token is not None else state.config.api_token_role
    if ROLE_RANK.get(role, 0) < ROLE_RANK["operator"]:
        for section, secret in (("mqtt", "password"), ("mqtt", "client_key_password"),
                                ("network", "tls_key_password")):
            if view[section].get(secret):
                view[section][secret] = REDACTED
    view["status"] = state.supervisor.status(mapping.id)
    return view


async def _check_tls_files(mapping) -> None:
    """MappingConfig.check_tls_files and check_mqtt_tls_files, off the event loop (they
    read the files)."""
    await asyncio.to_thread(mapping.check_tls_files)
    await asyncio.to_thread(mapping.check_mqtt_tls_files)


def merge_patch(target, patch):
    """RFC 7396 JSON merge patch: `patch` applied to `target` (neither is changed). An
    object in the patch is merged key by key into the target's (a missing or non-object
    target counts as empty), null removes a key, anything else replaces the value."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def _edit_view(mapping, state, request=None) -> dict:
    """The response to a create / update: the mapping as stored (_mapping_view), and when
    there are any, the `warnings` about what was sent (keys that are no setting, a setting
    replaced by a supported one) and about settings it has that do nothing
    (MappingConfig.ignored_settings)."""
    view = _mapping_view(mapping, state, request)
    warnings = mapping.input_warnings() + mapping.ignored_settings()
    if warnings:
        view["warnings"] = warnings
    return view


# what a mapping in a request can make from_dict / validate raise besides ConfigError:
# values of the wrong type are read as the right one (config._read_fields), so these only
# guard against a check that still trips over one
_BAD_INPUT = (ValueError, TypeError, AttributeError, OverflowError)


def _etag(state, mapping) -> str:
    """A mapping's ETag: its revision (config.mapping_revision), as a strong entity tag."""
    return f'"{mapping_revision(state.config.secret_key, mapping)}"'


def _if_match(request, state, mapping) -> bool:
    """True unless the request has an If-Match header that names neither the mapping's
    current ETag nor "*" (RFC 9110: strong comparison, so a weak tag never matches). A
    client that sends no If-Match changes the mapping whatever its revision, as before."""
    header = request.headers.get("if-match")
    if header is None:
        return True
    tags = [t.strip() for t in header.split(",")]
    return "*" in tags or _etag(state, mapping) in tags


_STALE = {"error": "precondition_failed",
          "detail": "the mapping changed since it was read: GET it again (If-Match)"}


def _not_saved(e: ConfigError) -> JSONResponse:
    """A change that was refused when saved, and so not made: 400 when the config would
    not validate (it shares a port or a serial device with another mapping, ...), 500 when
    config.json can't be written."""
    if isinstance(e, ConfigWriteError):
        return JSONResponse({"error": "save_failed", "detail": str(e)}, status_code=500)
    return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)


def build_api_routes(state: AppState) -> list[Route]:
    async def list_mappings(request):
        return JSONResponse({"mappings": [_mapping_view(m, state, request) for m in state.config.mappings]})

    async def get_mapping(request):
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_mapping_view(m, state, request), headers={"ETag": _etag(state, m)})

    async def create_mapping(request):
        try:
            body = await request.json()
        except HTTPException:  # a body over the size limit: 413
            raise
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_json", "detail": "object expected"}, status_code=400)
        body.pop("id", None)         # the server assigns the id
        try:
            mapping = MappingConfig.from_dict(body)
            mapping.validate()
            mapping.validate_input()
            await _check_tls_files(mapping)
        except _BAD_INPUT as e:  # ConfigError is a ValueError
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        try:
            async with state.edit_config():
                state.config.mappings.append(mapping)
                # next to the config as it is saved: ser2netweb's own ports, a serial port
                # another mapping uses, the admin UI's TLS key
                if conflict := ops.edit_conflict(state.config, mapping):
                    raise ConfigError(conflict)
        except ConfigError as e:  # not saved: not created either
            return _not_saved(e)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping created: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_create", mapping.name, user=_caller(request))
        return JSONResponse(_edit_view(mapping, state, request), status_code=201,
                            headers={"Location": proxy.url(request,
                                                           f"{API_PREFIX}/mappings/{mapping.id}"),
                                     "ETag": _etag(state, mapping)})

    async def update_mapping(request):
        mid = request.path_params["mid"]
        if not valid_mapping_id(mid):  # no mapping can have it (ids are checked on load)
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            body = await request.json()
        except HTTPException:  # a body over the size limit: 413
            raise
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_json", "detail": "object expected"}, status_code=400)
        body["id"] = mid             # PUT replaces the mapping with this id
        try:
            mapping = MappingConfig.from_dict(body)
            stored = state.config.get_mapping(mid)
            if mapping.network.tls_key_password == REDACTED and stored is not None:
                mapping.network.tls_key_password = stored.network.tls_key_password
            if mapping.mqtt.client_key_password == REDACTED and stored is not None:
                mapping.mqtt.client_key_password = stored.mqtt.client_key_password
            mapping.validate()
            mapping.validate_input()
            await _check_tls_files(mapping)
        except _BAD_INPUT as e:  # ConfigError is a ValueError
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        existing = None
        try:
            async with state.edit_config():
                existing = state.config.get_mapping(mid)
                if existing and not _if_match(request, state, existing):
                    raise ops.StaleRevision(mid)
                if existing and mapping.mqtt.password == REDACTED:  # what a viewer token reads
                    mapping.mqtt.password = existing.mqtt.password
                # the same settings again: nothing to save, its runner stays as it is
                if existing and existing.to_dict() != mapping.to_dict():
                    state.config.mappings[state.config.mappings.index(existing)] = mapping
                    if conflict := ops.edit_conflict(state.config, mapping):
                        raise ConfigError(conflict)
        except ConfigError as e:  # not saved: not changed either
            return _not_saved(e)
        except ops.StaleRevision:
            return JSONResponse(_STALE, status_code=412)
        if not existing:
            return JSONResponse({"error": "not_found"}, status_code=404)
        same = existing.to_dict() == mapping.to_dict()
        _ok, _msg, restarted = await state.supervisor.ensure_running(existing if same else mapping)
        state.log(f"api: mapping updated: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_update", mapping.name, user=_caller(request))
        view = _edit_view(mapping, state, request)
        view["changed"] = not same or restarted  # False: the same settings, still running
        return JSONResponse(view, headers={"ETag": _etag(state, mapping)})

    async def patch_mapping(request):
        """PATCH: an RFC 7396 JSON merge patch on the stored mapping. Only what the body
        names changes (an object is merged into the one it replaces, a list or a value
        replaces it, null resets a field to its default), unlike PUT, which replaces the
        whole mapping with the body. Merged and checked under the config lock, so a
        concurrent change to another field is not lost. A patch that changes nothing
        leaves the mapping running as it is (`changed`: false), as a PUT of its settings."""
        mid = request.path_params["mid"]
        if not valid_mapping_id(mid):  # no mapping can have it (ids are checked on load)
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            body = await request.json()
        except HTTPException:  # a body over the size limit: 413
            raise
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_json", "detail": "object expected"}, status_code=400)
        body.pop("id", None)         # the path names the mapping
        body.pop("status", None)     # read-only field
        try:
            async with state.edit_config():
                existing = state.config.get_mapping(mid)
                if not existing:
                    return JSONResponse({"error": "not_found"}, status_code=404)
                merged = merge_patch(existing.to_dict(), body)
                merged["id"] = mid
                try:
                    mapping = MappingConfig.from_dict(merged)
                    # the mask a viewer token reads, sent back: the stored secret stays
                    if mapping.mqtt.password == REDACTED:
                        mapping.mqtt.password = existing.mqtt.password
                    if mapping.network.tls_key_password == REDACTED:
                        mapping.network.tls_key_password = existing.network.tls_key_password
                    if mapping.mqtt.client_key_password == REDACTED:
                        mapping.mqtt.client_key_password = existing.mqtt.client_key_password
                    mapping.validate()
                    mapping.validate_input()
                    await _check_tls_files(mapping)
                except _BAD_INPUT as e:  # ConfigError is a ValueError
                    return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
                same = existing.to_dict() == mapping.to_dict()
                if not same:
                    state.config.mappings[state.config.mappings.index(existing)] = mapping
                    # next to the config as it is saved: ser2netweb's own ports, a serial port
                    # another mapping uses, the admin UI's TLS key
                    if conflict := ops.edit_conflict(state.config, mapping):
                        raise ConfigError(conflict)
        except ConfigError as e:  # not saved: not changed either
            return _not_saved(e)
        _ok, _msg, restarted = await state.supervisor.ensure_running(existing if same else mapping)
        state.log(f"api: mapping patched: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_patch", mapping.name, user=_caller(request))
        view = _edit_view(mapping, state, request)
        view["changed"] = not same or restarted
        return JSONResponse(view, headers={"ETag": _etag(state, mapping)})

    async def delete_mapping(request):
        mid = request.path_params["mid"]
        try:
            removed = await ops.delete_mapping(
                state, mid, expected=lambda m: _if_match(request, state, m))
        except ConfigError as e:  # not deleted
            return _not_saved(e)
        except ops.StaleRevision:
            return JSONResponse(_STALE, status_code=412)
        if removed is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        state.log(f"api: mapping deleted: {removed.name}")
        state.audit(_client_ip(request), "api_mapping_delete", removed.name, user=_caller(request))
        return JSONResponse({"deleted": mid})

    async def mapping_action(request):
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return JSONResponse({"error": "unknown_action"}, status_code=400)
        res = await ops.apply_action(state, mid, action)
        if not res.found:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if res.error is not None:  # nothing changed
            return _not_saved(res.error)
        state.log(f"api: mapping {action}: {res.name}")
        state.audit(_client_ip(request), f"api_mapping_{action}", res.name, user=_caller(request))
        out = {"id": mid, "action": action, "status": state.supervisor.status(mid),
               "changed": res.changed}
        if res.warning:  # stopped, but config.json could not keep it
            out["warning"] = res.warning
        return JSONResponse(out)

    async def import_mappings(request):
        """POST /import: many mappings in one change. The body is a mappings export
        ({"mappings": [...]}, which is also what GET /mappings answers), a whole
        config.json (only its mappings are used) or a classic ser2net.yaml / ser2net.conf.
        ?mode=merge (the default: a mapping with the same id, else the same name, is
        updated, the others added) or replace (the body's mappings replace all of them);
        ?dry_run=true answers what it would do, and why it would be refused, without
        changing anything. A real run is one save: one backup, all or nothing."""
        mode = request.query_params.get("mode", "merge")
        if mode not in importer.MODES:
            return JSONResponse({"error": "invalid_parameter",
                                 "detail": "mode must be merge or replace"}, status_code=400)
        dry = request.query_params.get("dry_run", "false").strip().lower()
        if dry not in _BOOLEANS:
            return JSONResponse({"error": "invalid_parameter",
                                 "detail": "dry_run must be true or false"}, status_code=400)
        raw = await request.body()  # over the route's size limit: 413
        try:
            # parsing is bounded, but a big file takes a moment: not on the event loop
            plan = await asyncio.to_thread(importer.parse_mappings_file, raw, "")
        except ConfigError as e:
            return JSONResponse({"error": "invalid_import", "detail": str(e)}, status_code=400)
        answer = {"mode": mode, "dry_run": _BOOLEANS[dry], "source": plan.source,
                  "count": len(plan.mappings), "warnings": plan.warnings}
        if _BOOLEANS[dry]:
            rows, problem = ops.import_preview(state, plan, mode)
            return JSONResponse({**answer, "rows": rows, "summary": ops.import_counts(rows),
                                 "valid": problem is None, "problem": problem, "applied": False})
        try:
            rows, _new, _backed_up = await ops.import_mappings(state, plan, mode)
        except ConfigError as e:
            return JSONResponse({**answer, "error": "validation", "detail": str(e),
                                 "applied": False}, status_code=400)
        count = ops.import_counts(rows)
        # as the import form logs it (the REST API is English): the warnings stay on record
        warnings = importer.log_lines(plan)
        state.log(f"api: mappings imported ({mode}) from {plan.source}: {count['add']} added, "
                  f"{count['update']} updated, {count['same']} unchanged, {count['remove']} "
                  f"removed, {len(warnings)} warnings")
        for w in warnings:
            state.log(f"api: import warning: {w}")
        state.audit(_client_ip(request), f"api_config_import_{mode}", importer.audit_detail(plan),
                    user=_caller(request))
        return JSONResponse({**answer, "rows": rows, "summary": count, "valid": True,
                             "problem": None, "applied": True})

    async def metrics(request):
        return PlainTextResponse(await metrics_text(state), media_type=METRICS_TYPE)

    async def status(request):
        return JSONResponse({"mappings": state.supervisor.all_status()})

    async def ports(request):
        return JSONResponse({"ports": await state.ports.current()})

    async def health(request):
        return JSONResponse({"status": "ok", "mappings": len(state.config.mappings)})

    async def info(request):
        return JSONResponse({"version": __version__, "api": API_VERSION,
                             "features": list(API_FEATURES)})

    async def openapi(request):
        return JSONResponse(openapi_spec(state.config.admin_ui, proxy.base(request)))

    return [
        Route(f"{API_PREFIX}/health", health, methods=["GET"]),
        Route(f"{API_PREFIX}/openapi.json", openapi, methods=["GET"]),
        Route(f"{API_PREFIX}/info", info, methods=["GET"]),
        Route(f"{API_PREFIX}/status", status, methods=["GET"]),
        Route(f"{API_PREFIX}/metrics", metrics, methods=["GET"]),
        Route(f"{API_PREFIX}/import", import_mappings, methods=["POST"],
              max_body_size=importer.MAX_FILE_BYTES),
        Route(f"{API_PREFIX}/ports", ports, methods=["GET"]),
        Route(f"{API_PREFIX}/mappings", list_mappings, methods=["GET"]),
        Route(f"{API_PREFIX}/mappings", create_mapping, methods=["POST"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", get_mapping, methods=["GET"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", update_mapping, methods=["PUT"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", patch_mapping, methods=["PATCH"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", delete_mapping, methods=["DELETE"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}/{{action}}", mapping_action, methods=["POST"]),
    ]


# ---------------------------------------------------------------------------
# OpenAPI description
# ---------------------------------------------------------------------------
# The config classes a mapping is made of, by their schema name in the spec
_MAPPING_CLASSES = {
    "Mapping": config.MappingConfig, "SerialSettings": config.SerialSettings,
    "SerialAdvanced": config.SerialAdvanced, "NetworkSettings": config.NetworkSettings,
    "MappingOptions": config.MappingOptions, "MqttSettings": config.MqttSettings,
    "ModbusPoll": config.ModbusPoll, "ModbusPoint": config.ModbusPoint,
}
# the values a field may have (what validate() accepts)
_ENUMS = {
    ("MappingConfig", "kind"): config.MAPPING_KINDS,
    ("SerialSettings", "bytesize"): config.BYTESIZES,
    ("SerialSettings", "parity"): config.PARITIES,
    ("SerialSettings", "stopbits"): config.STOPBITS,
    ("SerialSettings", "flowcontrol"): config.FLOWCONTROLS,
    ("SerialSettings", "rts_on_open"): config.LINE_STATES,
    ("SerialSettings", "dtr_on_open"): config.LINE_STATES,
    ("NetworkSettings", "mode"): config.NET_MODES,
    ("NetworkSettings", "protocol"): config.PROTOCOLS,
    ("MqttSettings", "qos"): (0, 1, 2),
    ("MqttSettings", "payload_format"): config.MQTT_FORMATS,
    ("MqttSettings", "line_end"): config.MQTT_LINE_ENDS,
    ("MqttSettings", "value_format"): config.MQTT_VALUE_FORMATS,
    ("MqttSettings", "protocol"): config.MQTT_PROTOCOLS,
    ("ModbusPoint", "fn"): (1, 2, 3, 4),
    ("ModbusPoint", "dtype"): config.MODBUS_DTYPES,
    ("ModbusPoint", "word_order"): config.MODBUS_WORD_ORDERS,
}
_NOTES = {
    ("MappingConfig", "id"): "Assigned by the server (1-64 letters, digits or _); "
                             "ignored in a request body.",
    ("MappingConfig", "enabled"): "Run the mapping. Left out of a POST or PUT: true.",
    ("MappingConfig", "serial_b"): "The second serial port of a serialbridge mapping.",
    ("MappingConfig", "network"): "The network side of a net mapping.",
    ("SerialSettings", "port"): "The serial device (COM3, /dev/ttyUSB0, ...); required. "
                                "URLs (socket://, rfc2217://, ...) are refused.",
    ("SerialSettings", "match"): "The USB identity (vid, pid, serial_number) the device is "
                                 "found by when its name changes; {} for none.",
    ("NetworkSettings", "bind_ip"): "The address a server or udp mapping listens on; "
                                    "0.0.0.0 = every interface.",
    ("NetworkSettings", "allowed_client_ips"): "The clients (IP or CIDR) that may connect; "
                                               "empty = any client.",
    ("NetworkSettings", "read_only"): "Clients only read: nothing they send reaches the device.",
    ("MqttSettings", "password"): "Masked as ******** for a viewer token; sent back so in a PUT "
                                  "or PATCH, the stored one stays.",
    ("MqttSettings", "client_key_password"): "The password of an encrypted client_key. "
                                             "Masked as ******** for a viewer token; sent back "
                                             "so in a PUT or PATCH, the stored one stays.",
    ("MqttSettings", "protocol"): "The MQTT version of the broker connection.",
    ("MqttSettings", "clean_start"): "MQTT 5: begin a new session at every connect. false "
                                     "needs a client_id; Sparkplug B always starts clean.",
    ("MqttSettings", "session_expiry_s"): "MQTT 5: seconds the broker keeps the session "
                                          "after the connection ends (0: not at all).",
    ("MqttSettings", "message_expiry_s"): "MQTT 5: seconds after which the broker stops "
                                          "handing out retained data (0: never).",
    ("ModbusPoint", "pulse_ms"): "A writable coil: a write of true from MQTT is followed by "
                                 "a write of false this many milliseconds later (50-600000); "
                                 "null: none.",
    ("ModbusPoint", "safe_value"): "With safe_after_s: the value (true / false for a coil, a "
                                   "number for a register) written to a writable point once "
                                   "the broker, or the Sparkplug primary host, has been away "
                                   "that long; null: none.",
    ("ModbusPoint", "safe_after_s"): "Seconds (1-86400) without the broker, or the Sparkplug "
                                     "primary host, after which safe_value is written; "
                                     "null: none.",
    ("NetworkSettings", "tls_key_password"): "The password of an encrypted tls_key. Masked as "
                                             "******** for a viewer token; sent back so in a "
                                             "PUT or PATCH, the stored one stays.",
}
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _field_schema(cls, f, hint) -> dict:
    """The schema of one config dataclass field: a $ref for a nested config class, else
    its JSON type with the default, the allowed values and a note where there are any."""
    if dataclasses.is_dataclass(hint):
        return {"$ref": f"#/components/schemas/{_schema_name(hint)}"}
    nullable = hint is typing.Any  # (a point's safe_value: true / false or a number)
    options = typing.get_args(hint)
    if type(None) in options and len(options) == 2:  # Optional[...]: null = not set
        hint = next(o for o in options if o is not type(None))
        nullable = True
    origin = typing.get_origin(hint)
    if nullable:
        out: dict = ({} if hint is typing.Any else {"type": "array", "items": {}}
                     if hint is list else {"type": _JSON_TYPES.get(hint, "string")})
        out["nullable"] = True
    elif origin is list:
        (item,) = typing.get_args(hint)
        items = ({"$ref": f"#/components/schemas/{_schema_name(item)}"}
                 if dataclasses.is_dataclass(item) else {"type": _JSON_TYPES.get(item, "string")})
        out = {"type": "array", "items": items}
    elif origin is dict or hint is dict:
        out = {"type": "object", "additionalProperties": True}
    else:
        out = {"type": _JSON_TYPES.get(hint, "string")}
    if f.default is not dataclasses.MISSING:
        out["default"] = f.default
    elif f.default_factory is not dataclasses.MISSING:
        out["default"] = f.default_factory()
    if (cls.__name__, f.name) in _ENUMS:
        out["enum"] = list(_ENUMS[cls.__name__, f.name])
    if (cls.__name__, f.name) in _NOTES:
        out["description"] = _NOTES[cls.__name__, f.name]
    return out


def _schema_name(cls) -> str:
    return next(name for name, c in _MAPPING_CLASSES.items() if c is cls)


def mapping_schemas() -> dict:
    """The schemas of a mapping and its parts, made from the config dataclasses, so the
    spec names every field with its type and default. Open (additionalProperties): a
    field added later must not break a client generated from an older spec."""
    out = {}
    for name, cls in _MAPPING_CLASSES.items():
        hints = typing.get_type_hints(cls)
        # id: below; input_notes: what reading it noted, never part of the JSON (to_dict)
        props = {f.name: _field_schema(cls, f, hints[f.name]) for f in dataclasses.fields(cls)
                 if not (cls is config.MappingConfig and f.name in ("id", "input_notes"))}
        if cls is config.MappingConfig:
            props["id"] = {"type": "string", "readOnly": True,
                           "description": _NOTES["MappingConfig", "id"]}
            props["warnings"] = {"type": "array", "items": {"type": "string"}, "readOnly": True,
                                 "description": "In the answer to a create / update, when "
                                                "there are any: keys that are no setting "
                                                "(ignored) and settings that do nothing."}
            props["status"] = {"allOf": [{"$ref": "#/components/schemas/MappingStatus"}],
                               "readOnly": True, "nullable": True,
                               "description": "Live state (null: the mapping has no runner)."}
        out[name] = {"type": "object", "properties": props, "additionalProperties": True}
    out["Mapping"]["description"] = (
        "A serial<->network (kind net) or serial<->serial (kind serialbridge) mapping. In a "
        "POST or PUT, every field left out gets the default shown here (PUT replaces the "
        "whole mapping); PATCH changes only the fields it names. A value may be sent as text "
        "(\"9600\", \"false\"); one that is not of the setting's type is refused (400). "
        "The MQTT topics and payloads, the Modbus point keys and the Sparkplug B model: "
        "docs/MODBUS-MQTT.md in the source tree.")
    out["MqttSettings"]["description"] = (
        "A mapping's MQTT connection: what it publishes and takes on which topic, and the "
        "Sparkplug B model, in docs/MODBUS-MQTT.md.")
    out["ModbusPoint"]["description"] = (
        "A Modbus point polled (and written) by a gateway mapping: address 0-based, the "
        "types, word orders and limits in docs/MODBUS-MQTT.md.")
    return out


def _json(schema_ref: str, description: str) -> dict:
    return {"description": description, "content": {"application/json": {
        "schema": {"$ref": f"#/components/schemas/{schema_ref}"}}}}


def openapi_spec(admin, base: str = "") -> dict:
    """The OpenAPI 3.0 description of the REST API, written by hand (Starlette has no
    auto-generation) except for the mapping schemas (mapping_schemas). Every route in
    build_api_routes is in it (tests/test_rest_patch.py checks). `base`: the path prefix
    the request came under (ser2netweb/web/proxy.py)."""
    schemas = mapping_schemas()
    schemas.update({
        "MappingStatus": {"type": "object", "additionalProperties": True, "properties": {
            "state": {"type": "string",
                      "description": "stopped, running, reconnecting, device-missing or error"},
            "device": {"type": "string"},
            "client_count": {"type": "integer"},
            "clients": {"type": "array", "items": {"type": "object", "properties": {
                "peer": {"type": "string"},
                "duration": {"type": "string", "description": "connected for (text)"},
                "connected_s": {"type": "integer", "description": "connected for (seconds)"},
                "priority": {"type": "boolean"}}}},
            "bytes_in": {"type": "integer", "description": "read from the serial device"},
            "bytes_out": {"type": "integer", "description": "written to the serial device"},
            "bytes_dropped": {"type": "integer"},
            "reconnects": {"type": "integer"},
            "dropped_clients": {"type": "integer"},
            "queue_overflows": {"type": "integer"},
            "last_error": {"type": "string"},
            "trace_error": {"type": "string",
                            "description": "why the trace is not written now (\"\": it is)"},
            "last_rx": {"type": "number", "nullable": True,
                        "description": "when the device last sent something (Unix time)"},
            "rx_idle_s": {"type": "number", "nullable": True,
                          "description": "how long the device has sent nothing"},
            "reply_wait_s": {"type": "number", "nullable": True,
                             "description": "how long what was sent to it has had no answer"},
            "mqtt": {"type": "object", "additionalProperties": True, "description": (
                "The MQTT connection while the mapping runs ({}: MQTT off): state "
                "(connecting, connected, down, off), broker, since, error, dropped, "
                "protocol (3.1.1 or 5); with MQTT 5 also reason_code (the broker's reason "
                "code for refusing or ending the connection, else null) and rejected "
                "(publications the broker refused).")},
            "safe": {"type": "object", "additionalProperties": True, "description": (
                "A Modbus gateway whose points have a safe value ({}: none): state (ok, "
                "waiting: the broker or the Sparkplug primary host is away, applied: every "
                "safe value is written), watching (broker or primary host), since, and "
                "points: name, value, after_s, written (Unix time, null: not yet), "
                "error.")}}},
        "MappingList": {"type": "object", "properties": {
            "mappings": {"type": "array", "items": {"$ref": "#/components/schemas/Mapping"}}}},
        "StatusMap": {"type": "object", "properties": {"mappings": {
            "type": "object", "description": "mapping id -> its live state",
            "additionalProperties": {"$ref": "#/components/schemas/MappingStatus"}}}},
        "PortList": {"type": "object", "properties": {"ports": {
            "type": "array", "items": {"type": "object", "additionalProperties": True}}}},
        "Health": {"type": "object", "properties": {"status": {"type": "string"},
                                                    "mappings": {"type": "integer"}}},
        "ActionResult": {"type": "object", "properties": {
            "id": {"type": "string"}, "action": {"type": "string"},
            "status": {"allOf": [{"$ref": "#/components/schemas/MappingStatus"}],
                       "nullable": True},
            "changed": {"type": "boolean",
                        "description": "false: it ran (or was stopped) already"},
            "warning": {"type": "string",
                        "description": "a stop that config.json could not keep"}}},
        "Info": {"type": "object", "properties": {
            "version": {"type": "string", "description": "ser2netweb's version"},
            "api": {"type": "string", "description": "this API's version (info.version)"},
            "features": {"type": "array", "items": {"type": "string"},
                         "description": "what it offers beyond 1.0.0"}}},
        "Deleted": {"type": "object", "properties": {"deleted": {"type": "string"}}},
        "ImportResult": {"type": "object", "additionalProperties": True, "properties": {
            "mode": {"type": "string", "enum": list(importer.MODES)},
            "dry_run": {"type": "boolean"},
            "source": {"type": "string", "description": "what the body was"},
            "count": {"type": "integer", "description": "mappings in the body"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "kind": {"type": "string"},
                "action": {"type": "string", "enum": ["add", "update", "same", "remove"]},
                "target": {"type": "string", "description": "the mapping it updates"}}}},
            "summary": {"type": "object", "additionalProperties": {"type": "integer"}},
            "valid": {"type": "boolean", "description": "the result can be saved"},
            "problem": {"type": "string", "nullable": True,
                        "description": "why the result can't be saved"},
            "applied": {"type": "boolean"}}},
        "MergePatch": {"type": "object", "additionalProperties": True, "description": (
            "An RFC 7396 JSON merge patch on a Mapping: an object merges into the stored "
            "one, a list or a value replaces it, null resets the field to its default.")},
        "Error": {"type": "object", "required": ["error"], "properties": {
            "error": {"type": "string", "description": "a machine-readable code"},
            "detail": {"type": "string", "description": "what is wrong (English)"}}},
    })
    responses = {
        "BadRequest": _json("Error", "The request is not valid: a body that is not a JSON "
                                     "object (invalid_json), a mapping or an import result "
                                     "that is not valid or a mapping that can't be started, "
                                     "e.g. on a serial port another one uses (validation), "
                                     "an unknown action "
                                     "(unknown_action) or query value (invalid_parameter), a "
                                     "body that is nothing importable (invalid_import)."),
        "Unauthorized": _json("Error", "No token, a wrong or expired one (unauthorized), or no API "
                                       "token configured (api_token_not_configured)."),
        "Forbidden": _json("Error", "The token's role may only read (forbidden)."),
        "NotFound": _json("Error", "No mapping has this id (not_found)."),
        "TooLarge": {"description": "The request body is larger than the server accepts.",
                     "content": {"text/plain": {"schema": {"type": "string"}}}},
        "SetupIncomplete": _json("Error", "The first-run setup is not done yet "
                                          "(setup_incomplete)."),
        "PreconditionFailed": _json("Error", "If-Match names another revision of the mapping "
                                             "(precondition_failed): GET it again."),
        "SaveFailed": _json("Error", "config.json could not be written (save_failed): "
                                     "nothing changed."),
    }

    def ref(name: str) -> dict:
        return {"$ref": f"#/components/responses/{name}"}

    def op(summary: str, ok: tuple, *errors: str, write: bool = False, body=None,
           description: str = "", parameters=None) -> dict:
        """One operation: its 2xx answer, the errors it can give, and the token errors
        every authenticated call can (403 for a call that changes something)."""
        resp = {ok[0]: _json(ok[1], ok[2])}
        for name in errors:
            code = {"BadRequest": "400", "NotFound": "404", "PreconditionFailed": "412",
                    "TooLarge": "413", "SaveFailed": "500"}[name]
            resp[code] = ref(name)
        resp["401"] = ref("Unauthorized")
        if write:
            resp["403"] = ref("Forbidden")
        resp["503"] = ref("SetupIncomplete")
        out = {"summary": summary, "security": bearer, "responses": resp}
        if description:
            out["description"] = description
        if body is not None:
            out["requestBody"] = body
        if parameters:
            out["parameters"] = parameters
        return out

    def mapping_body(schema: str = "Mapping", types=("application/json",)) -> dict:
        return {"required": True, "content": {
            t: {"schema": {"$ref": f"#/components/schemas/{schema}"}} for t in types}}

    bearer: list[dict[str, list[str]]] = [{"bearerAuth": []}]
    mid = {"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}}
    if_match = {"name": "If-Match", "in": "header", "required": False,
                "schema": {"type": "string"},
                "description": "The ETag a GET returned (or *): change the mapping only if it "
                               "is still at that revision, else 412."}
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "ser2netweb REST API",
            "version": API_VERSION,
            "description": "Manage serial<->network mappings, status and ports. "
                           "Authenticate with `Authorization: Bearer <token>` "
                           "(generate the token in Settings). A token with the viewer role "
                           "may only read; operator and admin tokens may also change. "
                           "Messages are in English.",
        },
        "servers": [{"url": base + API_PREFIX}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": schemas,
            "responses": responses,
        },
        "security": bearer,
        "paths": {
            "/health": {"get": {"summary": "Liveness probe (no auth)", "security": [],
                                "responses": {"200": _json("Health", "ok")}}},
            "/openapi.json": {"get": {"summary": "This description (no auth)", "security": [],
                                      "responses": {"200": {
                                          "description": "the OpenAPI 3.0 document",
                                          "content": {"application/json": {
                                              "schema": {"type": "object"}}}}}}},
            "/info": {"get": op("Software and API version, API features",
                                ("200", "Info", "version, api, features"),
                                description="`version`: ser2netweb's version; `api`: this API's "
                                            "version (info.version here); `features`: what it "
                                            "offers beyond 1.0.0 (`warnings` and `changed` in "
                                            "create / update / action answers, `if-match` on "
                                            "PUT / DELETE).")},
            "/status": {"get": op("Live status of all mappings", ("200", "StatusMap", "status map"))},
            "/ports": {"get": op("Detected serial ports", ("200", "PortList", "port list"))},
            "/metrics": {"get": {
                "summary": "Prometheus metrics (the same as /metrics in a web UI session)",
                "security": bearer,
                "responses": {"200": {"description": "text exposition format 0.0.4",
                                      "content": {"text/plain": {"schema": {"type": "string"}}}},
                              "401": ref("Unauthorized"), "503": ref("SetupIncomplete")}}},
            "/import": {"post": op(
                "Import many mappings in one change", ("200", "ImportResult", "imported, or "
                                                       "what a dry run would do"),
                "BadRequest", "TooLarge", write=True,
                description="The body: a mappings export ({\"mappings\": [...]}, also what "
                            "GET /mappings answers), a whole config.json (only its mappings) "
                            "or a classic ser2net.yaml / ser2net.conf. One save and one "
                            "backup; refused as a whole (400, nothing changed) when the "
                            "result is not valid. In merge mode, a mapping updated keeps the "
                            "settings the body doesn't have.",
                body={"required": True, "content": {
                    "application/json": {"schema": {"type": "object"}},
                    "text/plain": {"schema": {"type": "string"}}}},
                parameters=[
                    {"name": "mode", "in": "query", "schema": {
                        "type": "string", "enum": list(importer.MODES), "default": "merge"},
                     "description": "merge: a mapping with the same id, else the same name, is "
                                    "updated, the others are added; replace: the body's "
                                    "mappings replace all of them"},
                    {"name": "dry_run", "in": "query",
                     "schema": {"type": "boolean", "default": False},
                     "description": "true: answer what it would do; change nothing"}])},
            "/mappings": {
                "get": op("List mappings (config + status)", ("200", "MappingList", "mappings")),
                "post": op("Create a mapping", ("201", "Mapping", "created (Location: its URL; "
                                                                "see `warnings`; ETag header)"),
                           "BadRequest", "TooLarge", "SaveFailed", write=True, body=mapping_body(),
                           description="Fields left out get their defaults; the server "
                                       "assigns the id."),
            },
            "/mappings/{mid}": {
                "parameters": [mid],
                "get": op("Get a mapping", ("200", "Mapping", "mapping (ETag header)"), "NotFound",
                          description="The `ETag` header is the mapping's revision: send it "
                                      "back in `If-Match` with a PUT or DELETE to change it "
                                      "only if nobody changed it since."),
                "put": op("Replace a mapping", ("200", "Mapping", "updated (see `warnings`; "
                                                              "ETag header)"), "BadRequest",
                          "NotFound", "PreconditionFailed", "TooLarge", "SaveFailed", write=True,
                          body=mapping_body(), parameters=[if_match],
                          description="Replaces the whole mapping: every field the body "
                                      "leaves out gets its default (listen on 0.0.0.0, no "
                                      "allow-list, not read-only, enabled, TLS/MQTT/trace "
                                      "off, no USB match, kind net). GET it first and send "
                                      "it all back changed, or use PATCH. The same settings "
                                      "again change nothing: the mapping keeps running as it "
                                      "is (`changed`: false). Without `If-Match` the mapping "
                                      "is replaced whatever its revision."),
                "patch": op("Change some fields of a mapping (JSON merge patch, RFC 7396)",
                            ("200", "Mapping", "updated (see `warnings`; ETag header)"),
                            "BadRequest", "NotFound", "TooLarge", "SaveFailed", write=True,
                            body=mapping_body("MergePatch", ("application/merge-patch+json",
                                                             "application/json")),
                            description="Only the fields the body names change; null resets "
                                        "one to its default. A patch that makes the mapping "
                                        "invalid changes nothing."),
                "delete": op("Delete a mapping", ("200", "Deleted", "deleted"), "NotFound",
                             "PreconditionFailed", "SaveFailed", write=True,
                             parameters=[if_match]),
            },
            "/mappings/{mid}/{action}": {
                "parameters": [
                    mid,
                    {"name": "action", "in": "path", "required": True,
                     "schema": {"type": "string", "enum": ["start", "stop", "restart"]}},
                ],
                "post": op("Start/stop/restart a mapping", ("200", "ActionResult", "action applied"),
                           "BadRequest", "NotFound", "SaveFailed", write=True,
                           description="start and stop are idempotent: a start of a mapping "
                                       "that runs with its settings, or a stop of a stopped "
                                       "one, changes nothing (`changed`: false); restart "
                                       "always restarts. A start or restart that can't be "
                                       "saved changes nothing (400 validation: e.g. its "
                                       "serial port is in use by another mapping; 500 "
                                       "save_failed: config.json can't be written). A stop "
                                       "always stops; when config.json can't be written, "
                                       "`warning` says it starts again at the next start of "
                                       "ser2netweb."),
            },
        },
    }
