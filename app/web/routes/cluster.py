"""LAN cluster: the unified fleet view, remote start/stop/restart and remote edit.

Two kinds of endpoint live here. Peer-facing ones (``/api/cluster/local``,
``/control``, ``/mapping-data``, ``/mapping-save``) are called by OTHER nodes and are
authenticated by the shared cluster key, not a session. Browser-facing ones (status,
``control-peer``, ``peer-form``, ``peer-save``) need a signed-in operator and proxy to
a peer the browser names by host + port: it must be a discovered or configured peer,
and the call goes to that peer's own address and scheme.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import re
import socket
import time

from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from ... import __version__
from ...config import ConfigError, MappingConfig, peer_host, trace_file_name
from ...engine import netinfo, portlist
from ...engine.cluster import peer_addr, peer_port, peer_url
from ...i18n import _
from .. import auth
from . import ops
from .common import Ctx, csrf_failed
from .forms import _build_mapping_dict, _modbus_points_json, _preserve_unmanaged_fields

# The mapping form's own field names (bare ones + these prefixes). cluster_peer_save
# forwards only these to a peer, so a value the browser adds beyond the form is never
# relayed with the shared key.
_PEER_FORM_FIELDS = {"id", "name", "kind", "enabled"}
_PEER_FORM_PREFIXES = ("serial_", "network_", "opt_", "mqtt_", "modbus_")


def _peer_below_2_8(version: str) -> bool:
    """True when a peer's reported version is older than 2.8 (or unknown): such a node
    stores a trace file name as a path and opens it, so it must not be handed a new one."""
    parts = re.findall(r"\d+", version or "")
    try:
        return (int(parts[0]), int(parts[1])) < (2, 8)
    except (IndexError, ValueError):
        return True


def _peer_save_fields(form, peer_mapping, peer_version):
    """The fields cluster_peer_save forwards to a peer's key-guarded mapping-save, or an
    error to show instead. Only the known mapping-form fields are forwarded (never a raw
    value the browser added), the submission is validated locally (so a URL serial port
    never reaches the peer), and the trace file is reduced to a sanitised name — kept as
    the peer stored it when unchanged, and refused as a new name for a pre-2.8 peer.
    Returns (fields, None) or (None, error)."""
    if not peer_mapping:
        return None, _("This mapping no longer exists on the peer; reload the fleet view.")
    try:
        MappingConfig.from_dict(_build_mapping_dict(form, strict=False)).validate_input()
    except (ValueError, ConfigError) as e:
        return None, str(e)
    peer_raw = str(((peer_mapping.get("options") or {}).get("trace_both")) or "").strip()
    submitted = form.get("opt_trace_both", "").strip()
    if submitted == trace_file_name(peer_raw):
        out_trace = peer_raw  # unchanged: keep the peer's own stored value (legacy path too)
    else:
        out_trace = trace_file_name(submitted)
        if out_trace and _peer_below_2_8(peer_version):
            return None, _("This cluster node is older than 2.8 and would store a trace file "
                           "name as a path. Upgrade it to 2.8 or newer before setting a trace "
                           "file for it here.")
    fields = [(k, v) for k, v in form.multi_items()
              if k != "opt_trace_both"
              and (k in _PEER_FORM_FIELDS or k.startswith(_PEER_FORM_PREFIXES))]
    fields.append(("opt_trace_both", out_trace))
    return fields, None


def _remote(target, peer_id, mapping) -> dict:
    """The fleet view's edit form for a peer's mapping: the peer's host + port it posts
    back (resolved again on save), the address it shows, and the id it is saved under.
    That is the id the peer itself keys the mapping by, sent back to it unchanged: a
    v2.7/v2.8 peer may keep an id that this node would not take (``mapping`` carries the
    id derived from it here, which that peer has never heard of). This node uses it only
    to address the mapping on the peer, never for a file or path of its own."""
    if not isinstance(peer_id, str) or not peer_id:
        peer_id = mapping.id
    return {"host": target["ip"], "port": target["port"],
            "addr": peer_addr(target["ip"], target["port"]), "id": peer_id}


def _mapping_kind_label(m) -> str:
    if m.kind == "serialbridge":
        return "serial↔serial"
    return f"{m.network.mode}/{m.network.protocol}"


def _mapping_endpoint(m) -> str:
    if m.kind == "serialbridge":
        return f"{m.serial.port} ↔ {m.serial_b.port}"
    if m.network.mode == "client":
        return f"→ {m.network.remote_host}:{m.network.remote_port}"
    return f"{m.network.bind_ip}:{m.network.port}"


def _fmt_uptime(seconds) -> str:
    try:
        s = int(max(0, seconds))
    except (TypeError, ValueError):
        return ""
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _sec = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"



def routes(ctx: Ctx) -> list:
    state, render, client_ip = ctx.state, ctx.render, ctx.client_ip
    can_edit, is_anonymous = ctx.can_edit, ctx.is_anonymous
    require_peer_access = ctx.require_peer_access

    # ---------------- LAN cluster (federated view + remote control) ----------------
    def local_node_payload() -> dict:
        """This node's identity, health and a status row per mapping — the unit other
        nodes aggregate. No secrets: just what the unified dashboard table shows."""
        cfg = state.config
        ip, port, scheme = state.cluster.advertised()
        statuses = state.supervisor.all_status()
        maps, running = [], 0
        for m in cfg.mappings:
            st = statuses.get(m.id) or {}
            if st.get("state") == "running":
                running += 1
            maps.append({
                "id": m.id, "name": m.name, "enabled": m.enabled,
                "kind": _mapping_kind_label(m), "endpoint": _mapping_endpoint(m),
                "serial": m.serial.port,
                "state": st.get("state", "stopped"),
                "client_count": st.get("client_count", 0),
                "bytes_in": st.get("bytes_in", 0), "bytes_out": st.get("bytes_out", 0),
            })
        return {"id": cfg.instance_id, "name": socket.gethostname(),
                "ip": ip, "port": port, "scheme": scheme,
                "version": __version__, "uptime_s": int(time.time() - state.started_at),
                "mapping_count": len(maps), "running_count": running, "mappings": maps}

    async def cluster_local(request):
        """Peer-facing: returns this node's mappings to another cluster node that
        presents the matching shared key. Guarded by the key, not a user session
        (added to PUBLIC_PATHS) — read-only, exposes no credentials."""
        cfg = state.config
        key = request.headers.get("x-cluster-key", "")
        if not (cfg.cluster.active and key and hmac.compare_digest(key, cfg.cluster.key)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return JSONResponse(local_node_payload())

    def _node_from(target, data) -> dict:
        """Build a fleet-table node entry from a fetch target + the peer's payload
        (data is None when the peer is unreachable). The address shown, the Open link
        and the actions are the target's - where this node found the peer: its beacon
        or its manual entry - never the address the peer reports for itself. That one
        differs behind NAT or Docker, for a peer entered by name and for one that could
        not tell its LAN address; it is shown only as information."""
        ip, port, scheme = target["ip"], target["port"], target["scheme"]
        base = {"self": False, "source": target.get("source", "auto"),
                "ip": ip, "port": port, "scheme": scheme, "addr": peer_addr(ip, port)}
        with contextlib.suppress(ValueError):
            base["url"] = peer_url(scheme, ip, port)
        if not data:
            return {**base, "online": False, "id": target.get("id", ""),
                    "name": target.get("name", base["addr"]),
                    "uptime": "", "version": "", "mapping_count": 0, "running_count": 0,
                    "mappings": []}
        maps = data.get("mappings")
        maps = [m for m in maps if isinstance(m, dict)] if isinstance(maps, list) else []
        own_ip, own_port = peer_host(data.get("ip", "")), peer_port(data.get("port"))
        if own_ip and own_port and peer_addr(own_ip, own_port) != base["addr"]:
            base["reported"] = peer_addr(own_ip, own_port)
        return {**base, "online": True, "id": data.get("id", target.get("id", "")),
                "name": data.get("name", base["addr"]),
                "uptime": _fmt_uptime(data.get("uptime_s", 0)), "version": data.get("version", ""),
                "mapping_count": data.get("mapping_count", len(maps)),
                "running_count": data.get("running_count", 0), "mappings": maps}

    async def cluster_status(request):
        """Aggregated view for the browser: local node + every discovered/manual peer,
        fetched server-side with the shared key. Renders the unified table."""
        if not state.config.cluster.active:
            return render(request, "_cluster_body.html", nodes=[], can_edit=can_edit(request),
                          discovery_error="")
        local = local_node_payload()
        nodes = [{**local, "self": True, "online": True, "source": "self",
                  "addr": peer_addr(local["ip"], local["port"]),
                  "uptime": _fmt_uptime(local["uptime_s"])}]
        if is_anonymous(request):  # other nodes' data needs a real login here
            return render(request, "_cluster_body.html", nodes=nodes, can_edit=False,
                          discovery_error="", peers_hidden=True)
        targets = state.cluster.all_targets()
        results = await asyncio.gather(*[state.cluster.fetch_peer(t) for t in targets]) \
            if targets else []
        for t, data in zip(targets, results, strict=False):
            nodes.append(_node_from(t, data))
        return render(request, "_cluster_body.html", nodes=nodes, can_edit=can_edit(request),
                      discovery_error=state.cluster.discovery_error)

    async def cluster_control(request):
        """Peer-facing remote control: another cluster node (presenting the shared key)
        asks us to start/stop/restart one of OUR mappings. Key-guarded, no session."""
        cfg = state.config
        key = request.headers.get("x-cluster-key", "")
        if not (cfg.cluster.active and key and hmac.compare_digest(key, cfg.cluster.key)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        form = await request.form()
        mid, action = form.get("mapping_id", ""), form.get("action", "")
        if action not in ("start", "stop", "restart"):
            return JSONResponse({"error": "bad_action"}, status_code=400)
        ok, name = await ops.apply_action(state, mid, action)
        if not ok:
            return JSONResponse({"error": name}, status_code=404)
        ip = client_ip(request)
        state.log(f"cluster: remote {action} of '{name}' from {ip}")
        state.audit(ip, f"cluster_remote_{action}", name, user="cluster-peer")
        return JSONResponse({"ok": True, "name": name})

    async def cluster_control_peer(request):
        """Browser-facing proxy: an operator on THIS node controls a mapping on a peer.
        The peer must be a discovered or configured one (so the browser can't aim this
        at an arbitrary host); the call goes to its address with the shared key."""
        if deny := require_peer_access(request):
            return deny
        form = await request.form()
        action, mid = form.get("action", ""), form.get("mid", "")
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse(_("Unknown action"), status_code=400)
        target, err = _peer_target(form.get)
        if err:
            return err
        res = await state.cluster.control_peer(target, mid, action)
        if not res or not res.get("ok"):
            return PlainTextResponse(_("Peer did not accept the command "
                                       "(unreachable or rejected)."), status_code=502)
        addr = peer_addr(target["ip"], target["port"])
        state.log(f"cluster: sent {action} to {addr} for '{res.get('name', mid)}'")
        ctx.audit(request, f"cluster_control_{action}", f"{addr}/{res.get('name', mid)}")
        return Response("", headers={"HX-Trigger": "refreshCluster"})

    # ---- remote EDIT: peer renders no UI; THIS node renders the form (so the CSRF
    #      token + form action belong to the browser's session) and proxies the save.
    async def cluster_mapping_data(request):
        """Peer-facing: a mapping's full config + this node's serial ports + IP
        candidates, so the controlling node can render the edit form. Key-guarded."""
        cfg = state.config
        key = request.headers.get("x-cluster-key", "")
        if not (cfg.cluster.active and key and hmac.compare_digest(key, cfg.cluster.key)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        m = cfg.get_mapping(request.query_params.get("mid", ""))
        if not m:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse({"mapping": m.to_dict(), "ports": state.ports.get(),
                             "ips": netinfo.list_ip_candidates()})

    async def cluster_mapping_save(request):
        """Peer-facing: validate + persist + apply an edited mapping on THIS node, run
        from the same form the controlling node rendered. Key-guarded."""
        cfg = state.config
        key = request.headers.get("x-cluster-key", "")
        if not (cfg.cluster.active and key and hmac.compare_digest(key, cfg.cluster.key)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        form = await request.form()
        ok, msg = await ops.save_mapping_from_form(state, form)
        if not ok:
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        ip = client_ip(request)
        state.log(f"cluster: remote edit of '{msg}' from {ip}")
        state.audit(ip, "cluster_remote_save", msg, user="cluster-peer")
        return JSONResponse({"ok": True, "name": msg})

    def _peer_target(getter):
        """The peer a browser request names by host + port (as the fleet view shows them)
        — a discovered or configured one — or (None, error response). The address and
        scheme used, and so where the shared key goes, are that peer's own; nothing else
        in the request picks them."""
        try:
            port = int(getter("port", "0"))
        except (TypeError, ValueError):
            return None, PlainTextResponse(_("Bad peer port"), status_code=400)
        target = state.cluster.find_target(getter("host", ""), port)
        if target is None:
            return None, PlainTextResponse(_("Unknown cluster peer"), status_code=403)
        return target, None

    async def cluster_peer_form(request):
        """Browser-facing proxy: load a peer's mapping into the edit form (rendered
        here so the CSRF token is the browser's). Submits to /api/cluster/peer-save."""
        if deny := require_peer_access(request):
            return deny
        # A GET, so the middleware's CSRF check doesn't cover it; the Edit button (htmx)
        # sends the token header, a link from another site can't — so such a link can't
        # make this node call a peer with the key.
        if not auth.csrf_token_matches(request, request.headers.get("x-csrf-token")):
            return csrf_failed()
        target, err = _peer_target(request.query_params.get)
        if err:
            return err
        data = await state.cluster.get_peer(target, "/api/cluster/mapping-data",
                                            {"mid": request.query_params.get("mid", "")})
        if not data or not isinstance(data.get("mapping"), dict):
            return PlainTextResponse(_("Could not load the peer's mapping."), status_code=502)
        mapping = MappingConfig.from_dict(data["mapping"])
        return render(request, "_mapping_form.html", mapping=mapping, is_new=False,
                      modbus_points_json=_modbus_points_json(mapping),
                      ports=portlist.visible(data.get("ports", [])), ips=data.get("ips", []),
                      remote=_remote(target, data["mapping"].get("id"), mapping), error=None)

    async def cluster_peer_save(request):
        """Browser-facing proxy: forward an edited mapping to the peer's key-guarded save.
        Only the known mapping-form fields are relayed, validated locally first, and the
        trace file is reduced to a sanitised name (see _peer_save_fields); the mapping must
        already exist on the peer. On a peer or validation error, re-render the form."""
        if deny := require_peer_access(request):
            return deny
        form = await request.form()
        target, err = _peer_target(form.get)
        if err:
            return err

        # Fetch the peer's current mapping (for the stored trace value + to confirm it
        # exists — the fleet view only edits) and its version, concurrently.
        data, local = await asyncio.gather(
            state.cluster.get_peer(target, "/api/cluster/mapping-data",
                                   {"mid": form.get("id", "")}),
            state.cluster.get_peer(target, "/api/cluster/local"))

        def _reload(msg):
            try:
                mapping = MappingConfig.from_dict(_build_mapping_dict(form, strict=False))
                if data and data.get("mapping"):
                    _preserve_unmanaged_fields(mapping, MappingConfig.from_dict(data["mapping"]), form)
            except Exception:
                mapping = MappingConfig(name=form.get("name", ""))
            return render(request, "_mapping_form.html", status_code=400, mapping=mapping,
                          is_new=False, modbus_points_json=_modbus_points_json(mapping),
                          ports=portlist.visible((data or {}).get("ports", [])),
                          ips=(data or {}).get("ips", []),
                          remote=_remote(target, form.get("id", ""), mapping), error=msg)

        if not data:
            return _reload(_("The peer rejected the change or was unreachable."))
        fields, ferr = _peer_save_fields(form, data.get("mapping"),
                                         (local or {}).get("version", ""))
        if ferr:
            return _reload(ferr)
        res = await state.cluster.post_peer(target, "/api/cluster/mapping-save", fields)
        if res and res.get("ok"):
            addr = peer_addr(target["ip"], target["port"])
            state.log(f"cluster: edited '{res.get('name', '')}' on {addr}")
            ctx.audit(request, "cluster_edit", f"{addr}/{res.get('name', '')}")
            return Response("", headers={"HX-Trigger": "refreshCluster"})
        return _reload((res or {}).get("error") or _("The peer rejected the change or was "
                                                     "unreachable."))

    return [
        Route("/api/cluster/status", cluster_status),
        Route("/api/cluster/local", cluster_local),
        Route("/api/cluster/control", cluster_control, methods=["POST"]),
        Route("/api/cluster/control-peer", cluster_control_peer, methods=["POST"]),
        Route("/api/cluster/mapping-data", cluster_mapping_data),
        Route("/api/cluster/mapping-save", cluster_mapping_save, methods=["POST"]),
        Route("/api/cluster/peer-form", cluster_peer_form),
        Route("/api/cluster/peer-save", cluster_peer_save, methods=["POST"]),
    ]
