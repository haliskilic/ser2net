"""`ser2net --healthcheck` (the Docker HEALTHCHECK): 0 while the UI answers, 1 when not.

Run: python3 tests/test_healthcheck.py
"""
import json
import os
import subprocess
import sys
import tempfile

from _server import ROOT, Server, free_port

from app import healthcheck


def run_check(data_dir, env=None):
    p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", data_dir,
                        "--healthcheck"], cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, **(env or {})))
    return p.returncode, (p.stdout + p.stderr).strip()


def main():
    d = tempfile.mkdtemp(prefix="s2n_hc_")
    cfg = os.path.join(d, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "0.0.0.0", "port": 8123}}, fh)
    assert healthcheck.target(cfg) == "http://127.0.0.1:8123/healthz"
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "::", "port": 9, "tls_cert": "c", "tls_key": "k"}}, fh)
    assert healthcheck.target(cfg) == "https://[::1]:9/healthz"
    os.environ["SER2NET_PORT"] = "7001"
    try:
        assert healthcheck.target(cfg) == "https://[::1]:7001/healthz", "env must win (as at start)"
    finally:
        del os.environ["SER2NET_PORT"]
    assert healthcheck.target(os.path.join(d, "missing.json")) == "http://127.0.0.1:8080/healthz"
    print("target: wildcard -> loopback, TLS -> https, env overrides, no config -> defaults  OK")

    with Server({"users": []}) as srv:
        code, out = run_check(srv.data, {"SER2NET_PORT": str(srv.port)})
        assert code == 0 and out.startswith("ok: http://127.0.0.1:"), (code, out)
        print(f"running instance: exit 0 ({out})  OK")
        data_dir = srv.data
        port = srv.port
        srv.stop()
        code, out = run_check(data_dir, {"SER2NET_PORT": str(port)})
        assert code == 1 and out.startswith("unhealthy:"), (code, out)
        print("stopped instance: exit 1  OK")
    code, out = run_check(d, {"SER2NET_PORT": str(free_port())})
    assert code == 1, (code, out)
    print("\nPASS: --healthcheck")


if __name__ == "__main__":
    main()
