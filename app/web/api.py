"""JSON REST API (``/api/v1``) for automation: Ansible, scripts, CI, fleet tools.

Separate from the HTMX endpoints under ``/api`` (which return HTML fragments for
the browser UI). Authentication is a bearer token enforced in GuardMiddleware
(``Authorization: Bearer <token>``); these handlers assume the request is already
authenticated. Every response is JSON. The OpenAPI 3.0 description is served at
``/api/v1/openapi.json`` and ``/api/v1/health`` is unauthenticated for probes.
"""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.routing import Route

from ..config import ConfigError, MappingConfig

API_PREFIX = "/api/v1"


def _client_ip(request) -> str:
    ip = request.client.host if request.client else "?"
    if ip.startswith("::ffff:") and "." in ip:
        ip = ip[len("::ffff:"):]
    return ip


def _mapping_view(mapping, state) -> dict:
    """A mapping's full config plus its live runtime status."""
    view = mapping.to_dict()
    view["status"] = state.supervisor.status(mapping.id)
    return view


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
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_json", "detail": "object expected"}, status_code=400)
        body.pop("id", None)         # the server assigns the id
        body.pop("status", None)     # read-only field
        try:
            mapping = MappingConfig.from_dict(body)
            mapping.validate()
        except (ValueError, ConfigError) as e:
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        async with state.config_lock:
            state.config.mappings.append(mapping)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings.remove(mapping)
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping created: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_create", mapping.name)
        return JSONResponse(_mapping_view(mapping, state), status_code=201)

    async def update_mapping(request):
        mid = request.path_params["mid"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_json", "detail": "object expected"}, status_code=400)
        body["id"] = mid             # PUT replaces the mapping with this id
        body.pop("status", None)
        try:
            mapping = MappingConfig.from_dict(body)
            mapping.validate()
        except (ValueError, ConfigError) as e:
            return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        async with state.config_lock:
            existing = state.config.get_mapping(mid)
            if not existing:
                return JSONResponse({"error": "not_found"}, status_code=404)
            backup = list(state.config.mappings)
            state.config.mappings[state.config.mappings.index(existing)] = mapping
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return JSONResponse({"error": "validation", "detail": str(e)}, status_code=400)
        await state.supervisor.apply_mapping(mapping)
        state.log(f"api: mapping updated: {mapping.name}")
        state.audit(_client_ip(request), "api_mapping_update", mapping.name)
        return JSONResponse(_mapping_view(mapping, state))

    async def delete_mapping(request):
        mid = request.path_params["mid"]
        removed = None
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if m:
                state.config.mappings.remove(m)
                await state.asave()
                removed = m
        if removed is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await state.supervisor.remove_mapping(mid)
        state.delete_mapping_log(mid)
        state.log(f"api: mapping deleted: {removed.name}")
        state.audit(_client_ip(request), "api_mapping_delete", removed.name)
        return JSONResponse({"deleted": mid})

    async def mapping_action(request):
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return JSONResponse({"error": "unknown_action"}, status_code=400)
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if not m:
                return JSONResponse({"error": "not_found"}, status_code=404)
            m.enabled = action != "stop"
            await state.asave()
        if action == "stop":
            await state.supervisor.stop_mapping(mid)
        elif action == "restart":
            await state.supervisor.restart_mapping(m)
        else:
            await state.supervisor.apply_mapping(m)
        state.log(f"api: mapping {action}: {m.name}")
        state.audit(_client_ip(request), f"api_mapping_{action}", m.name)
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
    mapping_obj = {"type": "object", "description": "A serial<->network mapping (see config model)."}
    error_obj = {"type": "object", "properties": {"error": {"type": "string"},
                                                  "detail": {"type": "string"}}}
    bearer = [{"bearerAuth": []}]
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
                         "responses": {"201": {"description": "created"},
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
                        "responses": {"200": {"description": "updated"},
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
