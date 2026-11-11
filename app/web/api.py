"""JSON REST API (``/api/v1``) for automation: Ansible, scripts, CI, fleet tools.

Separate from the HTMX endpoints under ``/api`` (which return HTML fragments for
the browser UI). Authentication is a bearer token enforced in GuardMiddleware
(``Authorization: Bearer <token>``); these handlers assume the request is already
authenticated. Every response is JSON. The OpenAPI 3.0 description is served at
``/api/v1/openapi.json`` and ``/api/v1/health`` is unauthenticated for probes.
"""
from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..config import ROLE_RANK, ConfigError, MappingConfig, valid_mapping_id
from .routes import ops
from .routes.common import client_ip as _client_ip

API_PREFIX = "/api/v1"

REDACTED = "********"


def _mapping_view(mapping, state) -> dict:
    """A mapping's full config plus its live runtime status. A read-only (viewer)
    token doesn't get stored secrets (the MQTT password) — the same rule as the web
    UI, where viewers can't open the edit form or the export. Operator/admin tokens
    see the real value, so a GET -> PUT round trip keeps working for them."""
    view = mapping.to_dict()
    if (ROLE_RANK.get(state.config.api_token_role, 0) < ROLE_RANK["operator"]
            and view["mqtt"].get("password")):
        view["mqtt"]["password"] = REDACTED
    view["status"] = state.supervisor.status(mapping.id)
    return view


def _edit_view(mapping, state) -> dict:
    """The response to a create / update: the mapping as stored, and when there are any,
    the `warnings` about what was sent (keys that are no setting, a setting replaced by a
    supported one)."""
    view = _mapping_view(mapping, state)
    warnings = mapping.input_warnings()
    if warnings:
        view["warnings"] = warnings
    return view


# what a mapping in a request can make from_dict / validate raise besides ConfigError:
# values of the wrong type are read as the right one (config._read_fields), so these only
# guard against a check that still trips over one
_BAD_INPUT = (ValueError, TypeError, AttributeError, OverflowError)


def build_api_routes(state):
    async def list_mappings(request):
        return JSONResponse({"mappings": [_mapping_view(m, state) for m in state.config.mappings]})

    async def get_mapping(request):
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(_mapping_view(m, state))

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
        except _BAD_INPUT as e:  # ConfigError is a ValueError
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        async with state.config_lock:
            state.config.mappings.append(mapping)
            try:
                await state.asave()
            except Exception as e:  # not saved: not created either
                state.config.mappings.remove(mapping)
                if not isinstance(e, ConfigError):
                    raise
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping created: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_create", mapping.name, user="api-token")
        return JSONResponse(_edit_view(mapping, state), status_code=201)

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
            mapping.validate()
            mapping.validate_input()
        except _BAD_INPUT as e:  # ConfigError is a ValueError
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        async with state.config_lock:
            existing = state.config.get_mapping(mid)
            if not existing:
                return JSONResponse({"error": "not_found"}, status_code=404)
            backup = list(state.config.mappings)
            state.config.mappings[state.config.mappings.index(existing)] = mapping
            try:
                await state.asave()
            except Exception as e:  # not saved: not changed either
                state.config.mappings = backup
                if not isinstance(e, ConfigError):
                    raise
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping updated: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_update", mapping.name, user="api-token")
        return JSONResponse(_edit_view(mapping, state))

    async def delete_mapping(request):
        mid = request.path_params["mid"]
        removed = await ops.delete_mapping(state, mid)
        if removed is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        state.log(f"api: mapping deleted: {removed.name}")
        state.audit(_client_ip(request), "api_mapping_delete", removed.name, user="api-token")
        return JSONResponse({"deleted": mid})

    async def mapping_action(request):
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return JSONResponse({"error": "unknown_action"}, status_code=400)
        ok, name = await ops.apply_action(state, mid, action)
        if not ok:
            return JSONResponse({"error": "not_found"}, status_code=404)
        state.log(f"api: mapping {action}: {name}")
        state.audit(_client_ip(request), f"api_mapping_{action}", name, user="api-token")
        return JSONResponse({"id": mid, "action": action, "status": state.supervisor.status(mid)})

    async def status(request):
        return JSONResponse({"mappings": state.supervisor.all_status()})

    async def ports(request):
        return JSONResponse({"ports": state.ports.get()})

    async def health(request):
        return JSONResponse({"status": "ok", "mappings": len(state.config.mappings)})

    async def openapi(request):
        return JSONResponse(openapi_spec(state.config.admin_ui))

    return [
        Route(f"{API_PREFIX}/health", health, methods=["GET"]),
        Route(f"{API_PREFIX}/openapi.json", openapi, methods=["GET"]),
        Route(f"{API_PREFIX}/status", status, methods=["GET"]),
        Route(f"{API_PREFIX}/ports", ports, methods=["GET"]),
        Route(f"{API_PREFIX}/mappings", list_mappings, methods=["GET"]),
        Route(f"{API_PREFIX}/mappings", create_mapping, methods=["POST"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", get_mapping, methods=["GET"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", update_mapping, methods=["PUT"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}", delete_mapping, methods=["DELETE"]),
        Route(f"{API_PREFIX}/mappings/{{mid}}/{{action}}", mapping_action, methods=["POST"]),
    ]


def openapi_spec(admin) -> dict:
    """Hand-written OpenAPI 3.0 description of the REST API (Starlette has no
    auto-generation). Kept deliberately compact: mappings are an open object so
    the spec doesn't have to mirror every serial/network field."""
    mapping_obj = {"type": "object", "description": (
        "A serial<->network mapping (see config model). A value may be sent as text "
        "(\"9600\", \"false\"); one that is not of the setting's type is refused (400). "
        "Responses to a create / update list in `warnings` the keys that are no setting "
        "(ignored) and settings that do nothing.")}
    error_obj = {"type": "object", "properties": {"error": {"type": "string"},
                                                  "detail": {"type": "string"}}}
    bearer: list[dict[str, list[str]]] = [{"bearerAuth": []}]
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "ser2net REST API",
            "version": "1.0.0",
            "description": "Manage serial<->network mappings, status and ports. "
                           "Authenticate with `Authorization: Bearer <token>` "
                           "(generate the token in Settings).",
        },
        "servers": [{"url": API_PREFIX}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": {"Mapping": mapping_obj, "Error": error_obj},
        },
        "security": bearer,
        "paths": {
            "/health": {"get": {"summary": "Liveness probe (no auth)", "security": [],
                                "responses": {"200": {"description": "ok"}}}},
            "/status": {"get": {"summary": "Live status of all mappings", "security": bearer,
                                "responses": {"200": {"description": "status map"}}}},
            "/ports": {"get": {"summary": "Detected serial ports", "security": bearer,
                               "responses": {"200": {"description": "port list"}}}},
            "/mappings": {
                "get": {"summary": "List mappings (config + status)", "security": bearer,
                        "responses": {"200": {"description": "mappings"}}},
                "post": {"summary": "Create a mapping", "security": bearer,
                         "requestBody": {"required": True, "content": {"application/json": {
                             "schema": {"$ref": "#/components/schemas/Mapping"}}}},
                         "responses": {"201": {"description": "created (see `warnings`)"},
                                       "400": {"description": "validation error"}}},
            },
            "/mappings/{mid}": {
                "parameters": [{"name": "mid", "in": "path", "required": True,
                                "schema": {"type": "string"}}],
                "get": {"summary": "Get a mapping", "security": bearer,
                        "responses": {"200": {"description": "mapping"},
                                      "404": {"description": "not found"}}},
                "put": {"summary": "Replace a mapping", "security": bearer,
                        "requestBody": {"required": True, "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/Mapping"}}}},
                        "responses": {"200": {"description": "updated (see `warnings`)"},
                                      "400": {"description": "validation error"},
                                      "404": {"description": "not found"}}},
                "delete": {"summary": "Delete a mapping", "security": bearer,
                           "responses": {"200": {"description": "deleted"},
                                         "404": {"description": "not found"}}},
            },
            "/mappings/{mid}/{action}": {
                "parameters": [
                    {"name": "mid", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "action", "in": "path", "required": True,
                     "schema": {"type": "string", "enum": ["start", "stop", "restart"]}},
                ],
                "post": {"summary": "Start/stop/restart a mapping", "security": bearer,
                         "responses": {"200": {"description": "action applied"},
                                       "404": {"description": "not found"}}},
            },
        },
    }
