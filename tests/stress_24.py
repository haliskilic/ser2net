"""Stress test: N simultaneous serial<->TCP bridges under realistic request/response load,
and with --cycles K, K rounds of every device going away and coming back.

Real serial protocols (Modbus RTU, SCPI, NMEA polling, console I/O) are request/response,
not simultaneous bulk in both directions. This test models that: N socat PTY pairs, N raw
mappings in one Supervisor/event loop, and for each bridge an echo device + a client that
performs ITERS round-trips (send MSG bytes, read the same MSG bytes back, verify) — all N
bridges concurrently. Reports round-trip latency percentiles, message rate, integrity,
errors, reconnects, and process resource use.

(One-directional bulk throughput is covered separately — each direction alone sustains
hundreds of KB instantly; this test targets concurrency + latency under realistic traffic.)

Each device is a socat pair linked under fixed names (pty,link=...): stopping socat is an
unplug, starting it again under the same names a replug. With --cycles K, after the load
every device is unplugged at once (each mapping must report the loss), plugged back (each
must reopen its port and pass round trips again), K times. The open files and the memory
after the first cycle must not grow over the others: a descriptor, task or buffer left
behind by each reconnect shows there.

Run: python3 tests/stress_24.py [N_BRIDGES] [MSG_BYTES] [ITERS] [--cycles K]
CI runs it small on the Linux test legs (8 bridges, 100 round trips, 2 cycles) and at full
size with 20 cycles every week (.github/workflows/scheduled.yml). Linux, needs socat.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor
from _server import free_port
from _socat import pty_pair

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
N = int(_args[0]) if len(_args) > 0 else 24
MSG = int(_args[1]) if len(_args) > 1 else 512
ITERS = int(_args[2]) if len(_args) > 2 else 300
CYCLES = int(sys.argv[sys.argv.index("--cycles") + 1]) if "--cycles" in sys.argv else 0
CYCLE_ITERS = min(ITERS, 20)  # round trips per bridge after each replug
PORTS: list[int] = []  # a free port per bridge (main)
LOST = ("reconnecting", "device-missing")
# what may grow from the first cycle to the last: nothing per reconnect, some slack for
# the allocator (memory) and a descriptor or two the runtime opens on its own
FD_SLACK = 4
RSS_SLACK = 16 * 1024 * 1024


def device_echo(ser, stop):
    """Echo device: read a request, write it straight back (self-paced by the client)."""
    while not stop.is_set():
        try:
            data = ser.read(MSG)
            if data:
                ser.write(data)
        except Exception:
            break


class Devices:
    """The N devices: socat pairs under fixed names, with an echo thread on each device end."""

    def __init__(self, folder: str):
        self.links = [(os.path.join(folder, f"port{i}"), os.path.join(folder, f"dev{i}"))
                      for i in range(N)]
        self.procs: list = []
        self.serials: list = []
        self.stop = threading.Event()

    @property
    def ports(self) -> list[str]:
        return [port for port, _dev in self.links]

    async def plug(self) -> None:
        self.procs = [p for p, _port, _dev in await asyncio.gather(
            *(pty_pair(links=links) for links in self.links))]

    def echo(self) -> None:
        import serial
        self.stop = threading.Event()
        self.serials = [serial.Serial(dev, baudrate=115200, timeout=0.05)
                        for _port, dev in self.links]
        for s in self.serials:
            threading.Thread(target=device_echo, args=(s, self.stop), daemon=True).start()

    async def unplug(self) -> None:
        self.stop.set()
        for s in self.serials:
            with suppress(Exception):
                s.close()
        self.serials = []
        for p in self.procs:
            with suppress(ProcessLookupError):
                p.terminate()
        await asyncio.gather(*(p.wait() for p in self.procs), return_exceptions=True)
        self.procs = []


async def drive_bridge(i, iters=ITERS):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORTS[i])
    req = bytes(((i + k) % 256) for k in range(MSG))
    lats = []
    bad = 0
    for _ in range(iters):
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


async def wait_all(sup, mappings, states, timeout) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if all((sup.status(m.id) or {}).get("state") in states for m in mappings):
            return True
        await asyncio.sleep(0.1)
    return False


async def settled(pi) -> tuple[int, int]:
    """(open files, RSS) once the dust settled: the fewest open files of a few looks (a
    connection being closed or a worker thread's handle counts for a moment)."""
    looks = []
    for _ in range(5):
        await asyncio.sleep(0.2)
        looks.append((pi.num_fds(), pi.memory_info().rss))
    return min(looks)


async def cycle(sup, mappings, devices, n) -> list[str]:
    """Unplug every device, plug them back, check the mappings follow; returns problems."""
    problems = []
    await devices.unplug()
    if not await wait_all(sup, mappings, LOST, 30):
        states = sorted({(sup.status(m.id) or {}).get("state") for m in mappings})
        problems.append(f"cycle {n}: not every mapping reported the loss ({states})")
    await devices.plug()
    if not await wait_all(sup, mappings, ("running",), 60):
        states = sorted({(sup.status(m.id) or {}).get("state") for m in mappings})
        problems.append(f"cycle {n}: not every mapping reopened its port ({states})")
        return problems
    devices.echo()
    await asyncio.sleep(0.3)
    res = await asyncio.gather(*(drive_bridge(i, CYCLE_ITERS) for i in range(N)),
                               return_exceptions=True)
    failed = [r for r in res if not isinstance(r, dict) or r["bad"] or r["iters"] != CYCLE_ITERS]
    if failed:
        problems.append(f"cycle {n}: {len(failed)} bridges failed their round trips "
                        f"after the replug: {failed[:2]}")
    return problems


async def main():
    import psutil

    print(f"=== Stress: {N} bridges x {ITERS} request/response round-trips, {MSG}B each"
          + (f", then {CYCLES} unplug/replug cycles" if CYCLES else "") + " ===")
    pi = psutil.Process()
    folder = tempfile.mkdtemp(prefix="s2n_stress_")
    devices = Devices(folder)
    sup = Supervisor(logger=lambda m: None)
    try:
        await devices.plug()
        print(f"created {N} socat PTY pairs")
        PORTS[:] = [free_port() for _ in range(N)]
        mappings = [MappingConfig.from_dict({
            "name": f"s{i:02d}", "enabled": True,
            "serial": {"port": devices.ports[i], "baudrate": 115200},
            "network": {"protocol": "raw", "bind_ip": "127.0.0.1", "port": PORTS[i]},
        }) for i in range(N)]

        t = time.monotonic()
        await asyncio.gather(*(sup.apply_mapping(m) for m in mappings))
        await wait_all(sup, mappings, ("running",), 10)
        running = sum(1 for m in mappings if (sup.status(m.id) or {}).get("state") == "running")
        print(f"started {running}/{N} bridges in {time.monotonic()-t:.2f}s")

        devices.echo()
        t0 = time.monotonic()
        res = await asyncio.gather(*(drive_bridge(i) for i in range(N)), return_exceptions=True)
        elapsed = time.monotonic() - t0

        all_lats = sorted(lat for r in res if isinstance(r, dict) for lat in r["lats"])
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
        ok = (total_rt == N * ITERS and bad == 0 and not exc and reconnects == 0
              and non_running == 0)

        problems: list[str] = []
        base = None
        for n in range(1, CYCLES + 1):
            t = time.monotonic()
            problems += await cycle(sup, mappings, devices, n)
            fds, rss = await settled(pi)
            print(f"cycle {n}: every device unplugged and back in {time.monotonic() - t:.1f}s; "
                  f"open fds {fds}, RSS {rss / 1e6:.1f} MB")
            if base is None:
                base = (fds, rss)
            if problems:
                break
        if CYCLES and not problems and base is not None:
            fds, rss = await settled(pi)
            if fds > base[0] + FD_SLACK:
                problems.append(f"open files grew from {base[0]} to {fds} over {CYCLES - 1} "
                                "reconnects of every port")
            if rss > base[1] + RSS_SLACK:
                problems.append(f"memory grew from {base[1] / 1e6:.1f} MB to {rss / 1e6:.1f} "
                                f"MB over {CYCLES - 1} reconnects of every port")
            print(f"after {CYCLES} cycles      : open fds {base[0]} -> {fds}, "
                  f"RSS {base[1] / 1e6:.1f} -> {rss / 1e6:.1f} MB")
        for p in problems:
            print("PROBLEM:", p)
        ok = ok and not problems
    finally:
        await sup.stop_all()
        await devices.unplug()
        shutil.rmtree(folder, ignore_errors=True)

    print("\n" + (f"PASS: {N} simultaneous bridges, {N*ITERS} round-trips, byte-exact, no errors"
                  + (f"; {CYCLES} unplug/replug cycles, no leaks" if CYCLES else "")
                  if ok else "ISSUES DETECTED — see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    if not shutil.which("socat"):
        print("SKIP: socat not found")
        sys.exit(0)
    sys.exit(asyncio.run(main()))
