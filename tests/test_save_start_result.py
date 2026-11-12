"""Saving or starting a mapping that then doesn't start says so.

The save answered as if all was well even when the mapping could not start (its port held
by another program - another ser2net instance on the machine - or its TLS files missing):
only the dashboard row showed it. Checks:
  * a mapping saved on a port another socket listens on: kept in config.json, the form
    closes (empty answer, the table refreshed) and the answer's notice (X-Notice, the
    dashboard's feedback area) warns that it didn't start, with the reason in the page's
    language and the error as its tooltip, what to do about a taken port and that it is
    tried again (the supervisor's bind retry); the row says error;
  * Start on such a mapping says the same; a mapping saved stopped just says "saved";
  * which errors count as a taken port (Linux, macOS, Windows texts).

Calls the web app in this process. Run: python3 tests/test_save_start_result.py
"""
from __future__ import annotations

import json
import socket
import urllib.parse

from _asgi import App, cookies, header

from app.web import auth
from app.web.routes.mappings import _port_taken

PW = "save-start-pass-1"
CSRF = "save-start-csrf"
# a taken port in the OS's own words: POSIX, Windows (WSAEADDRINUSE)
IN_USE = ("address already in use", "only one usage of each socket address")


def _sign_in(a: App) -> dict:
    st, h, _ = a.call("POST", "/login", fields={"username": "admin", "password": PW,
                                                "_csrf": CSRF},
                      headers={"Cookie": f"ser2net_csrf={CSRF}"})
    assert st == 303, st
    jar = f"ser2net_csrf={CSRF}; ser2net_session={cookies(h)['ser2net_session'][0]}"
    return {"Cookie": jar, "X-CSRF-Token": CSRF, "HX-Request": "true"}


def notice(headers) -> dict:
    """The X-Notice of an answer (percent-encoded JSON)."""
    return json.loads(urllib.parse.unquote(header(headers, "x-notice")))


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
        assert st == 200 and body == "" and header(hdrs, "hx-trigger") == "refreshMappings", \
            (st, hdrs, body[:300])
        n = notice(hdrs)
        assert n["kind"] == "warn" and n["text"].startswith("“Line <1>” saved, but it could not "
                                                            "start: "), n
        assert "The port is already in use by another program" in n["text"], n
        assert "listens on that port: give the mapping a port of its own." in n["text"], n
        assert "It is started as soon as it can listen there" in n["text"], n
        assert any(w in n["detail"].lower() for w in IN_USE), ("the error itself: the tooltip",
                                                               n["detail"])
        saved = [m for m in a.state.config.mappings if m.name == "Line <1>"]
        assert len(saved) == 1 and a.state.store.load().mappings[0].name == "Line <1>"
        assert n["focus"] == f"m-{saved[0].id}-edit", n
        status = a.state.supervisor.status(saved[0].id)
        assert status is not None and status["state"] == "error" and status["retrying"], status
        assert "mapping saved: Line <1>" in a.lines
        # Start on it again: the same hints
        st, hdrs, _ = a.call("POST", f"/api/mappings/{saved[0].id}/start", headers=h)
        n = notice(hdrs)
        assert st == 200 and n["kind"] == "warn" and "could not start" in n["text"], (st, n)
        assert "give the mapping a port of its own" in n["text"] and \
            "It is started as soon as it can listen there" in n["text"], n
        # saved stopped (on a port of its own: this instance's mappings may not share one):
        # the form closes as before
        st, hdrs, body = a.call("POST", "/api/mappings/save", fields=form("Off", port + 1, False),
                                headers=h)
        assert st == 200 and body == "" and header(hdrs, "hx-trigger") == "refreshMappings", \
            (st, body[:300])
        assert notice(hdrs) == {"kind": "ok", "text": "“Off” saved.",
                                "focus": notice(hdrs)["focus"]}, notice(hdrs)
    print("mapping save / start: a mapping that didn't start is kept and the notice says why "
          "(a taken port: what to do, and that it is tried again); one saved stopped says "
          "saved  OK")


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
