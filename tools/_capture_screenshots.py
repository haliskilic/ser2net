"""One-off helper: refresh docs/screenshots/* from the running UI via Playwright.

    pip install playwright && python -m playwright install chromium
    python tools/_capture_screenshots.py

Seeds a few (disabled) demo mappings, completes first-run setup, adds an operator
and a viewer so the Users panel is populated, then captures the login, dashboard,
add-mapping form and settings pages. Not part of the app or the test suite.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
UI = 8099
BASE = f"http://127.0.0.1:{UI}"

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


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="ser2net_shots_")
    cfg = os.path.join(tmp, "config.json")
    with open(cfg, "w") as fh:
        json.dump({"admin_ui": {"bind_ip": "127.0.0.1", "port": UI}, "mappings": DEMO_MAPPINGS}, fh)
    srv = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", cfg],
                           cwd=ROOT, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_port(UI), "server did not start"
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

            # 04 settings
            page.goto(f"{BASE}/settings")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=os.path.join(SHOTS, "04-settings.png"), full_page=True)

            # 02 dashboard (wait for the mappings table to load)
            page.goto(f"{BASE}/")
            page.wait_for_selector("table.grid")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(SHOTS, "02-dashboard.png"), full_page=True)

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
        print("captured: 01-login, 02-dashboard, 03-add-mapping, 04-settings")
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    main()
