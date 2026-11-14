"""The web UI's Turkish translation is complete and consistent.

  * every string wrapped for translation — _("...") in the templates and in the Python
    sources, and both forms of an ngettext("...", "...", n) — has an entry in
    ser2netweb/locale/tr.json, and every entry is still used; the two forms of a text with a
    count have the same placeholders, and no text writes a count as "(s)";
  * the languages are listed once (i18n.LANGUAGES; config.UI_LANGUAGES is derived from
    it), and every one but English has its catalog file;
  * a translation keeps the placeholders of its English source ({0} {1} in Python,
    %(name)s in templates) and the HTML tags in it, and can't break the formatting (a
    bare % in a template string, a stray brace in a Python one);
  * the language is chosen from the visitor's choice (cookie), the admin's default or
    the browser (Accept-Language), and English is the fallback;
  * a language whose catalog can't be loaded (left out of a build) is reported for the
    start-up warning instead of silently showing English;
  * pages really render in Turkish (and English), the language switch works, and the
    REST API, logs and audit log stay English.

Run: python3 tests/test_i18n.py
"""
import ast
import glob
import json
import os
import re
import tempfile

import markupsafe
from _server import ROOT, Browser, Server

from ser2netweb import i18n
from ser2netweb.web.auth import hash_password

TEMPLATES = os.path.join(ROOT, "ser2netweb", "web", "templates")
CATALOG = os.path.join(ROOT, "ser2netweb", "locale", "tr.json")
# _("...") / _('...') in templates (the first argument; Jinja string escapes are rare)
_TEMPLATE_CALL = re.compile(r"""\b_\(\s*(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""", re.S)
# ngettext("one", "other", n) in templates: both forms; for app.js (base.html) the two
# forms are a list, [_("one"), _("other")]
_TEMPLATE_PLURAL = re.compile(r"""\bngettext\(\s*(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*,"""
                              r"""\s*(?P<q2>["'])(?P<p>(?:\\.|(?!(?P=q2)).)*)(?P=q2)""", re.S)
_JS_PLURAL = re.compile(r"""\[_\(\s*(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)\s*,"""
                        r"""\s*_\(\s*(?P<q2>["'])(?P<p>(?:\\.|(?!(?P=q2)).)*)(?P=q2)\s*\)\s*\]""", re.S)
_PY_PLACEHOLDER = re.compile(r"(?<!{){(\d+)}(?!})")
_TPL_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_TAG = re.compile(r"</?([a-z][a-z0-9]*)\b")
# templates format every translation with %, so a percent sign must be written %%;
# Python strings go through str.format, so their braces must match the source exactly
_BARE_PERCENT = re.compile(r"%(?!\(\w+\)s|%)")
_BRACES = re.compile(r"\{\{|\}\}|\{[^{}]*\}|[{}]")


def _unescape(s: str) -> str:
    return bytes(s, "utf-8").decode("unicode_escape") if "\\" in s else s


def template_strings(plurals: list | None = None) -> set:
    """The texts wrapped in the templates; `plurals` gets each ngettext's (one, other)."""
    out = set()
    for f in glob.glob(os.path.join(TEMPLATES, "*.html")):
        src = open(f, encoding="utf-8").read()
        for m in _TEMPLATE_CALL.finditer(src):
            out.add(_unescape(m.group("s")))
        for m in (*_TEMPLATE_PLURAL.finditer(src), *_JS_PLURAL.finditer(src)):
            pair = (_unescape(m.group("s")), _unescape(m.group("p")))
            out.update(pair)
            if plurals is not None:
                plurals.append(pair)
    return out


def python_strings(plurals: list | None = None) -> set:
    """Literal first arguments of _() calls in ser2netweb/**/*.py, both of an ngettext()
    (implicitly concatenated literals are one Constant in the AST)."""
    out = set()
    for f in glob.glob(os.path.join(ROOT, "ser2netweb", "**", "*.py"), recursive=True):
        tree = ast.parse(open(f, encoding="utf-8").read())
        for node in ast.walk(tree):
            name = isinstance(node, ast.Call) and (getattr(node.func, "id", None)
                                                   or getattr(node.func, "attr", None))
            literal = [isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args[:2]] \
                if isinstance(node, ast.Call) else []
            if name in ("_", "gettext") and literal[:1] == [True]:
                out.add(node.args[0].value)
            elif name == "ngettext" and literal == [True, True]:
                pair = (node.args[0].value, node.args[1].value)
                out.update(pair)
                if plurals is not None:
                    plurals.append(pair)
    return out


def test_catalog_complete():
    cat = json.load(open(CATALOG, encoding="utf-8"))
    plurals: list = []
    in_templates, in_python = template_strings(plurals), python_strings(plurals)
    used = in_templates | in_python
    assert len(plurals) >= 20, plurals
    for one, other in plurals:   # either is formatted with the same values
        for rx in (_PY_PLACEHOLDER, _TPL_PLACEHOLDER):
            assert sorted(rx.findall(one)) == sorted(rx.findall(other)), (one, other)
    counted = sorted(t for t in used if re.search(r"[a-z]\(s\)", t))
    assert not counted, "a count written as '(s)' (use ngettext):\n  " + "\n  ".join(counted)
    missing = sorted(used - set(cat))
    stale = sorted(set(cat) - used)
    assert not missing, "untranslated strings:\n  " + "\n  ".join(missing[:40])
    assert not stale, "translations nothing uses any more:\n  " + "\n  ".join(stale[:40])
    bad = []
    for en, tr in cat.items():
        if not tr.strip():
            bad.append(f"empty: {en!r}")
        for rx in (_PY_PLACEHOLDER, _TPL_PLACEHOLDER):
            if sorted(rx.findall(en)) != sorted(rx.findall(tr)):
                bad.append(f"placeholders differ: {en!r} -> {tr!r}")
        if sorted(_TAG.findall(en)) != sorted(_TAG.findall(tr)):
            bad.append(f"HTML tags differ: {en!r} -> {tr!r}")
        if en in in_templates and (_BARE_PERCENT.search(en) or _BARE_PERCENT.search(tr)):
            bad.append(f"a template string needs %% for a percent sign: {en!r} -> {tr!r}")
        if en in in_python and sorted(_BRACES.findall(en)) != sorted(_BRACES.findall(tr)):
            bad.append(f"braces differ (str.format): {en!r} -> {tr!r}")
    assert not bad, "\n  ".join(bad[:40])
    print(f"catalog: {len(used)} strings ({len(plurals)} with a count, both forms), all "
          "translated, placeholders and tags kept  OK")


def test_language_registry():
    from ser2netweb.config import UI_LANGUAGES
    assert UI_LANGUAGES == ("auto", *i18n.LANGUAGES), UI_LANGUAGES
    for code in i18n.LANGUAGES:
        if code != i18n.DEFAULT:
            assert os.path.isfile(os.path.join(ROOT, "ser2netweb", "locale", f"{code}.json")), code
    assert i18n.ngettext("%(num)s record", "%(num)s records", 1) == "%(num)s record"
    assert i18n.ngettext("%(num)s record", "%(num)s records", 0) == "%(num)s records"
    print("languages: one list (UI_LANGUAGES derived), a catalog for each; ngettext's forms  OK")


def test_negotiation():
    n = i18n.negotiate
    assert n(None, None) == "en"
    assert n(None, "tr-TR,tr;q=0.9,en;q=0.8") == "tr"
    assert n(None, "de-DE,en;q=0.5,tr;q=0.4") == "en"
    assert n(None, "de-DE,tr;q=0.7") == "tr"
    assert n("en", "tr") == "en", "the visitor's choice wins"
    assert n(None, "tr", default="en") == "en", "a fixed admin default beats the browser"
    assert n("tr", "en", default="en") == "tr"
    assert n("xx", "tr") == "tr", "an unknown cookie is ignored"
    tr = json.load(open(CATALOG, encoding="utf-8"))
    tok = i18n.set_language("tr")
    try:
        assert i18n._("Mappings") == tr["Mappings"] != "Mappings"
    finally:
        i18n.reset(tok)
    assert i18n._("Mappings") == "Mappings", "outside a request: English"
    print("language: cookie > admin default > Accept-Language > English  OK")


def test_missing_catalog_reported():
    assert i18n.missing_catalogs() == [], "every offered language has its catalog"
    real = i18n.LOCALE_DIR
    with tempfile.TemporaryDirectory() as empty:  # a build that left ser2netweb/locale out
        i18n.LOCALE_DIR = empty
        i18n.catalog.cache_clear()
        try:
            assert i18n.missing_catalogs() == ["tr"]
        finally:
            i18n.LOCALE_DIR = real
            i18n.catalog.cache_clear()
    assert i18n.catalog("tr"), "the real catalog loads again"
    print("a language without its catalog is reported (start-up warning)  OK")


def esc(text):
    """As the template autoescape writes it."""
    return str(markupsafe.escape(text))


def banner(page: str) -> str:
    m = re.search(r'<div class="banner error"[^>]*>(.*?)</div>', page, re.S)
    assert m, page[:400]
    return m.group(1)


def test_form_errors(b, tr):
    """A blank or mistyped number, or a value that isn't allowed, is refused in the page's
    language with the field's name — never Python's own text ("invalid literal for int()
    with base 10: ''", "could not convert string to float") or a tuple ("(5, 6, 7, 8)")."""
    form = {"name": "x", "kind": "net", "serial_port": "/dev/ttyUSB-i18n", "network_mode": "server",
            "network_protocol": "raw", "network_bind_ip": "127.0.0.1", "network_port": "4123"}
    whole, number = tr["{0}: enter a whole number."], tr["{0}: enter a number."]
    cases = [
        ({"network_port": ""}, whole.format(tr["Listen port"])),
        ({"network_port": "12x"}, whole.format(tr["Listen port"])),
        ({"serial_baudrate": "custom", "serial_custom_baud": ""}, whole.format(tr["Baud rate"])),
        ({"kind": "serialbridge", "serial_b_port": "/dev/ttyUSB-i18n-b", "serial_b_baudrate": "custom",
          "serial_b_custom_baud": " "}, whole.format(tr["{0} (serial port B)"].format(tr["Baud rate"]))),
        ({"network_mode": "client", "network_remote_host": "10.0.0.9", "network_remote_port": "4001",
          "network_connect_timeout_s": "abc"}, number.format(tr["Connect timeout (s)"])),
        ({"opt_trace_max_mb": "nan", "opt_trace_keep": "3"}, number.format(tr["Trace file size (MB)"])),
        ({"serial_bytesize": "9"}, tr["Data bits must be one of {0}."].format("5, 6, 7, 8")),
    ]
    for change, want in cases:
        st, _, page = b.post("/api/mappings/save", {**form, **change}, htmx=True)
        text = banner(page)
        assert st == 400 and esc(want) in text, (change, text)
        assert not any(raw in text for raw in ("invalid literal", "could not convert", "('", "(5,")), text
    print(f"form errors: {len(cases)} blank / mistyped / not allowed values named in Turkish, "
          "no Python or tuple text  OK")


def test_option_labels(b, tr):
    """Options that were shown as their raw ids: flow control, RTS / DTR on open, roles
    (the role's name stays, as the REST API and the audit log write it; what it may do
    is added in the page's language)."""
    st, _, form = b.get("/api/mappings/form")
    assert st == 200, st
    for value, label in (("none", tr["None"]), ("rtscts", tr["RTS/CTS (hardware)"]),
                         ("keep", tr["Leave as is"]), ("on", tr["On"]), ("off", tr["Off"])):
        assert f'<option value="{value}" ' in form and f">{esc(label)}</option>" in form, (value, label)
    st, _, page = b.get("/settings")
    for role, what in (("admin", "everything"), ("operator", "manage mappings"), ("viewer", "read-only")):
        assert f">{role} ({esc(tr[what])})</option>" in page, role
    print("options: flow control, RTS/DTR and roles labelled in Turkish  OK")


def test_pages():
    tr = json.load(open(CATALOG, encoding="utf-8"))
    users = [{"username": "admin", "password_hash": hash_password("i18n-pass-1"),
              "role": "admin", "pwd_version": 1}]
    with Server({"users": users}) as srv:
        b = Browser(srv.base)
        b.op.addheaders = [("Accept-Language", "tr-TR,tr;q=0.9")]
        st, _, page = b.get("/login")
        assert st == 200 and 'lang="tr"' in page and esc(tr["Sign in"]) in page, page[:300]
        st, _, page = b.post("/login", {"username": "admin", "password": "wrong"})
        assert esc(tr["Invalid username or password."]) in page
        b.login("admin", "i18n-pass-1")
        for path, en in (("/", "Mappings"), ("/settings", "Settings"), ("/settings/audit", "Audit log")):
            st, _, page = b.get(path)
            assert st == 200 and esc(tr[en]) in page, (path, page[:300])
        st, _, page = b.post("/api/mappings/save", {"name": "x", "kind": "net", "serial_port": ""}, htmx=True)
        assert st == 400 and esc(tr["Serial port is required."]) in page, page[page.find("banner"):][:200]
        test_form_errors(b, tr)
        test_option_labels(b, tr)
        classic = (b"connection: &plc\n  accepter: tcp,127.0.0.1,4001\n"
                   b"  connector: serialdev,/dev/ttyUSB9,9600n81\n  options:\n    mdns-name: plc\n")
        st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                             files={"file": ("ser2net.yaml", classic)})
        assert st == 200 and esc(tr["Import preview"]) in page, page[page.find("banner"):][:300]
        assert esc(tr["not imported (no equivalent here): {0}"].format("mdns-name")) in page
        st, h, _ = b.get("/lang/en?next=/settings")
        assert st == 303 and h.get("Location") == "/settings"
        st, _, page = b.get("/settings")
        assert 'lang="en"' in page and "Settings" in page, "the switch must win over the browser"
        assert b.get("/lang/en?next=https://evil.example/")[1].get("Location") == "/", "open redirect"
        import urllib.request
        req = urllib.request.Request(srv.base + "/api/v1/health", headers={"Accept-Language": "tr"})
        assert json.loads(urllib.request.urlopen(req, timeout=10).read())["status"] == "ok"
        out = srv.output()
        assert tr["Invalid username or password."] not in out and "failed login for 'admin'" in out, \
            "logs must stay English"
        assert "UI translation is missing" not in out, out[:1500]
        print("pages: Turkish by Accept-Language (login, errors, dashboard, settings, audit, "
              "ser2net.yaml import warnings), switch to English sticks, API/logs English  OK")


def main():
    test_catalog_complete()
    test_language_registry()
    test_negotiation()
    test_missing_catalog_reported()
    test_pages()
    print("\nPASS: TR/EN web UI")


if __name__ == "__main__":
    main()
