"""Per-connection timers (issue #24): idle timeout, max connection time, connect timeout.

  - idle timeout: a client with no traffic either way is dropped (raw bridge AND the
    Modbus gateway, which used to ignore the option),
  - max connection time: a client is dropped after N seconds even while it is busy,
  - connect timeout: a connect-out attempt that hangs (here: a TLS handshake against
    a server that never answers) is abandoned and retried instead of waiting for
    asyncio's 60 s handshake default / the OS connect timeout,
  - every disconnect is logged with its reason.

No serial hardware or privileges needed (the serial side just sits in reconnect).
Runs on Windows and Linux. Run: python3 tests/test_conn_timers.py
"""
import asyncio
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import MappingConfig
from ser2netweb.engine.bridge import MappingRunner
from ser2netweb.engine.modbus_gateway import ModbusGatewayRunner


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mapping(port=0, network=None, options=None) -> MappingConfig:
    return MappingConfig.from_dict({
        "name": "timers", "enabled": True,
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": port, **(network or {})},
        "options": options or {},
    })


async def _wait_for(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


async def _wait_closed_by_server(reader, timeout: float) -> bool:
    """True once the server has closed the connection (EOF/reset) within timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data = await asyncio.wait_for(reader.read(256), timeout=max(0.05, end - time.monotonic()))
        except asyncio.TimeoutError:
            return False
        except (ConnectionError, OSError):
            return True
        if not data:
            return True
    return False


def _disconnect_lines(logs):
    return [ln for ln in logs if "disconnected" in ln or "closed after" in ln]


async def test_idle_timeout_raw():
    port = _free_port()
    logs = []
    runner = MappingRunner(_mapping(port, options={"idle_timeout_s": 1}), logger=logs.append)
    await runner.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        t0 = time.monotonic()
        assert await _wait_closed_by_server(r, 5), "idle client was not disconnected"
        assert 0.8 <= time.monotonic() - t0 <= 4, f"idle disconnect after {time.monotonic() - t0:.1f}s"
        assert await _wait_for(lambda: _disconnect_lines(logs), 2), logs
        assert "idle timeout" in _disconnect_lines(logs)[0], logs
        w.close()
    finally:
        await runner.stop()
    print("idle timeout (raw): silent client dropped, reason logged  OK")


async def test_max_connection_time_raw():
    port = _free_port()
    logs = []
    runner = MappingRunner(_mapping(port, options={"max_connection_time_s": 2, "idle_timeout_s": 60}),
                           logger=logs.append)
    await runner.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        t0 = time.monotonic()

        async def keep_busy():  # never idle: the lifetime limit must still apply
            while True:
                w.write(b"ping")
                await w.drain()
                await asyncio.sleep(0.2)

        busy = asyncio.create_task(keep_busy())
        try:
            assert await _wait_closed_by_server(r, 6), "busy client outlived max_connection_time_s"
        finally:
            busy.cancel()
        elapsed = time.monotonic() - t0
        assert 1.5 <= elapsed <= 5, f"dropped after {elapsed:.1f}s, expected ~2s"
        assert await _wait_for(lambda: _disconnect_lines(logs), 2), logs
        assert "max connection time" in _disconnect_lines(logs)[0], logs
        w.close()
    finally:
        await runner.stop()
    print(f"max connection time (raw): busy client dropped after {elapsed:.1f}s, reason logged  OK")


async def test_max_connection_time_connect_out_reconnects():
    accepted = asyncio.Queue()

    async def on_conn(reader, writer):
        await accepted.put(writer)

    srv = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    sport = srv.sockets[0].getsockname()[1]
    logs = []
    runner = MappingRunner(_mapping(network={"mode": "client", "remote_host": "127.0.0.1",
                                             "remote_port": sport},
                                    options={"max_connection_time_s": 1}), logger=logs.append)
    await runner.start()
    try:
        first = await asyncio.wait_for(accepted.get(), 5)
        second = await asyncio.wait_for(accepted.get(), 5)  # dropped at 1 s, then reconnected
        assert any("max connection time" in ln for ln in _disconnect_lines(logs)), logs
        for w in (first, second):
            w.close()
    finally:
        await runner.stop()
        srv.close()
    print("max connection time (connect-out): link recycled and re-established  OK")


async def test_connect_timeout_aborts_hung_handshake():
    """A server that accepts TCP but never answers the TLS ClientHello: without a
    connect timeout the bridge would sit in the handshake for 60 s."""
    accepts = []

    async def silent(reader, writer):
        accepts.append(time.monotonic())
        await reader.read()  # never reply; just wait for the bridge to give up

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    sport = srv.sockets[0].getsockname()[1]
    logs = []
    runner = MappingRunner(_mapping(network={"mode": "client", "remote_host": "127.0.0.1",
                                             "remote_port": sport, "tls": True,
                                             "connect_timeout_s": 1}), logger=logs.append)
    await runner.start()
    try:
        assert await _wait_for(lambda: any("timed out after 1s" in ln for ln in logs), 4), logs
        assert runner.status.last_error == "timed out after 1s", runner.status.last_error
        assert await _wait_for(lambda: len(accepts) >= 2, 4), "connect-out did not retry"
    finally:
        await runner.stop()
        srv.close()
    print("connect timeout: hung TLS handshake abandoned after 1s and retried  OK")


async def test_connect_timeout_zero_means_no_limit():
    async def silent(reader, writer):
        await reader.read()

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    sport = srv.sockets[0].getsockname()[1]
    logs = []
    runner = MappingRunner(_mapping(network={"mode": "client", "remote_host": "127.0.0.1",
                                             "remote_port": sport, "tls": True,
                                             "connect_timeout_s": 0}), logger=logs.append)
    await runner.start()
    try:
        await asyncio.sleep(2.0)
        assert not any("connect-out failed" in ln for ln in logs), logs
    finally:
        await runner.stop()
        srv.close()
    print("connect timeout 0: no limit of our own (asyncio/OS defaults apply)  OK")


async def test_connect_timeout_excludes_dns():
    """The DNS lookup is not under connect_timeout_s: getaddrinfo runs in a worker
    thread that a timeout can't stop, so timing it out would only pile up stuck
    threads (one per retry) during a DNS outage. A resolver slower than the timeout
    must therefore still connect, with one lookup in flight at a time."""
    loop = asyncio.get_running_loop()
    real_getaddrinfo = loop.getaddrinfo
    lookups = {"now": 0, "max": 0}

    async def slow_getaddrinfo(*args, **kwargs):
        lookups["now"] += 1
        lookups["max"] = max(lookups["max"], lookups["now"])
        try:
            await asyncio.sleep(1.5)  # resolver slower than the 1 s connect timeout
            return await real_getaddrinfo(*args, **kwargs)
        finally:
            lookups["now"] -= 1

    accepted = asyncio.Queue()

    async def on_conn(reader, writer):
        await accepted.put(writer)

    srv = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    sport = srv.sockets[0].getsockname()[1]
    logs = []
    runner = MappingRunner(_mapping(network={"mode": "client", "remote_host": "localhost",
                                             "remote_port": sport, "connect_timeout_s": 1}),
                           logger=logs.append)
    loop.getaddrinfo = slow_getaddrinfo
    try:
        await runner.start()
        w = await asyncio.wait_for(accepted.get(), 6)
        assert not any("timed out" in ln for ln in logs), logs
        assert lookups["max"] == 1, f"{lookups['max']} lookups were in flight at once"
        w.close()
    finally:
        loop.getaddrinfo = real_getaddrinfo
        await runner.stop()
        srv.close()
    print("connect timeout: a slow DNS lookup is outside it (connects; one lookup at a time)  OK")


async def test_modbus_gateway_idle_and_max_time():
    for options, expect in (({"idle_timeout_s": 1}, "idle timeout"),
                            ({"max_connection_time_s": 1}, "max connection time")):
        port = _free_port()
        logs = []
        gw = ModbusGatewayRunner(_mapping(port, network={"protocol": "modbus"}, options=options),
                                 logger=logs.append)
        await gw.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            assert await _wait_closed_by_server(r, 5), f"modbus master not dropped ({expect})"
            assert await _wait_for(lambda: _disconnect_lines(logs), 2), logs
            assert expect in _disconnect_lines(logs)[0], logs
            w.close()
        finally:
            await gw.stop()
    print("modbus gateway: idle timeout + max connection time now enforced, reasons logged  OK")


async def test_kick_reason_logged():
    port = _free_port()
    logs = []
    runner = MappingRunner(_mapping(port, network={"max_connections": 1, "kick_old_user": True}),
                           logger=logs.append)
    await runner.start()
    try:
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5)
        _r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        assert await _wait_closed_by_server(r1, 5), "old client was not kicked"
        assert await _wait_for(lambda: _disconnect_lines(logs), 2), logs
        assert "kicked for new client" in _disconnect_lines(logs)[0], logs
        w1.close()
        w2.close()
    finally:
        await runner.stop()
    print("kick-old-user: the kicked client's disconnect says why  OK")


async def main():
    await test_idle_timeout_raw()
    await test_max_connection_time_raw()
    await test_max_connection_time_connect_out_reconnects()
    await test_connect_timeout_aborts_hung_handshake()
    await test_connect_timeout_zero_means_no_limit()
    await test_connect_timeout_excludes_dns()
    await test_modbus_gateway_idle_and_max_time()
    await test_kick_reason_logged()
    print("\nPASS: per-connection timers (idle / max connection time / connect timeout)")


if __name__ == "__main__":
    asyncio.run(main())
