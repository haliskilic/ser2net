"""stop() must not deadlock when clients are connected (H3).

On Python 3.12+ asyncio.Server.wait_closed() blocks until all active
connections finish. The old stop() awaited wait_closed() BEFORE kicking the
connected clients, so stopping/restarting/deleting a mapping that had a live
TCP client could hang the event loop forever. This test connects a real TCP
client to a server-mode mapping and asserts stop() completes promptly.

No serial hardware needed (the serial side just sits in reconnect; raw protocol
does not require an open port). Runs on Windows and Linux.

stop() must also return when the serial device takes no data (CTS low, XOFF, a PTY or
virtual port nobody reads) while data is still waiting for it: closing the port waited
for that data to go out, for good. Checked on PTYs (POSIX; skipped on Windows) for a UDP
mapping and a serial<->serial bridge. And with a stand-in port whose OS output queue
never drains (the transport's buffer empty or not; Windows too): the close drops what is
left after the grace period, purges the OS queue and closes the port without ever
waiting for the OS drain (pyserial's flush(), tcdrain on POSIX, which would block for
good), while a queue that drains in time is not purged.

A client connection whose peer reads nothing is closed (aborted) within the graceful
close timeout, instead of holding its connection slot.

Run: python3 tests/test_stop_with_clients.py
"""
import asyncio
import os
import socket
import sys
import threading
import time
from contextlib import suppress
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine.bridge import MappingRunner


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_stop_with_connected_client():
    port = _free_port()
    m = MappingConfig.from_dict({
        "name": "stop-test", "enabled": True,
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw",
                    "bind_ip": "127.0.0.1", "port": port, "max_connections": 4},
    })
    runner = MappingRunner(m, logger=lambda _msg: None)
    await runner.start()
    await asyncio.sleep(0.2)

    # connect a real client and make sure it's registered
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    for _ in range(50):
        if runner._clients:
            break
        await asyncio.sleep(0.02)
    assert runner._clients, "client did not register with the runner"

    # the regression: this must return promptly, not hang on wait_closed()
    await asyncio.wait_for(runner.stop(), timeout=5.0)
    assert runner.status.state == "stopped"
    assert runner.status.client_count == 0 and runner.status.clients == []
    # kicked clients tear down their own tasks; they should drain shortly after
    for _ in range(50):
        if not runner._clients:
            break
        await asyncio.sleep(0.02)
    assert not runner._clients, "kicked clients did not drain after stop()"

    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    print("stop() with a connected client returned promptly (no 3.12+ deadlock)  OK")


def _pty() -> tuple[int, int, str]:
    import tty
    master, slave = os.openpty()
    tty.setraw(master)
    return master, slave, os.ttyname(slave)


async def _wait_for(cond, seconds: float = 5.0) -> bool:
    for _ in range(int(seconds / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def _stop_promptly(runner: MappingRunner, what: str) -> None:
    t0 = time.monotonic()
    await asyncio.wait_for(runner.stop(), timeout=15.0)
    took = time.monotonic() - t0
    assert took < 6, f"{what}: stop() took {took:.1f} s"
    assert runner.status.state == "stopped"


async def test_stop_while_the_device_takes_nothing():
    """A serial port whose device takes no data (here a PTY nobody reads; CTS low or XOFF
    alike) with data still waiting for it: stop() returns within the close timeout, for a
    network mapping and for a serial<->serial bridge."""
    if os.name != "posix":
        print("SKIP stop() with a device that takes nothing: needs POSIX PTYs")
        return
    from app.engine.serial_link import SERIAL_CLOSE_TIMEOUT_S

    assert SERIAL_CLOSE_TIMEOUT_S <= 2.5
    master, slave, path = _pty()
    port = _free_port()
    runner = MappingRunner(MappingConfig.from_dict({
        "name": "stuck-udp", "serial": {"port": path, "baudrate": 9600},
        "network": {"mode": "udp", "protocol": "raw", "bind_ip": "127.0.0.1", "port": port}}),
        logger=lambda _msg: None)
    await runner.start()
    try:
        assert await _wait_for(lambda: runner._link.writer is not None), "the PTY did not open"
        writer = runner._link.writer
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            for i in range(200):
                udp.sendto(b"x" * 1000, ("127.0.0.1", port))
                if i % 20 == 0:
                    await asyncio.sleep(0.01)
        assert await _wait_for(lambda: writer.transport.get_write_buffer_size() > 0), \
            "the PTY took everything: nothing is left waiting for the device"
        await _stop_promptly(runner, "UDP mapping")
    finally:
        await runner.stop()
        os.close(master)
        os.close(slave)

    a_master, a_slave, a_path = _pty()
    b_master, b_slave, b_path = _pty()  # B is never read
    runner = MappingRunner(MappingConfig.from_dict({
        "name": "stuck-bridge", "kind": "serialbridge",
        "serial": {"port": a_path, "baudrate": 9600},
        "serial_b": {"port": b_path, "baudrate": 9600}}), logger=lambda _msg: None)
    await runner.start()
    try:
        assert await _wait_for(lambda: runner.status.state == "running"), runner.status.state
        os.set_blocking(a_master, False)
        sent = 0
        for _ in range(400):
            with suppress(BlockingIOError):
                sent += os.write(a_master, b"y" * 1024)
            await asyncio.sleep(0.005)
        assert sent > 64 * 1024, sent
        await asyncio.sleep(0.3)
        await _stop_promptly(runner, "serial<->serial bridge")
    finally:
        await runner.stop()
        for fd in (a_master, a_slave, b_master, b_slave):
            os.close(fd)
    print("stop() with data waiting for a device that takes nothing returns within the "
          "close timeout (UDP mapping, serial<->serial bridge)  OK")


class HeldPort:
    """A port whose device takes nothing (more): writes are refused, ``out_waiting`` is
    what the OS still holds for it, and flush() (the OS drain: tcdrain, or pyserial's
    out_waiting loop on Windows) would wait as long as the line is held."""

    def __init__(self, queued):
        self.timeout = self.write_timeout = None
        self.in_waiting = 0
        self.out_waiting = queued
        self.resets = 0
        self.flushed = threading.Event()   # the close waited for the OS drain
        self.released = threading.Event()  # (lets such a wait end once the test is done)
        self.closed = threading.Event()
        self._a, self._b = socket.socketpair()  # a descriptor for the POSIX transport

    def fileno(self):
        return self._a.fileno()

    def read(self, size=1):
        return b""

    def write(self, data):
        raise BlockingIOError(11, "no room")

    def flush(self):
        self.flushed.set()
        self.released.wait(10)

    def reset_output_buffer(self):
        self.resets += 1
        self.out_waiting = 0

    def close(self):
        self.closed.set()
        self._a.close()
        self._b.close()


async def _close_held(port, buffered: bytes = b""):
    """close_stream() on a stream over ``port``; (seconds it took, log lines)."""
    from app.engine import serial_compat, serial_link

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport = serial_compat.Transport(loop, protocol, port)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    await asyncio.sleep(0.01)
    if buffered:
        writer.write(buffered)  # refused by the port: kept in the transport's buffer
        assert transport.get_write_buffer_size() == len(buffered)
    lines = []
    t0 = time.monotonic()
    await serial_link.close_stream(writer, lines.append)
    return time.monotonic() - t0, lines


async def test_close_never_waits_for_the_os_drain():
    from app.engine import serial_link

    with mock.patch.object(serial_link, "SERIAL_CLOSE_TIMEOUT_S", 0.5):
        for buffered in (b"", b"z" * 100):
            port = HeldPort(queued=500)
            try:
                took, lines = await _close_held(port, buffered)
                assert took < 1.5, f"close took {took:.1f} s"
                assert port.closed.wait(2), "the port was not closed"
                assert not port.flushed.is_set(), "the close waited for the OS drain"
                assert port.resets == 1, "what the OS held for the device was not purged"
                assert lines == [f"serial close: dropped {500 + len(buffered)} byte(s) the "
                                 "device did not take within 0.5 s"], lines
            finally:
                port.released.set()

        port = HeldPort(queued=300)  # a device that takes it, just not at once

        def drains():
            while port.out_waiting:
                time.sleep(0.05)
                port.out_waiting = max(0, port.out_waiting - 100)

        threading.Thread(target=drains, daemon=True).start()
        try:
            took, lines = await _close_held(port)
            assert port.closed.wait(2) and not port.flushed.is_set()
            assert port.resets == 0 and not lines, (port.resets, lines)
            assert took < 1.0, f"close took {took:.1f} s"
        finally:
            port.released.set()
    print("serial close: output the device has not taken within the grace period is "
          "dropped (logged) and the OS queue purged; output that drains in time is not; "
          "the OS drain (tcdrain) is never waited for  OK")


async def test_close_of_a_peer_that_reads_nothing():
    from app.engine import bridge

    got = asyncio.get_running_loop().create_future()
    server = await asyncio.start_server(lambda r, w: got.set_result(w), "127.0.0.1", 0)
    peer = socket.socket()
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    peer.connect(server.sockets[0].getsockname())
    peer.setblocking(False)
    try:
        writer = await asyncio.wait_for(got, 5)
        # far more than the peer's and the kernel's buffers hold. (Windows' Proactor loop
        # hands what is queued to one overlapped send, which the OS may take whole while
        # its buffer is not full yet: the next round's sends then wait.)
        for _round in range(5):
            for _ in range(200):
                writer.write(b"q" * 65536)
            await asyncio.sleep(0.2)
            if writer.transport.get_write_buffer_size() > 0:
                break
        assert writer.transport.get_write_buffer_size() > 0, "the OS took it all"
        with mock.patch.object(bridge, "TCP_CLOSE_TIMEOUT_S", 0.5):
            t0 = time.monotonic()
            await bridge._close_client(writer)
            took = time.monotonic() - t0
        assert took < 1.5, f"closing took {took:.1f} s"
        assert writer.transport.is_closing()
        await asyncio.sleep(0.1)
        assert writer.transport.get_extra_info("socket") is None or \
            writer.transport.get_extra_info("socket").fileno() == -1, "still open"
    finally:
        peer.close()
        server.close()
        await server.wait_closed()
    print("a client whose peer reads nothing is cut off within the close timeout  OK")


async def main():
    await test_stop_with_connected_client()
    await test_stop_while_the_device_takes_nothing()
    await test_close_never_waits_for_the_os_drain()
    await test_close_of_a_peer_that_reads_nothing()
    print("\nPASS: stop() client teardown ordering, and with a device that takes nothing")


if __name__ == "__main__":
    asyncio.run(main())
