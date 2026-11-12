"""Shared helpers for the end-to-end tests that boot a real ser2net server.

    from _server import Server, Browser, free_port
    with Server(config_dict) as srv:          # data dir in a temp folder, UI on 127.0.0.1
        b = Browser(srv.base)
        b.login("admin", "pw")
        status, headers, body = b.post("/settings/access", {...})

The server runs as a subprocess (`ser2net.py --no-bootstrap`) with SER2NET_BIND_IP set,
like a Docker / headless boot, and logs to a file (never an unread pipe).
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, ROOT)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port: int, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def wait_served(port: int, timeout: float = 20) -> bool:
    """Wait until the admin UI on 127.0.0.1:``port`` answers (anything, a close too: a TLS
    listener closes a plain request). Its port listens before the mappings start and is
    served only after, so this also means the mappings have started."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
                s.settimeout(2)
                s.sendall(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
                s.recv(1)
                return True
        except OSError:  # refused, reset, or no answer yet
            time.sleep(0.2)
    return False


def silent_tls_client(port: int, ctx, timeout: float = 5) -> socket.socket:
    """A TLS client that completes its handshake (``ctx``) with 127.0.0.1:``port`` and
    then neither reads nor answers anything, not even the server's close_notify. Returns
    the plain socket under it: ``closed_by_server`` tells when the server really let go."""
    import ssl

    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing)
    while True:
        try:
            tls.do_handshake()
            break
        except ssl.SSLWantReadError:
            raw.sendall(outgoing.read())
            data = raw.recv(65536)
            if not data:
                raise ConnectionError("closed during the TLS handshake") from None
            incoming.write(data)
    raw.sendall(outgoing.read())  # the client's last handshake message
    return raw


def closed_by_server(raw: socket.socket, seconds: float) -> float:
    """Seconds until the server closed the plain socket ``raw`` (whatever it sent before);
    raises AssertionError when it is still open after ``seconds``."""
    t0 = time.monotonic()
    while True:
        left = seconds - (time.monotonic() - t0)
        if left <= 0:
            raise AssertionError(f"the server kept the connection open past {seconds} s")
        raw.settimeout(left)
        try:
            if not raw.recv(65536):
                return time.monotonic() - t0
        except socket.timeout:
            continue  # checked at the top
        except OSError:  # reset
            return time.monotonic() - t0


class Server:
    def __init__(self, config: dict | None = None, data_dir: str | None = None, env=None):
        self.tmp = tempfile.mkdtemp(prefix="s2n_e2e_")
        self.data = data_dir or os.path.join(self.tmp, "data")
        os.makedirs(self.data, exist_ok=True)
        self.config_path = os.path.join(self.data, "config.json")
        self.port = free_port()
        if config is not None:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(config, fh, indent=2)
        self.env = dict(os.environ, SER2NET_BIND_IP="127.0.0.1", SER2NET_PORT=str(self.port),
                        **(env or {}))
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self, served: bool = True) -> "Server":
        """Start it and wait until the admin UI answers (its mappings have started then);
        served=False: only until its port takes a connection."""
        self.log_path = os.path.join(self.tmp, "server.log")
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "ser2net.py", "--no-bootstrap", "--data-dir", self.data],
            cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, stdout=self._log,
            stderr=subprocess.STDOUT)
        if not (wait_served(self.port) if served else wait_port(self.port)):
            raise AssertionError("server did not start:\n" + self.output()[-2000:])
        return self

    def output(self) -> str:
        self._log.flush()
        with open(self.log_path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")

    def saved_config(self) -> dict:
        with open(self.config_path, encoding="utf-8") as fh:
            return json.load(fh)

    def stop(self) -> None:
        if getattr(self, "proc", None) is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._log.close()
        self.proc = None

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def ui_cookie(name: str, port, scheme: str = "http", path: str = "/") -> str:
    """The name the web UI gives its cookie `name` (ser2net_session, ser2net_csrf, ...) at
    scheme://127.0.0.1:`port`: tagged by the port unless it is 8080, with __Host- over https
    (app/web/proxy.cookie_name; test_cookie_names pins the scheme)."""
    from app.web import proxy

    host = "127.0.0.1" + ("" if port is None else f":{port}")
    scope = {"type": "http", "scheme": scheme, "path": "/", "headers": [(b"host", host.encode())]}
    return proxy.cookie_name(scope, name, path)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    """A cookie-keeping client that speaks the UI's CSRF scheme (form field + header)."""

    def __init__(self, base: str):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                              _NoRedirect())

    def cookie(self, name: str):
        """The UI's cookie `name` under its name at this address (ui_cookie: ser2net_csrf ->
        ser2net_csrf_<tag> on a port other than 8080), else the jar's cookie `name`."""
        u = urllib.parse.urlsplit(self.base)
        names = (ui_cookie(name, u.port, u.scheme), name)
        return next((c for n in names for c in self.jar if c.name == n), None)

    def csrf(self) -> str:
        for page in ("/login", "/setup"):  # before first-run setup only /setup sets it
            if self.cookie("ser2net_csrf") is None:
                self.get(page)
        return self.cookie("ser2net_csrf").value

    def _open(self, req):
        try:
            r = self.op.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            r = e
        body = r.read()
        # headers: an http.client.HTTPMessage (get / get_all, e.g. every Set-Cookie)
        return r.status if hasattr(r, "status") else r.code, r.headers, body

    def get(self, path: str, raw: bool = False):
        st, headers, body = self._open(urllib.request.Request(self.base + path))
        return st, headers, (body if raw else body.decode("utf-8", "replace"))

    def post(self, path: str, fields=None, files=None, htmx: bool = False, method: str = "POST"):
        fields = dict(fields or {})
        fields.setdefault("_csrf", self.csrf())
        headers = {"X-CSRF-Token": self.csrf()}
        if htmx:
            headers["HX-Request"] = "true"
        if files:
            boundary = "s2nboundary7c1f"
            parts = []
            for k, v in fields.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k, (fname, content) in files.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                             f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                             + content + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            data = urllib.parse.urlencode(fields, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        st, h, body = self._open(urllib.request.Request(self.base + path, data=data,
                                                        headers=headers, method=method))
        return st, h, body.decode("utf-8", "replace")

    def login(self, username: str, password: str) -> None:
        st, h, _ = self.post("/login", {"username": username, "password": password})
        assert st == 303 and h.get("Location") == "/", f"login as {username} failed ({st})"

    def setup(self, username: str = "admin", password: str = "admin-pass-1") -> None:
        st, _, _ = self.post("/setup", {"username": username, "password": password,
                                        "password2": password})
        assert st == 303, f"first-run setup failed ({st})"
