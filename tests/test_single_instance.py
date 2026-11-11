"""One ser2net per data directory (app.datalock), tested on Linux and Windows alike.

A second instance on the same data directory would save its own config.json over the
first's and lose its edits. runtime.main() takes an exclusive lock on <data>/ser2net.lock
before writing and holds it until it stops; a second start stops with one line naming the
holder, and --healthcheck / --install-service only test the lock, never hold it. Where the
lock can't be taken (read-only data dir) the start goes on, with a warning in the log.
A --set-password / --sign-out-all holds it briefly ("<pid> brief") while it changes
config.json: a start waits for it (and refuses, saying so, if it takes too long), while
an ordinary holder is refused at once.

Run: python3 tests/test_single_instance.py
"""
import os
import subprocess
import sys
import tempfile
import threading
import time

from _server import ROOT, free_port, wait_port

sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from app import datalock, runtime  # noqa: E402

# A subprocess that tries to take the lock and reports what happened, so the holder is a
# different process (an flock is per-process: the same process would re-lock its own fd).
_PROBE = (
    "import sys; sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[2] + '/lib')\n"
    "from app import datalock as l\n"
    "try:\n"
    "    note = l.acquire(sys.argv[1])\n"
    "    print('ACQUIRED' + (' WARN ' + note if note else ''))\n"
    "    l.release(sys.argv[1])\n"
    "except l.Held as e:\n"
    "    print(f'HELD {e.pid}' + (' brief' if e.brief else ''))\n"
    "print('HOLDER', l.holder(sys.argv[1]))\n"
)


def probe(data_dir):
    out = subprocess.run([sys.executable, "-c", _PROBE, data_dir, ROOT],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def test_lock_blocks_a_second_process():
    d = tempfile.mkdtemp(prefix="s2n_lock_")
    try:
        assert datalock.acquire(d) is None, "first acquire should succeed silently"
        assert os.path.isfile(os.path.join(d, datalock.LOCK_NAME))
        held = probe(d)
        assert held.startswith(f"HELD {os.getpid()}"), held  # names this process
        assert f"HOLDER {os.getpid()}" in held, held

        datalock.release(d)
        after = probe(d)
        assert after.startswith("ACQUIRED") and "WARN" not in after, after
        assert "HOLDER None" in after, after  # released in-process: nobody holds it now

        # acquire is idempotent in one process (the service thread re-enters main())
        assert datalock.acquire(d) is None and datalock.acquire(d) is None
        datalock.release(d)
        print("data-dir lock: a second process is refused and names the holder; "
              "released on exit  OK")
    finally:
        datalock.release(d)
        _rmtree(d)


def test_readonly_dir_falls_back_with_a_warning():
    if os.name != "posix":  # chmod 0500 only models a read-only dir on POSIX
        print("SKIP read-only data-dir fallback (POSIX only)")
        return
    d = tempfile.mkdtemp(prefix="s2n_lock_ro_")
    try:
        os.chmod(d, 0o500)  # can't create the lock file
        note = datalock.acquire(d)
        assert note and note.startswith("WARNING: can't lock the data directory"), note
        assert datalock.holder(d) is None
        print("read-only data dir: start goes on with a warning, holder() stays quiet  OK")
    finally:
        os.chmod(d, 0o700)
        datalock.release(d)
        _rmtree(d)


def test_second_server_refuses_to_start():
    """runtime.main() holds the lock, so a real second start on the data dir exits with
    one line and no traceback — the path --reconfigure next to a running service takes."""
    d = tempfile.mkdtemp(prefix="s2n_lock_srv_")
    port = free_port()
    cfg = os.path.join(d, "config.json")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write('{"admin_ui": {"bind_ip": "127.0.0.1", "port": %d}, "users": []}' % port)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", runtime.main(cfg)), daemon=True)
    t.start()
    try:
        assert wait_port(port), "first server did not start"
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", d],
                           cwd=ROOT, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, env=env)
        assert p.returncode != 0, p.stdout + p.stderr
        assert "already running with the data directory" in (p.stdout + p.stderr), p.stdout + p.stderr
        assert "Traceback" not in p.stderr, p.stderr
        print("second server on the same data dir: refused with one line, no traceback  OK")
    finally:
        runtime.request_shutdown()
        t.join(30)
        runtime._shutdown_requested.clear()
        _rmtree(d)


# Holds the lock in another process (brief or not) for argv[3] seconds.
_HOLD = (
    "import sys, time; sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[2] + '/lib')\n"
    "from app import datalock as l\n"
    "assert l.acquire(sys.argv[1], brief=sys.argv[4] == 'brief') is None\n"
    "print('held', flush=True)\n"
    "time.sleep(float(sys.argv[3]))\n"
)


def hold(data_dir, seconds, kind):
    p = subprocess.Popen([sys.executable, "-c", _HOLD, data_dir, ROOT, str(seconds), kind],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_start_waits_for_a_brief_holder():
    """What runtime.main() does first (hold_data_dir): it waits while an account command
    holds the lock, and refuses an ordinary holder without waiting for it."""
    d = tempfile.mkdtemp(prefix="s2n_lock_brief_")
    cfg = os.path.join(d, "config.json")
    saved = datalock.BRIEF_WAIT_S
    p = None
    try:
        p = hold(d, 3, "brief")
        held = time.monotonic()
        assert probe(d).startswith(f"HELD {p.pid} brief"), probe(d)
        assert runtime.hold_data_dir(cfg) is None
        assert time.monotonic() - held >= 2.5, "taken while the account command still held it"
        assert p.wait(10) == 0
        datalock.release(d)
        print("a start waits while --set-password / --sign-out-all holds the lock  OK")

        datalock.BRIEF_WAIT_S = 0.3
        p = hold(d, 30, "brief")
        try:
            runtime.hold_data_dir(cfg)
            raise AssertionError("taken while held")
        except SystemExit as e:
            assert "--set-password / --sign-out-all" in str(e.code) and \
                "start again once it is done" in str(e.code), e.code
        p.kill()
        p.wait(10)

        datalock.BRIEF_WAIT_S = 30.0
        p = hold(d, 30, "server")
        assert datalock.holder(d) == p.pid
        t = time.monotonic()
        try:
            runtime.hold_data_dir(cfg)
            raise AssertionError("taken while held")
        except SystemExit as e:
            assert "already running with the data directory" in str(e.code), e.code
        assert time.monotonic() - t < 5, "an ordinary holder must be refused without the wait"
        print("... and refuses, saying so, when it takes too long; another ser2net is refused "
              "at once  OK")
    finally:
        datalock.BRIEF_WAIT_S = saved
        if p is not None:
            p.kill()
            p.wait(10)
        datalock.release(d)
        _rmtree(d)


def _rmtree(d):
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def main():
    test_lock_blocks_a_second_process()
    test_readonly_dir_falls_back_with_a_warning()
    test_a_start_waits_for_a_brief_holder()
    test_second_server_refuses_to_start()
    print("\nPASS: single-instance data-directory lock")


if __name__ == "__main__":
    main()
