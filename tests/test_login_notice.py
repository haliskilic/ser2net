"""The sign-in notice (Settings -> Web UI access -> Sign-in notice).

Checks:
  * admin_ui.login_notice is plain text (escaped, line breaks kept, control characters
    left out, at most LOGIN_NOTICE_MAX characters) and login_notice_url an http(s)
    address; a hand-edited value that can't be used is cut or left out at load with a
    note (validate() never refuses it); configs without them show no notice;
  * the sign-in page shows it on every render (the page, a wrong password's 401) and the
    first-run setup page too; open-access visitors get a "Notice" link in the top bar;
  * only an admin can change it, the form refuses what can't be used, and a change is
    audited.

Run: python3 tests/test_login_notice.py
"""
import os

from _server import Browser, Server

from ser2netweb.config import (
    LOGIN_NOTICE_MAX, AppConfig, clean_login_notice, login_notice_problem,
    login_notice_url_problem,
)
from ser2netweb.web.auth import hash_password

PW = "notice-pass-1"
USERS = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
          "pwd_version": 1},
         {"username": "op", "password_hash": hash_password(PW), "role": "operator",
          "pwd_version": 1}]


def test_values():
    assert clean_login_notice(" a\r\nb\rc\x00\x1b[31m\td \n") == "a\nb\nc[31m\td"
    assert login_notice_problem("x" * LOGIN_NOTICE_MAX) is None
    assert login_notice_problem("x" * (LOGIN_NOTICE_MAX + 1))
    assert login_notice_problem(("a\r\n" * 700)) is None, "CRLF counts as one character"
    assert login_notice_problem(5)
    for good in ("", "  ", "https://example.com/privacy", "http://intranet/kvkk?lang=tr#x",
                 "https://[2001:db8::1]:8443/notice"):
        assert login_notice_url_problem(good) is None, good
    for bad in ("javascript:alert(1)", "ftp://example.com/x", "https://", "example.com",
                "https://user:pw@example.com/", "https://exa mple.com/", "https://x:port/",
                "https://x/\u0007", "https://x/" + "a" * 600, 3):
        assert login_notice_url_problem(bad), bad
    cfg = AppConfig.from_dict({})
    assert (cfg.admin_ui.login_notice, cfg.admin_ui.login_notice_url) == ("", "")
    assert not cfg.load_notes and {"login_notice", "login_notice_url"} <= set(
        cfg.to_dict()["admin_ui"])
    cfg = AppConfig.from_dict({"admin_ui": {"login_notice": "y" * 2500 + "\x07",
                                            "login_notice_url": "javascript:x"}})
    assert cfg.admin_ui.login_notice == "y" * LOGIN_NOTICE_MAX and cfg.admin_ui.login_notice_url == ""
    assert any("login_notice has 2500 characters" in n for n in cfg.load_notes), cfg.load_notes
    assert any("login_notice_url" in n for n in cfg.load_notes), cfg.load_notes
    cfg.validate()
    cfg = AppConfig.from_dict({"admin_ui": {"login_notice": ["not", "text"]}})
    assert cfg.admin_ui.login_notice == "" and any("not text" in n for n in cfg.load_notes)
    print("sign-in notice: plain text up to the limit, http(s) link; unusable hand-edited "
          "values cut or left out at load with a note  OK")


def test_pages():
    with Server({"users": USERS}) as srv:
        b = Browser(srv.base)
        _, _, page = b.get("/login")
        assert 'id="notice"' not in page, "no notice unless one is set"
        b.login("admin", PW)
        _, _, page = b.get("/settings")
        assert 'action="/settings/login-notice"' in page and 'name="login_notice"' in page
        text = "Bu sistem <b>kayıt</b> tutar.\nGiriş, IP adresi & kullanıcı adı yazılır."
        st, _, page = b.post("/settings/login-notice", {
            "login_notice": text.replace("\n", "\r\n"),
            "login_notice_url": "https://example.com/kvkk"})
        assert "Sign-in notice saved." in page, page[:400]
        saved = srv.saved_config()["admin_ui"]
        assert saved["login_notice"] == text and saved["login_notice_url"] == "https://example.com/kvkk"
        escaped = ("Bu sistem &lt;b&gt;kayıt&lt;/b&gt; tutar.\nGiriş, IP adresi &amp; kullanıcı "
                   "adı yazılır.")
        for st_want, (st, _, page) in ((200, Browser(srv.base).get("/login")),
                                       (401, Browser(srv.base).post("/login", {
                                           "username": "admin", "password": "wrong"}))):
            assert st == st_want, st
            assert f'<p class="notice-text">{escaped}</p>' in page, page[:2000]
            assert ('<a href="https://example.com/kvkk" target="_blank" '
                    'rel="noopener noreferrer">Read the full notice</a>') in page
        # what can't be used is refused at edit time, and only an admin may change it
        for form, error in (({"login_notice": "x" * (LOGIN_NOTICE_MAX + 1)}, "at most 2000"),
                            ({"login_notice": "ok", "login_notice_url": "javascript:alert(1)"},
                             "must be an http:// or https:// address")):
            _, _, page = b.post("/settings/login-notice", form)
            assert error in page, page[:400]
        assert srv.saved_config()["admin_ui"]["login_notice"] == text
        op = Browser(srv.base)
        op.login("op", PW)
        st, _, _ = op.post("/settings/login-notice", {"login_notice": ""})
        assert st == 403 and srv.saved_config()["admin_ui"]["login_notice"] == text
        # open access: a visitor without a session gets a link to it in the top bar
        b.post("/settings/access", {"anonymous_role": "viewer", "session_minutes": "480"})
        _, _, page = Browser(srv.base).get("/")
        assert ('<a href="https://example.com/kvkk" target="_blank" rel="noopener noreferrer">'
                'Notice</a>') in page, page[:1500]
        b.post("/settings/login-notice", {"login_notice": "Only text", "login_notice_url": ""})
        _, _, page = Browser(srv.base).get("/")
        assert '<a href="/login#notice">Notice</a>' in page
        _, _, page = b.get("/")
        assert ">Notice</a>" not in page, "signed in: no link"
        st, _, page = b.post("/settings/login-notice", {"login_notice": "", "login_notice_url": ""})
        assert "Sign-in notice removed." in page
        _, _, page = Browser(srv.base).get("/login")
        assert 'id="notice"' not in page
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = fh.read()
        assert (f"\tlogin_notice_set\t{len(text)} characters, link https://example.com/kvkk"
                "\tadmin") in audit, audit
        assert "\tlogin_notice_set\tnone, link none\tadmin" in audit, audit
    print("pages: the notice on the sign-in page (also a 401), escaped, line breaks kept, "
          "its link; open-access visitors get a link; admin only, refused when unusable, "
          "audited  OK")

    # first-run setup shows it too (a config.json provisioned before the first account)
    with Server({"admin_ui": {"login_notice": "Kurulum bildirimi"}}) as srv:
        _, _, page = Browser(srv.base).get("/setup")
        assert '<p class="notice-text">Kurulum bildirimi</p>' in page, page[:1500]
    print("first-run setup page: the notice  OK")


def main():
    test_values()
    test_pages()
    print("\nPASS: sign-in notice")


if __name__ == "__main__":
    main()
