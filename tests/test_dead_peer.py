"""Dead-peer (half-open TCP) detection — issue #24, the "stays ESTABLISHED" bug.

A client that vanishes WITHOUT a FIN/RST (power cut, cable pull, Wi-Fi/VPN drop,
NAT/firewall state expiry, a crashed VM, ...) leaves the server-side socket
ESTABLISHED. With the raw protocol nothing is sent while the device is quiet, so a
bridge without TCP keepalive never notices: the dead client keeps its slot forever
and — with the default max_connections=1 — every reconnect is refused with
"Busy: maximum connections reached".

Two layers:

1. Always (portable, unprivileged): the accepted (server-mode), connect-out and
   Modbus-gateway sockets carry SO_KEEPALIVE with the mapping's idle / interval /
   count (default on: 60 s / 10 s / 3), and tcp_keepalive=false turns it off.
2. Opt-in end-to-end (SER2NET_BLACKHOLE_TESTS=1): a REAL half-open connection is
   produced by blackholing one direction of a loopback flow — Linux: iptables (as
   root or via passwordless sudo); Windows: WinDivert (``pip install pydivert``, run
   as Administrator). Then:
     - keepalive on  -> the dead peer is reaped within idle + interval * count and a
                        new client gets the slot (server, connect-out, Modbus);
     - keepalive off -> (control) the dead client is still ESTABLISHED after the
                        same window and a new client is refused "Busy" — the
                        reported bug, which also proves the harness really produces
                        a half-open connection (FIN/RST never reach the server).
   Also: a dead client while the device keeps streaming (data in flight, so Linux
   needs TCP_USER_TIMEOUT), the legacy Windows SIO_KEEPALIVE_VALS path, a dead remote
   in connect-out mode (it reconnects), a dead Modbus master, and a connect-out SYN
   to a silent host (connect timeout). FIN/RST closes are checked portably.
   CI sets SER2NET_BLACKHOLE_TESTS=1, so a missing backend FAILS there instead of
   silently skipping. Locally it is opt-in because it edits the firewall
   (tip: run it inside a throwaway network namespace on Linux).

Windows production runs the SelectorEventLoop (app/runtime.py), so the scenarios run
on that loop there; the server-side socket checks also run on the default
(Proactor) loop. Run: python3 tests/test_dead_peer.py
"""
import asyncio
import contextlib
import faulthandler
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MappingConfig
from app.engine import sockopts
from app.engine.bridge import MappingRunner
from app.engine.modbus_gateway import ModbusGatewayRunner

# Short keepalive so a dead peer is detected in ~3 s (1 s idle + 2 probes x 1 s).
KA = {"tcp_keepalive": True, "keepalive_idle_s": 1, "keepalive_interval_s": 1,
      "keepalive_count": 2}
OPT_IN = os.environ.get("SER2NET_BLACKHOLE_TESTS") == "1"
MARGIN_S = 5.0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mapping(port: int, **network) -> MappingConfig:
    return MappingConfig.from_dict({
        "name": "dead-peer", "enabled": True,
        # no device needed: raw/modbus serve TCP while the serial side reconnects
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                    "port": port, **network},
    })


async def _wait_for(pred, timeout: float, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    return pred()


async def _read_some(reader, timeout: float) -> bytes:
    with contextlib.suppress(asyncio.TimeoutError, ConnectionError, OSError):
        return await asyncio.wait_for(reader.read(256), timeout=timeout)
    return b""


async def _close(writer) -> None:
    with contextlib.suppress(Exception):
        writer.transport.abort()


def _keepalive_state(sock) -> dict:
    """SO_KEEPALIVE plus whatever idle/interval/count the OS lets us read back."""
    state = {"on": bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))}
    for key, name in (("idle", "TCP_KEEPIDLE"), ("interval", "TCP_KEEPINTVL"),
                      ("count", "TCP_KEEPCNT")):
        opt = getattr(socket, name, None)
        if opt is None:
            continue
        with contextlib.suppress(OSError):
            state[key] = sock.getsockopt(socket.IPPROTO_TCP, opt)
    return state


def _assert_keepalive(state: dict, idle: int, interval: int, count: int, where: str) -> None:
    assert state["on"], f"{where}: SO_KEEPALIVE is off - a vanished peer is never detected"
    want = {"idle": idle, "interval": interval, "count": count}
    for key, value in want.items():
        if key in state:
            assert state[key] == value, f"{where}: {key}={state[key]}, expected {value}"
    unread = [k for k in want if k not in state]
    if unread:
        print(f"   (note: this OS does not report {', '.join(unread)} back; only SO_KEEPALIVE checked)")


def _detect_budget() -> float:
    """Seconds the kernel needs to declare a silent peer dead with KA, plus margin.
    Pre-1703 Windows can't set the probe count (fixed at 10), hence the fallback."""
    count = KA["keepalive_count"] if hasattr(socket, "TCP_KEEPCNT") else 10
    return KA["keepalive_idle_s"] + KA["keepalive_interval_s"] * count + MARGIN_S


def _server_side_established(listen_port: int, peer_port: int) -> bool:
    """Is the SERVER end (local=listen_port, remote=peer_port) still ESTABLISHED in
    the OS socket table (what `netstat` shows)?"""
    import psutil

    proc = psutil.Process()
    conns = (proc.net_connections if hasattr(proc, "net_connections") else proc.connections)(kind="tcp")
    return any(c.laddr and c.raddr and c.laddr.port == listen_port and c.raddr.port == peer_port
               and c.status == psutil.CONN_ESTABLISHED for c in conns)


# --------------------------------------------------------------------------
# blackhole backends: drop every packet of ONE direction of a loopback TCP flow,
# so the receiver sees its peer vanish with no FIN/RST (a dead host, not a close)
# --------------------------------------------------------------------------
def _sudo() -> list:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


class _IptablesBlackhole:
    def __init__(self, src_port, dst_port: int) -> None:  # src_port None = any
        sport = ["--sport", str(src_port)] if src_port else []
        self._rule = ["INPUT", "-i", "lo", "-p", "tcp", *sport, "--dport", str(dst_port),
                      "-m", "comment", "--comment", "ser2net-test-dead-peer", "-j", "DROP"]

    @staticmethod
    def _iptables(*args) -> None:
        subprocess.run([*_sudo(), "iptables", "-w", *args], check=True,
                       capture_output=True, timeout=15)

    def __enter__(self):
        self._iptables("-I", *self._rule)
        return self

    def __exit__(self, *_exc) -> None:
        self._iptables("-D", *self._rule)


class _WinDivertBlackhole:
    def __init__(self, src_port, dst_port: int) -> None:  # src_port None = any
        import pydivert

        sport = f" and tcp.SrcPort == {src_port}" if src_port else ""
        # DROP mode: the driver discards matching packets itself, so no reader thread
        # (pydivert's timed recv() cancels overlapped I/O unsafely and can crash).
        self._w = pydivert.WinDivert(f"loopback and tcp.DstPort == {dst_port}{sport}",
                                     flags=pydivert.Flag.DROP)

    def __enter__(self):
        self._w.open()
        return self

    def __exit__(self, *_exc) -> None:
        self._w.close()


def _blackhole_backend():
    """(backend class, None) when one is usable here, else (None, reason)."""
    if sys.platform.startswith("linux"):
        if not (shutil.which("iptables") or os.path.exists("/usr/sbin/iptables")):
            return None, "iptables not found"
        try:
            r = subprocess.run([*_sudo(), "iptables", "-w", "-S", "INPUT"],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"iptables not runnable: {e}"
        if r.returncode != 0:
            return None, ("iptables needs root or passwordless sudo: "
                          f"{(r.stderr or r.stdout).strip()[:200]}")
        return _IptablesBlackhole, None
    if sys.platform == "win32":
        try:
            import pydivert
        except ImportError:
            return None, "pydivert is not installed (pip install pydivert)"
        try:
            with pydivert.WinDivert("tcp.SrcPort == 1 and tcp.DstPort == 1"):
                pass
        except Exception as e:
            return None, f"WinDivert driver unavailable (run as Administrator): {e}"
        return _WinDivertBlackhole, None
    return None, f"no blackhole backend for {sys.platform}"


# --------------------------------------------------------------------------
# 1) portable: keepalive is applied to every TCP data socket
# --------------------------------------------------------------------------
async def test_server_socket_keepalive_default():
    port = _free_port()
    runner = MappingRunner(_mapping(port), logger=lambda _m: None)
    await runner.start()
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
        conn = next(iter(runner._clients))
        _assert_keepalive(_keepalive_state(conn.writer.get_extra_info("socket")),
                          60, 10, 3, "server-mode default")
        await _close(w)
    finally:
        await runner.stop()
    print("server mode: accepted socket has keepalive ON by default (60s/10s/3)  OK")


async def test_server_socket_keepalive_custom_and_off():
    for net, expect_on in ((KA, True), ({"tcp_keepalive": False}, False)):
        port = _free_port()
        runner = MappingRunner(_mapping(port, **net), logger=lambda _m: None)
        await runner.start()
        try:
            _r, w = await asyncio.open_connection("127.0.0.1", port)
            assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
            state = _keepalive_state(next(iter(runner._clients)).writer.get_extra_info("socket"))
            if expect_on:
                _assert_keepalive(state, 1, 1, 2, "server-mode custom")
            else:
                assert not state["on"], "tcp_keepalive=false must leave SO_KEEPALIVE off"
            await _close(w)
        finally:
            await runner.stop()
    print("server mode: custom idle/interval/count applied; tcp_keepalive=false disables it  OK")


async def test_extreme_keepalive_values_are_safe():
    """The form's maximum values (32767 s / 32767 s / 127) must not break anything:
    they once overflowed TCP_USER_TIMEOUT (a C int) and the uncaught error dropped
    every connection. A socket that rejects the options must not either."""
    port = _free_port()
    logs = []
    big = {"keepalive_idle_s": 32767, "keepalive_interval_s": 32767, "keepalive_count": 127}
    runner = MappingRunner(_mapping(port, **big), logger=logs.append)
    await runner.start()
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), f"client dropped: {logs}"
        await asyncio.sleep(0.3)
        assert len(runner._clients) == 1, f"client dropped: {logs}"
        assert not any("keepalive not enabled" in ln for ln in logs), logs
        sock = next(iter(runner._clients)).writer.get_extra_info("socket")
        _assert_keepalive(_keepalive_state(sock), 32767, 32767, 127, "maximum values")
        await _close(w)
    finally:
        await runner.stop()

    class _RejectingSocket:
        def setsockopt(self, *_args):
            raise TypeError("a bytes-like object is required, not 'int'")

    class _Writer:
        def get_extra_info(self, _key):
            return _RejectingSocket()

    err = sockopts.apply_keepalive(_Writer(), _mapping(port, **big).network)
    assert err and "TypeError" in err, err
    print("keepalive at the form's maximums: connection stays up; option errors never raise  OK")


async def test_linux_user_timeout_only_when_tighter():
    """Linux: TCP_USER_TIMEOUT = idle + interval * count, but only when that is shorter
    than the kernel's own ~15 min give-up for unacknowledged data."""
    if not sys.platform.startswith("linux"):
        print("TCP_USER_TIMEOUT policy: Linux only  SKIP")
        return
    for net, expect_ms in ((KA, 3000), ({}, 90_000), ({"keepalive_idle_s": 900}, 0)):
        port = _free_port()
        runner = MappingRunner(_mapping(port, **net), logger=lambda _m: None)
        await runner.start()
        try:
            _r, w = await asyncio.open_connection("127.0.0.1", port)
            assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
            sock = next(iter(runner._clients)).writer.get_extra_info("socket")
            got = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT)
            assert got == expect_ms, f"{net or 'defaults'}: TCP_USER_TIMEOUT={got}, want {expect_ms}"
            await _close(w)
        finally:
            await runner.stop()
    print("Linux TCP_USER_TIMEOUT: set to idle+interval*count only when < 15 min  OK")


async def test_closed_and_reset_clients_are_reaped():
    """The paths that already worked: a FIN or an RST that DOES arrive is handled
    promptly on every platform — the bug is only the silent (half-open) loss."""
    port = _free_port()
    logs = []
    runner = MappingRunner(_mapping(port, max_connections=1), logger=logs.append)
    await runner.start()
    try:
        for how in ("close (FIN)", "abort (RST)"):
            _r, w = await asyncio.open_connection("127.0.0.1", port)
            assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
            if how.startswith("close"):
                w.close()
            else:
                linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
                w.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                w.transport.abort()
            assert await _wait_for(lambda: not runner._clients, 3), f"client not reaped after {how}"
        assert sum("client disconnected" in ln for ln in logs) == 2, logs
    finally:
        await runner.stop()
    print("FIN and RST from the client: reaped immediately (unchanged behaviour)  OK")


async def test_windows_legacy_keepalive_fallback():
    """Windows before 10 1709 (e.g. Server 2016) has no TCP_KEEPIDLE; the
    SIO_KEEPALIVE_VALS path must still turn keepalive on with our timing."""
    if sys.platform != "win32":
        print("legacy Windows SIO_KEEPALIVE_VALS fallback: n/a on this OS  SKIP")
        return
    hidden = {n: getattr(socket, n) for n in ("TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT")
              if hasattr(socket, n)}
    port = _free_port()
    runner = MappingRunner(_mapping(port, **KA), logger=lambda _m: None)
    await runner.start()
    try:
        for name in hidden:
            delattr(socket, name)
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
    finally:
        for name, value in hidden.items():
            setattr(socket, name, value)
    try:
        ts = next(iter(runner._clients)).writer.get_extra_info("socket")
        assert ts.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), "fallback left keepalive off"
        if "TCP_KEEPIDLE" in hidden:  # read the ioctl's idle time back through the new API
            idle = ts.getsockopt(socket.IPPROTO_TCP, hidden["TCP_KEEPIDLE"])
            assert idle == KA["keepalive_idle_s"], f"SIO_KEEPALIVE_VALS idle not applied: {idle}"
        await _close(w)
    finally:
        await runner.stop()
    print("legacy Windows SIO_KEEPALIVE_VALS fallback: keepalive on with our idle time  OK")


async def test_connect_out_socket_keepalive():
    accepted = asyncio.Queue()

    async def on_conn(reader, writer):
        await accepted.put(writer)

    srv = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    sport = srv.sockets[0].getsockname()[1]
    m = MappingConfig.from_dict({
        "name": "dead-peer-out", "enabled": True,
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
                    "remote_port": sport, **KA},
    })
    runner = MappingRunner(m, logger=lambda _m: None)
    await runner.start()
    try:
        remote_end = await asyncio.wait_for(accepted.get(), 5)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "connect-out did not register"
        _assert_keepalive(_keepalive_state(next(iter(runner._clients)).writer.get_extra_info("socket")),
                          1, 1, 2, "connect-out")
        await _close(remote_end)
    finally:
        await runner.stop()
        srv.close()
    print("client (connect-out) mode: outgoing socket has keepalive  OK")


async def test_modbus_gateway_socket_keepalive():
    port = _free_port()
    gw = ModbusGatewayRunner(_mapping(port, protocol="modbus", **KA), logger=lambda _m: None)
    await gw.start()
    try:
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        assert await _wait_for(lambda: len(gw._clients) == 1, 5), "master did not register"
        _assert_keepalive(_keepalive_state(next(iter(gw._clients)).writer.get_extra_info("socket")),
                          1, 1, 2, "modbus gateway")
        await _close(w)
    finally:
        await gw.stop()
    print("modbus gateway: accepted master socket has keepalive  OK")


# --------------------------------------------------------------------------
# 2) end-to-end with a real half-open connection
# --------------------------------------------------------------------------
async def e2e_raw_dead_client_is_reaped(Blackhole):
    port = _free_port()
    logs = []
    runner = MappingRunner(_mapping(port, max_connections=1, **KA), logger=logs.append)
    await runner.start()
    writers = []
    try:
        _r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        writers.append(w1)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
        cport = w1.get_extra_info("sockname")[1]
        t0 = time.monotonic()
        with Blackhole(cport, port):  # the client host "loses power": silence, no FIN/RST
            reaped = await _wait_for(lambda: not runner._clients, _detect_budget())
            elapsed = time.monotonic() - t0
            assert reaped, (f"dead client still registered after {elapsed:.1f}s "
                            f"(status: {runner.status.client_count} client(s)) - never detected")
            assert await _wait_for(lambda: not _server_side_established(port, cport), 2), \
                "server-side socket is still ESTABLISHED in the OS table"
        # the only slot is free again: a new client is accepted, not refused "Busy"
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        writers.append(w2)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "new client not accepted"
        got = await _read_some(r2, 0.5)
        assert b"Busy" not in got, f"new client refused: {got!r}"
        why = next((ln for ln in logs if "client disconnected" in ln), "")
        print(f"raw server: dead client reaped after {elapsed:.1f}s, slot reused  OK\n"
              f"   log: {why.replace('—', '-')}")
    finally:
        for w in writers:
            await _close(w)
        await runner.stop()


async def e2e_raw_dead_client_while_streaming(Blackhole):
    """The device keeps talking while the client is dead: the data stays unacked, so
    Linux sends no keepalive probes — TCP_USER_TIMEOUT bounds it to the same window
    (~15 min of retransmissions otherwise). Windows relies on its own, much shorter
    retransmission limit, hence the more generous budget there."""
    port = _free_port()
    logs = []
    runner = MappingRunner(_mapping(port, max_connections=1, **KA), logger=logs.append)
    await runner.start()
    writers, feeder = [], None
    try:
        _r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        writers.append(w1)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
        conn = next(iter(runner._clients))
        cport = w1.get_extra_info("sockname")[1]

        async def device_streams():  # what the serial read loop does per chunk read
            while conn in runner._clients:
                conn.feed_from_serial(b"x" * 256)
                await asyncio.sleep(0.05)

        budget = _detect_budget() if sys.platform.startswith("linux") else 60.0
        t0 = time.monotonic()
        with Blackhole(cport, port):
            feeder = asyncio.create_task(device_streams())
            reaped = await _wait_for(lambda: not runner._clients, budget)
            elapsed = time.monotonic() - t0
            assert reaped, f"dead client with data in flight still registered after {elapsed:.1f}s"
        why = next((ln for ln in logs if "client disconnected" in ln), "")
        assert "queue full" not in why, f"reaped by queue overflow, not by TCP: {why}"
        print(f"raw server, device streaming: dead client reaped after {elapsed:.1f}s  OK\n"
              f"   log: {why.replace('—', '-')}")
    finally:
        if feeder is not None:
            feeder.cancel()
        for w in writers:
            await _close(w)
        await runner.stop()


async def e2e_legacy_windows_fallback_reaps(Blackhole):
    """Same headline scenario through the pre-1709 SIO_KEEPALIVE_VALS path (the OS
    fixes the probe count at 10, so detection takes idle + 10 * interval)."""
    hidden = {n: getattr(socket, n) for n in ("TCP_KEEPIDLE", "TCP_KEEPINTVL", "TCP_KEEPCNT")
              if hasattr(socket, n)}
    for name in hidden:
        delattr(socket, name)
    try:
        await e2e_raw_dead_client_is_reaped(Blackhole)
    finally:
        for name, value in hidden.items():
            setattr(socket, name, value)


async def e2e_raw_control_without_keepalive(Blackhole):
    """Control = the reported bug: with keepalive OFF the half-open client stays."""
    port = _free_port()
    runner = MappingRunner(_mapping(port, max_connections=1, tcp_keepalive=False),
                           logger=lambda _m: None)
    await runner.start()
    writers = []
    try:
        _r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        writers.append(w1)
        assert await _wait_for(lambda: len(runner._clients) == 1, 5), "client did not register"
        cport = w1.get_extra_info("sockname")[1]
        window = _detect_budget()
        with Blackhole(cport, port):
            await asyncio.sleep(window)
            assert len(runner._clients) == 1, "without keepalive the dead client should linger"
            assert _server_side_established(port, cport), "expected ESTABLISHED (half-open)"
            r2, w2 = await asyncio.open_connection("127.0.0.1", port)
            writers.append(w2)
            got = await _read_some(r2, 2.0)
            assert got.startswith(b"Busy"), f"expected the new client to be refused, got {got!r}"
        print(f"control (keepalive off): after {window:.0f}s the dead client is still "
              f"ESTABLISHED and a new client gets {got.strip()!r} - the reported bug  OK")
    finally:
        for w in writers:
            await _close(w)
        await runner.stop()


async def e2e_connect_out_dead_server_reconnects(Blackhole):
    accepted = asyncio.Queue()

    async def on_conn(reader, writer):
        await accepted.put(writer)

    srv = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    sport = srv.sockets[0].getsockname()[1]
    m = MappingConfig.from_dict({
        "name": "dead-peer-out", "enabled": True,
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
                    "remote_port": sport, **KA},
    })
    runner = MappingRunner(m, logger=lambda _m: None)
    await runner.start()
    ends = []
    try:
        first = await asyncio.wait_for(accepted.get(), 5)
        ends.append(first)
        bridge_port = first.get_extra_info("peername")[1]
        t0 = time.monotonic()
        budget = _detect_budget() + 2  # + the supervisor's reconnect backoff
        with Blackhole(sport, bridge_port):  # the remote server goes silent
            try:
                second = await asyncio.wait_for(accepted.get(), budget)
            except asyncio.TimeoutError:
                raise AssertionError(f"dead remote never detected: no reconnect within "
                                     f"{budget:.0f}s") from None
            ends.append(second)
        print(f"client (connect-out): dead remote detected, reconnected after "
              f"{time.monotonic() - t0:.1f}s  OK")
    finally:
        for w in ends:
            await _close(w)
        await runner.stop()
        srv.close()


async def e2e_connect_timeout_on_silent_host(Blackhole):
    """Connect-out to a host that swallows SYNs (down / firewalled-drop): the attempt
    is abandoned after connect_timeout_s and retried, instead of the OS's ~21 s
    (Windows) / ~2 min (Linux) SYN timeout."""
    port = _free_port()  # nothing listens; dropping the SYN means no RST either
    logs = []
    m = MappingConfig.from_dict({
        "name": "silent-host", "enabled": True,
        "serial": {"port": "/dev/does-not-exist", "baudrate": 115200},
        "network": {"mode": "client", "protocol": "raw", "remote_host": "127.0.0.1",
                    "remote_port": port, "connect_timeout_s": 1},
    })
    with Blackhole(None, port):
        runner = MappingRunner(m, logger=logs.append)
        await runner.start()
        try:
            failures = lambda: [ln for ln in logs if "connect-out failed" in ln]  # noqa: E731
            assert await _wait_for(lambda: len(failures()) >= 2, 6), \
                f"no timed-out connect attempts: {logs}"
            assert all("timed out after 1s" in ln for ln in failures()), failures()
        finally:
            await runner.stop()
    print("client (connect-out): SYN to a silent host abandoned after 1s and retried  OK")


async def e2e_modbus_dead_master_is_reaped(Blackhole):
    port = _free_port()
    gw = ModbusGatewayRunner(_mapping(port, protocol="modbus", max_connections=1, **KA),
                             logger=lambda _m: None)
    await gw.start()
    writers = []
    try:
        _r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        writers.append(w1)
        assert await _wait_for(lambda: len(gw._clients) == 1, 5), "master did not register"
        cport = w1.get_extra_info("sockname")[1]
        t0 = time.monotonic()
        with Blackhole(cport, port):
            assert await _wait_for(lambda: not gw._clients, _detect_budget()), \
                "dead Modbus master still holds the gateway"
            elapsed = time.monotonic() - t0
        _r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        writers.append(w2)
        assert await _wait_for(lambda: len(gw._clients) == 1, 5), "new master not accepted"
        print(f"modbus gateway: dead master reaped after {elapsed:.1f}s, new master accepted  OK")
    finally:
        for w in writers:
            await _close(w)
        await gw.stop()


# --------------------------------------------------------------------------
def _loops():
    """(label, factory) event loops to exercise: production's loop first."""
    if sys.platform == "win32":
        return [("selector", asyncio.SelectorEventLoop), ("proactor", asyncio.ProactorEventLoop)]
    return [("default", asyncio.new_event_loop)]


def _run(fn, *args, loop_factory=None) -> bool:
    loop = (loop_factory or _loops()[0][1])()
    try:
        loop.run_until_complete(fn(*args))
        return True
    except AssertionError as e:
        print(f"FAIL {fn.__name__}: {e}")
        return False
    except Exception as e:  # noqa: BLE001 - report and keep going
        print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
        return False
    finally:
        # like asyncio.run(): let kicked clients' handler tasks finish, then close
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        with contextlib.suppress(Exception):
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def main() -> int:
    # a native crash (packet-filter driver) must still leave the log and a traceback
    sys.stdout.reconfigure(line_buffering=True)
    faulthandler.enable()
    results = []
    for label, factory in _loops():
        if len(_loops()) > 1:
            print(f"-- {label} event loop --")
        results.append(_run(test_server_socket_keepalive_default, loop_factory=factory))
    for fn in (test_server_socket_keepalive_custom_and_off, test_extreme_keepalive_values_are_safe,
               test_linux_user_timeout_only_when_tighter, test_closed_and_reset_clients_are_reaped,
               test_windows_legacy_keepalive_fallback, test_connect_out_socket_keepalive,
               test_modbus_gateway_socket_keepalive):
        results.append(_run(fn))

    if not OPT_IN:
        print("\nSKIP end-to-end half-open tests (they edit the firewall): set "
              "SER2NET_BLACKHOLE_TESTS=1 (Linux: root/sudo + iptables; "
              "Windows: Administrator + pip install pydivert)")
    else:
        backend, why = _blackhole_backend()
        if backend is None:
            print(f"FAIL end-to-end half-open tests requested but unavailable: {why}")
            results.append(False)
        else:
            print(f"\n-- end-to-end half-open connections ({backend.__name__}) --")
            for fn in (e2e_raw_dead_client_is_reaped, e2e_raw_control_without_keepalive,
                       e2e_raw_dead_client_while_streaming, e2e_connect_out_dead_server_reconnects,
                       e2e_connect_timeout_on_silent_host, e2e_modbus_dead_master_is_reaped):
                results.append(_run(fn, backend))
            if sys.platform == "win32":
                print("-- legacy (pre-1709) Windows keepalive path --")
                results.append(_run(e2e_legacy_windows_fallback_reaps, backend))
                print("-- proactor event loop --")
                results.append(_run(e2e_raw_dead_client_is_reaped, backend,
                                    loop_factory=asyncio.ProactorEventLoop))

    failed = results.count(False)
    if failed:
        print(f"\nFAILED: {failed} of {len(results)} dead-peer checks")
        return 1
    print("\nPASS: dead-peer detection (TCP keepalive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
