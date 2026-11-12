"""Web panel layout with long names: nothing may stick out of its panel (issue #26).

Long mapping names, /dev/serial/by-id/... paths, remote host names and error
messages used to make the mappings and cluster tables wider than their panel: the
row buttons (Start/Stop/Restart/Edit/Log/Monitor/Copy/Delete) ended up outside it
and the whole page scrolled sideways. This drives a real browser (Playwright +
Chromium) against two real nodes — A shows B through a manual cluster peer — whose
mappings use such values, at desktop, half-screen, tablet and phone widths, and
checks the dashboard, the log / console / edit / trace views, the settings page (users with
row buttons, a backup's restore preview) — in English and again in Turkish, whose texts are
longer:

  * the page never scrolls sideways and no table needs horizontal scrolling
    (wide windows: the table fits; narrow ones: its rows become cards);
  * every element stays inside its panel; row buttons stay inside their cell
    and never overlap; no cell's content overflows the cell;
  * the browser reports no errors (e.g. a Content-Security-Policy violation);
  * the cluster panel isn't refreshed while the tab is hidden (each refresh makes the
    node ask every peer) and is refreshed as soon as the tab is shown again;
  * the mapping form of every kitchen-sink mapping (tests/_form_cases.py), on this node
    and on the peer, posts what tests/_formsubmit.py says a browser posts, and saving
    it unchanged keeps every setting of the mapping.

Opt-in: needs `pip install playwright && python -m playwright install chromium`.
Skipped when Playwright or its browser is missing, unless SER2NET_UI_TESTS=1 (the
CI 'ui' job sets it, so a missing browser fails there instead of skipping). Set
SER2NET_UI_ARTIFACTS=<dir> to keep a screenshot of every page and width.
Like test_cluster.py, the two nodes open their UDP cluster-discovery socket.

Run: python3 tests/test_ui_layout.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

import _form_cases as form_cases  # noqa: E402
from _formsubmit import browser_post  # noqa: E402

from app.web.auth import hash_password  # noqa: E402

REQUIRED = os.environ.get("SER2NET_UI_TESTS") == "1"
ARTIFACTS = os.environ.get("SER2NET_UI_ARTIFACTS", "")
WIDTHS = (1440, 1280, 1024, 960, 768, 390)  # desktop … half-screen … tablet … phone

with open(os.path.join(ROOT, "app", "locale", "tr.json"), encoding="utf-8") as _fh:
    TR = json.load(_fh)

LONG_NAME = "PRODUCTION_LINE_3_PLC_SIEMENS_S7_1500_MAIN_CONTROLLER_BUILDING_B_EAST_WING"
LONG_WORDS = "Packaging line 2 weighing scale near the north loading dock (Mettler Toledo IND780)"
BY_ID = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0"
LONG_HOST = "very-long-hostname-for-the-remote-serial-server.factory-floor.example-industries.internal"
USER = "operations.supervisor.building-b.east-wing"   # long account name in the top bar
TRACE_ID = "uilayout0001"  # the long-named mapping traces: the Trace panel's file rows
TRACE_NAME = "production-line-3-plc-siemens-s7-1500-main-controller-building-b-east.trace"
PASSWORD = "layout-pass-123"

# Returns a list of human-readable layout problems on the current page.
CHECK_JS = r"""() => {
  const bad = [], vw = window.innerWidth;
  const label = el => el.tagName.toLowerCase() + ' "' +
      (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40) + '"';
  const inside = (r, box) => r.left >= box.left - 1 && r.right <= box.right + 1;
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 || r.height > 0; };
  const span = r => `[${Math.round(r.left)}..${Math.round(r.right)}]`;

  const sw = document.documentElement.scrollWidth;
  if (sw > vw + 1) bad.push(`page scrolls sideways: ${sw}px wide in a ${vw}px window`);
  for (const sc of document.querySelectorAll('.table-scroll'))
    if (sc.scrollWidth > sc.clientWidth + 1)
      bad.push(`table needs sideways scrolling: ${sc.scrollWidth}px in ${sc.clientWidth}px`);

  for (const panel of document.querySelectorAll('.panel')) {
    const pr = panel.getBoundingClientRect();
    for (const el of panel.querySelectorAll(
        'button, a.btn, td, th, .state, .tag, .peer, h2, h3, input, select, .err-small')) {
      if (!shown(el)) continue;
      const r = el.getBoundingClientRect();
      if (!inside(r, pr)) bad.push(`${label(el)} sticks out of its panel: ${span(r)} vs ${span(pr)}`);
    }
    for (const td of panel.querySelectorAll('td')) {
      if (shown(td) && td.scrollWidth > td.clientWidth + 1)
        bad.push(`${label(td)} overflows its cell (${td.scrollWidth} > ${td.clientWidth}px)`);
    }
    for (const group of panel.querySelectorAll('.row-actions, .actions')) {
      const box = (group.closest('td') || group.parentElement).getBoundingClientRect();
      const btns = [...group.querySelectorAll('button, a.btn')].filter(shown)
                     .map(b => [b, b.getBoundingClientRect()]);
      for (const [b, r] of btns)
        if (!inside(r, box)) bad.push(`button ${label(b)} outside its cell: ${span(r)} vs ${span(box)}`);
      for (let i = 0; i < btns.length; i++)
        for (let j = i + 1; j < btns.length; j++) {
          const a = btns[i][1], c = btns[j][1];
          if (a.left < c.right - 1 && c.left < a.right - 1 && a.top < c.bottom - 1 && c.top < a.bottom - 1)
            bad.push(`buttons overlap: ${label(btns[i][0])} / ${label(btns[j][0])}`);
        }
    }
  }
  for (const el of document.querySelectorAll('.topbar *')) {
    const r = el.getBoundingClientRect();
    if (shown(el) && r.right > vw + 1) bad.push(`top bar ${label(el)} goes past the window edge`);
  }
  return bad;
}"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def demo_mappings(prefix, raw_port):
    """Worst-case values: an unbroken 75-char name, a by-id path, a long remote host,
    a serial↔serial bridge; enabled ones fail to open their port → long errors."""
    return [
        {"id": TRACE_ID, "name": prefix + LONG_NAME, "enabled": True,
         "serial": {"port": BY_ID, "baudrate": 115200, "flowcontrol": "rtscts"},
         "network": {"mode": "server", "protocol": "rfc2217", "bind_ip": "127.0.0.1",
                     "port": free_port(), "read_only": True},
         "options": {"trace_both": TRACE_NAME, "trace_keep": 50}},
        {"name": prefix + LONG_WORDS, "enabled": False,
         "serial": {"port": BY_ID + "-second", "baudrate": 9600},
         "network": {"mode": "client", "protocol": "raw", "remote_host": LONG_HOST, "remote_port": 4001}},
        {"name": prefix + "bridge_" + LONG_NAME.lower(), "enabled": False, "kind": "serialbridge",
         "serial": {"port": BY_ID, "baudrate": 115200},
         "serial_b": {"port": BY_ID.replace("A50285BI", "B71234CD"), "baudrate": 115200}},
        {"name": prefix + "short", "enabled": True, "serial": {"port": "/dev/ttyUSB0"},
         "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                     "port": raw_port, "max_connections": 4}},
    ]


def kitchen_sinks():
    """Mappings with every setting other than its default (stopped: they bind nothing)."""
    return [dict(m, enabled=False) for m in form_cases.mappings()]


def start_node(cfg, backup=False):
    tmp = tempfile.mkdtemp(prefix="ser2net_ui_")
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    os.makedirs(os.path.join(tmp, "traces"))  # rotated files of the trace (Trace panel)
    for n in (1, 2, 49):
        with open(os.path.join(tmp, "traces", f"{TRACE_ID}-{TRACE_NAME}.{n}"), "wb") as fh:
            fh.write(b"x" * 1500 * n)
    if backup:  # an older version in backups/, for the restore preview
        os.makedirs(os.path.join(tmp, "backups"))
        with open(os.path.join(tmp, "backups", "config-20260101T000000Z.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(dict(cfg, admin_ui=dict(cfg["admin_ui"], require_login=False),
                           users=cfg["users"][:1]), fh)
    # output to a file, never an unread pipe (a full pipe blocks the server)
    log = open(os.path.join(tmp, "server.log"), "wb")
    proc = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--config", path],
                            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    return proc, tmp, log


def skip(why):
    if REQUIRED:
        raise AssertionError(f"SER2NET_UI_TESTS=1 but {why}")
    print(f"SKIP browser layout test: {why} "
          "(pip install playwright && python -m playwright install chromium)")


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return skip("Playwright is not installed")

    key = "ui-layout-key"
    ui_a, ui_b, raw_a = free_port(), free_port(), free_port()
    users = [{"username": USER, "password_hash": hash_password(PASSWORD), "role": "admin",
              "pwd_version": 1}]
    node_b = {"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_b}, "users": users,
              "cluster": {"enabled": True, "key": key, "discovery_port": free_port(),
                          "advertise_ip": "127.0.0.1"},
              "mappings": demo_mappings("B-", free_port()) + kitchen_sinks()}
    # a second (long-named) account: the Users table's row buttons + the set-password form
    others = [{"username": "night-shift.operator.building-b.east", "role": "operator",
               "password_hash": hash_password(PASSWORD), "pwd_version": 1}]
    node_a = {"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_a}, "users": users + others,
              "cluster": {"enabled": True, "key": key, "discovery_port": free_port(),
                          "peers": [f"127.0.0.1:{ui_b}"]},
              "mappings": demo_mappings("", raw_a) + kitchen_sinks()}
    nodes = [start_node(node_a, backup=True), start_node(node_b)]
    clients, problems, browser_errors = [], [], []
    base = f"http://127.0.0.1:{ui_a}"
    try:
        assert wait_port(ui_a) and wait_port(ui_b) and wait_port(raw_a), "nodes did not start"
        for _ in range(3):  # peer chips in the Clients cell
            clients.append(socket.create_connection(("127.0.0.1", raw_a), timeout=5))

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # browser binary not downloaded
                return skip(f"Playwright's Chromium is not available ({type(e).__name__})")
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", lambda m: m.type == "error" and browser_errors.append(m.text))
            page.on("pageerror", lambda e: browser_errors.append(str(e)))

            page.goto(f"{base}/login")
            page.fill("input[name=username]", USER)
            page.fill("input[name=password]", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{base}/")

            def shot(name):
                if ARTIFACTS:
                    os.makedirs(ARTIFACTS, exist_ok=True)
                    page.screenshot(path=os.path.join(ARTIFACTS, f"{name}.png"), full_page=True)

            def check(where):
                bad = page.evaluate(CHECK_JS)
                problems.extend(f"{where}: {b}" for b in bad)
                print(f"{where}: {'OK' if not bad else f'{len(bad)} problem(s)'}")
                shot(where.replace(" ", "_").replace("@", ""))

            def dashboard():
                page.goto(f"{base}/")
                page.wait_for_selector("#mappings table")
                page.wait_for_selector("#mappings .err-small")      # long serial errors shown
                page.wait_for_selector("#mappings .peer")           # client chips shown
                page.wait_for_function(
                    "() => (document.querySelector('#cluster') || {}).textContent?.includes('B-short')",
                    timeout=20000)

            def open_view(button):
                row = page.locator("#mappings tr", has_text=LONG_NAME).first
                row.get_by_role("button", name=button, exact=True).click()
                page.wait_for_selector("#form-panel .form-head")
                page.wait_for_timeout(300)

            for lang in ("en", "tr"):
                page.goto(f"{base}/lang/{lang}?next=/")
                for w in WIDTHS:
                    page.set_viewport_size({"width": w, "height": 900})
                    dashboard()
                    check(f"{lang} dashboard @{w}")
                for w in (1024, 390):
                    page.set_viewport_size({"width": w, "height": 900})
                    for view in ("Log", "Monitor", "Edit", "Trace"):
                        dashboard()
                        open_view(TR[view] if lang == "tr" else view)
                        check(f"{lang} {view.lower()} view @{w}")
                    page.goto(f"{base}/settings")
                    page.wait_for_selector("section.panel")
                    check(f"{lang} settings @{w}")
                    page.goto(f"{base}/settings?restore=config-20260101T000000Z.json#backups")
                    page.wait_for_selector("#backups h3")
                    check(f"{lang} restore preview @{w}")

            # the cluster panel polls every 5 s: not from a hidden tab, and at once when
            # the tab is shown again
            page.set_viewport_size({"width": 1280, "height": 900})
            dashboard()
            polls = []
            page.on("request", lambda r: "/api/cluster/status" in r.url and polls.append(r.url))
            set_hidden = ("h => { Object.defineProperty(document, 'hidden', {configurable: true, "
                          "get: () => h}); document.dispatchEvent(new Event('visibilitychange')); }")
            page.evaluate(set_hidden, True)
            page.wait_for_timeout(6500)
            if polls:
                problems.append(f"a hidden tab refreshed the cluster panel {len(polls)}x")
            page.evaluate(set_hidden, False)
            page.wait_for_timeout(1500)
            if not polls:
                problems.append("showing the tab again did not refresh the cluster panel")
            print("cluster panel: no refresh while the tab is hidden, one when shown")

            # UDP offers raw only: picking UDP selects raw, the other protocols can't be
            # picked until the mode is TCP again
            dashboard()
            open_view(TR["Edit"])  # the language is Turkish here (the loop above)
            selects = "#form-panel select[name=network_%s]"
            protocols = ("() => { const s = document.querySelector('#form-panel "
                         "select[name=network_protocol]'); return [s.value, [...s.options]"
                         ".filter(o => o.disabled).map(o => o.value)]; }")
            page.select_option(selects % "protocol", "telnet")
            page.select_option(selects % "mode", "udp")
            if page.evaluate(protocols) != ["raw", ["telnet", "rfc2217", "modbus"]]:
                problems.append(f"UDP mode: protocol select is {page.evaluate(protocols)}")
            page.select_option(selects % "mode", "server")
            if page.evaluate(protocols) != ["raw", []]:
                problems.append(f"server mode: protocol select is {page.evaluate(protocols)}")
            print("mapping form: UDP leaves only raw to pick, TCP all protocols")

            # the form data the browser posts for each kitchen-sink mapping is what
            # tests/_formsubmit.py computes from the same page, and saving the form
            # unchanged keeps every setting — on this node and on the peer
            page.goto(f"{base}/lang/en?next=/")
            dashboard()
            token = page.evaluate("() => document.querySelector('meta[name=csrf-token]').content")
            form_data = "() => [...new FormData(document.querySelector('#form-panel form'))]"
            for where, tmp, url_of in (
                    ("this node", nodes[0][1], lambda mid: f"/api/mappings/{mid}/form"),
                    ("the peer", nodes[1][1], lambda mid: "/api/cluster/peer-form?" +
                     urllib.parse.urlencode({"host": "127.0.0.1", "port": ui_b, "mid": mid}))):
                with open(os.path.join(tmp, "config.json"), encoding="utf-8") as fh:
                    before = form_cases.relevant(json.load(fh)["mappings"])
                for sink in form_cases.mappings():
                    url = url_of(sink["id"])
                    sent = page.request.get(base + url, headers={"X-CSRF-Token": token}).text()
                    page.evaluate("u => htmx.ajax('GET', u, {target: '#form-panel', "
                                  "swap: 'innerHTML'})", url)
                    page.wait_for_selector(f"#form-panel input[name=name][value='{sink['name']}']")
                    page.wait_for_timeout(200)
                    real = [tuple(x) for x in page.evaluate(form_data)]
                    if real != browser_post(sent):
                        problems.append(f"{sink['name']} on {where}: the browser posts {real}, "
                                        f"tests/_formsubmit.py says {browser_post(sent)}")
                    refused = page.evaluate(
                        "() => [...document.querySelectorAll('#form-panel form :invalid')]"
                        ".filter(e => e.name).map(e => e.name)")
                    if refused:  # htmx would not submit it
                        problems.append(f"{sink['name']} on {where}: the browser refuses "
                                        f"{refused}")
                        continue
                    with page.expect_response(lambda r: r.request.method == "POST"
                                              and "save" in r.url) as saved:
                        page.click("#form-panel button[type=submit]")
                    if saved.value.status != 200:
                        problems.append(f"{sink['name']} on {where}: saving it unchanged "
                                        f"answered {saved.value.status}")
                with open(os.path.join(tmp, "config.json"), encoding="utf-8") as fh:
                    after = form_cases.relevant(json.load(fh)["mappings"])
                problems.extend(f"{a['name']} on {where} changed by an unchanged save: {a} -> {b}"
                                for a, b in zip(before, after, strict=True) if a != b)
            print("mapping form: the browser posts what tests/_formsubmit.py computes; an "
                  "unchanged save keeps every setting, on this node and on the peer")
            browser.close()

        assert not problems, "layout problems:\n  " + "\n  ".join(problems)
        assert not browser_errors, "browser errors:\n  " + "\n  ".join(browser_errors)
        print("\nPASS: long names, paths and errors stay inside their panels at "
              f"{', '.join(map(str, WIDTHS))} px, in English and Turkish")
    finally:
        for c in clients:
            c.close()
        for proc, tmp, log in nodes:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
