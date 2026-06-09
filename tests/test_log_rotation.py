"""Global logs must rotate, not grow forever (M8).

all.log was a plain FileHandler and audit.log a manual append — both grew
without bound (log maintenance only trimmed per-mapping logs). all.log now uses
a RotatingFileHandler and audit.log is size-rotated. This drives both past a
shrunk threshold and checks a backup appears.

Run: python3 tests/test_log_rotation.py
"""
import logging
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.state as st
from app.config import AppConfig, ConfigStore
from app.state import AppState


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def test_all_log_and_audit_rotate():
    d = tempfile.mkdtemp(prefix="s2n_rot_")
    # shrink thresholds BEFORE constructing AppState (read in _setup_logging)
    st.ALL_LOG_MAX_BYTES = 2048
    st.ALL_LOG_BACKUPS = 2
    st.AUDIT_LOG_MAX_BYTES = 512
    try:
        store = ConfigStore(os.path.join(d, "config.json"))
        state = AppState(store, AppConfig())

        for i in range(300):
            state.log(f"event {i} " + "x" * 40)
        assert os.path.exists(state.log_path + ".1"), "all.log did not rotate"

        for i in range(60):
            state.audit("10.0.0.1", "mapping_save", f"detail-{i}-" + "y" * 30)
        assert os.path.exists(os.path.join(state.data_dir, "audit.log.1")), "audit.log did not rotate"
        # the live audit.log stays small (rotation kicked in)
        assert os.path.getsize(os.path.join(state.data_dir, "audit.log")) <= st.AUDIT_LOG_MAX_BYTES + 512
        print("all.log + audit.log rotate past their size caps  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_all_log_and_audit_rotate()
    print("\nPASS: global log rotation (M8)")


if __name__ == "__main__":
    main()
