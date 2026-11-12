"""Developer helper: refresh docs/screenshots/* from the running UI via Playwright.

    pip install playwright && python -m playwright install chromium
    python tools/_capture_screenshots.py [--lang en tr] [--out docs/screenshots]
                                         [--as-version X.Y.Z]

Starts TWO ser2net nodes that share a cluster key, so the dashboard's LAN cluster panel
shows a real multi-host view, with demo mappings and accounts only (no real names,
addresses or devices: one mapping reads a socat PTY fed with made-up NMEA sentences, the
others are stopped). For each language (the English images in --out, the Turkish ones in
--out/tr for README.md) it captures the sign-in page, the dashboard, the LAN cluster
panel, a new mapping's form, the console in its hex view and Settings → Notifications — at
1280 px and device scale 1, panels as single elements, so they can be read at README size
and keep the repository small. --as-version shows that version in the UI (screenshots
taken before the release commit bumps ser2netweb/__init__.py). Needs socat on PATH for the
console (else that one is skipped). Run it where nothing else uses the network, e.g. a
throwaway network namespace. Not part of the app or the test suite.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

from playwright.sync_api import sync_playwright

from ser2netweb.web.auth import hash_password

CLUSTER_KEY = "demo-cluster-key-0123456789"
PASSWORD = "demo-pass-123"  # noqa: S105  (the demo accounts' password)
# made-up GPS sentences for the console's hex view
NMEA = [b"$GPGGA,120000.00,4100.000,N,02900.000,E,1,08,0.9,40.0,M,36.0,M,,*4C\r\n",
        b"$GPRMC,120000.00,A,4100.000,N,02900.000,E,0.0,0.0,010126,,,A*6F\r\n"]


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def users() -> list[dict]:
    h = hash_password(PASSWORD)
    return [{"username": name, "password_hash": h, "role": role, "pwd_version": 1}
            for name, role in (("admin", "admin"), ("operator1", "operator"),
                               ("viewer1", "viewer"))]


def demo_mappings(gps_device: str, mapping_port: int) -> list[dict]:
    return [
        {"name": "PLC-1", "enabled": False, "serial": {"port": "COM3", "baudrate": 9600},
         "network": {"mode": "server", "protocol": "rfc2217", "bind_ip": "0.0.0.0",
                     "port": 4001}},
        {"name": "Scale-2", "enabled": False, "serial": {"port": "/dev/ttyUSB0", "baudrate": 19200},
         "network": {"mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4002,
                     "max_connections": 4}},
        {"name": "GPS-roof", "enabled": bool(gps_device),
         "serial": {"port": gps_device or "/dev/ttyUSB1", "baudrate": 38400},
         "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                     "port": mapping_port}},
        {"name": "Modbus-GW", "enabled": False,
         "serial": {"port": "/dev/ttyUSB2", "baudrate": 9600, "parity": "E"},
         "network": {"mode": "server", "protocol": "modbus", "bind_ip": "0.0.0.0", "port": 1502},
         "mqtt": {"enabled": True, "host": "broker.example.com", "base_topic": "plant/line1"},
         "modbus_poll": {"interval_s": 5, "points": [
             {"name": "temperature", "unit": 1, "fn": 4, "address": 0, "dtype": "float32"}]}},
        {"name": "Lab-bridge", "enabled": False, "kind": "serialbridge",
         "serial": {"port": "COM6", "baudrate": 115200},
         "serial_b": {"port": "COM7", "baudrate": 115200}},
    ]


DEMO_MAPPINGS_B = [
    {"name": "Robot-arm", "enabled": False, "serial": {"port": "/dev/ttyACM0", "baudrate": 115200},
     "network": {"mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4001}},
    {"name": "Welder-3", "enabled": False, "serial": {"port": "COM4", "baudrate": 9600},
     "network": {"mode": "server", "protocol": "rfc2217", "bind_ip": "0.0.0.0", "port": 4002}},
    {"name": "Weigh-east", "enabled": False, "serial": {"port": "/dev/ttyUSB3", "baudrate": 19200},
     "network": {"mode": "server", "protocol": "raw", "bind_ip": "0.0.0.0", "port": 4003}},
]


def wait_port(port: int, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def start_node(tmp: str, name: str, cfg: dict, version: str):
    folder = os.path.join(tmp, name)
    os.makedirs(folder)
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    # --as-version: the version is set before ser2net.py imports ser2netweb (every module takes it
    # from there)
    code = ("import runpy, sys, ser2netweb\n"
            f"if {version!r}: ser2netweb.__version__ = {version!r}\n"
            "sys.argv = ['ser2net.py'] + sys.argv[1:]\n"
            "runpy.run_path('ser2net.py', run_name='__main__')\n")
    log = open(os.path.join(folder, "out.log"), "wb")
    # run in the temporary folder, where the demo GPS device is (a short relative path on
    # the pages instead of this machine's folders); ser2net.py by its full path
    code = code.replace("'ser2net.py', run_name", repr(os.path.join(ROOT, "ser2net.py")) + ", run_name")
    return subprocess.Popen([sys.executable, "-c", code, "--no-bootstrap", "--config", path],
                            cwd=tmp, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            env=dict(os.environ, PYTHONPATH=ROOT))


def start_gps(tmp: str):
    """A PTY pair (socat) and a thread that writes NMEA sentences into one end; the mapping
    opens the other. (None, None) without socat."""
    socat = shutil.which("socat")
    if not socat:
        print("warning: no socat on PATH: the console screenshot is skipped")
        return None, None, ""
    dev, feed = os.path.join(tmp, "ttyGPS0"), os.path.join(tmp, "gps-feed")
    proc = subprocess.Popen([socat, f"pty,raw,echo=0,link={dev}", f"pty,raw,echo=0,link={feed}"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(dev) and os.path.exists(feed):
            break
        time.sleep(0.1)
    stop = threading.Event()

    def feed_loop():
        try:
            with open(feed, "wb", buffering=0) as fh:
                while not stop.is_set():
                    for line in NMEA:
                        fh.write(line)
                    stop.wait(0.5)
        except OSError:  # (socat ended)
            pass
    threading.Thread(target=feed_loop, daemon=True).start()
    return proc, stop, "ttyGPS0"  # (relative to the nodes' folder: start_node)


def capture(page, base: str, lang: str, out: str, cluster_port: int, console: bool) -> None:
    os.makedirs(out, exist_ok=True)

    def shot(name: str, element: str | None = None) -> None:
        path = os.path.join(out, name)
        if element:
            page.locator(element).first.screenshot(path=path)
        else:
            page.screenshot(path=path)
        print(f"  {os.path.relpath(path, ROOT)}")

    page.goto(f"{base}/lang/{lang}?next=/login")
    page.wait_for_selector("input[name=username]")
    shot("01-login.png")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", PASSWORD)
    page.click("form[action$='/login'] button[type=submit]")
    page.wait_for_url(f"{base}/")
    page.wait_for_selector("table.grid")
    try:
        page.wait_for_function(
            "p => document.querySelector('#cluster') && "
            "document.querySelector('#cluster').textContent.includes(':' + p)",
            arg=str(cluster_port), timeout=20000)
    except Exception:
        print("warning: the second node did not appear in the cluster panel")
    page.wait_for_timeout(800)
    shot("02-dashboard.png")
    if page.query_selector("#cluster"):
        shot("06-cluster.png", "#cluster")
    if console:
        page.locator("tr", has_text="GPS-roof").locator("button[hx-get$='/console-view']").click()
        page.wait_for_selector("#term .xterm")
        page.click("#form-panel [data-term-mode]")   # hex view
        page.wait_for_timeout(2500)
        shot("05-console.png", "#form-panel")
        page.click("#console-close")
        page.wait_for_timeout(300)
    page.click("button[hx-get$='/api/mappings/form']")
    page.wait_for_selector("#form-panel input[name=mqtt_base_topic]")
    page.fill("#form-panel input[name=name]", "PLC-2")
    page.wait_for_timeout(400)
    shot("03-add-mapping.png", "#form-panel")
    # the Notifications panel: the top of Settings shows this machine's paths and host name
    page.goto(f"{base}/settings")
    page.wait_for_load_state("networkidle")
    shot("04-settings.png", "#notifications")
    page.click("form[action$='/logout'] button")
    page.wait_for_url(f"{base}/login")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lang", nargs="+", default=["en", "tr"], choices=["en", "tr"])
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshots"))
    ap.add_argument("--as-version", default="", help="the version the UI shows")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="ser2net_shots_")
    ui_a, ui_b, mapping_port = free_port(), free_port(), free_port()
    cluster = {"enabled": True, "key": CLUSTER_KEY,
               "discovery_port": free_port(socket.SOCK_DGRAM)}
    socat, stop, gps = start_gps(tmp)
    nodes = []
    try:
        nodes.append(start_node(tmp, "a", {
            "admin_ui": {"bind_ip": "0.0.0.0", "port": ui_a, "node_name": "gateway-a"},
            "users": users(),
            "cluster": cluster, "mappings": demo_mappings(gps, mapping_port)}, args.as_version))
        nodes.append(start_node(tmp, "b", {
            "admin_ui": {"bind_ip": "0.0.0.0", "port": ui_b, "node_name": "gateway-b"},
            "users": users()[:1],
            "cluster": cluster, "mappings": DEMO_MAPPINGS_B}, args.as_version))
        assert wait_port(ui_a) and wait_port(ui_b), "a node did not start"
        base = f"http://127.0.0.1:{ui_a}"
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for lang in args.lang:
                ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                          device_scale_factor=1)
                capture(ctx.new_page(), base, lang,
                        args.out if lang == "en" else os.path.join(args.out, lang),
                        ui_b, console=bool(gps))
                ctx.close()
            browser.close()
    finally:
        for proc in nodes + ([socat] if socat else []):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if stop is not None:
            stop.set()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
