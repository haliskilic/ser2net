"""Editing a mapping through the web form changes nothing the user didn't change.

The form doesn't show (or post) every setting, and an edit rebuilds the mapping from
what was posted: a setting survives only when the form shows it or the parsing keeps
the stored value (app/web/routes/forms.py: _OPTIONAL_FORM_FIELDS, _UNMANAGED_FIELDS,
...). This test makes sure no setting falls between the two, now or when one is added:

  * the settings are found by walking MappingConfig's dataclasses; between them, the
    kitchen-sink mappings (tests/_form_cases.py) give every one a value other than its
    default — a setting they don't cover yet gets a value of its type here;
  * each mapping is rendered into the real _mapping_form.html, posted back unchanged as
    a browser posts it (tests/_formsubmit.py: app.js show-when rules, disabled blocks,
    unticked boxes, one-line inputs) and parsed as a save parses it: it comes back
    identical, but for the settings that don't apply to its kind (_form_cases.IRRELEVANT);
  * the same through the form shown again after an error, locally and in the fleet view,
    and through the fleet view's edit of a peer's mapping (what peer-save forwards);
  * every form field the save reads is in the form, every presence marker too, and no
    setting the form shows is also one it always keeps.

A setting added without a form field or a rule that keeps it fails here, by name.
Pure in-process test (no sockets).

Run: python3 tests/test_mapping_form_roundtrip.py
"""
from __future__ import annotations

import dataclasses
import typing
from types import SimpleNamespace

import _form_cases as cases
from _formsubmit import Form, browser_post
from _server import ROOT  # noqa: F401  (sys.path setup)
from starlette.datastructures import FormData

from app.config import MappingConfig
from app.web.routes import forms
from app.web.routes.cluster import _peer_save_fields, _remote
from app.web.server import make_templates

TEMPLATE = make_templates().get_template("_mapping_form.html")
REQUEST = SimpleNamespace(state=SimpleNamespace(csrf_token="t0ken"))
PEER = {"ip": "192.0.2.7", "port": 8443}
# a form field name per section of the config (the template's naming)
PREFIX = {"serial": "serial_", "serial.advanced": "serial_", "serial_b": "serial_b_",
          "serial_b.advanced": "serial_b_", "network": "network_", "options": "opt_",
          "mqtt": "mqtt_", "modbus_poll": "modbus_poll_"}


# ---- the settings -------------------------------------------------------------------

def settings(cls=MappingConfig, prefix="") -> list[tuple[str, dataclasses.Field]]:
    """(path, field) of every setting: "serial.advanced.nobreak",
    "modbus_poll.points[].writable" ... (input_notes is no setting: never stored)."""
    out = []
    hints = typing.get_type_hints(cls)
    for f in dataclasses.fields(cls):
        if not f.compare:
            continue
        hint = hints[f.name]
        args = typing.get_args(hint)
        if dataclasses.is_dataclass(hint):
            out += settings(hint, f"{prefix}{f.name}.")
        elif typing.get_origin(hint) is list and args and dataclasses.is_dataclass(args[0]):
            out += settings(args[0], f"{prefix}{f.name}[].")
        else:
            out.append((prefix + f.name, f))
    return out


def default_of(f: dataclasses.Field):
    if f.default is not dataclasses.MISSING:
        return f.default
    return f.default_factory()  # type: ignore[misc]


def values_at(d: dict, path: str) -> list:
    """The values at *path* in a mapping dict (several for a points[] path)."""
    head, _sep, rest = path.partition(".")
    if head.endswith("[]"):
        return [v for item in d.get(head[:-2], []) for v in values_at(item, rest)]
    if head not in d:
        return []
    return values_at(d[head], rest) if rest else [d[head]]


def set_at(d: dict, path: str, value) -> None:
    head, _sep, rest = path.partition(".")
    if head.endswith("[]"):
        for item in d.get(head[:-2], []):
            set_at(item, rest, value)
    elif rest:
        set_at(d.setdefault(head, {}), rest, value)
    else:
        d[head] = value


def other_value(f: dataclasses.Field):
    """A value of the setting's type other than its default (for a setting the
    kitchen-sink mappings don't cover yet)."""
    d = default_of(f)
    if isinstance(d, bool):
        return not d
    if isinstance(d, int):
        return d + 3
    if isinstance(d, float):
        return d + 0.5
    if isinstance(d, str):
        return f"{d}x{f.name}"[:40]
    if isinstance(d, list):
        return [f"x{f.name}"]
    if isinstance(d, dict):
        return {"x": f.name}
    raise AssertionError(f"no value to try for a setting of type {type(d).__name__}")


def irrelevant(m: MappingConfig, path: str) -> bool:
    return any(path == p or path.startswith(p) for p in cases.IRRELEVANT.get(m.kind, ()))


def sinks() -> list[MappingConfig]:
    """The kitchen-sink mappings, with a value for any setting none of them covers."""
    dicts = cases.mappings()
    for path, f in settings():
        default = default_of(f)
        if not any(v != default for d in dicts for v in values_at(d, path)):
            print(f"  (no kitchen-sink value for {path}: trying {other_value(f)!r})")
            for d in dicts:
                set_at(d, path, other_value(f))
    out = []
    for d in dicts:
        m = MappingConfig.from_dict(d)
        assert not m.input_notes, (m.name, [n.text() for n in m.input_notes])
        m.validate()
        m.validate_input()
        out.append(m)
    return out


# ---- the form, as the routes render and parse it -------------------------------------

def render(mapping, *, error=None, remote=None, ports=None, hide=frozenset()) -> str:
    return TEMPLATE.render(request=REQUEST, **forms.form_context(
        mapping, cases.PORTS if ports is None else ports, cases.IPS, is_new=False,
        error=error, remote=remote, hide=hide))


class Recorder(FormData):
    """A submitted form that remembers which fields the parsing asked for."""
    asked: set = set()

    def get(self, key, default=None):
        Recorder.asked.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        Recorder.asked.add(key)
        return super().__contains__(key)


def local_save(stored: MappingConfig, page: str) -> MappingConfig:
    """mappings.mapping_save -> ops.save_mapping_from_form, on this node."""
    return forms.mapping_from_form(Recorder(browser_post(page)), stored, cases.PORTS)


def remote_save(stored: MappingConfig, page: str) -> MappingConfig:
    """cluster.cluster_peer_save (this node) -> mapping-save on the peer: the fields it
    forwards, parsed there against the peer's stored mapping."""
    fields, err = _peer_save_fields(FormData(browser_post(page)), stored.to_dict(), "9.9.9")
    assert err is None, err
    return forms.mapping_from_form(Recorder(fields), stored, cases.PORTS)


def remote_form(stored: MappingConfig) -> str:
    """cluster.cluster_peer_form: the peer's mapping-data rendered on this node."""
    data = {"mapping": stored.to_dict(), "ports": cases.PORTS, "ips": cases.IPS}
    shown = MappingConfig.from_dict(data["mapping"])
    return render(shown, remote=_remote(PEER, data["mapping"]["id"], shown))


def diff(a: dict, b: dict, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in a.keys() | b.keys():
            out += diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}{k}.")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and \
            all(isinstance(x, dict) for x in a + b):
        return [p for x, y in zip(a, b, strict=True) for p in diff(x, y, path[:-1] + "[].")]
    return [] if a == b else [f"{path[:-1]}: {a!r} -> {b!r}"]


def assert_kept(stored: MappingConfig, edited: MappingConfig, how: str) -> None:
    changed = [d for d in diff(stored.to_dict(), edited.to_dict())
               if not irrelevant(stored, d.split(":")[0])]
    assert not changed, (f"{stored.name}, {how}: an unchanged save changed settings — show "
                         "them in _mapping_form.html or keep them in forms.py "
                         "(_OPTIONAL_FORM_FIELDS / _UNMANAGED_FIELDS):\n  " + "\n  ".join(changed))


# ---- the checks -----------------------------------------------------------------------

def test_every_setting_covered(all_sinks):
    missing = []
    for path, f in settings():
        default = default_of(f)
        if not any(v != default for m in all_sinks if not irrelevant(m, path)
                   for v in values_at(m.to_dict(), path)):
            missing.append(path)
    assert not missing, f"settings no kitchen-sink mapping exercises: {missing}"
    print(f"{len(settings())} settings, each other than its default in a kitchen-sink "
          f"mapping ({len(all_sinks)} mappings)  OK")


def test_unchanged_save(all_sinks):
    for m in all_sinks:
        assert_kept(m, local_save(m, render(m)), "local edit")
        # a form shown again after an error (validation, a port clash found at save time)
        again = forms.mapping_for_rerender(FormData(browser_post(render(m))), m, cases.PORTS)
        assert_kept(m, local_save(m, render(again, error="Something was wrong.")),
                    "local edit after an error")
        # the fleet view: this node renders the peer's mapping and forwards the save
        assert_kept(m, remote_save(m, remote_form(m)), "fleet-view edit")
        # ...and shows it again after the peer refused it (cluster_peer_save._reload)
        again = forms.mapping_for_rerender(FormData(browser_post(remote_form(m))), m,
                                           cases.PORTS)
        page = render(again, error="The peer refused it.",
                      remote=_remote(PEER, m.id, again))
        assert_kept(m, remote_save(m, page), "fleet-view edit after an error")
    print("unchanged save keeps every setting: local and fleet-view edit, and both after "
          "an error  OK")


def test_form_fields(all_sinks):
    shown = set()
    for m in all_sinks:
        shown |= Form(render(m)).names() | Form(remote_form(m)).names()
    unknown = sorted(Recorder.asked - shown)
    assert not unknown, f"the save reads form fields the form doesn't have: {unknown}"
    markers = sorted(set(forms._OPTIONAL_FORM_FIELDS) - shown)
    assert not markers, f"presence markers the form never posts (always kept): {markers}"
    both = sorted(f"{section}.{attr}" for section, attrs in forms._UNMANAGED_FIELDS.items()
                  for attr in attrs if PREFIX[section] + attr in shown)
    assert not both, (f"settings the form shows but an edit always keeps (so they can't "
                      f"be changed; take them off _UNMANAGED_FIELDS): {both}")
    print(f"the save reads {len(Recorder.asked)} form fields, all in the form; every "
          "presence marker is posted; nothing shown is also always kept  OK")


def test_edit_takes_effect(all_sinks):
    """The rules that keep stored values don't swallow a real change."""
    m = next(s for s in all_sinks if s.name == "TCP server")
    posted = dict(browser_post(render(m)))
    posted.update(mqtt_client_id="s2n-other", opt_closeon="quit", network_port="4999",
                  network_client_queue_max="1024")
    posted.pop("serial_usb_match")   # unticked
    edited = forms.mapping_from_form(FormData(list(posted.items())), m, cases.PORTS)
    assert (edited.mqtt.client_id, edited.options.closeon, edited.network.port,
            edited.network.client_queue_max, edited.serial.match) == (
        "s2n-other", "quit", 4999, 1024, {}), edited
    print("a changed field is taken (client id, close-on, port, client buffer, USB box)  OK")


def main():
    all_sinks = sinks()
    test_every_setting_covered(all_sinks)
    test_unchanged_save(all_sinks)
    test_form_fields(all_sinks)
    test_edit_takes_effect(all_sinks)
    print("\nPASS: a mapping edited through the form keeps every setting it didn't change")


if __name__ == "__main__":
    main()
