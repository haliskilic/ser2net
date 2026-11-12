"""Liveness and readiness: the event loop watchdog and systemd's notifications.

Checks:
  * a process whose event loop is blocked longer than the limit writes every thread's
    stack to hang.log (naming the blocking call), a line to all.log, and exits 70;
  * a process that was frozen as a whole (sleep, docker pause, a suspended VM) is not
    ended for it: the watchdog measures anew; without heartbeats it still fires later;
  * SER2NET_LOOP_WATCHDOG: seconds, 0 = off, anything else: the default and a warning;
  * the next start keeps the previous hang.log as hang.log.1 and says so in all.log;
  * sd_notify does nothing without NOTIFY_SOCKET; with it a real server sends READY=1
    (with a STATUS) once its UI listens, WATCHDOG=1 pings under WatchdogSec=, STOPPING=1
    when it stops; /metrics has the event loop's lag;
  * runtime.when_ready hooks (the Windows service's RUNNING) are called once the server
    listens.

Run: python3 tests/test_liveness.py
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from _server import ROOT, Browser, Server, free_port, wait_port

from app import runtime, sdnotify, watchdog
from app.web.auth import hash_password

BLOCKING_SCRIPT = r'''
import asyncio, sys, time
sys.path.insert(0, sys.argv[1])
from app import watchdog
watchdog.CHECK_S = 0.2

async def main():
    dog = watchdog.LoopWatchdog(sys.argv[2], print, 1.5)
    dog.start()
    await asyncio.sleep(0.5)
    print("blocking the loop", flush=True)
    time.sleep(60)  # a call that never returns in time
    print("not reached", flush=True)

asyncio.run(main())
'''


def test_blocked_loop_exits():
    d = tempfile.mkdtemp(prefix="s2n_hang_")
    try:
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-c", BLOCKING_SCRIPT, ROOT, d],
                           capture_output=True, text=True, timeout=40)
        took = time.monotonic() - t0
        assert p.returncode == watchdog.EXIT_CODE, (p.returncode, p.stdout, p.stderr)
        assert took < 15, f"exited after {took:.1f} s"
        assert "not reached" not in p.stdout
        with open(os.path.join(d, "hang.log"), encoding="utf-8") as fh:
            hang = fh.read()
        assert "the event loop did not run for" in hang.splitlines()[0], hang[:200]
        assert "Thread" in hang and "<string>" in hang, hang  # the blocked main thread's stack
        with open(os.path.join(d, "all.log"), encoding="utf-8") as fh:
            assert "ser2net exits (70) to be started again" in fh.read()
        assert "the event loop did not run for" in p.stderr
        print(f"blocked event loop: hang.log with the stacks, all.log line, exit 70 after "
              f"{took:.1f} s  OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


class FakeTime:
    """time for LoopWatchdog._watch: sleep() moves a clock on (by `frozen` once)."""

    def __init__(self):
        self.now = 1000.0
        self.freeze_at = None

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s
        if self.freeze_at is not None and self.now >= self.freeze_at:
            self.now += 600  # the process was frozen for 10 minutes
            self.freeze_at = None

    def __getattr__(self, name):
        return getattr(time, name)


def test_frozen_process_is_not_a_hang():
    d = tempfile.mkdtemp(prefix="s2n_frozen_")
    fake, real = FakeTime(), watchdog.time
    exits = []
    try:
        watchdog.time = fake
        dog = watchdog.LoopWatchdog(d, lambda m: None, 30, exit_process=exits.append)
        dog.beat = fake.now
        fake.freeze_at = fake.now + 10  # frozen 10 s in, the loop had beaten at the start
        start = fake.now
        dog._watch()  # returns once it fired
        # not at the freeze (it measured anew from it), but 30 s without a beat after it
        assert exits == [watchdog.EXIT_CODE], exits
        assert fake.now - start > 600 + 30, fake.now - start
        with open(os.path.join(d, "hang.log"), encoding="utf-8") as fh:
            assert "did not run for 3" in fh.readline()  # (~31 s: measured after the freeze)
        print("frozen process (sleep / pause): measured anew, fired only 30 s after it  OK")
    finally:
        watchdog.time = real
        shutil.rmtree(d, ignore_errors=True)


def test_settings_and_previous_hang():
    assert watchdog.limit_from_env({}) == (watchdog.DEFAULT_LIMIT_S, "")
    assert watchdog.limit_from_env({watchdog.ENV: "0"}) == (0.0, "")
    assert watchdog.limit_from_env({watchdog.ENV: " 120 "}) == (120.0, "")
    for bad in ("soon", "-5", "nan", "1e9"):
        limit, note = watchdog.limit_from_env({watchdog.ENV: bad})
        assert limit == watchdog.DEFAULT_LIMIT_S and note.startswith("WARNING:"), (bad, note)
    d = tempfile.mkdtemp(prefix="s2n_prevhang_")
    try:
        assert watchdog.previous_hang(d) == ""
        with open(os.path.join(d, "hang.log"), "w") as fh:
            fh.write("2026-11-12 10:00:00 the event loop did not run for 91 s\n\nstacks\n")
        note = watchdog.previous_hang(d)
        assert "did not run for 91 s" in note and "hang.log.1" in note, note
        assert not os.path.exists(os.path.join(d, "hang.log"))
        assert os.path.exists(os.path.join(d, "hang.log.1"))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("SER2NET_LOOP_WATCHDOG parsed (0 = off, bad -> default + warning); previous "
          "hang.log kept as .1  OK")


def _listener(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(0.2)
    return s


def _drain(s, until, seconds):
    got = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            got.append(s.recv(4096).decode())
        except socket.timeout:
            continue
        if until(got):
            break
    return got


def test_sd_notify():
    assert sdnotify.notify("READY=1", env={}) is False
    assert sdnotify.watchdog_interval({}) is None
    if not hasattr(socket, "AF_UNIX"):
        print("SKIP: sd_notify over AF_UNIX (not on this platform)")
        return
    d = tempfile.mkdtemp(prefix="s2n_notify_")
    path = os.path.join(d, "notify")
    s = _listener(path)
    try:
        env = {"NOTIFY_SOCKET": path}
        assert sdnotify.ready("2 of 2 mappings running", env=env) is True
        assert s.recv(4096) == b"READY=1\nSTATUS=2 of 2 mappings running"
        assert sdnotify.stopping(env=env) and s.recv(4096) == b"STOPPING=1"
        assert sdnotify.watchdog_interval({**env, "WATCHDOG_USEC": "4000000"}) == 2.0
        assert sdnotify.watchdog_interval({**env, "WATCHDOG_USEC": "4000000",
                                           "WATCHDOG_PID": "0"}) is None  # another process
        assert sdnotify.watchdog_interval({**env, "WATCHDOG_USEC": "4000000",
                                           "WATCHDOG_PID": str(os.getpid())}) == 2.0
        assert sdnotify.watchdog_interval({**env, "WATCHDOG_USEC": "x"}) is None
        assert sdnotify.notify("READY=1", env={"NOTIFY_SOCKET": path + ".gone"}) is False
        # nobody reads the socket: its queue fills, and a send must not wait for room (it
        # runs on the event loop). How many datagrams fit depends on the machine: Linux
        # counts them (net.unix.max_dgram_qlen: 10 in a new network namespace, 512 where
        # systemd sets it, as on CI runners), BSD and macOS count the receiver's buffer
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        bound = 5000
        try:
            with open("/proc/sys/net/unix/max_dgram_qlen") as fh:
                bound = int(fh.read()) + 100
        except (OSError, ValueError):
            pass
        t0 = time.monotonic()
        sent = [sdnotify.notify("STATUS=x" * 100, env=env)]
        while sent[-1] and len(sent) < bound:  # until one is refused
            sent.append(sdnotify.notify("STATUS=x" * 100, env=env))
        assert time.monotonic() - t0 < 5 and sent[0] and not sent[-1], (len(sent), bound)
        while True:  # (what queued)
            try:
                s.recv(4096)
            except OSError:  # timed out: empty
                break
        if sys.platform.startswith("linux"):
            name = f"s2n-test-{os.getpid()}"
            a = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            a.bind("\0" + name)
            try:
                assert sdnotify.watchdog(env={"NOTIFY_SOCKET": "@" + name})
                assert a.recv(4096) == b"WATCHDOG=1"
            finally:
                a.close()
        print("sd_notify: nothing without NOTIFY_SOCKET; READY/STATUS, STOPPING, WATCHDOG "
              "datagrams (path and abstract socket); WatchdogSec read; a full socket never "
              "waits  OK")
    finally:
        s.close()
        shutil.rmtree(d, ignore_errors=True)


def test_server_notifies():
    if not hasattr(socket, "AF_UNIX"):
        print("SKIP: server notifications (no AF_UNIX)")
        return
    d = tempfile.mkdtemp(prefix="s2n_srvnotify_")
    path = os.path.join(d, "notify")
    s = _listener(path)
    users = [{"username": "admin", "password_hash": hash_password("live-pass-1"),
              "role": "admin", "pwd_version": 1}]
    srv = Server({"users": users}, env={"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "1000000",
                                       watchdog.ENV: "45"})
    os.makedirs(srv.data, exist_ok=True)
    with open(os.path.join(srv.data, "hang.log"), "w") as fh:
        fh.write("2026-11-12 10:00:00 the event loop did not run for 91 s\n")
    try:
        srv.start()
        got = _drain(s, lambda g: any(m.startswith("READY=1") for m in g), 20)
        ready = next(m for m in got if m.startswith("READY=1"))
        assert "STATUS=0 of 0 mapping(s) running, admin UI on 127.0.0.1:" in ready, ready
        pings = _drain(s, lambda g: g.count("WATCHDOG=1") >= 2, 10)
        assert pings.count("WATCHDOG=1") >= 2, pings
        b = Browser(srv.base)
        b.login("admin", "live-pass-1")
        _st, _h, body = b.get("/metrics")
        assert "\nser2net_event_loop_lag_seconds " in body and \
            "# TYPE ser2net_event_loop_stalls_total counter" in body, body[:500]
        with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
            log = fh.read()
        assert "event loop watchdog: on" in log and "for 45 s" in log, log[-2000:]
        assert "the previous run was ended by the event loop watchdog" in log
        assert os.path.exists(os.path.join(srv.data, "hang.log.1"))
        srv.stop()
        rest = _drain(s, lambda g: "STOPPING=1" in g, 15)
        assert "STOPPING=1" in rest, rest
        print("server: READY=1 + STATUS once the UI listens, WATCHDOG=1 pings, STOPPING=1 at "
              "the stop; watchdog limit logged; previous hang reported; loop lag in /metrics  OK")
    finally:
        srv.stop()
        s.close()
        shutil.rmtree(srv.tmp, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)


def test_when_ready_in_process():
    import json
    d = tempfile.mkdtemp(prefix="s2n_ready_")
    port = free_port()
    cfg = os.path.join(d, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": port}, "users": []}, fh)
    seen = []
    runtime.when_ready(lambda: seen.append(("ready", wait_port(port, 0.1) or "?")))
    t = threading.Thread(target=lambda: runtime.main(cfg), daemon=True)
    t.start()
    try:
        end = time.monotonic() + 20
        while not seen and time.monotonic() < end:
            time.sleep(0.05)
        assert seen and seen[0][0] == "ready", seen
        assert wait_port(port, 5)
        assert runtime._ready_hooks == [], "a hook is called once"
    finally:
        runtime.request_shutdown()
        t.join(30)
        runtime._shutdown_requested.clear()
        shutil.rmtree(d, ignore_errors=True)
    print("runtime.when_ready: called once the admin UI listens  OK")


def main():
    test_settings_and_previous_hang()
    test_frozen_process_is_not_a_hang()
    test_blocked_loop_exits()
    test_sd_notify()
    test_server_notifies()
    test_when_ready_in_process()
    print("\nPASS: liveness and readiness")


if __name__ == "__main__":
    main()
