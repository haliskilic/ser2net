"""Web panel layout with long names: nothing may stick out of its panel (issue #26).

Long mapping names, /dev/serial/by-id/... paths, remote host names and error
messages used to make the mappings and cluster tables wider than their panel: the
row buttons (Start/Stop/Restart/Edit/Log/Monitor/Copy/Delete) ended up outside it
and the whole page scrolled sideways. This drives a real browser (Playwright +
Chromium) against two real nodes — A shows B through a manual cluster peer — whose
mappings use such values, at desktop, half-screen, tablet and phone widths, and
checks the dashboard, the log / console / edit views, the settings page (users with row
buttons, a backup's restore preview) — in English and again in Turkish, whose texts are
longer:

  * the page never scrolls sideways and no table needs horizontal scrolling
    (wide windows: the table fits; narrow ones: its rows become cards);
  * every element stays inside its panel; row buttons stay inside their cell
    and never overlap; no cell's content overflows the cell;
  * the browser reports no errors (e.g. a Content-Security-Policy violation);
  * the filter rows (the dashboard's toolbar, the audit log's filters) take one line in
    wide windows, and their search boxes are drawn like the other fields;
  * the self-refreshing panels (mappings, cluster, ports) aren't refreshed while the tab
    is hidden (a cluster refresh makes the node ask every peer) and are refreshed as soon
    as the tab is shown again;
  * the mappings table (live_checks): the clients' connection times count on in the
    browser; an answer with only new counters updates those cells, the rows and the focus
    stay; the table isn't replaced under a pressed pointer or a text selection; the log
    panel keeps its lines while scrolled down;
  * what app.js does around the dashboard's requests (feedback_checks): the keyboard
    focus survives the table's refreshes; a panel opened from far down the page comes
    into view and takes the focus, which goes back to its button on Cancel; unsaved
    form changes are not thrown away without asking; a save says so and the focus goes
    back to the row; Shift+Tab leaves the console; a failed action, a lost connection
    and an ended session are shown (refreshing panels marked "not up to date") and
    cleared once requests work again; a bulk action acts on the ticked rows shown only
    ("Select all shown" ticks those), asks first with the counts and clears the
    selection; a filter that matches nothing says so, with a button that clears it.

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

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)

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


# The filter rows (the dashboard's toolbar, the audit log's filters): how many lines their
# visible fields take, and whether a search box is drawn by the browser (white) rather than
# like the other fields.
FILTER_ROW_JS = r"""sel => {
  const row = document.querySelector(sel);
  if (!row) return null;
  const tops = new Set([...row.children].filter(el => el.getBoundingClientRect().width > 0)
                       .map(el => Math.round(el.getBoundingClientRect().top / 8)));
  const search = row.querySelector('input[type=search]');
  return {lines: tops.size, searchBg: getComputedStyle(search).backgroundColor,
          pageBg: getComputedStyle(document.body).backgroundColor};
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
        {"name": prefix + LONG_NAME, "enabled": True,
         "serial": {"port": BY_ID, "baudrate": 115200, "flowcontrol": "rtscts"},
         "network": {"mode": "server", "protocol": "rfc2217", "bind_ip": "127.0.0.1",
                     "port": free_port(), "read_only": True}},
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


def start_node(cfg, backup=False):
    tmp = tempfile.mkdtemp(prefix="ser2net_ui_")
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
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


def live_checks(page, bad, active, row_button, press):
    """How the mappings table and the log panel refresh (app.js 1d): only the counters
    when only they changed; not under a pressed pointer or a text selection; the log not
    while it is scrolled down."""
    table = "document.querySelector('#mappings table')"
    mark = f"window.__table = {table}"
    same = f"{table} === window.__table"
    forget = "document.getElementById('mappings').removeAttribute('data-hash')"

    # only the counters changed: htmx swaps their spans in, the rows (and the focus) stay
    log_id = row_button("short", "log")
    span = page.evaluate("document.querySelector('#mappings [id$=\"-bytes\"]').id")
    page.focus(f"#{log_id}")
    page.evaluate(mark)
    page.route("**/api/status*", lambda r: r.fulfill(
        status=200, headers={"Content-Type": "text/html; charset=utf-8", "HX-Reswap": "none",
                             "X-Content-Hash": "patched.counters"},
        body=f'<span id="{span}" hx-swap-oob="true">9.9 MB / 1 B</span>'))
    page.wait_for_timeout(2600)
    page.unroute("**/api/status*")
    if page.evaluate(f"document.getElementById('{span}').textContent") != "9.9 MB / 1 B":
        bad("a counters-only answer did not update the In / Out cell")
    if not page.evaluate(same) or active() != log_id:
        bad(f"a counters-only answer replaced the table or took the focus: {active()!r}")

    # a pressed pointer or a text selection in the table: it is not replaced under them
    for what, start, end in (
            ("a pointer pressed", "document.querySelector('#mappings td').dispatchEvent("
             "new PointerEvent('pointerdown', {bubbles: true}))",
             "document.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}))"),
            ("a text selection", "(() => { const r = document.createRange(); "
             "r.selectNodeContents(document.querySelector('#mappings td.col-name span'));"
             " getSelection().removeAllRanges(); getSelection().addRange(r); })()",
             "getSelection().removeAllRanges()")):
        page.evaluate(mark)
        page.evaluate(start)
        page.evaluate(forget)
        page.wait_for_timeout(2600)
        if not page.evaluate(same):
            bad(f"the table was replaced under {what}")
        page.evaluate(end)
        page.wait_for_timeout(2600)
        if page.evaluate(same):
            bad(f"the table was not refreshed after {what} ended")

    # the log panel keeps its lines while scrolled down to older ones
    press(row_button("short", "log"))
    page.wait_for_selector("#form-panel .logbox")
    box = "document.querySelector('#form-panel .logbox')"
    page.evaluate(f"(() => {{ const b = {box}; b.style.maxHeight = '30px'; b.scrollTop = 20;"
                  f" window.__log = b; document.querySelector('#form-panel [data-hash]')"
                  f".setAttribute('data-hash', 'old'); }})()")
    page.wait_for_timeout(5600)
    if page.evaluate(f"{box} !== window.__log"):
        bad("the log panel was refreshed while scrolled down")
    page.evaluate(f"{box}.scrollTop = 0")
    page.wait_for_timeout(5600)
    if page.evaluate(f"{box} === window.__log"):
        bad("the log panel was not refreshed once scrolled back to the newest lines")
    page.click("#log-close")


def feedback_checks(browser, base, problems):
    """The dashboard's behaviour around its requests (app.js), in a browser context of its
    own. Failed requests are part of it: the browser's "failed to load" lines and htmx's
    error lines are expected here; a script error or a CSP violation is not."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 600})
    page = ctx.new_page()
    errors, dialogs, answer = [], [], {"accept": False}
    # (htmx logs the event name of a request that got no answer)
    expected = ("Failed to load resource", "Response Status Error Code", "net::ERR_FAILED",
                "htmx:afterRequest", "htmx:sendError")
    page.on("console", lambda m: m.type == "error" and not m.text.startswith(expected)
            and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))

    messages = []

    def on_dialog(d):
        dialogs.append(d.type)
        messages.append(d.message)
        d.accept() if answer["accept"] else d.dismiss()
    page.on("dialog", on_dialog)

    def bad(what):
        problems.append(f"feedback: {what}")

    def active():
        return page.evaluate("document.activeElement && (document.activeElement.id || "
                             "document.activeElement.className)")

    def row_button(name, suffix):
        row = page.locator("#mappings tr", has_text=name).first
        return row.locator(f"button[id$='-{suffix}']").first.get_attribute("id")

    def press(button_id):
        """Click a row button. Not with the mouse: the table is replaced every 2 s while
        clients are connected, and a slow machine may not hit a button in time."""
        page.evaluate("id => document.getElementById(id).click()", button_id)

    def sign_in(p):
        p.goto(f"{base}/login")
        p.fill("input[name=username]", USER)
        p.fill("input[name=password]", PASSWORD)
        p.click("button[type=submit]")
        p.wait_for_url(f"{base}/")

    def until(js, ms=8000):
        try:
            page.wait_for_function(js, timeout=ms)
            return True
        except Exception:
            return False

    sign_in(page)
    page.wait_for_selector("#mappings table")
    page.evaluate("window.__swaps = 0; document.addEventListener('htmx:afterSwap', "
                  "e => { if (e.target.id === 'mappings') window.__swaps++; })")

    # the clients' connection times count on in the browser, with no answer from the server
    dur = "document.querySelector('#mappings .peer .dur').textContent"
    page.route("**/api/status*", lambda r: r.fulfill(status=204))
    first = page.evaluate(dur)
    page.wait_for_timeout(2600)
    if page.evaluate(dur) == first:
        bad(f"a client's connection time did not count on in the browser: {first!r}")
    page.unroute("**/api/status*")

    # the focus survives a refresh of the table (one that changed: its hash forgotten)
    log_id = row_button("short", "log")
    page.focus(f"#{log_id}")
    page.evaluate("window.__swaps = 0; document.getElementById('mappings').removeAttribute('data-hash')")
    page.wait_for_timeout(2600)
    if page.evaluate("window.__swaps") < 1:
        bad("the table was not refreshed after its hash was forgotten")
    if active() != log_id:
        bad(f"a table refresh took the focus from the row's Log button: {active()!r}")

    live_checks(page, bad, active, row_button, press)

    # a panel opened from far down comes into view and takes the focus; Cancel gives it back
    edit_id = row_button(LONG_NAME, "edit")
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    press(edit_id)
    page.wait_for_selector("#form-panel form")
    page.wait_for_timeout(400)
    top = page.evaluate("document.getElementById('form-panel').getBoundingClientRect().top")
    if not 0 <= top < 600:
        bad(f"the opened form panel is not in view (top {top})")
    if active() != "panel-title":
        bad(f"the opened panel did not take the focus: {active()!r}")
    # unsaved changes: another panel or Cancel asks first
    page.fill("#form-panel input[name=name]", "changed")
    press(row_button(LONG_NAME, "log"))
    page.wait_for_timeout(400)
    if dialogs != ["confirm"] or not page.query_selector("#form-panel form"):
        bad(f"opening the log over a changed form did not ask, or replaced it: {dialogs}")
    answer["accept"] = True
    page.click("#form-cancel")
    page.wait_for_timeout(300)
    if dialogs != ["confirm", "confirm"] or page.query_selector("#form-panel form"):
        bad(f"Cancel on a changed form did not ask, or kept it: {dialogs}")
    if active() != edit_id:
        bad(f"Cancel did not give the focus back to Edit: {active()!r}")

    # Shift+Tab leaves an operator's console (Tab goes to the device)
    press(row_button("short", "monitor"))
    page.wait_for_selector("#term .xterm")
    page.wait_for_timeout(500)
    if active() != "xterm-helper-textarea":
        bad(f"the console in view did not take the focus: {active()!r}")
    page.keyboard.press("Shift+Tab")
    if active() == "xterm-helper-textarea":
        bad("Shift+Tab did not leave the console")
    page.click("#console-close")

    # a save says so, and the focus goes back to the row
    short_edit = row_button("short", "edit")
    press(short_edit)
    page.wait_for_selector("#form-panel form")
    page.click("#form-panel button[type=submit]")
    if not until("() => document.getElementById('ui-status').textContent.includes('“short” saved.')"):
        bad("a save was not confirmed")
    page.wait_for_timeout(300)
    if active() != short_edit:
        bad(f"after a save the focus is not on the row's Edit: {active()!r}")

    # a failed action is shown (a copy can't run while its original holds the serial port)
    press(row_button("short", "copy"))
    if not until("() => document.getElementById('ui-status').textContent.includes('Copied as')"):
        bad("Copy was not confirmed")
    page.wait_for_selector("#mappings tr:has-text('short (copy)')")
    press(row_button("short (copy)", "run"))
    if not until("() => document.getElementById('ui-alert').textContent.trim().length > 0"):
        bad("a failed Start showed nothing")
    press(row_button("short (copy)", "delete"))   # hx-confirm: accepted
    page.wait_for_function("() => !document.querySelector('#mappings').textContent.includes('short (copy)')")
    page.evaluate("document.querySelectorAll('#ui-alert .dismiss').forEach(b => b.click())")

    # no answer at all: "can't be reached", the table marked; gone with the next answer
    page.route("**/api/status*", lambda r: r.abort())
    if not until("() => document.querySelector('#mappings.stale') && "
                 "document.getElementById('ui-alert').textContent.includes('can’t be reached')"):
        bad("a lost connection was not shown")
    page.unroute("**/api/status*")
    if not until("() => !document.querySelector('#mappings.stale') && "
                 "!document.getElementById('ui-alert').textContent.trim()"):
        bad("the lost-connection note stayed after the server answered again")

    # an ended session: a lasting note with a sign-in link; signing in (another tab) clears it
    ctx.clear_cookies(name="ser2net_session")
    if not until("() => document.querySelector('#mappings.stale') && "
                 "document.querySelector('#ui-alert a[href=\"/login\"]')"):
        bad("an ended session was not shown with a sign-in link")
    other = ctx.new_page()
    sign_in(other)
    other.close()
    if not until("() => !document.querySelector('#mappings.stale') && "
                 "!document.getElementById('ui-alert').textContent.trim()"):
        bad("the session note stayed after signing in again")

    # bulk actions act on the ticked rows shown, ask first, and clear the selection after;
    # a filter that matches nothing says so
    js_click = "sel => document.querySelector(sel).click()"
    count = "document.querySelector('[data-bulk-count]').textContent"
    page.evaluate("document.querySelector('#mappings tr[data-mid] .row-select').click()")  # LONG_NAME
    page.fill("[data-filter-text]", "short")
    if "1 more hidden" not in page.evaluate(count):
        bad(f"a ticked row the filter hides is not counted as left out: {page.evaluate(count)!r}")
    page.evaluate(js_click, "[data-select-shown]")
    short = page.evaluate("document.querySelector('#mappings tr[data-mid]:not([hidden])').getAttribute('data-mid')")
    if not page.evaluate(f"document.getElementById('m-{short}-select').checked"):
        bad("“Select all shown” did not tick the row shown")
    posts = []
    page.on("request", lambda r: r.url.endswith("/api/mappings/bulk") and posts.append(r.post_data))
    answer["accept"], del_messages = False, len(messages)
    page.evaluate(js_click, "[data-bulk-action=restart]")
    page.wait_for_timeout(300)
    asked = messages[del_messages:]
    n = int(page.evaluate(f"document.querySelector('tr[data-mid=\"{short}\"]').getAttribute('data-clients')"))
    want = ("Restart 1 mapping", f"{n} connected client" if n else "?",
            "1 selected mapping(s) hidden")
    if not asked or not all(t in asked[0] for t in want):
        bad(f"the bulk restart did not ask with the counts ({n} clients): {asked}")
    if posts:
        bad("a dismissed bulk restart was sent")
    answer["accept"] = True
    page.evaluate(js_click, "[data-bulk-action=restart]")
    if not until("() => document.querySelector('[data-bulk]').hidden"):
        bad("the selection was not cleared after the bulk action")
    if len(posts) != 1 or f"ids={short}" not in (posts[0] or "") or posts[0].count("ids=") != 1:
        bad(f"the bulk action did not send just the row shown: {posts}")
    page.fill("[data-filter-text]", "no-such-mapping-zzz")
    if not page.query_selector("#mappings tr.no-match button[data-filter-clear]"):
        bad("a filter matching nothing does not say so in the table")
    page.click("#mappings tr.no-match button[data-filter-clear]")
    if page.query_selector("#mappings tr.no-match") or page.input_value("[data-filter-text]"):
        bad("“Show all” did not clear the filter")
    problems.extend(f"feedback: browser error: {e}" for e in errors)
    ctx.close()


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
              "mappings": demo_mappings("B-", free_port())}
    # a second (long-named) account: the Users table's row buttons + the set-password form
    others = [{"username": "night-shift.operator.building-b.east", "role": "operator",
               "password_hash": hash_password(PASSWORD), "pwd_version": 1}]
    node_a = {"admin_ui": {"bind_ip": "127.0.0.1", "port": ui_a}, "users": users + others,
              "cluster": {"enabled": True, "key": key, "discovery_port": free_port(),
                          "peers": [f"127.0.0.1:{ui_b}"]},
              "mappings": demo_mappings("", raw_a)}
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

            def filter_row(where, sel):
                """At these widths the row fits on one line: each select used to take a
                line of its own (and the search box was drawn white in the dark theme)."""
                r = page.evaluate(FILTER_ROW_JS, sel)
                if r is None:
                    problems.append(f"{where}: no {sel}")
                elif r["lines"] != 1:
                    problems.append(f"{where}: {sel} takes {r['lines']} lines")
                elif r["searchBg"] != r["pageBg"]:
                    problems.append(f"{where}: the search box is drawn {r['searchBg']}, "
                                    f"the other fields {r['pageBg']}")

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
                    if w >= 1024:
                        filter_row(f"{lang} dashboard @{w}", "[data-mapping-tools]")
                for w in (1024, 390):
                    page.set_viewport_size({"width": w, "height": 900})
                    for view in ("Log", "Monitor", "Edit"):
                        dashboard()
                        open_view(TR[view] if lang == "tr" else view)
                        check(f"{lang} {view.lower()} view @{w}")
                    page.goto(f"{base}/settings")
                    page.wait_for_selector("section.panel")
                    check(f"{lang} settings @{w}")
                    page.goto(f"{base}/settings?restore=config-20260101T000000Z.json#backups")
                    page.wait_for_selector("#backups h3")
                    check(f"{lang} restore preview @{w}")
                    page.goto(f"{base}/settings/audit")
                    page.wait_for_selector("form.filters")
                    check(f"{lang} audit log @{w}")
                    if w >= 1024:
                        filter_row(f"{lang} audit log @{w}", "form.filters")

            # the cluster panel polls every 5 s: not from a hidden tab, and at once when
            # the tab is shown again
            page.set_viewport_size({"width": 1280, "height": 900})
            dashboard()
            polls = {"/api/cluster/status": [], "/api/status": [], "/api/ports/table": []}
            page.on("request", lambda r: [v.append(r.url) for k, v in polls.items()
                                          if r.url.split("?")[0].endswith(k)])
            set_hidden = ("h => { Object.defineProperty(document, 'hidden', {configurable: true, "
                          "get: () => h}); document.dispatchEvent(new Event('visibilitychange')); }")
            page.evaluate(set_hidden, True)
            page.wait_for_timeout(6500)
            for path, seen in polls.items():
                if seen:
                    problems.append(f"a hidden tab refreshed {path} {len(seen)}x")
            page.evaluate(set_hidden, False)
            page.wait_for_timeout(1500)
            for path, seen in polls.items():
                if not seen:
                    problems.append(f"showing the tab again did not refresh {path}")
            print("self-refreshing panels: no refresh while the tab is hidden, one when shown")
            n = len(problems)
            feedback_checks(browser, base, problems)
            print(f"dashboard feedback, focus, unsaved changes, console: "
                  f"{'OK' if len(problems) == n else f'{len(problems) - n} problem(s)'}")
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
