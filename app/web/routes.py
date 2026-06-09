"""HTTP routes: pages (login/setup/dashboard/settings) + HTMX/JSON API.

Interaction model (HTMX, no SPA):
  - the mappings panel polls GET /api/status every 2s and also refreshes when a
    response sets the `HX-Trigger: refreshMappings` header,
  - add/edit load a server-rendered form fragment into #form-panel,
  - save/delete/start/stop return an empty body + the refresh trigger.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
from urllib.parse import urlparse

from starlette.responses import (
    JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from ..config import ConfigError, MappingConfig
from ..engine import netinfo
from . import auth

SESSION_TTL = 8 * 3600
_TRIGGER = {"HX-Trigger": "refreshMappings"}


def build_routes(templates, state, static_dir):
    def render(request, name, status_code=200, headers=None, **ctx):
        return templates.TemplateResponse(request, name, ctx,
                                          status_code=status_code, headers=headers)

    def client_ip(request) -> str:
        ip = request.client.host if request.client else "?"
        # normalize IPv4-mapped IPv6 (::ffff:1.2.3.4 -> 1.2.3.4) so the rate-limit
        # key is consistent on dual-stack listeners
        if ip.startswith("::ffff:") and "." in ip:
            ip = ip[len("::ffff:"):]
        return ip

    def set_session(response, request):
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(state.config.secret_key, SESSION_TTL, state.config.pwd_version),
            max_age=SESSION_TTL, httponly=True, samesite="lax",
            secure=state.config.admin_ui.tls_enabled, path="/",
        )

    # ---------------- pages ----------------
    async def healthz(request):
        return PlainTextResponse("ok")

    async def login_get(request):
        return render(request, "login.html", error=None)

    async def login_post(request):
        ip = client_ip(request)
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        if state.rate_limiter.blocked(ip):
            return render(request, "login.html", status_code=429,
                          error="Too many attempts. Wait a few minutes and try again.")
        password = form.get("password", "")
        # scrypt is deliberately slow (~tens of ms); run it off the event loop so a
        # login attempt doesn't stall every active bridge.
        pw_ok = state.config.password_set and await asyncio.to_thread(
            auth.verify_password, password, state.config.password_hash)
        if pw_ok:
            state.rate_limiter.reset(ip)
            state.log(f"admin login from {ip}")
            resp = RedirectResponse("/", status_code=303)
            set_session(resp, request)
            return resp
        state.rate_limiter.record_failure(ip)
        state.log(f"failed login from {ip}")
        return render(request, "login.html", status_code=401, error="Invalid password.")

    async def logout_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE, path="/")
        return resp

    async def setup_get(request):
        return render(request, "setup.html", error=None,
                      ips=netinfo.list_ip_candidates(),
                      admin=state.config.admin_ui)

    async def setup_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        pw = form.get("password", "")
        pw2 = form.get("password2", "")
        err = _password_problem(pw, pw2)
        if err:
            return render(request, "setup.html", status_code=400, error=err,
                          ips=netinfo.list_ip_candidates(), admin=state.config.admin_ui)
        pw_hash = await asyncio.to_thread(auth.hash_password, pw)
        async with state.config_lock:
            state.config.password_hash = pw_hash
            state.config.pwd_version += 1
            await state.asave()
        state.log("admin password set (first-run setup complete)")
        state.audit(client_ip(request), "first_run_setup", "")
        resp = RedirectResponse("/", status_code=303)
        set_session(resp, request)
        return resp

    async def dashboard(request):
        return render(
            request, "dashboard.html",
            mappings=state.config.mappings,
            statuses=state.supervisor.all_status(),
            admin=state.config.admin_ui,
        )

    async def settings_get(request, ok=None, error=None):
        return render(request, "settings.html", ok=ok, error=error,
                      admin=state.config.admin_ui,
                      uptime=int(state.started_at))

    async def settings_password_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        cur = form.get("current", "")
        new = form.get("password", "")
        new2 = form.get("password2", "")
        if not await asyncio.to_thread(auth.verify_password, cur, state.config.password_hash):
            return await settings_get(request, error="Current password is incorrect.")
        err = _password_problem(new, new2)
        if err:
            return await settings_get(request, error=err)
        new_hash = await asyncio.to_thread(auth.hash_password, new)
        async with state.config_lock:
            state.config.password_hash = new_hash
            state.config.pwd_version += 1
            await state.asave()
        state.log("admin password changed; other sessions signed out")
        state.audit(client_ip(request), "password_change", "")
        # refresh THIS session so the admin isn't logged out by their own change
        resp = await settings_get(request, ok="Password updated. Other sessions were signed out.")
        set_session(resp, request)
        return resp

    async def settings_tls_post(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        cert, key = form.get("tls_cert", "").strip(), form.get("tls_key", "").strip()
        async with state.config_lock:
            old = (state.config.admin_ui.tls_cert, state.config.admin_ui.tls_key)
            state.config.admin_ui.tls_cert = cert
            state.config.admin_ui.tls_key = key
            try:
                await state.asave()
            except ConfigError as e:
                state.config.admin_ui.tls_cert, state.config.admin_ui.tls_key = old
                return await settings_get(request, error=str(e))
        state.audit(client_ip(request), "admin_tls_set", cert)
        return await settings_get(request, ok="Admin TLS saved. Restart to apply.")

    async def settings_tls_generate(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        tdir = os.path.join(state.data_dir, "tls")
        os.makedirs(tdir, exist_ok=True)
        cert, key = os.path.join(tdir, "cert.pem"), os.path.join(tdir, "key.pem")
        try:
            await asyncio.to_thread(subprocess.run, [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key, "-out", cert, "-days", "825", "-subj", "/CN=ser2net",
            ], check=True, capture_output=True, timeout=30)
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            return await settings_get(request, error=f"openssl not available or failed: {e}")
        async with state.config_lock:
            state.config.admin_ui.tls_cert = cert
            state.config.admin_ui.tls_key = key
            await state.asave()
        state.audit(client_ip(request), "admin_tls_generate", "")
        return await settings_get(request, ok="Self-signed certificate generated. Restart to apply TLS.")

    # ---------------- config export / import ----------------
    async def config_export(request):
        # mappings only — never export password_hash / secret_key
        data = {"version": 1, "mappings": [m.to_dict() for m in state.config.mappings]}
        return Response(
            json.dumps(data, indent=2), media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=ser2net-mappings.json"})

    async def config_import(request):
        form = await request.form()
        if not auth.csrf_token_matches(request, form.get("_csrf")):
            return PlainTextResponse("CSRF validation failed. Reload the page.", status_code=403)
        upload = form.get("file")
        try:
            raw = await upload.read() if hasattr(upload, "read") else (upload or "")
            doc = json.loads(raw)
            new_maps = [MappingConfig.from_dict(x) for x in doc.get("mappings", [])]
        except (AttributeError, ValueError, TypeError) as e:
            return await settings_get(request, error=f"Import failed: invalid file ({e}).")
        async with state.config_lock:
            backup = state.config.mappings
            state.config.mappings = new_maps
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return await settings_get(request, error=f"Import rejected: {e}")
        await state.supervisor.stop_all()
        await state.supervisor.start_all(state.config)
        state.audit(client_ip(request), "config_import", f"{len(new_maps)} mappings")
        return await settings_get(request, ok=f"Imported {len(new_maps)} mappings (replaced existing).")

    async def mapping_duplicate(request):
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
        state.audit(client_ip(request), "mapping_duplicate", dup.name)
        return Response("", headers=_TRIGGER)

    # ---------------- console (xterm over WebSocket) ----------------
    async def console_ws(websocket):
        cfg = state.config
        # BaseHTTPMiddleware does NOT run for WebSocket scope, so authenticate here.
        token = websocket.cookies.get(auth.SESSION_COOKIE)
        if not (cfg.password_set and auth.check_session(cfg.secret_key, token, cfg.pwd_version)):
            await websocket.close(code=1008)
            return
        # CSWSH guard: cross-origin WebSocket is rejected.
        origin = websocket.headers.get("origin")
        if origin and urlparse(origin).netloc != websocket.headers.get("host"):
            await websocket.close(code=1008)
            return
        mid = websocket.path_params["mid"]
        runner = state.supervisor.get_runner(mid)
        mapping = cfg.get_mapping(mid)
        if runner is None or mapping is None:
            await websocket.close(code=1011)
            return

        await websocket.accept()
        mon = _Monitor()
        runner.add_monitor(mon)
        interactive = (mapping.kind == "net" and not mapping.network.read_only)
        peer = websocket.client.host if websocket.client else "?"
        state.log(f"[{mapping.name}] console opened by {peer}")

        async def sender():
            while True:
                data = await mon.queue.get()
                if data is None:  # mapping stopped
                    with contextlib.suppress(Exception):
                        await websocket.close()
                    return
                await websocket.send_bytes(data)

        async def receiver():
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                if data is None and msg.get("text") is not None:
                    data = msg["text"].encode("utf-8", "replace")
                if data and interactive:
                    await runner.serial_write(data)

        st = asyncio.create_task(sender())
        rt = asyncio.create_task(receiver())
        try:
            await asyncio.wait({st, rt}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (st, rt):
                t.cancel()
            for t in (st, rt):
                with contextlib.suppress(BaseException):
                    await t
            runner.discard_monitor(mon)
            with contextlib.suppress(Exception):
                await websocket.close()
            state.log(f"[{mapping.name}] console closed ({peer})")

    # ---------------- status / ports / ips ----------------
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

    async def api_status(request):
        return render(request, "_mappings_body.html",
                      mappings=state.config.mappings,
                      statuses=state.supervisor.all_status())

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

    # ---------------- mapping form ----------------
    async def mapping_form_new(request):
        return render(request, "_mapping_form.html",
                      mapping=MappingConfig(name=""), is_new=True,
                      ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                      error=None)

    async def mapping_form_edit(request):
        m = state.config.get_mapping(request.path_params["mid"])
        if not m:
            return PlainTextResponse("Mapping not found", status_code=404)
        return render(request, "_mapping_form.html",
                      mapping=m, is_new=False,
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
        form = await request.form()
        try:
            data = _mapping_from_form(form)
            mapping = MappingConfig.from_dict(data)
            # The form doesn't expose every field; when editing, carry the unexposed
            # ones over so a save never silently wipes them (get_mapping is a sync
            # read — safe outside the config lock).
            _preserve_unmanaged_fields(mapping, state.config.get_mapping(mapping.id))
            mapping.validate()
        except (ValueError, ConfigError) as e:
            return _form_error(render, request, state, form, str(e))

        # Hold the lock only to mutate + persist config; release it BEFORE the
        # (potentially slow) supervisor call so other admin requests aren't blocked
        # while a serial port is being (re)opened.
        async with state.config_lock:
            backup = list(state.config.mappings)
            existing = state.config.get_mapping(mapping.id)
            if existing:
                state.config.mappings[state.config.mappings.index(existing)] = mapping
            else:
                state.config.mappings.append(mapping)
            try:
                await state.asave()
            except ConfigError as e:
                state.config.mappings = backup
                return _form_error(render, request, state, form, str(e))
        await state.supervisor.apply_mapping(mapping)
        state.log(f"mapping saved: {mapping.name}")
        state.audit(client_ip(request), "mapping_save", mapping.name)
        return Response("", headers=_TRIGGER)

    async def mapping_delete(request):
        mid = request.path_params["mid"]
        removed = None
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if m:
                state.config.mappings.remove(m)
                await state.asave()
                removed = m
        if removed is not None:
            await state.supervisor.remove_mapping(mid)
            state.log(f"mapping deleted: {removed.name}")
            state.audit(client_ip(request), "mapping_delete", removed.name)
            state.delete_mapping_log(mid)
        return Response("", headers=_TRIGGER)

    async def mapping_action(request):
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse("Unknown action", status_code=400)
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if not m:
                return PlainTextResponse("Mapping not found", status_code=404)
            m.enabled = action != "stop"
            await state.asave()
        if action == "stop":
            await state.supervisor.stop_mapping(mid)
        elif action == "restart":
            await state.supervisor.restart_mapping(m)
        else:  # start
            await state.supervisor.apply_mapping(m)
        state.log(f"mapping {action}: {m.name}")
        state.audit(client_ip(request), f"mapping_{action}", m.name)
        return Response("", headers=_TRIGGER)

    routes = [
        Route("/", dashboard),
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout_post, methods=["POST"]),
        Route("/setup", setup_get, methods=["GET"]),
        Route("/setup", setup_post, methods=["POST"]),
        Route("/settings", settings_get, methods=["GET"]),
        Route("/settings/password", settings_password_post, methods=["POST"]),
        Route("/settings/tls", settings_tls_post, methods=["POST"]),
        Route("/settings/tls/generate", settings_tls_generate, methods=["POST"]),
        Route("/healthz", healthz),
        Route("/metrics", metrics),
        Route("/settings/config/export", config_export),
        Route("/settings/config/import", config_import, methods=["POST"]),
        Route("/api/mappings/{mid}/duplicate", mapping_duplicate, methods=["POST"]),
        WebSocketRoute("/api/mappings/{mid}/console", console_ws),
        Route("/api/status", api_status),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/refresh", api_ports_refresh, methods=["POST"]),
        Route("/api/ports/table", api_ports_table),
        Route("/api/mappings/form", mapping_form_new),
        Route("/api/mappings/{mid}/form", mapping_form_edit),
        Route("/api/mappings/{mid}/log", mapping_log),
        Route("/api/mappings/{mid}/console-view", console_view),
        Route("/api/mappings/save", mapping_save, methods=["POST"]),
        Route("/api/mappings/{mid}", mapping_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/{action}", mapping_action, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=static_dir), name="static"),
    ]
    return routes


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _metric_label(name: str) -> str:
    """Escape a mapping name for a Prometheus label value."""
    return name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Monitor:
    """A browser console observer: serial traffic is pushed here (best-effort —
    drops oldest on overflow so a slow viewer never stalls the bridge)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2048)

    def feed(self, data: bytes) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            with contextlib.suppress(Exception):
                self.queue.get_nowait()
                self.queue.put_nowait(data)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.queue.put_nowait(None)  # sentinel: tells the sender to close


def _password_problem(pw: str, pw2: str) -> str | None:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "Passwords do not match."
    return None


def _checkbox(form, name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def _split(s: str) -> list:
    return [x for x in re.split(r"[\s,]+", s or "") if x]


def _num(form, name, default, *, strict, cast=int):
    v = form.get(name, default)
    if strict:
        return cast(v)
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _serial_dict(form, prefix: str, strict: bool) -> dict:
    baud = form.get(prefix + "baudrate", "9600")
    if baud == "custom":
        baud = form.get(prefix + "custom_baud", "").strip()
    return {
        "port": form.get(prefix + "port", "").strip(),
        "baudrate": (int(baud) if strict else _num({"b": baud}, "b", 9600, strict=False)),
        "bytesize": _num(form, prefix + "bytesize", 8, strict=strict),
        "parity": form.get(prefix + "parity", "N"),
        "stopbits": _num(form, prefix + "stopbits", 1, strict=strict, cast=float),
        "flowcontrol": form.get(prefix + "flowcontrol", "none"),
        "rts_on_open": form.get(prefix + "rts_on_open", "keep"),
        "dtr_on_open": form.get(prefix + "dtr_on_open", "keep"),
        "exclusive": _checkbox(form, prefix + "exclusive"),
    }


def _build_mapping_dict(form, strict: bool) -> dict:
    bind = form.get("network_bind_ip", "0.0.0.0")
    if bind == "custom":
        bind = form.get("network_bind_ip_custom", "").strip()
    return {
        "id": form.get("id") or None,
        "name": (form.get("name", "").strip() if strict else form.get("name", "")),
        "enabled": _checkbox(form, "enabled"),
        "kind": form.get("kind", "net"),
        "serial": _serial_dict(form, "serial_", strict),
        "serial_b": _serial_dict(form, "serial_b_", strict),
        "network": {
            "mode": form.get("network_mode", "server"),
            "protocol": form.get("network_protocol", "raw"),
            "bind_ip": bind or "0.0.0.0",
            "port": _num(form, "network_port", 4001, strict=strict),
            "remote_host": form.get("network_remote_host", "").strip(),
            "remote_port": (_num(form, "network_remote_port", 0, strict=strict)
                            if form.get("network_remote_port") else 0),
            "max_connections": _num(form, "network_max_connections", 1, strict=strict),
            "kick_old_user": _checkbox(form, "network_kick_old_user"),
            "read_only": _checkbox(form, "network_read_only"),
            "allowed_client_ips": _split(form.get("network_allowed_client_ips", "")),
            "priority_client_ips": _split(form.get("network_priority_client_ips", "")),
            "client_queue_max": _num(form, "network_client_queue_max", 2048, strict=strict),
            "tls": _checkbox(form, "network_tls"),
            "tls_cert": form.get("network_tls_cert", "").strip(),
            "tls_key": form.get("network_tls_key", "").strip(),
        },
        "options": {
            "banner": form.get("opt_banner", ""),
            "idle_timeout_s": _num(form, "opt_idle_timeout_s", 0, strict=strict),
            "closeon": form.get("opt_closeon", ""),
            "trace_both": form.get("opt_trace_both", "").strip(),
            "trace_hexdump": _checkbox(form, "opt_trace_hexdump"),
        },
    }


def _mapping_from_form(form) -> dict:
    return _build_mapping_dict(form, strict=True)


def _preserve_unmanaged_fields(new_map, existing) -> None:
    """Carry over config fields the mapping form does not expose, so editing an
    existing mapping never silently discards them: the stable-id `match`, the
    advanced/RS-485 serial settings, open/close strings, the trace-timestamp flag
    and the RFC2217 interop knobs. No-op when creating a new mapping."""
    if existing is None:
        return
    for cur, old in ((new_map.serial, existing.serial),
                     (new_map.serial_b, existing.serial_b)):
        cur.match = old.match
        cur.advanced = old.advanced
    o, prev = new_map.options, existing.options
    o.openstr = prev.openstr
    o.closestr = prev.closestr
    o.trace_timestamp = prev.trace_timestamp
    o.rfc2217_poll_modem_interval_s = prev.rfc2217_poll_modem_interval_s
    o.rfc2217_net_timeout_s = prev.rfc2217_net_timeout_s


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner."""
    from ..engine import netinfo
    try:
        mapping = MappingConfig.from_dict(_build_mapping_dict(form, strict=False))
    except Exception:
        mapping = MappingConfig(name=form.get("name", ""))
    is_new = not form.get("id")
    return render(request, "_mapping_form.html", status_code=400,
                  mapping=mapping, is_new=is_new,
                  ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                  error=message)
