"""A failed start() must not leak the serial supervisor task (H4).

start() creates the serial supervisor task FIRST, then brings up the network
side. If the network side fails (address-in-use, bad TLS cert, ...), the old
code left the serial task running — it kept the serial device open forever, so
no other mapping could ever use that port. This test forces a start() failure
(missing TLS cert) and asserts the runner cleaned itself up.

Run: python3 tests/test_start_failure_cleanup.py
"""
import asyncio
import os
import socket
import sys

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


async def test_failed_start_cleans_serial_task():
    m = MappingConfig.from_dict({
        "name": "bad-tls", "enabled": True,
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw",
                    "bind_ip": "127.0.0.1", "port": _free_port(),
                    "tls": True, "tls_cert": "/no/such/cert.pem", "tls_key": "/no/such/key.pem"},
    })
    runner = MappingRunner(m, logger=lambda _msg: None)

    raised = False
    try:
        await runner.start()
    except BaseException:
        raised = True
    assert raised, "start() should have failed on the missing TLS certificate"

    # the regression: the serial supervisor task must not be left running
    assert runner._serial_task is None, "serial supervisor task leaked after failed start()"
    assert runner._server is None, "listener leaked after failed start()"
    assert runner.serial_instance is None, "serial instance leaked after failed start()"

    # give the loop a tick to confirm nothing is still scheduled for this runner
    await asyncio.sleep(0.05)
    leaked = [t for t in asyncio.all_tasks()
              if t is not asyncio.current_task() and not t.done()
              and (t.get_name() or "").startswith(("serial:", "connectout:", "serbridge:"))]
    assert not leaked, f"leaked engine tasks after failed start(): {[t.get_name() for t in leaked]}"
    print("failed start() cleaned up the serial task / listener (no device leak)  OK")


async def main():
    await test_failed_start_cleans_serial_task()
    print("\nPASS: start() failure cleanup (H4)")


if __name__ == "__main__":
    asyncio.run(main())
