"""Post an HTML form the way a browser does, without a browser (for tests).

    form = Form(html)          # the first <form> of a page or fragment
    form.run_app_js()          # what app.js does when htmx shows the fragment
    fields = form.data()       # [(name, value), ...]: what htmx 2 posts (new FormData(form))

The page is read as a browser reads it (line breaks normalized, NUL -> U+FFFD, the
first line break of a <textarea> dropped), an input's value as its type sanitizes it
(a one-line text field loses its line breaks, a number field a value that is no
number), and the form data set is built as the HTML spec says: no disabled control,
no unticked checkbox, the selected options of a select that are not disabled.

run_app_js() repeats app.js on a swapped-in fragment: syncCustomInputs (a
<select data-custom> enables its "Custom..." input only while "custom" is picked),
then applyShowWhen (each [data-show-when="field=v1,v2"] element in document order:
an <option> is disabled when it doesn't apply, and if it was the selected one the
first option that is not disabled is selected; any other element sets every input,
select and textarea inside it disabled or enabled). option_rule=False is app.js
before 2.9.0 (options were only hidden). tests/test_ui_layout.py checks the result
against a real browser.
"""
from __future__ import annotations

import html.parser
import re

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}
CONTROLS = ("input", "select", "textarea")
_FLOAT = re.compile(r"-?(\d+(\.\d+)?|\.\d+)([eE][-+]?\d+)?")


class El:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list = []   # El or str
        if parent is not None:
            parent.children.append(self)

    def walk(self):
        """This element and every element inside it, in document order."""
        yield self
        for c in self.children:
            if isinstance(c, El):
                yield from c.walk()

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def closest(self, tag):
        el = self
        while el is not None and el.tag != tag:
            el = el.parent
        return el

    def query_name(self, name):
        """querySelector('[name="..."]') inside this element."""
        return next((e for e in self.walk() if e is not self and e.attrs.get("name") == name),
                    None)

    @property
    def disabled(self) -> bool:
        return "disabled" in self.attrs

    @disabled.setter
    def disabled(self, on: bool) -> None:
        if on:
            self.attrs["disabled"] = ""
        else:
            self.attrs.pop("disabled", None)


class _Builder(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("#document", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        el = El(tag, {k: ("" if v is None else v) for k, v in attrs}, self.cur)
        if tag not in VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        El(tag, {k: ("" if v is None else v) for k, v in attrs}, self.cur)

    def handle_endtag(self, tag):
        el = self.cur
        while el is not None and el.tag != tag:
            el = el.parent
        if el is not None and el.parent is not None:
            self.cur = el.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def _options(select: El) -> list:
    return [e for e in select.walk() if e.tag == "option"]


def _option_value(opt: El) -> str:
    if "value" in opt.attrs:
        return opt.attrs["value"]
    return " ".join(opt.text().split())


class Form:
    def __init__(self, page: str):
        # the input stream as a browser preprocesses it: CR LF / CR -> LF; and a NUL in an
        # attribute value or a textarea is a U+FFFD
        page = page.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "�")
        b = _Builder()
        b.feed(page)
        b.close()
        self.form = next(e for e in b.root.walk() if e.tag == "form")
        self.selected: dict = {}   # select -> its selected option
        for sel in self._all("select"):
            opts = _options(sel)
            chosen = [o for o in opts if "selected" in o.attrs]
            # the last option marked selected wins; none: the first that isn't disabled
            self.selected[id(sel)] = (chosen[-1] if chosen else
                                      next((o for o in opts if not o.disabled), None))

    def _all(self, *tags):
        return [e for e in self.form.walk() if e.tag in tags]

    def names(self) -> set:
        """The name of every control in the form, posted or not."""
        return {e.attrs["name"] for e in self._all(*CONTROLS) if e.attrs.get("name")}

    def value(self, el: El) -> str:
        """The control's value as script reads it (el.value)."""
        if el.tag == "select":
            opt = self.selected.get(id(el))
            return _option_value(opt) if opt is not None else ""
        if el.tag == "textarea":
            text = el.text()
            return text[1:] if text.startswith("\n") else text
        kind = el.attrs.get("type", "text").lower()
        v = el.attrs.get("value", "")
        if kind in ("checkbox", "radio"):
            return el.attrs.get("value", "on")
        if kind in ("text", "search", "tel", "password"):
            return v.replace("\n", "")
        if kind in ("url", "email"):
            return v.replace("\n", "").strip(" \t\n\f\r")
        if kind == "number":
            return v if _FLOAT.fullmatch(v) else ""
        return v   # hidden and the rest: as written

    def run_app_js(self, option_rule: bool = True) -> "Form":
        for sel in self._all("select"):              # syncCustomInputs
            target = sel.attrs.get("data-custom")
            if target is None or sel.parent is None:
                continue
            inp = sel.parent.query_name(target)
            if inp is not None:
                inp.disabled = self.value(sel) != "custom"
        for el in list(self.form.walk()):            # applyShowWhen
            spec = el.attrs.get("data-show-when")
            if spec is None:
                continue
            field, _eq, vals = spec.partition("=")
            ctrl = self.form.query_name(field)
            if ctrl is None:
                continue
            show = self.value(ctrl) in vals.split(",")
            if el.tag == "option":
                if not option_rule:
                    continue
                el.disabled = not show
                sel = el.closest("select")
                if not show and sel is not None and self.selected.get(id(sel)) is el:
                    first = next((o for o in _options(sel) if not o.disabled), None)
                    if first is not None:
                        self.selected[id(sel)] = first
                continue
            for inp in el.walk():
                if inp is not el and inp.tag in CONTROLS:
                    inp.disabled = not show
        return self

    def data(self) -> list:
        """new FormData(form): (name, value) of every control that is submitted."""
        out = []
        for el in self._all(*CONTROLS):
            name = el.attrs.get("name")
            if not name or el.disabled or self._in_disabled_fieldset(el):
                continue
            if el.tag == "select":
                opt = self.selected.get(id(el))
                if opt is not None and not opt.disabled:
                    out.append((name, _option_value(opt)))
                continue
            kind = el.attrs.get("type", "text").lower() if el.tag == "input" else ""
            if kind in ("submit", "button", "reset", "image", "file"):
                continue
            if kind in ("checkbox", "radio") and "checked" not in el.attrs:
                continue
            out.append((name, self.value(el)))
        return out

    @staticmethod
    def _in_disabled_fieldset(el: El) -> bool:
        p = el.parent
        while p is not None:
            if p.tag == "fieldset" and p.disabled:
                return True
            p = p.parent
        return False


def browser_post(page: str, option_rule: bool = True) -> list:
    """What a browser posts for the form in *page* when it is saved unchanged."""
    return Form(page).run_app_js(option_rule).data()
