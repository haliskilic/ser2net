"""The session lifetime setting (session_timeout_s) takes effect.

Before v2.8 the value was stored in config.json but sessions always lasted 8 hours.
Checks: the cookie's Max-Age and the signed token's expiry follow the setting, the
admin can change it under Settings -> Web UI access (new sign-ins use it), bad input
is refused, and odd values in an old config.json are clamped instead of failing.

Run: python3 tests/test_session_timeout.py
"""
import base64
import json
import time

from _server import Browser, Server, ui_cookie

from app.config import (SESSION_TIMEOUT_DEFAULT_S, SESSION_TIMEOUT_MAX_S, SESSION_TIMEOUT_MIN_S,
                        AppConfig, ConfigError)
from app.web.auth import hash_password

PASSWORD = "session-pass-1"


def test_config_values():
    load = lambda v: AppConfig.from_dict({"session_timeout_s": v}).session_timeout_s  # noqa: E731
    assert load(None) == SESSION_TIMEOUT_DEFAULT_S == 8 * 3600
    assert load("abc") == SESSION_TIMEOUT_DEFAULT_S
    assert load(10) == SESSION_TIMEOUT_MIN_S
    assert load(10**9) == SESSION_TIMEOUT_MAX_S
    assert load("3600") == 3600 and load(1800.7) == 1800
    cfg = AppConfig()
    cfg.session_timeout_s = 59
    try:
        cfg.validate()
    except ConfigError:
        pass
    else:
        raise AssertionError("a 59 s session lifetime must be refused")
    print("config: unreadable -> 8 h default, out-of-range clamped, validate() checks the range  OK")


def session_expiry(browser):
    c = browser.cookie("ser2net_session")
    payload = c.value.rsplit(".", 1)[0]
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return data["exp"] - time.time()


def max_age(headers, port):
    name = ui_cookie("ser2net_session", port)
    cookie = next(c for c in headers.get_all("Set-Cookie") if c.startswith(name + "="))
    part = next(p for p in cookie.split(";") if p.strip().lower().startswith("max-age="))
    return int(part.split("=", 1)[1])


def main():
    test_config_values()
    config = {"users": [{"username": "admin", "password_hash": hash_password(PASSWORD),
                         "role": "admin", "pwd_version": 1}],
              "session_timeout_s": 600}
    with Server(config) as srv:
        b = Browser(srv.base)
        st, h, _ = b.post("/login", {"username": "admin", "password": PASSWORD})
        assert st == 303 and max_age(h, srv.port) == 600, h.get("Set-Cookie")
        assert 590 <= session_expiry(b) <= 600, session_expiry(b)
        print("sign-in: cookie Max-Age and token expiry = configured 600 s  OK")

        st, _, page = b.get("/settings")
        assert 'name="session_minutes"' in page and 'value="10"' in page
        st, _, page = b.post("/settings/access", {"require_login": "on", "anonymous_role": "viewer",
                                                  "session_minutes": "1"})
        assert "Session lifetime must be between 5 minutes and 30 days" in page, page[:400]
        assert srv.saved_config()["session_timeout_s"] == 600
        st, _, page = b.post("/settings/access", {"require_login": "on", "anonymous_role": "viewer",
                                                  "session_minutes": "45"})
        assert st == 200 and srv.saved_config()["session_timeout_s"] == 2700, page[:400]
        assert "web UI session lifetime set to 45 min" in srv.output()
        print("Settings -> Web UI access: 1 min refused, 45 min saved  OK")

        b2 = Browser(srv.base)
        st, h, _ = b2.post("/login", {"username": "admin", "password": PASSWORD})
        assert st == 303 and max_age(h, srv.port) == 2700, h.get("Set-Cookie")
        print("next sign-in lasts 45 min  OK")
    print("\nPASS: session lifetime setting")


if __name__ == "__main__":
    main()
