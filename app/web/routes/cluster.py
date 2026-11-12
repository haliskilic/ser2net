"""LAN cluster: the unified fleet view, remote start/stop/restart and remote edit.

Two kinds of endpoint live here. Peer-facing ones (``/api/cluster/local``,
``/control``, ``/mapping-data``, ``/mapping-save``) are called by OTHER nodes and are
authenticated by the shared cluster key, not a session: a request signed with it (or,
from a node running 2.8.0 or older while legacy authentication is on, the raw key), and
their replies are signed for that request. Browser-facing ones (status, ``control-peer``,
``peer-form``, ``peer-save``) need a signed-in operator and proxy to a peer the
browser names by host + port: it must be a discovered or configured peer, and the
call goes to that peer's own address and scheme.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time

from markupsafe import escape
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from ... import __version__
from ...config import (
    ConfigError, MappingConfig, host_port, mapping_revision, node_label, peer_host, trace_file_name,
)
from ...engine import netinfo, portlist
from ...engine.cluster import (ACTOR_FIELD, ACTOR_MAX, ACTOR_NODE_FIELD, AUTH_HEADER, REFUSALS,
                               peer_addr, peer_port, peer_url)
from ...i18n import _
from .. import auth
from . import ops
from .common import Ctx, LiveTable, bytesize, csrf_failed, domid, notice, reason, start_failed
from .forms import (
    _TEXT_INPUTS, MQTT_PASSWORD_CLEAR, MQTT_PASSWORD_WRITE_ONLY, _build_mapping_dict,
    _parse_modbus_points, blocks_not_read, form_context, in_text_box, mapping_for_rerender,
    mqtt_password_from_form,
)

# The mapping form's own field names (bare ones + these prefixes). cluster_peer_save
# forwards only these to a peer, so a value the browser adds beyond the form is never
# relayed with the shared key.
_PEER_FORM_FIELDS = {"id", "name", "kind", "enabled", "rev"}
_PEER_FORM_PREFIXES = ("serial_", "network_", "opt_", "mqtt_", "modbus_")
# Not forwarded as posted: the MQTT password goes as the value to store (_peer_save_fields)
_PEER_PASSWORD_FIELDS = {"mqtt_password", MQTT_PASSWORD_WRITE_ONLY, MQTT_PASSWORD_CLEAR}
# A peer request's body (at most a mapping form) is far smaller; a larger one is refused
# (413) before it is read.
PEER_MAX_BODY = 1024 * 1024


# Fields a node before 2.8.1 sets from every form it saves (it has no presence marker
# for them), which this node's form leaves out for a Modbus gateway (it doesn't use
# them): forwarded with the peer's stored values, so the edit doesn't reset them there.
#   form field -> (config section, attribute)
_READ_BEFORE_2_8_1 = {
    "opt_banner": ("options", "banner"),
    "opt_closeon": ("options", "closeon"),
    "network_client_queue_max": ("network", "client_queue_max"),
}


def _peer_version(version) -> tuple[int, int, int] | None:
    """(2, 8, 1) for a peer's reported "2.8.1" ("v2.8.1", "2.8.1rc1" ...); None when it
    reports none (a node before 2.6.0) or something else."""
    parts = re.findall(r"\d+", version if isinstance(version, str) else "")
    if len(parts) < 2:
        return None
    major, minor, patch = (int(p) for p in (parts + ["0"])[:3])
    return major, minor, patch


def _peer_older(version, than: tuple[int, int, int]) -> bool:
    """True when a peer's reported version is older than *than*, or unknown."""
    v = _peer_version(version)
    return v is None or v < than


# What other nodes may ask this one to do with its mappings: its /api/cluster/local and
# mapping-data answers list them as "caps" (nodes before 2.9.0 send none, see peer_caps).
CAPS = ("control", "edit")


def peer_caps(payload) -> frozenset[str]:
    """What a peer can be asked to do, by its /api/cluster/local answer: its "caps" list
    (2.9.0+), else what its version could do: start/stop/restart ("control") since 2.6.0,
    edit since 2.6.1; a node before 2.6.0 reports no version and can only be shown."""
    if not isinstance(payload, dict):
        return frozenset()
    caps = payload.get("caps")
    if isinstance(caps, list):
        return frozenset(c for c in caps if isinstance(c, str))
    v = _peer_version(payload.get("version"))
    if v is None or v < (2, 6, 0):
        return frozenset()
    return frozenset({"control"} if v < (2, 6, 1) else {"control", "edit"})


def _version_label(version) -> str:
    """How a peer's version compares to this node's: "older", "newer" or ""."""
    v, mine = _peer_version(version), _peer_version(__version__)
    if v is None or mine is None or v == mine:
        return ""
    return "older" if v < mine else "newer"


def _leaves(d: dict, path: str = ""):
    """(path, value) of every setting in a mapping dict: "serial.advanced.nobreak",
    "modbus_poll.points[2].fn"; a USB identity (match) is one value."""
    for k, v in d.items():
        p = path + k
        if isinstance(v, dict) and k != "match":
            yield from _leaves(v, p + ".")
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for i, x in enumerate(v):
                yield from _leaves(x, f"{p}[{i}].")
        else:
            yield p, v


def not_kept(sent: MappingConfig, stored: dict) -> list[str]:
    """The settings of the mapping a peer stores after a fleet-view save (its mapping-data
    *stored*, in the peer's own schema) that differ from what this node sent (*sent*: the
    forwarded fields as this node's save parses them). Only settings the peer has are
    compared; the trace (stored as the peer's own value) and the id are left out."""
    mine = dict(_leaves(sent.to_dict()))
    theirs = dict(_leaves(MappingConfig.from_dict(stored).to_dict()))
    have = {p for p, _v in _leaves(stored)}
    return [p for p in sorted(have) if p not in ("id", "options.trace_both")
            and p in mine and mine[p] != theirs.get(p)]


def _peer_below_2_8(version: str) -> bool:
    """True when a peer's reported version is older than 2.8 (or unknown): such a node
    stores a trace file name as a path and opens it, so it must not be handed a new one."""
    return _peer_older(version, (2, 8, 0))


def _peer_save_fields(form, peer_mapping, peer_version):
    """The fields cluster_peer_save forwards to a peer's key-guarded mapping-save, or an
    error to show instead. Only the known mapping-form fields are forwarded (never a raw
    value the browser added), the submission is validated locally (so a URL serial port
    never reaches the peer), and the trace file is reduced to a sanitised name — kept as
    the peer stored it when unchanged, and refused as a new name for a pre-2.8 peer. The
    form never had the peer's MQTT password in it: the password to store is sent, the one
    the peer has when the field was left blank (a peer of any version stores what it gets).
    A one-line field posted back unchanged keeps the peer's stored value (line breaks
    the field can't show), and a peer before 2.8.1 gets its stored values for the fields
    it reads from every form but this form leaves out (_READ_BEFORE_2_8_1).
    Returns (fields, None) or (None, error)."""
    if not peer_mapping:
        return None, _("This mapping no longer exists on the peer; reload the fleet view.")
    try:
        # the points as strictly as the peer reads them: an older peer would store a
        # top-level object as no points
        _parse_modbus_points(form.get("modbus_points", ""), strict=True)
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
    def stored(section, attr):
        return (peer_mapping.get(section) or {}).get(attr)

    mqtt = peer_mapping.get("mqtt")
    stored_password = str(mqtt.get("password") or "") if isinstance(mqtt, dict) else ""
    unchanged = {field: value for field, (section, attr) in _TEXT_INPUTS.items()
                 if isinstance(value := stored(section, attr), str)
                 and form.get(field) == in_text_box(value)}
    fields = [(k, unchanged.get(k, v)) for k, v in form.multi_items()
              if k != "opt_trace_both" and k not in _PEER_PASSWORD_FIELDS
              and (k in _PEER_FORM_FIELDS or k.startswith(_PEER_FORM_PREFIXES))]
    fields.append(("opt_trace_both", out_trace))
    fields.append(("mqtt_password", mqtt_password_from_form(form, stored_password)))
    if _peer_older(peer_version, (2, 8, 1)):
        fields += [(field, str(value)) for field, (section, attr) in _READ_BEFORE_2_8_1.items()
                   if field not in form and (value := stored(section, attr)) is not None]
    return fields, None


def _peer_actor(a, form) -> str:
    """The user column of the audit record of a peer's remote action: "cluster-peer", or
    "cluster-peer:NODE/USER" when the request names who acted on the sending node — what
    that node says (it signed the request with the cluster key, so no one else could
    have), never an account of this node. Only a signed request's name is taken: a node
    running 2.8.0 or older sends the raw key, unsigned, and never a name."""
    user = str(form.get(ACTOR_FIELD) or "").strip()[:ACTOR_MAX]
    if a is None or a.legacy or not user:
        return "cluster-peer"
    node = re.sub(r"[\s/]+", "", str(form.get(ACTOR_NODE_FIELD) or ""))[:64]
    return f"cluster-peer:{node or a.sender[:16] or '?'}/{user}"


def _remote(target, peer_id, mapping, version="") -> dict:
    """The fleet view's edit form for a peer's mapping: the peer's host + port it posts
    back (resolved again on save), the address it shows, and the id it is saved under.
    That is the id the peer itself keys the mapping by, sent back to it unchanged: a
    v2.7/v2.8 peer may keep an id that this node would not take (``mapping`` carries the
    id derived from it here, which that peer has never heard of). This node uses it only
    to address the mapping on the peer, never for a file or path of its own. A peer
    running another version than this node gets a note saying so."""
    if not isinstance(peer_id, str) or not peer_id:
        peer_id = mapping.id
    addr = peer_addr(target["ip"], target["port"])
    note = ""
    if (age := _version_label(version)) == "older":
        note = _("{0} runs version {1}, this node {2}: the settings {1} doesn’t have are left "
                 "out of this form. Upgrade every node to the same version (README: Upgrading "
                 "a cluster).").format(addr, version, __version__)
    elif age == "newer":
        note = _("{0} runs version {1}, this node {2}: the settings only {1} has are not in "
                 "this form, and it keeps them as they are. Upgrade this node too (README: "
                 "Upgrading a cluster).").format(addr, version, __version__)
    return {"host": target["ip"], "port": target["port"], "addr": addr, "id": peer_id,
            "version_note": note}


def _peer_form(target, mapping, version, peer_id, data) -> dict:
    """form_context's arguments for a peer's mapping: its remote settings, the blocks a
    peer running *version* doesn't read left out, and the revision of the mapping its
    mapping-data answer (*data*) gave (2.9.0+)."""
    rev = data.get("rev") if isinstance(data, dict) else ""
    return {"remote": _remote(target, peer_id, mapping, version),
            "hide": blocks_not_read(_peer_version(version)),
            "rev": rev if isinstance(rev, str) else ""}


def _peer_links(data) -> dict:
    """The "links" of a peer's mapping-data (older peers send none), as {name: device}."""
    raw = (data or {}).get("links")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _mapping_kind_label(m) -> str:
    if m.kind == "serialbridge":
        return "serial↔serial"
    return f"{m.network.mode}/{m.network.protocol}"


def _mapping_endpoint(m) -> str:
    if m.kind == "serialbridge":
        return f"{m.serial.port} ↔ {m.serial_b.port}"
    if m.network.mode == "client":
        return f"→ {host_port(m.network.remote_host, m.network.remote_port)}"
    return f"{m.network.bind_shown}:{m.network.port}"


def _fmt_uptime(seconds) -> str:
    try:
        s = int(max(0, seconds))
    except (TypeError, ValueError):
        return ""
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _sec = divmod(s, 60)
    # in the page's language
    if d:
        return _("{0}d {1}h").format(d, h)
    if h:
        return _("{0}h {1}m").format(h, m)
    return _("{0}m").format(m)



def _peer_done(action: str, name: str, addr: str) -> str:
    """What a start / stop / restart of the mapping `name` on the peer `addr` did."""
    if action == "start":
        return _("“{0}” started on {1}.").format(name, addr)
    if action == "stop":
        return _("“{0}” stopped on {1}.").format(name, addr)
    return _("“{0}” restarted on {1}.").format(name, addr)


def _fleet_snapshot(view: dict):
    """(shape, counter spans) of the fleet view for LiveTable: the spans by the ids
    _cluster_body.html gives them. Without them (two rows with one id: a peer's odd
    mapping ids), the counters stay part of the shape."""
    spans: dict[str, str] | None = {}
    for n in view["nodes"]:
        for m in n.get("mappings") or ():
            key = "c-" + domid(f"{n.get('addr', '')}-{m.get('id', '')}") + "-bytes"
            if spans is None or key in spans:
                spans = None
                continue
            spans[key] = f"{bytesize(m.get('bytes_in', ''))} / {bytesize(m.get('bytes_out', ''))}"
    counters = ("bytes_in", "bytes_out") if spans is not None else ()
    nodes = [({k: v for k, v in n.items() if k not in ("uptime_s", "mappings")},   # (shown: uptime)
              [{k: v for k, v in m.items() if k not in counters} for m in n.get("mappings") or ()])
             for n in view["nodes"]]
    return (nodes, {k: v for k, v in view.items() if k != "nodes"}), spans


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
                "link": st.get("link", ""),  # connect-out: connecting / up / down
                "client_count": st.get("client_count", 0),
                "bytes_in": st.get("bytes_in", 0), "bytes_out": st.get("bytes_out", 0),
            })
        return {"id": cfg.instance_id, "name": node_label(cfg.admin_ui),
                "ip": ip, "port": port, "scheme": scheme,
                "version": __version__, "caps": list(CAPS),
                "uptime_s": int(time.monotonic() - state.started_mono),
                "mapping_count": len(maps), "running_count": running, "mappings": maps}

    async def _peer_auth(request):
        """The one check every key-guarded endpoint makes: (auth, None) when the request
        is signed with this node's cluster key for this node (or carries the raw key of a
        node running 2.8.0 or older while that is allowed), else (None, the 403 to answer). A refusal
        is logged and audited, and raw-key use logged, once per change per address - a
        peer with another key polls every few seconds."""
        ip = client_ip(request)
        # only a signed request's body is read here (its signature covers it): any other
        # request is decided on its headers, before its body is read
        body = await request.body() if request.headers.get(AUTH_HEADER) is not None else b""
        a = state.cluster.check_request(
            request.method, request.scope.get("path", ""),
            request.scope.get("query_string", b"").decode("latin-1"), body, request.headers, ip)
        where = f"{request.method} {request.url.path}"
        if a.ok:
            if a.again:
                state.log(f"cluster: requests from {ip} are accepted again")
            if a.legacy and a.first:
                state.log(f"cluster: {ip} sent the cluster key unsigned (a node running 2.8.0 or older) "
                          "— upgrade it, then turn legacy authentication off")
            return a, None
        if a.first:
            state.log(f"cluster: refused {where} from {ip}: {REFUSALS.get(a.reason, a.reason)}")
            state.audit(ip, "cluster_auth_failed", f"{where}: {a.reason}", user="cluster-peer")
        return None, _peer_json(a, {"error": "forbidden", "reason": a.reason}, 403)

    def _peer_json(a, payload, status_code=200):
        """A peer-facing JSON answer; for a signed request (also a refusal of one with a
        valid signature) signed for that request, so the caller can trust it."""
        resp = JSONResponse(payload, status_code=status_code)
        resp.headers[AUTH_HEADER] = state.cluster.reply_auth(a, status_code, resp.body)
        return resp

    async def cluster_local(request):
        """Peer-facing: returns this node's mappings to another cluster node that
        presents the matching shared key. Guarded by the key, not a user session
        (added to PUBLIC_PATHS) — read-only, exposes no credentials."""
        a, deny = await _peer_auth(request)
        if deny:
            return deny
        return _peer_json(a, local_node_payload())

    def _node_from(target, data) -> dict:
        """Build a fleet-table node entry from a fetch target + the peer's payload
        (data is None when the peer is unreachable). The address shown, the Open link
        and the actions are the target's - where this node found the peer: its beacon
        or its manual entry - never the address the peer reports for itself. That one
        differs behind NAT or Docker, for a peer entered by name and for one that could
        not tell its LAN address; it is shown only as information."""
        ip, port, scheme = target["ip"], target["port"], target["scheme"]
        base = {"self": False, "source": target.get("source", "auto"),
                "ip": ip, "port": port, "scheme": scheme, "addr": peer_addr(ip, port),
                **state.cluster.peer_state(target)}   # auth / problem / previous_key
        with contextlib.suppress(ValueError):
            base["url"] = peer_url(scheme, ip, port)
        if not data:
            return {**base, "online": False, "id": target.get("id", ""),
                    "name": target.get("name", base["addr"]),
                    "uptime": "", "version": "", "mapping_count": 0, "running_count": 0,
                    "mappings": [], "caps": frozenset(), "age": ""}
        maps = data.get("mappings")
        maps = [m for m in maps if isinstance(m, dict)] if isinstance(maps, list) else []
        own_ip, own_port = peer_host(data.get("ip", "")), peer_port(data.get("port"))
        if own_ip and own_port and peer_addr(own_ip, own_port) != base["addr"]:
            base["reported"] = peer_addr(own_ip, own_port)
        version = data.get("version", "")
        return {**base, "online": True, "id": data.get("id", target.get("id", "")),
                "name": data.get("name", base["addr"]),
                "uptime": _fmt_uptime(data.get("uptime_s", 0)), "version": version,
                "mapping_count": data.get("mapping_count", len(maps)),
                "running_count": data.get("running_count", 0), "mappings": maps,
                "caps": peer_caps(data), "age": _version_label(version)}

    fleet = LiveTable(ctx, "_cluster_body.html")

    async def cluster_status(request):
        """Aggregated view for the browser: local node + every discovered/manual peer,
        fetched server-side with the shared key. Renders the unified table."""
        if not state.config.cluster.active:
            return await ctx.arender(request, "_cluster_body.html", nodes=[],
                                     can_edit=can_edit(request), discovery_error="")
        local = local_node_payload()
        nodes = [{**local, "self": True, "online": True, "source": "self",
                  "addr": peer_addr(local["ip"], local["port"]),
                  "uptime": _fmt_uptime(local["uptime_s"])}]
        # refreshed every 5 s: 204 while the fleet view is what the browser shows already,
        # only the In / Out counters when only they changed (see LiveTable)
        if is_anonymous(request):  # other nodes' data needs a real login here
            view = {"nodes": nodes, "can_edit": False, "discovery_error": "", "peers_hidden": True}
            return await fleet.answer(request, lambda: _fleet_snapshot(view), **view)
        targets = state.cluster.all_targets()
        results = await asyncio.gather(*[state.cluster.fetch_peer(t) for t in targets]) \
            if targets else []
        for t, data in zip(targets, results, strict=False):
            nodes.append(_node_from(t, data))
        c = state.cluster
        versions = {n["version"] for n in nodes if n["online"] and isinstance(n["version"], str)}
        notes = {"copies": c.copies(), "shared": c.shared_ids(), "other_key": c.other_key_nodes(),
                 "legacy": c.legacy_nodes(),
                 "versions": sorted(versions, key=lambda v: _peer_version(v) or (0, 0, 0))
                 if len(versions) > 1 or "" in versions else []}
        view = {"nodes": nodes, "can_edit": can_edit(request),
                "discovery_error": state.cluster.discovery_error, "notes": notes}
        return await fleet.answer(request, lambda: _fleet_snapshot(view), **view)

    async def cluster_control(request):
        """Peer-facing remote control: another cluster node (presenting the shared key)
        asks us to start/stop/restart one of OUR mappings. Key-guarded, no session."""
        a, deny = await _peer_auth(request)
        if deny:
            return deny
        form = await request.form()
        mid, action = form.get("mapping_id", ""), form.get("action", "")
        if action not in ("start", "stop", "restart"):
            return _peer_json(a, {"error": "bad_action"}, 400)
        res = await ops.apply_action(state, mid, action)
        if not res.found:
            return _peer_json(a, {"error": "Mapping not found"}, 404)
        if res.error is not None:  # not saved, nothing changed: the controlling node says why
            return _peer_json(a, {"ok": False, "error": str(res.error)}, 400)
        name = res.name
        ip = client_ip(request)
        state.log(f"cluster: remote {action} of '{name}' from {ip}")
        state.audit(ip, f"cluster_remote_{action}", name, user=_peer_actor(a, form))
        # start_error: why it did not start; changed: False when it ran (or was stopped)
        # already (both ignored by nodes older than 2.9)
        why = start_failed(state, mid) if action != "stop" else ""
        return _peer_json(a, {"ok": True, "name": name, "changed": res.changed,
                              **({"start_error": why} if why else {})})

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
        res = await state.cluster.control_peer(target, mid, action, actor=ctx.actor(request))
        if not res or not res.get("ok"):
            why = res.get("error") if res and res.get("ok") is False else None
            if isinstance(why, str) and why:  # a 2.9+ peer that could not save the change
                return PlainTextResponse(_("The peer did not accept the command: {0}").format(
                    why[:500]), status_code=502)
            return PlainTextResponse(_("Peer did not accept the command "
                                       "(unreachable or rejected)."), status_code=502)
        state.cluster.forget(target)   # the refresh this triggers shows the new state
        addr = peer_addr(target["ip"], target["port"])
        name = str(res.get("name", mid))
        state.log(f"cluster: sent {action} to {addr} for '{name}'")
        ctx.audit(request, f"cluster_control_{action}", f"{addr}/{name}")
        why = res.get("start_error")   # from a 2.9+ peer
        why, raw = reason(why) if isinstance(why, str) else ("", "")
        note = (notice(_("“{0}” on {1} could not start: {2}").format(name, addr, why), "warn",
                       detail=raw)
                if why else notice(_peer_done(action, name, addr)))
        return Response("", headers={"HX-Trigger": "refreshCluster", **note})

    # ---- remote EDIT: peer renders no UI; THIS node renders the form (so the CSRF
    #      token + form action belong to the browser's session) and proxies the save.
    async def cluster_mapping_data(request):
        """Peer-facing: a mapping's full config + this node's serial ports + IP
        candidates, so the controlling node can render the edit form, and which listed port
        the mapping's ports lead to when one is a link elsewhere (/dev/serial0; "links",
        which only this node can tell). Key-guarded."""
        a, deny = await _peer_auth(request)
        if deny:
            return deny
        cfg = state.config
        m = cfg.get_mapping(request.query_params.get("mid", ""))
        if not m:
            return _peer_json(a, {"error": "not_found"}, 404)
        ports = await state.ports.current()
        # (nodes before 2.9.0 read only mapping, ports and ips; "rev" goes back with the
        # form, so this node can tell whether the mapping changed since)
        return _peer_json(a, {"mapping": m.to_dict(), "ports": ports,
                              "ips": netinfo.list_ip_candidates(),
                              "links": portlist.links(ports, (m.serial.port, m.serial_b.port)),
                              "version": __version__, "caps": list(CAPS),
                              "rev": mapping_revision(cfg.secret_key, m)})

    async def cluster_mapping_save(request):
        """Peer-facing: validate + persist + apply an edited mapping on THIS node, run
        from the same form the controlling node rendered. Key-guarded."""
        a, deny = await _peer_auth(request)
        if deny:
            return deny
        form = await request.form()
        ok, msg, saved = await ops.save_mapping_from_form(state, form)
        if not ok or saved is None:
            return _peer_json(a, {"ok": False, "error": msg}, 400)
        ip = client_ip(request)
        state.log(f"cluster: remote edit of '{msg}' from {ip}")
        state.audit(ip, "cluster_remote_save", msg, user=_peer_actor(a, form))
        # start_error: why it did not start (ignored by nodes older than 2.9)
        return _peer_json(a, {"ok": True, "name": msg,
                              **({"start_error": saved.start_error} if saved.start_error else {})})

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
        # its version: in the answer since 2.9.0, else what the fleet view was told
        version = data.get("version") or (await state.cluster.fetch_peer(target) or {}).get(
            "version", "")
        mapping = MappingConfig.from_dict(data["mapping"])
        return render(request, "_mapping_form.html", **form_context(
            mapping, data.get("ports", []), data.get("ips", []), is_new=False,
            links=_peer_links(data), mqtt_stored=mapping.mqtt.password,
            **_peer_form(target, mapping, version, data["mapping"].get("id"), data)))

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

        version = (data or {}).get("version") or (local or {}).get("version", "")

        def _reload(msg):
            peer = data or {}
            try:
                stored = (MappingConfig.from_dict(peer["mapping"])
                          if isinstance(peer.get("mapping"), dict) else None)
            except Exception:  # (a peer's mapping this node can't read: the form as posted)
                stored = None
            ports = peer.get("ports", [])
            mapping = mapping_for_rerender(form, stored, ports)
            return render(request, "_mapping_form.html", status_code=400, **form_context(
                mapping, ports, peer.get("ips", []), is_new=False, error=msg,
                links=_peer_links(data), mqtt_stored=stored.mqtt.password if stored else "",
                tls_stored=stored.network.tls_key_password if stored else "", form=form,
                mqtt_key_stored=stored.mqtt.client_key_password if stored else "",
                **_peer_form(target, mapping, version, form.get("id", ""), peer)))

        if not data:
            return _reload(_("The peer rejected the change or was unreachable."))
        fields, ferr = _peer_save_fields(form, data.get("mapping"), version)
        if ferr:
            return _reload(ferr)
        res = await state.cluster.post_peer(target, "/api/cluster/mapping-save", fields,
                                            actor=ctx.actor(request))
        if res and res.get("ok"):
            state.cluster.forget(target)   # the refresh this triggers shows the change
            addr = peer_addr(target["ip"], target["port"])
            name = str(res.get("name", ""))
            state.log(f"cluster: edited '{name}' on {addr}")
            ctx.audit(request, "cluster_edit", f"{addr}/{name}")
            why = res.get("start_error")   # from a 2.9+ peer
            why, raw = reason(why) if isinstance(why, str) else ("", "")
            note = (notice(_("“{0}” saved on {1}, but it could not start: {2}").format(
                        name, addr, why), "warn", detail=raw) if why
                    else notice(_("“{0}” saved on {1}.").format(name, addr)))
            # read it back: what the peer didn't keep of what was sent (an older version)
            after = await state.cluster.get_peer(target, "/api/cluster/mapping-data",
                                                 {"mid": form.get("id", "")})
            lost = []
            if after and isinstance(after.get("mapping"), dict):
                stored = MappingConfig.from_dict(data["mapping"])
                lost = not_kept(mapping_for_rerender(FormData(fields), stored,
                                                     data.get("ports", [])), after["mapping"])
            if not lost:
                return Response("", headers={"HX-Trigger": "refreshCluster", **note})
            state.log(f"cluster: {addr} (version {version or '?'}) did not keep "
                      f"{', '.join(lost)} of '{name}'")
            text = _("Saved on {0} (version {1}), but it did not keep: {2}.").format(
                addr, version or "?", ", ".join(lost[:12]) + (" …" if len(lost) > 12 else ""))
            return HTMLResponse(f'<div class="banner warn">{escape(text)} <button type="button" '
                                f'class="btn sm" data-cancel>{escape(_("Close"))}</button></div>',
                                headers={"HX-Trigger": "refreshCluster", **note})
        return _reload((res or {}).get("error") or _("The peer rejected the change or was "
                                                     "unreachable."))

    return [
        Route("/api/cluster/status", cluster_status),
        Route("/api/cluster/local", cluster_local, max_body_size=PEER_MAX_BODY),
        Route("/api/cluster/control", cluster_control, methods=["POST"],
              max_body_size=PEER_MAX_BODY),
        Route("/api/cluster/control-peer", cluster_control_peer, methods=["POST"]),
        Route("/api/cluster/mapping-data", cluster_mapping_data, max_body_size=PEER_MAX_BODY),
        Route("/api/cluster/mapping-save", cluster_mapping_save, methods=["POST"],
              max_body_size=PEER_MAX_BODY),
        Route("/api/cluster/peer-form", cluster_peer_form),
        Route("/api/cluster/peer-save", cluster_peer_save, methods=["POST"]),
    ]
