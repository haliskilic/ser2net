"""The systemd units: ser2net.service and the per-instance template ser2net@.service.

Checks:
  * both parse as unit files (sections, key=value lines);
  * the template keeps each instance's data in /var/lib/ser2net/<instance> (StateDirectory,
    0700: never the shared code tree or /opt/ser2net/data, whatever the instance is called),
    starts with --no-bootstrap (the instances share ./lib), reads an optional
    /etc/ser2net/<instance>.env (SER2NET_BIND_IP / SER2NET_PORT) after naming the instance
    by its instance name (SER2NET_NODE_NAME), runs as the ser2net user and has every
    hardening line of ser2net.service;
  * `systemd-analyze verify` has nothing to say about either (when it is installed).

Run: python3 tests/test_systemd_units.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
UNITS = os.path.join(ROOT, "systemd")


def parse(name: str) -> dict[str, list[tuple[str, str]]]:
    """{section: [(key, value), ...]} of a unit file, keys in order (they may repeat)."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    with open(os.path.join(UNITS, name), encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = sections.setdefault(line[1:-1], [])
                continue
            key, sep, value = line.partition("=")
            assert current is not None and sep and key.strip() == key and key, (name, n, line)
            current.append((key, value))
    return sections


def values(unit: dict, section: str, key: str) -> list[str]:
    return [v for k, v in unit.get(section, []) if k == key]


def test_template() -> None:
    plain, tmpl = parse("ser2net.service"), parse("ser2net@.service")
    assert set(tmpl) == {"Unit", "Service", "Install"}, set(tmpl)
    svc = dict(tmpl["Service"])
    assert svc["User"] == "ser2net" and svc["SupplementaryGroups"] == "dialout"
    assert svc["StateDirectory"] == "ser2net/%i" and svc["StateDirectoryMode"] == "0700"
    exec_start = svc["ExecStart"].split()
    assert exec_start[:2] == ["/usr/bin/python3", "/opt/ser2net/ser2net.py"], exec_start
    assert "--no-bootstrap" in exec_start
    assert exec_start[exec_start.index("--data-dir") + 1] == "/var/lib/ser2net/%i", exec_start
    assert "ReadWritePaths" not in svc, "nothing to write but the state directory"
    # the instance's name first, then its own file (which may set another, or none)
    keys = [k for k, _v in tmpl["Service"]]
    assert values(tmpl, "Service", "Environment") == ["SER2NET_NODE_NAME=%i"]
    assert values(tmpl, "Service", "EnvironmentFile") == ["-/etc/ser2net/%i.env"]
    assert keys.index("Environment") < keys.index("EnvironmentFile")
    assert "%i" in dict(tmpl["Unit"])["Description"]
    # every hardening line of ser2net.service, the same way
    skip = {"ExecStart", "ReadWritePaths", "WorkingDirectory"}
    hardening = [(k, v) for k, v in plain["Service"] if k not in skip]
    missing = [kv for kv in hardening if kv not in tmpl["Service"]]
    assert not missing, missing
    assert tmpl["Install"] == plain["Install"]
    print("ser2net@.service: data in /var/lib/ser2net/<instance> (0700), --no-bootstrap, "
          "/etc/ser2net/<instance>.env, named by the instance, ser2net.service's hardening  OK")


def test_systemd_analyze() -> None:
    tool = shutil.which("systemd-analyze")
    if not sys.platform.startswith("linux") or tool is None:
        print("SKIP: systemd-analyze is not installed: the units were only parsed")
        return
    d = tempfile.mkdtemp(prefix="s2n-units-")
    try:
        for name in ("ser2net.service", "ser2net@.service"):
            shutil.copy(os.path.join(UNITS, name), d)
        env = dict(os.environ, SYSTEMD_UNIT_PATH=d + ":")
        for unit in ("ser2net.service", "ser2net@line1.service"):
            r = subprocess.run([tool, "verify", "--man=no", os.path.join(d, unit)], env=env,
                               capture_output=True, text=True, timeout=120)
            said = [ln for ln in (r.stdout + r.stderr).splitlines() if "ser2net" in ln]
            if r.returncode != 0 and not said and "Failed to" in r.stderr:
                print(f"SKIP: systemd-analyze can't check units here: {r.stderr.strip()[:200]}")
                return
            assert r.returncode == 0 and not said, (unit, r.returncode, said)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("systemd-analyze verify: nothing to say about either unit  OK")


def main() -> None:
    test_template()
    test_systemd_analyze()
    print("\nPASS: systemd units")


if __name__ == "__main__":
    main()
