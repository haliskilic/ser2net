// Modbus points editor of the mapping form: one box of fields per point, over the points
// JSON box (<textarea name="modbus_points">). The JSON stays the field the form posts — what
// an older cluster node's form posts as well — and the editor only rewrites it; a point's
// keys it has no field for (min, max, allowed, poll, a newer node's) are kept as they are.
// CSP-friendly: no inline handlers, the texts come from the editor's data-texts.
(function () {
  var DTYPES = ["uint16", "int16", "uint32", "int32", "float32", "int64", "uint64", "float64",
                "string", "bool"];
  var ORDERS = ["ABCD", "CDAB", "BADC", "DCBA"];
  var FIELDS = ["name", "unit", "fn", "address", "dtype", "bit", "length", "word_order",
                "scale", "writable"];
  // numbers past 2**53 would come back changed from JSON.parse: such JSON stays text
  var LONG_NUMBER = /(^|[^\w.])-?\d{16,}/;

  function texts(ed) {
    try { return JSON.parse(ed.getAttribute("data-texts") || "{}"); } catch (e) { return {}; }
  }
  function t(T, key, arg) { return (T[key] || key).replace("{0}", arg === undefined ? "" : arg); }

  function boxOf(ed) {
    var form = ed.closest("form");
    return form ? form.querySelector('textarea[name="modbus_points"]') : null;
  }

  // The points in the box, or throws an Error saying why they can't be shown as fields.
  function parse(box, T) {
    var raw = box.value.trim();
    if (!raw) return [];
    if (LONG_NUMBER.test(raw)) throw new Error(t(T, "long_number"));
    var pts = JSON.parse(raw);
    if (pts && typeof pts === "object" && !Array.isArray(pts)) pts = [pts];
    if (!Array.isArray(pts)) throw new Error(t(T, "not_list"));
    pts.forEach(function (p, i) {
      if (!p || typeof p !== "object" || Array.isArray(p)) throw new Error(t(T, "not_object", i + 1));
    });
    return pts;
  }

  function option(sel, value, label) {
    var o = document.createElement("option");
    o.value = String(value);
    o.textContent = label === undefined ? String(value) : label;
    sel.appendChild(o);
  }

  function field(row, T, key, p) {
    var label = document.createElement("label");
    label.setAttribute("data-field", key);
    var input;
    if (key === "fn" || key === "dtype" || key === "word_order") {
      input = document.createElement("select");
      var values = key === "fn" ? [1, 2, 3, 4] : key === "dtype" ? DTYPES : ORDERS;
      values.forEach(function (v) { option(input, v, key === "fn" ? t(T, "fn" + v) : v); });
      var cur = p[key] === undefined ? (key === "fn" ? 3 : key === "dtype" ? "uint16" : "ABCD") : p[key];
      if (values.map(String).indexOf(String(cur)) === -1) option(input, cur);  // keep it as typed
      input.value = String(cur);
    } else if (key === "writable") {
      label.className = "chk";
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = p.writable === true;
    } else {
      input = document.createElement("input");
      input.type = key === "name" ? "text" : "number";
      if (key !== "name") input.step = key === "scale" ? "any" : "1";
      if (key === "name") { input.spellcheck = false; label.className = "wide"; }
      var placeholder = {unit: "1", address: "0", scale: "1"}[key];
      if (placeholder) input.placeholder = placeholder;
      input.value = p[key] === undefined || p[key] === null ? "" : String(p[key]);
    }
    input.setAttribute("data-k", key);
    if (key === "writable") { label.appendChild(input); label.appendChild(document.createTextNode(" " + t(T, key))); }
    else { label.appendChild(document.createTextNode(t(T, key))); label.appendChild(input); }
    row.appendChild(label);
  }

  // which of a point's fields apply to it: a bit for a bool on a register, a length for a
  // string, a word order for a register value. ``prune`` (the type or function was just
  // changed): the key of a field that no longer applies goes too, as the server refuses it
  function showFields(row, p, prune) {
    var register = Number(p.fn === undefined ? 3 : p.fn) >= 3;
    row.querySelectorAll("[data-field]").forEach(function (label) {
      var k = label.getAttribute("data-field");
      label.hidden = (k === "bit" && !(p.dtype === "bool" && register)) ||
                     (k === "length" && p.dtype !== "string") ||
                     (k === "word_order" && (p.dtype === "bool" || !register));
      if (label.hidden && prune && k in p) {
        delete p[k];
        var input = label.querySelector("[data-k]");
        input.value = k === "word_order" ? "ABCD" : "";
      }
    });
  }

  function others(p) {
    return Object.keys(p).filter(function (k) { return FIELDS.indexOf(k) === -1; });
  }

  function render(ed, pts) {
    var T = texts(ed), list = ed.querySelector("[data-points-rows]");
    list.textContent = "";
    pts.forEach(function (p, i) {
      var row = document.createElement("div");
      row.className = "point-row";
      row.setAttribute("data-row", String(i));
      FIELDS.forEach(function (k) { field(row, T, k, p); });
      var actions = document.createElement("div");
      actions.className = "point-actions";
      var more = others(p);
      if (more.length) {
        var note = document.createElement("span");
        note.className = "muted";
        note.textContent = t(T, "more", more.join(", "));
        actions.appendChild(note);
      }
      var rm = document.createElement("button");
      rm.type = "button";
      rm.className = "btn sm";
      rm.setAttribute("data-points-remove", "");
      rm.textContent = t(T, "remove");
      actions.appendChild(rm);
      row.appendChild(actions);
      showFields(row, p);
      list.appendChild(row);
    });
    var add = document.createElement("button");
    add.type = "button";
    add.className = "btn sm";
    add.setAttribute("data-points-add", "");
    add.textContent = t(T, "add");
    list.appendChild(add);
  }

  function write(ed) {
    var box = boxOf(ed);
    if (box) box.value = ed._points.length ? JSON.stringify(ed._points, null, 2) : "";
  }

  function setMode(ed, table) {
    var T = texts(ed), box = boxOf(ed), msg = ed.querySelector("[data-points-msg]");
    var list = ed.querySelector("[data-points-rows]"), btn = ed.querySelector("[data-points-mode]");
    if (!box) return;
    msg.textContent = "";
    msg.hidden = true;
    if (table) {
      try {
        ed._points = parse(box, T);
      } catch (e) {
        msg.textContent = t(T, "bad_json", e.message);
        msg.className = "points-msg banner warn";  // (a warning only once there is one)
        msg.hidden = false;
        return;
      }
      render(ed, ed._points);
    }
    ed._table = table;
    list.hidden = !table;
    box.hidden = table;  // (still posted: hidden, not disabled)
    btn.textContent = t(T, table ? "as_json" : "as_table");
  }

  function update(ed, input) {
    var row = input.closest("[data-row]");
    if (!row) return;
    var p = ed._points[Number(row.getAttribute("data-row"))], k = input.getAttribute("data-k");
    if (k === "writable") {
      if (input.checked) p.writable = true; else delete p.writable;
    } else if (k === "name") {
      p.name = input.value;
    } else if (k === "dtype" || k === "word_order") {
      if (k === "word_order" && input.value === "ABCD") delete p.word_order;
      else p[k] = input.value;
    } else {
      var raw = input.value.trim();
      var n = Number(raw);
      if (raw === "" || !isFinite(n)) delete p[k]; else p[k] = n;
    }
    showFields(row, p, k === "dtype" || k === "fn");
    write(ed);
  }

  function init(root) {
    (root || document).querySelectorAll("[data-points-editor]").forEach(function (ed) {
      if (ed._ready) return;
      ed._ready = true;
      ed._points = [];
      ed._table = false;
    });
  }

  document.addEventListener("click", function (e) {
    var target = e.target.closest ? e.target : null;
    var ed = target ? target.closest("[data-points-editor]") : null;
    if (!ed) return;
    if (target.closest("[data-points-mode]")) {
      setMode(ed, !ed._table);
    } else if (target.closest("[data-points-add]")) {
      ed._points.push({name: "", unit: 1, fn: 3, address: 0, dtype: "uint16"});
      write(ed);
      render(ed, ed._points);
      var rows = ed.querySelectorAll(".point-row");
      if (rows.length) rows[rows.length - 1].querySelector('[data-k="name"]').focus();
    } else if (target.closest("[data-points-remove]")) {
      var row = target.closest("[data-row]");
      ed._points.splice(Number(row.getAttribute("data-row")), 1);
      write(ed);
      render(ed, ed._points);
    }
  });
  function onEdit(e) {
    var ed = e.target.closest ? e.target.closest("[data-points-editor]") : null;
    if (ed && ed._table && e.target.hasAttribute("data-k")) update(ed, e.target);
  }
  document.addEventListener("input", onEdit);
  document.addEventListener("change", onEdit);
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
  document.addEventListener("DOMContentLoaded", function () { init(document); });
})();
