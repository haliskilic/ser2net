"""Health, Prometheus metrics and the serial port list."""
from __future__ import annotations

from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from ... import i18n
from ...engine import netguard, portlist
from .common import Ctx


def routes(ctx: Ctx) -> list:
    state, render = ctx.state, ctx.render

    async def healthz(request):
        return PlainTextResponse("ok")

    async def metrics(request):
        statuses = state.supervisor.all_status()
        lines = [
            "# ser2net metrics",
            "ser2net_up 1",
            f"ser2net_mappings_total {len(state.config.mappings)}",
        ]
        # the process's open files and their limit (POSIX: what serial ports, sockets and
        # files share; not reported on Windows)
        fds, fd_limit = netguard.open_fds(), netguard.fd_limit()
        if fds is not None:
            lines.append(f"ser2net_open_fds {fds}")
        if fd_limit is not None:
            lines.append(f"ser2net_fd_limit {fd_limit}")
        guard = getattr(state, "admin_guard", None)
        if guard is not None:
            lines.append(f"ser2net_admin_connections {len(guard.connections)}")
        budget = netguard.BUDGET
        lines.append(f"ser2net_tls_handshakes_in_progress {len(budget.handshakes)}")
        lines.append(f"ser2net_tls_handshakes_waiting {len(budget.waiting)}")  # for a slot
        # connections refused right after accept: near the open-file limit or (Windows) the
        # event loop's socket limit, too many TLS handshakes, the admin UI's own limits
        for why in netguard.REFUSAL_REASONS:
            lines.append(f'ser2net_connections_refused_total{{reason="{why}"}} '
                         f"{budget.refused.get(why, 0)}")
        for m in state.config.mappings:
            st = statuses.get(m.id) or {}
            lbl = f'mapping="{_metric_label(m.name)}",id="{_metric_label(m.id)}"'
            running = 1 if st.get("state") == "running" else 0
            lines += [
                f"ser2net_mapping_running{{{lbl}}} {running}",
                f"ser2net_mapping_clients{{{lbl}}} {st.get('client_count', 0)}",
                # bytes read from / written to the serial device, counted once at the device;
                # dropped: sent for the device but not written (see RunnerStatus)
                f"ser2net_mapping_bytes_in_total{{{lbl}}} {st.get('bytes_in', 0)}",
                f"ser2net_mapping_bytes_out_total{{{lbl}}} {st.get('bytes_out', 0)}",
                f"ser2net_mapping_bytes_dropped_total{{{lbl}}} {st.get('bytes_dropped', 0)}",
                f"ser2net_mapping_reconnects_total{{{lbl}}} {st.get('reconnects', 0)}",
                f"ser2net_mapping_dropped_clients_total{{{lbl}}} {st.get('dropped_clients', 0)}",
            ]
            if m.kind == "net" and m.network.mode == "client":
                # the connection out to the remote server (ser2net_mapping_running is the
                # serial side's)
                up = 1 if st.get("link") == "up" else 0
                lines += [
                    f"ser2net_mapping_link_up{{{lbl}}} {up}",
                    f"ser2net_mapping_connect_failures_total{{{lbl}}} "
                    f"{st.get('connect_failures', 0)}",
                ]
        return PlainTextResponse("\n".join(lines) + "\n",
                                 media_type="text/plain; version=0.0.4")

    async def api_ports_json(request):
        return JSONResponse(state.ports.get())

    async def api_ports_refresh(request):
        ports = await state.ports.refresh_now()
        selected = request.query_params.get("selected", "")
        return render(request, "_serial_port_select.html", ports=ports, selected=selected)

    async def api_ports_table(request):
        ports = state.ports.get()
        show_all = request.query_params.get("all") == "1"
        shown = portlist.visible(ports, show_all)
        return render(request, "_ports_table.html", ports=shown, show_all=show_all,
                      hidden=len(ports) - len(portlist.visible(ports)),
                      in_use=state.supervisor.devices_in_use())

    async def set_language(request):
        """/lang/tr?next=/settings: remember the visitor's UI language (a cookie), then
        go back. Only a local path is followed (no open redirect)."""
        code = request.path_params["code"]
        nxt = request.query_params.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
            nxt = "/"
        resp = RedirectResponse(nxt, status_code=303)
        if code in i18n.LANGUAGES:
            resp.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, httponly=True,
                            samesite="lax", secure=state.config.admin_ui.tls_enabled, path="/")
        return resp

    return [
        Route("/lang/{code}", set_language),
        Route("/healthz", healthz),
        Route("/metrics", metrics),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/refresh", api_ports_refresh, methods=["POST"]),
        Route("/api/ports/table", api_ports_table),
    ]


def _metric_label(name: str) -> str:
    """Escape a mapping name (or id) for a Prometheus label value."""
    return name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

