// pyser2net admin UI — minimal vanilla JS (no framework, CSP-friendly).

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

document.addEventListener("change", function (e) {
  if (e.target.matches && e.target.matches("select[data-custom]")) {
    syncCustomInputs(e.target.parentNode);
  }
});

// Initialize newly-swapped form fragments (and the first page load).
document.addEventListener("htmx:afterSwap", function (e) {
  syncCustomInputs(e.target);
});
document.addEventListener("DOMContentLoaded", function () {
  syncCustomInputs(document);
});

// 3) Cancel/Close buttons clear the form/log panel. Done via a delegated
//    listener (not inline onclick, which the Content-Security-Policy blocks).
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-cancel]");
  if (btn) {
    e.preventDefault();
    var panel = document.getElementById("form-panel");
    if (panel) panel.innerHTML = "";
  }
});
