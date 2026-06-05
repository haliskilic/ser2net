"""Access control, max-connections, kick_old_user (oldest-first), and the new
high-priority-client kick behavior. Uses loopback source IPs (127.0.0.1 vs
127.0.0.2) to distinguish clients. Run: python3 tests/test_access_priority.py
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

PORT = 47101


async def socat():
    p = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        m = re.search(rb"PTY is (\S+)", await p.stderr.readline())
        if m:
            devs.append(m.group(1).decode())
    return p, devs[0], devs[1]


async def connect(local_ip):
    return await asyncio.open_connection("127.0.0.1", PORT, local_addr=(local_ip, 0))


def peers(sup, mid):
    return {c["peer"].rsplit(":", 1)[0] for c in (sup.status(mid)["clients"])}


async def run_case(dev_a, net, steps_label):
    sup = Supervisor(logger=lambda m: None)
    m = MappingConfig.from_dict({
        "name": steps_label, "enabled": True,
        "serial": {"port": dev_a, "baudrate": 115200},
        "network": dict(net, protocol="raw", bind_ip="127.0.0.1", port=PORT),
    })
    ok, msg = await sup.apply_mapping(m)
    assert ok, msg
    await asyncio.sleep(0.4)
    return sup, m.id


async def main():
    p, dev_a, dev_b = await socat()
    fails = []

    # --- Case 1: allowed_client_ips filters by source IP ---
    sup, mid = await run_case(dev_a, {"max_connections": 5, "allowed_client_ips": ["127.0.0.1/32"]}, "acl")
    r1, w1 = await connect("127.0.0.1")     # allowed
    r2, w2 = await connect("127.0.0.2")     # not allowed -> rejected
    await asyncio.sleep(0.3)
    pr = peers(sup, mid)
    if not ("127.0.0.1" in pr and "127.0.0.2" not in pr):
        fails.append(f"ACL: expected only 127.0.0.1 connected, got {pr}")
    else:
        print("ACL: 127.0.0.1 allowed, 127.0.0.2 rejected  OK")
    w1.close(); w2.close()
    await sup.stop_all()

    # --- Case 2: max_connections=1, no kick -> second client refused ---
    sup, mid = await run_case(dev_a, {"max_connections": 1}, "nokick")
    r1, w1 = await connect("127.0.0.1")
    await asyncio.sleep(0.2)
    r2, w2 = await connect("127.0.0.2")
    await asyncio.sleep(0.3)
    pr = peers(sup, mid)
    if pr != {"127.0.0.1"}:
        fails.append(f"NOKICK: expected only first client, got {pr}")
    else:
        print("NOKICK: second client refused, first stays  OK")
    w1.close(); w2.close()
    await sup.stop_all()

    # --- Case 3: kick_old_user kicks the OLDEST client ---
    sup, mid = await run_case(dev_a, {"max_connections": 1, "kick_old_user": True}, "kickold")
    r1, w1 = await connect("127.0.0.1")     # oldest
    await asyncio.sleep(0.2)
    r2, w2 = await connect("127.0.0.2")     # should kick the oldest (127.0.0.1)
    await asyncio.sleep(0.3)
    pr = peers(sup, mid)
    if pr != {"127.0.0.2"}:
        fails.append(f"KICKOLD: expected newest (127.0.0.2), got {pr}")
    else:
        print("KICKOLD: oldest client evicted for newcomer  OK")
    w1.close(); w2.close()
    await sup.stop_all()

    # --- Case 4: priority client kicks an existing client even with kick_old_user=False ---
    sup, mid = await run_case(dev_a, {"max_connections": 1, "kick_old_user": False,
                                      "priority_client_ips": ["127.0.0.1"]}, "prio")
    r1, w1 = await connect("127.0.0.2")     # normal client occupies the slot
    await asyncio.sleep(0.2)
    pr_before = peers(sup, mid)
    r2, w2 = await connect("127.0.0.1")     # PRIORITY -> must kick the normal one
    await asyncio.sleep(0.3)
    pr_after = peers(sup, mid)
    if pr_before == {"127.0.0.2"} and pr_after == {"127.0.0.1"}:
        print("PRIORITY: priority client evicted the normal client  OK")
    else:
        fails.append(f"PRIORITY: before={pr_before} after={pr_after} (want 127.0.0.2 -> 127.0.0.1)")
    w1.close(); w2.close()
    await sup.stop_all()

    p.terminate(); await p.wait()
    if fails:
        print("\nFAILURES:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("\nPASS: access control + max-conn + kick-oldest + priority-kick")


if __name__ == "__main__":
    asyncio.run(main())
