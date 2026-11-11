"""Listener admission before TLS, and the connection budget (app/engine/netguard.py).

A mapping's TLS listener (TCP server bridge, Modbus gateway) used to run the handshake
first, with asyncio's 60 s timeout, and check the allow-list only once it was done: any
host that reached the port could make the server run handshakes and keep any number of
sockets open for a minute each, with no log line. Now each connection is admitted right
after accept:
  - the process's budget: refused while the process is within its reserve of the
    open-file limit (serial ports, logs and the configuration keep that much);
  - TLS listeners: a host not in the allow-list is closed before any handshake; at most
    TLS_HANDSHAKES_PER_IP handshakes at a time from one address (TLS_HANDSHAKES_MAX in
    all), more connections wait for a slot (bounded, first come first served) and are
    refused past that; a handshake must be over within TLS_HANDSHAKE_TIMEOUT_S, a failed
    one is logged; real clients, at once and one by one, all complete their handshake;
  - capacity, kicking and "Busy" stay after the handshake, as before.
No serial device is needed (the port is missing; the listener runs anyway). Binds
127.0.0.1 (and 127.0.0.2 where the OS allows it).

Run: python3 tests/test_tls_admission.py
"""
import asyncio
import os
import socket
import ssl
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import modbus, netguard
from app.engine.supervisor import make_runner

MISSING_PORT = "COM250" if sys.platform == "win32" else "/dev/ser2net-test-missing"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_cert():
    from app.web.selfsigned import CertError, generate
    d = tempfile.mkdtemp(prefix="s2n_tlsadm_")
    cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    try:
        generate(cert, key)
    except CertError as e:
        return None, str(e)
    return cert, key


def client_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def mapping(port: int, cert: str = "", key: str = "", protocol: str = "raw", **net) -> MappingConfig:
    network = {"mode": "server", "protocol": protocol, "bind_ip": "127.0.0.1", "port": port,
               "max_connections": 4, **net}
    if cert:
        network.update(tls=True, tls_cert=cert, tls_key=key)
    return MappingConfig.from_dict({"name": "adm", "serial": {"port": MISSING_PORT},
                                    "network": network})


async def closed_by_server(reader, timeout: float) -> bool:
    """True when the server closes the connection (EOF/reset) within ``timeout``, having
    sent nothing."""
    try:
        data = await asyncio.wait_for(reader.read(1), timeout)
    except asyncio.TimeoutError:
        return False
    except (ConnectionError, OSError):
        return True
    return data == b""


async def raw_connect(port: int, source: str = "127.0.0.1"):
    return await asyncio.open_connection("127.0.0.1", port, local_addr=(source, 0))


async def started(m: MappingConfig, lines: list):
    runner = make_runner(m, logger=lines.append)
    await runner.start()
    return runner


async def test_refused_before_the_handshake(cert, key):
    for protocol in ("raw", "modbus"):
        lines = []
        port = free_port()
        runner = await started(mapping(port, cert, key, protocol,
                                       allowed_client_ips=["10.1.2.3"]), lines)
        try:
            r, w = await raw_connect(port)  # sends nothing: no ClientHello at all
            t0 = time.monotonic()
            assert await closed_by_server(r, 3), f"{protocol}: a host not in the allow-list " \
                                                  "must be closed at once, before any handshake"
            assert time.monotonic() - t0 < 2
            w.close()
            assert any(ln.startswith("rejected 127.0.0.1:") and ln.endswith("not in allowed list")
                       for ln in lines), lines
            try:  # a real TLS client gets no handshake either
                await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port,
                                                               ssl=client_ctx()), 3)
            except (ssl.SSLError, ConnectionError, OSError):
                pass
            else:
                raise AssertionError(f"{protocol}: a refused host completed a TLS handshake")
            assert runner.status.client_count == 0
        finally:
            await runner.stop()
    print("TLS listener (bridge, Modbus gateway): a host not in the allow-list is closed "
          "before any handshake, and logged  OK")


async def test_allowed_handshake_and_capacity(cert, key):
    lines = []
    port = free_port()
    runner = await started(mapping(port, cert, key, allowed_client_ips=["127.0.0.1"],
                                   max_connections=1), lines)
    try:
        r1, w1 = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx())
        assert w1.get_extra_info("ssl_object") is not None
        for _ in range(50):
            if runner.status.client_count == 1:
                break
            await asyncio.sleep(0.05)
        assert runner.status.client_count == 1, lines
        # at capacity: the second client still completes its handshake and gets "Busy"
        # over TLS (capacity is decided after the handshake, as before)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx())
        busy = await asyncio.wait_for(r2.read(100), 3)
        assert busy.startswith(b"Busy"), busy
        assert runner.status.client_count == 1
        for w in (w1, w2):
            w.close()
    finally:
        await runner.stop()
    print("TLS listener: an allowed host's handshake, capacity and Busy after it  OK")


async def test_close_does_not_wait_for_a_silent_client(cert, key):
    """A TLS connection the server closes ("Busy" here) is gone TLS_SHUTDOWN_TIMEOUT_S
    after the close even when the client never answers the close_notify (Python 3.10 has
    no such timeout of its own)."""
    from _server import closed_by_server, silent_tls_client

    lines = []
    port = free_port()
    saved = netguard.TLS_SHUTDOWN_TIMEOUT_S
    netguard.TLS_SHUTDOWN_TIMEOUT_S = 1.0
    runner = await started(mapping(port, cert, key, max_connections=1), lines)
    try:
        _r1, w1 = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx())
        for _ in range(50):
            if runner.status.client_count == 1:
                break
            await asyncio.sleep(0.05)
        raw = await asyncio.to_thread(silent_tls_client, port, client_ctx())
        try:
            took = await asyncio.to_thread(closed_by_server, raw, 4)
        finally:
            raw.close()
        assert took < 3, f"the closed connection's socket stayed open {took:.1f} s"
        w1.close()
    finally:
        netguard.TLS_SHUTDOWN_TIMEOUT_S = saved
        await runner.stop()
    print("TLS listener: a connection closed by the server is let go "
          "TLS_SHUTDOWN_TIMEOUT_S later when the client never answers the close  OK")


async def test_handshake_timeout_and_caps(cert, key):
    lines = []
    port = free_port()
    saved = (netguard.TLS_HANDSHAKE_TIMEOUT_S, netguard.BUDGET.handshakes,
             netguard.BUDGET.waiting)
    netguard.TLS_HANDSHAKE_TIMEOUT_S = 1.0
    netguard.BUDGET.handshakes = netguard.Pool(total=4, per_ip=2)
    netguard.BUDGET.waiting = netguard.Pool(total=4, per_ip=1)
    runner = await started(mapping(port, cert, key), lines)
    writers = []
    try:
        # two idle connections from one address hold their handshake slots ...
        idle = [await raw_connect(port) for _ in range(2)]
        writers += [w for _r, w in idle]
        await asyncio.sleep(0.2)
        assert len(netguard.BUDGET.handshakes) == 2, len(netguard.BUDGET.handshakes)
        # ... so a real TLS client from it waits for one of them to end, ...
        waiting = asyncio.ensure_future(asyncio.open_connection("127.0.0.1", port,
                                                                ssl=client_ctx()))
        await asyncio.sleep(0.2)
        assert len(netguard.BUDGET.waiting) == 1 and not waiting.done()
        # ... and one more from it is refused at once (logged), while another address
        # gets a slot
        r3, w3 = await raw_connect(port)
        writers.append(w3)
        assert await closed_by_server(r3, 2), "past the waiting ones, a connection is refused"
        assert any("too many TLS handshakes in progress (2 at once from one address is the "
                   "limit) and waiting for one to end (1 at once from one address" in ln
                   for ln in lines), lines
        try:
            r4, w4 = await raw_connect(port, "127.0.0.2")
        except OSError:
            print("  (no 127.0.0.2 here: the other-address check is skipped)")
        else:
            writers.append(w4)
            await asyncio.sleep(0.2)
            assert len(netguard.BUDGET.handshakes) == 3, "another address must get a slot"
        # the idle ones are dropped once the handshake timeout is over, and that is logged
        t0 = time.monotonic()
        for r, _w in idle:
            assert await closed_by_server(r, 4), "an idle TLS connection outlived the timeout"
        assert time.monotonic() - t0 < 3.5
        assert any("TLS handshake with 127.0.0.1:" in ln and "longer than 1.0 seconds" in ln
                   for ln in lines), lines
        # the waiting client got the slot that came free and completed its handshake
        _r, w = await asyncio.wait_for(waiting, 3)
        writers.append(w)
        assert w.get_extra_info("ssl_object") is not None
        await asyncio.sleep(0.1)
        assert len(netguard.BUDGET.handshakes) <= 1, "finished handshakes must free their slot"
        assert len(netguard.BUDGET.waiting) == 0
        # a handshake still pending when the mapping stops is closed with it, at once
        r5, w5 = await raw_connect(port, "127.0.0.1")
        writers.append(w5)
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        await runner.stop()
        assert time.monotonic() - t0 < 2, "stop() waited for a pending handshake"
        assert await closed_by_server(r5, 2)
        assert len(netguard.BUDGET.handshakes) == 0
    finally:
        (netguard.TLS_HANDSHAKE_TIMEOUT_S, netguard.BUDGET.handshakes,
         netguard.BUDGET.waiting) = saved
        for w in writers:
            w.close()
        await runner.stop()
    print("TLS listener: handshake timeout (logged), per-address handshake cap (a client "
          "past it waits for a slot, past the waiting ones it is refused), a pending "
          "handshake ends with the mapping  OK")


async def test_waiting_for_a_handshake_slot(cert, key):
    """A reconnect burst from one address beyond TLS_HANDSHAKES_PER_IP: the excess waits
    for slots (first come, first served) and every client is served; a connection that
    gets no slot within the timeout is closed (logged), as is one still waiting when the
    mapping stops."""
    lines = []
    port = free_port()
    saved = (netguard.TLS_HANDSHAKE_TIMEOUT_S, netguard.BUDGET.handshakes,
             netguard.BUDGET.waiting)
    netguard.BUDGET.handshakes = netguard.Pool(total=8, per_ip=4)
    netguard.BUDGET.waiting = netguard.Pool(total=32, per_ip=32)
    runner = await started(mapping(port, cert, key, max_connections=32), lines)
    writers = []
    try:
        results = await asyncio.to_thread(tls_clients, port, 24)
        failed = [why for status, why in results if status != "ok"]
        assert not failed, f"{len(failed)} of 24 clients at once failed: {failed[:3]}"
        assert not any("refused" in ln for ln in lines), lines
        await asyncio.sleep(0.2)
        assert len(netguard.BUDGET.handshakes) == 0 and len(netguard.BUDGET.waiting) == 0

        netguard.TLS_HANDSHAKE_TIMEOUT_S = 1.0
        netguard.BUDGET.handshakes = netguard.Pool(total=8, per_ip=1)
        held = await raw_connect(port)  # holds the address's one slot for 1 s
        writers.append(held[1])
        await asyncio.sleep(0.1)
        first = await raw_connect(port)  # gets the slot at 1 s, holds it to 2 s
        second = await raw_connect(port)  # still waiting at 1.1 s: closed
        writers += [first[1], second[1]]
        t0 = time.monotonic()
        assert await closed_by_server(second[0], 3)
        assert 0.8 < time.monotonic() - t0 < 2, time.monotonic() - t0
        assert any("waited 1 seconds for a TLS handshake to end" in ln for ln in lines), lines
        assert await closed_by_server(first[0], 3), "the promoted one has its own timeout"
        # one still waiting when the mapping stops ends at once with it
        writers += [(await raw_connect(port))[1] for _ in range(2)]
        await asyncio.sleep(0.1)
        assert len(netguard.BUDGET.waiting) == 1
        t0 = time.monotonic()
        await runner.stop()
        assert time.monotonic() - t0 < 2, "stop() waited for a connection waiting for a slot"
        await asyncio.sleep(0.1)
        assert len(netguard.BUDGET.handshakes) == 0 and len(netguard.BUDGET.waiting) == 0
        assert not netguard.BUDGET._turns
    finally:
        (netguard.TLS_HANDSHAKE_TIMEOUT_S, netguard.BUDGET.handshakes,
         netguard.BUDGET.waiting) = saved
        for w in writers:
            w.close()
        await runner.stop()
    print("TLS listener: a burst past the per-address handshake cap waits for slots and is "
          "served; a connection that gets none in time is closed (logged), one waiting "
          "ends with the mapping  OK")


def tls_clients(port: int, n: int, request: bytes = b"", timeout: float = 5.0,
                together: bool = True) -> list:
    """``n`` real TLS clients (blocking sockets, one thread each) that send their
    ClientHello right after connect, as real clients do: all at the same moment
    (``together``) or one after the other. Each optionally sends ``request`` and reads
    the first reply. Returns per client ("ok", reply) or ("failed", why)."""
    barrier = threading.Barrier(n) if together else None
    results: list = [("failed", "did not run")] * n

    def one(i: int) -> None:
        try:
            if barrier is not None:
                barrier.wait(timeout)
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as raw, \
                    client_ctx().wrap_socket(raw) as s:
                reply = b""
                if request:
                    s.sendall(request)
                    reply = s.recv(256)
                results[i] = ("ok", reply)
        except Exception as e:  # noqa: BLE001 - reported per client
            results[i] = ("failed", f"{type(e).__name__}: {e}")

    if barrier is None:
        for i in range(n):
            one(i)
        return results
    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


async def test_concurrent_handshakes(cert, key):
    """Real clients against the real listeners (no stand-ins): every handshake completes,
    whether the clients come at once or one after the other, and the connection serves."""
    # Modbus/TCP read holding registers 0..1 from unit 1: the gateway answers it with an
    # exception while its serial port is missing
    read = modbus.build_tcp_adu(7, 1, bytes([0x03, 0, 0, 0, 2]))
    for protocol, request in (("raw", b""), ("modbus", read)):
        lines = []
        port = free_port()
        runner = await started(mapping(port, cert, key, protocol, max_connections=32), lines)
        try:
            for together, n in ((True, 16), (False, 8)):
                results = await asyncio.to_thread(tls_clients, port, n, request,
                                                  together=together)
                failed = [why for status, why in results if status != "ok"]
                assert not failed, (f"{protocol}: {len(failed)} of {n} TLS clients "
                                    f"({'at once' if together else 'one by one'}) failed: "
                                    f"{failed[:3]}; log: {lines[-3:]}")
                if protocol == "modbus":
                    for _status, reply in results:
                        frame = modbus.take_tcp_adu(bytearray(reply))
                        assert frame is not None and frame[0] == 7, reply
                        assert frame[2][0] == 0x83, f"expected an exception reply: {reply!r}"
                await asyncio.sleep(0.2)
            assert len(netguard.BUDGET.handshakes) == 0, "finished handshakes must free their slot"
        finally:
            await runner.stop()
    print("TLS listener (bridge, Modbus gateway): 16 real clients at once and 8 one by one "
          "all complete their handshake and are served  OK")


async def test_budget_refuses_near_the_fd_limit():
    lines = []
    port = free_port()
    saved = (netguard.fd_limit, netguard.open_fds)
    netguard.open_fds = lambda: 1000
    netguard.fd_limit = lambda: 1024  # 1000 >= 1024 - 128 (the reserve)
    runner = await started(mapping(port), lines)
    try:
        before = netguard.BUDGET.refused.get("open_files", 0)
        netguard.BUDGET._counted = (-1.0, 0)
        r, w = await raw_connect(port)
        assert await closed_by_server(r, 2), "a client must be refused near the open-file limit"
        w.close()
        assert netguard.BUDGET.refused.get("open_files", 0) == before + 1
        assert any(ln.startswith("refused 127.0.0.1:") and
                   "1000 of this process's 1024 open files are in use" in ln for ln in lines), lines
        assert runner.status.client_count == 0
        netguard.fd_limit = lambda: 65536
        netguard.BUDGET._counted = (-1.0, 0)
        r, w = await raw_connect(port)
        await asyncio.sleep(0.3)
        assert runner.status.client_count == 1, "with room again, clients get in"
        w.close()
    finally:
        netguard.fd_limit, netguard.open_fds = saved
        netguard.BUDGET._counted = (-1.0, 0)
        await runner.stop()
    print("budget: a client is refused (logged once a minute) near the open-file limit, and "
          "admitted once there is room  OK")


async def test_start_tls_hand_over():
    """What arrives between the end of the handshake and the hand-over is passed on, after
    connection_made; the timeouts are asyncio's arguments (shutdown: Python 3.11+)."""
    calls = []

    class FakeTls:
        def is_closing(self):
            return False

        def set_protocol(self, p):
            calls.append("set_protocol")

        def close(self):
            calls.append("tls close")

    class App(asyncio.Protocol):
        def connection_made(self, t):
            calls.append("connection_made")

        def data_received(self, d):
            calls.append(("data", d))

        def eof_received(self):
            calls.append("eof")

    loop = asyncio.get_running_loop()
    seen = {}

    async def fake_start_tls(transport, protocol, ctx, **kw):
        seen.update(kw)
        protocol.data_received(b"early ")
        protocol.data_received(b"bytes")
        protocol.eof_received()
        return FakeTls()

    class Plain:
        def close(self):
            calls.append("close")

    saved = loop.start_tls
    loop.start_tls = fake_start_tls
    try:
        assert await netguard.start_tls(Plain(), None, App()) is True
    finally:
        loop.start_tls = saved
    assert calls == ["set_protocol", "connection_made", ("data", b"early bytes"), "eof"], calls
    assert seen["server_side"] is True
    assert seen["ssl_handshake_timeout"] == netguard.TLS_HANDSHAKE_TIMEOUT_S
    if sys.version_info >= (3, 11):
        assert seen["ssl_shutdown_timeout"] == netguard.TLS_SHUTDOWN_TIMEOUT_S
    else:
        assert "ssl_shutdown_timeout" not in seen
    print("start_tls: early data handed over after connection_made; handshake/shutdown "
          "timeouts passed  OK")


def test_pool():
    class T:
        def __init__(self):
            self.closing = False

        def is_closing(self):
            return self.closing

    pool = netguard.Pool(total=3, per_ip=2)
    a, b, c = T(), T(), T()
    pool.add(a, "10.0.0.1")
    pool.add(b, "10.0.0.1")
    assert pool.full("10.0.0.1") == "2 at once from one address is the limit"
    assert pool.full("10.0.0.2") == ""
    pool.add(c, "10.0.0.2")
    assert pool.full("10.0.0.3") == "3 at once is the limit"
    a.closing = True  # a closing connection no longer counts
    assert len(pool) == 2 and pool.full("10.0.0.1") == ""
    pool.discard(b)
    assert pool.members() == [c]
    assert netguard.Pool().full("x") == "", "0 = no cap"
    assert netguard.fd_reserve(1024) == 128 and netguard.fd_reserve(65536) == 256
    assert netguard.fd_reserve(100) == 32
    print("Pool: total and per-address caps, closing connections drop out; fd_reserve  OK")


async def main():
    test_pool()
    await test_start_tls_hand_over()
    await test_budget_refuses_near_the_fd_limit()
    cert, key = make_cert()
    if cert is None:
        print(f"SKIP TLS listener checks: no certificate ({key})")
        return
    await test_concurrent_handshakes(cert, key)
    await test_refused_before_the_handshake(cert, key)
    await test_allowed_handshake_and_capacity(cert, key)
    await test_close_does_not_wait_for_a_silent_client(cert, key)
    await test_handshake_timeout_and_caps(cert, key)
    await test_waiting_for_a_handshake_slot(cert, key)


if __name__ == "__main__":
    asyncio.run(main())
    print("\nPASS: listener admission before TLS, handshake limits, connection budget")
