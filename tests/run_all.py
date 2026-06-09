"""Unified test runner.

Runs the portable test suite (no serial hardware, no socat) on any platform —
Windows and Linux alike. Pass --socat to also run the socat/PTY-based data-path
tests (Linux only; requires the `socat` binary on PATH).

    python3 tests/run_all.py            # portable suite (cross-platform)
    python3 tests/run_all.py --socat    # + socat data-path tests (Linux)

Exit code is non-zero if any selected test fails, so CI can gate on it.
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Confirmed cross-platform (stdlib + ./lib; no socat, no real serial device).
PORTABLE = [
    "test_config_validation.py",
    "test_dir_permissions.py",
    "test_env_bind.py",
    "test_udp_acl.py",
    "test_rfc2217_readonly.py",
    "test_stop_with_clients.py",
    "test_start_failure_cleanup.py",
    "test_log_read.py",
    "test_log_rotation.py",
    "test_log_maintenance.py",
    "test_web_auth.py",
    "test_form_validation.py",
    "test_rest_api.py",
    "test_modbus_frame.py",
    "test_modbus_gateway.py",
]

# Data-path tests that need socat-backed PTYs (Linux). Best-effort: skipped if the
# socat binary is missing.
SOCAT = [
    "test_bridge_raw.py",
    "test_rfc2217.py",
    "test_telnet.py",
    "test_options_engine.py",
    "test_access_priority.py",
    "test_fullduplex.py",
    "test_v2_transports.py",
    "test_v2_console.py",
    "test_v2_web.py",
    "test_web_e2e.py",
]


def run_one(name: str) -> bool:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        print(f"SKIP  {name} (missing)")
        return True
    t0 = time.time()
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
    dt = time.time() - t0
    ok = proc.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({dt:.1f}s)")
    if not ok:
        sys.stdout.write(proc.stdout[-2000:])
        sys.stderr.write(proc.stderr[-2000:])
    return ok


def main(argv) -> int:
    want_socat = "--socat" in argv
    selected = list(PORTABLE)
    if want_socat:
        if shutil.which("socat"):
            selected += SOCAT
        else:
            print("note: --socat given but socat binary not found; skipping socat tests")

    print(f"running {len(selected)} test file(s)\n")
    failed = [name for name in selected if not run_one(name)]
    print(f"\n{'-' * 40}\n{len(selected) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
