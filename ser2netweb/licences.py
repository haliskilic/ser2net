"""What the web UI says about ser2netweb's licences (the footer and the /licenses page).

ser2netweb is AGPL-3.0-only or under a commercial licence (NOTICE). AGPL-3.0 section 13 asks
a modified version that users reach over a network to offer them its source: the footer
of every page links source_url(), which SER2NETWEB_SOURCE_URL changes for such a copy (an
internal mirror of an air-gapped site, a fork). texts() are the licence texts this copy
carries — LICENSE, NOTICE, COMMERCIAL_LICENSE.md and the third-party notices from a
source tree or the Docker image, the generated THIRD-PARTY-LICENSES.txt in a standalone
executable — which the /licenses page shows to signed-in users (they name the versions of
the bundled components, so they are not shown before a sign-in).
"""
from __future__ import annotations

import glob
import os
import re
import sys

from . import SOURCE_URL, envvars

SOURCE_URL_ENV = "SER2NETWEB_SOURCE_URL"  # (or SER2NET_SOURCE_URL, its 2.9.x name)
# an http(s) address without spaces, quotes or angle brackets (it goes into an href)
_URL = re.compile(r"https?://[^\s\"'<>`]{1,2000}", re.I)
# the folder that holds ser2netweb/ (a source tree, the Docker image's /app; a standalone
# executable's unpack folder)
_ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source_url(env=None) -> str:
    """The address of this copy's source code: SER2NETWEB_SOURCE_URL when it is an http://
    or https:// address, else ser2netweb's own."""
    value = envvars.text(SOURCE_URL_ENV, env)
    return value if _URL.fullmatch(value) else SOURCE_URL


def source_url_note(env=None) -> str:
    """The start-up warning for a SER2NETWEB_SOURCE_URL that is not used, else ""."""
    value = envvars.text(SOURCE_URL_ENV, env)
    if value and not _URL.fullmatch(value):
        return (f"WARNING: {envvars.source(SOURCE_URL_ENV, env)} is not an http:// or https:// "
                f"address; the web "
                f"UI links {SOURCE_URL} as the source code")
    return ""


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def texts(root: str | None = None) -> list[tuple[str, str, str]]:
    """(anchor, title, text) of each licence text this copy of ser2netweb carries."""
    root = _ROOT if root is None else root
    out = []
    for anchor, title, name in (
            ("license", "ser2netweb — GNU Affero General Public License v3.0 (LICENSE)", "LICENSE"),
            ("notice", "NOTICE", "NOTICE"),
            ("commercial", "Commercial licence (COMMERCIAL_LICENSE.md)", "COMMERCIAL_LICENSE.md"),
            ("third-party", "Third-party components (THIRD-PARTY-NOTICES.md)",
             "THIRD-PARTY-NOTICES.md"),
            ("bundled", "Licences of every bundled component (THIRD-PARTY-LICENSES.txt)",
             "THIRD-PARTY-LICENSES.txt")):
        text = _read(os.path.join(root, name))
        if text is not None:
            out.append((anchor, title, text))
    # the browser libraries the pages load (htmx, xterm.js): their texts, which the
    # generated THIRD-PARTY-LICENSES.txt of an executable has already
    if not any(anchor == "bundled" for anchor, _t, _x in out):
        for path in sorted(glob.glob(os.path.join(root, "tools", "licenses", "npm", "*.txt"))):
            text = _read(path)
            if text is not None:
                name = os.path.splitext(os.path.basename(path))[0]
                out.append((f"npm-{name}", f"{name} (in the web UI's static files)", text))
    return out
