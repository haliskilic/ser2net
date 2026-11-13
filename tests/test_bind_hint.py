"""Ports the system refuses say why and what to do (ser2netweb/engine/bindhint.py).

On Linux a process that isn't root needs CAP_NET_BIND_SERVICE for a port below 1024 (the
systemd unit's user, the Docker image's on the host network). The refused bind said only
"permission denied". Checked:
- hint(): EACCES on a port below the unprivileged start is "privileged_port" on Linux,
  WSAEACCES (10013) on Windows "excluded_port"; anything else, a port above, a connect-out
  mapping or macOS give none; the log line names the capability and the README section;
- a mapping started on port 502 by this account (Linux, not root): its status carries
  the hint (the REST API's status is that same dict), the log explains it; the dashboard
  shows the sentence in English and Turkish;
- an admin UI port below 1024: a start-up warning in all.log names the cause (the server
  then can't start its web UI); none for a port above, for root or with the capability.

Run: python3 tests/test_bind_hint.py
"""
import asyncio
import errno
import html
import json
import os
import shutil
import subprocess
import sys
import time

from _server import ROOT, Browser, Server

from ser2netweb.config import MappingConfig
from ser2netweb.engine import bindhint
from ser2netweb.engine.supervisor import Supervisor
from ser2netweb.web.auth import hash_password

PW = "hint-pass-1"
LOW = 502  # Modbus/TCP's port: below 1024, refused to an account without the capability


def _can_test_real_bind() -> bool:
    if not sys.platform.startswith("linux") or bindhint.may_bind_low_ports():
        print("SKIP the real bind: needs Linux and an account without CAP_NET_BIND_SERVICE")
        return False
    if bindhint.unprivileged_port_start() <= LOW:
        print(f"SKIP the real bind: ports from {bindhint.unprivileged_port_start()} are open "
              "to everyone here")
        return False
    return True


def test_hint():
    saved = bindhint.unprivileged_port_start
    bindhint.unprivileged_port_start = lambda: 1024
    try:
        denied = OSError(errno.EACCES, "error while attempting to bind on address "
                                       "('0.0.0.0', 502): [errno 13] permission denied")
        assert bindhint.hint(denied, 502, "linux") == bindhint.PRIVILEGED_PORT
        assert bindhint.hint(denied, 4001, "linux") == "", "a port above: another cause"
        assert bindhint.hint(OSError(errno.EADDRINUSE, "in use"), 502, "linux") == ""
        assert bindhint.hint(denied, 502, "darwin") == ""
        assert bindhint.hint(OSError(10013, "forbidden by its access permissions"), 4001,
                             "win32") == bindhint.EXCLUDED_PORT
        assert bindhint.hint(denied, 4001, "win32") == bindhint.EXCLUDED_PORT
        assert bindhint.hint(OSError(10048, "only one usage"), 4001, "win32") == ""
        line = bindhint.explain(bindhint.PRIVILEGED_PORT)
        assert "CAP_NET_BIND_SERVICE" in line and "below 1024" in line and "README" in line
        assert "excludedportrange" in bindhint.explain(bindhint.EXCLUDED_PORT)
        client = MappingConfig.from_dict({"name": "out", "serial": {"port": "/dev/x"},
                                          "network": {"mode": "client", "remote_host": "h",
                                                      "remote_port": 502}})
        if sys.platform.startswith("linux"):
            assert bindhint.for_mapping(denied, client) == "", "a connection out binds nothing"
    finally:
        bindhint.unprivileged_port_start = saved
    print("hint(): Linux EACCES below 1024, Windows WSAEACCES; nothing for other causes  OK")


def test_admin_port_note():
    saved = bindhint.may_bind_low_ports, bindhint.unprivileged_port_start
    bindhint.unprivileged_port_start = lambda: 1024
    try:
        bindhint.may_bind_low_ports = lambda: False
        note = bindhint.admin_port_note(80, "linux")
        assert note.startswith("WARNING: the admin UI port 80 is below 1024") and \
            "CAP_NET_BIND_SERVICE" in note, note
        assert bindhint.admin_port_note(8080, "linux") == ""
        assert bindhint.admin_port_note(80, "win32") == ""
        bindhint.may_bind_low_ports = lambda: True  # root, or the capability
        assert bindhint.admin_port_note(80, "linux") == ""
    finally:
        bindhint.may_bind_low_ports, bindhint.unprivileged_port_start = saved
    print("admin UI below 1024: a warning unless this process may listen there  OK")


def _mapping(mid: str = "hint00000001", port: int = LOW) -> dict:
    return {"id": mid, "name": "Modbus-GW", "enabled": True, "kind": "net",
            "serial": {"port": "/dev/ser2net-hint-missing"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": port}}


def test_supervisor_status():
    if not _can_test_real_bind():
        return
    lines: list[str] = []

    async def go():
        sup = Supervisor(logger=lines.append)
        try:
            ok, err = await sup.apply_mapping(MappingConfig.from_dict(_mapping()))
            return (ok, err, sup.status("hint00000001"),
                    sup.get_runner("hint00000001").status.hint)
        finally:
            await sup.stop_all()

    ok, err, st, note = asyncio.run(go())
    assert not ok and "permission denied" in err.lower(), err
    assert note is not None and note.kind == bindhint.PRIVILEGED_PORT, note
    # the REST API's status: the hint in English, as for a serial port's
    assert st["state"] == "error" and st["hint"].startswith(
        "Ports below 1024 need extra rights on Linux"), st
    assert any("CAP_NET_BIND_SERVICE" in ln for ln in lines), lines
    print(f"a mapping on {LOW}: state error, hint {note.kind}, the log says what to do  OK")


def test_dashboard_and_api():
    if not _can_test_real_bind():
        return
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    with open(os.path.join(ROOT, "ser2netweb", "locale", "tr.json"), encoding="utf-8") as fh:
        tr = json.load(fh)
    en_text = ("Ports below 1024 need extra rights on Linux: give ser2netweb the "
               "CAP_NET_BIND_SERVICE capability")
    tr_text = next(v for k, v in tr.items() if k.startswith(en_text))
    with Server({"users": users, "mappings": [_mapping()]}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, body = b.get("/api/status")
        shown = html.unescape(body.replace("<wbr>", ""))
        assert st == 200 and en_text in shown, body[:2000]
        # in place of the general sentence for a refused bind (wrong on Windows), once
        assert "No permission to listen on this port" not in shown, body[:2000]
        assert shown.count(en_text) == 1, body[:2000]
        b.get("/lang/tr?next=/")
        st, _, body = b.get("/api/status")
        assert st == 200 and tr_text in html.unescape(body.replace("<wbr>", "")), body[:2000]
    print("dashboard: the hint in English and Turkish  OK")


def test_admin_ui_port_warning():
    if not _can_test_real_bind():
        return
    users = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
              "pwd_version": 1}]
    srv = Server({"users": users})
    srv.env["SER2NETWEB_PORT"] = "80"
    log_path = os.path.join(srv.tmp, "server.log")
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "ser2netweb.py", "--no-bootstrap", "--data-dir", srv.data],
                cwd=ROOT, env=srv.env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT)
            try:
                all_log, deadline = "", time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
                            all_log = fh.read()
                    except OSError:
                        pass
                    if "admin UI port 80 is below 1024" in all_log or proc.poll() is not None:
                        break
                    time.sleep(0.2)
            finally:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            out = fh.read()
        assert "WARNING: the admin UI port 80 is below 1024" in all_log, out[-2000:]
        assert "CAP_NET_BIND_SERVICE" in all_log, all_log
    finally:
        shutil.rmtree(srv.tmp, ignore_errors=True)
    print("admin UI on port 80: all.log says why the web UI can't start  OK")


if __name__ == "__main__":
    test_hint()
    test_admin_port_note()
    test_supervisor_status()
    test_dashboard_and_api()
    test_admin_ui_port_warning()
    print("\nPASS: refused ports explained")
