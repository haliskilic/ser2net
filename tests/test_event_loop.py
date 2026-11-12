"""The event loop ser2net runs on, and Windows' 512-socket select() limit (ser2netweb/runtime.py,
ser2netweb/engine/netguard.py).

On Windows ser2net runs a SelectorEventLoop, whose select() takes at most 512 sockets in a
set; the 513th used to make select() raise inside the loop, which stopped the process:
every bridge, Modbus poll and MQTT publisher (and as a service, again after each restart).
Now the loop's selector (CappedSelectSelector) refuses a registration past the limit with
an OSError for whoever asked, and incoming connections are refused, with a log line, while
it watches all but SELECT_HEADROOM of them. SER2NET_WINDOWS_LOOP=proactor runs the
ProactorEventLoop instead (no such limit). A fatal error reaches all.log, not only stderr.

Portable: the decision (event_loop_choice / loop_factory), the capped selector, and the
budget on a select() loop with a small limit (select() exists everywhere). Windows only
(skipped elsewhere): a real server with its admin limits off takes 600 idle sockets and
keeps answering; the proactor loop boots.

Run: python3 tests/test_event_loop.py
"""
import asyncio
import contextlib
import errno
import os
import selectors
import socket
import subprocess
import sys
import tempfile
import time

from _server import Server

from ser2netweb import runtime
from ser2netweb.engine import netguard
from ser2netweb.web.auth import hash_password

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PW = "event-loop-pass-1"


def test_choice():
    assert runtime.event_loop_choice("linux", {}) == ("default", "")
    assert runtime.event_loop_choice("darwin", {"SER2NET_WINDOWS_LOOP": "proactor"})[0] == "default"
    choice, note = runtime.event_loop_choice("win32", {})
    assert choice == "selector" and "at most 512 sockets" in note and "proactor" in note, note
    assert runtime.event_loop_choice("win32", {"SER2NET_WINDOWS_LOOP": "selector"})[0] == "selector"
    choice, note = runtime.event_loop_choice("win32", {"SER2NET_WINDOWS_LOOP": " Proactor "})
    assert choice == "proactor" and "no 512-socket limit" in note, note
    choice, note = runtime.event_loop_choice("win32", {"SER2NET_WINDOWS_LOOP": "fast"})
    assert choice == "selector" and note.startswith("SER2NET_WINDOWS_LOOP='fast' is neither")
    assert runtime.loop_factory("default") is None
    if sys.platform != "win32":
        assert runtime.loop_factory("proactor") is None
    print("event_loop_choice: Windows selector unless SER2NET_WINDOWS_LOOP=proactor; "
          "elsewhere the default loop  OK")


def test_worker_threads():
    import threading

    async def go():
        runtime.use_worker_threads(asyncio.get_running_loop())
        names = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert names.startswith("ser2net"), names
        gate, ready = threading.Barrier(runtime.WORKER_THREADS, timeout=10), []

        def wait_for_all():  # every thread at once, whatever the number of CPUs
            gate.wait()
            ready.append(1)

        await asyncio.gather(*(asyncio.to_thread(wait_for_all)
                               for _ in range(runtime.WORKER_THREADS)))
        assert len(ready) == runtime.WORKER_THREADS

    asyncio.run(go())
    print(f"default thread pool: {runtime.WORKER_THREADS} threads ('ser2net-*'), "
          f"not min(32, CPUs + 4) = {min(32, (os.cpu_count() or 1) + 4)}  OK")


def test_capped_selector():
    sel = netguard.CappedSelectSelector(limit=4)
    pairs = [socket.socketpair() for _ in range(6)]
    try:
        for a, _b in pairs[:4]:
            sel.register(a, selectors.EVENT_READ)
        assert sel.watched() == 4
        try:
            sel.register(pairs[4][0], selectors.EVENT_READ)
        except OSError as e:
            assert e.errno == errno.EMFILE and "512" not in str(e) and "4 sockets" in str(e), e
        else:
            raise AssertionError("a 5th reader must be refused")
        sel.register(pairs[4][0], selectors.EVENT_WRITE)  # the write set is separate
        for a, _b in pairs[:3]:
            sel.modify(a, selectors.EVENT_READ | selectors.EVENT_WRITE)
        try:  # the write set is full now: the socket keeps what it had
            sel.modify(pairs[3][0], selectors.EVENT_READ | selectors.EVENT_WRITE)
        except OSError:
            pass
        else:
            raise AssertionError("a 5th writer must be refused")
        assert sel.get_map()[pairs[3][0]].events == selectors.EVENT_READ, \
            "a refused modify() must leave the registration as it was"
        sel.unregister(pairs[0][0])
        sel.register(pairs[5][0], selectors.EVENT_READ)  # room again
        assert sel.watched() == 4
        assert netguard.CappedSelectSelector().limit == 512
    finally:
        sel.close()
        for a, b in pairs:
            a.close()
            b.close()
    print("CappedSelectSelector: refuses the reader / writer past its limit (OSError), "
          "a refused modify keeps the old registration  OK")


async def _hold(reader, writer):
    with contextlib.suppress(Exception):
        await reader.read()
    writer.close()


def _open_client(port: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(0.3)
    return s


def _is_open(s: socket.socket) -> bool:
    try:
        return s.recv(1) != b""
    except socket.timeout:
        return True
    except OSError:
        return False


async def _budget_on_a_select_loop():
    loop = asyncio.get_running_loop()
    sel = netguard.BUDGET.selector
    assert isinstance(sel, netguard.CappedSelectSelector), sel
    # the loop watches its self-pipe and the listener; room for 4 connections, then none
    sel.limit = netguard.SELECT_HEADROOM + 6
    lines = []
    listener = await netguard.start_server(_hold, "127.0.0.1", 0, log=lines.append)
    port = listener.sockets[0].getsockname()[1]
    clients = []
    try:
        before = netguard.BUDGET.refused.get("event_loop", 0)
        for _ in range(8):
            clients.append(await asyncio.to_thread(_open_client, port))
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        state = await asyncio.to_thread(lambda: [_is_open(c) for c in clients])
        assert state.count(True) == 4, state
        assert netguard.BUDGET.refused.get("event_loop", 0) >= before + 4
        assert any("the event loop watches 6 sockets, near the 70 its select() takes" in ln
                   for ln in lines), lines
        # past the limit itself, a registration fails with an OSError; the loop goes on
        extra = [socket.socketpair() for _ in range(sel.limit)]
        refused = 0
        for a, _b in extra:
            try:
                loop.add_reader(a.fileno(), lambda: None)
            except OSError:
                refused += 1
        assert refused > 0, "registrations past the limit must fail"
        await asyncio.sleep(0.05)  # the loop still runs
        for a, b in extra:
            loop.remove_reader(a.fileno())
            a.close()
            b.close()
        for c in clients:
            c.close()
        await asyncio.sleep(0.3)
        again = await asyncio.to_thread(_open_client, port)
        await asyncio.sleep(0.2)
        assert await asyncio.to_thread(_is_open, again), "with room again, a client gets in"
        again.close()
        await asyncio.sleep(0.2)  # its handler sees the close
    finally:
        listener.close()
        for c in clients:
            c.close()
        await asyncio.wait_for(listener.wait_closed(), 5)


def test_budget_on_a_select_loop():
    factory = runtime.loop_factory("selector")
    loop = factory()
    try:
        loop.run_until_complete(_budget_on_a_select_loop())
    finally:
        netguard.BUDGET.selector = None
        loop.close()
    print("select() loop: incoming connections refused near the limit (logged, counted), "
          "a registration past it fails without stopping the loop  OK")


def test_fatal_error_reaches_the_log():
    data = tempfile.mkdtemp(prefix="s2n_fatal_")
    code = (
        "import logging, sys\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        "from ser2netweb import runtime\n"
        "logging.getLogger('ser2net').addHandler(logging.StreamHandler(sys.stdout))\n"
        "async def broken(*a, **k):\n"
        "    raise RuntimeError('selector exploded')\n"
        "runtime._serve = broken\n"
        f"runtime.main({os.path.join(data, 'config.json')!r})\n")
    env = dict(os.environ, SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT="1")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=60)
    assert proc.returncode != 0, "a fatal error must still end the process"
    assert "fatal error, ser2net stops" in proc.stdout and "selector exploded" in proc.stdout, \
        (proc.stdout, proc.stderr)
    print("a fatal error is written to ser2net's log (all.log) before the process ends  OK")


def _config(**admin) -> dict:
    return {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                       "pwd_version": 1}], "admin_ui": admin, "mappings": []}


def _healthz(port: int) -> bool:
    import urllib.request
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read() == b"ok"
    except OSError:
        return False


def _wait_for(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            return False
        time.sleep(0.2)
    return True


def test_windows_600_idle_sockets():
    # the admin UI's own limits off: the event loop's socket limit is what is left. The
    # admitted sockets must all still be open when the loop gets near its limit, however
    # slowly a busy runner connects and accepts them, so the request timeout is far longer
    # than that takes; they are closed here once the refusal is seen.
    marker = "the event loop watches"
    with Server(_config(max_connections=0, max_connections_per_ip=0,
                        request_timeout_s=300)) as srv:
        time.sleep(0.5)
        held, failed = [], 0
        end = time.monotonic() + 120
        while len(held) < 600 and time.monotonic() < end:
            try:
                held.append(socket.create_connection(("127.0.0.1", srv.port), timeout=30))
            except OSError:  # the listen queue is full: let the server accept what is in it
                failed += 1
                time.sleep(0.2)
        # the server accepts the queued ones at its own pace: wait for its refusal line
        _wait_for(lambda: marker in srv.output() or srv.proc.poll() is not None, 60)
        assert srv.proc.poll() is None, "the server stopped:\n" + srv.output()[-2000:]
        assert marker in srv.output(), (f"{len(held)} sockets open ({failed} connects failed)\n"
                                        + srv.output()[-2000:])
        for s in held:
            s.close()
        assert _wait_for(lambda: _healthz(srv.port), 60), \
            "the UI must answer again:\n" + srv.output()[-2000:]
        assert "event loop: selector" in srv.output()
    print(f"Windows: {len(held)} idle sockets; the event loop refuses near 512 and keeps "
          "running, the UI answers again  OK")


def test_windows_proactor_boots():
    with Server(_config(), env={"SER2NET_WINDOWS_LOOP": "proactor"}) as srv:
        assert _healthz(srv.port)
        assert "event loop: proactor" in srv.output(), srv.output()[-2000:]
    print("Windows: SER2NET_WINDOWS_LOOP=proactor boots on the proactor loop  OK")


if __name__ == "__main__":
    test_choice()
    test_worker_threads()
    test_capped_selector()
    test_budget_on_a_select_loop()
    test_fatal_error_reaches_the_log()
    if sys.platform == "win32":
        test_windows_600_idle_sockets()
        test_windows_proactor_boots()
    else:
        print("SKIP the real-server checks of the Windows loops (Windows CI runs them)")
    print("\nPASS: event loop choice and the select() socket limit")
