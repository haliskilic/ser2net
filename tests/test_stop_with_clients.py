"""stop() must not deadlock when clients are connected (H3).

On Python 3.12+ asyncio.Server.wait_closed() blocks until all active
connections finish. The old stop() awaited wait_closed() BEFORE kicking the
connected clients, so stopping/restarting/deleting a mapping that had a live
TCP client could hang the event loop forever. This test connects a real TCP
client to a server-mode mapping and asserts stop() completes promptly.

No serial hardware needed (the serial side just sits in reconnect; raw protocol
does not require an open port). Runs on Windows and Linux.

Run: python3 tests/test_stop_with_clients.py
"""
import asyncio
import os
import socket
import sys
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


async def main():
    await test_stop_with_connected_client()
    print("\nPASS: stop() client teardown ordering (H3)")


if __name__ == "__main__":
    asyncio.run(main())
