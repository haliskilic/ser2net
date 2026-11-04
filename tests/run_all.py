"""Unified test runner.

Runs the portable test suite (no serial hardware, no socat) on any platform —
Windows and Linux alike. Pass --socat to also run the socat/PTY-based data-path
tests (Linux only; requires the `socat` binary on PATH).

    python3 tests/run_all.py            # portable suite (cross-platform)
    python3 tests/run_all.py --socat    # + socat data-path tests (Linux)
    python3 tests/run_all.py -v         # also show the output of passing tests

Exit code is non-zero if any selected test fails, so CI can gate on it.
"""
import contextlib
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_TIMEOUT = 240  # seconds; longest real tests finish in ~7s, so this only traps hangs

# Confirmed cross-platform (stdlib + ./lib; no socat, no real serial device).
PORTABLE = [
    "test_config_validation.py",
    "test_dir_permissions.py",
    "test_env_bind.py",
    "test_udp_acl.py",
    "test_rfc2217_readonly.py",
    "test_stop_with_clients.py",
    "test_dead_peer.py",
    "test_conn_timers.py",
    "test_upgrade_from_2_6.py",
    "test_start_failure_cleanup.py",
    "test_log_read.py",
    "test_log_rotation.py",
    "test_log_maintenance.py",
    "test_web_auth.py",
    "test_open_access.py",
    "test_form_validation.py",
    "test_rest_api.py",
    "test_api_token_role.py",
    "test_modbus_frame.py",
    "test_modbus_gateway.py",
    "test_rbac.py",
    "test_mqtt_pub.py",
    "test_modbus_poll.py",
    "test_ldap.py",
    "test_oidc.py",
    "test_cluster.py",
    "test_ui_markup.py",
    "test_ui_layout.py",      # browser layout check; skips without Playwright (CI 'ui' job runs it)
    "test_tls_selfsigned.py",
    "test_standalone.py",     # binary part needs a build; CI 'standalone' workflow runs it
    "test_vendor_wheels.py",
    "test_trace.py",
    "test_session_timeout.py",
    "test_backups_import.py",
    "test_audit_log.py",
    "test_healthcheck.py",
    "test_upgrade_from_2_7.py",
    "test_windows_service.py",  # real service part: Windows + SER2NET_SERVICE_TESTS=1 (CI)
    "test_notifications.py",
    "test_ports_usb_identity.py",
    "test_dashboard.py",
    "test_modbus_mqtt_write.py",
    "test_modbus_write.py",
    "test_sparkplug.py",
    "test_ser2net_import.py",
    "test_i18n.py",
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
    "test_tls_bridge.py",
    "test_v2_console.py",
    "test_v2_web.py",
    "test_web_e2e.py",
]


def run_one(name: str, verbose: bool = False) -> bool:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        print(f"SKIP  {name} (missing)")
        return True
    t0 = time.time()
    # Per-test wall-clock cap: a wedged test must fail loudly (with its partial
    # output) rather than hang the whole CI job until the 6-hour runner limit.
    # Children write UTF-8 regardless of the console code page (Windows: cp1252),
    # and undecodable bytes never crash the runner.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, env=env,
                              encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        dt = time.time() - t0
        print(f"FAIL  {name}  ({dt:.1f}s, TIMED OUT after {TEST_TIMEOUT}s)")
        out = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        err = (e.stderr or "") if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
        sys.stdout.write(out[-2000:])
        sys.stderr.write(err[-2000:])
        return False
    dt = time.time() - t0
    ok = proc.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({dt:.1f}s)")
    if not ok:
        sys.stdout.write(proc.stdout[-2000:])
        sys.stderr.write(proc.stderr[-2000:])
    elif verbose:
        sys.stdout.write("".join(f"      {ln}\n" for ln in proc.stdout[-4000:].splitlines()))
    return ok


def main(argv) -> int:
    with contextlib.suppress(AttributeError, ValueError):  # don't die on a narrow console
        sys.stdout.reconfigure(errors="replace")
    want_socat = "--socat" in argv
    verbose = "-v" in argv or "--verbose" in argv
    selected = list(PORTABLE)
    if want_socat:
        if shutil.which("socat"):
            selected += SOCAT
        else:
            print("note: --socat given but socat binary not found; skipping socat tests")

    print(f"running {len(selected)} test file(s)\n")
    failed = [name for name in selected if not run_one(name, verbose)]
    print(f"\n{'-' * 40}\n{len(selected) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
