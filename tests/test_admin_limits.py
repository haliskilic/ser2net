"""The admin UI listener closes connections that send no request, and caps them
(app/web/httpguard.py).

uvicorn's h11 server kept a connection for as long as the client did while no complete
request arrived (none at all, half a header, or half a header after a first request on a
kept-alive connection): anyone who could reach the port could hold any number of idle
sockets, unauthenticated, until the process ran out of descriptors (on Windows, until the
event loop stopped). Now, on a real server:
  - a connection must send a complete request head within admin_ui.request_timeout_s of
    connecting and of the end of each response, or it is closed; a slow but complete head
    is served; an upgraded WebSocket (the console) is not timed;
  - at most admin_ui.max_connections_per_ip from one address and admin_ui.max_connections
    in all; past that a connection is closed at once and the refusal is logged (at most
    once a minute), while other addresses keep working; 600 idle sockets from one address
    leave the UI answering, and are all closed in time;
  - HTTPS: the TLS handshake is done after admission; a connection that completes it but
    sends no request is closed in time too;
  - limits that are not usable in config.json are put back to their defaults on load.
Binds 127.0.0.1 (and 127.0.0.2-4 for the other-address checks where the OS allows it).

Run: python3 tests/test_admin_limits.py
"""
import contextlib
import os
import socket
import ssl
import tempfile
import threading
import time

from _server import Browser, Server, closed_by_server, silent_tls_client

from app.config import AppConfig
from app.web.auth import hash_password

PW = "admin-limits-pass-1"


def config(**admin) -> dict:
    return {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                       "pwd_version": 1}],
            "admin_ui": dict({"request_timeout_s": 2}, **admin),
            "mappings": [{"id": "m1", "name": "m1", "enabled": False,
                          "serial": {"port": "/dev/ser2net-none"}}]}


def connect(port: int, source: str = "127.0.0.1", timeout: float = 10) -> socket.socket:
    s = socket.socket()
    s.settimeout(timeout)
    s.bind((source, 0))
    s.connect(("127.0.0.1", port))
    return s


def can_bind(ip: str) -> bool:
    with contextlib.suppress(OSError), socket.socket() as s:
        s.bind((ip, 0))
        return True
    return False


def closed_within(s: socket.socket, seconds: float) -> float:
    """Seconds until the server closed ``s`` (sending nothing); raises if it didn't."""
    t0 = time.monotonic()
    s.settimeout(seconds)
    try:
        data = s.recv(100)
    except (ConnectionError, OSError) as e:
        if isinstance(e, socket.timeout):
            raise AssertionError(f"still open after {seconds} s") from None
        data = b""
    assert data == b"", f"the server answered {data!r} instead of closing"
    return time.monotonic() - t0


def get(s: socket.socket, path: str = "/healthz") -> bytes:
    """One request on ``s``; returns the response body (reads exactly its Content-Length)."""
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        assert chunk, "closed before a response"
        buf += chunk
    head, body = buf.split(b"\r\n\r\n", 1)
    length = next(int(ln.split(b":")[1]) for ln in head.split(b"\r\n")
                  if ln.lower().startswith(b"content-length:"))
    while len(body) < length:
        body += s.recv(4096)
    assert head.startswith(b"HTTP/1.1 200"), head
    return body


def still_open(s: socket.socket) -> bool:
    s.settimeout(0.05)
    try:
        return s.recv(1) != b""
    except socket.timeout:
        return True
    except OSError:
        return False


def settle() -> None:
    """Let the server see Server.start()'s port probe close (it counts until then)."""
    time.sleep(0.5)


def healthz(port: int, source: str = "127.0.0.1") -> bool:
    try:
        with connect(port, source, timeout=5) as s:
            return get(s) == b"ok"
    except (OSError, AssertionError):
        return False


def test_request_deadline():
    with Server(config()) as srv:
        port = srv.port
        idle = connect(port)
        assert 1.5 < closed_within(idle, 6) < 4, "a socket that sends nothing must be closed"
        partial = connect(port)
        partial.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n")  # never finished
        assert closed_within(partial, 6) < 4, "half a request head must not keep a socket"
        kept = connect(port)
        assert get(kept) == b"ok"
        time.sleep(1)
        assert get(kept) == b"ok", "a kept-alive connection is served again within the time"
        kept.sendall(b"GET /healthz HTTP/1.1\r\n")  # half a head on a kept-alive connection
        assert closed_within(kept, 6) < 4, "half a head after a response must be closed too"
        slow = connect(port)
        for part in (b"GET /hea", b"lthz HTTP/1.1\r\n", b"Host: x\r\n", b"\r\n"):
            slow.sendall(part)
            time.sleep(0.3)
        buf = slow.recv(4096)
        assert buf.startswith(b"HTTP/1.1 200"), buf
        for s in (idle, partial, kept, slow):
            s.close()
    print("admin UI: idle and half-sent request heads closed after request_timeout_s (also "
          "after a response); a slow but complete head is served  OK")


def test_websocket_is_not_timed():
    import asyncio

    import websockets

    with Server(config()) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        cookie = "; ".join(f"{c.name}={c.value}" for c in b.jar)

        async def console():
            url = f"ws://127.0.0.1:{srv.port}/api/mappings/m1/console"
            async with websockets.connect(url, additional_headers={"Cookie": cookie},
                                          open_timeout=10) as ws:
                await asyncio.sleep(3.5)  # past request_timeout_s
                await ws.ping()
                assert ws.state.name == "OPEN", ws.state

        asyncio.run(console())
    print("admin UI: an upgraded WebSocket (the console) outlives request_timeout_s  OK")


def test_caps():
    others = [ip for ip in ("127.0.0.2", "127.0.0.3", "127.0.0.4") if can_bind(ip)]
    with Server(config(max_connections=20, max_connections_per_ip=8,
                       request_timeout_s=4)) as srv:
        port = srv.port
        settle()
        held = [connect(port) for _ in range(8)]
        time.sleep(0.3)
        assert all(still_open(s) for s in held), "8 from one address are within the limit"
        extra = connect(port)
        assert closed_within(extra, 2) < 1, "the 9th connection from one address must be refused"
        assert "admin UI: refused 127.0.0.1:" in srv.output() and \
            "too many connections to the admin UI (8 at once from one address is the limit)" \
            in srv.output(), srv.output()[-1500:]
        if len(others) >= 2:
            assert healthz(port, others[0]), "another address must still be served"
            held += [connect(port, ip) for ip in others[:2] for _ in range(6)]  # 8 + 12 = 20
            time.sleep(0.3)
            # (its log line is held back: one refusal line a minute)
            assert not healthz(port, others[-1]), "past max_connections nobody gets in"
        else:
            print("  (no 127.0.0.2/3: the other-address checks are skipped)")
        for s in held:  # all closed in time, after which the UI answers everyone again
            closed_within(s, 8)
            s.close()
        assert healthz(port), "once the idle connections are gone, the address gets in again"
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, metrics = b.get("/metrics")
        refused = [ln for ln in metrics.splitlines()
                   if ln.startswith('ser2net_connections_refused_total{reason="admin_ui"}')]
        assert refused and int(refused[0].split()[-1]) >= (2 if len(others) >= 2 else 1), metrics
        assert any(ln.startswith("ser2net_admin_connections ") for ln in metrics.splitlines())
    print("admin UI: per-address and total caps refuse at once (logged, counted in "
          "/metrics), other addresses served, everyone again once the idle ones close  OK")


def test_many_idle_sockets():
    other = next((ip for ip in ("127.0.0.2",) if can_bind(ip)), None)
    with Server(config(request_timeout_s=5)) as srv:
        port = srv.port
        settle()
        held = []
        for _ in range(600):
            try:
                held.append(connect(port, timeout=10))
            except OSError:
                break
        time.sleep(0.5)
        assert srv.proc.poll() is None, "the server must keep running"
        if other:
            assert healthz(port, other), "the UI must answer another address meanwhile"
        refused = sum(not still_open(s) for s in held)
        assert refused >= len(held) - 64, f"only {refused} of {len(held)} refused"
        deadline = time.monotonic() + 10
        for s in held:
            with contextlib.suppress(OSError):
                s.settimeout(max(0.1, deadline - time.monotonic()))
                assert s.recv(1) == b""
            s.close()
        assert healthz(port), "after the idle sockets are closed, the UI answers again"
    print(f"admin UI: {len(held)} idle sockets from one address: the UI keeps answering, "
          "all of them closed in time  OK")


def test_https():
    from app.web.selfsigned import CertError, generate

    d = tempfile.mkdtemp(prefix="s2n_admtls_")
    cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    try:
        generate(cert, key)
    except CertError as e:
        print(f"SKIP HTTPS checks: no certificate ({e})")
        return
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with Server(config(tls_cert=cert, tls_key=key)) as srv:
        with ctx.wrap_socket(connect(srv.port)) as s:
            assert get(s) == b"ok"
            assert s.version() is not None
        # real clients at once, each sending its ClientHello right after connect
        barrier = threading.Barrier(12)
        answers: list = []

        def client() -> None:
            try:
                barrier.wait(5)
                with ctx.wrap_socket(connect(srv.port, timeout=5)) as s:
                    answers.append(get(s))
            except (OSError, AssertionError, threading.BrokenBarrierError) as e:
                answers.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=client) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert answers == [b"ok"] * 12, f"HTTPS clients at once: {answers}"
        with ctx.wrap_socket(connect(srv.port)) as s:  # handshake done, then nothing
            assert closed_within(s, 6) < 4, "an idle TLS connection must be closed in time"
        # ... and its socket really goes, also when the client never answers the close
        with silent_tls_client(srv.port, ctx) as raw:
            took = closed_by_server(raw, 6)
            assert took < 4, f"a silent TLS client's socket stayed open {took:.1f} s"
        with connect(srv.port) as plain:  # plain HTTP on the TLS port gets no answer
            plain.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            plain.settimeout(5)
            with contextlib.suppress(OSError):
                assert not plain.recv(100).startswith(b"HTTP/1.1 200")
    print("admin UI over HTTPS: TLS after admission, 12 clients at once served; an idle TLS "
          "connection is closed in time  OK")


def test_limits_normalized_on_load():
    cfg = AppConfig.from_dict({"admin_ui": {"max_connections": 3, "max_connections_per_ip": -1,
                                            "request_timeout_s": "soon"}})
    a = cfg.admin_ui
    assert (a.max_connections, a.max_connections_per_ip, a.request_timeout_s) == (16, 64, 10)
    assert len(cfg.load_notes) == 3 and all("admin_ui." in n for n in cfg.load_notes)
    cfg.validate()
    off = AppConfig.from_dict({"admin_ui": {"max_connections": 0, "max_connections_per_ip": 0,
                                            "request_timeout_s": 0}}).admin_ui
    assert (off.max_connections, off.max_connections_per_ip, off.request_timeout_s) == (0, 0, 0)
    default = AppConfig.from_dict({}).admin_ui
    assert (default.max_connections, default.max_connections_per_ip,
            default.request_timeout_s) == (256, 64, 10)
    print("admin_ui limits: defaults 256 / 64 / 10 s, 0 = off, unusable values normalized "
          "on load with a note  OK")


if __name__ == "__main__":
    test_limits_normalized_on_load()
    test_request_deadline()
    test_websocket_is_not_timed()
    test_caps()
    test_many_idle_sockets()
    test_https()
    print("\nPASS: admin UI request deadline and connection caps")
