"""One-off helper: refresh docs/screenshots/* from the running UI via Playwright.

    pip install playwright && python -m playwright install chromium
    python tools/_capture_screenshots.py

Starts TWO ser2net nodes that share a cluster key so the dashboard's LAN-cluster
panel shows a real multi-host view, completes first-run setup on the primary node,
adds an operator + viewer (to populate the Users panel), then captures the login,
dashboard (with the cluster panel), add-mapping form, settings and cluster pages.
Not part of the app or the test suite.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from playwright.sync_api import sync_playwright   # noqa: E402
from app.web.auth import hash_password            # noqa: E402

SHOTS = os.path.join(ROOT, "docs", "screenshots")
UI = 8099            # primary node (the browser drives this one)
UI_B = 8100          # second cluster node (data source for the fleet view)
BASE = f"http://127.0.0.1:{UI}"
CLUSTER_KEY = "demo-cluster-key"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


DPORT = free_port()  # shared UDP discovery port for both nodes

# Primary node's mappings (the browser node).
DEMO_MAPPINGS = [
    {"name": "PLC-1", "enabled": False, "serial": {"port": "COM3", "baudrate": 9600},
     "network": {"mode": "server", "protocol": "rfc2217", "bind_ip": "0.0.0.0", "port": 4001}},
    {"name": "Scale-2", "enabled": False, "serial": {"port": "/dev/ttyUSB0", "baudrate": 19200},
     "network": {"mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4002,
                 "max_connections": 4}},
    {"name": "GPS-roof", "enabled": False, "serial": {"port": "/dev/ttyUSB1", "baudrate": 38400},
     "network": {"mode": "udp", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4003}},
    {"name": "Modbus-GW", "enabled": False, "serial": {"port": "COM5", "baudrate": 9600},
     "network": {"mode": "server", "protocol": "modbus", "bind_ip": "0.0.0.0", "port": 502}},
    {"name": "Lab-bridge", "enabled": False, "kind": "serialbridge",
     "serial": {"port": "COM6", "baudrate": 115200}, "serial_b": {"port": "COM7", "baudrate": 115200}},
]

# Second node's mappings (shown as a remote host in the fleet table).
DEMO_MAPPINGS_B = [
    {"name": "Robot-arm", "enabled": False, "serial": {"port": "/dev/ttyACM0", "baudrate": 115200},
     "network": {"mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4001}},
    {"name": "Welder-3", "enabled": False, "serial": {"port": "COM4", "baudrate": 9600},
     "network": {"mode": "server", "protocol": "rfc2217", "bind_ip": "0.0.0.0", "port": 4002}},
    {"name": "Weigh-east", "enabled": False, "serial": {"port": "/dev/ttyUSB3", "baudrate": 19200},
     "network": {"mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4003}},
]


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def start_node(cfg_obj):
    tmp = tempfile.mkdtemp(prefix="ser2net_shots_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump(cfg_obj, fh)
    proc = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                            cwd=ROOT, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def main():
    cluster = {"enabled": True, "key": CLUSTER_KEY, "discovery_port": DPORT}
    # Bind both nodes to 0.0.0.0 so each is reachable at its auto-detected LAN IP
    # (that is the address the other node advertises and fetches).
    node_a = start_node({"admin_ui": {"bind_ip": "0.0.0.0", "port": UI},
                         "cluster": cluster, "mappings": DEMO_MAPPINGS})
    # Second node is pre-seeded with an admin so its key-guarded /api/cluster/local
    # responds immediately (no first-run /setup gate); we never log into it.
    node_b = start_node({"admin_ui": {"bind_ip": "0.0.0.0", "port": UI_B},
                        "users": [{"username": "admin", "password_hash": hash_password("clusterdemo1"),
                                   "role": "admin", "pwd_version": 1}],
                        "cluster": cluster, "mappings": DEMO_MAPPINGS_B})
    try:
        assert wait_port(UI), "primary node did not start"
        assert wait_port(UI_B), "second node did not start"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                      device_scale_factor=2)
            page = ctx.new_page()

            # first-run setup
            page.goto(f"{BASE}/setup")
            page.fill("input[name=username]", "admin")
            page.fill("input[name=password]", "adminpass123")
            page.fill("input[name=password2]", "adminpass123")
            page.click("button[type=submit]")
            page.wait_for_url(f"{BASE}/")

            # add an operator + a viewer so the Users panel is populated
            page.goto(f"{BASE}/settings")
            for uname, role in (("operator1", "operator"), ("viewer1", "viewer")):
                page.fill("form[action='/settings/users'] input[name=username]", uname)
                page.select_option("form[action='/settings/users'] select[name=role]", role)
                page.fill("form[action='/settings/users'] input[name=password]", "userpass123")
                page.fill("form[action='/settings/users'] input[name=password2]", "userpass123")
                page.click("form[action='/settings/users'] button[type=submit]")
                page.wait_for_load_state("networkidle")

            # 04 settings (now includes the LAN cluster section)
            page.goto(f"{BASE}/settings")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=os.path.join(SHOTS, "04-settings.png"), full_page=True)

            # 02 dashboard — wait for the cluster panel to discover the second node
            page.goto(f"{BASE}/")
            page.wait_for_selector("table.grid")
            try:
                page.wait_for_function(
                    f"() => document.querySelector('#cluster') && "
                    f"document.querySelector('#cluster').textContent.includes(':{UI_B}')",
                    timeout=15000)
            except Exception:
                print("warning: second node did not appear in the cluster panel "
                      "(UDP discovery may be blocked in this environment)")
            page.wait_for_timeout(600)
            page.screenshot(path=os.path.join(SHOTS, "02-dashboard.png"), full_page=True)

            # 06 cluster — close-up of just the fleet panel
            panel = page.query_selector("#cluster")
            if panel:
                panel.screenshot(path=os.path.join(SHOTS, "06-cluster.png"))

            # 03 add-mapping form
            page.click("text=+ Add mapping")
            page.wait_for_selector("input[name=mqtt_base_topic]")
            page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(SHOTS, "03-add-mapping.png"), full_page=True)

            # 01 login (log out first)
            page.click("text=Logout")
            page.wait_for_url(f"{BASE}/login")
            page.wait_for_timeout(200)
            page.screenshot(path=os.path.join(SHOTS, "01-login.png"))

            browser.close()
        print("captured: 01-login, 02-dashboard, 03-add-mapping, 04-settings, 06-cluster")
    finally:
        for proc in (node_a, node_b):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
