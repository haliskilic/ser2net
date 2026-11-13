"""The snap (snap/snapcraft.yaml, its service and configure hook, .github/workflows/snap.yml).

  * strictly confined, core24, amd64 and arm64; the release's executable as it is (the
    workflow checks it against SHA256SUMS), its version from the executable itself;
  * the command and a service that is off after installing (install-mode: disable), both
    with the interfaces a USB serial port needs: raw-usb (/dev/ttyUSB*, /dev/ttyACM*;
    connected by hand), serial-port (Ubuntu Core gadgets), hardware-observe (the port list)
    and the network ones;
  * the service runs ser2netweb on $SNAP_COMMON/data (layout.py's default for root in the
    snap), with `snap set ser2netweb bind-ip=... port=...` as SER2NETWEB_BIND_IP / _PORT;
    the configure hook refuses a bad address or port and restarts a running service;
  * snap.yml: on a release (not a pre-release; to stable; release.yml's channels job starts
    it with the tag), by hand with a tag and a channel,
    on pull requests that change the snap; snapcore/action-build, the snap tested on the
    runner (tests/snap_smoke.sh) before the upload, which needs SNAPCRAFT_STORE_CREDENTIALS
    (skipped with a notice without it) and has no GitHub permission.

Run: python3 tests/test_snap_package.py   (stdlib only; the hook runs where sh is)
"""
import os
import re
import shutil
import subprocess
import tempfile

from _workflows import ROOT, jobs, key, needs, read, steps, uses


def _text(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_snapcraft_yaml():
    y = _text("snap", "snapcraft.yaml")
    for line in ("name: ser2netweb", "base: core24", "confinement: strict", "grade: stable",
                 "adopt-info: ser2netweb", "license: AGPL-3.0-only"):
        assert re.search(rf"(?m)^{re.escape(line)}$", y), line
    summary = re.search(r"(?m)^summary: (.*)$", y).group(1)
    assert len(summary) <= 78, len(summary)
    assert "\nplatforms:\n  amd64:\n  arm64:\n" in y
    plugs = re.search(r"(?m)^    plugs: &plugs\n((?:      - .*\n)+)", y).group(1)
    names = [p.split("#")[0].strip(" -") for p in plugs.splitlines()]
    assert names == ["network", "network-bind", "network-observe", "raw-usb",
                     "hardware-observe", "serial-port"], names
    app = y.split("\n  ser2netweb:\n    command: bin/ser2netweb\n", 1)
    assert len(app) == 2, "the command ser2netweb"
    daemon = y.split("\n  daemon:\n    command: bin/daemon\n", 1)[1].split("\n\n", 1)[0]
    for line in ("daemon: simple", "install-mode: disable", "restart-condition: on-failure",
                 "plugs: *plugs"):
        assert f"\n    {line}" in "\n" + daemon, line
    part = y.split("\n  ser2netweb:\n    plugin: nil\n", 1)[1].split("\n  daemon:\n", 1)[0]
    assert "    source: build/snap\n" in part
    assert 'install -Dm755 ser2netweb "$CRAFT_PART_INSTALL/bin/ser2netweb"' in part
    assert 'craftctl set version="$("$CRAFT_PART_INSTALL/bin/ser2netweb" --version | cut -d\' \' -f2)"' \
        in part
    for licence in ("LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "THIRD-PARTY-NOTICES.md",
                    "THIRD-PARTY-LICENSES.txt"):
        assert licence in part, licence
    assert "sudo snap connect ser2netweb:raw-usb" in y
    print("snapcraft.yaml: strict, core24, amd64 + arm64, the release's executable, the "
          "service off until enabled, raw-usb / serial-port / hardware-observe  OK")


def test_service_and_hook():
    daemon = _text("snap", "local", "daemon")
    assert 'exec "$SNAP/bin/ser2netweb" --data-dir "$SNAP_COMMON/data"' in daemon
    assert 'export SER2NETWEB_BIND_IP="$bind_ip"' in daemon
    assert 'export SER2NETWEB_PORT="$port"' in daemon
    for name in ("snap/local/daemon", "snap/hooks/configure"):
        assert os.access(os.path.join(ROOT, name), os.X_OK) or os.name == "nt", name
    sh = shutil.which("sh")
    if not sh or os.name == "nt":
        print("SKIP: the configure hook needs sh")
        return
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "log")
        snapctl = os.path.join(tmp, "snapctl")
        with open(snapctl, "w", encoding="utf-8") as fh:  # a stand-in for snapctl
            fh.write('#!/bin/sh\necho "$*" >> "$LOG"\ncase "$1 $2" in\n'
                     '  "get bind-ip") echo "$BIND_IP" ;;\n  "get port") echo "$PORT" ;;\n'
                     '  "services ser2netweb.daemon") echo "Service Startup Current Notes"; '
                     'echo "ser2netweb.daemon enabled $STATE -" ;;\nesac\n')
        os.chmod(snapctl, 0o755)

        def hook(bind_ip, port, state="active"):
            if os.path.exists(log):
                os.remove(log)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], LOG=log,
                       BIND_IP=bind_ip, PORT=port, STATE=state)
            p = subprocess.run([sh, os.path.join(ROOT, "snap", "hooks", "configure")], env=env,
                               capture_output=True, text=True, timeout=30)
            with open(log, encoding="utf-8") as fh:
                return p.returncode, fh.read()

        # the service's wrapper, with a stand-in for the executable that prints what it got
        snap = os.path.join(tmp, "snap")
        os.makedirs(os.path.join(snap, "bin"))
        exe = os.path.join(snap, "bin", "ser2netweb")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "ip=${SER2NETWEB_BIND_IP-unset} '
                     'port=${SER2NETWEB_PORT-unset} $*"\n')
        os.chmod(exe, 0o755)
        common = os.path.join(tmp, "common")

        def service(bind_ip, port):
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SER2NETWEB_BIND_IP", "SER2NETWEB_PORT")}
            env.update(PATH=tmp + os.pathsep + os.environ["PATH"], LOG=log, BIND_IP=bind_ip,
                       PORT=port, STATE="active", SNAP=snap, SNAP_COMMON=common)
            p = subprocess.run([sh, os.path.join(ROOT, "snap", "local", "daemon")], env=env,
                               capture_output=True, text=True, timeout=30)
            assert p.returncode == 0, p.stderr
            return p.stdout.strip()

        data = os.path.join(common, "data")
        for (bind_ip, port), expect in ((("", ""), "ip=unset port=unset"),
                                        (("0.0.0.0", "8081"), "ip=0.0.0.0 port=8081"),
                                        (("192.168.1.10", ""), "ip=192.168.1.10 port=unset"),
                                        (("", "8092"), "ip=127.0.0.1 port=8092")):
            got = service(bind_ip, port)
            assert got == f"{expect} --data-dir {data}", (bind_ip, port, got)
        assert os.path.isdir(data)

        for ok in (("0.0.0.0", "8080"), ("*", "1"), ("::", "65535"), ("", ""),
                   ("192.168.1.10", "")):
            rc, calls = hook(*ok)
            assert rc == 0 and "restart ser2netweb.daemon" in calls, (ok, rc, calls)
        rc, calls = hook("0.0.0.0", "8080", state="inactive")
        assert rc == 0 and "restart" not in calls, "a stopped service stays stopped"
        for bad in (("0.0.0.0;id", "8080"), ("host name", ""), ("", "0"), ("", "65536"),
                    ("", "80a"), ("", "-1")):
            rc, calls = hook(*bad)
            assert rc == 1 and "restart" not in calls, (bad, rc, calls)
    print("the service: $SNAP_COMMON/data, snap set's bind-ip / port (port alone: on "
          "127.0.0.1); the configure hook "
          "refuses bad ones and restarts a running service only  OK")


def _started_by_release(workflow: str) -> None:
    """release.yml's channels job starts `workflow` on main with the tag (a release made with
    the GITHUB_TOKEN starts no workflow by itself)."""
    channels = jobs(read("release.yml"))["channels"]
    loop = next(line for line in channels.splitlines() if "for workflow in" in line)
    assert workflow in re.findall(r"[\w.-]+\.yml", loop), loop
    assert 'gh workflow run "$workflow" --ref main -f tag="$GITHUB_REF_NAME"' in channels


def test_workflow():
    text = read("snap.yml")
    on = text.split("\npermissions:", 1)[0]
    assert "\n  release:\n    types: [published]\n" in on
    assert re.search(r"(?m)^      tag:\n        description: .*\n        required: true", on)
    assert "        options: [stable, candidate, beta, edge]\n" in on
    assert "\n  pull_request:\n    paths:\n      - snap/**\n" in on
    assert "\npermissions:\n  contents: read\n" in text
    wf = jobs(text)
    build, publish = wf["build"], wf["publish"]
    assert key(build, "if") == "${{ !github.event.release.prerelease }}"
    assert "{ arch: amd64, runner: ubuntu-latest, exe: ser2netweb-linux-x64 }" in build
    assert "{ arch: arm64, runner: ubuntu-24.04-arm, exe: ser2netweb-linux-arm64 }" in build
    assert "sha256sum -c sums" in build and "mkdir -p build/snap" in build
    refs = [ref.split("@")[0] for _l, ref, _r in uses(build)]
    assert refs == ["actions/checkout", "snapcore/action-build", "actions/upload-artifact"], refs
    order = steps(build)
    built = next(i for i, s in enumerate(order) if "snapcore/action-build@" in s)
    tested = next(i for i, s in enumerate(order) if "tests/snap_smoke.sh" in s)
    assert built < tested, "tested before it is handed on"
    assert needs(publish) == ["build"] and key(publish, "if") == "github.event_name != 'pull_request'"
    assert "\n    permissions: {}\n" in publish
    st = steps(publish)
    assert 'if [ -z "$CREDENTIALS" ]; then' in st[1] and "::notice::" in st[1]
    assert st[2].startswith("      - if: steps.store.outputs.upload == 'true'\n"), st[2]
    assert "snapcore/action-publish@" in st[2]
    assert "release: ${{ github.event_name == 'release' && 'stable' || inputs.channel || 'stable' }}" \
        in st[2]
    smoke = _text("tests", "snap_smoke.sh")
    for part in ("sudo snap install --dangerous", "disabled +inactive", "ser2netweb:$plug",
                 "sudo snap start --enable ser2netweb.daemon", "healthy 8080",
                 "sudo snap set ser2netweb bind-ip=127.0.0.1 port=8091", "healthy 8091",
                 "sudo snap unset ser2netweb bind-ip\nsudo snap set ser2netweb port=8092\n"
                 "healthy 8092",
                 "sudo snap remove --purge ser2netweb"):
        assert part in smoke, part
    _started_by_release("snap.yml")
    print("snap.yml: releases (stable; release.yml starts it), by hand (a channel), pull "
          "requests; built and tested on both architectures before the upload, which needs "
          "its secret  OK")


if __name__ == "__main__":
    test_snapcraft_yaml()
    test_service_and_hook()
    test_workflow()
    print("\nPASS: snap")
