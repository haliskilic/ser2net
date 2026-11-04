"""Health, Prometheus metrics and the serial port list."""
from __future__ import annotations

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

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
        for m in state.config.mappings:
            st = statuses.get(m.id) or {}
            lbl = f'mapping="{_metric_label(m.name)}",id="{m.id}"'
            running = 1 if st.get("state") == "running" else 0
            lines += [
                f"ser2net_mapping_running{{{lbl}}} {running}",
                f"ser2net_mapping_clients{{{lbl}}} {st.get('client_count', 0)}",
                f"ser2net_mapping_bytes_in_total{{{lbl}}} {st.get('bytes_in', 0)}",
                f"ser2net_mapping_bytes_out_total{{{lbl}}} {st.get('bytes_out', 0)}",
                f"ser2net_mapping_reconnects_total{{{lbl}}} {st.get('reconnects', 0)}",
                f"ser2net_mapping_dropped_clients_total{{{lbl}}} {st.get('dropped_clients', 0)}",
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
        return render(request, "_ports_table.html",
                      ports=state.ports.get(),
                      in_use=state.supervisor.devices_in_use())

    return [
        Route("/healthz", healthz),
        Route("/metrics", metrics),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/refresh", api_ports_refresh, methods=["POST"]),
        Route("/api/ports/table", api_ports_table),
    ]


def _metric_label(name: str) -> str:
    """Escape a mapping name for a Prometheus label value."""
    return name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

