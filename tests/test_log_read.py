"""read_mapping_log must read only the tail, not the whole file (M6).

Per-mapping logs can reach 100 MB; loading the entire file just to show the last
1000 lines wasted memory and blocked the event loop. This verifies the reader
returns the most recent lines (newest first) while only touching the tail.

Run: python3 tests/test_log_read.py
"""
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ser2netweb.config import AppConfig, ConfigStore
from ser2netweb.state import AppState


def _close_loggers():
    # AppState opens FileHandlers (all.log + per-mapping) that keep the files open;
    # close them so the temp dir can be removed on Windows.
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def test_tail_read_returns_recent_lines():
    d = tempfile.mkdtemp(prefix="s2n_logread_")
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        state = AppState(store, AppConfig())
        mid = "m1"
        os.makedirs(state.logs_dir, exist_ok=True)
        path = state.mapping_log_path(mid)
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(50000):
                fh.write(f"2026-06-09 00:00:00 line {i}\n")

        lines = state.read_mapping_log(mid, limit=10, tail_bytes=4096)
        assert len(lines) == 10, f"expected 10 lines, got {len(lines)}"
        assert lines[0].endswith("line 49999"), lines[0]      # newest first
        assert lines[-1].endswith("line 49990"), lines[-1]
        assert not any(ln.endswith("line 0") for ln in lines), "early lines leaked (full read?)"
        print("read_mapping_log: tail-only read returns the newest lines  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def test_only_line_feeds_end_a_line():
    """A CR, NEL or U+2028 inside a message (as an older version wrote them) doesn't cut
    the line in two; a CRLF ends one like a line feed."""
    d = tempfile.mkdtemp(prefix="s2n_logread_")
    try:
        state = AppState(ConfigStore(os.path.join(d, "config.json")), AppConfig())
        os.makedirs(state.logs_dir, exist_ok=True)
        with open(state.mapping_log_path("m2"), "w", encoding="utf-8", newline="") as fh:
            fh.write("2026-06-09 00:00:00 a\u2028b\x85c\rd\r\n2026-06-09 00:00:01 e\u2029f\n"
                     "2026-06-09 00:00:02 last")
        assert state.read_mapping_log("m2") == [
            "2026-06-09 00:00:02 last", "2026-06-09 00:00:01 e\u2029f",
            "2026-06-09 00:00:00 a\u2028b\x85c\rd"], state.read_mapping_log("m2")
        assert state.read_mapping_log("nosuch") == []
        print("read_mapping_log: only a line feed (or CRLF) ends a line  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_tail_read_returns_recent_lines()
    test_only_line_feeds_end_a_line()
    print("\nPASS: mapping log tail read (M6)")


if __name__ == "__main__":
    main()
