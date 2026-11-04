"""Windows service mode (--install-service / --service / --uninstall-service).

Portable part (any OS):
  * the command line the Service Control Manager runs is quoted correctly (spaces,
    quotes, trailing backslashes) for a frozen executable and a source checkout;
  * service names are validated; on Linux the options refuse with a hint (no files);
  * runtime.request_shutdown() — what the service's stop request calls, from another
    thread — stops a running server cleanly.
Windows part (SER2NET_SERVICE_TESTS=1, as Administrator; CI runs it): install a
service on a throwaway data directory, wait until the UI answers, kill the process
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

from _server import ROOT, free_port, wait_port

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
    try:
        p = subprocess.run(base + ["--install-service", "--service-name", name, "--data-dir", data],
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        print(p.stdout.strip())
        assert p.returncode == 0, p.stdout + p.stderr
        assert "is running" in p.stdout
        assert wait_for(lambda: healthy(port), 60), "the service's UI did not answer"
        q = sc("qc", name).stdout
        assert "AUTO_START" in q and "--service" in q and f'"{data}"' in q, q
        print(f"installed + running as service '{name}' (auto start), UI answers  OK")

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
    test_not_windows()
    test_request_shutdown()
    test_real_service()
    print("\nPASS: Windows service mode")


if __name__ == "__main__":
    main()
