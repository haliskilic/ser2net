"""Health, Prometheus metrics and the serial port list."""
from __future__ import annotations

import asyncio
import platform
import sys

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from ... import __version__, i18n, licences, storage
from ...notify import no_data_for
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

    async def licenses_page(request):
        """The licence texts this copy carries (app/licences.py), for any signed-in role."""
        texts = await asyncio.to_thread(licences.texts)
        return render(request, "licenses.html", texts=texts)

    async def metrics(request):
        return PlainTextResponse(await metrics_text(state), media_type=METRICS_TYPE)

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
        Route("/licenses", licenses_page),
        Route("/metrics", metrics),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/select", api_ports_select),
        Route("/api/ports/table", api_ports_table),
    ]


# the exposition format /metrics and /api/v1/metrics answer in
METRICS_TYPE = "text/plain; version=0.0.4"


async def metrics_text(state) -> str:
    """The Prometheus metrics (text exposition format: exposition()): served at /metrics to
    a web UI session and at /api/v1/metrics to an API token."""
    statuses = state.supervisor.all_status()
    lines = [
        "# ser2net metrics",
        "ser2net_up 1",
        *process_metrics(state),
        f"ser2net_mappings_total {len(state.config.mappings)}",
    ]
    # the process's open files and their limit (POSIX: what serial ports, sockets and
    # files share; not reported on Windows)
    fds, fd_limit = netguard.open_fds(), netguard.fd_limit()
    if fds is not None:
        lines.append(f"ser2net_open_fds {fds}")
    if fd_limit is not None:
        lines.append(f"ser2net_fd_limit {fd_limit}")
    # the data folder's disk (traces pause below storage.floor()) and what traces and
    # logs take there
    data_dir = getattr(state, "data_dir", None)
    if data_dir is not None:
        disk = await asyncio.to_thread(storage.usage, data_dir)
        if disk["free"] is not None:
            lines.append(f"ser2net_data_free_bytes {disk['free']}")
            lines.append(f"ser2net_data_size_bytes {disk['total']}")
        lines.append(f"ser2net_traces_bytes {disk['traces']}")
        lines.append(f"ser2net_logs_bytes {disk['logs']}")
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
            *mapping_state_metrics(m, st, lbl),
            *device_traffic_metrics(m, st, lbl),
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
    # Modbus gateways in edge mode: each polled unit, up = it answers (left out until it
    # has been polled), and its failed point reads
    units = [(m, u) for m in state.config.mappings
             for u in (statuses.get(m.id) or {}).get("units") or []]
    for m, u in units:
        if u.get("state") in ("online", "offline"):
            lines.append(f"ser2net_modbus_unit_up{{{_unit_label(m, u)}}} "
                         f"{1 if u['state'] == 'online' else 0}")
    for m, u in units:
        lines.append(f"ser2net_modbus_unit_poll_errors_total{{{_unit_label(m, u)}}} "
                     f"{u.get('errors', 0)}")
    # mappings that publish to MQTT (while they run): connected to the broker, and the
    # messages not handed to it (see MqttPublisher.state)
    brokers = [(m, mq) for m in state.config.mappings
               if (mq := (statuses.get(m.id) or {}).get("mqtt"))]
    for m, mq in brokers:
        lines.append(f"ser2net_mqtt_connected{{{_label(m)}}} "
                     f"{1 if mq.get('state') == 'connected' else 0}")
    for m, mq in brokers:
        lines.append(f"ser2net_mqtt_messages_dropped_total{{{_label(m)}}} "
                     f"{mq.get('dropped', 0)}")
    return exposition(lines)


# The # HELP and # TYPE of each series /metrics answers (text exposition format 0.0.4).
# The names stay as they are, ser2net_mappings_total too (a gauge despite its suffix).
METRIC_INFO = {
    "ser2net_up": ("gauge", "1 while ser2net answers"),
    "ser2net_build_info": ("gauge", "The version that runs (labels), always 1"),
    "ser2net_start_time_seconds": ("gauge", "When this ser2net process started (Unix time)"),
    "ser2net_event_loop_lag_seconds": ("gauge", "How late the event loop's last heartbeat "
                                                "was (a loop that is held up)"),
    "ser2net_event_loop_stalls_total": ("counter", "Heartbeats of the event loop 2 s or more "
                                                   "late"),
    "ser2net_mappings_total": ("gauge", "Mappings configured"),
    "ser2net_open_fds": ("gauge", "Files the process has open (POSIX)"),
    "ser2net_fd_limit": ("gauge", "The process's open-file limit (POSIX)"),
    "ser2net_data_free_bytes": ("gauge", "Free space on the data folder's disk"),
    "ser2net_data_size_bytes": ("gauge", "Size of the data folder's disk"),
    "ser2net_traces_bytes": ("gauge", "What the trace files take in the data folder"),
    "ser2net_logs_bytes": ("gauge", "What the logs take in the data folder"),
    "ser2net_admin_connections": ("gauge", "Connections to the admin UI"),
    "ser2net_tls_handshakes_in_progress": ("gauge", "TLS handshakes under way"),
    "ser2net_tls_handshakes_waiting": ("gauge", "Connections waiting for a TLS handshake slot"),
    "ser2net_connections_refused_total": ("counter", "Connections refused right after accept, "
                                                     "by reason"),
    "ser2net_mapping_enabled": ("gauge", "1 when the mapping is enabled, 0 when it was "
                                         "stopped (or is off)"),
    "ser2net_mapping_state": ("gauge", "1 for the mapping's state now (label state), 0 for "
                                       "the others"),
    "ser2net_mapping_running": ("gauge", "1 while the mapping runs"),
    "ser2net_mapping_last_rx_timestamp_seconds": ("gauge", "When the serial device last sent "
                                                           "something (Unix time; 0: not since "
                                                           "the start)"),
    "ser2net_mapping_no_data": ("gauge", "1 while the device is past the mapping's no-data "
                                         "alarm"),
    "ser2net_mapping_clients": ("gauge", "Clients connected to the mapping"),
    "ser2net_mapping_bytes_in_total": ("counter", "Bytes read from the serial device"),
    "ser2net_mapping_bytes_out_total": ("counter", "Bytes written to the serial device"),
    "ser2net_mapping_bytes_dropped_total": ("counter", "Bytes for the serial device that "
                                                       "were not written"),
    "ser2net_mapping_reconnects_total": ("counter", "Reconnects after the serial port failed "
                                                    "or went away"),
    "ser2net_mapping_open_failures_total": ("counter", "Failed opens of the serial port"),
    "ser2net_mapping_device_lost_total": ("counter", "Losses of the open serial port"),
    "ser2net_mapping_dropped_clients_total": ("counter", "Clients dropped for not reading "
                                                         "fast enough"),
    "ser2net_mapping_link_up": ("gauge", "1 while a connect-out mapping is connected"),
    "ser2net_mapping_connect_failures_total": ("counter", "Failed connect-out attempts"),
    "ser2net_modbus_unit_up": ("gauge", "1 while a polled Modbus unit answers"),
    "ser2net_modbus_unit_poll_errors_total": ("counter", "Failed point reads of a Modbus unit"),
    "ser2net_mqtt_connected": ("gauge", "1 while the mapping is connected to its MQTT broker"),
    "ser2net_mqtt_messages_dropped_total": ("counter", "MQTT messages not handed to the "
                                                       "broker"),
}

# The states a mapping's runner has (RunnerStatus.state): ser2net_mapping_state has one
# series for each
MAPPING_STATES = ("running", "reconnecting", "device-missing", "error", "stopped")


def process_metrics(state) -> list[str]:
    """The version that runs, when it started and how its event loop keeps up."""
    labels = (f'version="{_metric_label(__version__)}",'
              f'python="{_metric_label(platform.python_version())}",'
              f'platform="{_metric_label(sys.platform)}"')
    lines = [f"ser2net_build_info{{{labels}}} 1"]
    started = getattr(state, "started_at", None)
    if started is not None:
        lines.append(f"ser2net_start_time_seconds {started:.3f}")
    dog = getattr(state, "loop_watchdog", None)  # (app/watchdog.py)
    if dog is not None:
        lines.append(f"ser2net_event_loop_lag_seconds {dog.lag:.3f}")
        lines.append(f"ser2net_event_loop_stalls_total {dog.slow_beats}")
    return lines


def mapping_state_metrics(mapping, status: dict, lbl: str) -> list[str]:
    """Whether the mapping is enabled (a mapping stopped on purpose is not; one that
    failed is) and its state, one-hot."""
    now = status.get("state") or "stopped"
    states = MAPPING_STATES if now in MAPPING_STATES else (*MAPPING_STATES, now)
    return [f"ser2net_mapping_enabled{{{lbl}}} {1 if mapping.enabled else 0}"] + [
        f'ser2net_mapping_state{{{lbl},state="{_metric_label(s)}"}} {1 if s == now else 0}'
        for s in states]


def device_traffic_metrics(mapping, status: dict, lbl: str) -> list[str]:
    """When the device last sent something, and, with its no-data alarm on, whether it is
    past it (notify.no_data_for)."""
    lines = [f"ser2net_mapping_last_rx_timestamp_seconds{{{lbl}}} "
             f"{float(status.get('last_rx') or 0):.3f}"]
    if mapping.options.no_data_alarm_s:
        lines.append(f"ser2net_mapping_no_data{{{lbl}}} {1 if no_data_for(mapping, status) else 0}")
    return lines


def _series(line: str) -> str:
    """The series name of a sample line."""
    return line.split("{", 1)[0].split(" ", 1)[0]


def exposition(lines: list[str]) -> str:
    """The metrics text from its sample lines: the samples of each series together (in
    the order the series first appear), behind its # HELP and # TYPE lines (METRIC_INFO;
    a series without an entry gets none), comment lines first."""
    comments = [ln for ln in lines if ln.startswith("#")]
    groups: dict[str, list[str]] = {}
    for ln in lines:
        if not ln.startswith("#"):
            groups.setdefault(_series(ln), []).append(ln)
    out = list(comments)
    for name, samples in groups.items():
        if name in METRIC_INFO:
            kind, text = METRIC_INFO[name]
            out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
        out += samples
    return "\n".join(out) + "\n"


def _label(mapping) -> str:
    return f'mapping="{_metric_label(mapping.name)}",id="{_metric_label(mapping.id)}"'


def _unit_label(mapping, unit: dict) -> str:
    return (f'mapping="{_metric_label(mapping.name)}",id="{_metric_label(mapping.id)}",'
            f'unit="{unit.get("unit")}"')


def _metric_label(name: str) -> str:
    """Escape a mapping name (or id) for a Prometheus label value."""
    return name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

