"""The READMEs and docs/ point at what exists.

  * every command-line option (ser2netweb/cli.py; but the hidden --service) is in both READMEs;
  * every guide in docs/ is linked from both READMEs;
  * every relative link in the READMEs, docs/*.md, CONTRIBUTING.md and SECURITY.md leads to
    a file of the repository, and a link to a heading of a Markdown file (#anchor) to a
    heading that file has (GitHub's anchors).

Pure file checks (no sockets).

Run: python3 tests/test_docs.py
"""
from __future__ import annotations

import glob
import os
import re

from _server import ROOT

READMES = ("README.md", "README.en.md")
DOCS = sorted(os.path.relpath(p, ROOT).replace(os.sep, "/")
              for p in glob.glob(os.path.join(ROOT, "docs", "*.md")))
CHECKED = [*READMES, *DOCS, "CONTRIBUTING.md", "SECURITY.md", "ROADMAP.md"]
# [text](target): the target up to a space or the closing parenthesis
LINK = re.compile(r"\]\(([^)\s]+)\)")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def cli_options() -> list[str]:
    """The options the command line (ser2netweb/cli.py) defines, as written in its
    add_argument calls."""
    src = read(os.path.join("ser2netweb", "cli.py"))
    out = []
    for m in re.finditer(r"add_argument\(\s*\"(--[a-z][a-z-]*)\"(.*?)\)\n", src, re.S):
        if "argparse.SUPPRESS" not in m.group(2):
            out.append(m.group(1))
    return out


def anchors(markdown: str) -> set[str]:
    """GitHub's anchors for the headings of a Markdown text: lower case, punctuation
    (emoji too) dropped, spaces as hyphens; a repeated one gets -1, -2 ..."""
    seen: dict[str, int] = {}
    out = set()
    fence = False
    for line in markdown.splitlines():
        if line.startswith("```"):
            fence = not fence
        if fence or not line.startswith("#"):
            continue
        text = line.lstrip("#").strip()
        slug = re.sub(r"[^\w\- ]", "", text.lower().replace("`", "")).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def test_cli_options():
    opts = cli_options()
    assert "--data-dir" in opts and "--reload" in opts and "--service" not in opts, opts
    for readme in READMES:
        text = read(readme)
        missing = [o for o in opts if not re.search(re.escape(o) + r"(?![a-z-])", text)]
        assert not missing, f"{readme} doesn't name the command-line options {missing}"
    print(f"both READMEs name every command-line option ({len(opts)})  OK")


def test_guides_linked():
    for readme in READMES:
        text = read(readme)
        missing = [d for d in DOCS if f"]({d}" not in text]
        assert not missing, f"{readme} links no {missing}"
    print(f"both READMEs link every guide in docs/ ({len(DOCS)})  OK")


def test_links():
    broken = []
    for rel in CHECKED:
        text = read(rel)
        base = os.path.dirname(rel)
        for target in LINK.findall(text):
            if re.match(r"[a-z]+:", target):
                continue  # an URL (https:, mailto:)
            path, _sep, anchor = target.partition("#")
            dest = os.path.normpath(os.path.join(base, path)) if path else rel
            if path and not os.path.exists(os.path.join(ROOT, dest)):
                broken.append(f"{rel}: {target} (no {dest})")
            elif anchor and dest.endswith(".md") and anchor not in anchors(read(dest)):
                broken.append(f"{rel}: {target} (no heading #{anchor} in {dest})")
    assert not broken, "broken links:\n  " + "\n  ".join(broken)
    print(f"every relative link in {len(CHECKED)} files leads somewhere  OK")


def main():
    test_cli_options()
    test_guides_linked()
    test_links()
    print("\nPASS: the READMEs and docs/ point at what exists")


if __name__ == "__main__":
    main()
