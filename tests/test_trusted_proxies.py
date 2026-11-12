"""Which connections may say who the client is (X-Forwarded-For) and that the browser used
HTTPS (X-Forwarded-Proto): trusted proxies only.

Checks:
  * nothing is trusted by default - also not 127.0.0.1 / ::1, which uvicorn trusted up to
    2.8.1: a client rotating X-Forwarded-For is limited, logged and audited under its
    own address, and all.log says (at most once an hour) that the header was ignored;
  * from a trusted proxy (an address or a network) the client is the last entry that
    isn't a trusted proxy, or the first when all are; an entry that is not an IP address
    is never taken (the address after it stands: in the end the proxy's own); ports,
    brackets and IPv4-mapped IPv6 are read; X-Forwarded-Proto gives the scheme (wss for a
    WebSocket); a proxy that isn't listed changes nothing;
  * where the list comes from: SER2NET_TRUSTED_PROXIES, else admin_ui.trusted_proxies,
    else FORWARDED_ALLOW_IPS (uvicorn's, which up to 2.8.1 was the only way); entries
    that are not addresses are left out with a start-up warning, '*' is warned about;
  * config.json: the key is optional, unusable entries are dropped with a load note (no
    new rule in validate()), a downgrade drops it;
  * Settings -> Reverse proxy -> Trusted proxies: saved, applies at once, a wrong entry
    is refused with the reason, logged and audited; set by SER2NET_TRUSTED_PROXIES, the
    field is shown read-only and a form without it keeps the stored list.

Calls the web app in this process (no socket).
Run: python3 tests/test_trusted_proxies.py
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from _asgi import App, header

from ser2netweb.config import AdminUI, AppConfig
from ser2netweb.web import auth, proxy

PW = "proxy-trust-pass-1"
CSRF = "trusted-proxies-csrf"


def _admin_cfg(**admin) -> dict:
    return {"users": [{"username": "admin", "password_hash": auth.hash_password(PW),
                       "role": "admin", "pwd_version": 1}], "admin_ui": admin}


def _form(extra: str = "") -> dict:
    return {"Cookie": f"ser2net_csrf={CSRF}" + extra}


def test_parsing() -> None:
    a = proxy._address
    assert a("1.2.3.4") == "1.2.3.4" and a(" 1.2.3.4:5678 ") == "1.2.3.4"
    assert a("[2001:db8::1]:443") == "2001:db8::1" and a("2001:DB8::1") == "2001:db8::1"
    assert a("::ffff:10.0.0.7") == "10.0.0.7" and a("[::1]") == "::1"
    for bad in ("not-an-ip <x>", "unknown", "", "1.2.3.4:x", "[::1", "[::1]x", "1.2.3",
                "ı.2.3.4", "_hidden"):
        assert a(bad) is None, bad
    trusted = proxy.Trusted(["127.0.0.1", "10.0.0.0/8", "2001:db8::/32"])
    assert "127.0.0.1" in trusted and "10.200.3.4" in trusted and "2001:db8::9" in trusted
    assert "::ffff:10.1.1.1" in trusted and "11.0.0.1" not in trusted and "x" not in trusted
    assert "1.2.3.4" in proxy.Trusted(["*"]) and not proxy.Trusted([])
    fc = proxy.forwarded_client
    assert fc(["198.51.100.7"], "10.0.0.2", trusted) == "198.51.100.7"
    # each proxy adds the address it was reached from at the end: the last untrusted one
    assert fc(["203.0.113.9, 198.51.100.7, 10.0.0.3"], "10.0.0.2", trusted) == "198.51.100.7"
    assert fc(["203.0.113.9", "198.51.100.7"], "10.0.0.2", trusted) == "198.51.100.7"
    assert fc(["10.0.0.9, 10.0.0.3"], "10.0.0.2", trusted) == "10.0.0.9"   # all proxies
    # not an address: the one after it stands
    assert fc(["not-an-ip <x>"], "10.0.0.2", trusted) == "10.0.0.2"
    assert fc(["198.51.100.7, bogus, 10.0.0.3"], "10.0.0.2", trusted) == "10.0.0.3"
    assert fc([""], "10.0.0.2", trusted) == "10.0.0.2"
    assert fc(["[2001:db8:1::5]:1234"], "10.0.0.2", proxy.Trusted(["10.0.0.0/8"])) \
        == "2001:db8:1::5"
    print("X-Forwarded-For: the last entry that isn't a trusted proxy; never a non-address  OK")


def test_sources() -> None:
    blank, listed = AdminUI(), AdminUI(trusted_proxies=["172.18.0.0/16"])
    te = proxy.trusted_entries
    assert te(blank, {}) == ([], "")
    assert te(listed, {}) == (["172.18.0.0/16"], "admin_ui.trusted_proxies")
    assert te(listed, {"SER2NET_TRUSTED_PROXIES": "127.0.0.1, ::1 10.0.0.1"}) == \
        (["127.0.0.1", "::1", "10.0.0.1"], "SER2NET_TRUSTED_PROXIES")
    assert te(blank, {"FORWARDED_ALLOW_IPS": "10.0.0.5"}) == (["10.0.0.5"], "FORWARDED_ALLOW_IPS")
    assert te(listed, {"FORWARDED_ALLOW_IPS": "10.0.0.5"})[1] == "admin_ui.trusted_proxies"
    assert te(blank, {"SER2NET_TRUSTED_PROXIES": "  "}) == ([], "")
    assert proxy.start_notes(blank, {}) == []
    notes = proxy.start_notes(blank, {"SER2NET_TRUSTED_PROXIES": "127.0.0.1 proxy.lan"})
    assert any("WARNING: SER2NET_TRUSTED_PROXIES: 'proxy.lan' left out" in n for n in notes), notes
    assert any("trusted proxies (SER2NET_TRUSTED_PROXIES): 127.0.0.1 —" in n for n in notes), notes
    notes = proxy.start_notes(blank, {"FORWARDED_ALLOW_IPS": "*"})
    assert any(n.startswith("WARNING: FORWARDED_ALLOW_IPS='*'") for n in notes), notes
    assert any("FORWARDED_ALLOW_IPS is read as the list of trusted proxies" in n for n in notes)
    print("trusted proxies: SER2NET_TRUSTED_PROXIES > Settings > FORWARDED_ALLOW_IPS; start-up "
          "lines  OK")


def test_config() -> None:
    cfg = AppConfig.from_dict({})
    assert cfg.admin_ui.trusted_proxies == [] and not cfg.load_notes
    assert cfg.to_dict()["admin_ui"]["trusted_proxies"] == []
    cfg = AppConfig.from_dict({"admin_ui": {"trusted_proxies": ["10.0.0.1", " 172.18.0.0/16 ",
                                                                "proxy.lan", 7, "*"]}})
    assert cfg.admin_ui.trusted_proxies == ["10.0.0.1", "172.18.0.0/16"], cfg.admin_ui
    assert any("left out" in n for n in cfg.load_notes), cfg.load_notes
    cfg.validate()
    for value in ("10.0.0.1", {"a": 1}, None, 5):
        cfg = AppConfig.from_dict({"admin_ui": {"trusted_proxies": value}})
        assert cfg.admin_ui.trusted_proxies == (["10.0.0.1"] if value == "10.0.0.1" else [])
        cfg.validate()
    print("config: admin_ui.trusted_proxies optional, unusable entries left out with a note  OK")


def test_middleware() -> None:
    """The ASGI layer alone: what the app behind it sees."""
    lines: list[str] = []
    state = SimpleNamespace(config=AppConfig.from_dict({}), log=lines.append)
    seen: dict = {}

    async def app(scope, receive, send):
        seen.update(client=scope["client"][0], scheme=scope["scheme"])

    mw = proxy.ProxyMiddleware(app, state)

    def through(peer, kind="http", **headers):
        scope = {"type": kind, "scheme": "ws" if kind == "websocket" else "http", "path": "/",
                 "headers": [(k.replace("_", "-").lower().encode(), v.encode())
                             for k, v in headers.items()], "client": (peer, 40000)}
        asyncio.run(mw(scope, None, None))
        return seen["client"], seen["scheme"]

    # nothing trusted: the connection's own address, the headers ignored (noted once)
    assert through("127.0.0.1", x_forwarded_for="1.2.3.4", x_forwarded_proto="https") == \
        ("127.0.0.1", "http")
    assert through("::1", x_forwarded_for="1.2.3.5") == ("::1", "http")
    assert len(lines) == 1 and "X-Forwarded-For from 127.0.0.1 is ignored" in lines[0], lines
    assert "Settings → Reverse proxy → Trusted proxies" in lines[0]
    state.config.admin_ui.trusted_proxies = ["127.0.0.1", "172.18.0.0/16"]   # at once
    assert through("127.0.0.1", x_forwarded_for="1.2.3.4", x_forwarded_proto="https") == \
        ("1.2.3.4", "https")
    assert through("172.18.0.5", x_forwarded_for="not-an-ip <x>") == ("172.18.0.5", "http")
    assert through("::ffff:172.18.0.5", x_forwarded_for="1.2.3.4") == ("1.2.3.4", "http")
    assert through("172.18.0.5", "websocket", x_forwarded_proto="https") == ("172.18.0.5", "wss")
    assert through("172.18.0.5", x_forwarded_proto="https, http") == ("172.18.0.5", "https")
    assert through("172.18.0.5", x_forwarded_proto="gopher") == ("172.18.0.5", "http")
    # a proxy that isn't listed
    assert through("192.0.2.1", x_forwarded_for="1.2.3.4", x_forwarded_proto="https") == \
        ("192.0.2.1", "http")
    os.environ["SER2NET_TRUSTED_PROXIES"] = "192.0.2.1"
    try:
        assert through("192.0.2.1", x_forwarded_for="1.2.3.4") == ("1.2.3.4", "http")
        assert through("127.0.0.1", x_forwarded_for="1.2.3.4") == ("127.0.0.1", "http")
    finally:
        del os.environ["SER2NET_TRUSTED_PROXIES"]
    assert len(lines) == 1, lines   # once an hour
    print("ASGI layer: only trusted proxies set the client and the scheme (WebSockets too)  OK")


def _sign_in(a: App, name: str, password: str, xff: str | None = None):
    headers = _form()
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    return a.call("POST", "/login", fields={"username": name, "password": password,
                                            "_csrf": CSRF}, headers=headers)


def test_sign_in_limit() -> None:
    with App(_admin_cfg()) as a:
        st, h, _ = _sign_in(a, "admin", PW)
        assert st == 303, st
        session = header(h, "set-cookie").split(";")[0]
        limit = a.state.rate_limiter.max_attempts
        # default: a rotating X-Forwarded-For from 127.0.0.1 changes nothing
        codes = [_sign_in(a, f"user{i}", "wrong", xff=f"10.0.0.{i}")[0] for i in range(limit + 2)]
        assert codes == [401] * limit + [429, 429], codes
        assert "failed login for '(unknown user)' from 127.0.0.1" in a.lines
        assert sum("X-Forwarded-For from 127.0.0.1 is ignored" in ln for ln in a.lines) == 1

        # trusted (Settings): every client its own budget, logged and audited under its address
        st, _, page = a.call("POST", "/settings/proxy",
                             fields={"_csrf": CSRF, "trusted_proxies": "127.0.0.1 bad.example"},
                             headers=_form("; " + session))
        assert st == 400 and "“bad.example” is not an IP address or network" in page, st
        assert a.state.config.admin_ui.trusted_proxies == []
        st, _, page = a.call("POST", "/settings/proxy",
                             fields={"_csrf": CSRF, "trusted_proxies": "127.0.0.1, 127.0.0.1 ::1"},
                             headers=_form("; " + session))
        assert st == 303, st   # saved: back to the page (Post/Redirect/Get)
        assert a.state.config.admin_ui.trusted_proxies == ["127.0.0.1", "::1"]
        saved = a.state.store.load().admin_ui.trusted_proxies
        assert saved == ["127.0.0.1", "::1"], saved
        assert "reverse proxy settings: trusted proxies 127.0.0.1 ::1" in a.lines, a.lines[-3:]
        assert any(r["action"] == "reverse_proxy_set" and "trusted proxies" in r["detail"]
                   for r in a.state.read_audit())
        codes = [_sign_in(a, f"user{i}", "wrong", xff=f"198.51.100.{i}")[0]
                 for i in range(limit + 2)]
        assert codes == [401] * (limit + 2), codes
        assert "failed login for '(unknown user)' from 198.51.100.3" in a.lines
        assert any(r["ip"] == "198.51.100.3" and r["action"] == "login_failed"
                   for r in a.state.read_audit())
        # a proxy that passes on what the client sent: the address it adds counts
        st = _sign_in(a, "other", "wrong", xff="198.51.100.200, 203.0.113.5")[0]
        assert st == 401 and "failed login for '(unknown user)' from 203.0.113.5" in a.lines
        # not an address: the proxy's own, which is limited already
        assert _sign_in(a, "other2", "wrong", xff="not-an-ip <x>")[0] == 429
        st, h, _ = _sign_in(a, "admin", PW, xff="2001:db8:5::7")
        assert st == 303 and "login: admin (admin, local) from 2001:db8:5::7" in a.lines
        print("sign-in limit / logs / audit: the proxy's X-Forwarded-For only once it is "
              "trusted (Settings, at once)  OK")


def test_env_setting() -> None:
    os.environ["SER2NET_TRUSTED_PROXIES"] = "127.0.0.1"
    try:
        with App(_admin_cfg(trusted_proxies=["10.0.0.0/8"])) as a:
            st, h, _ = _sign_in(a, "admin", PW, xff="198.51.100.9")
            assert st == 303 and "login: admin (admin, local) from 198.51.100.9" in a.lines
            session = header(h, "set-cookie").split(";")[0]
            st, _, page = a.call("GET", "/settings", headers=_form("; " + session))
            assert st == 200 and '<input type="text" value="127.0.0.1" disabled>' in page
            assert "Set by <span class=\"mono\">SER2NET_TRUSTED_PROXIES</span>" in page
            # the form has no such field then: the stored list stays
            st, _, page = a.call("POST", "/settings/proxy", fields={"_csrf": CSRF},
                                 headers=_form("; " + session))
            assert st == 303 and a.state.config.admin_ui.trusted_proxies == ["10.0.0.0/8"]
    finally:
        del os.environ["SER2NET_TRUSTED_PROXIES"]
    os.environ["FORWARDED_ALLOW_IPS"] = "127.0.0.1"
    try:
        with App(_admin_cfg()) as a:
            st, h, _ = _sign_in(a, "admin", PW, xff="198.51.100.10")
            assert st == 303 and "login: admin (admin, local) from 198.51.100.10" in a.lines
            session = header(h, "set-cookie").split(";")[0]
            st, _, page = a.call("GET", "/settings", headers=_form("; " + session))
            assert "FORWARDED_ALLOW_IPS</span> gives <span class=\"mono\">127.0.0.1</span>" in page
            assert 'name="trusted_proxies" value=""' in page
    finally:
        del os.environ["FORWARDED_ALLOW_IPS"]
    print("SER2NET_TRUSTED_PROXIES wins and is shown read-only; FORWARDED_ALLOW_IPS still works  OK")


def main() -> None:
    for name in ("SER2NET_TRUSTED_PROXIES", "FORWARDED_ALLOW_IPS", "SER2NET_PUBLIC_URL"):
        os.environ.pop(name, None)
    test_parsing()
    test_sources()
    test_config()
    test_middleware()
    test_sign_in_limit()
    test_env_setting()
    print("\nPASS: trusted proxies")


if __name__ == "__main__":
    main()
