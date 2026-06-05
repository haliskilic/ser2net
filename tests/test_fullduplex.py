"""True full-duplex throughput test: device end = os.openpty master serviced by
threads; client end = blocking sockets serviced by threads — both fully decoupled
from the bridge's asyncio event loop (as real remote clients are). For each bridge
the client sends PAYLOAD while concurrently receiving PAYLOAD the device sends.
Verifies byte-exactness in both directions.

Run: python3 tests/test_fullduplex.py [N_BRIDGES] [PAYLOAD_BYTES]
"""
import asyncio
import os
import socket
import sys
import threading
import time
from contextlib import suppress

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.supervisor import Supervisor

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1
PAYLOAD = int(sys.argv[2]) if len(sys.argv) > 2 else 256 * 1024
BASE = 47401


def cli_pattern(i):
    return bytes((i + j) % 256 for j in range(PAYLOAD))


def dev_pattern(i):
    return bytes((i + j + 117) % 256 for j in range(PAYLOAD))


def fd_write(fd, data):
    off = 0
    while off < len(data):
        try:
            off += os.write(fd, data[off:off + 65536])
        except BlockingIOError:
            time.sleep(0.001)
        except OSError:
            break


def fd_read(fd, n, out):
    buf = bytearray()
    while len(buf) < n:
        try:
            d = os.read(fd, n - len(buf))
        except BlockingIOError:
            time.sleep(0.001); continue
        except OSError:
            break
        if not d:
            break
        buf += d
    out.append(bytes(buf))


def sock_send(sock, data):
    with suppress(OSError):
        sock.sendall(data)


def sock_recv(sock, n, out):
    buf = bytearray()
    while len(buf) < n:
        try:
            d = sock.recv(n - len(buf))
        except OSError:
            break
        if not d:
            break
        buf += d
    out.append(bytes(buf))


def harness(ports, masters, cli_out, dev_out, done):
    threads = []
    socks = []
    for i in range(N):
        # device end (pty master): write dev_pattern, read cli_pattern
        threads.append(threading.Thread(target=fd_write, args=(masters[i], dev_pattern(i)), daemon=True))
        threads.append(threading.Thread(target=fd_read, args=(masters[i], PAYLOAD, dev_out[i]), daemon=True))
        # client end (blocking socket): send cli_pattern, recv dev_pattern
        s = socket.create_connection(("127.0.0.1", ports[i]), timeout=10)
        socks.append(s)
        threads.append(threading.Thread(target=sock_send, args=(s, cli_pattern(i)), daemon=True))
        threads.append(threading.Thread(target=sock_recv, args=(s, PAYLOAD, cli_out[i]), daemon=True))
    for t in threads:
        t.start()
    deadline = time.monotonic() + 40
    for t in threads:
        t.join(timeout=max(0.1, deadline - time.monotonic()))
    for s in socks:
        with suppress(OSError):
            s.close()
    done.set()


async def main():
    import psutil
    print(f"=== Full-duplex (decoupled loops): {N} bridge(s), {PAYLOAD//1024} KB BOTH ways ===")
    ptys = [os.openpty() for _ in range(N)]
    masters = [m for m, s in ptys]
    names = [os.ttyname(s) for m, s in ptys]

    sup = Supervisor(logger=lambda m: None)
    maps = [MappingConfig.from_dict({
        "name": f"fd{i}", "enabled": True,
        "serial": {"port": names[i], "baudrate": 115200},
        "network": {"protocol": "raw", "bind_ip": "127.0.0.1", "port": BASE + i},
    }) for i in range(N)]
    await asyncio.gather(*(sup.apply_mapping(m) for m in maps))
    await asyncio.sleep(0.5)
    running = sum(1 for m in maps if (sup.status(m.id) or {}).get("state") == "running")
    print(f"running bridges: {running}/{N}")

    cli_out = [[] for _ in range(N)]
    dev_out = [[] for _ in range(N)]
    done = threading.Event()
    ports = [BASE + i for i in range(N)]
    hthread = threading.Thread(target=harness, args=(ports, masters, cli_out, dev_out, done), daemon=True)
    t0 = time.monotonic()
    hthread.start()
    while not done.is_set():            # keep the bridge's event loop running
        await asyncio.sleep(0.05)
    elapsed = time.monotonic() - t0

    client_ok = sum(1 for i in range(N) if cli_out[i] and cli_out[i][0] == dev_pattern(i))
    dev_ok = sum(1 for i in range(N) if dev_out[i] and dev_out[i][0] == cli_pattern(i))
    total = PAYLOAD * N * 2
    reconnects = sum((sup.status(m.id) or {}).get("reconnects", 0) for m in maps)

    print(f"client<-device verified : {client_ok}/{N}")
    print(f"device<-client verified : {dev_ok}/{N}")
    print(f"wall time               : {elapsed:.2f}s")
    print(f"aggregate throughput    : {total/elapsed/1e6:.1f} MB/s ({total/1e6:.1f} MB both ways)")
    print(f"reconnects              : {reconnects}")
    print(f"RSS                     : {psutil.Process().memory_info().rss/1e6:.1f} MB")

    await sup.stop_all()
    for m, s in ptys:
        with suppress(OSError):
            os.close(m)
        with suppress(OSError):
            os.close(s)

    ok = client_ok == N and dev_ok == N and reconnects == 0
    print("\n" + (f"PASS: {N} bridge(s) full-duplex {PAYLOAD//1024}KB both ways, byte-exact"
                  if ok else "ISSUES — see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
