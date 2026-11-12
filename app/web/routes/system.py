"""Health, Prometheus metrics and the serial port list."""
from __future__ import annotations

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from ... import i18n
from ...engine import netguard, portlist
from ...i18n import _
from .. import auth, proxy
from .common import Ctx, local_path, unchanged_or

# the serial sides of a mapping form (GET /api/ports/select?prefix=)
_PORT_FIELDS = ("serial_", "serial_b_")


def routes(ctx: Ctx) -> list:
    state, render = ctx.state, ctx.render

    async def healthz(request):
        return PlainTextResponse("ok")

    async def metrics(request):
        return PlainTextResponse(metrics_text(state), media_type=METRICS_TYPE)

    async def api_ports_json(request):
        return JSONResponse(await state.ports.current())

    async def api_ports_select(request):
        """An open mapping form's Device field again, with the serial ports there are now
        (its ↻ button): ?prefix=serial_|serial_b_&selected=<what the form has chosen>. For
        operators, as the form is."""
        if deny := ctx.require_role(request, "operator"):
            return deny
        prefix = request.query_params.get("prefix", "")
        if prefix not in _PORT_FIELDS:
            return PlainTextResponse(_("Unknown serial port field."), status_code=400)
        selected = request.query_params.get("selected", "")[:1024]
        ports = portlist.visible(await state.ports.current())
        return render(request, "_serial_port_select.html", fragment=True, prefix=prefix,
                      selected=selected, ports=ports, links=portlist.links(ports, (selected,)))

    async def api_ports_table(request):
        ports = await state.ports.current()
        show_all = request.query_params.get("all") == "1"
        shown = portlist.visible(ports, show_all)
        # refreshed every 5 s: 204 while the ports are what the browser shows already
        # (rendered off the event loop: see Ctx.arender)
        return unchanged_or(request, await ctx.arender(
            request, "_ports_table.html", ports=shown, show_all=show_all,
            hidden=len(ports) - len(portlist.visible(ports)),
            in_use=portlist.used(shown, state.supervisor.devices_in_use())))

    async def set_language(request):
        """/lang/tr?next=/settings: remember the visitor's UI language (a cookie), then
        go back. Only a local path is followed (no open redirect), under the path prefix
        the request came under (`next` is the page's path without it)."""
        code = request.path_params["code"]
        resp = proxy.redirect(request, local_path(request.query_params.get("next")))
        if code in i18n.LANGUAGES:
            auth.set_cookie(resp, request, state.config, i18n.COOKIE, code, max_age=365 * 86400)
        return resp

    return [
        Route("/lang/{code}", set_language),
        Route("/healthz", healthz),
        Route("/metrics", metrics),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/select", api_ports_select),
        Route("/api/ports/table", api_ports_table),
    ]


# the exposition format /metrics and /api/v1/metrics answer in
METRICS_TYPE = "text/plain; version=0.0.4"


def metrics_text(state) -> str:
    """The Prometheus metrics (text exposition format): served at /metrics to a web UI
    session and at /api/v1/metrics to an API token."""
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
            # of those: failed opens of the serial port, and losses of the open port
            f"ser2net_mapping_open_failures_total{{{lbl}}} {st.get('open_failures', 0)}",
            f"ser2net_mapping_device_lost_total{{{lbl}}} {st.get('device_lost', 0)}",
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
    return "\n".join(lines) + "\n"


def _metric_label(name: str) -> str:
    """Escape a mapping name (or id) for a Prometheus label value."""
    return name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

