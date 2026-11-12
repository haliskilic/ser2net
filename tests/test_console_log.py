"""The console copy of the log is written off the event loop.

Checks (no sockets, no server):
  * a console that takes its output gets every line, in order, by the time log() returns;
  * a console that doesn't (a paused Windows console, a pipe nobody reads) costs one short
    wait, then log() returns at once; lines past the queue's room are dropped and counted,
    and once the console takes output again it gets the queued lines and how many were
    dropped; after it has caught up log() waits for it again;
  * all.log gets every line meanwhile (AppState's logger);
  * a traceback stays below its line, in the format all.log uses;
  * closing (at exit) writes what is queued, and doesn't hang on a console that is stuck.

Run: python3 tests/test_console_log.py
"""
import io
import logging
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import logsetup
from app.state import AppState


class Console(io.StringIO):
    """A console stream whose writes wait while `open` is clear."""

    def __init__(self):
        super().__init__()
        self.open = threading.Event()
        self.open.set()

    def write(self, text):
        self.open.wait()
        return super().write(text)


def _logger(name, handler):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    return lg


def test_console_takes_it():
    out = Console()
    target = logging.StreamHandler(out)
    target.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h = logsetup.ConsoleQueueHandler(target)
    lg = _logger("t.console.ok", h)
    for i in range(200):
        lg.info("line %d", i)
        assert out.getvalue().endswith(f"INFO line {i}\n"), "on the console when log() returns"
    assert out.getvalue().splitlines() == [f"INFO line {i}" for i in range(200)]
    assert not h.behind and h.take_dropped() == 0
    try:
        raise ValueError("bad value")
    except ValueError:
        lg.exception("it failed")
    text = out.getvalue().split("INFO line 199\n", 1)[1]
    first, *rest = text.splitlines()
    assert first == "ERROR it failed", text
    assert rest[0] == "Traceback (most recent call last):" and rest[-1] == "ValueError: bad value", text
    h.close()
    print("console that takes its output: every line, in order, when log() returns; "
          "traceback below its line  OK")


def test_console_blocked():
    out = Console()
    target = logging.StreamHandler(out)
    target.setFormatter(logging.Formatter("%(message)s"))
    h = logsetup.ConsoleQueueHandler(target, maxsize=10, wait=0.05)
    lg = _logger("t.console.blocked", h)
    lg.info("before")
    out.open.clear()  # the console stops taking output
    t0 = time.monotonic()
    lg.info("first while blocked")
    first = time.monotonic() - t0
    assert 0.04 <= first < 1.0 and h.behind, (first, h.behind)
    t0 = time.monotonic()
    for i in range(500):
        lg.info("blocked %d", i)
    took = time.monotonic() - t0
    assert took < 0.5, f"500 lines while the console is blocked took {took:.2f} s"
    assert h.dropped > 400, h.dropped
    out.open.set()  # the console takes output again
    end = time.monotonic() + 5
    while "dropped" not in out.getvalue() and time.monotonic() < end:
        time.sleep(0.01)
    lines = out.getvalue().splitlines()
    assert lines[:2] == ["before", "first while blocked"], lines[:3]
    kept = [ln for ln in lines if ln.startswith("blocked ")]
    assert kept and len(kept) <= 10 and kept == sorted(kept, key=lambda x: int(x.split()[1])), kept
    lg.info("after")
    lines = out.getvalue().splitlines()
    assert lines[-1] == "after" and not h.behind, lines[-3:]
    note = next(ln for ln in lines if "dropped" in ln)
    n = int(note.split()[1])
    assert len(kept) + n == 500, (len(kept), n, note)
    assert note.endswith("the console did not take them (all.log has them)"), note
    print(f"blocked console: one {first * 1000:.0f} ms wait, then 500 lines in "
          f"{took * 1000:.0f} ms; {n} dropped and said so once it took output again; "
          "waited for again after  OK")

    # closing with lines queued writes them; with the console stuck it gives up
    out.open.clear()
    lg.info("queued 1")
    lg.info("queued 2")
    threading.Timer(0.2, out.open.set).start()
    h.close()
    assert out.getvalue().splitlines()[-2:] == ["queued 1", "queued 2"], out.getvalue()[-60:]
    out2 = Console()
    out2.open.clear()
    t2 = logging.StreamHandler(out2)
    h2 = logsetup.ConsoleQueueHandler(t2, wait=0.01)
    lg2 = _logger("t.console.stuck", h2)
    lg2.info("never written")
    t0 = time.monotonic()
    h2.close()
    assert time.monotonic() - t0 < 2 * logsetup.CONSOLE_STOP_WAIT_S + 1
    out2.open.set()
    print("close: queued lines written; a stuck console doesn't hang it  OK")


def test_app_state_logger():
    d = tempfile.mkdtemp(prefix="s2n_console_")
    saved = sys.stdout
    out = Console()
    try:
        sys.stdout = out
        lg = AppState._setup_logging(os.path.join(d, "all.log"))
        lg.info("start")
        assert out.getvalue().endswith(" start\n") and out.getvalue()[:4].isdigit(), out.getvalue()
        out.open.clear()
        t0 = time.monotonic()
        for i in range(300):
            lg.info(f"msg {i}")
        took = time.monotonic() - t0
        assert took < 1.0, f"300 lines with the console blocked took {took:.2f} s"
        with open(os.path.join(d, "all.log"), encoding="utf-8") as fh:
            logged = fh.read().splitlines()
        assert len(logged) == 301 and logged[-1].endswith(" msg 299"), logged[-2:]
        out.open.set()
        # a second set-up stops the first console thread
        before = {t.name for t in threading.enumerate()}.intersection({"console-log"})
        AppState._setup_logging(os.path.join(d, "all.log"))
        consoles = [t for t in threading.enumerate() if t.name == "console-log"]
        assert before and len(consoles) == 1, consoles
    finally:
        sys.stdout = saved
        lg = logging.getLogger("ser2net")
        logsetup.close_console(lg)
        for hd in list(lg.handlers):
            hd.close()
        lg.handlers.clear()
        shutil.rmtree(d, ignore_errors=True)
    print(f"AppState log: 300 lines in {took * 1000:.0f} ms with the console blocked, "
          "all.log has them all; set up again: one console thread  OK")


def main():
    test_console_takes_it()
    test_console_blocked()
    test_app_state_logger()
    print("\nPASS: console log off the event loop")


if __name__ == "__main__":
    main()
