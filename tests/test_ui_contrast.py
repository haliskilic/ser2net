"""Text in the web UI's status colours is readable in both themes (no browser needed).

The badges (running / stopped / reconnecting / device missing / error), the client chips,
the banners (open access, the one-time API token, sign-in and form errors), short error
texts, tags, links and the primary button: each text colour, on its own tint over every
background it is shown on (page, panel, form panel), has at least the 4.5:1 contrast
WCAG AA asks of small text — in the dark theme and in the light one. The light theme used
the dark theme's status colours (1.6-2.3:1). Both themes tell the browser their colour
scheme, so it draws its own controls (checkboxes, scrollbars) to match, and the saved
theme is applied by a script that is not deferred (no dark first paint).

The values come from app/web/static/app.css itself: its :root and :root[data-theme=light]
custom properties and the rules below.

Run: python3 tests/test_ui_contrast.py
"""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
CSS = os.path.join(ROOT, "app", "web", "static", "app.css")
AA = 4.5

# what is checked: the rules that style it (later ones add to earlier ones), and the
# backgrounds (custom properties) it is drawn on; its own background, if any, is a tint
# over those
PAGE = ("--bg", "--panel", "--panel2")
CHECKS = {
    "running badge": ((".state", ".state-running"), PAGE),
    "stopped badge": ((".state", ".state-stopped"), PAGE),
    "reconnecting badge": ((".state", ".state-reconnecting"), PAGE),
    "device-missing badge": ((".state", ".state-device-missing"), PAGE),
    "error badge (offline cluster node too)": ((".state", ".state-error"), PAGE),
    "client chip": ((".peer",), PAGE),
    "priority client chip": ((".peer", ".peer-prio"), PAGE),
    "error banner": ((".banner", ".banner.error"), PAGE),
    "ok banner (the one-time token)": ((".banner", ".banner.ok"), PAGE),
    "warning banner (open access)": ((".banner", ".banner.warn"), PAGE),
    "short error text": ((".err-small",), PAGE),
    "short warning text (a port's hint)": ((".warn-small",), PAGE),
    "muted text": ((".muted",), PAGE),
    "link": (("a",), PAGE),
    "link button": (("button.link",), PAGE),
    "not-up-to-date note": ((".stale::before",), PAGE),
    "tag": ((".tag",), ("--panel2",)),
    "danger tag": ((".tag", ".tag-danger"), ("--panel2",)),
    "warning tag": ((".tag", ".tag-warn"), ("--panel2",)),
    "danger button, pointed at": ((".btn", ".btn.danger:hover"), ("--panel2",)),
    "primary button": ((".btn", ".btn.primary"), ()),
    "primary button, pointed at": ((".btn", ".btn.primary", ".btn.primary:hover"), ()),
}


def strip_media(css):
    """The CSS without its @media blocks (their rules only change the layout)."""
    out, i = [], 0
    while (j := css.find("@media", i)) != -1:
        out.append(css[i:j])
        depth, k = 0, css.index("{", j)
        while True:
            depth += {"{": 1, "}": -1}.get(css[k], 0)
            k += 1
            if depth == 0:
                break
        i = k
    out.append(css[i:])
    return "".join(out)


def rules(css):
    """{selector: {property: value}} of the top-level rules (later ones add to earlier)."""
    out = {}
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        decls = dict((k.strip(), v.strip()) for k, _, v in
                     (d.partition(":") for d in body.split(";") if ":" in d))
        for one in sel.split(","):
            out.setdefault(" ".join(one.split()), {}).update(decls)
    return out


def parse_color(value, props):
    value = value.strip()
    while value.startswith("var("):
        value = props[value[4:value.index(")")].strip()].strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (1.0,)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    assert m, f"not a colour: {value!r}"
    parts = [p.strip() for p in m.group(1).split(",")]
    return tuple(float(p) for p in parts[:3]) + ((float(parts[3]),) if len(parts) > 3 else (1.0,))


def over(top, bottom):
    a = top[3]
    return tuple(t * a + b * (1 - a) for t, b in zip(top[:3], bottom[:3])) + (1.0,)


def luminance(c):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def themes(css):
    dark = rules(css)[":root"]
    light = dict(dark, **rules(css)[':root[data-theme="light"]'])
    return {"dark": {k: v for k, v in dark.items() if k.startswith("--")},
            "light": {k: v for k, v in light.items() if k.startswith("--")}}


def test_status_text_contrast():
    css = strip_media(re.sub(r"/\*.*?\*/", "", open(CSS, encoding="utf-8").read(), flags=re.S))
    table = rules(css)
    bad = []
    for theme, props in themes(css).items():
        for what, (sels, backdrops) in CHECKS.items():
            decl = {}
            for sel in sels:
                assert sel in table, f"no rule for {sel}"
                decl.update(table[sel])
            assert "color" in decl, f"{what}: no colour"
            text = parse_color(decl["color"], props)
            tint = decl.get("background")
            tint = None if tint in ("none", "transparent") else tint
            for name in backdrops or ("(own background)",):
                base = parse_color(f"var({name})", props) if backdrops else (0, 0, 0, 1.0)
                ground = over(parse_color(tint, props), base) if tint else base
                ratio = contrast(text, ground)
                if ratio < AA:
                    bad.append(f"{theme}: {what} ({decl['color']} on {tint or name}, "
                               f"over {name}): {ratio:.2f}:1")
    assert not bad, "text below 4.5:1:\n  " + "\n  ".join(bad)
    print(f"status colours, banners, tags, links, primary button: >= {AA}:1 in both themes  OK")


def test_color_scheme_and_early_theme():
    css = re.sub(r"/\*.*?\*/", "", open(CSS, encoding="utf-8").read(), flags=re.S)
    table = rules(strip_media(css))
    assert table[":root"].get("color-scheme") == "dark", "the default theme is dark"
    assert table[':root[data-theme="light"]'].get("color-scheme") == "light"
    for box in (".logbox", ".term"):   # dark in both themes: their scrollbars too
        assert table[box].get("color-scheme") == "dark", box
    base = open(os.path.join(ROOT, "app", "web", "templates", "base.html"), encoding="utf-8").read()
    head = base[:base.index("</head>")]
    tag = re.search(r'<script src="\{\{ base \}\}/static/theme\.js\?v=\{\{ asset_version \}\}"([^>]*)>',
                    head)
    assert tag and "defer" not in tag.group(1) and "async" not in tag.group(1), \
        "theme.js runs before the first paint"
    assert head.index("theme.js") < head.index("app.css"), "theme.js before the stylesheets"
    js = open(os.path.join(ROOT, "app", "web", "static", "theme.js"), encoding="utf-8").read()
    assert 'localStorage.getItem("ser2net-theme")' in js and '"data-theme", "light"' in js
    print("color-scheme per theme; the saved theme is set by a script in <head>  OK")


def main():
    test_status_text_contrast()
    test_color_scheme_and_early_theme()
    print("\nPASS: UI contrast")


if __name__ == "__main__":
    main()
