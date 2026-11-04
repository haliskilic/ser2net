"""The dashboard and the mapping endpoints behind it: the live table, the add/edit
form, save/delete/start/stop/restart/duplicate, and the per-mapping log and console
panels."""
from __future__ import annotations

import asyncio

from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from ...config import ConfigError, MappingConfig
from ...engine import netinfo
from . import ops
from .common import TRIGGER, Ctx
from .forms import _form_error, _modbus_points_json


def routes(ctx: Ctx) -> list:
    state, render = ctx.state, ctx.render
    current_user, require_role, can_edit = ctx.current_user, ctx.require_role, ctx.can_edit

    async def dashboard(request):
        return render(
            request, "dashboard.html",
            mappings=state.config.mappings,
            statuses=state.supervisor.all_status(),
            admin=state.config.admin_ui,
            cluster_on=state.config.cluster.active,
            me=current_user(request), can_edit=can_edit(request),
        )

    async def mapping_duplicate(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if not m:
                return PlainTextResponse("Mapping not found", status_code=404)
            d = m.to_dict()
            d.pop("id", None)
            d["name"] = f"{m.name} (copy)"
            d["enabled"] = False  # don't auto-start a duplicate (avoids port clash)
            dup = MappingConfig.from_dict(d)
            # bump a listening port to the next free one
            if dup.kind == "net" and dup.network.listens:
                used = {mm.network.port for mm in state.config.mappings if mm.kind == "net"}
                while dup.network.port in used and dup.network.port < 65535:
                    dup.network.port += 1
            state.config.mappings.append(dup)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings.remove(dup)
                return PlainTextResponse(f"Duplicate failed: {e}", status_code=400)
        ctx.audit(request, "mapping_duplicate", dup.name)
        return Response("", headers=TRIGGER)

    async def api_status(request):
        return render(request, "_mappings_body.html",
                      mappings=state.config.mappings,
                      statuses=state.supervisor.all_status(),
                      can_edit=can_edit(request))

    # ---------------- mapping form ----------------
    # The form is for editing (operator+), and it renders stored secrets (the MQTT
    # password) into the page, so viewers get 403 — the UI never offers it to them.
    async def mapping_form_new(request):
        if deny := require_role(request, "operator"):
            return deny
        m = MappingConfig(name="")
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=True, modbus_points_json=_modbus_points_json(m),
                      ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_form_edit(request):
        if deny := require_role(request, "operator"):
            return deny
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=False, modbus_points_json=_modbus_points_json(m),
                      ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_log(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        lines = await asyncio.to_thread(state.read_mapping_log, mid, 1000)
        return render(request, "_mapping_log.html", mid=mid, mapping_name=m.name,
                      lines=lines)

    async def console_view(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        interactive = (m.kind == "net" and not m.network.read_only)
        return render(request, "_console.html", mid=mid, mapping_name=m.name,
                      interactive=interactive)

    async def mapping_save(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        ok, msg = await ops.save_mapping_from_form(state, form)
        if not ok:
            return _form_error(render, request, state, form, msg)
        state.log(f"mapping saved: {msg}")
        ctx.audit(request, "mapping_save", msg)
        return Response("", headers=TRIGGER)

    async def mapping_delete(request):
        if deny := require_role(request, "operator"):
            return deny
        removed = await ops.delete_mapping(state, request.path_params["mid"])
        if removed is not None:
            state.log(f"mapping deleted: {removed.name}")
            ctx.audit(request, "mapping_delete", removed.name)
        return Response("", headers=TRIGGER)

    async def mapping_action(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse("Unknown action", status_code=400)
        ok, name = await ops.apply_action(state, mid, action)
        if not ok:
            return PlainTextResponse(name, status_code=404)
        state.log(f"mapping {action}: {name}")
        ctx.audit(request, f"mapping_{action}", name)
        return Response("", headers=TRIGGER)

    return [
        Route("/", dashboard),
        # before /api/mappings/{mid}/{action}: "duplicate" is not a start/stop action
        Route("/api/mappings/{mid}/duplicate", mapping_duplicate, methods=["POST"]),
        Route("/api/status", api_status),
        Route("/api/mappings/form", mapping_form_new),
        Route("/api/mappings/{mid}/form", mapping_form_edit),
        Route("/api/mappings/{mid}/log", mapping_log),
        Route("/api/mappings/{mid}/console-view", console_view),
        Route("/api/mappings/save", mapping_save, methods=["POST"]),
        Route("/api/mappings/{mid}", mapping_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/{action}", mapping_action, methods=["POST"]),
    ]
