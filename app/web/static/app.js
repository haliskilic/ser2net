// ser2net admin UI — minimal vanilla JS (no framework, CSP-friendly).

// UI texts in the page's language (base.html puts them on <body data-i18n>)
var I18N = (function () {
  try { return JSON.parse(document.body.getAttribute("data-i18n") || "{}"); } catch (e) { return {}; }
})();
function tr(key, fallback) {
  var s = I18N[key] || fallback;
  for (var i = 2; i < arguments.length; i++) s = s.replace("{" + (i - 2) + "}", arguments[i]);
  return s;
}

// 0) Theme: apply the saved light/dark preference as early as possible, and toggle
//    it (persisted in localStorage) when a [data-theme-toggle] control is clicked.
(function () {
  try {
    if (localStorage.getItem("ser2net-theme") === "light")
      document.documentElement.setAttribute("data-theme", "light");
  } catch (e) {}
})();
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

// 1) Send the CSRF token on every htmx request (double-submit cookie pattern).
document.addEventListener("htmx:configRequest", function (e) {
  var m = document.querySelector('meta[name="csrf-token"]');
  if (m) e.detail.headers["X-CSRF-Token"] = m.getAttribute("content");
});

// 1a) Detected-ports panel: "show / hide the ports without hardware" sticks across the
//     5-second refreshes (the polling element carries the choice).
document.addEventListener("htmx:configRequest", function (e) {
  var el = e.detail.elt;
  if (el && el.id === "ports-table" && el.getAttribute("data-show-all") === "1")
    e.detail.parameters["all"] = "1";
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
  if (polls(el) && xhr && xhr.status === 200) {
    var h = xhr.getResponseHeader("X-Content-Hash");
    if (h) el.setAttribute("data-hash", h);
  }
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
    el.querySelectorAll("input,select,textarea").forEach(function (inp) {
      inp.disabled = !show;
    });
  });
}

document.addEventListener("change", function (e) {
  if (e.target.matches && e.target.matches("select[data-custom]")) {
    syncCustomInputs(e.target.parentNode);
  }
  if (e.target.matches && e.target.matches("select")) {
    applyShowWhen(e.target);
  }
});

// Initialize newly-swapped form fragments (and the first page load).
document.addEventListener("htmx:afterSwap", function (e) {
  syncCustomInputs(e.target);
  applyShowWhen(e.target);
  initTerm(e.target);
});
document.addEventListener("DOMContentLoaded", function () {
  syncCustomInputs(document);
  applyShowWhen(document);
});

// 2c) <form data-confirm="question">: ask before submitting (plain forms; htmx
//     elements use hx-confirm). Delegated, so no inline JS is needed (CSP).
document.addEventListener("submit", function (e) {
  var f = e.target.closest ? e.target.closest("form[data-confirm]") : null;
  if (f && !window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
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
  // the device. (false: xterm leaves the key alone, and the browser moves the focus.)
  var typing = el.getAttribute("data-interactive") === "1";
  term.attachCustomKeyEventHandler(function (ev) {
    return !(ev.key === "Tab" && (ev.shiftKey || !typing));
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
    term.onData(function (d) {
      if (el._mode === "text" && el._ws && el._ws.readyState === 1) el._ws.send(d);
    });
    // the focus: once the panel is in view (8) — keys typed into a console out of view
    // (Space / PageDown to scroll the page) would go to the device
  }
  connectTerm(el, "text");
}

// text: the console socket (raw device output, typing goes to the device);
// hex: the traffic socket — frames [dir u8][time f64 BE][bytes], both directions
function connectTerm(el, mode) {
  var term = el._term;
  if (el._ws) { try { el._ws.onclose = null; el._ws.close(); } catch (e) {} }
  el._mode = mode;
  term.reset();
  var scheme = location.protocol === "https:" ? "wss" : "ws";
  var path = el.getAttribute(mode === "hex" ? "data-ws-hex" : "data-ws");
  var ws = new WebSocket(scheme + "://" + location.host + path);
  ws.binaryType = "arraybuffer";
  el._ws = ws;
  var bridge = el.getAttribute("data-bridge") === "1";
  ws.onmessage = function (ev) {
    if (mode === "text") {
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(new Uint8Array(ev.data));
      return;
    }
    if (typeof ev.data === "string" || ev.data.byteLength < 9) return;
    var dv = new DataView(ev.data);
    term.write(hexLines(dv.getUint8(0), dv.getFloat64(1, false),
                        new Uint8Array(ev.data, 9), bridge));
  };
  ws.onclose = function () {
    term.write("\r\n\x1b[33m[" + tr("disconnected", "console disconnected") + "]\x1b[0m\r\n");
  };
  var btn = el.closest("#form-panel") ? el.closest("#form-panel").querySelector("[data-term-mode]") : null;
  if (btn) btn.textContent = mode === "hex" ? tr("text_view", "Text view") : tr("hex_view", "Hex view");
  if (mode === "hex")
    term.write("\x1b[90m " + tr("hex_header", "time          direction   offset  bytes" +
               "                                            text") + "\x1b[0m\r\n");
}

function hexLines(dir, ts, bytes, bridge) {
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
        text += (b >= 32 && b < 127) ? String.fromCharCode(b) : ".";
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
    var text = t.querySelector("[data-filter-text]").value.trim().toLowerCase();
    var st = t.querySelector("[data-filter-state]").value;
    var sort = t.querySelector("[data-sort]").value;
    save({ text: text, state: st, sort: sort });
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
    var key = {
      "": function (r) { return +r.getAttribute("data-order"); },
      name: function (r) { return r.getAttribute("data-name"); },
      state: function (r) { return r.getAttribute("data-state") + " " + r.getAttribute("data-name"); },
      port: function (r) { return +r.getAttribute("data-port") || 0; }
    }[sort] || function (r) { return +r.getAttribute("data-order"); };
    var sorted = rows.slice().sort(function (a, b) { var x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
    // move rows only when the order changes: moving a row takes the keyboard focus from
    // its buttons (put back below, as htmx put it back after the refresh)
    if (sorted.some(function (r, i) { return r !== rows[i]; })) {
      var had = document.activeElement;
      sorted.forEach(function (r) { body.appendChild(r); });
      if (had && had !== document.activeElement && body.contains(had)) had.focus({ preventScroll: true });
    }
    rows = sorted;
    var count = t.querySelector("[data-filter-count]");
    var said = (text || st) ? tr("shown", "{0} of {1} shown", shown, rows.length) : "";
    // a live region: set only when it changes (not at every refresh), so it is read once
    if (count && count.textContent !== said) count.textContent = said;
    // forget selections of mappings that no longer exist
    var ids = new Set(rows.map(function (r) { return r.getAttribute("data-mid"); }));
    selected.forEach(function (id) { if (!ids.has(id)) selected.delete(id); });
    updateBulk();
  }

  function updateBulk() {
    var t = tools(); if (!t) return;
    var bar = t.querySelector("[data-bulk]"); if (!bar) return;
    bar.hidden = selected.size === 0;
    var count = t.querySelector("[data-bulk-count]");
    var said = tr("selected", "{0} selected", selected.size);
    if (count.textContent !== said) count.textContent = said;   // a live region, as above
  }

  document.addEventListener("input", function (e) {
    if (e.target.closest && e.target.closest("[data-mapping-tools]")) apply();
  });
  document.addEventListener("change", function (e) {
    if (e.target.closest && e.target.closest("[data-mapping-tools]")) apply();
    if (e.target.classList && e.target.classList.contains("row-select")) {
      if (e.target.checked) selected.add(e.target.value); else selected.delete(e.target.value);
      updateBulk();
    }
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-bulk-action], [data-bulk-clear]") : null;
    if (!b) return;
    if (b.hasAttribute("data-bulk-clear")) { selected.clear(); apply(); return; }
    if (!selected.size || !window.htmx) return;
    // the button is the source: its hx-disabled-elt holds a second click off while the
    // request runs, and the feedback area (7) gets the answer
    htmx.ajax("POST", "/api/mappings/bulk", { source: b, swap: "none", values: {
      action: b.getAttribute("data-bulk-action"), ids: Array.from(selected) } });
  });

  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "mappings") apply();
  });
  document.addEventListener("DOMContentLoaded", restoreControls);
})();

// 6) Cluster panel: a tab in the background skips its refresh (each one makes this node
//    ask every peer) and refreshes as soon as it is shown again.
document.addEventListener("htmx:beforeRequest", function (e) {
  var el = e.detail.elt;
  if (el && el.id === "cluster" && document.hidden) e.preventDefault();
});
document.addEventListener("visibilitychange", function () {
  if (!document.hidden && document.getElementById("cluster") && window.htmx)
    htmx.trigger(document.body, "refreshCluster");
});

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

  // kind "ok" (status), "warn" or "error" (alert); link: {href, text, newTab}. The same
  // text under the same key again changes nothing, so a refresh that keeps failing is
  // announced once; a message without a key is always a new one. "ok" goes by itself.
  function show(key, kind, text, link) {
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
  // unsaved changes. Signing in again (it keeps the CSRF cookie) brings the page back.
  function sessionEnded() {
    var here = location.pathname + location.search, keep = !!dirtyForm();
    feedback.show("session", "error",
                  tr("session_ended", "Your session has ended: this page is not up to date, and changes can’t be saved until you sign in again."),
                  { href: "/login" + (here !== "/" ? "?next=" + encodeURIComponent(here) : ""),
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
        if (n && n.text) feedback.show(null, n.kind === "warn" ? "warn" : "ok", String(n.text));
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
