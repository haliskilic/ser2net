"""config.json read again while ser2net runs: SIGHUP (POSIX), `ser2net --reload` and
Settings -> Configuration backups -> Reload config.json.

  * an edit made by hand is applied part by part: an unchanged mapping keeps running with
    its client, a changed one is restarted, a removed one stops listening, a new one
    listens; other settings take effect (log retention); the log line and the audit record
    say what changed; SIGHUP no longer ends the process, and systemd hears RELOADING=1
    then READY=1;
  * a file that doesn't validate (two mappings on one port, no user accounts, broken JSON)
    changes nothing: the log says why, the client stays;
  * a changed admin UI address is reported as waiting for the next start (Settings too),
    the UI keeps listening where it does;
  * `ser2net --reload` reloads the running instance (any platform) and fails when none
    runs; the Settings button reloads, admins only;
  * an unchanged TLS mapping reads its renewed certificate files for new connections
    (cryptography makes them).

Run: python3 tests/test_config_reload.py
"""
from __future__ import annotations

import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from _optional import need
from _server import ROOT, Browser, Server, free_port

from app.web.auth import hash_password, hash_token

PW = "reload-pass-1"
TOKEN = "s2n_reload-token"
HAS_SIGHUP = hasattr(signal, "SIGHUP")


def api(srv, method, path, body=None):
    req = urllib.request.Request(srv.base + "/api/v1" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait(cond, what, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def still_connected(sock) -> bool:
    sock.settimeout(0.5)
    try:
        data = sock.recv(256)
    except socket.timeout:
        return True
    except OSError:
        return False
    return False if not data else still_connected(sock)


def listens(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def clients(srv, mid) -> int:
    code, m = api(srv, "GET", f"/mappings/{mid}")
    return (m.get("status") or {}).get("client_count", 0) if code == 200 else -1


def names(srv) -> list[str]:
    return sorted(m["name"] for m in api(srv, "GET", "/mappings")[1]["mappings"])


def write_config(srv, cfg) -> None:
    with open(srv.config_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def reload_lines(srv, after: int) -> list[str]:
    return [ln for ln in srv.output()[after:].splitlines() if "config.json reloaded" in ln
            or "config.json not reloaded" in ln]


def sighup(srv, mark: int) -> str:
    srv.proc.send_signal(signal.SIGHUP)
    wait(lambda: reload_lines(srv, mark), "the reload's log line")
    assert srv.proc.poll() is None, "SIGHUP must not end ser2net"
    return reload_lines(srv, mark)[-1]


def cli_reload(data_dir, env=None):
    child = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
    p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", data_dir,
                        "--reload"], cwd=ROOT, capture_output=True, text=True, timeout=90,
                       env=dict(child, **(env or {})))
    return p.returncode, (p.stdout + p.stderr).strip()


def mapping(mid, name, port, **network):
    return {"id": mid, "name": name, "serial": {"port": f"/dev/s2n-reload-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": port, **network}}


def server(cfg_extra, env=None) -> Server:
    """A server whose admin UI address comes from config.json (not the environment), so a
    changed address can be seen waiting for the next start."""
    srv = Server(None, env=env)
    del srv.env["SER2NET_BIND_IP"], srv.env["SER2NET_PORT"]
    cfg = {"admin_ui": {"bind_ip": "127.0.0.1", "port": srv.port},
           "users": [{"username": "admin", "password_hash": hash_password(PW),
                      "role": "admin", "pwd_version": 1},
                     {"username": "op", "password_hash": hash_password(PW),
                      "role": "operator", "pwd_version": 1}],
           "api_token_hash": hash_token(TOKEN), "api_token_role": "admin", **cfg_extra}
    write_config(srv, cfg)
    return srv


def test_reload() -> None:
    pa, pb, pc, pd, pb2 = (free_port() for _ in range(5))
    notify_dir = tempfile.mkdtemp(prefix="s2n_reload_notify_")
    notify_path = os.path.join(notify_dir, "notify")
    listener = None
    if hasattr(socket, "AF_UNIX"):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.bind(notify_path)
        listener.settimeout(0.2)
    srv = server({"mappings": [mapping("a1", "A", pa), mapping("b1", "B", pb),
                               mapping("c1", "C", pc)]},
                 env={"NOTIFY_SOCKET": notify_path} if listener else None)
    try:
        srv.start()
        ca = socket.create_connection(("127.0.0.1", pa), timeout=5)
        cb = socket.create_connection(("127.0.0.1", pb), timeout=5)
        wait(lambda: clients(srv, "a1") == 1 and clients(srv, "b1") == 1, "the clients")
        cfg = srv.saved_config()

        # a hand edit: B changes, C goes, D comes, a global setting changes
        cfg["mappings"] = [mapping("a1", "A", pa), mapping("b1", "B2", pb2),
                           mapping("d1", "D", pd)]
        cfg["log_max_age_days"] = 7
        write_config(srv, cfg)
        if HAS_SIGHUP:
            if listener is not None:
                while True:  # what start-up sent
                    try:
                        listener.recv(4096)
                    except socket.timeout:
                        break
            line = sighup(srv, len(srv.output()))
            assert "mappings added: D" in line and "mappings changed (restarted): B2" in line \
                and "mappings removed: C" in line, line
            if listener is not None:
                got = []
                end = time.time() + 10
                while time.time() < end and not any(m.startswith("READY=1") for m in got):
                    try:
                        got.append(listener.recv(4096).decode())
                    except socket.timeout:
                        pass
                assert got and got[0].startswith("RELOADING=1\nMONOTONIC_USEC="), got
                assert any(m.startswith("READY=1\nSTATUS=") for m in got), got
                print("systemd: RELOADING=1 (with MONOTONIC_USEC), then READY=1  OK")
        else:
            code, out = cli_reload(srv.data)
            assert code == 0, out
        assert still_connected(ca), "an unchanged mapping must keep its client"
        assert not still_connected(cb), "a changed mapping is restarted"
        wait(lambda: listens(pb2) and listens(pd), "B2 and D to listen")
        assert not listens(pc) and not listens(pb), "C is removed, B moved"
        assert names(srv) == ["A", "B2", "D"], names(srv)
        assert clients(srv, "a1") == 1
        print("hand edit + reload: unchanged A keeps its client, B restarted on its new port, "
              "C gone, D listens; SIGHUP keeps ser2net running  OK")

        b = Browser(srv.base)
        b.login("admin", PW)
        st, _, page = b.get("/settings")
        assert 'name="log_max_age_days" min="0" max="3650" value="7"' in page, \
            "the reloaded retention period is in use"

        # files that don't validate change nothing
        mark = len(srv.output())
        bad = json.loads(json.dumps(cfg))
        bad["mappings"].append(mapping("e1", "E", pa))  # A's port
        write_config(srv, bad)
        code, out = cli_reload(srv.data)
        assert code == 1 and "config.json not reloaded" in out and "both use TCP port" in out \
            and "the running configuration is kept" in out, out
        assert any("WARNING: config.json not reloaded (command line)" in ln
                   for ln in reload_lines(srv, mark)), srv.output()[mark:]
        for broken in ({**cfg, "users": []}, None):
            if broken is None:
                with open(srv.config_path, "w") as fh:
                    fh.write('{"mappings": [')  # cut off
            else:
                write_config(srv, broken)
            code, out = cli_reload(srv.data)
            assert code == 1 and "not reloaded" in out, out
            assert ("no user accounts" in out) if broken is not None else \
                ("is not a valid configuration" in out), out
        assert still_connected(ca) and names(srv) == ["A", "B2", "D"]
        print("a clash, no accounts, broken JSON: not reloaded (CLI exit 1, all.log says why), "
              "nothing changes  OK")

        # a changed admin UI address waits for the next start
        moved = free_port()
        cfg["admin_ui"]["port"] = moved
        write_config(srv, cfg)
        st, _, page = b.post("/settings/config/reload", {})
        assert "config.json was read again and applied" in page, page[-800:]
        assert f"the admin UI address 127.0.0.1:{moved} takes effect at the next start" in page
        st, _, page = b.get("/settings")
        assert f'<span class="mono">127.0.0.1:{srv.port}</span>' in page
        assert f"config.json sets <span class=\"mono\">127.0.0.1:{moved}</span>" in page, \
            page[page.find("Admin interface"):][:800]
        assert not listens(moved)
        op = Browser(srv.base)
        op.login("op", PW)
        st, _, _ = op.post("/settings/config/reload", {})
        assert st == 403
        print("a new admin UI address: reported, used from the next start; Settings reload is "
              "admin only  OK")

        code, out = cli_reload(srv.data)
        assert code == 0 and out.startswith("ser2net: config.json reloaded: ") and \
            "(done by the running ser2net)" in out, out
        with open(os.path.join(srv.data, "audit.log"), encoding="utf-8") as fh:
            audit = fh.read()
        assert "config_reload" in audit and "config_reload_refused" in audit, audit[-1500:]
        print("ser2net --reload: the running instance reloads (exit 0); audit records  OK")
        data, log = srv.data, srv.output()
        assert "Traceback" not in log, log[-3000:]
    finally:
        srv.stop()
        if listener is not None:
            listener.close()
    code, out = cli_reload(data)
    assert code == 1 and "not running" in out, out
    print("ser2net --reload without a running instance: exit 1, nothing to reload  OK")


def test_mapping_tls_renewal() -> None:
    if not HAS_SIGHUP:
        print("SKIP: the TLS renewal part sends SIGHUP (POSIX)")
        return
    from _pki import TestCA

    tmp = tempfile.mkdtemp(prefix="s2n_reload_tls_")
    ca = TestCA(tmp)
    cert, key = ca.issue(tmp, "m", ["127.0.0.1"])
    cert2, key2 = ca.issue(tmp, "m2", ["127.0.0.1"])
    port = free_port()
    srv = server({"mappings": [mapping("t1", "T", port, tls=True, tls_cert=cert,
                                       tls_key=key)]})
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    def peer() -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw, \
                ctx.wrap_socket(raw) as tls:
            return tls.getpeercert(binary_form=True)

    def der(path):
        with open(path) as fh:
            return ssl.PEM_cert_to_DER_cert(fh.read())

    try:
        srv.start()
        wait(lambda: listens(port), "the TLS mapping")
        assert peer() == der(cert)
        held = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5))
        os.replace(cert2, cert)
        os.replace(key2, key)
        line = sighup(srv, len(srv.output()))
        assert "TLS certificate loaded again by 1 mapping listener(s)" in line, line
        assert peer() == der(cert), "a new connection gets the renewed certificate"
        held.settimeout(0.5)
        try:
            assert held.recv(16) != b""
        except socket.timeout:
            pass  # still open: the connection made before keeps its session
        held.close()
        # files that no longer load: the certificate in use stays
        with open(key, "w") as fh:
            fh.write("not a key")
        line = sighup(srv, len(srv.output()))
        assert "TLS certificate loaded again" not in line
        assert "[T] WARNING: the TLS certificate files can't be loaded again" in srv.output()
        assert peer() == der(cert)
        print("an unchanged TLS mapping: renewed files are served after a reload, broken ones "
              "keep the certificate in use  OK")
        assert "Traceback" not in srv.output(), srv.output()[-3000:]
    finally:
        srv.stop()


def test_terminal_hangup() -> None:
    """Started in a terminal: `kill -HUP` reloads, the terminal closing ends ser2net cleanly
    (the kernel's SIGHUP to the session it leads)."""
    if not (HAS_SIGHUP and sys.platform.startswith("linux")):
        print("SKIP: the terminal hang-up part needs a Linux pseudo-terminal")
        return
    import fcntl
    import termios

    srv = server({})
    master, slave = os.openpty()
    log = open(os.path.join(srv.tmp, "server.log"), "wb")
    srv.log_path, srv._log = log.name, log

    def take_terminal() -> None:  # (after setsid: the pty becomes its controlling terminal)
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir",
                             srv.data], cwd=ROOT, env=srv.env, stdin=slave, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            preexec_fn=take_terminal)
    os.close(slave)
    srv.proc = proc
    try:
        wait(lambda: listens(srv.port), "the server", 20)
        sighup(srv, len(srv.output()))
        print("in a terminal: kill -HUP reloads  OK")
        os.close(master)  # the terminal goes away
        master = -1
        proc.wait(timeout=20)
        out = srv.output()
        assert "SIGHUP: the terminal ser2net was started in was closed — shutting down" in out \
            and "shutting down — stopping all mappings" in out, out[-1500:]
        assert proc.returncode == 0, proc.returncode
        print("in a terminal: the terminal closing ends ser2net cleanly (exit 0)  OK")
    finally:
        if master >= 0:
            os.close(master)
        srv.stop()


def main() -> None:
    test_reload()
    test_terminal_hangup()
    if need("cryptography", what="the TLS mapping's certificates (made in-process)"):
        test_mapping_tls_renewal()
    print("\nPASS: config.json reloads without a restart")


if __name__ == "__main__":
    main()
