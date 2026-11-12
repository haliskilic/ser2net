"""Saving a mapping that then doesn't start says so where the form was.

The save answered as if all was well even when the mapping could not start (its port held
by another program - another ser2net instance on the machine - or its TLS files missing):
only the dashboard row showed it. Checks:
  * a mapping saved on a port another socket listens on: kept in config.json, and the
    answer (instead of an empty one that closes the form) says it didn't start, with the
    error and, for a taken port, what to do; the table is refreshed and the row says error;
  * a mapping that is saved stopped (or starts) closes the form as before;
  * which errors count as a taken port (Linux, macOS, Windows texts).

Calls the web app in this process. Run: python3 tests/test_save_start_result.py
"""
from __future__ import annotations

import socket

from _asgi import App, cookies, header

from app.web import auth
from app.web.routes.mappings import _port_taken

PW = "save-start-pass-1"
CSRF = "save-start-csrf"


def _sign_in(a: App) -> dict:
    st, h, _ = a.call("POST", "/login", fields={"username": "admin", "password": PW,
                                                "_csrf": CSRF},
                      headers={"Cookie": f"ser2net_csrf={CSRF}"})
    assert st == 303, st
    jar = f"ser2net_csrf={CSRF}; ser2net_session={cookies(h)['ser2net_session'][0]}"
    return {"Cookie": jar, "X-CSRF-Token": CSRF, "HX-Request": "true"}


def form(name: str, port: int, enabled: bool) -> dict:
    return {"name": name, "kind": "net", "enabled": "on" if enabled else "",
            "network_mode": "server", "network_protocol": "raw",
            "network_bind_ip": "127.0.0.1", "network_port": str(port),
            "serial_port": "/dev/ser2net-test-missing", "serial_baudrate": "9600"}


def test_save() -> None:
    users = [{"username": "admin", "password_hash": auth.hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    with App({"users": users}) as a, socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        h = _sign_in(a)
        st, hdrs, body = a.call("POST", "/api/mappings/save", fields=form("Line <1>", port, True),
                                headers=h)
        assert st == 200 and header(hdrs, "hx-trigger") == "refreshMappings", (st, hdrs)
        assert 'class="banner warn"' in body and "“Line &lt;1&gt;” is saved, but it didn’t start:" \
            in body, body
        assert "listens on that port: give the mapping a port of its own." in body, body
        saved = [m for m in a.state.config.mappings if m.name == "Line <1>"]
        assert len(saved) == 1 and a.state.store.load().mappings[0].name == "Line <1>"
        status = a.state.supervisor.status(saved[0].id)
        assert status is not None and status["state"] == "error", status
        assert "mapping saved: Line <1>" in a.lines
        # saved stopped (on a port of its own: this instance's mappings may not share one):
        # the form closes as before
        st, hdrs, body = a.call("POST", "/api/mappings/save", fields=form("Off", port + 1, False),
                                headers=h)
        assert st == 200 and body == "" and header(hdrs, "hx-trigger") == "refreshMappings", \
            (st, body[:300])
    print("mapping save: a mapping that didn't start is kept and the answer says why (a taken "
          "port: what to do); one saved stopped closes the form as before  OK")


def test_port_taken() -> None:
    linux = ("[Errno 98] error while attempting to bind on address ('0.0.0.0', 4001): "
             "[errno 98] address already in use")
    mac = "[Errno 48] error while attempting to bind on address ('0.0.0.0', 4001): address " \
          "already in use"
    windows = ("[Errno 10048] error while attempting to bind on address ('0.0.0.0', 4001): "
               "only one usage of each socket address (protocol/network address/port) is "
               "normally permitted")
    for text in (linux, mac, windows):
        assert _port_taken(text), text
    for text in ("[Errno 13] error while attempting to bind on address ('0.0.0.0', 502): "
                 "permission denied", "TLS certificate not found: /data/cert.pem", ""):
        assert not _port_taken(text), text
    print("a taken port: the Linux, macOS and Windows errors; others aren't  OK")


def main() -> None:
    test_save()
    test_port_taken()
    print("\nPASS: a mapping save says when the mapping didn't start")


if __name__ == "__main__":
    main()
