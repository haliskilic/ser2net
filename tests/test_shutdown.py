"""A stop has an overall time limit, and a stop asked for during start-up is kept.

  - Supervisor.stop_all() stops the runners side by side, for at most STOP_ALL_TIMEOUT_S:
    a runner that hangs (a device, broker or client that stopped answering) is logged by
    name and left to finish, the others are stopped meanwhile, not one after another;
  - runtime.stop_engine() gives the whole engine stop at most SHUTDOWN_ENGINE_TIMEOUT_S
    and says so when it cuts it off;
  - the admin UI's uvicorn gets timeout_graceful_shutdown (a response still under way, a
    large trace download, can't hold up the stop);
  - POSIX: a SIGTERM that arrives while the engine is still starting (docker stop in the
    first seconds; Python is PID 1 in the container) is not lost and does not kill the
    process: the admin UI is not started, the mappings are stopped, it exits with 0.
The servers run in child processes on 127.0.0.1.

Run: python3 tests/test_shutdown.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from unittest import mock

from _server import ROOT, free_port

from app import runtime
from app.engine import supervisor
from app.engine.supervisor import Supervisor
from app.web.auth import hash_password


class FakeRunner:
    def __init__(self, name, takes):
        self.mapping = type("M", (), {"name": name})()
        self.takes = takes
        self.stopped = False

    async def stop(self):
        await asyncio.sleep(self.takes)
        self.stopped = True


async def _stop_all():
    lines = []
    sup = Supervisor(logger=lines.append)
    runners = [FakeRunner(f"m{i}", 0.3) for i in range(5)] + [FakeRunner("stuck", 60)]
    sup._runners = {r.mapping.name: r for r in runners}
    with mock.patch.object(supervisor, "STOP_ALL_TIMEOUT_S", 1.0):
        t0 = time.monotonic()
        await sup.stop_all()
        took = time.monotonic() - t0
    assert 0.9 < took < 1.6, f"stop_all took {took:.1f} s"
    assert all(r.stopped for r in runners[:5]), "the other runners must stop meanwhile"
    assert lines == ["stopping: 1 mapping(s) did not stop within 1 s, left to finish on "
                     "their own: stuck"], lines
    assert not sup._runners and len(sup._stopping) == 1
    for task in list(sup._stopping):
        task.cancel()
    await asyncio.sleep(0)

    sup._runners = {f"q{i}": FakeRunner(f"q{i}", 0.3) for i in range(6)}
    t0 = time.monotonic()
    await sup.stop_all()
    took = time.monotonic() - t0
    assert took < 0.9, f"six runners of 0.3 s each took {took:.1f} s: not side by side"
    print(f"stop_all: runners stop side by side; one that hangs is logged and left after "
          f"the timeout ({took:.2f} s for six)  OK")


def test_stop_all():
    asyncio.run(_stop_all())


async def _engine_budget():
    lines = []

    class State:
        def log(self, msg):
            lines.append(msg)

        async def stop_engine(self):
            await asyncio.sleep(60)

    with mock.patch.object(runtime, "SHUTDOWN_ENGINE_TIMEOUT_S", 0.3):
        t0 = time.monotonic()
        await runtime.stop_engine(State())
        took = time.monotonic() - t0
    assert took < 1.0, took
    assert lines == ["shutting down: still not done after 0.3 s, exiting anyway"], lines
    print("the engine stop is cut off after its budget, with a log line  OK")


def test_engine_budget():
    asyncio.run(_engine_budget())


CHILD = textwrap.dedent("""
    import asyncio, os, signal, sys, threading
    sys.path.insert(0, {root!r})
    sys.path.insert(0, os.path.join({root!r}, "lib"))
    import uvicorn
    from app import runtime, state

    seen = {{}}
    real_start, real_serve = state.AppState.start_engine, uvicorn.Server.serve
    mode = sys.argv[2]

    async def slow_start(self):
        if mode == "sigterm":  # the stop arrives while the engine is still starting
            threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
            await asyncio.sleep(1.5)
        await real_start(self)

    async def serve(self, *a, **k):
        seen["timeout"] = self.config.timeout_graceful_shutdown
        if mode == "config":
            return
        return await real_serve(self, *a, **k)

    state.AppState.start_engine = slow_start
    uvicorn.Server.serve = serve
    rc = runtime.main(sys.argv[1])
    print("RC", rc, "SERVED", seen.get("timeout", "never"), flush=True)
""")


def _run_child(mode):
    work = tempfile.mkdtemp(prefix="s2n_shutdown_")
    data = os.path.join(work, "data")
    os.makedirs(data)
    cfg = {"admin_ui": {"bind_ip": "127.0.0.1", "port": free_port()},
           "users": [{"username": "admin", "password_hash": hash_password("pw-123456"),
                      "role": "admin", "pwd_version": 1}],
           "mappings": [{"name": "early", "serial": {"port": "/dev/ttyNOPE0"},
                         "network": {"mode": "server", "protocol": "raw",
                                     "bind_ip": "127.0.0.1", "port": free_port()}}]}
    with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    script = os.path.join(work, "child.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(CHILD.format(root=ROOT))
    t0 = time.monotonic()
    out = subprocess.run([sys.executable, script, os.path.join(data, "config.json"), mode],
                         capture_output=True, text=True, timeout=60)
    took = time.monotonic() - t0
    with open(os.path.join(data, "all.log"), encoding="utf-8") as fh:
        log = fh.read()
    return out, took, log


def test_uvicorn_graceful_timeout():
    out, _took, _log = _run_child("config")
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RC 0 SERVED {runtime.SHUTDOWN_HTTP_TIMEOUT_S}" in out.stdout, out.stdout + out.stderr
    print(f"the admin UI's uvicorn gets timeout_graceful_shutdown="
          f"{runtime.SHUTDOWN_HTTP_TIMEOUT_S}  OK")


def test_sigterm_while_starting():
    if os.name != "posix":
        print("SKIP (part): stop signals during start-up are POSIX only (the Windows "
              "service stops through request_shutdown)")
        return
    out, took, log = _run_child("sigterm")
    assert out.returncode == 0, (out.returncode, out.stdout, out.stderr)
    assert "RC 0 SERVED never" in out.stdout, out.stdout
    assert "stop requested while starting — the admin UI is not started" in log, log[-2000:]
    assert "shutting down — stopping all mappings" in log and "[early] stopped" in log, \
        log[-2000:]
    assert took < 20, took
    print(f"SIGTERM while the engine starts: kept, the admin UI not started, the mappings "
          f"stopped, exit 0 ({took:.1f} s)  OK")


if __name__ == "__main__":
    test_stop_all()
    test_engine_budget()
    test_uvicorn_graceful_timeout()
    test_sigterm_while_starting()
    print("\nPASS: the stop has an overall time limit; a stop during start-up is kept")
