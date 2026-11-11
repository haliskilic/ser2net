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
mapping and a serial<->serial bridge.

Run: python3 tests/test_stop_with_clients.py
"""
import asyncio
import os
import socket
import sys
import time
from contextlib import suppress

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


async def main():
    await test_stop_with_connected_client()
    await test_stop_while_the_device_takes_nothing()
    print("\nPASS: stop() client teardown ordering, and with a device that takes nothing")


if __name__ == "__main__":
    asyncio.run(main())
