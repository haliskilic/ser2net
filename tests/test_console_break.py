"""The web console's serial BREAK: POST /api/mappings/{mid}/break (a real server).

  - an operator gets 204 on a mapping whose port is open: the BREAK is sent (the mapping's
    log says so, with who sent it) and audited as mapping_break;
  - refused, with nothing sent or audited: a viewer (requires the operator role), no
    X-CSRF-Token header, a read-only mapping (400), a Modbus gateway (400), a mapping
    whose port is not open (409), a mapping that doesn't exist (404).
The serial ports are pseudo-terminals (POSIX; skipped on Windows), on which the break
ioctl does nothing but succeed.

Run: python3 tests/test_console_break.py
"""
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _server import Browser, Server, free_port

from app.web.auth import hash_password

PW = "break-pass-1"


def _drain(fd, stop):
    while not stop.is_set():
        try:
            if not os.read(fd, 4096):
                return
        except OSError:
            return


def _mapping(mid, name, port, **network):
    return {"id": mid, "name": name, "enabled": True, "serial": {"port": port},
            "network": {"mode": "server", "protocol": "telnet", "bind_ip": "127.0.0.1",
                        "port": free_port(), **network}}


def _wait(check, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.1)
    return check()


def test_console_break():
    if not hasattr(os, "openpty"):
        print("SKIP: no pseudo-terminals on this platform")
        return
    ptys = [os.openpty() for _ in range(2)]
    stop = threading.Event()
    for master, _slave in ptys:
        threading.Thread(target=_drain, args=(master, stop), daemon=True).start()
    users = [{"username": name, "password_hash": hash_password(PW), "role": role,
              "pwd_version": 1}
             for name, role in (("admin", "admin"), ("op", "operator"), ("view", "viewer"))]
    maps = [_mapping("open00000001", "Console", os.ttyname(ptys[0][1])),
            _mapping("ro0000000001", "Tap", os.ttyname(ptys[1][1]), read_only=True),
            _mapping("gone00000001", "Gone", "/dev/ttyNOPE-break"),
            _mapping("modbus000001", "Bus", "/dev/ttyNOPE-bus", protocol="modbus")]
    try:
        with Server({"users": users, "mappings": maps}) as srv:
            op, view = Browser(srv.base), Browser(srv.base)
            op.login("op", PW)
            view.login("view", PW)
            assert _wait(lambda: "serial open: " + maps[0]["serial"]["port"] in srv.output()
                         and "serial open: " + maps[1]["serial"]["port"] in srv.output()), \
                srv.output()[-2000:]

            st, _h, body = op.post("/api/mappings/open00000001/break", htmx=True)
            assert st == 204, (st, body)
            assert _wait(lambda: "serial BREAK sent (web console, op)" in srv.output()), \
                srv.output()[-2000:]

            st, _h, body = view.post("/api/mappings/open00000001/break", htmx=True)
            assert st == 403 and "requires the operator role" in body, (st, body)
            try:  # no X-CSRF-Token header: refused by the gate
                op.op.open(urllib.request.Request(srv.base + "/api/mappings/open00000001/break",
                                                  data=b"", method="POST"), timeout=10)
                raise AssertionError("a POST without the CSRF header was taken")
            except urllib.error.HTTPError as e:
                assert e.code == 403, e.code
            for mid, code, text in (("ro0000000001", 400, "read-only"),
                                    ("modbus000001", 400, "can't be sent on this mapping"),
                                    ("gone00000001", 409, "serial port is not open"),
                                    ("nope00000001", 404, "not found")):
                st, _h, body = op.post(f"/api/mappings/{mid}/break", htmx=True)
                assert st == code and text in body, (mid, st, body)

            with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
                records = [ln.split("\t") for ln in fh if "\tmapping_break\t" in ln]
            assert len(records) == 1, "only the BREAK that was sent is audited"
            assert [f.strip() for f in records[0][2:]] == ["mapping_break", "Console", "op"], \
                records
    finally:
        stop.set()
        for master, slave in ptys:
            for fd in (master, slave):
                try:
                    os.close(fd)
                except OSError:
                    pass
    print("console BREAK: 204 and audited for an operator; refused for a viewer, without "
          "CSRF, read-only, Modbus, port not open, unknown mapping  OK")


def main():
    test_console_break()
    print("\nPASS: the web console's serial BREAK")


if __name__ == "__main__":
    main()
