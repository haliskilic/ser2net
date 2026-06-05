// ser2net admin UI — minimal vanilla JS (no framework, CSP-friendly).

// 1) Send the CSRF token on every htmx request (double-submit cookie pattern).
document.addEventListener("htmx:configRequest", function (e) {
  var m = document.querySelector('meta[name="csrf-token"]');
  if (m) e.detail.headers["X-CSRF-Token"] = m.getAttribute("content");
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

  var scheme = location.protocol === "https:" ? "wss" : "ws";
  var ws = new WebSocket(scheme + "://" + location.host + el.getAttribute("data-ws"));
  ws.binaryType = "arraybuffer";
  el._ws = ws;
  ws.onmessage = function (ev) {
    if (typeof ev.data === "string") term.write(ev.data);
    else term.write(new Uint8Array(ev.data));
  };
  ws.onopen = refit;
  ws.onclose = function () { term.write("\r\n\x1b[33m[console disconnected]\x1b[0m\r\n"); };
  if (el.getAttribute("data-interactive") === "1") {
    term.onData(function (d) { if (ws.readyState === 1) ws.send(d); });
    term.focus();
  }
}

// close any open console socket BEFORE the form panel is replaced by a new swap
document.addEventListener("htmx:beforeSwap", function (e) {
  if (e.target && e.target.id === "form-panel") closeTerm();
});
