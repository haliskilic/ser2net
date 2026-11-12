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

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .. import config, importer
from ..config import REDACTED, ROLE_RANK, ConfigError, MappingConfig, valid_mapping_id
from . import proxy
from .routes import ops
from .routes.common import client_ip as _client_ip
from .routes.system import METRICS_TYPE, metrics_text

API_PREFIX = "/api/v1"
# the values a true/false query parameter may have
_BOOLEANS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


def _caller(request) -> str:
    """Who calls, for the audit log: the token's name (api-token:<name>), 'api-token' for
    the token of api_token_hash."""
    token = getattr(request.state, "api_token", None)
    return token.audit_label if token is not None else "api-token"


def _mapping_view(mapping, state, request=None) -> dict:
    """A mapping's full config plus its live runtime status. A read-only (viewer)
    token doesn't get stored secrets (the MQTT password, the TLS key password) — the
    same rule as the web UI, where viewers can't open the edit form or the export.
    Operator/admin tokens see the real value, so a GET -> PUT round trip keeps working
    for them (a PUT that sends the mask back keeps the stored TLS key password). The
    role is the calling token's (GuardMiddleware puts it on the request)."""
    view = mapping.to_dict()
    token = getattr(request.state, "api_token", None) if request is not None else None
    role = token.role if token is not None else state.config.api_token_role
    if ROLE_RANK.get(role, 0) < ROLE_RANK["operator"]:
        for section, secret in (("mqtt", "password"), ("network", "tls_key_password")):
            if view[section].get(secret):
                view[section][secret] = REDACTED
    view["status"] = state.supervisor.status(mapping.id)
    return view


async def _check_tls_files(mapping) -> None:
    """MappingConfig.check_tls_files, off the event loop (it reads the files)."""
    await asyncio.to_thread(mapping.check_tls_files)


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


def build_api_routes(state):
    async def list_mappings(request):
        return JSONResponse({"mappings": [_mapping_view(m, state, request) for m in state.config.mappings]})

    async def get_mapping(request):
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_mapping_view(m, state, request))

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
        body.pop("status", None)     # read-only field
        try:
            mapping = MappingConfig.from_dict(body)
            mapping.validate()
            mapping.validate_input()
            await _check_tls_files(mapping)
        except (ValueError, ConfigError) as e:
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        if conflict := (state.config.own_port_conflict(mapping)  # the admin UI's port, ...
                        or state.config.serial_conflict(mapping)  # a port another one uses
                        or state.config.admin_key_reuse(mapping)):
            return JSONResponse({"error": "validation", "detail": conflict}, status_code=400)
        async with state.config_lock:
            state.config.mappings.append(mapping)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings.remove(mapping)
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping created: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_create", mapping.name, user=_caller(request))
        return JSONResponse(_mapping_view(mapping, state, request), status_code=201,
                            headers={"Location": proxy.url(request,
                                                           f"{API_PREFIX}/mappings/{mapping.id}")})

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
        body.pop("status", None)
        try:
            mapping = MappingConfig.from_dict(body)
            stored = state.config.get_mapping(mid)
            if mapping.network.tls_key_password == REDACTED and stored is not None:
                mapping.network.tls_key_password = stored.network.tls_key_password
            mapping.validate()
            mapping.validate_input()
            await _check_tls_files(mapping)
        except (ValueError, ConfigError) as e:
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        if conflict := (state.config.own_port_conflict(mapping)  # the admin UI's port, ...
                        or state.config.serial_conflict(mapping)  # a port another one uses
                        or state.config.admin_key_reuse(mapping)):
            return JSONResponse({"error": "validation", "detail": conflict}, status_code=400)
        async with state.config_lock:
            existing = state.config.get_mapping(mid)
            if not existing:
                return JSONResponse({"error": "not_found"}, status_code=404)
            if mapping.mqtt.password == REDACTED:  # what a viewer token reads: keep it
                mapping.mqtt.password = existing.mqtt.password
            backup = list(state.config.mappings)
            state.config.mappings[state.config.mappings.index(existing)] = mapping
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping updated: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_update", mapping.name, user=_caller(request))
        return JSONResponse(_mapping_view(mapping, state, request))

    async def patch_mapping(request):
        """PATCH: an RFC 7396 JSON merge patch on the stored mapping. Only what the body
        names changes (an object is merged into the one it replaces, a list or a value
        replaces it, null resets a field to its default), unlike PUT, which replaces the
        whole mapping with the body. Merged and checked under the config lock, so a
        concurrent change to another field is not lost."""
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
        async with state.config_lock:
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
                mapping.validate()
                mapping.validate_input()
                await _check_tls_files(mapping)
            except (ValueError, TypeError, AttributeError) as e:  # ConfigError is a ValueError
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
            if conflict := (state.config.own_port_conflict(mapping)  # the admin UI's port, ...
                            or state.config.serial_conflict(mapping)  # a port another one uses
                            or state.config.admin_key_reuse(mapping)):
                return JSONResponse({"error": "validation", "detail": conflict}, status_code=400)
            backup = list(state.config.mappings)
            state.config.mappings[state.config.mappings.index(existing)] = mapping
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping patched: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_patch", mapping.name, user=_caller(request))
        return JSONResponse(_mapping_view(mapping, state, request))

    async def delete_mapping(request):
        mid = request.path_params["mid"]
        removed = await ops.delete_mapping(state, mid)
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
        try:
            ok, name = await ops.apply_action(state, mid, action)
        except ConfigError as e:  # it can't be started (a serial port in use, ...)
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        if not ok:
            return JSONResponse({"error": "not_found"}, status_code=404)
        state.log(f"api: mapping {action}: {name}")
        state.audit(_client_ip(request), f"api_mapping_{action}", name, user=_caller(request))
        return JSONResponse({"id": mid, "action": action, "status": state.supervisor.status(mid)})

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
            rows, _new = await ops.import_mappings(state, plan, mode)
        except ConfigError as e:
            return JSONResponse({**answer, "error": "validation", "detail": str(e),
                                 "applied": False}, status_code=400)
        count = ops.import_counts(rows)
        state.log(f"api: mappings imported ({mode}): {count['add']} added, {count['update']} "
                  f"updated, {count['same']} unchanged, {count['remove']} removed")
        state.audit(_client_ip(request), f"api_config_import_{mode}",
                    f"{len(plan.mappings)} mappings ({plan.source})", user=_caller(request))
        return JSONResponse({**answer, "rows": rows, "summary": count, "valid": True,
                             "problem": None, "applied": True})

    async def metrics(request):
        return PlainTextResponse(metrics_text(state), media_type=METRICS_TYPE)

    async def status(request):
        return JSONResponse({"mappings": state.supervisor.all_status()})

    async def ports(request):
        return JSONResponse({"ports": await state.ports.current()})

    async def health(request):
        return JSONResponse({"status": "ok", "mappings": len(state.config.mappings)})

    async def openapi(request):
        return JSONResponse(openapi_spec(state.config.admin_ui, proxy.base(request)))

    return [
        Route(f"{API_PREFIX}/health", health, methods=["GET"]),
        Route(f"{API_PREFIX}/openapi.json", openapi, methods=["GET"]),
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
    ("ModbusPoint", "fn"): (1, 2, 3, 4),
    ("ModbusPoint", "dtype"): config.MODBUS_DTYPES,
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
    origin = typing.get_origin(hint)
    if origin is list:
        (item,) = typing.get_args(hint)
        items = ({"$ref": f"#/components/schemas/{_schema_name(item)}"}
                 if dataclasses.is_dataclass(item) else {"type": _JSON_TYPES.get(item, "string")})
        out: dict = {"type": "array", "items": items}
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
        props = {f.name: _field_schema(cls, f, hints[f.name]) for f in dataclasses.fields(cls)
                 if not (cls is config.MappingConfig and f.name == "id")}  # id: below
        if cls is config.MappingConfig:
            props["id"] = {"type": "string", "readOnly": True,
                           "description": _NOTES["MappingConfig", "id"]}
            props["status"] = {"allOf": [{"$ref": "#/components/schemas/MappingStatus"}],
                               "readOnly": True, "nullable": True,
                               "description": "Live state (null: the mapping has no runner)."}
        out[name] = {"type": "object", "properties": props, "additionalProperties": True}
    out["Mapping"]["description"] = (
        "A serial<->network (kind net) or serial<->serial (kind serialbridge) mapping. In a "
        "POST or PUT, every field left out gets the default shown here (PUT replaces the "
        "whole mapping); PATCH changes only the fields it names.")
    return out


def _json(schema_ref: str, description: str) -> dict:
    return {"description": description, "content": {"application/json": {
        "schema": {"$ref": f"#/components/schemas/{schema_ref}"}}}}


def openapi_spec(admin, base: str = "") -> dict:
    """The OpenAPI 3.0 description of the REST API, written by hand (Starlette has no
    auto-generation) except for the mapping schemas (mapping_schemas). Every route in
    build_api_routes is in it (tests/test_rest_patch.py checks). `base`: the path prefix
    the request came under (app/web/proxy.py)."""
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
            "last_error": {"type": "string"}}},
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
                       "nullable": True}}},
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
    }

    def ref(name: str) -> dict:
        return {"$ref": f"#/components/responses/{name}"}

    def op(summary: str, ok: tuple, *errors: str, write: bool = False, body=None,
           description: str = "", parameters=None) -> dict:
        """One operation: its 2xx answer, the errors it can give, and the token errors
        every authenticated call can (403 for a call that changes something)."""
        resp = {ok[0]: _json(ok[1], ok[2])}
        for name in errors:
            code = {"BadRequest": "400", "NotFound": "404", "TooLarge": "413"}[name]
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
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "ser2net REST API",
            "version": "1.0.0",
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
                            "result is not valid.",
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
                "post": op("Create a mapping", ("201", "Mapping", "created (Location: its URL)"),
                           "BadRequest",
                           "TooLarge", write=True, body=mapping_body(),
                           description="Fields left out get their defaults; the server "
                                       "assigns the id."),
            },
            "/mappings/{mid}": {
                "parameters": [mid],
                "get": op("Get a mapping", ("200", "Mapping", "mapping"), "NotFound"),
                "put": op("Replace a mapping", ("200", "Mapping", "updated"), "BadRequest",
                          "NotFound", "TooLarge", write=True, body=mapping_body(),
                          description="Replaces the whole mapping: every field the body "
                                      "leaves out gets its default (listen on 0.0.0.0, no "
                                      "allow-list, not read-only, enabled, TLS/MQTT/trace "
                                      "off, no USB match, kind net). GET it first and send "
                                      "it all back changed, or use PATCH."),
                "patch": op("Change some fields of a mapping (JSON merge patch, RFC 7396)",
                            ("200", "Mapping", "updated"), "BadRequest", "NotFound",
                            "TooLarge", write=True,
                            body=mapping_body("MergePatch", ("application/merge-patch+json",
                                                             "application/json")),
                            description="Only the fields the body names change; null resets "
                                        "one to its default. A patch that makes the mapping "
                                        "invalid changes nothing."),
                "delete": op("Delete a mapping", ("200", "Deleted", "deleted"), "NotFound",
                             write=True),
            },
            "/mappings/{mid}/{action}": {
                "parameters": [
                    mid,
                    {"name": "action", "in": "path", "required": True,
                     "schema": {"type": "string", "enum": ["start", "stop", "restart"]}},
                ],
                "post": op("Start/stop/restart a mapping", ("200", "ActionResult", "action applied"),
                           "BadRequest", "NotFound", write=True,
                           description="400 (validation): it can't be started, e.g. its "
                                       "serial port is in use by another mapping."),
            },
        },
    }
