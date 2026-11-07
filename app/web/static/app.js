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
//     do nothing — the user never sees why Save failed. For 400/422 (validation), let
//     the re-rendered form fragment (with its error banner) swap into the panel.
//     401/403 (auth/CSRF) are left as errors so they don't render a fragment.
document.addEventListener("htmx:beforeSwap", function (e) {
  var status = e.detail.xhr ? e.detail.xhr.status : 0;
  if (status === 400 || status === 422) {
    e.detail.shouldSwap = true;
    e.detail.isError = false;
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

// 3) Cancel/Close buttons clear the form/log panel. Done via a delegated
//    listener (not inline onclick, which the Content-Security-Policy blocks).
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-cancel]");
  if (btn) {
    e.preventDefault();
    closeTerm();
    var panel = document.getElementById("form-panel");
    if (panel) panel.innerHTML = "";
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
  var term = new Terminal({ convertEol: true, fontSize: 13, scrollback: 5000,
                            theme: { background: "#0b0f14" } });
  el._term = term;

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
    term.focus();
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
//    on the client to every refreshed copy of the table. The table refresh itself
//    answers 204 when nothing changed (X-Content-Hash), so the page doesn't flicker.
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
    rows.sort(function (a, b) { var x = key(a), y = key(b); return x < y ? -1 : x > y ? 1 : 0; });
    rows.forEach(function (r) { body.appendChild(r); });
    var count = t.querySelector("[data-filter-count]");
    if (count) count.textContent = (text || st) ? tr("shown", "{0} of {1} shown", shown, rows.length) : "";
    // forget selections of mappings that no longer exist
    var ids = new Set(rows.map(function (r) { return r.getAttribute("data-mid"); }));
    selected.forEach(function (id) { if (!ids.has(id)) selected.delete(id); });
    updateBulk();
  }

  function updateBulk() {
    var t = tools(); if (!t) return;
    var bar = t.querySelector("[data-bulk]"); if (!bar) return;
    bar.hidden = selected.size === 0;
    t.querySelector("[data-bulk-count]").textContent = tr("selected", "{0} selected", selected.size);
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
    htmx.ajax("POST", "/api/mappings/bulk", { swap: "none", values: {
      action: b.getAttribute("data-bulk-action"), ids: Array.from(selected) } });
  });

  // only swap the table when it changed (the server answers 204 otherwise)
  document.addEventListener("htmx:configRequest", function (e) {
    var el = e.detail.elt;
    if (el && el.id === "mappings" && el.getAttribute("data-hash"))
      e.detail.headers["X-Content-Hash"] = el.getAttribute("data-hash");
  });
  document.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.elt, xhr = e.detail.xhr;
    if (el && el.id === "mappings" && xhr && xhr.status === 200) {
      var h = xhr.getResponseHeader("X-Content-Hash");
      if (h) el.setAttribute("data-hash", h);
    }
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
