"""The web UI says under which licence ser2net runs and where its source code is.

Checks:
  * every page has a footer with the licence and a link to the source code (AGPL-3.0
    section 13); the sign-in and setup pages leave out the version and the licences
    page, which signed-in pages link;
  * SER2NET_SOURCE_URL points that link elsewhere (a modified copy's own source), and a
    value that is not an http(s) address is not used (start-up warning);
  * /licenses needs a sign-in (any role) and shows the licence texts this copy carries:
    LICENSE, NOTICE, the commercial licence, the third-party notices and the browser
    libraries' licences (a standalone executable: its THIRD-PARTY-LICENSES.txt).

Run: python3 tests/test_licenses_page.py
"""
import os
import tempfile

from _server import Browser, Server

from app import SOURCE_URL, licences
from app.web.auth import hash_password

PW = "licence-pass-1"
USERS = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
          "pwd_version": 1},
         {"username": "viewer", "password_hash": hash_password(PW), "role": "viewer",
          "pwd_version": 1}]
MIRROR = "https://git.example.com/plant/ser2net-fork"


def test_source_url():
    assert licences.source_url({}) == SOURCE_URL
    assert licences.source_url({"SER2NET_SOURCE_URL": f" {MIRROR} "}) == MIRROR
    for bad in ("git.example.com/x", "javascript:alert(1)", 'https://x.example.com/"><b>',
                "ftp://example.com/src", "https://"):
        assert licences.source_url({"SER2NET_SOURCE_URL": bad}) == SOURCE_URL, bad
        assert "SER2NET_SOURCE_URL" in licences.source_url_note({"SER2NET_SOURCE_URL": bad})
    assert licences.source_url_note({}) == "" and \
        licences.source_url_note({"SER2NET_SOURCE_URL": MIRROR}) == ""
    print("SER2NET_SOURCE_URL: an http(s) address is used, anything else warned about  OK")


def test_texts():
    names = [anchor for anchor, _title, _text in licences.texts()]
    assert names[:4] == ["license", "notice", "commercial", "third-party"], names
    assert any(n.startswith("npm-") for n in names), names  # htmx, xterm.js
    text = dict((a, t) for a, _title, t in licences.texts())["license"]
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    with tempfile.TemporaryDirectory() as frozen:  # a standalone executable's files
        with open(os.path.join(frozen, "THIRD-PARTY-LICENSES.txt"), "w", encoding="utf-8") as fh:
            fh.write("Third-party licenses of the ser2net standalone executable\n")
        assert [a for a, _t, _x in licences.texts(frozen)] == ["bundled"]
    print("the licence texts of a source tree and of a standalone executable  OK")


def test_pages():
    with Server({"users": USERS}, env={"SER2NET_SOURCE_URL": MIRROR}) as srv:
        b = Browser(srv.base)
        st, _h, page = b.get("/login")
        assert st == 200 and 'class="site-footer"' in page and f'href="{MIRROR}"' in page
        assert "AGPL-3.0" in page and "/licenses" not in page
        st, h, _body = b.get("/licenses")
        assert st == 303 and h.get("Location") == "/login?next=%2Flicenses", (st, h)
        b.login("viewer", PW)
        st, _h, page = b.get("/")
        assert st == 200 and 'class="site-footer"' in page and 'href="/licenses"' in page
        st, _h, page = b.get("/licenses")
        assert st == 200, st
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in page and "COMMERCIAL" in page.upper()
        assert f'href="{MIRROR}"' in page and 'id="third-party"' in page
    with Server({"users": USERS}, env={"SER2NET_SOURCE_URL": "not a url"}) as srv:
        st, _h, page = Browser(srv.base).get("/login")
        assert f'href="{SOURCE_URL}"' in page
        assert "SER2NET_SOURCE_URL is not an http:// or https:// address" in srv.output()
    print("the footer on the sign-in page and the dashboard; /licenses for signed-in users  OK")


def main():
    test_source_url()
    test_texts()
    test_pages()
    print("\nPASS: licence and source code in the web UI")


if __name__ == "__main__":
    main()
