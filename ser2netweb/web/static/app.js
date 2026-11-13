// ser2netweb admin UI — minimal vanilla JS (no framework, CSP-friendly).

// UI texts in the page's language (base.html puts them on <body data-i18n>)
var I18N = (function () {
  try { return JSON.parse(document.body.getAttribute("data-i18n") || "{}"); } catch (e) { return {}; }
})();
function tr(key, fallback) {
  var s = I18N[key] || fallback;
  for (var i = 2; i < arguments.length; i++) s = s.replace("{" + (i - 2) + "}", arguments[i]);
  return s;
}
// the path prefix the page came under ("" at "/"; a public URL's path, base.html)
function basePath() {
  var m = document.querySelector('meta[name="ser2net-base"]');
  return m ? m.getAttribute("content") : "";
}

// a text with a count n ({0}): base.html gives [the form for 1, the form for other
// numbers] (the English rule, as i18n.ngettext; a language that has one form after a
// number has the same text twice); `one` / `other`: the English forms, as fallbacks
function trn(key, one, other, n) {
  var forms = I18N[key], s = Array.isArray(forms) ? forms[n === 1 ? 0 : 1] : n === 1 ? one : other;
  for (var i = 3; i < arguments.length; i++) s = s.replace("{" + (i - 3) + "}", arguments[i]);
  return s;
}

// 0) Theme: toggle the light/dark theme (persisted in localStorage) when a
//    [data-theme-toggle] control is clicked; theme.js applies the saved one, in <head>.
document.addEventListener("click", function (e) {
  if (!e.target.closest("[data-theme-toggle]")) return;
  var toLight = document.documentElement.getAttribute("data-theme") !== "light";
  if (toLight) document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.removeAttribute("data-theme");
  try { localStorage.setItem("ser2net-theme", toLight ? "light" : "dark"); } catch (e) {}
});

// 0b) htmx sets detail.elt to the element each event is dispatched on — afterRequest goes
//     to an ancestor still in the page when the element that asked was replaced
//     meanwhile (a row button while its table refreshed). The element that asked is
//     kept here, on the request's detail that all its events share.
document.addEventListener("htmx:beforeRequest", function (e) {
  e.detail.askedBy = e.detail.elt;
});

// 1) Send the CSRF token on every htmx request (double-submit cookie pattern). A new
//    one (a sign-in in another tab renews it) comes with the next answer (X-CSRF-Token):
//    the page and its forms take it, so what was left open there can still be saved.
document.addEventListener("htmx:configRequest", function (e) {
  var m = document.querySelector('meta[name="csrf-token"]');
  if (m) e.detail.headers["X-CSRF-Token"] = m.getAttribute("content");
});
document.addEventListener("htmx:afterRequest", function (e) {
  var xhr = e.detail.xhr, t = xhr && xhr.status ? xhr.getResponseHeader("X-CSRF-Token") : null;
  if (!t) return;
  var m = document.querySelector('meta[name="csrf-token"]');
  if (m) m.setAttribute("content", t);
  document.querySelectorAll('input[name="_csrf"]').forEach(function (i) { i.value = t; });
});

// 1a) Detected-ports panel: "show / hide the ports without hardware" sticks across the
//     5-second refreshes and the Refresh button (the panel carries the choice; it is the
//     target of both requests, the button their element).
document.addEventListener("htmx:configRequest", function (e) {
  var el = e.detail.target || e.detail.elt;
  if (el && el.id === "ports-table" && el.getAttribute("data-show-all") === "1")
    e.detail.parameters["all"] = "1";
});

// 1a') Mapping form: ↻ next to Device lists the serial ports again (an adapter plugged in
//      after the form was opened), keeping what is chosen and the rest of the form.
document.addEventListener("click", function (e) {
  var b = e.target.closest ? e.target.closest("[data-port-rescan]") : null;
  if (!b || !window.htmx) return;
  var pick = b.closest(".port-pick"), prefix = b.getAttribute("data-port-rescan");
  var field = pick ? pick.querySelector('[name="' + prefix + 'port"]') : null;
  if (!pick) return;
  htmx.ajax("GET", basePath() + "/api/ports/select?prefix=" + encodeURIComponent(prefix) +
            "&selected=" + encodeURIComponent(field ? field.value : ""),
            {target: pick, swap: "outerHTML"});
});
document.addEventListener("click", function (e) {
  var b = e.target.closest ? e.target.closest("[data-ports-all]") : null;
  var panel = document.getElementById("ports-table");
  if (!b || !panel) return;
  panel.setAttribute("data-show-all", b.getAttribute("data-ports-all"));
  if (window.htmx) htmx.trigger(panel, "refreshPorts");
});

// 1b) Surface server-side validation errors. htmx does NOT swap non-2xx responses
//     by default, so a 400 from a form post (e.g. an invalid mapping) would silently
//     do nothing — the user never sees why Save failed. A 400/422 that is a
//     re-rendered form (HTML, with its error banner) swaps into its panel. Any other
//     answer — a plain-text 400 (Copy, bulk actions), 401, 403, 5xx — stays an
//     error, and the feedback area (7) shows it.
document.addEventListener("htmx:beforeSwap", function (e) {
  var xhr = e.detail.xhr, status = xhr ? xhr.status : 0;
  if ((status === 400 || status === 422) &&
      /^text\/html/i.test(xhr.getResponseHeader("Content-Type") || "")) {
    e.detail.shouldSwap = true;
    e.detail.isError = false;
  }
});

// 1c) A panel that refreshes itself ("every Ns": the mappings table, the fleet view, the
//     ports, a mapping's log) is swapped only when its content changed: it sends the hash
//     of what it shows, and the server answers 204 otherwise (no flicker; the keyboard
//     focus, a text selection and the scroll position stay). The hash comes back in
//     X-Content-Hash, or (the log, which each refresh replaces) in the panel's data-hash.
function polls(el) {
  var t = el && el.getAttribute ? el.getAttribute("hx-trigger") : "";
  return !!t && /\bevery\b/.test(t);
}
document.addEventListener("htmx:configRequest", function (e) {
  var el = e.detail.elt;
  if (polls(el) && el.getAttribute("data-hash"))
    e.detail.headers["X-Content-Hash"] = el.getAttribute("data-hash");
});
document.addEventListener("htmx:afterRequest", function (e) {
  var el = e.detail.askedBy, xhr = e.detail.xhr;
  if (polls(el) && xhr && xhr.status === 200 && !xhr.keptOld) {   // (keptOld: 1d)
    var h = xhr.getResponseHeader("X-Content-Hash");
    if (h) el.setAttribute("data-hash", h);
  }
});

// 1d) A panel that refreshes itself does not refresh in a background tab (it catches up
//     as soon as the tab is shown again), and keeps what it shows while a pointer is
//     pressed in it or text in it is selected: a click whose press and release straddled
//     a refresh was lost, and so was the selection. The log panel also keeps its lines
//     while it is scrolled down to older ones (the newest are at the top). A refresh an
//     action asks for (refreshMappings) is shown all the same. A refresh not shown keeps
//     the panel's old hash, so the next one brings the news.
//     (For 15 seconds at most: a selection left behind, or a release the page never saw,
//     must not keep a panel out of date.)
var pressed = null, heldAt = 0;
document.addEventListener("pointerdown", function (e) { pressed = e.target; heldAt = Date.now(); }, true);
document.addEventListener("pointerup", function () { pressed = null; }, true);
document.addEventListener("pointercancel", function () { pressed = null; }, true);
document.addEventListener("selectionchange", function () { heldAt = Date.now(); });
function busyIn(el) {
  if (Date.now() - heldAt > 15000) return false;
  if (pressed && el.contains(pressed)) return true;
  var sel = window.getSelection ? window.getSelection() : null;
  return !!(sel && !sel.isCollapsed && sel.rangeCount &&
            el.contains(sel.getRangeAt(0).commonAncestorContainer));
}
document.addEventListener("htmx:beforeRequest", function (e) {
  if (document.hidden && polls(e.detail.elt)) e.preventDefault();
});
document.addEventListener("visibilitychange", function () {
  if (document.hidden || !window.htmx) return;
  document.querySelectorAll("[hx-trigger][hx-get]").forEach(function (el) {
    if (polls(el))
      htmx.ajax("GET", el.getAttribute("hx-get"), { source: el,
        target: el.getAttribute("hx-target") || el, swap: el.getAttribute("hx-swap") || "innerHTML" });
  });
});
document.addEventListener("htmx:beforeSwap", function (e) {
  var d = e.detail, el = d.askedBy, panel = d.target, xhr = d.xhr;
  var asked = d.requestConfig && d.requestConfig.triggeringEvent;   // none: its own timer
  if (!xhr || xhr.status !== 200 || !panel || !polls(el) || asked) return;
  var log = panel.id === "form-panel" ? panel.querySelector(".logbox") : null;
  if (busyIn(panel) || (log && log.scrollTop > 0)) {
    d.shouldSwap = false;
    xhr.keptOld = true;
  }
});

// 1e) Connection times in the mappings table count on in the browser, from when each
//     client connected (data-since, by the server's clock, which each answer of the table
//     gives in X-Server-Time): the table is not sent again every second for them. As the
//     duration filter in ser2netweb/web/routes/common.py.
var serverSkew = 0;
document.addEventListener("htmx:afterRequest", function (e) {
  var xhr = e.detail.xhr, t = xhr && xhr.status ? xhr.getResponseHeader("X-Server-Time") : null;
  if (t && isFinite(+t)) serverSkew = +t - Date.now() / 1000;
});
function fmtDuration(secs) {   // in the page's language, as the server's duration filter
  secs = Math.max(0, Math.floor(secs));
  var h = Math.floor(secs / 3600), m = Math.floor(secs % 3600 / 60), s = secs % 60;
  function two(n) { return (n < 10 ? "0" : "") + n; }
  return h ? tr("dur_hm", "{0}h {1}m", h, two(m))
           : m ? tr("dur_ms", "{0}m {1}s", m, two(s)) : tr("dur_s", "{0}s", s);
}
function tickDurations() {
  if (document.hidden) return;
  var now = Date.now() / 1000 + serverSkew;
  document.querySelectorAll("#mappings [data-since]").forEach(function (chip) {
    var dur = chip.querySelector(".dur"), since = +chip.getAttribute("data-since");
    if (!dur || !isFinite(since)) return;
    var text = fmtDuration(now - since);
    if (dur.textContent === text) return;
    dur.textContent = text;
    chip.title = tr("connected", "connected {0}", text) +
                 (chip.classList.contains("peer-prio") ? " · " + tr("priority", "priority") : "");
  });
}
setInterval(tickDurations, 1000);
document.addEventListener("htmx:afterSwap", function (e) {
  if (e.target && e.target.id === "mappings") tickDurations();
});

// 2) "Custom…" reveal: a <select data-custom="fieldName"> toggles the sibling
//    <input name="fieldName"> when its value is "custom".
function syncCustomInputs(root) {
  (root || document).querySelectorAll("select[data-custom]").forEach(function (sel) {
    var target = sel.getAttribute("data-custom");
    var input = sel.parentNode.querySelector('[name="' + target + '"]');
    if (!input) return;
    var show = sel.value === "custom";
    input.hidden = !show;
    input.disabled = !show;
    if (show && !input.value) input.focus();
  });
}

// a "Custom…" input stays disabled while its select has another value: a block shown by
// applyShowWhen doesn't enable it (it is required while in use, and hidden otherwise)
function customUnused(inp) {
  var sel = inp.name && inp.parentNode ? inp.parentNode.querySelector(
    'select[data-custom="' + inp.name + '"]') : null;
  return !!sel && sel.value !== "custom";
}

// 2a) Device "Other device path…": while that option is chosen the text box under the
//     select has the select's name ({prefix}port), so the typed path is posted as the field
//     itself (an older cluster peer saves it as it is) and the select is not posted.
function syncPortPaths(root, focus) {
  (root || document).querySelectorAll("select[data-port-field]").forEach(function (sel) {
    var name = sel.getAttribute("data-port-field");
    var input = sel.parentNode.querySelector("[data-port-typed]");
    if (!input) return;
    var opt = sel.options[sel.selectedIndex];
    var typed = !!(opt && opt.hasAttribute("data-port-other"));
    if (typed) { sel.removeAttribute("name"); input.setAttribute("name", name); }
    else { sel.setAttribute("name", name); input.removeAttribute("name"); }
    sel.required = !typed && sel.hasAttribute("data-required");
    input.required = typed && sel.hasAttribute("data-required");
    input.hidden = !typed;
    input.disabled = !typed;
    if (typed && focus) input.focus();
  });
}

// 2b) data-show-when="field=v1,v2": show the element only when the form control
//     named `field` has one of those values; hidden blocks are also disabled so
//     their inputs neither submit nor block validation (CSP-safe; no inline JS).
function applyShowWhen(root) {
  var form = (root && root.closest) ? root.closest("form") : null;
  var scope = form || document;
  scope.querySelectorAll("[data-show-when]").forEach(function (el) {
    var spec = el.getAttribute("data-show-when");
    var eq = spec.indexOf("=");
    var field = spec.slice(0, eq);
    var vals = spec.slice(eq + 1).split(",");
    var f = el.closest("form");
    if (!f) return;
    var ctrl = f.querySelector('[name="' + field + '"]');
    if (!ctrl) return;
    var show = vals.indexOf(ctrl.value) !== -1;
    el.hidden = !show;
    if (el.tagName === "OPTION") {
      // an option that doesn't apply can't be picked (iOS shows hidden ones); if it was
      // the selected one, the first one that applies is (blocks after it follow that)
      el.disabled = !show;
      var sel = el.closest("select");
      if (!show && el.selected && sel) {
        var first = sel.querySelector("option:not([disabled])");
        if (first) first.selected = true;
      }
      return;
    }
    el.querySelectorAll("input,select,textarea").forEach(function (inp) {
      inp.disabled = !show || customUnused(inp);
    });
  });
}

document.addEventListener("change", function (e) {
  if (e.target.matches && e.target.matches("select[data-custom]")) {
    syncCustomInputs(e.target.parentNode);
  }
  if (e.target.matches && e.target.matches("select[data-port-field]")) {
    syncPortPaths(e.target.parentNode, true);
  }
  if (e.target.matches && e.target.matches("select")) {
    applyShowWhen(e.target);
  }
});

// Initialize newly-swapped form fragments (and the first page load).
document.addEventListener("htmx:afterSwap", function (e) {
  syncCustomInputs(e.target);
  applyShowWhen(e.target);
  syncPortPaths(document);  // (↻ swaps the Device field out: its target is gone)
  initTerm(e.target);
});
document.addEventListener("DOMContentLoaded", function () {
  syncCustomInputs(document);
  applyShowWhen(document);
  syncPortPaths(document);
});

// 2c) <form data-confirm="question">, or a <button data-confirm> that submits it: ask
//     before submitting (plain forms; htmx elements use hx-confirm). Delegated, so no
//     inline JS is needed (CSP). The button's question is asked when it submits.
document.addEventListener("submit", function (e) {
  var b = e.submitter, f = e.target.closest ? e.target.closest("form") : null;
  var q = (b && b.getAttribute("data-confirm")) || (f && f.getAttribute("data-confirm"));
  if (q && !window.confirm(q)) e.preventDefault();
});

// 2d) Settings. A form that did its work is answered with a redirect to the page, at the
//     form's section; one that was refused, with the page itself (what was typed shown
//     again, the reason in the form's section), whose <main data-page-url> names the
//     page's own address. That goes in the address bar, so a reload or Back loads the
//     page instead of posting the form again. Either way the form's message
//     ([data-feedback]) takes the keyboard focus, with its section in view - once the
//     page has loaded: the browser's own scroll to the #section of the address moves
//     the focus to the page itself, after DOMContentLoaded.
document.addEventListener("DOMContentLoaded", function () {
  var main = document.querySelector("main[data-page-url]");
  if (main && window.history && history.replaceState) {
    try { history.replaceState(history.state, "", main.getAttribute("data-page-url")); } catch (e) {}
  }
});
window.addEventListener("load", function () {
  setTimeout(function () {
    var msg = document.querySelector("[data-feedback]");
    var active = document.activeElement;
    if (!msg || (active && active !== document.body)) return;   // the user is somewhere already
    var section = msg.closest("section");
    if (section) section.scrollIntoView();
    msg.focus({ preventScroll: true });
  }, 0);
});

// 2e) Settings → Single sign-on: with no Redirect URI set, the one shown is made from
//     the address the request reached ser2netweb with. When a reverse proxy changed that
//     address, show the redirect URI for this browser's address too.
document.addEventListener("DOMContentLoaded", function () {
  var shown = document.querySelector("#oidc-redirect[data-auto]");
  var warn = document.getElementById("oidc-redirect-mismatch");
  if (!shown || !warn) return;
  var mine = location.origin + basePath() + "/auth/oidc/callback";
  if (shown.getAttribute("data-uri") === mine) return;
  warn.querySelector("[data-browser-uri]").textContent = mine;
  warn.hidden = false;
});

// 3) Unsaved changes in the mapping form. The form is "dirty" once a field was changed
//    (an input / change event, nothing else: a form nobody touched never asks) or when it
//    came back from a failed save (data-dirty="1": the values shown are not saved).
//    Cancel and anything else that would replace the panel — a row's Edit / Log /
//    Monitor / Trace, + Add mapping, a peer's Edit — ask before they throw the changes
//    away; leaving or reloading the page gets the browser's own question.
function dirtyForm() {
  var f = document.querySelector("#form-panel form.mform");
  return f && f.getAttribute("data-dirty") === "1" ? f : null;
}
function mayDiscard() {
  return !dirtyForm() ||
    window.confirm(tr("discard_changes", "Discard the unsaved changes in the open form?"));
}
function markDirty(e) {
  var f = e.target.closest ? e.target.closest("form.mform") : null;
  if (f) f.setAttribute("data-dirty", "1");
}
document.addEventListener("input", markDirty);
document.addEventListener("change", markDirty);
document.addEventListener("htmx:beforeRequest", function (e) {
  var f = dirtyForm();
  if (f && e.detail.target === document.getElementById("form-panel") &&
      !f.contains(e.detail.elt) && !mayDiscard())
    e.preventDefault();
});
window.addEventListener("beforeunload", function (e) {
  if (dirtyForm()) { e.preventDefault(); e.returnValue = ""; }
});

// 3b) TLS files uploaded from the mapping form (its "Upload and check"): the server
//     answers where it keeps them (HX-Trigger: tlsUploaded {field name: path}); they go
//     into the form's path fields, to be saved with it, and the file inputs are emptied.
//     The button is no submit button (it sits in the mapping's form, whose save button
//     must stay that form's first one): [data-submit-form] submits the form it names.
document.addEventListener("click", function (e) {
  var b = e.target.closest ? e.target.closest("[data-submit-form]") : null;
  var f = b ? document.getElementById(b.getAttribute("data-submit-form")) : null;
  if (!f) return;
  e.preventDefault();
  if (f.requestSubmit) f.requestSubmit();
  else htmx.trigger(f, "submit");
});
document.addEventListener("tlsUploaded", function (e) {
  var f = document.querySelector("#form-panel form.mform"), paths = e.detail || {};
  if (!f) return;
  Object.keys(paths).forEach(function (name) {
    var input = /^network_tls_\w+$/.test(name) ? f.elements[name] : null;
    if (input && typeof paths[name] === "string") input.value = paths[name];
  });
  Array.prototype.forEach.call(document.querySelectorAll("input[type=file][form=tls-upload]"),
                               function (input) { input.value = ""; });
  f.setAttribute("data-dirty", "1");
});

// 3a) Cancel/Close buttons clear the form/log panel. Done via a delegated
//     listener (not inline onclick, which the Content-Security-Policy blocks).
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-cancel]");
  if (btn) {
    e.preventDefault();
    if (!mayDiscard()) return;
    closeTerm();
    var panel = document.getElementById("form-panel");
    if (panel) panel.innerHTML = "";
    panelFocus.back();   // to the button that opened it (8)
  }
});

// 4) Console (xterm) over WebSocket. Initialized when a #term element is swapped
//    into the page; the socket is closed when the panel is replaced/cleared.
function closeTerm() {
  var el = document.getElementById("term");
  if (!el) return;
  clearTimeout(el._retry);
  if (el._ws) { try { el._ws.close(); } catch (e) {} el._ws = null; }
  if (el._onresize) { window.removeEventListener("resize", el._onresize); el._onresize = null; }
  if (el._term) { try { el._term.dispose(); } catch (e) {} el._term = null; }
}

function initTerm(root) {
  var el = (root && root.querySelector) ? root.querySelector("#term") : null;
  if (!el && root && root.id === "term") el = root;
  if (!el) el = document.getElementById("term");
  if (!el || el._inited || typeof Terminal === "undefined") return;
  el._inited = true;
  if (Terminal.strings) Terminal.strings.promptLabel = tr("terminal_input", "Terminal input");
  var term = new Terminal({ convertEol: true, fontSize: 13, scrollback: 5000,
                            theme: { background: "#0b0f14" } });
  el._term = term;
  // xterm keeps Tab and Shift+Tab (it sends them to the device), so the keyboard could
  // not leave the console: Shift+Tab leaves it — and Tab too where nothing typed goes to
  // the device (a read-only console, the hex view). (false: xterm leaves the key alone,
  // and the browser moves the focus.)
  var typing = el.getAttribute("data-interactive") === "1";
  term.attachCustomKeyEventHandler(function (ev) {
    return !(ev.key === "Tab" && (ev.shiftKey || !typing || el._mode === "hex"));
  });

  // size the terminal to its container (FitAddon); refit after layout settles
  var fit = null;
  if (typeof FitAddon !== "undefined") {
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
  }
  term.open(el);
  function refit() { if (fit) { try { fit.fit(); } catch (e) {} } }
  refit();
  setTimeout(refit, 30);          // after the panel finishes laying out
  el._onresize = refit;
  window.addEventListener("resize", refit);
  if (el.getAttribute("data-interactive") === "1") {
    var cs = charset(el);
    term.onData(function (d) {
      // UTF-8: a text message (the server sends it as UTF-8); a code page: its bytes
      if (el._mode === "text" && el._ws && el._ws.readyState === 1)
        el._ws.send(cs ? encodeText(d, cs) : d);
    });
    // the focus: once the panel is in view (8) — keys typed into a console out of view
    // (Space / PageDown to scroll the page) would go to the device
  }
  // a Modbus gateway's console opens in the hex view (data-mode): its frames as they are
  connectTerm(el, el.getAttribute("data-mode") === "hex" ? "hex" : "text");
}

// text: the console socket (raw device output, typing goes to the device);
// hex: the traffic socket, both directions. Both speak protocol 2 (?v=2, see
// routes/console.py): a text message is a notice (JSON: {"dropped": n}, {"state":
// "stopped"}); a binary one of the hex view holds records [dir u8][time f64 BE]
// [length u32 BE][bytes], one after another. `again`: connecting again after the mapping
// stopped or restarted (CLOSE_STOPPED) — what the terminal shows stays.
function connectTerm(el, mode, again) {
  var term = el._term;
  if (el._ws) { try { el._ws.onclose = null; el._ws.close(); } catch (e) {} }
  clearTimeout(el._retry);
  el._mode = mode;
  if (!again) {
    el._retries = 0;
    term.reset();
    if (mode === "hex")   // the times below are in the browser's zone (logs and traces: the server's)
      term.write("\x1b[90m " + tr("hex_zone", "times in this browser’s time zone: {0}", browserZone()) +
                 "\r\n " + tr("hex_header", "time          direction   offset  bytes" +
                 "                                            text") + "\x1b[0m\r\n");
  }
  var scheme = location.protocol === "https:" ? "wss" : "ws";
  var path = el.getAttribute(mode === "hex" ? "data-ws-hex" : "data-ws");
  var ws = new WebSocket(scheme + "://" + location.host + path + "?v=2");
  ws.binaryType = "arraybuffer";
  el._ws = ws;
  var bridge = el.getAttribute("data-bridge") === "1", opened = false, cs = charset(el);
  function say(msg) { term.write("\r\n\x1b[33m[" + msg + "]\x1b[0m\r\n"); }
  function notice(text) {
    var n = null;
    try { n = JSON.parse(text); } catch (e) {}
    if (n && n.dropped)
      say(trn("dropped", "{0} byte left out: this browser did not keep up",
              "{0} bytes left out: this browser did not keep up", n.dropped));
    if (n && n.state === "stopped")
      say(tr("ws_not_running", "the mapping is not running: its traffic shows here once it is started"));
  }
  ws.onopen = function () {
    opened = true;
    if (again) say(tr("ws_reconnected", "connected again"));
  };
  ws.onmessage = function (ev) {
    if (typeof ev.data === "string") { notice(ev.data); return; }
    el._retries = 0;
    var bytes = new Uint8Array(ev.data);
    if (mode === "text") term.write(cs ? decodeBytes(bytes, cs) : bytes);
    else term.write(hexRecords(ev.data, bridge, cs));
  };
  ws.onclose = function (ev) {
    if (el._ws !== ws || !el._term) return;   // replaced (the other view) or closed
    el._ws = null;
    // the mapping stopped or restarted: its runner let the viewer go — connect again,
    // to the runner it has now (a few times in a row at most, if nothing comes)
    if (opened && ev.code === 4001 && el._retries < 5) {
      el._retries++;
      say(tr("ws_reconnecting", "the mapping stopped or restarted: connecting again"));
      el._retry = setTimeout(function () { if (el._term) connectTerm(el, mode, true); }, 1000);
      return;
    }
    say(closedText(ev.code, opened));
  };
  var panel = el.closest("#form-panel");
  if (panel) {
    // what is typed goes to the device in the text view only
    panel.querySelectorAll("[data-text-only]").forEach(function (n) { n.hidden = mode === "hex"; });
    var btn = panel.querySelector("[data-term-mode]");
    if (btn) btn.textContent = mode === "hex" ? tr("text_view", "Text view") : tr("hex_view", "Hex view");
  }
}

// "Europe/Istanbul, UTC+03:00" (the zone name where the browser knows it)
function browserZone() {
  var name = "";
  try { name = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
  var off = -new Date().getTimezoneOffset();
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  var utc = off === 0 ? "UTC" : "UTC" + (off > 0 ? "+" : "-") + pad(Math.floor(Math.abs(off) / 60)) +
            ":" + pad(Math.abs(off) % 60);
  return name && name !== "UTC" ? name + ", " + utc : utc;
}

// why a console or hex view ended: a handshake that was refused (no session, or a
// reverse proxy that changed the Host or Origin header: HTTP 403, the browser only
// sees 1006), else the close code (routes/console.py CLOSE_*)
function closedText(code, opened) {
  if (!opened)
    return tr("ws_refused", "could not connect (signed out, or a reverse proxy changed the Host / Origin header)");
  switch (code) {
    case 1013: return tr("ws_busy", "too many consoles and hex views are open: close one, then try again");
    case 4001: return tr("ws_stopped", "the mapping stopped or restarted");
    case 4002: return tr("ws_access", "closed: signed out, or your access changed");
    case 4003: return tr("ws_slow", "closed: this browser took nothing for too long");
    case 4004: return tr("ws_gone", "the mapping no longer exists");
  }
  return tr("disconnected", "console disconnected");
}

// The mapping's console text encoding (its form's "Console text encoding"): UTF-8, which
// xterm.js decodes itself (null), or a single-byte code page, whose characters for the
// bytes 0x80-0xFF the page carries (data-charset: the browser has no CP857, and encodes
// nothing but UTF-8). The bytes 0x00-0x7F are ASCII in all of them.
function charset(el) {
  var cs = el.getAttribute("data-charset") || "";
  return cs.length === 128 ? cs : null;
}
function decodeBytes(bytes, cs) {
  var out = "";
  for (var i = 0; i < bytes.length; i++)
    out += bytes[i] < 128 ? String.fromCharCode(bytes[i]) : cs.charAt(bytes[i] - 128);
  return out;
}
// what is typed, as the code page's bytes: '?' for a character it doesn't have
function encodeText(text, cs) {
  var out = [];
  for (var i = 0; i < text.length; i++) {
    var c = text.charCodeAt(i);
    if (c < 128) { out.push(c); continue; }
    if (c >= 0xd800 && c < 0xdc00) i++;   // beyond U+FFFF: no code page here has one
    var j = (c >= 0xd800 && c < 0xe000) || c === 0xfffd ? -1 : cs.indexOf(text.charAt(i));
    out.push(j >= 0 ? 128 + j : 63);
  }
  return new Uint8Array(out);
}

// the hex view's lines for one message of packed records
function hexRecords(buf, bridge, cs) {
  var dv = new DataView(buf), out = "", off = 0;
  while (off + 13 <= buf.byteLength) {
    var n = dv.getUint32(off + 9, false);
    if (off + 13 + n > buf.byteLength) break;
    out += hexLines(dv.getUint8(off), dv.getFloat64(off + 1, false),
                    new Uint8Array(buf, off + 13, n), bridge, cs);
    off += 13 + n;
  }
  return out;
}

// `cs`: the code page's characters for the text column (else it shows ASCII only)
function hexLines(dir, ts, bytes, bridge, cs) {
  var d = new Date(ts * 1000);
  function pad(n, w) { n = String(n); while (n.length < w) n = "0" + n; return n; }
  var stamp = pad(d.getHours(), 2) + ":" + pad(d.getMinutes(), 2) + ":" + pad(d.getSeconds(), 2) +
              "." + pad(d.getMilliseconds(), 3);
  function col(s) { while (s.length < 11) s += " "; return s; }
  var label = bridge ? (dir === 0 ? "A → B      " : "B → A      ")
                     : col(dir === 0 ? tr("from_device", "◀ device") : tr("to_device", "▶ device"));
  var color = dir === 0 ? "\x1b[32m" : "\x1b[33m";
  var out = "";
  for (var off = 0; off < bytes.length; off += 16) {
    var hex = "", text = "";
    for (var i = off; i < off + 16; i++) {
      if (i < bytes.length) {
        var b = bytes[i];
        hex += (b < 16 ? "0" : "") + b.toString(16) + " ";
        text += (b >= 32 && b < 127) ? String.fromCharCode(b) : (cs && b >= 128 ? cs.charAt(b - 128) : ".");
      } else {
        hex += "   ";
      }
    }
    out += (off === 0 ? stamp + "  " + label : "              " + "           ") +
           color + pad(off.toString(16), 4) + "  " + hex + " \x1b[0m" + text + "\r\n";
  }
  return out;
}

document.addEventListener("click", function (e) {
  var btn = e.target.closest ? e.target.closest("[data-term-mode]") : null;
  if (!btn) return;
  var el = document.getElementById("term");
  if (el && el._term) connectTerm(el, el._mode === "hex" ? "text" : "hex");
});

// close any open console socket BEFORE the form panel is replaced by a new swap
document.addEventListener("htmx:beforeSwap", function (e) {
  if (e.target && e.target.id === "form-panel") closeTerm();
});

// 5) Dashboard mappings table: search / state filter / sort / bulk actions, applied
//    on the client to every refreshed copy of the table (which is swapped only when it
//    changed, see 1c).
(function () {
  var selected = new Set();          // mapping ids ticked for a bulk action
  var KEY = "ser2net-dashboard";     // per-browser filter memory

  // What is typed is folded as the rows' data-search is on the server (search_key in
  // ser2netweb/textfold.py): accents and compatibility forms left off (NFKD without combining
  // marks), Turkish İ/I/ı/i as one letter, letter case and runs of spaces ignored — so
  // "istanbul" finds "İstanbul Depo" and "corlu" "Çorlu". Not toLocaleLowerCase("tr"),
  // which turns I into ı and so breaks searches typed in English.
  // (combining marks: \p{M} where the browser knows it — written as a literal, it would
  // be a SyntaxError for this whole file in a browser that doesn't)
  var MARKS = /[\u0300-\u036f]/g;
  try { MARKS = new RegExp("\\p{M}", "gu"); } catch (e) {}
  function searchKey(s) {
    return String(s).normalize("NFKD").replace(MARKS, "").replace(/\u0131/g, "i").toLowerCase()
      .replace(/\u00df/g, "ss").replace(/\u03c2/g, "\u03c3").replace(/\s+/g, " ").trim();
  }
  // names sort as people read them, in the page's language: Ç after C, İ with I, Hat 2
  // before Hat 10
  var collator = window.Intl && Intl.Collator
    ? new Intl.Collator(document.documentElement.lang || undefined, { numeric: true, sensitivity: "base" })
    : { compare: function (a, b) { return a < b ? -1 : a > b ? 1 : 0; } };

  function tools() { return document.querySelector("[data-mapping-tools]"); }
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } }
  function save(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) {} }

  function restoreControls() {
    var t = tools(); if (!t) return;
    var v = load();
    t.querySelector("[data-filter-text]").value = v.text || "";
    t.querySelector("[data-filter-state]").value = v.state || "";
    t.querySelector("[data-sort]").value = v.sort || "";
  }

  function apply() {
    var t = tools(); var box = document.getElementById("mappings");
    if (!t || !box) return;
    var typed = t.querySelector("[data-filter-text]").value, text = searchKey(typed);
    var st = t.querySelector("[data-filter-state]").value;
    var sort = t.querySelector("[data-sort]").value;
    save({ text: typed.trim(), state: st, sort: sort });   // as typed: shown again as it was
    var body = box.querySelector("table.list tbody"); if (!body) return;
    var rows = Array.prototype.slice.call(body.querySelectorAll("tr[data-mid]"));
    var shown = 0;
    rows.forEach(function (r, i) {
      if (!r.hasAttribute("data-order")) r.setAttribute("data-order", i);
      var ok = (!text || r.getAttribute("data-search").indexOf(text) !== -1) &&
               (!st || r.getAttribute("data-state") === st);
      r.hidden = !ok;
      if (ok) shown++;
      var cb = r.querySelector(".row-select");
      if (cb) cb.checked = selected.has(cb.value);
    });
    function attr(r, a) { return r.getAttribute(a) || ""; }
    function order(a, b) { return +attr(a, "data-order") - +attr(b, "data-order"); }
    function byName(a, b) { return collator.compare(attr(a, "data-name"), attr(b, "data-name")); }
    var cmp = {
      name: byName,
      state: function (a, b) {
        var x = attr(a, "data-state"), y = attr(b, "data-state");
        return x < y ? -1 : x > y ? 1 : byName(a, b);
      },
      port: function (a, b) { return (+attr(a, "data-port") || 0) - (+attr(b, "data-port") || 0); }
    }[sort];
    var sorted = rows.slice().sort(function (a, b) { return (cmp && cmp(a, b)) || order(a, b); });
    // move rows only when the order changes: moving a row takes the keyboard focus from
    // its buttons (put back below, as htmx put it back after the refresh)
    if (sorted.some(function (r, i) { return r !== rows[i]; })) {
      var had = document.activeElement;
      sorted.forEach(function (r) { body.appendChild(r); });
      if (had && had !== document.activeElement && body.contains(had)) had.focus({ preventScroll: true });
    }
    rows = sorted;
    noMatch(body, rows.length > 0 && shown === 0);
    var count = t.querySelector("[data-filter-count]");
    var said = (text || st) ? tr("shown", "{0} of {1} shown", shown, rows.length) : "";
    // a live region: set only when it changes (not at every refresh), so it is read once
    if (count && count.textContent !== said) count.textContent = said;
    // forget selections of mappings that no longer exist
    var ids = new Set(rows.map(function (r) { return r.getAttribute("data-mid"); }));
    selected.forEach(function (id) { if (!ids.has(id)) selected.delete(id); });
    updateBulk();
  }

  // A filter that matches no mapping says so in the table (a filter is kept across visits:
  // a table left empty by it looked like a dashboard without mappings), with a button
  // that shows all of them again.
  function noMatch(body, on) {
    var row = body.querySelector("tr.no-match");
    if (!on) { if (row) body.removeChild(row); return; }
    if (row) return;
    row = document.createElement("tr");
    row.className = "no-match";
    var td = document.createElement("td");
    td.className = "muted";
    td.colSpan = body.parentNode.querySelectorAll("thead th").length || 1;
    td.appendChild(document.createTextNode(
      tr("no_match", "No mapping matches the search or the state filter.") + " "));
    var b = document.createElement("button");
    b.type = "button";
    b.className = "link";
    b.setAttribute("data-filter-clear", "");
    b.textContent = tr("show_all", "Show all");
    td.appendChild(b);
    row.appendChild(td);
    body.appendChild(row);
  }

  // A bulk action acts on the ticked rows the table shows: a row the search or the state
  // filter hides stays ticked (it is shown again with the filter) but is left out, and
  // the bar says how many those are. (A ticked row can be hidden without anyone touching
  // the filter: a state filter hides it when its state changes.)
  function shownRows() {
    var box = document.getElementById("mappings");
    return box ? Array.prototype.filter.call(box.querySelectorAll("table.list tbody tr[data-mid]"),
                                             function (r) { return !r.hidden; }) : [];
  }
  function shownSelected() {
    return shownRows().filter(function (r) { return selected.has(r.getAttribute("data-mid")); });
  }

  function updateBulk() {
    var t = tools(); if (!t) return;
    var bar = t.querySelector("[data-bulk]"); if (!bar) return;
    var rows = shownRows(), on = shownSelected(), hidden = selected.size - on.length;
    bar.hidden = selected.size === 0;
    var count = t.querySelector("[data-bulk-count]");
    var said = hidden
      ? tr("selected_hidden", "{0} selected ({1} more hidden by the filter: left out)", on.length, hidden)
      : tr("selected", "{0} selected", on.length);
    if (count.textContent !== said) count.textContent = said;   // a live region, as above
    bar.querySelectorAll("[data-bulk-action]").forEach(function (b) { b.disabled = !on.length; });
    // "Select all shown": ticked when every row shown is, half when some are
    var all = t.querySelector("[data-select-shown]");
    if (all) {
      all.disabled = !rows.length;
      all.checked = rows.length > 0 && on.length === rows.length;
      all.indeterminate = on.length > 0 && on.length < rows.length;
    }
  }

  document.addEventListener("input", function (e) {   // (the search box; not a checkbox's)
    if (e.target.matches && e.target.matches("[data-mapping-tools] [data-filter-text]")) apply();
  });
  document.addEventListener("change", function (e) {
    if (e.target.hasAttribute && e.target.hasAttribute("data-select-shown")) {
      var tick = e.target.checked;
      shownRows().forEach(function (r) {
        if (tick) selected.add(r.getAttribute("data-mid")); else selected.delete(r.getAttribute("data-mid"));
      });
      apply();
      return;
    }
    if (e.target.closest && e.target.closest("[data-mapping-tools]")) apply();
    if (e.target.classList && e.target.classList.contains("row-select")) {
      if (e.target.checked) selected.add(e.target.value); else selected.delete(e.target.value);
      updateBulk();
    }
  });
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-filter-clear]")) {
      var t = tools();
      if (!t) return;
      t.querySelector("[data-filter-text]").value = "";
      t.querySelector("[data-filter-state]").value = "";
      apply();
      t.querySelector("[data-filter-text]").focus();
      return;
    }
    var b = e.target.closest ? e.target.closest("[data-bulk-action], [data-bulk-clear]") : null;
    if (!b) return;
    if (b.hasAttribute("data-bulk-clear")) { selected.clear(); apply(); return; }
    var rows = shownSelected(), action = b.getAttribute("data-bulk-action");
    if (!rows.length || !window.htmx) return;
    // asked every time: how many, how many connected clients that drops, how many ticked
    // rows the filter hides (left out)
    var clients = rows.reduce(function (n, r) { return n + (+r.getAttribute("data-clients") || 0); }, 0);
    var hidden = selected.size - rows.length;
    var n = rows.length;
    var q = action === "stop"
      ? trn("bulk_stop", "Stop {0} mapping? It stays stopped until it is started again.",
            "Stop {0} mappings? They stay stopped until they are started again.", n)
      : action === "restart" ? trn("bulk_restart", "Restart {0} mapping?", "Restart {0} mappings?", n)
      : trn("bulk_start", "Start {0} mapping?", "Start {0} mappings?", n);
    if (action !== "start" && clients)
      q += " " + trn("bulk_clients", "{0} connected client will be disconnected.",
                     "{0} connected clients will be disconnected.", clients);
    if (hidden)
      q += " " + trn("bulk_hidden", "{0} selected mapping hidden by the filter is left out.",
                     "{0} selected mappings hidden by the filter are left out.", hidden);
    if (!window.confirm(q)) return;
    // the button is the source: its hx-disabled-elt holds a second click off while the
    // request runs, and the feedback area (7) gets the answer
    htmx.ajax("POST", basePath() + "/api/mappings/bulk", { source: b, swap: "none", values: {
      action: action, ids: rows.map(function (r) { return r.getAttribute("data-mid"); }) } });
  });
  // done: the selection is cleared (an old one is not acted on again by mistake)
  document.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.askedBy;
    if (el && el.hasAttribute && el.hasAttribute("data-bulk-action") && e.detail.successful) {
      selected.clear();
      apply();
    }
  });

  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "mappings") apply();
  });
  document.addEventListener("DOMContentLoaded", restoreControls);
})();

// 6) (The cluster panel, like every self-refreshing panel, is not refreshed in a
//    background tab - each refresh makes this node ask every peer: see 1d.)

// 7) Feedback: what an action did and why a request failed, in the two live regions of
//    _shell.html — #ui-status (polite: the "X-Notice" header of a successful action)
//    and #ui-alert (a failed request; the session ended; the server is unreachable).
//    htmx shows nothing of a 4xx/5xx answer or a network error by itself. A server
//    text goes in with textContent only, never as HTML. A panel that refreshes itself
//    ("every Ns") is marked as not up to date while its refreshes fail, until the next
//    answer that works (also a 204 "unchanged").
//    The answers are taken from htmx:afterRequest, not htmx:responseError: when the
//    element that asked was replaced meanwhile (a row button while the table
//    refreshed), htmx sends afterRequest on to the nearest element still in the page,
//    while responseError would go to the replaced one and be lost (see 0b).
var feedback = (function () {
  var shown = {}, seq = 0;

  function drop(key) {
    var el = shown[key];
    if (!el) return;
    delete shown[key];
    if (el.parentNode) el.parentNode.removeChild(el);
  }

  // kind "ok" (status), "warn" or "error" (alert); link: {href, text, newTab}; detail:
  // the message's tooltip (the raw error a translated reason stands for). The same
  // text under the same key again changes nothing, so a refresh that keeps failing is
  // announced once; a message without a key is always a new one. "ok" goes by itself.
  function show(key, kind, text, link, detail) {
    var box = document.getElementById(kind === "ok" ? "ui-status" : "ui-alert");
    if (!box || !text) return;
    key = key || "n" + (++seq);
    if (shown[key] && shown[key]._text === text) return;
    drop(key);
    var note = document.createElement("div");
    note.className = "notice";
    note._text = text;
    note._key = key;
    var banner = document.createElement("div");
    banner.className = "banner " + (kind === "ok" ? "ok" : kind === "warn" ? "warn" : "error");
    var msg = document.createElement("span");
    msg.textContent = text;
    if (detail) msg.title = detail;
    banner.appendChild(msg);
    if (link) {
      var a = document.createElement("a");
      a.href = link.href;
      a.textContent = link.text;
      if (link.newTab) { a.target = "_blank"; a.rel = "noopener"; }
      banner.appendChild(document.createTextNode(" "));
      banner.appendChild(a);
    }
    var x = document.createElement("button");
    x.type = "button";
    x.className = "link dismiss";
    x.textContent = "×";
    x.setAttribute("aria-label", tr("dismiss", "Dismiss"));
    x.addEventListener("click", function () { drop(key); });
    banner.appendChild(x);
    note.appendChild(banner);
    box.appendChild(note);
    shown[key] = note;
    while (box.children.length > 4) {   // the newest few
      var old = box.firstChild;
      if (shown[old._key] === old) delete shown[old._key];
      box.removeChild(old);
    }
    if (kind === "ok") setTimeout(function () { if (shown[key] === note) drop(key); }, 6000);
  }

  return { show: show, drop: drop };
})();

(function () {
  // the framework's own status texts (English, and saying no more than the status)
  var STATUS_TEXTS = ["Bad Request", "Unauthorized", "Forbidden", "Not Found",
    "Method Not Allowed", "Request Entity Too Large", "Content Too Large",
    "Internal Server Error", "Bad Gateway", "Service Unavailable", "Gateway Timeout"];

  // The server's message: its refusals (role, CSRF, sign-in to peers, a peer that
  // didn't answer, validation) are plain text in the page's language. For anything
  // else — JSON, a proxy's HTML error page, a bare status text — a translated one.
  function errorText(xhr) {
    var type = xhr.getResponseHeader("Content-Type") || "";
    var body = (xhr.responseText || "").trim();
    if (/^text\/plain/i.test(type) && body && body.length <= 2000 &&
        STATUS_TEXTS.indexOf(body) === -1)
      return body;
    return xhr.status >= 500
      ? tr("server_error", "Something went wrong on the server (HTTP {0}).", xhr.status)
      : tr("failed", "That did not work (HTTP {0}).", xhr.status);
  }

  function pollKey(el) { return "poll " + (el.id || el.getAttribute("hx-get") || ""); }
  function setStale(el, on) {
    if (on) {
      el.classList.add("stale");
      el.setAttribute("data-stale", tr("stale", "not up to date"));
    } else if (el.classList.contains("stale")) {
      el.classList.remove("stale");
      el.removeAttribute("data-stale");
    }
  }

  // 401: the session ended (or was ended). Not a redirect: an open form would lose its
  // unsaved changes. Signing in again brings the page back (with the new CSRF token the
  // sign-in gave the browser: 1).
  function sessionEnded() {
    // ?next= is the page's path without the base path (the server puts it back)
    var base = basePath(), path = location.pathname, keep = !!dirtyForm();
    if (base && (path === base || path.indexOf(base + "/") === 0)) path = path.slice(base.length) || "/";
    var here = path + location.search;
    feedback.show("session", "error",
                  tr("session_ended", "Your session has ended: this page is not up to date, and changes can’t be saved until you sign in again."),
                  { href: basePath() + "/login" + (here !== "/" ? "?next=" + encodeURIComponent(here) : ""),
                    // with unsaved changes open here: sign in in another tab, then save here
                    text: keep ? tr("sign_in_tab", "Sign in in a new tab (keeps the open form)")
                               : tr("sign_in", "Sign in"),
                    newTab: keep });
  }

  document.addEventListener("htmx:afterRequest", function (e) {
    var d = e.detail, xhr = d.xhr, el = d.askedBy || d.elt;
    if (!xhr || !xhr.status) return;   // no answer: htmx:sendError (or an abort)
    var poll = polls(el);
    // 2xx (204, 286 too) or a re-rendered form (1b); htmx sets "successful" once it has
    // handled the answer
    if (d.successful !== undefined ? d.successful : xhr.status < 400) {
      feedback.drop("session");
      feedback.drop("offline");
      if (poll) { setStale(el, false); feedback.drop(pollKey(el)); }
      var n = xhr.getResponseHeader("X-Notice");
      if (n) {
        try { n = JSON.parse(decodeURIComponent(n)); } catch (err) { n = null; }
        if (n && n.text) feedback.show(null, n.kind === "warn" ? "warn" : "ok", String(n.text),
                                       null, n.detail ? String(n.detail) : "");
        if (n && n.focus) panelFocus.want(String(n.focus));   // when the panel closes (8)
      }
      return;
    }
    if (poll) setStale(el, true);
    if (xhr.status === 401) sessionEnded();
    else feedback.show(poll ? pollKey(el) : null, "error", errorText(xhr));
  });

  // no answer at all: the server is down or the network is gone. The refreshes go on,
  // and the first answer that works takes this away again.
  document.addEventListener("htmx:sendError", function (e) {
    var el = e.detail.askedBy || e.detail.elt;
    if (polls(el)) setStale(el, true);
    feedback.show("offline", "error", tr("offline", "The server can’t be reached: this page is not up to date. It updates again as soon as the server answers."));
  });
})();

// 7b) Copy a one-time token or key: a [data-copy] button copies the .token-box next to
//     it. The clipboard API works only on a secure page (https, localhost): on plain
//     http the text is selected for Ctrl+C.
document.addEventListener("click", function (e) {
  var b = e.target.closest ? e.target.closest("[data-copy]") : null;
  var box = b && b.parentNode ? b.parentNode.querySelector(".token-box") : null;
  if (!box) return;
  function byHand() {
    var range = document.createRange(), sel = window.getSelection();
    range.selectNodeContents(box);
    sel.removeAllRanges();
    sel.addRange(range);
    feedback.show("copy", "ok", tr("copy_by_hand", "Selected: press Ctrl+C (⌘C on a Mac) to copy it."));
  }
  if (!window.isSecureContext || !navigator.clipboard) { byHand(); return; }
  navigator.clipboard.writeText(box.textContent.trim()).then(function () {
    feedback.show("copy", "ok", tr("copied", "Copied to the clipboard."));
  }, byHand);
});

// 7c) Clock check: the server's clock when it rendered this page (<body data-server-time>)
//    against this browser's. A difference of more than 2 minutes is shown where the page
//    has a [data-clock-skew] element (Settings -> Admin interface).
(function () {
  function check() {
    var el = document.querySelector("[data-clock-skew]");
    var server = +(document.body && document.body.getAttribute("data-server-time"));
    if (!el || !server) return;
    try {   // a page from the history (back / forward) was rendered earlier
      var nav = performance.getEntriesByType("navigation")[0];
      if (nav && nav.type === "back_forward") return;
    } catch (e) {}
    var diff = Date.now() / 1000 - server, s = Math.abs(diff);
    if (s <= 120) return;
    var amount = s < 7200 ? tr("minutes", "{0} min", Math.round(s / 60))
               : s < 172800 ? tr("hours", "{0} h", Math.round(s / 3600))
               : trn("days", "{0} day", "{0} days", Math.round(s / 86400));
    el.textContent = diff > 0
      ? tr("clock_ahead", "This browser's clock is {0} ahead of the server's.", amount)
      : tr("clock_behind", "This browser's clock is {0} behind the server's.", amount);
    el.hidden = false;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", check);
  else check();
})();

// 8) The form panel (#form-panel: a mapping's form, log, console or traces) sits above
//    the table. The buttons that open it scroll it into view (hx-swap "show:top"), then
//    it takes the keyboard focus: its error banner when it has one; an operator's
//    console when it is in view (what is typed there goes to the device); else its
//    heading. Closing it, or a save that empties it, gives the focus back to the button
//    that opened it — or to the one the save names (X-Notice "focus": a new mapping's
//    Edit button, once the refreshed table has it). A refresh inside the panel (the
//    log's) moves nothing.
var panelFocus = (function () {
  var opener = null, wanted = null;

  function give(id) {
    var el = id ? document.getElementById(id) : null;
    if (!el) return false;
    el.focus();
    return true;
  }
  function back() {
    var id = opener;
    opener = null;
    if (give(wanted)) wanted = null;
    else if (!wanted) give(id);   // a wanted one not there yet: the refreshed table's (below)
  }
  function inView(el) {
    var r = el.getBoundingClientRect();
    return r.top >= 0 && r.top < window.innerHeight;
  }
  function settled(p, fromPanel) {
    if (!p.firstElementChild) { back(); return; }
    var err = p.querySelector(".banner.error");
    if (fromPanel && !err) return;
    var term = p.querySelector("#term");
    if (!err && term && term._term && term.getAttribute("data-interactive") === "1" && inView(term)) {
      term._term.focus();
      return;
    }
    var target = err || p.querySelector("#panel-title");
    if (target) target.focus({ preventScroll: true });
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    var p = document.getElementById("form-panel"), el = e.detail.elt;
    if (e.defaultPrevented || !p || !el || e.detail.target !== p) return;
    e.detail.fromPanel = p.contains(el);
    if (!e.detail.fromPanel) { opener = el.id || null; wanted = null; }
  });
  document.addEventListener("htmx:afterSettle", function (e) {
    var p = e.target;
    if (!p || p.id !== "form-panel") return;
    var fromPanel = !!e.detail.fromPanel;
    // htmx scrolls the panel into view right after this event
    setTimeout(function () { settled(p, fromPanel); }, 0);
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var id = e.target ? e.target.id : "";
    if (wanted && (id === "mappings" || id === "cluster") && give(wanted)) wanted = null;
  });
  return {
    back: back,
    want: function (id) { wanted = id; }
  };
})();
