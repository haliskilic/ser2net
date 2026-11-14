"""docs/REVERSE-PROXY.md's nginx examples, run as they are (CI: ci.yml's job "reverse proxy
(nginx)"). ser2netweb is put behind nginx configured with the page's two nginx blocks, the
one at / (Public URL https://ser2netweb.example.com) and the one under the path
/ser2netweb (Public URL https://gw.example.com/ser2netweb):

    python3 tests/nginx_proxy.py [--nginx PATH]

The blocks are taken from the page. Only what a test machine has to change is changed, and
each change must apply exactly once (a changed page fails here, it isn't worked around):
`listen 443 ssl;` becomes a free port of 127.0.0.1, the certificate comment the lines of a
self-signed certificate made here (openssl), `proxy_pass http://127.0.0.1:8080;` the test
server's port. The browser's host name and that port are the Public URL. nginx runs as
this user, with its files in a temporary folder. The client connects from 127.0.0.2 and
nginx to ser2netweb from 127.0.0.1, so the address ser2netweb names tells them apart.

For each block, through nginx over HTTPS:
  * the web UI: the sign-in page and a static file, signing in, a page with the mapping;
  * the client's address: nginx's (127.0.0.1) while Trusted proxies is empty, with all.log's
    note that X-Forwarded-For is ignored; Settings -> Reverse proxy -> Trusted proxies
    127.0.0.1 (the form); signed in again: the client's own (127.0.0.2), in all.log and in
    the audit log;
  * the console's WebSocket (what the device sends arrives, what is typed reaches the
    device) and the hex view's (the device's bytes, timestamped);
  * the REST API: a token made in Settings -> REST API, GET /api/v1/mappings with it, and
    OpenAPI's servers entry (/api/v1; under the path, with it).
It starts servers: run it on a CI runner or in a throwaway network namespace.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import http.client
import json
import os
import re
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _server import ROOT, Server, free_port

from ser2netweb.web.auth import hash_password

DOC = os.path.join(ROOT, "docs", "REVERSE-PROXY.md")
CLIENT = "127.0.0.2"   # the browser's address (nginx sees it, ser2netweb only once trusted)
PROXY = "127.0.0.1"    # nginx's, as ser2netweb sees it
PW = "nginx-proxy-pass-1"
MID = "m1"
# (location, the Public URL's host, its path)
VARIANTS = (("/", "ser2netweb.example.com", ""), ("/ser2netweb/", "gw.example.com", "/ser2netweb"))


def fail(msg: str) -> None:
    raise SystemExit(f"FAIL: {msg}")


def check(ok, msg: str) -> None:
    if not ok:
        fail(msg)


# --- the page's configuration -----------------------------------------------------------------

def doc_blocks() -> tuple[str, dict[str, str]]:
    """(the map line for http { }, {location: its server block}) of the page's nginx blocks."""
    with open(DOC, encoding="utf-8") as fh:
        blocks = re.findall(r"```nginx\n(.*?)```", fh.read(), re.S)
    found, maps = {}, []
    for block in blocks:
        maps += re.findall(r"(?m)^map \$http_upgrade .*?\}", block)
        server = block[block.index("server {"):]
        locations = re.findall(r"(?m)^    location (\S+) \{$", server)
        check(len(locations) == 1, f"an nginx block of the page has the locations {locations}")
        found[locations[0]] = server.rstrip("\n") + "\n"
    check(len(maps) == 1, f"the page's nginx blocks have {len(maps)} map lines, not one")
    check(set(found) == {v[0] for v in VARIANTS}, f"the page's nginx blocks are for {sorted(found)}")
    return maps[0], found


def _once(text: str, old: str, new: str) -> str:
    check(text.count(old) == 1, f"the page's nginx block has {text.count(old)} times {old!r}:\n{text}")
    return text.replace(old, new)


def nginx_conf(tmp: str, block: str, map_line: str, port: int, cert: str, key: str,
               upstream: int) -> str:
    block = _once(block, "    listen 443 ssl;\n", f"    listen {PROXY}:{port} ssl;\n")
    block = _once(block, "    # ssl_certificate / ssl_certificate_key …\n",
                  f"    ssl_certificate {cert};\n    ssl_certificate_key {key};\n")
    block = _once(block, "proxy_pass http://127.0.0.1:8080;", f"proxy_pass http://127.0.0.1:{upstream};")
    temp = " ".join(f"{d}_temp_path {tmp}/{d};" for d in ("client_body", "proxy", "fastcgi",
                                                           "uwsgi", "scgi"))
    return (f"daemon off;\nworker_processes 1;\npid {tmp}/nginx.pid;\n"
            f"error_log {tmp}/error.log info;\nevents {{ worker_connections 128; }}\n"
            f"http {{\n    access_log {tmp}/access.log;\n    {temp}\n    {map_line}\n\n"
            f"{block}}}\n")


def certificate(tmp: str) -> tuple[str, str]:
    cert, key = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
    names = ",".join(f"DNS:{v[1]}" for v in VARIANTS)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=ser2netweb.example.com", "-addext", f"subjectAltName={names}",
                    "-keyout", key, "-out", cert], check=True, capture_output=True)
    return cert, key


class Nginx:
    def __init__(self, binary: str, tmp: str, conf: str, port: int):
        self.tmp, self.port = tmp, port
        path = os.path.join(tmp, "nginx.conf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(conf)
        args = [binary, "-p", tmp, "-c", path, "-e", os.path.join(tmp, "error.log")]
        p = subprocess.run([*args, "-t"], capture_output=True, text=True)
        check(p.returncode == 0, f"nginx -t refuses the page's configuration:\n{p.stderr}\n{conf}")
        self.log = open(os.path.join(tmp, "nginx.out"), "wb")
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                fail(f"nginx exited:\n{self.errors()}")
            with socket.socket() as s:
                if s.connect_ex((PROXY, port)) == 0:
                    return
            time.sleep(0.1)
        fail(f"nginx doesn't listen on {PROXY}:{port}:\n{self.errors()}")

    def errors(self) -> str:
        try:
            with open(os.path.join(self.tmp, "error.log"), encoding="utf-8", errors="replace") as fh:
                return fh.read()[-3000:]
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.log.close()


# --- a browser in front of nginx --------------------------------------------------------------

def _tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE   # the certificate made here
    return ctx


class Browser:
    """Requests to nginx from CLIENT with the browser's Host (the Public URL's), its Origin
    on what changes something, and the cookies the UI set."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.origin = f"https://{host}:{port}"
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, fields: dict | None = None,
                headers: dict | None = None) -> tuple[int, http.client.HTTPMessage, str]:
        conn = http.client.HTTPSConnection(PROXY, self.port, timeout=30, context=_tls(),
                                           source_address=(CLIENT, 0))
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"{self.host}:{self.port}")
            if self.cookies:
                conn.putheader("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
            body = None
            if fields is not None:
                body = urllib.parse.urlencode(fields).encode()
                conn.putheader("Content-Type", "application/x-www-form-urlencoded")
                conn.putheader("Content-Length", str(len(body)))
                conn.putheader("Origin", self.origin)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders(body)
            r = conn.getresponse()
            data = r.read()
        finally:
            conn.close()
        for cookie in r.headers.get_all("Set-Cookie") or []:
            name, _, value = cookie.split(";", 1)[0].partition("=")
            if value and "max-age=0" not in cookie.lower():
                self.cookies[name.strip()] = value.strip()
            else:
                self.cookies.pop(name.strip(), None)
        return r.status, r.headers, data.decode("utf-8", "replace")

    def cookie(self, part: str) -> tuple[str, str]:
        names = [n for n in self.cookies if part in n]
        check(len(names) == 1, f"the UI's {part} cookie: {sorted(self.cookies)}")
        return names[0], self.cookies[names[0]]

    def login(self, prefix: str) -> None:
        st, _h, page = self.request("GET", f"{prefix}/login")
        check(st == 200 and f'action="{prefix}/login"' in page, f"GET {prefix}/login: {st}")
        st, h, _b = self.request("POST", f"{prefix}/login", {
            "username": "admin", "password": PW, "_csrf": self.cookie("csrf")[1]})
        check(st == 303 and h.get("Location") == f"{prefix}/",
              f"sign-in through nginx: {st} {h.get('Location')}")

    def form(self, path: str, fields: dict) -> str:
        st, h, page = self.request("POST", path, dict(fields, _csrf=self.cookie("csrf")[1]))
        then = ""
        if st == 303:  # (the page it sends the browser to, without its #fragment)
            then = urllib.parse.urlsplit(h["Location"])._replace(scheme="", netloc="",
                                                                 fragment="").geturl()
            st, _h, page = self.request("GET", then)
        check(st == 200, f"POST {path} through nginx: {st} {then}")
        return page


# --- the device side: a PTY the mapping opens -------------------------------------------------

class Pty:
    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def read_until(self, want: bytes, timeout: float = 10) -> bytes:
        got, end = b"", time.monotonic() + timeout
        while want not in got and time.monotonic() < end:
            ready, _w, _x = select.select([self.master], [], [], 0.2)
            if ready:
                got += os.read(self.master, 4096)
        return got

    def close(self) -> None:
        os.close(self.master)


async def _ws(b: Browser, path: str):
    import websockets

    name, session = b.cookie("session")
    return await websockets.connect(
        f"wss://{b.host}:{b.port}{path}", host=PROXY, port=b.port, local_addr=(CLIENT, 0),
        ssl=_tls(), server_hostname=b.host, origin=b.origin, proxy=None,
        additional_headers={"Cookie": f"{name}={session}"}, open_timeout=20)


async def _until(ws, pty: Pty, data: bytes, decode) -> bytes:
    """What the socket brings once `data` was written to the device, until it is there."""
    pty.write(data)
    got, end = b"", time.monotonic() + 15
    while data not in got and time.monotonic() < end:
        msg = await asyncio.wait_for(ws.recv(), timeout=15)
        if isinstance(msg, str):
            check(json.loads(msg).get("state") != "stopped", "the mapping isn't running")
            continue
        got += decode(msg)
    return got


def _records(frame: bytes, direction: int = 0) -> bytes:
    """The bytes of `direction` in a hex view frame of protocol 2 ([dir][time][len][bytes])."""
    out, off = b"", 0
    while off < len(frame):
        d, ts, n = struct.unpack_from(">BdI", frame, off)
        check(abs(ts - time.time()) < 60, f"a hex view record's time: {ts}")
        if d == direction:
            out += frame[off + 13:off + 13 + n]
        off += 13 + n
    return out


async def consoles(b: Browser, prefix: str, pty: Pty) -> None:
    ws = await _ws(b, f"{prefix}/api/mappings/{MID}/console?v=2")
    try:
        got = await _until(ws, pty, b"from-the-device", lambda m: m)
        check(b"from-the-device" in got, f"the console got {got!r}")
        await ws.send(b"typed-in-the-console")
        back = await asyncio.to_thread(pty.read_until, b"typed-in-the-console")
        check(b"typed-in-the-console" in back, f"the device got {back!r}")
    finally:
        await ws.close()
    ws = await _ws(b, f"{prefix}/api/mappings/{MID}/traffic?v=2")
    try:
        got = await _until(ws, pty, b"for-the-hex-view", _records)
        check(b"for-the-hex-view" in got, f"the hex view got {got!r}")
    finally:
        await ws.close()


# --- one block of the page -------------------------------------------------------------------

def run_variant(binary: str, tmp: str, location: str, host: str, prefix: str,
                block: str, map_line: str, cert: str, key: str) -> None:
    port = free_port()
    public = f"https://{host}:{port}{prefix}"
    pty = Pty()
    cfg = {"users": [{"username": "admin", "password_hash": hash_password(PW), "role": "admin",
                      "pwd_version": 1}],
           "admin_ui": {"public_url": public},
           "mappings": [{"id": MID, "name": "behind-nginx", "enabled": True,
                         "serial": {"port": pty.path, "baudrate": 115200},
                         "network": {"mode": "server", "protocol": "raw",
                                     "bind_ip": "127.0.0.1", "port": free_port()}}]}
    folder = os.path.join(tmp, location.strip("/") or "root")
    os.makedirs(folder)
    try:
        with Server(cfg) as srv:
            nginx = Nginx(binary, folder, nginx_conf(folder, block, map_line, port, cert, key,
                                                     srv.port), port)
            try:
                check_variant(srv, nginx, host, port, prefix, public, pty)
            except BaseException:
                print(f"--- nginx's error.log:\n{nginx.errors()}\n--- ser2netweb's output:\n"
                      f"{srv.output()[-4000:]}")
                raise
            finally:
                nginx.stop()
    finally:
        pty.close()


def check_variant(srv: Server, nginx: Nginx, host: str, port: int, prefix: str, public: str,
                  pty: Pty) -> None:
    where = f"nginx at {public}"
    log = srv.output()
    check(f"published as {public} (admin_ui.public_url)" in log, f"{where}: {log[-1500:]}")
    # the web UI
    b = Browser(host, port)
    st, _h, page = b.request("GET", f"{prefix}/login")
    check(st == 200, f"{where}: GET {prefix}/login {st}")
    css = re.search(rf'href="({re.escape(prefix)}/static/app\.css[^"]*)"', page)
    check(css, f"{where}: the sign-in page links no {prefix}/static/app.css")
    st, h, _css = b.request("GET", html.unescape(css.group(1)))
    check(st == 200 and h.get("Content-Type", "").startswith("text/css"), f"{where}: app.css {st}")
    b.login(prefix)
    name, _v = b.cookie("session")
    check(name.startswith("__Secure-" if prefix else "__Host-"), f"{where}: the cookie {name}")
    st, _h, page = b.request("GET", f"{prefix}/")
    check(st == 200 and f'hx-get="{prefix}/api/status"' in page, f"{where}: the dashboard {st}")
    # (the mapping table, as the page loads it)
    st, _h, table = b.request("GET", f"{prefix}/api/status", headers={"HX-Request": "true"})
    check(st == 200 and "behind-nginx" in table, f"{where}: {prefix}/api/status {st}")
    print(f"{where}: the sign-in page, app.css, sign-in ({name}), the dashboard and its "
          "mapping table  OK")
    # the client's address: nginx's until it is trusted
    time.sleep(0.3)
    log = srv.output()
    check(f"login: admin (admin, local) from {PROXY}" in log, f"{where}: {log[-1500:]}")
    check(f"X-Forwarded-For from {PROXY} is ignored: {PROXY} is not a trusted proxy" in log,
          f"{where}: no note that X-Forwarded-For is ignored")
    b.form(f"{prefix}/settings/proxy", {"trusted_proxies": PROXY})
    check(srv.saved_config()["admin_ui"]["trusted_proxies"] == [PROXY], f"{where}: not saved")
    trusted = Browser(host, port)
    trusted.login(prefix)
    page = trusted.form(f"{prefix}/settings/api-token", {})
    token = re.search(r"s2n_[A-Za-z0-9_\-]+", page)
    check(token, f"{where}: Settings -> REST API showed no new token")
    time.sleep(0.3)
    log = srv.output()
    check(f"AUDIT {PROXY} reverse_proxy_set trusted proxies {PROXY}" in log,
          f"{where}: the change isn't audited from nginx's address: {log[-1500:]}")
    check(f"login: admin (admin, local) from {CLIENT}" in log, f"{where}: {log[-1500:]}")
    check(re.search(rf"(?m)^.*AUDIT {re.escape(CLIENT)} api_token", log),
          f"{where}: the token isn't audited from the client's address: {log[-1500:]}")
    print(f"{where}: the client's address {PROXY} (X-Forwarded-For ignored), Trusted proxies "
          f"{PROXY} saved through the form, then {CLIENT} in all.log and the audit log  OK")
    # the console and the hex view
    asyncio.run(consoles(trusted, prefix, pty))
    print(f"{where}: the console (from and to the device) and the hex view over WebSockets  OK")
    # the REST API
    auth = {"Authorization": f"Bearer {token.group(0)}"}
    st, _h, body = trusted.request("GET", f"{prefix}/api/v1/mappings", headers=auth)
    check(st == 200 and [m["id"] for m in json.loads(body)["mappings"]] == [MID],
          f"{where}: GET {prefix}/api/v1/mappings {st} {body[:300]}")
    st, _h, body = trusted.request("GET", f"{prefix}/api/v1/openapi.json")
    servers = json.loads(body).get("servers") if st == 200 else None
    check(servers == [{"url": f"{prefix}/api/v1"}], f"{where}: OpenAPI servers {servers}")
    print(f"{where}: the REST API with a token, OpenAPI's servers {prefix}/api/v1  OK")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nginx", default=shutil.which("nginx") or "/usr/sbin/nginx")
    args = ap.parse_args(argv)
    check(os.access(args.nginx, os.X_OK), f"no nginx at {args.nginx}")
    print(subprocess.run([args.nginx, "-v"], capture_output=True, text=True).stderr.strip())
    map_line, blocks = doc_blocks()
    tmp = tempfile.mkdtemp(prefix="nginx-proxy-")
    try:
        cert, key = certificate(tmp)
        for location, host, prefix in VARIANTS:
            run_variant(args.nginx, tmp, location, host, prefix, blocks[location], map_line,
                        cert, key)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nPASS: ser2netweb behind docs/REVERSE-PROXY.md's nginx configurations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
