"""The admin UI starts before the mappings: TLS loaded and its port listening first.

  - httpguard.listen makes the UI's sockets as asyncio would (every address of the bind
    IP, IPV6_V6ONLY on IPv6), listening at once: a mapping started afterwards on the same
    address and port fails to bind (BindError) instead of taking the port;
  - a mapping stored on the admin UI's address and port no longer keeps the UI from
    starting: the UI comes up (/healthz answers), the mapping shows "error" (retrying);
  - the UI's port held by another program: the mappings start anyway, all.log says why
    the UI is missing, and the UI comes up by itself once the port is free;
  - an admin TLS pair that exists but does not load stops the start before any mapping
    starts, with the reason in all.log and no traceback; saving such a pair in Settings
    is refused (a pair that loads is saved).
The servers run as child processes on 127.0.0.1.

Run: python3 tests/test_admin_ui_first.py
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from _server import ROOT, Browser, Server, free_port

from app.engine import netguard
from app.web import httpguard
from app.web.auth import hash_password

PW = "ui-first-pass-1"
USERS = [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
          "pwd_version": 1}]


def mapping(name, port, mid):
    return {"id": mid, "name": name, "enabled": True, "serial": {"port": f"/dev/ttyNOPE-{mid}"},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1",
                        "port": port}}


def healthz(base: str) -> str:
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=3) as r:
            return r.read().decode()
    except (OSError, urllib.error.URLError) as e:
        return f"down: {e}"


def wait(check, timeout=20.0, what=""):
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.2)


def read_log(srv) -> str:
    try:
        with open(os.path.join(srv.data, "all.log"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


async def _listen():
    socks = await httpguard.listen("127.0.0.1", 0, 16)
    try:
        assert len(socks) == 1 and not socks[0].getblocking()
        port = socks[0].getsockname()[1]
        # listening already, before anything serves it
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
        # a mapping started afterwards on that address and port (or on all of them) fails
        for ip in ("127.0.0.1", "0.0.0.0"):
            try:
                await netguard.start_server(lambda r, w: None, ip, port)
            except netguard.BindError:
                pass
            else:
                raise AssertionError(f"a mapping on {ip}:{port} took the admin UI's port")
        # the port is in use: an OSError that says where
        try:
            await httpguard.listen("127.0.0.1", port, 16)
        except OSError as e:
            assert f"127.0.0.1 port {port}" in str(e), e
        else:
            raise AssertionError("bound a port in use")
    finally:
        for s in socks:
            s.close()
    try:
        await httpguard.listen("192.0.2.1", 0, 16)  # not an address of this machine
    except OSError as e:
        assert "cannot listen on 192.0.2.1" in str(e), e
    else:
        raise AssertionError("bound an address that is not here")
    if socket.has_ipv6:
        try:
            socks = await httpguard.listen("::1", 0, 16)
        except OSError:
            print("SKIP (part): no IPv6 loopback here")
        else:
            try:
                assert socks[0].getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
            finally:
                for s in socks:
                    s.close()
    socks = await httpguard.listen("", 0, 16)  # every interface
    try:
        assert {s.family for s in socks} >= {socket.AF_INET}, socks
    finally:
        for s in socks:
            s.close()
    print("httpguard.listen: listening at once (a later mapping on the port fails), clear "
          "errors, IPv6 only on IPv6, '' = every interface  OK")


def test_listen():
    asyncio.run(_listen())


def test_mapping_on_the_ui_port():
    srv = Server()
    with open(srv.config_path, "w", encoding="utf-8") as fh:
        json.dump({"users": USERS, "mappings": [mapping("On UI port", srv.port, "uiport000001")]},
                  fh)
    with srv:
        assert healthz(srv.base) == "ok", healthz(srv.base)
        b = Browser(srv.base)
        b.login("admin", PW)
        st, _h, body = b.get("/api/status")
        assert st == 200 and "state-error" in body and "retrying" in body, body[-2000:]
        log = read_log(srv)
        assert "[On UI port] start failed:" in log, log[-3000:]
    print("a mapping on the admin UI's address and port: the UI comes up, the mapping "
          "shows error (retrying)  OK")


def test_ui_port_held_at_start():
    srv = Server()
    work_port = free_port()
    with open(srv.config_path, "w", encoding="utf-8") as fh:
        json.dump({"users": USERS, "mappings": [mapping("Works", work_port, "works0000001")]}, fh)
    holder = socket.socket()
    holder.bind(("127.0.0.1", srv.port))
    holder.listen()
    try:
        srv.start()  # (its port check is answered by the holder)
        wait(lambda: "the mappings start anyway" in read_log(srv), what="the UI's log line")
        wait(lambda: f"[Works] listening on 127.0.0.1:{work_port}" in read_log(srv),
             what="the mapping")
        with socket.create_connection(("127.0.0.1", work_port), timeout=3):
            pass
        assert srv.proc.poll() is None, "the process ended"
        holder.close()
        wait(lambda: healthz(srv.base) == "ok", 40, "the admin UI")
        log = read_log(srv)
        assert f"admin UI: listening on 127.0.0.1:{srv.port} now" in log, log[-3000:]
        assert log.count("the mappings start anyway") == 1, log[-3000:]
    finally:
        holder.close()
        srv.__exit__(None, None, None)
    print("the admin UI's port taken at start: the mappings run, the UI comes up once the "
          "port is free  OK")


def test_tls_that_does_not_load():
    data = tempfile.mkdtemp(prefix="s2n_uitls_")
    try:
        cert, key = os.path.join(data, "cert.pem"), os.path.join(data, "key.pem")
        for path in (cert, key):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not a certificate\n")
        port = free_port()
        cfg = {"users": USERS, "admin_ui": {"tls_cert": cert, "tls_key": key},
               "mappings": [mapping("Never", free_port(), "never0000001")]}
        with open(os.path.join(data, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        env = dict(os.environ, SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(port))
        p = subprocess.run([sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", data],
                           cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=60)
        out = p.stdout + p.stderr
        with open(os.path.join(data, "all.log"), encoding="utf-8") as fh:
            log = fh.read()
        assert p.returncode == 1, (p.returncode, out[-2000:])
        assert "the TLS certificate could not be loaded" in log, log[-2000:]
        assert "no mapping was started" in log and "Traceback" not in out, out[-2000:]
        assert "[Never]" not in log, log[-2000:]
    finally:
        shutil.rmtree(data, ignore_errors=True)
    print("an admin TLS pair that does not load: exit 1 before any mapping starts, the "
          "reason in all.log  OK")


def test_tls_checked_when_saved():
    from app.web import selfsigned

    with Server({"users": USERS}) as srv:
        b = Browser(srv.base)
        b.login("admin", PW)
        bad_cert, bad_key = os.path.join(srv.tmp, "bad.pem"), os.path.join(srv.tmp, "bad.key")
        for path in (bad_cert, bad_key):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not a certificate\n")
        st, _h, page = b.post("/settings/tls", {"tls_cert": bad_cert, "tls_key": bad_key,
                                                "_csrf": b.csrf()})
        assert "could not be loaded" in page, page[-2000:]
        assert not srv.saved_config()["admin_ui"].get("tls_cert"), "a pair that can't load was saved"
        cert, key = os.path.join(srv.tmp, "cert.pem"), os.path.join(srv.tmp, "key.pem")
        try:
            selfsigned.generate(cert, key)
        except selfsigned.CertError:
            print("SKIP (part): neither cryptography nor openssl to make a certificate")
            return
        st, _h, page = b.post("/settings/tls", {"tls_cert": cert, "tls_key": key,
                                                "_csrf": b.csrf()})
        assert "Admin TLS saved" in page, page[-2000:]
        assert srv.saved_config()["admin_ui"]["tls_cert"] == cert
    print("Settings: an admin TLS pair that does not load is refused, one that loads is "
          "saved  OK")


if __name__ == "__main__":
    test_listen()
    test_mapping_on_the_ui_port()
    test_ui_port_held_at_start()
    test_tls_that_does_not_load()
    test_tls_checked_when_saved()
    print("\nPASS: the admin UI starts before the mappings")
