"""The dashboard and the mapping endpoints behind it: the live table, the add/edit
form, save/delete/start/stop/restart/duplicate, and the per-mapping log and console
panels."""
from __future__ import annotations

import asyncio
import hashlib
import os
import time

from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from ...config import ConfigError, MappingConfig
from ...engine import netinfo, portlist
from ...i18n import _
from . import ops
from .common import TRIGGER, Ctx, console_interactive, sees_to_device
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
                return PlainTextResponse(_("Mapping not found"), status_code=404)
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
                return PlainTextResponse(_("Duplicate failed: {0}").format(e), status_code=400)
        ctx.audit(request, "mapping_duplicate", dup.name)
        return Response("", headers=TRIGGER)

    async def api_status(request):
        resp = render(request, "_mappings_body.html",
                      mappings=state.config.mappings,
                      statuses=state.supervisor.all_status(),
                      can_edit=can_edit(request))
        # Polled every 2 s: when nothing changed since the browser's copy, answer 204
        # (htmx then keeps the table as it is — no flicker, selections and scroll stay).
        digest = hashlib.sha256(resp.body).hexdigest()[:16]
        if request.headers.get("x-content-hash") == digest:
            return Response(status_code=204, headers={"X-Content-Hash": digest})
        resp.headers["X-Content-Hash"] = digest
        return resp

    async def mapping_bulk(request):
        """Start / stop / restart several mappings at once (the dashboard's selection)."""
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        action = form.get("action", "")
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse(_("Unknown action"), status_code=400)
        ids = [i for i in form.getlist("ids") if i]
        if not ids or len(ids) > 500:
            return PlainTextResponse(_("Select one or more mappings."), status_code=400)
        done = []
        for mid in ids:
            ok, name = await ops.apply_action(state, mid, action)
            if ok:
                done.append(name)
                ctx.audit(request, f"mapping_{action}", name)
        state.log(f"bulk {action}: {len(done)} mapping(s)")
        return Response("", headers=TRIGGER)

    # ---------------- mapping form ----------------
    # The form is for editing (operator+), and it renders stored secrets (the MQTT
    # password) into the page, so viewers get 403 — the UI never offers it to them.
    async def mapping_form_new(request):
        if deny := require_role(request, "operator"):
            return deny
        m = MappingConfig(name="")
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=True, modbus_points_json=_modbus_points_json(m),
                      ports=portlist.visible(state.ports.get()), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_form_edit(request):
        if deny := require_role(request, "operator"):
            return deny
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=False, modbus_points_json=_modbus_points_json(m),
                      ports=portlist.visible(state.ports.get()), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_log(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            # deleted meanwhile: 286 makes htmx stop the panel's 5-second refresh, and the
            # panel says why (a 404 would be polled - and reported - every 5 seconds)
            return render(request, "_mapping_log.html", status_code=286, gone=True)
        lines = await asyncio.to_thread(state.read_mapping_log, mid, 1000)
        return render(request, "_mapping_log.html", mid=mid, mapping_name=m.name,
                      lines=lines)

    def _trace_files(mid):
        out = []
        for path in state.trace_files(mid):
            try:
                st = os.stat(path)
            except OSError:
                continue
            size = st.st_size
            text = (f"{size / 1048576:.1f} MB" if size >= 1048576 else
                    f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B")
            out.append({"name": os.path.basename(path), "path": path, "size_text": text,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
        return out

    # Traces record both directions, what clients type to the device included (see
    # common.sees_to_device): listing and downloading them needs operator+.
    async def mapping_traces(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        files = await asyncio.to_thread(_trace_files, mid)
        o = m.options
        return render(request, "_mapping_traces.html", mid=mid, mapping_name=m.name, files=files,
                      tracing=bool(o.trace_both), current=f"{mid}-{o.trace_both}",
                      max_mb="%g" % o.trace_max_mb, keep=o.trace_keep, hexdump=o.trace_hexdump)

    async def mapping_trace_file(request):
        """Download one of a mapping's trace files — only a name from its own list."""
        if deny := require_role(request, "operator"):
            return deny
        mid, name = request.path_params["mid"], request.path_params["name"]
        m = state.config.get_mapping(mid)
        f = next((f for f in await asyncio.to_thread(_trace_files, mid) if f["name"] == name), None)
        if not m or f is None:
            return PlainTextResponse(_("Trace file not found"), status_code=404)
        ctx.audit(request, "trace_download", f"{m.name}/{name}")
        return FileResponse(f["path"], media_type="text/plain; charset=utf-8", filename=name)

    async def console_view(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        me = current_user(request)  # what the console WebSockets will allow this user
        return render(request, "_console.html", mid=mid, mapping_name=m.name,
                      interactive=console_interactive(me, m), to_device=sees_to_device(me, m),
                      bridge=m.kind == "serialbridge")

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
            return PlainTextResponse(_("Unknown action"), status_code=400)
        ok, name = await ops.apply_action(state, mid, action)
        if not ok:   # (ops answers in English: the REST API and the cluster peers use it too)
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        state.log(f"mapping {action}: {name}")
        ctx.audit(request, f"mapping_{action}", name)
        return Response("", headers=TRIGGER)

    return [
        Route("/", dashboard),
        # before /api/mappings/{mid}/{action}: "duplicate" is not a start/stop action
        Route("/api/mappings/{mid}/duplicate", mapping_duplicate, methods=["POST"]),
        Route("/api/status", api_status),
        Route("/api/mappings/bulk", mapping_bulk, methods=["POST"]),
        Route("/api/mappings/form", mapping_form_new),
        Route("/api/mappings/{mid}/form", mapping_form_edit),
        Route("/api/mappings/{mid}/log", mapping_log),
        Route("/api/mappings/{mid}/console-view", console_view),
        Route("/api/mappings/{mid}/traces", mapping_traces),
        Route("/api/mappings/{mid}/traces/{name}", mapping_trace_file),
        Route("/api/mappings/save", mapping_save, methods=["POST"]),
        Route("/api/mappings/{mid}", mapping_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/{action}", mapping_action, methods=["POST"]),
    ]
