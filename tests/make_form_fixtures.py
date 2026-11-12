"""Make tests/fixtures/forms-<version>.json with the code of an older release.

    python tests/make_form_fixtures.py v2.7.1 v2.8.0 v2.8.1

Run by hand, once per release (not in CI, where an old release's pinned packages and
Python support drift), from a git checkout with the release tags. For each version it
unpacks `git archive <tag> app` into a temporary folder and, with that release's code:

  * "forms": the fields its fleet view forwards to a peer running the current version
    when a kitchen-sink mapping (tests/_form_cases.py) is saved unchanged: rendered by its
    _mapping_form.html from the peer's mapping-data (the current to_dict), posted as its
    app.js and a browser post it (tests/_formsubmit.py), filtered as its cluster_peer_save
    filters them;
  * "mapping_data": the same mappings as that release stores them (its to_dict, what its
    /api/cluster/mapping-data answers; the TCP server's trace is the path 2.7 suggested);
  * "own_forms": what its own form posts for them (an edit on a node of that release),
    which is what the current version must post to a peer running it.

tests/test_mixed_version_edit.py uses them. Paths of this checkout are written as
@TLS_FILE@.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
# what a node before 2.8 suggested and stored for a trace (a path, see config.trace_file_name)
LEGACY_TRACE = "data/trace-plc1.log"


def _inside(root: str, version: str) -> None:
    """Runs in a child process with the old release's code first on sys.path."""
    sys.path.insert(0, root)
    import _form_cases as cases
    from _formsubmit import browser_post

    from app.config import MappingConfig
    from app.web.server import make_templates
    try:   # 2.8.0+
        from app.web.routes.forms import _modbus_points_json
    except ImportError:
        from app.web.routes import _modbus_points_json
    from app.engine import portlist

    def visible(ports):  # 2.8.0+ hide the ports without hardware
        return portlist.visible(ports) if hasattr(portlist, "visible") else ports
    try:   # 2.8.1+: what cluster_peer_save forwards
        from app.web.routes.cluster import _peer_save_fields
    except ImportError:
        _peer_save_fields = None
    from types import SimpleNamespace

    from starlette.datastructures import FormData

    tpl = make_templates().get_template("_mapping_form.html")
    request = SimpleNamespace(state=SimpleNamespace(csrf_token="t0ken"))
    remote = {"scheme": "http", "host": "192.0.2.7", "port": 8443, "addr": "192.0.2.7:8443"}

    def form_for(mapping_dict, peer_version):
        m = MappingConfig.from_dict(mapping_dict)
        page = tpl.render(request=request, mapping=m, is_new=False,
                          modbus_points_json=_modbus_points_json(m), ports=visible(cases.PORTS),
                          ips=cases.IPS, remote=dict(remote, id=mapping_dict["id"]), error=None)
        posted = browser_post(page, option_rule=False)
        if _peer_save_fields is not None:
            fields, err = _peer_save_fields(FormData(posted), mapping_dict, peer_version)
            assert err is None, (mapping_dict["name"], err)
            return fields
        skip = {"_peer_scheme", "_peer_host", "_peer_port", "_csrf", "scheme", "host", "port"}
        return [(k, v) for k, v in posted if k not in skip]

    out = {"version": version, "made_by": "tests/make_form_fixtures.py",
           "forms": {}, "mapping_data": {}, "own_forms": {}}
    for d in cases.mappings():
        out["forms"][d["id"]] = form_for(d, "9.9.9")
        own = MappingConfig.from_dict(d).to_dict()
        if d["id"] == "sinkserver01" and version.lstrip("v") < "2.8":
            own["options"]["trace_both"] = LEGACY_TRACE
        out["mapping_data"][d["id"]] = own
        out["own_forms"][d["id"]] = form_for(own, version.lstrip("v"))
    out["ports"], out["ips"] = cases.PORTS, cases.IPS
    text = json.dumps(out, ensure_ascii=False, indent=1).replace(
        json.dumps(cases.TLS_FILE)[1:-1], "@TLS_FILE@")
    # a [name, value] pair of a form on one line
    pair = r'\[\s*("(?:[^"\\]|\\.)*"),\s*("(?:[^"\\]|\\.)*")\s*\]'
    sys.stdout.write(re.sub(pair, r"[\1, \2]", text))


def main(argv) -> int:
    if argv[:1] == ["--inside"]:
        _inside(argv[1], argv[2])
        return 0
    for version in argv:
        tmp = tempfile.mkdtemp(prefix="s2n_fixture_")
        try:
            archive = subprocess.run(["git", "archive", version, "app"], cwd=ROOT,
                                     check=True, capture_output=True).stdout
            subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
            res = subprocess.run([sys.executable, os.path.abspath(__file__), "--inside", tmp,
                                  version], cwd=tmp, capture_output=True, text=True,
                                 encoding="utf-8")
            if res.returncode:
                sys.stderr.write(res.stderr)
                return 1
            name = os.path.join(FIXTURES, f"forms-{version.lstrip('v')}.json")
            with open(name, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(res.stdout + "\n")
            print(f"wrote {os.path.relpath(name, ROOT)}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
