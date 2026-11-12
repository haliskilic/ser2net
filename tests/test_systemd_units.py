"""The systemd units in systemd/ start on a fresh install and keep the sandbox they promise.

Every unit file there is checked (ser2net.service for a source checkout,
ser2net-standalone.service for the binary, and any other one added later):
- each writable path the sandbox needs exists when the service starts: a ReadWritePaths=
  entry without '-' that is missing stops the start (status 226/NAMESPACE, again every
  3 s), so an ExecStartPre=+ line makes it, or it is the unit's StateDirectory=; and the
  --data-dir it runs with is writable;
- the service runs as the unprivileged ser2net user with NoNewPrivileges;
- systemd-analyze verify accepts the file (Linux, where systemd-analyze is installed);
- the release attaches the binary's unit.
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
            if entry.startswith("-"):
                continue  # optional: ignored when missing
            # made before the sandbox is set up ("+": run without it), only when missing
            pre = next((p for p in values(unit, "ExecStartPre")
                        if p.startswith("+") and f"test -d {entry} ||" in p
                        and re.search(rf"install -d -o ser2net -g ser2net -m 0700 {entry}'$", p)),
                       None)
            assert pre or entry in state, f"{name}: {entry} is not made before the start ({made})"
            writable.append(entry)
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
        assert one(unit, "Restart") == "on-failure", name
        print(f"{name}: user ser2net, NoNewPrivileges, no capabilities  OK")


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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_units_present()
    test_writable_paths_exist_at_start()
    test_unprivileged()
    test_binary_unit()
    test_systemd_analyze_verify()
    print("\nPASS: systemd units")
