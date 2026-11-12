"""The dashboard and the mapping endpoints behind it: the live table, the add/edit
form, save/delete/start/stop/restart/duplicate, and the per-mapping log and console
panels."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import urllib.parse

from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from ... import clock
from ...config import ConfigError, MappingConfig, console_charset, console_encoding, mapping_revision
from ...engine import netinfo, portlist
from ...i18n import _, ngettext
from . import ops
from .common import (
    TRIGGER, Ctx, LiveTable, action_done, bytesize, console_interactive, notice, reason,
    sees_to_device, start_failed,
)
from .forms import _form_error, form_context


def _port_taken(error: str) -> bool:
    """Does a mapping's start error say that its port is in use (EADDRINUSE: errno 98 on
    Linux, 48 on macOS, 10048 on Windows)?"""
    text = error.lower()
    return "address already in use" in text or any(f"[errno {n}]" in text
                                                   for n in (98, 48, 10048))


def _start_failure(error: str, retrying: bool) -> tuple[str, str]:
    """Why a mapping that was just saved or started did not start, for its notice:
    reason() (what it means in the page's language, the error for the tooltip), plus what
    to do when another program holds its port, and, while the supervisor tries the start
    again (a listener that could not bind), that it starts once it can listen there."""
    why, raw = reason(error)
    if _port_taken(error):
        why += " " + _("Another program — another ser2net instance? — listens on that port: "
                       "give the mapping a port of its own.")
    if retrying:
        why += " " + _("It is started as soon as it can listen there (its row shows when it "
                       "is tried again).")
    return why, raw


# what _mappings_body.html shows of a mapping's status (st.*), apart from the counters and
# the clients (tests/test_live_table.py checks the template uses no other field)
_SHOWN = ("state", "device", "last_error", "client_count", "dropped_clients", "bytes_dropped",
          # a connect-out link's state, a serial port's hint and warnings, the bind retry
          "link", "link_error", "hint", "warnings", "retrying", "next_retry_s",
          "trace_error")  # a trace that is not written now


def _steady(st):
    """What the table shows of a mapping's status, minus what changes all the time: the
    In / Out counters (patched on their own) and the clients' connection times (the
    browser counts them on from connected_at). Not what it doesn't show (the reconnect
    count of a mapping whose device is missing goes up every few seconds)."""
    if not st:
        return None
    return (tuple(st.get(k) for k in _SHOWN),
            [(c.get("peer"), c.get("priority"), c.get("connected_at")) for c in st.get("clients") or ()])


def _in_out(st) -> str:
    """The text of a row's In / Out cell, as _mappings_body.html renders it."""
    return f"{bytesize(st.get('bytes_in', 0) if st else 0)} / {bytesize(st.get('bytes_out', 0) if st else 0)}"


# what a download reads at a time (as Starlette's FileResponse)
DOWNLOAD_CHUNK = 64 * 1024


def snapshot_response(fh, filename: str, media_type: str, head: bool = False,
                      chunk_size: int = DOWNLOAD_CHUNK) -> StreamingResponse:
    """A download of the open binary file `fh` as it is now: exactly the size it has
    (taken on the open handle, so a rotation can't put another file in its place
    meanwhile) with that Content-Length, read off the event loop. What the trace writer
    adds meanwhile is not sent: a FileResponse would send it past the Content-Length it
    declared, and the server would break the download off. A file cut meanwhile (by
    another program) ends the download early. Closes `fh`; `head`: headers only."""
    size = os.fstat(fh.fileno()).st_size

    async def body():
        try:
            left = 0 if head else size
            while left > 0:
                data = await asyncio.to_thread(fh.read, min(chunk_size, left))
                if not data:
                    break
                left -= len(data)
                yield data
        finally:
            fh.close()

    quoted = urllib.parse.quote(filename)  # a trace name may be in any script
    disposition = (f'attachment; filename="{filename}"' if quoted == filename
                   else f"attachment; filename*=utf-8''{quoted}")
    return StreamingResponse(body(), media_type=media_type, headers={
        "Content-Length": str(size), "Content-Disposition": disposition})


def _bulk_refused(refused: list, total: int) -> str:
    """Why some mappings of a bulk start / restart were not changed (the others were)."""
    reasons = list(dict.fromkeys(str(r.error) for r in refused))  # once each, in order
    return ngettext("{0} of {1} selected mapping not changed: {2}",
                    "{0} of {1} selected mappings not changed: {2}", total).format(
        len(refused), total, " ".join(reasons[:3]) + (" …" if len(reasons) > 3 else ""))


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
        dup = None
        try:
            async with state.edit_config():
                m = state.config.get_mapping(mid)
                if m:
                    d = m.to_dict()
                    d.pop("id", None)
                    d["name"] = f"{m.name} (copy)"
                    d["enabled"] = False  # don't auto-start a duplicate (avoids port clash)
                    # a broker drops one of two connections with the same client id
                    d["mqtt"]["client_id"] = ""
                    dup = MappingConfig.from_dict(d)
                    # bump a listening port to the next free one (not the admin UI's, ...)
                    if dup.kind == "net" and dup.network.listens:
                        used = {mm.network.port for mm in state.config.mappings if mm.kind == "net"}
                        while ((dup.network.port in used or state.config.own_port_conflict(dup))
                               and dup.network.port < 65535):
                            dup.network.port += 1
                    state.config.mappings.append(dup)
        except ConfigError as e:  # not saved, so not copied (config.json unwritable too)
            return PlainTextResponse(_("Duplicate failed: {0}").format(e), status_code=400)
        if dup is None:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        ctx.audit(request, "mapping_duplicate", dup.name)
        return Response("", headers={**TRIGGER, **notice(
            _("Copied as “{0}” (disabled: it uses the same serial port).").format(dup.name))})

    table = LiveTable(ctx, "_mappings_body.html")

    async def api_status(request):
        # polled every 2 s: 204 while unchanged, only the In / Out counters when only they
        # changed, else the table rendered off the event loop (see LiveTable). On a
        # snapshot: an edit replaces a mapping object, a delete removes it from the list.
        mappings, statuses = list(state.config.mappings), state.supervisor.all_status()
        edit = can_edit(request)
        # Trace is offered while tracing is on, and after that as long as files are left
        traced = await asyncio.to_thread(state.traced_mapping_ids) if edit else set()

        def snapshot():
            return ((edit, sorted(traced), [(repr(m), _steady(statuses.get(m.id))) for m in mappings]),
                    {f"m-{m.id}-bytes": _in_out(statuses.get(m.id)) for m in mappings})
        # the server's clock: the browser counts the clients' connection times on from it
        return await table.answer(request, snapshot, headers={"X-Server-Time": f"{time.time():.3f}"},
                                  mappings=mappings, statuses=statuses, can_edit=edit,
                                  traced=traced)

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
        results = await ops.apply_actions(state, ids, action)  # one save for all
        done, refused, failed = [], [], []
        for mid, res in zip(dict.fromkeys(ids), results, strict=True):
            if res.error is not None:
                refused.append(res)
            elif res.found:
                done.append(res.name)
                ctx.audit(request, f"mapping_{action}", res.name)
                if action != "stop" and start_failed(state, mid):
                    failed.append(res.name)
        state.log(f"bulk {action}: {len(done)} mapping(s)"
                  + (f", {len(refused)} refused" if refused else ""))
        total = len(results)
        if refused:  # not changed, with the reasons; the others are done
            return PlainTextResponse(_bulk_refused(refused, total), status_code=400,
                                     headers=TRIGGER)
        n = len(done) - len(failed)
        text = (ngettext("Started {0} of {1} selected mapping.", "Started {0} of {1} selected mappings.",
                         total) if action == "start" else
                ngettext("Stopped {0} of {1} selected mapping.", "Stopped {0} of {1} selected mappings.",
                         total) if action == "stop" else
                ngettext("Restarted {0} of {1} selected mapping.",
                         "Restarted {0} of {1} selected mappings.", total)).format(n, total)
        if failed:
            more = len(failed) - 5
            text += " " + _("Could not start: {0}.").format(
                ", ".join(failed[:5]) + (" " + _("(+{0} more)").format(more) if more > 0 else ""))
        if warning := next((r.warning for r in results if r.warning), ""):
            text += " " + warning  # stopped, but not saved
        return Response("", headers={**TRIGGER, **notice(text, "warn" if failed or warning
                                                                else "ok")})

    # ---------------- mapping form ----------------
    # The form is for editing (operator+), so viewers get 403 — the UI never offers it to
    # them. It shows every setting but the MQTT password, which it only says is stored.
    async def mapping_form_new(request):
        if deny := require_role(request, "operator"):
            return deny
        return render(request, "_mapping_form.html", **form_context(
            MappingConfig(name=""), await state.ports.current(), netinfo.list_ip_candidates(),
            is_new=True))

    async def mapping_form_edit(request):
        if deny := require_role(request, "operator"):
            return deny
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        ports = await state.ports.current()
        return render(request, "_mapping_form.html", **form_context(
            m, ports, netinfo.list_ip_candidates(), is_new=False,
            rev=mapping_revision(state.config.secret_key, m),
            links=portlist.links(portlist.visible(ports), (m.serial.port, m.serial_b.port)),
            mqtt_stored=m.mqtt.password))

    async def mapping_log(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            # deleted meanwhile: 286 makes htmx stop the panel's 5-second refresh, and the
            # panel says why (a 404 would be polled - and reported - every 5 seconds)
            return render(request, "_mapping_log.html", status_code=286, gone=True)
        lines = await asyncio.to_thread(state.read_mapping_log, mid, 1000)
        # refreshed every 5 s: 204 while the browser shows these lines already (the hash
        # goes into the panel, which each refresh replaces)
        digest = hashlib.sha256(json.dumps([getattr(request.state, "lang", ""), m.name, lines])
                                .encode()).hexdigest()[:16]
        if request.headers.get("x-content-hash") == digest:
            return Response(status_code=204)
        return await ctx.arender(request, "_mapping_log.html", mid=mid, mapping_name=m.name,
                                 lines=lines, content_hash=digest)

    def _trace_files(mid):
        out = []
        for path in state.trace_files(mid):
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append({"name": os.path.basename(path), "path": path,
                        "size_text": bytesize(st.st_size), "time": clock.stamp(st.st_mtime)})
        return out

    async def _traces_panel(request, m, ok="", error=""):
        files = await asyncio.to_thread(_trace_files, m.id)
        o = m.options
        st = state.supervisor.status(m.id) or {}
        return render(request, "_mapping_traces.html", status_code=400 if error else 200,
                      mid=m.id, mapping_name=m.name, files=files,
                      tracing=bool(o.trace_both), current=f"{m.id}-{o.trace_both}",
                      max_mb="%g" % o.trace_max_mb, keep=o.trace_keep, hexdump=o.trace_hexdump,
                      trace_error=st.get("trace_error", ""), ok=ok, error=error,
                      trace_days=state.config.trace_max_age_days)

    # Traces record both directions, what clients type to the device included (see
    # common.sees_to_device): listing, downloading and deleting them needs operator+.
    async def mapping_traces(request):
        if deny := require_role(request, "operator"):
            return deny
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        return await _traces_panel(request, m)

    async def mapping_traces_delete(request):
        """Delete one of a mapping's trace files (a name of its own list) or, without a
        name, all of them; audited. Answers the trace panel with what was done (400 when
        something was not deleted: htmx shows it too)."""
        if deny := require_role(request, "operator"):
            return deny
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        name = request.path_params.get("name")
        deleted, failed = await state.delete_trace_files(m.id, None if name is None else [name])
        if deleted:
            detail = f"{m.name}/{deleted[0]}" if name is not None else \
                f"{m.name}: {len(deleted)} file(s): {', '.join(deleted)}"
            state.log(f"trace file(s) deleted: {detail}")
            ctx.audit(request, "trace_delete", detail)
        ok = ngettext("Deleted {0} trace file.", "Deleted {0} trace files.",
                      len(deleted)).format(len(deleted)) if deleted else ""
        if ok and m.options.trace_both and f"{m.id}-{m.options.trace_both}" in deleted:
            ok += " " + _("Tracing is still on: new traffic begins a new file.")
        error = "; ".join(_("Could not delete {0}: {1}").format(n, why) for n, why in failed)
        if not deleted and not failed:
            ok = _("No trace files to delete.")
        return await _traces_panel(request, m, ok=ok, error=error)

    async def mapping_trace_file(request):
        """Download one of a mapping's trace files — only a name from its own list."""
        if deny := require_role(request, "operator"):
            return deny
        mid, name = request.path_params["mid"], request.path_params["name"]
        m = state.config.get_mapping(mid)
        f = next((f for f in await asyncio.to_thread(_trace_files, mid) if f["name"] == name), None)
        if not m or f is None:
            return PlainTextResponse(_("Trace file not found"), status_code=404)
        try:  # the file it is now, as it is now: the trace may go on writing it
            fh = await asyncio.to_thread(open, f["path"], "rb")
        except OSError:  # rotated or deleted meanwhile
            return PlainTextResponse(_("Trace file not found"), status_code=404)
        ctx.audit(request, "trace_download", f"{m.name}/{name}")
        return snapshot_response(fh, name, "text/plain; charset=utf-8",
                                 head=request.method == "HEAD")

    async def console_view(request):
        mid = request.path_params["mid"]
        m = state.config.get_mapping(mid)
        if not m:
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        me = current_user(request)  # what the console WebSockets will allow this user
        return render(request, "_console.html", mid=mid, mapping_name=m.name,
                      interactive=console_interactive(me, m), to_device=sees_to_device(me, m),
                      bridge=m.kind == "serialbridge",
                      # a Modbus gateway's text view is binary noise: its frames in hex
                      hex_first=m.kind == "net" and m.network.protocol == "modbus",
                      encoding=console_encoding(m.options),
                      charset=console_charset(console_encoding(m.options)))

    async def mapping_save(request):
        if deny := require_role(request, "operator"):
            return deny
        form = await request.form()
        ok, msg, saved = await ops.save_mapping_from_form(state, form)
        if not ok or saved is None:
            return _form_error(render, request, state, form, msg)
        state.log(f"mapping saved: {msg}")
        ctx.audit(request, "mapping_save", msg)
        # the focus then: the mapping's Edit button (a new one's, once the table shows it)
        focus = f"m-{saved.id}-edit"
        if saved.start_error:  # saved, but it didn't start: say why, and what to do
            why, raw = _start_failure(saved.start_error, saved.retrying)
            note = notice(_("“{0}” saved, but it could not start: {1}").format(msg, why),
                          "warn", focus, raw)
        else:
            note = notice(_("“{0}” saved.").format(msg), focus=focus)
        return Response("", headers={**TRIGGER, **note})

    async def mapping_delete(request):
        if deny := require_role(request, "operator"):
            return deny
        try:
            removed = await ops.delete_mapping(state, request.path_params["mid"])
        except ConfigError as e:  # not deleted
            return PlainTextResponse(_("Not deleted: {0}").format(e), status_code=400)
        if removed is None:
            return Response("", headers=TRIGGER)
        state.log(f"mapping deleted: {removed.name}")
        ctx.audit(request, "mapping_delete", removed.name)
        return Response("", headers={**TRIGGER, **notice(_("“{0}” deleted.").format(removed.name))})

    async def mapping_action(request):
        if deny := require_role(request, "operator"):
            return deny
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse(_("Unknown action"), status_code=400)
        res = await ops.apply_action(state, mid, action)
        if not res.found:  # (ops answers in English: the REST API and the cluster peers use it too)
            return PlainTextResponse(_("Mapping not found"), status_code=404)
        if res.error is not None:  # nothing changed (a serial port in use, config.json, ...)
            return PlainTextResponse(str(res.error), status_code=400)
        state.log(f"mapping {action}: {res.name}")
        ctx.audit(request, f"mapping_{action}", res.name)
        if res.warning:  # stopped, but config.json could not keep it
            note = notice(res.warning, "warn")
        elif error := (start_failed(state, mid) if action != "stop" else ""):
            # as for a save: the reason, and the hints for a taken port and the bind retry
            retrying = bool((state.supervisor.status(mid) or {}).get("retrying"))
            why, raw = _start_failure(error, retrying)
            note = notice(_("“{0}” could not start: {1}").format(res.name, why), "warn", detail=raw)
        else:
            note = notice(action_done(action, res.name))
        return Response("", headers={**TRIGGER, **note})

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
        Route("/api/mappings/{mid}/traces", mapping_traces_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/traces/{name}", mapping_trace_file),
        Route("/api/mappings/{mid}/traces/{name}", mapping_traces_delete, methods=["DELETE"]),
        Route("/api/mappings/save", mapping_save, methods=["POST"]),
        Route("/api/mappings/{mid}", mapping_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/{action}", mapping_action, methods=["POST"]),
    ]
