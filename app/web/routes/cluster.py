"""LAN cluster: the unified fleet view, remote start/stop/restart and remote edit.

Two kinds of endpoint live here. Peer-facing ones (``/api/cluster/local``,
``/control``, ``/mapping-data``, ``/mapping-save``) are called by OTHER nodes and are
authenticated by the shared cluster key, not a session. Browser-facing ones (status,
``control-peer``, ``peer-form``, ``peer-save``) need a signed-in operator and proxy to
a peer that must be on the known-address allowlist.
"""
from __future__ import annotations

import asyncio
import hmac
import socket
import time

from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from ... import __version__
from ...config import MappingConfig
from ...engine import netinfo, portlist
from . import ops
from .common import Ctx
from .forms import _build_mapping_dict, _modbus_points_json, _preserve_unmanaged_fields


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


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
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
        (data is None when the peer is unreachable)."""
        base = {"self": False, "source": target.get("source", "auto"),
                "ip": target["ip"], "port": target["port"], "scheme": target["scheme"]}
        if not data:
            return {**base, "online": False, "id": target.get("id", ""),
                    "name": target.get("name", f"{target['ip']}:{target['port']}"),
                    "uptime": "", "version": "", "mapping_count": 0, "running_count": 0,
                    "mappings": []}
        return {**base, "online": True, "id": data.get("id", target.get("id", "")),
                "name": data.get("name", base["ip"]),
                "ip": data.get("ip", base["ip"]), "port": data.get("port", base["port"]),
                "scheme": data.get("scheme", base["scheme"]),
                "uptime": _fmt_uptime(data.get("uptime_s", 0)), "version": data.get("version", ""),
                "mapping_count": data.get("mapping_count", len(data.get("mappings", []))),
                "running_count": data.get("running_count", 0),
                "mappings": data.get("mappings", [])}

    async def cluster_status(request):
        """Aggregated view for the browser: local node + every discovered/manual peer,
        fetched server-side with the shared key. Renders the unified table."""
        if not state.config.cluster.active:
            return render(request, "_cluster_body.html", nodes=[], can_edit=can_edit(request),
                          discovery_error="")
        local = local_node_payload()
        nodes = [{**local, "self": True, "online": True, "source": "self",
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
        The peer is validated against the known-address allowlist (so the browser can't
        aim this at an arbitrary host), then the call is made with the shared key."""
        if deny := require_peer_access(request):
            return deny
        form = await request.form()
        scheme = form.get("scheme", "http")
        host, action = form.get("host", ""), form.get("action", "")
        mid = form.get("mid", "")
        try:
            port = int(form.get("port", "0"))
        except ValueError:
            return PlainTextResponse("Bad port", status_code=400)
        if action not in ("start", "stop", "restart"):
            return PlainTextResponse("Unknown action", status_code=400)
        if (host, port) not in state.cluster.known_addresses():
            return PlainTextResponse("Unknown cluster peer", status_code=403)
        res = await state.cluster.control_peer(scheme, host, port, mid, action)
        if not res or not res.get("ok"):
            return PlainTextResponse("Peer did not accept the command "
                                     "(unreachable or rejected).", status_code=502)
        state.log(f"cluster: sent {action} to {host}:{port} for '{res.get('name', mid)}'")
        ctx.audit(request, f"cluster_control_{action}", f"{host}:{port}/{res.get('name', mid)}")
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

    def _peer_from_request(getter):
        """(scheme, host, port) from a request, validated against the peer allowlist.
        Returns (tuple, None) on success or (None, error-response)."""
        scheme, host = getter("scheme", "http"), getter("host", "")
        try:
            port = int(getter("port", "0"))
        except ValueError:
            return None, PlainTextResponse("Bad peer port", status_code=400)
        if (host, port) not in state.cluster.known_addresses():
            return None, PlainTextResponse("Unknown cluster peer", status_code=403)
        return (scheme, host, port), None

    async def cluster_peer_form(request):
        """Browser-facing proxy: load a peer's mapping into the edit form (rendered
        here so the CSRF token is the browser's). Submits to /api/cluster/peer-save."""
        if deny := require_peer_access(request):
            return deny
        peer, err = _peer_from_request(request.query_params.get)
        if err:
            return err
        scheme, host, port = peer
        mid = request.query_params.get("mid", "")
        data = await state.cluster.get_peer(scheme, host, port, f"/api/cluster/mapping-data?mid={mid}")
        if not data or "mapping" not in data:
            return PlainTextResponse("Could not load the peer's mapping.", status_code=502)
        mapping = MappingConfig.from_dict(data["mapping"])
        return render(request, "_mapping_form.html", mapping=mapping, is_new=False,
                      modbus_points_json=_modbus_points_json(mapping),
                      ports=portlist.visible(data.get("ports", [])), ips=data.get("ips", []),
                      remote={"scheme": scheme, "host": host, "port": port}, error=None)

    async def cluster_peer_save(request):
        """Browser-facing proxy: forward an edited mapping form to the peer's key-guarded
        save (peer fields stripped); on a peer error, re-render the form with it."""
        if deny := require_peer_access(request):
            return deny
        form = await request.form()
        peer, err = _peer_from_request(form.get)
        if err:
            return err
        scheme, host, port = peer
        skip = {"_peer_scheme", "_peer_host", "_peer_port", "_csrf", "scheme", "host", "port"}
        fields = [(k, v) for k, v in form.multi_items() if k not in skip]
        res = await state.cluster.post_peer(scheme, host, port, "/api/cluster/mapping-save", fields)
        if res and res.get("ok"):
            state.log(f"cluster: edited '{res.get('name', '')}' on {host}:{port}")
            ctx.audit(request, "cluster_edit", f"{host}:{port}/{res.get('name', '')}")
            return Response("", headers={"HX-Trigger": "refreshCluster"})
        msg = (res or {}).get("error") or "The peer rejected the change or was unreachable."
        data = await state.cluster.get_peer(scheme, host, port,
                                            f"/api/cluster/mapping-data?mid={form.get('id', '')}")
        try:
            mapping = MappingConfig.from_dict(_build_mapping_dict(form, strict=False))
            if data and data.get("mapping"):
                _preserve_unmanaged_fields(mapping, MappingConfig.from_dict(data["mapping"]), form)
        except Exception:
            mapping = MappingConfig(name=form.get("name", ""))
        return render(request, "_mapping_form.html", status_code=400, mapping=mapping, is_new=False,
                      modbus_points_json=_modbus_points_json(mapping),
                      ports=portlist.visible((data or {}).get("ports", [])), ips=(data or {}).get("ips", []),
                      remote={"scheme": scheme, "host": host, "port": port}, error=msg)

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
