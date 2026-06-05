"""HTTP routes: pages (login/setup/dashboard/settings) + HTMX/JSON API.

Interaction model (HTMX, no SPA):
  - the mappings panel polls GET /api/status every 2s and also refreshes when a
    response sets the `HX-Trigger: refreshMappings` header,
  - add/edit load a server-rendered form fragment into #form-panel,
  - save/delete/start/stop return an empty body + the refresh trigger.
"""
from __future__ import annotations

import re

from starlette.responses import (
    JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from starlette.routing import Mount, Route
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
        return request.client.host if request.client else "?"

    def set_session(response, request):
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(state.config.secret_key, SESSION_TTL),
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
        if state.config.password_set and auth.verify_password(password, state.config.password_hash):
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
        async with state.config_lock:
            state.config.password_hash = auth.hash_password(pw)
            state.save()
        state.log("admin password set (first-run setup complete)")
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
        if not auth.verify_password(cur, state.config.password_hash):
            return await settings_get(request, error="Current password is incorrect.")
        err = _password_problem(new, new2)
        if err:
            return await settings_get(request, error=err)
        async with state.config_lock:
            state.config.password_hash = auth.hash_password(new)
            state.save()
        state.log("admin password changed")
        return await settings_get(request, ok="Password updated.")

    # ---------------- status / ports / ips ----------------
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
        return render(request, "_mapping_log.html", mid=mid, mapping_name=m.name,
                      lines=state.read_mapping_log(mid, limit=1000))

    async def mapping_save(request):
        form = await request.form()
        try:
            data = _mapping_from_form(form)
            mapping = MappingConfig.from_dict(data)
            mapping.validate()
        except (ValueError, ConfigError) as e:
            return _form_error(render, request, state, form, str(e))

        async with state.config_lock:
            backup = list(state.config.mappings)
            existing = state.config.get_mapping(mapping.id)
            if existing:
                state.config.mappings[state.config.mappings.index(existing)] = mapping
            else:
                state.config.mappings.append(mapping)
            try:
                state.save()
            except ConfigError as e:
                state.config.mappings = backup
                return _form_error(render, request, state, form, str(e))
            await state.supervisor.apply_mapping(mapping)
        state.log(f"mapping saved: {mapping.name}")
        return Response("", headers=_TRIGGER)

    async def mapping_delete(request):
        mid = request.path_params["mid"]
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if m:
                state.config.mappings.remove(m)
                state.save()
                await state.supervisor.remove_mapping(mid)
                state.log(f"mapping deleted: {m.name}")
                state.delete_mapping_log(mid)
        return Response("", headers=_TRIGGER)

    async def mapping_action(request):
        mid = request.path_params["mid"]
        action = request.path_params["action"]
        async with state.config_lock:
            m = state.config.get_mapping(mid)
            if not m:
                return PlainTextResponse("Mapping not found", status_code=404)
            if action == "start":
                m.enabled = True
                state.save()
                await state.supervisor.apply_mapping(m)
            elif action == "stop":
                m.enabled = False
                state.save()
                await state.supervisor.stop_mapping(mid)
            elif action == "restart":
                m.enabled = True
                state.save()
                await state.supervisor.restart_mapping(m)
            else:
                return PlainTextResponse("Unknown action", status_code=400)
        state.log(f"mapping {action}: {m.name}")
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
        Route("/healthz", healthz),
        Route("/api/status", api_status),
        Route("/api/ports.json", api_ports_json),
        Route("/api/ports/refresh", api_ports_refresh, methods=["POST"]),
        Route("/api/ports/table", api_ports_table),
        Route("/api/mappings/form", mapping_form_new),
        Route("/api/mappings/{mid}/form", mapping_form_edit),
        Route("/api/mappings/{mid}/log", mapping_log),
        Route("/api/mappings/save", mapping_save, methods=["POST"]),
        Route("/api/mappings/{mid}", mapping_delete, methods=["DELETE"]),
        Route("/api/mappings/{mid}/{action}", mapping_action, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=static_dir), name="static"),
    ]
    return routes


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _password_problem(pw: str, pw2: str) -> str | None:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "Passwords do not match."
    return None


def _checkbox(form, name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def _mapping_from_form(form) -> dict:
    baud = form.get("serial_baudrate", "9600")
    if baud == "custom":
        baud = form.get("serial_custom_baud", "").strip()
    bind = form.get("network_bind_ip", "0.0.0.0")
    if bind == "custom":
        bind = form.get("network_bind_ip_custom", "").strip()
    allowed = [x for x in re.split(r"[\s,]+", form.get("network_allowed_client_ips", "")) if x]
    priority = [x for x in re.split(r"[\s,]+", form.get("network_priority_client_ips", "")) if x]
    return {
        "id": form.get("id") or None,
        "name": form.get("name", "").strip(),
        "enabled": _checkbox(form, "enabled"),
        "serial": {
            "port": form.get("serial_port", "").strip(),
            "baudrate": int(baud),
            "bytesize": int(form.get("serial_bytesize", 8)),
            "parity": form.get("serial_parity", "N"),
            "stopbits": float(form.get("serial_stopbits", 1)),
            "flowcontrol": form.get("serial_flowcontrol", "none"),
            "rts_on_open": form.get("serial_rts_on_open", "keep"),
            "dtr_on_open": form.get("serial_dtr_on_open", "keep"),
            "exclusive": _checkbox(form, "serial_exclusive"),
        },
        "network": {
            "protocol": form.get("network_protocol", "raw"),
            "bind_ip": bind,
            "port": int(form.get("network_port", 4001)),
            "max_connections": int(form.get("network_max_connections", 1)),
            "kick_old_user": _checkbox(form, "network_kick_old_user"),
            "read_only": _checkbox(form, "network_read_only"),
            "allowed_client_ips": allowed,
            "priority_client_ips": priority,
        },
        "options": {
            "banner": form.get("opt_banner", ""),
            "idle_timeout_s": int(form.get("opt_idle_timeout_s", 0) or 0),
        },
    }


def _form_error(render, request, state, form, message: str):
    """Re-render the mapping form with the submitted values and an error banner."""
    from ..engine import netinfo
    # rebuild a MappingConfig-ish view from raw form so fields survive the round-trip
    try:
        mapping = MappingConfig.from_dict(_safe_form_dict(form))
    except Exception:
        mapping = MappingConfig(name=form.get("name", ""))
    is_new = not form.get("id")
    return render(request, "_mapping_form.html", status_code=400,
                  mapping=mapping, is_new=is_new,
                  ports=state.ports.get(), ips=netinfo.list_ip_candidates(),
                  error=message)


def _safe_form_dict(form) -> dict:
    """Best-effort form->dict that won't raise on bad numbers (for error redisplay)."""
    def _int(v, d):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    baud = form.get("serial_baudrate", "9600")
    if baud == "custom":
        baud = form.get("serial_custom_baud", "").strip()
    bind = form.get("network_bind_ip", "0.0.0.0")
    if bind == "custom":
        bind = form.get("network_bind_ip_custom", "").strip()
    return {
        "id": form.get("id") or None,
        "name": form.get("name", ""),
        "enabled": _checkbox(form, "enabled"),
        "serial": {
            "port": form.get("serial_port", ""),
            "baudrate": _int(baud, 9600),
            "bytesize": _int(form.get("serial_bytesize", 8), 8),
            "parity": form.get("serial_parity", "N"),
            "stopbits": float(_int(form.get("serial_stopbits", 1), 1)),
            "flowcontrol": form.get("serial_flowcontrol", "none"),
            "rts_on_open": form.get("serial_rts_on_open", "keep"),
            "dtr_on_open": form.get("serial_dtr_on_open", "keep"),
            "exclusive": _checkbox(form, "serial_exclusive"),
        },
        "network": {
            "protocol": form.get("network_protocol", "raw"),
            "bind_ip": bind or "0.0.0.0",
            "port": _int(form.get("network_port", 4001), 4001),
            "max_connections": _int(form.get("network_max_connections", 1), 1),
            "kick_old_user": _checkbox(form, "network_kick_old_user"),
            "read_only": _checkbox(form, "network_read_only"),
            "allowed_client_ips": [x for x in re.split(r"[\s,]+", form.get("network_allowed_client_ips", "")) if x],
            "priority_client_ips": [x for x in re.split(r"[\s,]+", form.get("network_priority_client_ips", "")) if x],
        },
        "options": {
            "banner": form.get("opt_banner", ""),
            "idle_timeout_s": _int(form.get("opt_idle_timeout_s", 0), 0),
        },
    }
