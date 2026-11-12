"""The test harness itself: tests/run_all.py and the helpers the tests share.

Checked:
  * every tests/test_*.py is in run_all's PORTABLE or SOCAT list, once (a file in neither
    never runs), and every listed file exists; a listed file that is gone fails the run;
  * a test that runs past the timeout is killed with everything it started (servers,
    socat): run_all kills its process group (POSIX) or its process tree (Windows
    taskkill /T), so nothing is left holding ports, devices or the output pipes;
  * _server.Server cleans up (the process, its temp folder) when its start fails, and
    show_log_on_failure prints the server's log only while a test fails;
  * no test starts the server with its output on a pipe it never reads.

Run: python3 tests/test_harness.py
"""
import ast
import contextlib
import io
import os
import tempfile
import time

import run_all
from _server import Server, show_log_on_failure

HERE = os.path.dirname(os.path.abspath(__file__))


def test_every_test_is_registered():
    listed = run_all.PORTABLE + run_all.SOCAT
    twice = sorted({n for n in listed if listed.count(n) > 1})
    assert not twice, f"listed more than once in tests/run_all.py: {twice}"
    files = sorted(n for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py"))
    missing = [n for n in files if n not in listed]
    assert not missing, f"never run (add them to PORTABLE or SOCAT in tests/run_all.py): {missing}"
    gone = [n for n in listed if n not in files]
    assert not gone, f"listed in tests/run_all.py but not there: {gone}"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert run_all.run_one("test_this_file_is_not_there.py") is False
    assert out.getvalue().startswith("FAIL  test_this_file_is_not_there.py  (missing"), out.getvalue()
    print(f"run_all: all {len(files)} test files listed once; a listed file that is gone fails  OK")


# a "test" that starts a helper process (as a test starts a server) and then hangs; the
# helper touches its heartbeat file until it is killed
_HANGS = """
import os, subprocess, sys, time
beat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beat")
subprocess.Popen([sys.executable, "-c",
    "import sys, time\\nwhile True:\\n    open(sys.argv[1], 'a').close(); "
    "__import__('os').utime(sys.argv[1]); time.sleep(0.1)", beat])
print("started the helper", flush=True)
time.sleep(600)
"""


def test_timeout_kills_what_the_test_started():
    d = tempfile.mkdtemp(prefix="s2n_harness_")
    try:
        script, beat = os.path.join(d, "hangs.py"), os.path.join(d, "beat")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(_HANGS)
        t0 = time.monotonic()
        code, out, _err = run_all.run_test(script, timeout=5)
        assert code is None and "started the helper" in out, (code, out)
        assert time.monotonic() - t0 < 60, "collecting the output waited for the helper"
        assert os.path.exists(beat), "the helper never ran"
        time.sleep(1.0)
        before = os.path.getmtime(beat)
        time.sleep(1.5)
        assert os.path.getmtime(beat) == before, "the helper the test started still runs"
    finally:
        for _ in range(20):  # (on Windows the helper's file may take a moment to let go)
            try:
                for name in os.listdir(d):
                    os.remove(os.path.join(d, name))
                os.rmdir(d)
                break
            except OSError:
                time.sleep(0.2)
    print("run_all: a test past its timeout is killed with what it started, its output kept  OK")


class _FailsAfterStart(Server):
    def start(self):
        super().start()
        raise AssertionError("a start that fails after the process is up")


def test_server_cleans_up_a_failed_start():
    srv = _FailsAfterStart({})
    try:
        with srv:
            raise RuntimeError("the with block must not run")
    except AssertionError:
        pass
    assert srv.proc is None, "the server process was left running"
    assert not os.path.exists(srv.tmp), "the server's temp folder was left behind"

    d = tempfile.mkdtemp(prefix="s2n_harness_")
    with open(os.path.join(d, "server.log"), "w", encoding="utf-8") as fh:
        fh.write("the server's last words\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        show_log_on_failure(d)  # nothing fails here
    assert out.getvalue() == "", out.getvalue()
    with contextlib.redirect_stdout(out), contextlib.suppress(AssertionError):
        try:
            raise AssertionError("a failing test")
        finally:
            show_log_on_failure(d)
    assert "the server's last words" in out.getvalue(), out.getvalue()
    os.remove(os.path.join(d, "server.log"))
    os.rmdir(d)
    print("_server: a failed start stops the server and removes its folder; the log is "
          "shown when a test fails  OK")


def _is_pipe(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "PIPE"


def test_no_unread_server_pipes():
    """A server's output on a pipe nobody reads blocks the server once the pipe is full."""
    found = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and getattr(call.func, "attr", getattr(
                    call.func, "id", "")) in ("Popen", "create_subprocess_exec")):
                continue
            runs_server = any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                              and n.value.endswith("ser2net.py") for n in ast.walk(call))
            piped = [k.arg for k in call.keywords if k.arg in ("stdout", "stderr")
                     and _is_pipe(k.value)]
            if runs_server and piped:
                found.append(f"{name}:{call.lineno} ({', '.join(piped)}=PIPE)")
    assert not found, ("a server started with its output on a pipe (use _server.Server or "
                       f"_server.server_log): {found}")
    print("no test starts the server with its output on an unread pipe  OK")


if __name__ == "__main__":
    test_every_test_is_registered()
    test_timeout_kills_what_the_test_started()
    test_server_cleans_up_a_failed_start()
    test_no_unread_server_pipes()
    print("\nPASS: the test harness (run_all, shared helpers)")
