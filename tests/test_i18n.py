"""The web UI's Turkish translation is complete and consistent.

  * every string wrapped for translation — _("...") in the templates and in the Python
    sources — has an entry in app/locale/tr.json, and every entry is still used;
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

from app import i18n
from app.web.auth import hash_password

TEMPLATES = os.path.join(ROOT, "app", "web", "templates")
CATALOG = os.path.join(ROOT, "app", "locale", "tr.json")
# _("...") / _('...') in templates (the first argument; Jinja string escapes are rare)
_TEMPLATE_CALL = re.compile(r"""\b_\(\s*(?P<q>["'])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""", re.S)
_PY_PLACEHOLDER = re.compile(r"(?<!{){(\d+)}(?!})")
_TPL_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_TAG = re.compile(r"</?([a-z][a-z0-9]*)\b")
# templates format every translation with %, so a percent sign must be written %%;
# Python strings go through str.format, so their braces must match the source exactly
_BARE_PERCENT = re.compile(r"%(?!\(\w+\)s|%)")
_BRACES = re.compile(r"\{\{|\}\}|\{[^{}]*\}|[{}]")


def template_strings() -> set:
    out = set()
    for f in glob.glob(os.path.join(TEMPLATES, "*.html")):
        src = open(f, encoding="utf-8").read()
        for m in _TEMPLATE_CALL.finditer(src):
            s = m.group("s")
            out.add(bytes(s, "utf-8").decode("unicode_escape") if "\\" in s else s)
    return out


def python_strings() -> set:
    """Literal first arguments of _() calls in app/**/*.py (implicitly concatenated
    literals are one Constant in the AST)."""
    out = set()
    for f in glob.glob(os.path.join(ROOT, "app", "**", "*.py"), recursive=True):
        tree = ast.parse(open(f, encoding="utf-8").read())
        for node in ast.walk(tree):
            name = isinstance(node, ast.Call) and (getattr(node.func, "id", None)
                                                   or getattr(node.func, "attr", None))
            if (name in ("_", "gettext")
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
    return out


def test_catalog_complete():
    cat = json.load(open(CATALOG, encoding="utf-8"))
    in_templates, in_python = template_strings(), python_strings()
    used = in_templates | in_python
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
    print(f"catalog: {len(used)} strings, all translated, placeholders and tags kept  OK")


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
    with tempfile.TemporaryDirectory() as empty:  # a build that left app/locale out
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
        classic = (b"connection: &plc\n  accepter: tcp,127.0.0.1,4001\n"
                   b"  connector: serialdev,/dev/ttyUSB9,9600n81\n  options:\n    chardelay-scale: 20\n")
        st, _, page = b.post("/settings/config/import", {"step": "preview", "mode": "merge"},
                             files={"file": ("ser2net.yaml", classic)})
        assert st == 200 and esc(tr["Import preview"]) in page, page[page.find("banner"):][:300]
        assert esc(tr["not imported (no equivalent here): {0}"].format("chardelay-scale")) in page
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
    test_negotiation()
    test_missing_catalog_reported()
    test_pages()
    print("\nPASS: TR/EN web UI")


if __name__ == "__main__":
    main()
