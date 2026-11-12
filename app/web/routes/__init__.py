"""HTTP routes: pages (login/setup/dashboard/settings) + HTMX/JSON API.

Interaction model (HTMX, no SPA):
  - the mappings panel polls GET /api/status every 2s and also refreshes when a
    response sets the `HX-Trigger: refreshMappings` header,
  - add/edit load a server-rendered form fragment into #form-panel,
  - save/delete/start/stop return an empty body + the refresh trigger.

One module per area; each exposes ``routes(ctx)``:
  login      sign-in, logout, first-run setup, OIDC SSO
  settings   the Settings page and its forms, mappings export/import
  mappings   dashboard, mapping table/form/actions, log and console panels
  console    the console and hex-view WebSockets, the console's serial BREAK
  cluster    LAN cluster view, remote control and remote edit
  system     /healthz, /metrics, serial port list
Shared helpers live in common (Ctx), forms (form parsing) and ops (mapping operations).
"""
from __future__ import annotations

from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from . import cluster, console, login, mappings, settings, system
from .common import Ctx
from .forms import _build_mapping_dict, _preserve_unmanaged_fields

__all__ = ["build_routes", "_build_mapping_dict", "_preserve_unmanaged_fields"]


def build_routes(templates, state, static_dir) -> list:
    from ..api import build_api_routes  # here: api imports .routes.ops (no import cycle)

    ctx = Ctx(templates, state)
    return [
        *login.routes(ctx),
        *settings.routes(ctx),
        # JSON REST API (bearer-token auth, enforced in GuardMiddleware)
        *build_api_routes(state),
        # before mappings: its POST /api/mappings/{mid}/{action} would take .../break
        *console.routes(ctx),
        *mappings.routes(ctx),
        *cluster.routes(ctx),
        *system.routes(ctx),
        Mount("/static", app=StaticFiles(directory=static_dir), name="static"),
    ]
