"""A device that takes no data (CTS low, XOFF, a PTY nobody reads): ser2net keeps going (POSIX).

pyserial 3.5's write() tries again while the OS refuses a write for now (EAGAIN), and with
the write_timeout of 0 that pyserial-asyncio-fast sets it never stops: while one device
took no data, the event loop spun in that write and every other mapping, client and the
web UI stood still (pyserial issue 280). ser2net's port class writes once and leaves the
rest to the transport, which waits until the port is writable (serial_compat; the port
level is tested in test_serial_fd_ceiling.py). Checked here end to end, on PTYs
(os.openpty, no socat):
  * a raw TCP mapping whose device reads nothing, with the way to it full (the port's next
    write is refused), and a client that sends it 8 MB: the client is held back (TCP
    backpressure), what the mapping buffers for the device stays bounded, the event loop
    keeps running (a 10 ms timer is never late by 0.5 s), the process does not spin (its
    CPU time stays well under the time passed), and a second mapping on another PTY
    passes data both ways in the meantime (with pyserial's own write the loop stalls in
    the first write and this fails);
  * once the device reads again, all of it arrives in order and the client finishes.
Skipped on Windows (no PTYs there).

Run: python3 tests/test_blocked_output.py
"""
import asyncio
import contextlib
import os
import select
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import free_port

CHUNK = 64 * 1024
# far more than the way there holds: a receive buffer only grows while its reader reads, so
# with the mapping not reading, the client is held back after about 1 MB
PAYLOAD = bytes(range(256)) * (8 * 4096)  # 8 MB


class Pty:
    """A PTY pair: the slave's path is the mapping's port, the master end is the device."""

    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)  # the mapping opens it by path

    def recv(self, n: int, timeout: float = 5.0) -> bytes:
        got, end = bytearray(), time.monotonic() + timeout
        while len(got) < n:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.master], [], [], left)[0]:
                break
            got += os.read(self.master, min(n - len(got), 1 << 20))
        return bytes(got)

    def fill(self) -> bytes:
        """Fill the way to the device until the OS takes no more (as a device that stopped
        taking data leaves it): the next write to the port is refused (EAGAIN). The kernel
        moves what a PTY holds on in the background and then takes a little more, so this
        writes until a pause brings no more room."""
        fd = os.open(self.path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        sent = bytearray()
        try:
            while True:
                before = len(sent)
                with contextlib.suppress(BlockingIOError):
                    while True:
                        sent += b"F" * os.write(fd, b"F" * 4096)
                if len(sent) == before:
                    return bytes(sent)
                time.sleep(0.2)
        finally:
            os.close(fd)

    def close(self) -> None:
        os.close(self.master)


def mapping(name: str, path: str, port: int):
    from ser2netweb.config import MappingConfig
    return MappingConfig.from_dict({
        "name": name, "enabled": True, "serial": {"port": path, "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": port}})


class Sender(threading.Thread):
    """A client with a blocking socket (outside the event loop, as a remote client is): it
    sends PAYLOAD and counts what the network took."""

    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port, self.sent, self.error = port, 0, None
        self.done = threading.Event()

    def run(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=120) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
                for i in range(0, len(PAYLOAD), CHUNK):
                    s.sendall(PAYLOAD[i:i + CHUNK])
                    self.sent += CHUNK
                s.shutdown(socket.SHUT_WR)
        except OSError as e:
            self.error = e
        finally:
            self.done.set()


async def run():
    from ser2netweb.engine.supervisor import Supervisor

    stuck, other = Pty(), Pty()
    sup = Supervisor(logger=lambda _m: None)
    p_stuck, p_other = free_port(), free_port()
    m_stuck, m_other = mapping("stuck", stuck.path, p_stuck), mapping("other", other.path, p_other)
    loop = asyncio.get_running_loop()
    late: list[float] = []

    async def ticker():
        while True:
            t = loop.time()
            await asyncio.sleep(0.01)
            late.append(loop.time() - t - 0.01)

    tick = None
    try:
        for m in (m_stuck, m_other):
            ok, msg = await sup.apply_mapping(m)
            assert ok, msg
        for _ in range(100):
            if all(sup.status(m.id)["state"] == "running" for m in (m_stuck, m_other)):
                break
            await asyncio.sleep(0.05)
        r2, w2 = await asyncio.open_connection("127.0.0.1", p_other)
        await asyncio.sleep(0.2)

        # the device has stopped taking data with the way to it full: the mapping's next
        # write is refused at once (where pyserial's own write spun)
        filled = stuck.fill()
        sender = Sender(p_stuck)
        sender.start()
        tick = asyncio.create_task(ticker())
        cpu0, t0 = time.process_time(), time.monotonic()
        for i in range(15):  # meanwhile the other mapping works, both ways
            os.write(other.master, b"reading %02d\n" % i)
            assert await asyncio.wait_for(r2.readexactly(11), 3) == b"reading %02d\n" % i
            w2.write(b"cmd %02d\n" % i)
            await w2.drain()
            assert await asyncio.to_thread(other.recv, 7) == b"cmd %02d\n" % i
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
        cpu, wall = time.process_time() - cpu0, time.monotonic() - t0
        runner = sup.get_runner(m_stuck.id)
        held = runner._link.writer.transport.get_write_buffer_size()
        assert max(late) < 0.5, f"the event loop stalled for {max(late):.2f} s"
        assert cpu < 0.5 * wall, f"the process spun: {cpu:.2f} s CPU in {wall:.2f} s"
        assert not sender.done.is_set() and sender.sent < len(PAYLOAD), \
            (sender.sent, sender.error, "the client was not held back")
        assert held <= 1024 * 1024, f"the mapping holds {held} bytes for the device"
        assert sup.status(m_stuck.id)["state"] == "running", sup.status(m_stuck.id)
        print(f"a device that takes nothing: the client is held back after {sender.sent >> 10} "
              f"KB, the mapping holds {held} bytes, the loop is never late by more than "
              f"{max(late) * 1000:.0f} ms, CPU {cpu:.2f} s in {wall:.1f} s, the other "
              "mapping passes data both ways  OK")

        want = filled + PAYLOAD
        got = await asyncio.to_thread(stuck.recv, len(want), 60)
        assert len(got) == len(want) and got == want, \
            f"{len(got)} of {len(want)} bytes arrived (or not in order)"
        await asyncio.to_thread(sender.done.wait, 30)
        assert sender.done.is_set() and sender.error is None, sender.error
        print(f"the device reads again: all {len(PAYLOAD) >> 20} MB arrive in order, the client "
              "finishes  OK")
        w2.close()
    finally:
        if tick is not None:
            tick.cancel()
        t = time.monotonic()
        await sup.stop_all()
        assert time.monotonic() - t < 10, "stop() waited for the device"
        stuck.close()
        other.close()


def _stuck(*_args):
    raise AssertionError("the event loop is stuck: a write to the device that takes nothing "
                         "did not return")


def main():
    if os.name != "posix":
        print("SKIP: POSIX only (PTYs)")
        return
    import signal
    signal.signal(signal.SIGALRM, _stuck)  # a loop spinning in a write never gets to a timeout
    signal.alarm(60)
    try:
        asyncio.run(run())
    finally:
        signal.alarm(0)
    print("\nPASS: a device that takes no data blocks nothing else")


if __name__ == "__main__":
    main()
