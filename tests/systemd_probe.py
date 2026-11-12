"""Runs inside the systemd unit's sandbox (tests/systemd_smoke.sh, a oneshot copy of
systemd/ser2net.service with only ExecStart= and Type= changed) and checks what ser2net
needs there:
- the sandbox is in force: /proc/net is hidden (ProcSubset=pid), no capability;
- the network interfaces are still listed by name (psutil over netlink, without
  /proc/net/dev), so the bind-IP pickers offer the LAN addresses and the cluster beacons go
  to each interface's directed broadcast, not only to 255.255.255.255;
- a system call the filter denies (chown) fails with EPERM instead of killing the
  process (SystemCallErrorNumber=EPERM), and the open-file limit can be raised (prlimit64).
Exit status 0 when all of it holds; the output goes to the journal.

Not a test of its own (tests/run_all.py doesn't run it): python3 tests/systemd_probe.py
"""
import os
import resource
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [os.path.join(ROOT, "lib"), ROOT]

from app.engine import cluster, netinfo  # (after the path above)


def main() -> int:
    assert not os.path.exists("/proc/net/dev"), "ProcSubset=pid is not in force"
    with open("/proc/self/status", encoding="ascii") as fh:
        caps = {k: v.strip() for k, v in (ln.split(":", 1) for ln in fh) if k.startswith("Cap")}
    assert int(caps["CapEff"], 16) == 0, f"capabilities: {caps}"
    print(f"sandbox: /proc/net hidden, uid {os.getuid()}, no capabilities  OK")

    assert netinfo.psutil is not None, "psutil is not importable from lib/"
    nics = netinfo._nic_addresses()
    assert nics and all(name for name, _ip in nics), f"interfaces: {nics}"
    picker = [c["value"] for c in netinfo.list_ip_candidates() if c["kind"] == "lan"]
    assert picker, "the bind-IP picker lists no LAN address"
    beacons = cluster.ClusterDiscovery._broadcast_addrs()
    assert len(beacons) > 1, f"beacons only to {beacons}"
    print(f"interfaces {nics}, beacons to {beacons}  OK")

    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        os.chown(path, -1, -1)  # in @privileged's @chown: denied by the filter
        raise AssertionError("chown was allowed: the system call filter is not in force")
    except PermissionError:
        pass
    finally:
        os.remove(path)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.prlimit(0, resource.RLIMIT_NOFILE, (min(hard, max(soft, 4096)), hard))
    print("a denied call (chown) raised PermissionError; prlimit raised the open-file "
          f"limit to {resource.getrlimit(resource.RLIMIT_NOFILE)[0]}  OK")
    print("PASS: systemd sandbox probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
