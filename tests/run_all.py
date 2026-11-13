"""Unified test runner.

Runs the portable test suite (no serial hardware, no socat) on any platform —
Windows and Linux alike. Pass --socat to also run the socat/PTY-based data-path
tests (Linux only; requires the `socat` binary on PATH).

    python3 tests/run_all.py            # portable suite (cross-platform)
    python3 tests/run_all.py --socat    # + socat data-path tests (Linux)
    python3 tests/run_all.py -v         # also show the output of passing tests

Exit code is non-zero if any selected test fails, so CI can gate on it. A passing test
that skipped a part (a line starting with SKIP: an optional package, a tool or an opt-in
environment variable is missing) has those lines repeated under its PASS line, and the
summary lists it.
"""
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_TIMEOUT = 240  # seconds; longest real tests finish in ~7s, so this only traps hangs
_SKIP_LINE = re.compile(r"^\s*SKIP\b")

# Confirmed cross-platform (stdlib + ./lib; no socat, no real serial device).
PORTABLE = [
    "test_config_validation.py",
    "test_dir_permissions.py",
    "test_env_bind.py",
    "test_boot_config.py",
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
    "test_form_uploads.py",    # form posts refuse file parts (400, not 500)
    "test_starlette_compat.py",  # body limits: Starlette's, or the stand-ins for one before 1.6
    "test_rest_api.py",
    "test_api_token_role.py",
    "test_rest_patch.py",     # PATCH /api/v1/mappings/{id} (JSON merge patch)
    "test_rest_import.py",    # POST /api/v1/import (dry run), /api/v1/metrics
    "test_api_tokens.py",     # named REST API tokens (role, expiry, audit name)
    "test_modbus_frame.py",
    "test_modbus_gateway.py",
    "test_rbac.py",
    "test_route_access.py",   # every route and WebSocket against the access matrix
    "test_console_feed.py",   # console / hex-view feeds: bounds, batches, close, limits
    "test_mqtt_pub.py",
    "test_mqtt_link.py",      # MQTT connection log, status, /metrics; broker parts need paho
    "test_mqtt_tls.py",       # MQTT TLS options; needs cryptography, broker parts paho
    "test_mqtt_identity.py",  # MQTT topics, client IDs, Sparkplug nodes apart; Copy
    "test_modbus_points.py",  # point types, the points JSON and its error path
    "test_modbus_point_reads.py",  # word order, 64-bit, strings, bits, block reads, unit map
    "test_modbus_poll.py",
    "test_modbus_unit_health.py",
    "test_ldap.py",
    "test_oidc.py",
    "test_directory_login.py",  # LDAP/OIDC sign-in end to end; skips without ldap3 / authlib
    "test_external_accounts.py",  # LDAP/OIDC accounts in the Users panel (no directory needed)
    "test_ldap_tls.py",       # LDAPS / StartTLS certificate checks; TLS parts need ldap3
    "test_optional_packages.py",
    "test_cluster.py",
    "test_netinfo.py",        # interface lists also where /proc/net is hidden (systemd unit)
    "test_bind_hint.py",      # a refused low port says why (CAP_NET_BIND_SERVICE), UI + log
    "test_ui_markup.py",
    "test_ui_layout.py",      # browser layout check; skips without Playwright (CI 'ui' job runs it)
    "test_ui_feedback.py",    # what the dashboard's htmx requests answer (notices, 286, next=)
    "test_ui_contrast.py",    # status colours readable in both themes (WCAG AA)
    "test_live_table.py",     # polled tables: 204 / counters only / whole table, off the loop
    "test_confirmations.py",  # disruptive actions ask first (only those)
    "test_settings_prg.py",   # Settings forms: redirect after a change, a refused one refilled
    "test_tls_selfsigned.py",
    "test_standalone.py",     # binary part needs a build; CI 'standalone' workflow runs it
    "test_vendor_wheels.py",
    "test_lib_refresh.py",    # ./lib follows the pins after an upgrade (real pip, vendor/wheels)
    "test_old_names.py",      # ser2net.py and the SER2NET_* variables of 2.9.x keep working
    "test_kept_names.py",     # wire, metric, cookie and default names that stay ser2net
    "test_bootstrap_offline.py",  # offline first run without psutil's wheel, pip/Python checks
    "test_build_lock.py",     # requirements-lock/-build.txt (hashes) match the pins
    "test_vendor_assets.py",  # htmx / xterm.js recorded (vendor/npm), cached by content hash
    "test_third_party.py",    # SBOM / --licenses: SPDX ids, native libs + CPEs, npm packages
    "test_trace.py",
    "test_trace_files.py",
    "test_session_timeout.py",
    "test_session_revocation.py",
    "test_account_recovery.py",
    "test_totp.py",           # two-factor sign-in (TOTP) for local accounts
    "test_first_run_setup.py",
    "test_backups_import.py",
    "test_audit_log.py",
    "test_sign_in_limits.py",  # request body limits, failed sign-in records, log caps
    "test_login_throttle.py",  # sign-in attempt limits (address, name), password work
    "test_password_hashing.py",  # hash format and cost, older hashes, renewal at sign-in
    "test_healthcheck.py",
    "test_upgrade_from_2_7.py",
    "test_upgrade_from_2_8.py",  # a 2.8.0 config.json (tests/make_config_fixture.py) and data/
    "test_windows_service.py",  # real service part: Windows + SER2NETWEB_SERVICE_TESTS=1 (CI)
    "test_systemd_units.py",    # systemd/*.service (also ser2netweb@.service): paths, sandbox, verify
    "test_install_script.py",   # systemd/ser2netweb-install.sh, staged under a scratch root
    "test_install_layout.py",   # default data folder per installation (source, binary, package, pip)
    "test_packages.py",         # .deb / .rpm: packaging/, the unit, the scripts (CI installs them)
    "test_pyproject.py",        # the PyPI package: ranges hold the pins, package data, build tools
    "test_single_instance.py",
    "test_notifications.py",
    "test_ports_usb_identity.py",
    "test_port_watcher.py",
    "test_port_picker.py",
    "test_dashboard.py",
    "test_modbus_mqtt_write.py",
    "test_modbus_mqtt_broker.py",  # edge mode with real paho and a test broker; needs paho
    "test_modbus_write.py",
    "test_sparkplug.py",
    "test_sparkplug_node.py",
    "test_ser2net_import.py",
    "test_import_safety.py",
    "test_i18n.py",
    "test_mapping_ids.py",
    "test_serial_url_ports.py",
    "test_serial_port_names.py",
    "test_cluster_peer_save.py",
    "test_access_policy.py",
    "test_traffic_counters.py",
    "test_protocol_parsers.py",
    "test_protocol_commands.py",   # telnet / RFC 2217 BREAK, answers and command order
    "test_console_break.py",       # the web console's BREAK; POSIX PTYs, skips on Windows
    "test_device_unavailable.py",  # disconnect_without_device (stand-in port, loopback)
    "test_serial_mqtt.py",         # MQTT line ends, <topic>/tx writes, the poll string
    "test_rfc2217_settings.py",
    "test_serial_fd_ceiling.py",  # POSIX PTYs (os.openpty); skips on Windows
    "test_serial_compat.py",      # PTY parts POSIX only; the transport parts run on Windows too
    "test_blocked_output.py",   # a device that takes no data stalls nothing (POSIX PTYs)
    "test_fd_limits.py",
    "test_tls_admission.py",
    "test_admin_limits.py",
    "test_event_loop.py",       # real-server part (600 idle sockets, proactor): Windows CI
    "test_cluster_peer_calls.py",
    "test_cluster_settings.py",
    "test_changelog.py",
    "test_shutdown.py",         # SIGTERM-during-start part: POSIX
    "test_listener_retry.py",
    "test_admin_ui_first.py",
    "test_connect_out_link.py",
    "test_serial_diagnostics.py",  # PTY parts POSIX / Linux only; the rest runs on Windows too
    "test_serial_retries.py",      # PTY parts POSIX only; the rest runs on Windows too
    "test_ipv6.py",                # IPv6 parts skip where the machine has no IPv6 loopback
    "test_tls_mapping.py",         # a mapping's TLS files; needs cryptography (SKIP without)
    "test_tls_paths.py",           # TLS paths in a moved data directory; most need cryptography
    "test_browser_hardening.py",  # CSP and the other protections the browser is told to apply
    "test_mqtt_password_form.py",  # the mapping form never holds the stored MQTT password
    "test_reverse_proxy.py",   # WebSockets, the SSO redirect URI, the UI through a proxy
    "test_hsts.py",            # Strict-Transport-Security max-age (admin_ui.hsts_max_age)
    "test_trusted_proxies.py",  # X-Forwarded-For / -Proto from trusted proxies only
    "test_secure_cookies.py",  # Secure cookies and HSTS behind a TLS reverse proxy
    "test_public_url.py",      # admin_ui.public_url and the path the UI is served under
    "test_cookie_names.py",    # cookie names per port, path and scheme; 2.8 names moved
    "test_node_name.py",       # Settings -> Admin interface -> Name of this instance
    "test_console_port_picker.py",  # the console picker tries the admin UI port it offers
    "test_save_start_result.py",  # a mapping save says when the mapping didn't start
    "test_activity_log.py",    # SER2NETWEB_LOG_STDOUT, admin_ui.request_log
    "test_login_notice.py",    # Settings -> Web UI access -> Sign-in notice
    "test_backup_erasure.py",  # deleting backups, a deleted account out of every backup
    "test_privacy_doc.py",     # docs/PRIVACY.md's numbers and settings match the code
    "test_docs.py",            # the READMEs name every CLI option and link every guide; links resolve
    "test_licenses_page.py",   # footer: licence + source link (SER2NETWEB_SOURCE_URL); /licenses
    "test_clock_ages.py",     # ages and client order by the monotonic clock (no wall clock)
    "test_time_stamps.py",    # log/audit times with their UTC offset; old lines still read
    "test_clock_check.py",    # a clock that reads earlier than it can: start-up log, Settings
    "test_text_fold.py",      # Turkish / Unicode search and sort (app.js part with node)
    "test_explain.py",        # OS / network errors explained in the page's language
    "test_config_types.py",
    "test_ignored_settings.py",
    "test_allow_list_unusable.py",
    "test_downgrade.py",
    "test_supervisor_turns.py",
    "test_config_edits.py",
    "test_start_idempotent.py",
    "test_mapping_form_roundtrip.py",  # every setting survives an unchanged form save
    "test_mixed_version_edit.py",   # fleet-view edits with 2.7.1 / 2.8.0 / 2.8.1 nodes
    "test_edit_conflicts.py",
    "test_storage.py",         # disk space and retention (no real disk filled)
    "test_retention.py",       # all.log / audit.log / backups / mapping logs by age
    "test_metrics.py",         # /metrics: HELP/TYPE, states, 401 for scrapers, quiet scrapes
    "test_console_log.py",     # the console copy of the log is written off the event loop
    "test_error_logs.py",      # library warnings, 500s, reset at the banner, all.log rotation blocked
    "test_liveness.py",        # event loop watchdog, hang.log, sd_notify, readiness hooks
    "test_health_events.py",   # no-data alarm, offline cluster nodes, start-up summary
    "test_tls_live.py",        # admin UI TLS applied to new connections, served-state cookies
    "test_config_reload.py",   # SIGHUP / --reload / Settings: config.json applied part by part
    "test_ci_gate.py",          # ci.yml: ci-ok needs every job; required names kept
    "test_harness.py",          # run_all itself: every test listed, timeouts kill what tests start
    "test_release_pipeline.py",  # release.yml gates, SHA-pinned actions
    "test_release_guard.py",    # release tag = version, CHANGELOG section, image tags; release files
    "test_dependency_snapshot.py",  # the Python 3.10 pins go to the dependency graph
    "test_pin_workflows.py",    # a pin bump fails one job; Dependabot branches get wheels
    "test_image_refresh.py",    # weekly image scan; rebuild on a moved base image
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
    "test_serial_reconnect.py",
    "test_no_data_alarm.py",
]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a test and everything it started (servers, socat), which a kill of the test
    alone would leave running, holding their ports, devices and its output pipes."""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)  # its own process group (start_new_session)
    else:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    with contextlib.suppress(OSError):
        proc.kill()


def run_test(path: str, timeout: float = TEST_TIMEOUT) -> tuple[int | None, str, str]:
    """Run one test file: (exit code, stdout, stderr); exit code None when it ran past
    ``timeout`` seconds and was killed, with everything it started (then the output is
    what it wrote until then)."""
    # Children write UTF-8 regardless of the console code page (Windows: cp1252),
    # and undecodable bytes never crash the runner.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, path], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            encoding="utf-8", errors="replace",
                            start_new_session=os.name == "posix")
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:  # what it wrote until then (the pipes close once the whole tree is gone)
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:  # something it started left the process group
            out, err = "", "(no output: a process the test started still holds its pipes)"
        return None, out or "", err or ""
    except BaseException:  # Ctrl+C: the test runs in a session of its own, take it along
        _kill_tree(proc)
        raise


def run_one(name: str, verbose: bool = False, skipped: list | None = None) -> bool:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):  # listed above but gone: a test that never runs
        print(f"FAIL  {name}  (missing: remove it from tests/run_all.py or restore it)")
        return False
    t0 = time.time()
    # Per-test wall-clock cap: a wedged test must fail loudly (with its partial
    # output) rather than hang the whole CI job until the 6-hour runner limit.
    code, stdout, stderr = run_test(path)
    if code is None:
        dt = time.time() - t0
        print(f"FAIL  {name}  ({dt:.1f}s, TIMED OUT after {TEST_TIMEOUT}s)")
        sys.stdout.write(stdout[-2000:])
        sys.stderr.write(stderr[-2000:])
        return False
    dt = time.time() - t0
    ok = code == 0
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({dt:.1f}s)")
    if not ok:
        sys.stdout.write(stdout[-2000:])
        sys.stderr.write(stderr[-2000:])
        return ok
    tail = stdout[-4000:] if verbose else ""
    skips = [ln.strip() for ln in stdout.splitlines() if _SKIP_LINE.match(ln)]
    shown = tail.splitlines() + [ln for ln in skips if ln not in tail]
    sys.stdout.write("".join(f"      {ln}\n" for ln in shown))
    if skips and skipped is not None:
        skipped.append(name)
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
    skipped: list[str] = []
    failed = [name for name in selected if not run_one(name, verbose, skipped)]
    print(f"\n{'-' * 40}\n{len(selected) - len(failed)} passed, {len(failed)} failed")
    if skipped:
        print(f"{len(skipped)} passed with skipped parts (SKIP lines above): " + ", ".join(skipped))
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
