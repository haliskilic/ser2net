"""Reading config.json again while ser2net runs: SIGHUP (POSIX: `systemctl reload ser2net`,
`kill -HUP`, `docker kill -s HUP`), `ser2net --reload` (any platform, the Windows service
too) and Settings -> Configuration backups -> Reload.

The file is read as at start-up (the typed reader and its load notes) and checked as an edit
is: a part that changed must validate, a part the running configuration accepted while
broken may stay as it is, and a mapping that changed or is new must also pass the edit
rules. When the file can't be read or doesn't validate, nothing changes: the running
configuration stays and the reason is logged. Otherwise it is applied the way an edit is,
in place (the objects the cluster, the notifier and the requests hold stay the ones in use)
and part by part (``apply_changes``): a mapping whose settings didn't change keeps running
with its clients (one that should run but doesn't is started); one that changed is stopped
and started with its new settings, one that is gone is removed, a new one started; the
cluster is restarted only when its settings changed. The admin UI's certificate files, and
those of the TLS listeners of unchanged mappings, are read again for new connections
(app/tlsreload.py). The admin UI's address can't move while it listens: a changed one is
reported, and used from the next start.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any, Optional

from . import sdnotify, tlsreload
from .config import AppConfig, ConfigError, mqtt_clashes
from .engine.access import server_ssl


@dataclasses.dataclass
class Before:
    """What apply_changes compares the configuration with: the parts as they were."""
    mappings: dict[str, dict]   # id -> MappingConfig.to_dict()
    parts: dict[str, Any]       # the other top-level parts of config.json, as saved


def _parts(config: AppConfig) -> dict[str, Any]:
    d = config.to_dict()
    for key in ("mappings", "written_by"):
        d.pop(key, None)
    return d


def capture(config: AppConfig) -> Before:
    return Before(mappings={m.id: m.to_dict() for m in config.mappings},
                  parts=_parts(config))


@dataclasses.dataclass
class Applied:
    """What apply_changes did, for the log line."""
    added: list[str] = dataclasses.field(default_factory=list)      # mapping names
    changed: list[str] = dataclasses.field(default_factory=list)
    removed: list[str] = dataclasses.field(default_factory=list)
    started: list[str] = dataclasses.field(default_factory=list)    # unchanged, now running
    failed: list[str] = dataclasses.field(default_factory=list)     # "name: why"
    sections: list[str] = dataclasses.field(default_factory=list)   # other parts that changed
    tls_renewed: int = 0   # listeners of unchanged mappings that read their certificate again

    def summary(self) -> str:
        parts = [f"{label}: {', '.join(names)}" for label, names in (
            ("mappings added", self.added), ("mappings changed (restarted)", self.changed),
            ("mappings removed", self.removed), ("mappings started again", self.started))
            if names]
        if self.sections:
            parts.append("settings changed: " + ", ".join(self.sections))
        if not parts:
            parts.append("no mapping or setting changed")
        if self.tls_renewed:
            parts.append(f"TLS certificate loaded again by {self.tls_renewed} mapping "
                         "listener(s)")
        return "; ".join(parts)


def keep_env_address(admin) -> None:
    """SER2NET_BIND_IP / SER2NET_PORT win over config.json (and a backup), as at start-up."""
    from .runtime import _env_bind_override  # (runtime imports this module's users)
    _env_bind_override(admin)


def _listener_tls(net):
    """A mapping listener's TLS context as its runner makes it (access.server_ssl: engine/
    tls.py, the key decrypted with tls_key_password, the client CA): raises
    tls.TlsFileError when its files can't be used."""
    return server_ssl(net)


async def apply_changes(state, before: Before, renew_tls: bool = False) -> Applied:
    """Bring the running state in line with state.config after it was changed in place
    from `before` (a reload, a backup restore): each mapping in its own turn (Supervisor),
    the cluster when its settings changed. renew_tls: also read the certificate files of
    the TLS listeners of the mappings that didn't change again (a reload)."""
    cfg, sup, done = state.config, state.supervisor, Applied()
    now = {m.id: m for m in cfg.mappings}
    removed = [mid for mid in before.mappings if mid not in now]
    changed = {mid for mid, m in now.items()
               if mid in before.mappings and before.mappings[mid] != m.to_dict()}
    done.removed = [str(before.mappings[mid].get("name") or mid) for mid in removed]
    await state.forget_mappings(removed)
    # first stop every changed mapping, so that a listening port or serial device one of
    # them gave up is free for the one that has it now
    for mid in changed:
        await sup.remove_mapping(mid)
    for m in list(cfg.mappings):
        was = sup.get_runner(m.id)
        ok, why, did = await sup.ensure_running(m)
        if m.id not in before.mappings:
            done.added.append(m.name)
        elif m.id in changed:
            done.changed.append(m.name)
        elif did and m.enabled and was is not None:
            done.started.append(m.name)
        if not ok and m.enabled:
            done.failed.append(f"{m.name}: {why}")
        if renew_tls and m.id not in changed and m.id in before.mappings and m.kind == "net" \
                and m.network.tls and not did:
            net = m.network
            why = await sup.renew_tls(m.id, lambda net=net: _listener_tls(net))
            done.tls_renewed += why == ""
            if why:
                state.log(f"[{m.name}] WARNING: the TLS certificate files can't be loaded "
                          f"again ({why}): new connections keep the certificate in use")
    now_parts = _parts(cfg)
    changed_parts = [k for k in now_parts if now_parts[k] != before.parts.get(k)]
    done.sections = [f"{k} (everyone was signed out)" if k == "secret_key" else k
                     for k in changed_parts]
    if "cluster" in changed_parts or "instance_id" in changed_parts:
        await state.cluster.stop()
        await state.cluster.start()
    if "secret_key" in changed_parts:
        state.revoked_sessions.clear()  # signed with the old key: dead anyway
    for source in ("ldap", "oidc"):
        if source in changed_parts:  # (their Settings notes are about the old settings)
            for key in [k for k in state.group_warnings if k[0] == source]:
                del state.group_warnings[key]
            if source == "ldap":
                state.ldap_tls_problem = None
    return done


def _refusals(cur: AppConfig, new: AppConfig) -> tuple[list[str], dict[str, Any]]:
    """Why `new` (read from config.json) can't replace the running `cur`, and what is still
    accepted while broken after it (AppConfig.validate_for_save)."""
    problems: list[str] = []
    new.tolerated = dict(cur.tolerated)
    still: dict[str, Any] = {}
    try:
        still = new.validate_for_save()
    except Exception as e:  # (a ConfigError; anything else is no valid configuration either)
        problems.append(str(e))
    ids = [m.id for m in new.mappings]
    for mid in sorted({mid for mid in ids if ids.count(mid) > 1}):
        problems.append(f"the mapping id {mid} is used more than once")
    if cur.users and not new.users:
        problems.append("it has no user accounts: the first-run setup page would be open to "
                        "anyone who can reach the UI (restart ser2net to begin afresh)")
    elif cur.usable_admin_count() and not new.usable_admin_count():
        problems.append("no admin could sign in afterwards (LDAP/OIDC accounts need their "
                        "sign-in method on)")
    for m in new.mappings:
        old = cur.get_mapping(m.id)
        if old is None or old.to_dict() != m.to_dict():
            # what an edit of the mapping is held to as well: its own rules, its TLS files
            # and MQTT TLS files (they must load), and nothing that clashes with ser2net's
            # own ports, a serial port another mapping uses, the admin UI's TLS key or what
            # another enabled mapping uses on its MQTT broker (ops.edit_conflict)
            try:
                m.validate_input()
                m.check_tls_files()
                m.check_mqtt_tls_files()
            except Exception as e:
                problems.append(f"mapping '{m.name}': {e}")
                continue
            if clash := (new.own_port_conflict(m) or new.serial_conflict(m)
                         or new.admin_key_reuse(m)
                         or next((c.text() for c in mqtt_clashes(m, new.mappings)
                                  if c.refused), "")):
                problems.append(clash)
    return problems, still


def _copy_into(cur: AppConfig, new: AppConfig) -> None:
    """Make `cur` hold `new` in place. A mapping that didn't change keeps its object (its
    runner was built from it)."""
    for f in dataclasses.fields(AppConfig):
        if f.name in ("load_notes", "ids_created", "tolerated"):
            continue
        if f.name == "mappings":
            kept = []
            for m in new.mappings:
                old = cur.get_mapping(m.id)
                kept.append(old if old is not None and old.to_dict() == m.to_dict() else m)
            cur.mappings[:] = kept
        else:
            setattr(cur, f.name, getattr(new, f.name))


async def reload_config(state, source: str, actor_ip: str = "local",
                        user: str = "") -> tuple[bool, str]:
    """Read config.json again and apply it (see the module docstring). `source` names who
    asked (SIGHUP, command line, Settings), for the log and the audit record. Returns
    (applied, one line saying what happened or why nothing did)."""
    sdnotify.reloading()
    try:
        ok, message = await _reload(state)
    except Exception as e:  # a bug: say so, and keep serving
        ok, message = False, f"reload failed: {type(e).__name__}: {e}"
        state.log(f"ERROR: config.json reload ({source}) failed: {e!r}")
    finally:
        statuses = state.supervisor.all_status().values()
        running = sum(1 for st in statuses if st.get("state") == "running")
        sdnotify.ready(f"{running} of {len(state.config.mappings)} mapping(s) running")
    if ok:
        text = f"config.json reloaded: {message}"
        state.log(f"config.json reloaded ({source}): {message}")
    else:
        text = f"config.json not reloaded: {message} — the running configuration is kept"
        state.log(f"WARNING: config.json not reloaded ({source}): {message} — the running "
                  "configuration is kept")
    state.audit(actor_ip, "config_reload" if ok else "config_reload_refused",
                f"{source}: {message}", user=user)
    return ok, text


async def _reload(state) -> tuple[bool, str]:
    cur = state.config
    async with state.config_lock:
        try:
            text, seen = await asyncio.to_thread(state.store.read_current)
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            new = AppConfig.from_dict(raw)
            keep_env_address(new.admin_ui)
        except FileNotFoundError:
            return False, f"{state.store.path} is missing"
        except OSError as e:
            return False, f"{state.store.path} can't be read: {e.strerror or e}"
        except (ValueError, TypeError, AttributeError, ConfigError) as e:
            return False, f"{state.store.path} is not a valid configuration: {e}"
        # what the file doesn't have is kept: a hand-written config.json without them must
        # not sign everyone out or give this node a new cluster id
        if not raw.get("secret_key"):
            new.secret_key = cur.secret_key
        if not raw.get("instance_id"):
            new.instance_id = cur.instance_id
        # TLS files of a data directory that moved are taken from this one's tls folder, as
        # at start-up (runtime.prepare_config)
        new.load_notes += new.find_moved_tls_files(state.data_dir)
        # (it loads TLS files: not on the event loop)
        problems, still = await asyncio.to_thread(_refusals, cur, new)
        if problems:
            return False, "; ".join(problems)
        # the admin UI's TLS: its files are read again (a renewed certificate); settings
        # that changed must be usable, as Settings -> Admin TLS requires
        def tls_of(admin):
            return admin.tls_cert, admin.tls_key, admin.tls_key_password
        tls_changed = tls_of(cur.admin_ui) != tls_of(new.admin_ui)
        prepared: Optional[tlsreload.Prepared] = None
        if tls_changed or new.admin_ui.tls_enabled:
            prepared = await state.prepare_admin_tls(new.admin_ui)
            if prepared.error and tls_changed:
                return False, f"admin UI TLS: {prepared.error}"
        for note in new.load_notes:  # what reading it changed, as at start-up
            state.log(note)
        before = capture(cur)
        _copy_into(cur, new)
        cur.tolerated = still
        state.store.note_read(seen)  # in use now: a save no longer replaces an outside edit
        state.store.replaced_edit = None
    done = await apply_changes(state, before, renew_tls=True)
    notes = []
    guard = state.admin_guard
    if guard is not None:
        guard.use_limits(cur.admin_ui)
    if prepared is not None:
        if prepared.usable or tls_changed:
            if not state.use_admin_tls(prepared):  # (no admin UI listener here: noted)
                notes.append(f"the admin UI TLS settings are used from the next start "
                             f"({prepared.later})")
        else:  # the same settings, files that no longer load: the certificate in use stays
            why = prepared.error or prepared.later
            notes.append(f"admin UI TLS certificate not loaded again ({why}); new connections "
                         "keep the one in use")
    elif tls_changed:  # TLS switched off
        state.use_admin_tls(tlsreload.Prepared())
    ip, port, _tls = state.admin_serving()
    if (cur.admin_ui.bind_ip, cur.admin_ui.port) != (ip, port):
        notes.append(f"the admin UI address {cur.admin_ui.bind_ip}:{cur.admin_ui.port} takes "
                     f"effect at the next start (it listens on {ip}:{port} until then)")
    if done.failed:
        notes.append("not running: " + "; ".join(done.failed))
    return True, "; ".join([done.summary(), *notes])
