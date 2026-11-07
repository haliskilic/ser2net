"""Windows service mode (--install-service / --service / --uninstall-service).

Portable part (any OS):
  * the command line the Service Control Manager runs is quoted correctly (spaces,
    quotes, trailing backslashes) for a frozen executable and a source checkout;
  * service names are validated; on Linux the options refuse with a hint (no files);
  * runtime.request_shutdown() — what the service's stop request calls, from another
    thread — stops a running server cleanly.
Windows part (SER2NET_SERVICE_TESTS=1, as Administrator; CI runs it): install a
service on a throwaway data directory, wait until the UI answers, set the admin's
password with --set-password while it runs (the service applies it), kill the process
and see the SCM restart it, stop it (clean shutdown in the log), remove it. Uses
SER2NET_STANDALONE_BIN when set (the frozen exe), else python + ser2net.py.

Run: python3 tests/test_windows_service.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

from _server import ROOT, Browser, free_port, wait_port

from app import runtime, winservice
from app.web.auth import hash_password

WINDOWS = os.name == "nt"


def test_command_line():
    cmd = winservice.service_command("ser2net", r"C:\Program Files\ser2net\data", frozen=True,
                                     executable=r"C:\Program Files\ser2net\ser2net.exe")
    assert cmd == ('"C:\\Program Files\\ser2net\\ser2net.exe" --service --service-name ser2net '
                   '--data-dir "C:\\Program Files\\ser2net\\data"'), cmd
    cmd = winservice.service_command("plant-2", "D:\\s2n\\", config="D:\\cfg dir\\c.json",
                                     frozen=False, executable="C:\\Python312\\python.exe",
                                     script="C:\\src\\ser2net.py")
    assert cmd == ('C:\\Python312\\python.exe C:\\src\\ser2net.py --service --service-name plant-2 '
                   '--data-dir D:\\s2n\\ --config "D:\\cfg dir\\c.json"'), cmd
    assert winservice._quote('a "b"\\') == '"a \\"b\\"\\\\"'
    for bad in ("", "a b", "x" * 65, "a/b", "ser2net;calc"):
        try:
            winservice.check_name(bad)
        except winservice.ServiceError:
            pass
        else:
            raise AssertionError(f"service name {bad!r} accepted")
    assert winservice.display_name("ser2net") == "ser2net"
    assert winservice.display_name("plant-2") == "ser2net (plant-2)"
    print("SCM command line quoted for exe / source; names validated  OK")


def test_acl_decision_table():
    """acl_offenders() is the pure core of the exe/folder permission check (the Windows
    caller reads the ACEs through the API). Runs on any OS."""
    w = winservice
    everyone, auth_users = "S-1-1-0", "S-1-5-11"
    user = "S-1-5-21-1-2-3-1001"
    system, admins = "S-1-5-18", "S-1-5-32-544"
    trusted_installer = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    modify = w.FILE_WRITE_DATA | w.FILE_APPEND_DATA | w._DELETE
    read = 0x00020000 | 0x1  # READ_CONTROL | FILE_READ_DATA

    # a Program Files-style ACL: only SYSTEM / Administrators / TrustedInstaller may write
    assert w.acl_offenders([(system, w.GENERIC_ALL, True), (admins, w.GENERIC_ALL, True),
                            (trusted_installer, w.GENERIC_ALL, True), (auth_users, read, True)]) == []
    # a C:\ser2net-style ACL: Authenticated Users inherit Modify -> escalation path
    assert w.acl_offenders([(system, w.GENERIC_ALL, True), (admins, w.GENERIC_ALL, True),
                            (auth_users, modify, True)]) == [auth_users]
    # any specific non-admin user with write, or just the right to take ownership, counts
    assert w.acl_offenders([(user, w.GENERIC_WRITE, True)]) == [user]
    assert w.acl_offenders([(user, w.WRITE_OWNER, True)]) == [user]
    assert w.acl_offenders([(everyone, modify, True)]) == [everyone]
    # a deny ACE for the same SID cancels its allowed write
    assert w.acl_offenders([(auth_users, modify, True), (auth_users, modify, False)]) == []
    # read-only access is fine; several offenders come back sorted
    assert w.acl_offenders([(auth_users, read, True)]) == []
    assert w.acl_offenders([(user, w.GENERIC_WRITE, True),
                            (everyone, modify, True)]) == sorted([user, everyone])
    # off Windows the ACL reader can't run and never blocks an install
    assert w.writable_by_non_admins(__file__) is None
    assert w.location_warning() is None
    print("ACL decision table: non-admin write on the exe/folder is flagged, deny honoured  OK")


def test_install_locations():
    """Where a frozen build installs itself and keeps a service's data (any OS)."""
    w = winservice
    old = {k: os.environ.get(k) for k in ("ProgramFiles", "ProgramData")}
    os.environ["ProgramFiles"], os.environ["ProgramData"] = r"C:\PF", r"C:\PD"
    try:
        assert w.installed_exe_path("ser2net") == os.path.join(r"C:\PF", "ser2net", "ser2net.exe")
        assert w.installed_exe_path("plant-2") == os.path.join(r"C:\PF", "ser2net", "plant-2.exe")
        assert w.default_service_data_dir("ser2net") == os.path.join(r"C:\PD", "ser2net")
        assert w.default_service_data_dir("plant-2") == os.path.join(r"C:\PD", "ser2net", "plant-2")
        # the SCM command points at the admin-only copy for a frozen install
        cmd = w.service_command("ser2net", r"C:\PD\ser2net", frozen=True,
                                executable=w.installed_exe_path("ser2net"))
        assert w.installed_exe_path("ser2net") in cmd and "--service" in cmd
        # the firewall hint is a program-scoped netsh rule named after the service
        fw = w.firewall_command("plant-2", exe=r"C:\PF\ser2net\plant-2.exe")
        assert fw.startswith("netsh advfirewall firewall add rule") and "ser2net (plant-2)" in fw
        assert r'program="C:\PF\ser2net\plant-2.exe"' in fw and "dir=in action=allow" in fw
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    print("frozen install path under %ProgramFiles%, data under %ProgramData%  OK")


def test_not_windows():
    if WINDOWS:
        return
    d = tempfile.mkdtemp(prefix="s2n_svc_")
    for flag in ("--install-service", "--uninstall-service"):
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir",
                            os.path.join(d, "data"), flag], cwd=ROOT, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert p.returncode == 1 and "only on Windows" in p.stderr, (flag, p.stdout, p.stderr)
    assert not os.path.exists(os.path.join(d, "data")), "nothing may be created on Linux"
    print("Linux: --install-service / --uninstall-service refuse with a systemd hint  OK")


def test_request_shutdown():
    """The service stop path: request_shutdown() from another thread ends runtime.main()."""
    d = tempfile.mkdtemp(prefix="s2n_stop_")
    port = free_port()
    cfg = os.path.join(d, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": port}, "users": []}, fh)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("code", runtime.main(cfg)), daemon=True)
    t.start()
    try:
        assert wait_port(port), "server did not start"
        t0 = time.time()
        runtime.request_shutdown()
        t.join(30)
        assert not t.is_alive(), "request_shutdown() did not stop the server"
        assert result.get("code") == 0, result
        with open(os.path.join(d, "all.log"), encoding="utf-8") as fh:
            assert "shutting down — stopping all mappings" in fh.read()
        print(f"request_shutdown() from another thread: clean stop in {time.time() - t0:.1f}s  OK")
    finally:
        runtime._shutdown_requested.clear()
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- Windows, real SCM
def sc(*args):
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=60)


def healthy(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.read() == b"ok"
    except OSError:
        return False


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False


def test_real_service():
    if not WINDOWS or os.environ.get("SER2NET_SERVICE_TESTS") != "1":
        print("SKIP real Windows service test (Windows + Administrator + SER2NET_SERVICE_TESTS=1)")
        return
    import psutil

    exe = os.environ.get("SER2NET_STANDALONE_BIN")
    base = [os.path.abspath(exe)] if exe else [sys.executable, os.path.join(ROOT, "ser2net.py")]
    name = "s2n-test-" + uuid.uuid4().hex[:6]
    work = tempfile.mkdtemp(prefix="s2n svc ")  # a space in the path, on purpose
    data = os.path.join(work, "data dir")
    os.makedirs(data)
    port = free_port()
    with open(os.path.join(data, "config.json"), "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": port},
                   "users": [{"username": "admin", "password_hash": hash_password("svc-pass-1"),
                              "role": "admin", "pwd_version": 1}]}, fh)
    # a CI runner's workspace is writable by non-admins, so a source-checkout install
    # needs the override; the frozen exe installs itself to an admin-only folder and
    # ignores it. Pass it either way so this test covers both legs.
    install_cmd = base + ["--install-service", "--service-name", name, "--data-dir", data,
                          "--allow-unsafe-location"]
    try:
        p = subprocess.run(install_cmd, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        print(p.stdout.strip())
        assert p.returncode == 0, p.stdout + p.stderr
        assert "is running" in p.stdout
        assert wait_for(lambda: healthy(port), 60), "the service's UI did not answer"
        q = sc("qc", name).stdout
        assert "AUTO_START" in q and "--service" in q and f'"{data}"' in q, q
        print(f"installed + running as service '{name}' (auto start), UI answers  OK")

        p = subprocess.run(base + ["--data-dir", data, "--set-password", "admin"],
                           input="svc-pass-2\n", capture_output=True, text=True, timeout=120)
        assert p.returncode == 0 and "done by the running ser2net" in p.stdout, p.stdout + p.stderr
        Browser(f"http://127.0.0.1:{port}").login("admin", "svc-pass-2")
        print("--set-password while the service runs: the service applies it at once  OK")

        # re-running --install-service upgrades the existing service in place (keeps its
        # name, points it at the current build) instead of failing with 'already exists'
        p = subprocess.run(install_cmd, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "upgraded in place" in p.stdout, p.stdout + p.stderr
        assert wait_for(lambda: healthy(port), 60), "the service did not come back after the upgrade"
        print("re-run --install-service: upgraded in place, still running  OK")

        def service_procs():
            return [pr for pr in psutil.process_iter(["cmdline"])
                    if name in " ".join(pr.info["cmdline"] or [])]
        for pr in service_procs():
            pr.kill()
        assert wait_for(lambda: not healthy(port), 20)
        assert wait_for(lambda: healthy(port), 60), "the SCM did not restart the service"
        print("killed: the SCM restarted it (failure actions)  OK")

        r = sc("stop", name)
        assert r.returncode == 0, r.stdout + r.stderr
        assert wait_for(lambda: "STOPPED" in sc("query", name).stdout, 60), sc("query", name).stdout
        assert not healthy(port)
        with open(os.path.join(data, "all.log"), encoding="utf-8") as fh:
            assert "shutting down — stopping all mappings" in fh.read(), "no clean shutdown"
        print("sc stop: clean shutdown (mappings stopped, logged)  OK")
    finally:
        p = subprocess.run(base + ["--uninstall-service", "--service-name", name],
                           capture_output=True, text=True, timeout=120)
        print(p.stdout.strip() or p.stderr.strip())
    assert "1060" in sc("query", name).stdout, "service still registered"
    assert os.path.isfile(os.path.join(data, "config.json")), "uninstall must keep the data"
    shutil.rmtree(work, ignore_errors=True)
    print("uninstalled; data directory kept  OK")


def main():
    test_command_line()
    test_acl_decision_table()
    test_install_locations()
    test_not_windows()
    test_request_shutdown()
    test_real_service()
    print("\nPASS: Windows service mode")


if __name__ == "__main__":
    main()
