"""The systemd units in systemd/ start on a fresh install and keep the sandbox they promise.

Every unit file there is checked (ser2net.service for a source checkout,
ser2net-standalone.service for the binary, the per-instance template ser2net@.service,
and any other one added later):
- each writable path the sandbox needs exists when the service starts: a ReadWritePaths=
  entry without '-' that is missing stops the start (status 226/NAMESPACE, again every
  3 s), also of the ExecStartPre=+ lines (systemd applies ReadWritePaths= to them), so it
  is the unit's StateDirectory=, or an ExecStartPre=+ line makes it and the entry has '-';
  and the --data-dir it runs with is writable;
- the service runs as the unprivileged ser2net user with NoNewPrivileges and no
  capability; CAP_NET_BIND_SERVICE (ports below 1024) is there to uncomment;
- netlink is allowed, which lists the network interfaces (the bind-IP pickers, the cluster's
  directed broadcasts), and ProcSubset=pid stays;
- a system call the filter denies fails with EPERM instead of killing the process;
- `systemctl reload` reads config.json again (ExecReload= sends SIGHUP);
- systemd-analyze verify accepts the file, and the template as an instance of it (Linux,
  where systemd-analyze is installed);
- the release attaches the binary's unit;
- the template keeps each instance's data in /var/lib/ser2net/<instance> (StateDirectory,
  0700: never the shared code tree or /opt/ser2net/data, whatever the instance is called),
  starts with --no-bootstrap (the instances share ./lib), reads an optional
  /etc/ser2net/<instance>.env (SER2NET_BIND_IP / SER2NET_PORT) after naming the instance
  by its instance name (SER2NET_NODE_NAME), and has every hardening line of
  ser2net.service.
What only a real start shows (the sandbox around a running server) is the CI job
"systemd unit" (tests/systemd_smoke.sh).

Run: python3 tests/test_systemd_units.py   (stdlib only)
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
UNITS = sorted(glob.glob(os.path.join(ROOT, "systemd", "*.service")))


def parse(path: str) -> dict[str, list[tuple[str, str]]]:
    """section -> [(key, value)] in file order (a key may repeat); comments left out."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for line in re.sub(r"\\\n", " ", text).splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        assert current is not None and "=" in line, f"{path}: {line!r}"
        key, value = line.split("=", 1)
        current.append((key.strip(), value.strip()))
    return sections


def values(unit: dict, key: str, section: str = "Service") -> list[str]:
    return [v for k, v in unit.get(section, []) if k == key]


def one(unit: dict, key: str) -> str:
    found = values(unit, key)
    assert len(found) == 1, (key, found)
    return found[0]


def data_dir(unit: dict) -> str:
    m = re.search(r"--data-dir (\S+)", one(unit, "ExecStart"))
    assert m, "ExecStart names no --data-dir"
    return m.group(1)


def test_units_present():
    names = [os.path.basename(u) for u in UNITS]
    assert "ser2net.service" in names and "ser2net-standalone.service" in names, names
    print(f"units: {', '.join(names)}  OK")


def test_writable_paths_exist_at_start():
    for path in UNITS:
        unit, name = parse(path), os.path.basename(path)
        made = " ".join(values(unit, "ExecStartPre"))
        state = ["/var/lib/" + d for v in values(unit, "StateDirectory") for d in v.split()]
        writable = list(state)
        for entry in " ".join(values(unit, "ReadWritePaths")).split():
            path = entry.lstrip("-")
            # made at the start, only when missing, by a line run as root ("+")
            pre = next((p for p in values(unit, "ExecStartPre")
                        if p.startswith("+") and f"test -d {path} ||" in p
                        and re.search(rf"install -d -o ser2net -g ser2net -m 0700 {path}'$", p)),
                       None)
            if pre:  # (that line gets ReadWritePaths= too: before it ran, the path is missing)
                assert entry.startswith("-"), \
                    f"{name}: {path} has no '-', so the ExecStartPre=+ line that makes it can't run"
            elif entry.startswith("-"):
                continue  # optional: ignored when missing
            else:
                assert path in state, f"{name}: {path} is not made before the start ({made})"
            writable.append(path)
        assert data_dir(unit) in writable, f"{name}: --data-dir {data_dir(unit)} not writable"
        if values(unit, "StateDirectory"):
            assert one(unit, "StateDirectoryMode") == "0700", name
        assert "strict" == one(unit, "ProtectSystem"), name
        print(f"{name}: writable paths {writable} exist when it starts  OK")


def test_unprivileged():
    for path in UNITS:
        unit, name = parse(path), os.path.basename(path)
        assert one(unit, "User") == "ser2net", name
        assert one(unit, "NoNewPrivileges") == "true", name
        assert not values(unit, "AmbientCapabilities"), f"{name}: no capability by default"
        # ports below 1024: offered, commented out (README: Ports below 1024)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for line in ("#AmbientCapabilities=CAP_NET_BIND_SERVICE",
                     "#CapabilityBoundingSet=CAP_NET_BIND_SERVICE"):
            assert re.search(rf"(?m)^{line}$", text), f"{name}: {line}"
        assert one(unit, "Restart") == "on-failure", name
        print(f"{name}: user ser2net, NoNewPrivileges, no capabilities (the one for ports "
              "below 1024 offered, commented out)  OK")


def test_interfaces_readable():
    # netlink lists the interfaces (psutil.net_if_addrs: the bind-IP pickers, the cluster's
    # directed broadcasts); ProcSubset=pid stays (it hides /proc/net, which app/engine/
    # netinfo.py does without: tests/test_netinfo.py)
    for path in UNITS:
        unit, name = parse(path), os.path.basename(path)
        families = one(unit, "RestrictAddressFamilies").split()
        assert "AF_NETLINK" in families, f"{name}: {families}"
        assert one(unit, "ProcSubset") == "pid", name
        print(f"{name}: AF_NETLINK allowed, ProcSubset=pid kept  OK")


def test_denied_calls_fail_with_eperm():
    # without SystemCallErrorNumber= a call the filter denies kills the whole process with
    # SIGSYS (status 31/SYS, no traceback) and Restart= repeats that every 3 s
    for path in UNITS:
        unit, name = parse(path), os.path.basename(path)
        assert values(unit, "SystemCallFilter")[:1] == ["@system-service"], name
        assert one(unit, "SystemCallErrorNumber") == "EPERM", name
        print(f"{name}: a denied system call fails with EPERM  OK")


def test_reload():
    for path in UNITS:
        unit, name = parse(path), os.path.basename(path)
        assert one(unit, "ExecReload") == "/bin/kill -HUP $MAINPID", name
        print(f"{name}: systemctl reload sends SIGHUP  OK")


def test_template():
    plain = parse(os.path.join(ROOT, "systemd", "ser2net.service"))
    tmpl = parse(os.path.join(ROOT, "systemd", "ser2net@.service"))
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
    assert values(tmpl, "Environment") == ["SER2NET_NODE_NAME=%i"]
    assert values(tmpl, "EnvironmentFile") == ["-/etc/ser2net/%i.env"]
    assert keys.index("Environment") < keys.index("EnvironmentFile")
    assert "%i" in dict(tmpl["Unit"])["Description"]
    # every hardening line of ser2net.service, the same way (not its paths: the template
    # writes only its StateDirectory, so it has no ReadWritePaths to make first)
    skip = {"ExecStart", "ExecStartPre", "ReadWritePaths", "WorkingDirectory"}
    hardening = [(k, v) for k, v in plain["Service"] if k not in skip]
    missing = [kv for kv in hardening if kv not in tmpl["Service"]]
    assert not missing, missing
    assert tmpl["Install"] == plain["Install"]
    print("ser2net@.service: data in /var/lib/ser2net/<instance> (0700), --no-bootstrap, "
          "/etc/ser2net/<instance>.env, named by the instance, ser2net.service's hardening  OK")


def test_binary_unit():
    unit = parse(os.path.join(ROOT, "systemd", "ser2net-standalone.service"))
    start = one(unit, "ExecStart")
    assert start.startswith("/usr/local/bin/ser2net ") and data_dir(unit) == "/var/lib/ser2net", start
    assert "/opt/ser2net" not in " ".join(v for _k, v in unit["Service"]), "no source paths"
    with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as fh:
        release = fh.read()
    assert "cp systemd/ser2net-standalone.service out/ser2net-standalone.service" in release
    print("binary unit: /usr/local/bin/ser2net, data in /var/lib/ser2net, attached to releases  OK")


def test_systemd_analyze_verify():
    exe = shutil.which("systemd-analyze")
    if not sys.platform.startswith("linux") or exe is None:
        print("SKIP systemd-analyze verify: no systemd-analyze here")
        return
    tmp = tempfile.mkdtemp(prefix="s2n_units_")
    try:
        for path in UNITS:
            name = os.path.basename(path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            # verify wants each program to exist: one this machine lacks (the binary in
            # /usr/local/bin) is checked as /bin/sh, the rest of the line as it is
            for program in set(re.findall(r"(?m)^ExecStart=[-+!@:]*(/\S+)", text)):
                if not os.access(program, os.X_OK):
                    text = text.replace(f"ExecStart={program} ", "ExecStart=/bin/sh ")
            copy = os.path.join(tmp, name)
            with open(copy, "w", encoding="utf-8") as fh:
                fh.write(text)
            p = subprocess.run([exe, "verify", "--man=no", copy], capture_output=True,
                               text=True, timeout=60)
            own = [ln for ln in (p.stdout + p.stderr).splitlines() if name in ln]
            assert p.returncode == 0 and not own, f"{name}: {p.stdout}{p.stderr}"
            print(f"{name}: systemd-analyze verify  OK")
        # the template as systemd runs it: an instance of it, found on the unit path
        env = dict(os.environ, SYSTEMD_UNIT_PATH=tmp + ":")
        p = subprocess.run([exe, "verify", "--man=no", os.path.join(tmp, "ser2net@line1.service")],
                           env=env, capture_output=True, text=True, timeout=60)
        own = [ln for ln in (p.stdout + p.stderr).splitlines() if "ser2net" in ln]
        assert p.returncode == 0 and not own, f"ser2net@line1.service: {p.stdout}{p.stderr}"
        print("ser2net@line1.service (an instance of the template): systemd-analyze verify  OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_units_present()
    test_writable_paths_exist_at_start()
    test_unprivileged()
    test_interfaces_readable()
    test_denied_calls_fail_with_eperm()
    test_reload()
    test_template()
    test_binary_unit()
    test_systemd_analyze_verify()
    print("\nPASS: systemd units")
