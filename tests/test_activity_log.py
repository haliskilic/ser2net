"""How much of the web UI's activity is logged, and where.

Checks:
  * SER2NET_LOG_STDOUT=off writes the log lines only to all.log (not to stdout, which the
    journal or Docker's container log keep a copy of); on / unset as before; another value
    keeps stdout and says so; when all.log can't be opened, stdout stays;
  * admin_ui.request_log: "all" (default) as before, "changes" logs only requests that
    change something and refused or failed ones, "off" none — also of the requests answered
    before any handler (no session: 401, or sent to the sign-in page); sign-ins and the
    audit records are logged whatever it says; an unknown value loads as "all" with a note;
  * Settings -> Audit log saves it (admin only, audited).

Run: python3 tests/test_activity_log.py
"""
import logging
import os
import re
import shutil
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from unittest import mock

from _server import Browser, Server

from ser2netweb import state as st
from ser2netweb.config import AppConfig, ConfigStore
from ser2netweb.web.auth import hash_password
from ser2netweb.web.server import logs_request

PW = "activity-pass-1"


def _close_loggers():
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _handlers(env: dict, data: str):
    with mock.patch.dict(os.environ, env):
        if "SER2NET_LOG_STDOUT" not in env:
            os.environ.pop("SER2NET_LOG_STDOUT", None)
        state = st.AppState(ConfigStore(os.path.join(data, "config.json")), AppConfig())
    # (the console copy is written by a thread of its own: its handler's target)
    kinds = sorted("file" if isinstance(h, RotatingFileHandler) else
                   "stdout" if getattr(getattr(h, "target", h), "stream", None) is sys.stdout
                   else "other" for h in state._logger.handlers)
    return state, kinds


def test_stdout_switch():
    assert st.log_stdout({}) == (True, "")
    for value in ("on", "ON", "1", "yes", "true", " on "):
        assert st.log_stdout({"SER2NET_LOG_STDOUT": value}) == (True, ""), value
    for value in ("off", "Off", "0", "no", "false"):
        assert st.log_stdout({"SER2NET_LOG_STDOUT": value}) == (False, ""), value
    on, problem = st.log_stdout({"SER2NET_LOG_STDOUT": "warnings"})
    assert on and "is not on or off" in problem, problem
    d = tempfile.mkdtemp(prefix="s2n_actlog_")
    try:
        _state, kinds = _handlers({}, d)
        assert kinds == ["file", "stdout"], kinds
        _close_loggers()
        state, kinds = _handlers({"SER2NET_LOG_STDOUT": "off"}, d)
        assert kinds == ["file"], kinds
        state.log("only in all.log")
        _close_loggers()
        with open(os.path.join(d, "all.log"), encoding="utf-8") as fh:
            assert "only in all.log" in fh.read()
        state, kinds = _handlers({"SER2NET_LOG_STDOUT": "sometimes"}, d)
        assert kinds == ["file", "stdout"], kinds
        _close_loggers()
        with open(os.path.join(d, "all.log"), encoding="utf-8") as fh:
            assert "SER2NET_LOG_STDOUT='sometimes' is not on or off" in fh.read()
        # all.log can't be opened (a folder in its place): stdout stays, or nothing would
        blocked = os.path.join(d, "blocked")
        os.makedirs(os.path.join(blocked, "all.log"))
        _state, kinds = _handlers({"SER2NET_LOG_STDOUT": "off"}, blocked)
        assert kinds == ["stdout"], kinds
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)
    print("SER2NET_LOG_STDOUT: off = all.log only; on / unset / unknown (with a warning) = "
          "stdout too; stdout stays when all.log can't be opened  OK")


def test_request_log_modes():
    cases = [  # (method, path, status): logged by all, changes, off
        ("GET", "/settings", 200, (True, False, False)),
        ("GET", "/api/status", 200, (False, False, False)),     # an automatic refresh
        ("GET", "/static/app.css", 200, (False, False, False)),
        ("POST", "/settings/access", 200, (True, True, False)),
        ("POST", "/api/mappings/x/start", 303, (True, True, False)),
        ("DELETE", "/api/mappings/x", 204, (True, True, False)),
        ("GET", "/settings/backups/nope", 404, (True, True, False)),
        ("GET", "/api/status", 401, (False, True, False)),
        ("HEAD", "/", 200, (True, False, False)),
    ]
    for method, path, status, want in cases:
        got = tuple(logs_request(m, method, path, status) for m in ("all", "changes", "off"))
        assert got == want, (method, path, status, got, want)
    assert AppConfig().admin_ui.request_log == "all"
    assert AppConfig.from_dict({}).to_dict()["admin_ui"]["request_log"] == "all"
    cfg = AppConfig.from_dict({"admin_ui": {"request_log": "changes"}})
    assert cfg.admin_ui.request_log == "changes" and not cfg.load_notes
    for bad in ("verbose", "", None, 3, ["off"]):
        cfg = AppConfig.from_dict({"admin_ui": {"request_log": bad}})
        assert cfg.admin_ui.request_log == "all", bad
        # (the range check's note, or the typed reader's for a value that is no text)
        assert any("admin_ui.request_log" in n and ("using all" in n or '"all" is used' in n)
                   for n in cfg.load_notes), cfg.load_notes
        cfg.validate()
    print("request_log: all / changes / off pick the right requests; an unknown value "
          "loads as all with a note  OK")


def _all_log(srv) -> str:
    with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
        return fh.read()


def _http_lines(text: str) -> list:
    return re.findall(r"HTTP \S+ (\w+) (\S+) -> (\d+)", text)


def test_settings_and_live_server():
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1},
             {"username": "op", "password_hash": hash_password(PW), "role": "operator",
              "pwd_version": 1}]
    with Server({"users": users}, env={"SER2NET_LOG_STDOUT": "off"}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        b.get("/settings")
        assert ("GET", "/settings", "200") in _http_lines(_all_log(srv))
        assert "HTTP " not in srv.output() and "login: admin" not in srv.output(), \
            srv.output()[-1500:]
        _, _, page = b.get("/settings")
        assert 'action="/settings/request-log"' in page and '<option value="all" selected' in page
        op = Browser(srv.base)
        op.login("op", PW)
        st_, _, _ = op.post("/settings/request-log", {"request_log": "off"})
        assert st_ == 403 and srv.saved_config()["admin_ui"]["request_log"] == "all"
        st_, _, page = b.post("/settings/request-log", {"request_log": "loud"})
        assert "Choose which web UI requests all.log records." in page
        st_, _, page = b.post("/settings/request-log", {"request_log": "changes"})
        assert "Saved: which web UI requests all.log records." in page, page[:300]
        assert srv.saved_config()["admin_ui"]["request_log"] == "changes"
        mark = len(_all_log(srv))
        b.get("/settings")
        b.get("/settings/backups/config-20000101T000000Z.json")   # 404
        b.post("/settings/node-name", {"node_name": "line-1"})
        anon = Browser(srv.base)
        assert anon.get("/api/status")[0] == 401      # refused before any handler
        lines = _http_lines(_all_log(srv)[mark:])
        assert ("GET", "/api/status", "401") in lines, lines
        assert ("GET", "/settings", "200") not in lines, lines
        assert ("GET", "/settings/backups/config-20000101T000000Z.json", "404") in lines, lines
        assert ("POST", "/settings/node-name", "303") in lines, lines
        b.post("/settings/request-log", {"request_log": "off"})
        mark = len(_all_log(srv))
        b.post("/settings/node-name", {"node_name": "line-2"})
        assert anon.get("/settings")[0] == 303        # sent to the sign-in page by the gate
        Browser(srv.base).post("/login", {"username": "admin", "password": "wrong"})
        op.login("op", PW)
        text = _all_log(srv)[mark:]
        assert not _http_lines(text), text
        # sign-ins, failed sign-ins and the audit records are logged whatever it says
        assert "failed login for 'admin'" in text and "login: op" in text, text
        assert "AUDIT 127.0.0.1 node_name_set line-2 by admin" in text, text
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = fh.read()
        assert "\trequest_log_set\tchanges\tadmin" in audit and "\trequest_log_set\toff\tadmin" \
            in audit, audit
    print("live: SER2NET_LOG_STDOUT=off keeps stdout empty of log lines; Settings -> Audit "
          "log sets the request log (admin only, audited); changes / off log as described  OK")


def main():
    test_stdout_switch()
    test_request_log_modes()
    test_settings_and_live_server()
    print("\nPASS: web UI activity logging (stdout copy, request log)")


if __name__ == "__main__":
    main()
