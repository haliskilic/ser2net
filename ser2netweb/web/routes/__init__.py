"""HTTP routes: pages (login/setup/dashboard/settings) + HTMX/JSON API.

Interaction model (HTMX, no SPA):
  - the mappings panel polls GET /api/status every 2s and also refreshes when a
    response sets the `HX-Trigger: refreshMappings` header,
  - add/edit load a server-rendered form fragment into #form-panel,
  - save/delete/start/stop return an empty body + the refresh trigger.

One module per area; each exposes ``routes(ctx)``:
  login      sign-in, logout, first-run setup, OIDC SSO
  totp       two-factor sign-in: the code step, setting it up in Settings
  settings   the Settings page and its forms, mappings export/import
  mappings   dashboard, mapping table/form/actions, log and console panels
  console    the console and hex-view WebSockets, the console's serial BREAK
  cluster    LAN cluster view, remote control and remote edit
  system     /healthz, /metrics, serial port list
Shared helpers live in common (Ctx), forms (form parsing) and ops (mapping operations).
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from starlette.routing import BaseRoute, Mount
from starlette.staticfiles import StaticFiles

from . import cluster, console, login, mappings, settings, system, totp
from .common import Ctx
from .forms import _build_mapping_dict, _preserve_unmanaged_fields

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.templating import Jinja2Templates
    from starlette.types import Scope

    from ...state import AppState

__all__ = ["build_routes", "_build_mapping_dict", "_preserve_unmanaged_fields"]

# a year: the longest lifetime HTTP caches honour
_IMMUTABLE = "public, max-age=31536000, immutable"


class StaticAssets(StaticFiles):
    """/static. The pages link every asset with ?v=<a hash of the static files>
    (server.asset_version()), so a request carrying the current one may be cached for good:
    the URL changes whenever an asset does. Others are revalidated as before."""

    def __init__(self, directory: str, version: str) -> None:
        super().__init__(directory=directory)
        self.version = version

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if response.status_code in (200, 304) and query.get("v") == [self.version]:
            response.headers["Cache-Control"] = _IMMUTABLE
        return response


def build_routes(templates: Jinja2Templates, state: AppState,
                 static_dir: str) -> list[BaseRoute]:
    from ..api import build_api_routes  # here: api imports .routes.ops (no import cycle)

    ctx = Ctx(templates, state)
    return [
        *login.routes(ctx),
        *totp.routes(ctx),
        *settings.routes(ctx),
        # JSON REST API (bearer-token auth, enforced in GuardMiddleware)
        *build_api_routes(state),
        # before mappings: its POST /api/mappings/{mid}/{action} would take .../break
        *console.routes(ctx),
        *mappings.routes(ctx),
        *cluster.routes(ctx),
        *system.routes(ctx),
        Mount("/static", app=StaticAssets(static_dir, str(templates.env.globals["asset_version"])),
              name="static"),
    ]
