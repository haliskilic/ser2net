"""Stress test: N simultaneous serial<->TCP bridges under realistic request/response load.

Real serial protocols (Modbus RTU, SCPI, NMEA polling, console I/O) are request/response,
not simultaneous bulk in both directions. This test models that: N socat PTY pairs, N raw
mappings in one Supervisor/event loop, and for each bridge an echo device + a client that
performs ITERS round-trips (send MSG bytes, read the same MSG bytes back, verify) — all N
bridges concurrently. Reports round-trip latency percentiles, message rate, integrity,
errors, reconnects, and process resource use.

(One-directional bulk throughput is covered separately — each direction alone sustains
hundreds of KB instantly; this test targets concurrency + latency under realistic traffic.)

Run: python3 tests/stress_24.py [N_BRIDGES] [MSG_BYTES] [ITERS]
"""
import asyncio
import os
import re
import sys
import threading
import time
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

N = int(sys.argv[1]) if len(sys.argv) > 1 else 24
MSG = int(sys.argv[2]) if len(sys.argv) > 2 else 512
ITERS = int(sys.argv[3]) if len(sys.argv) > 3 else 300
BASE_PORT = 46001


async def make_socat():
    proc = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    devs = []
    while len(devs) < 2:
        line = await proc.stderr.readline()
        if not line:
            raise RuntimeError("socat failed")
        m = re.search(rb"PTY is (\S+)", line)
        if m:
            devs.append(m.group(1).decode())
    return proc, devs[0], devs[1]


def device_echo(ser, stop):
    """Echo device: read a request, write it straight back (self-paced by the client)."""
    while not stop.is_set():
        try:
            data = ser.read(MSG)
            if data:
                ser.write(data)
        except Exception:
            break


async def drive_bridge(i):
    reader, writer = await asyncio.open_connection("127.0.0.1", BASE_PORT + i)
    req = bytes(((i + k) % 256) for k in range(MSG))
    lats = []
    bad = 0
    for _ in range(ITERS):
        t0 = time.monotonic()
        writer.write(req)
        await writer.drain()
        buf = bytearray()
        try:
            while len(buf) < MSG:
                chunk = await asyncio.wait_for(reader.read(MSG - len(buf)), timeout=10)
                if not chunk:
                    break
                buf += chunk
        except asyncio.TimeoutError:
            bad += 1
            break
        lats.append(time.monotonic() - t0)
        if bytes(buf) != req:
            bad += 1
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    return {"i": i, "iters": len(lats), "bad": bad, "lats": lats}


async def main():
    import serial
    import psutil

    print(f"=== Stress: {N} bridges x {ITERS} request/response round-trips, {MSG}B each ===")
    pi = psutil.Process()

    socats, runner_devs, dev_devs = [], [], []
    for _ in range(N):
        p, a, b = await make_socat()
        socats.append(p); runner_devs.append(a); dev_devs.append(b)
    print(f"created {N} socat PTY pairs")

    sup = Supervisor(logger=lambda m: None)
    mappings = [MappingConfig.from_dict({
        "name": f"s{i:02d}", "enabled": True,
        "serial": {"port": runner_devs[i], "baudrate": 115200},
        "network": {"protocol": "raw", "bind_ip": "127.0.0.1", "port": BASE_PORT + i},
    }) for i in range(N)]

    t = time.monotonic()
    await asyncio.gather(*(sup.apply_mapping(m) for m in mappings))
    await asyncio.sleep(0.6)
    running = sum(1 for m in mappings if (sup.status(m.id) or {}).get("state") == "running")
    print(f"started {running}/{N} bridges in {time.monotonic()-t:.2f}s")

    stop = threading.Event()
    devices = []
    for i in range(N):
        s = serial.Serial(dev_devs[i], baudrate=115200, timeout=0.05)
        devices.append(s)
        threading.Thread(target=device_echo, args=(s, stop), daemon=True).start()

    t0 = time.monotonic()
    res = await asyncio.gather(*(drive_bridge(i) for i in range(N)), return_exceptions=True)
    elapsed = time.monotonic() - t0

    stop.set()
    for s in devices:
        with suppress(Exception):
            s.close()

    all_lats = sorted(l for r in res if isinstance(r, dict) for l in r["lats"])
    total_rt = len(all_lats)
    bad = sum(r["bad"] for r in res if isinstance(r, dict))
    exc = [r for r in res if not isinstance(r, dict)]
    statuses = [sup.status(m.id) or {} for m in mappings]
    reconnects = sum(s.get("reconnects", 0) for s in statuses)
    non_running = sum(1 for s in statuses if s.get("state") != "running")

    def pct(p):
        return all_lats[min(len(all_lats) - 1, int(len(all_lats) * p))] * 1000 if all_lats else 0

    print("\n----- RESULTS -----")
    print(f"completed round-trips : {total_rt} / {N*ITERS}")
    print(f"integrity errors      : {bad}")
    print(f"exceptions            : {len(exc)}")
    print(f"wall time             : {elapsed:.2f}s")
    print(f"throughput            : {total_rt/elapsed:.0f} round-trips/s, "
          f"{total_rt*MSG*2/elapsed/1e6:.1f} MB/s aggregate")
    print(f"round-trip latency    : p50 {pct(0.5):.1f}ms  p95 {pct(0.95):.1f}ms  "
          f"p99 {pct(0.99):.1f}ms  max {pct(1.0):.1f}ms")
    print(f"reconnects (total)    : {reconnects}")
    print(f"non-running bridges   : {non_running}")
    print(f"process threads       : {pi.num_threads()}")
    print(f"open fds              : {pi.num_fds()}")
    print(f"RSS memory            : {pi.memory_info().rss/1e6:.1f} MB")
    if exc:
        print("EXCEPTIONS:", exc[:3])

    await sup.stop_all()
    for p in socats:
        p.terminate()
    await asyncio.gather(*(p.wait() for p in socats), return_exceptions=True)

    ok = (total_rt == N * ITERS and bad == 0 and not exc and reconnects == 0 and non_running == 0)
    print("\n" + (f"PASS: {N} simultaneous bridges, {N*ITERS} round-trips, byte-exact, no errors"
                  if ok else "ISSUES DETECTED — see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
