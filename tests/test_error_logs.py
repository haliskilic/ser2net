"""Errors that happen outside ser2netweb's own log lines still reach all.log.

Checks:
  * other libraries' warnings and errors (uvicorn's: a request it can't parse, a port it
    can't use, the traceback of a request that failed) go to all.log, each message at most
    once a minute with a count; asyncio's only to the console (the event loop's exception
    handler already writes them to all.log); a real server logs a malformed request;
  * a request whose handler fails is logged with its 500 and the exception;
  * a client that resets the connection while the banner is written is a normal
    disconnect with its reason, not an error of the event loop;
  * all.log that can't be renamed for its rotation (Windows: another program has it open)
    keeps every record and every backup, says so once, and is rotated once it can be.

Run: python3 tests/test_error_logs.py
"""
import asyncio
import io
import logging
import os
import shutil
import socket
import sys
import tempfile
import time

from _server import Browser, Server

from ser2netweb import logsetup
from ser2netweb import state as state_mod
from ser2netweb.config import AppConfig, ConfigStore, MappingConfig
from ser2netweb.engine import bridge
from ser2netweb.state import AppState
from ser2netweb.web import auth
from ser2netweb.web.server import build_app


def _close_loggers():
    for name in list(logging.root.manager.loggerDict) + ["ser2net"]:
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            logsetup.close_console(lg)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logsetup.LibraryLogHandler):
            logging.getLogger().removeHandler(h)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_library_logs():
    d = tempfile.mkdtemp(prefix="s2n_liblog_")
    saved_out = sys.stdout
    console = io.StringIO()
    try:
        sys.stdout = console
        lg = AppState._setup_logging(os.path.join(d, "all.log"))
        logsetup.route_library_logs(lg)
        handler = logsetup.route_library_logs(lg)  # again: still one handler
        assert sum(isinstance(h, logsetup.LibraryLogHandler)
                   for h in logging.getLogger().handlers) == 1
        uv = logging.getLogger("uvicorn.error")
        for _ in range(5):
            uv.warning("Invalid HTTP request received.")
        uv.info("Started server process")  # below WARNING: not taken
        try:
            raise RuntimeError("handler broke")
        except RuntimeError as e:
            uv.error("Exception in ASGI application\n", exc_info=e)
        logging.getLogger("asyncio").error("Exception in callback X()")
        logging.getLogger("ser2net.test").warning("not twice")  # (through "ser2net")
        handler._next = dict.fromkeys(handler._next, 0.0)  # a minute later
        uv.warning("Invalid HTTP request received.")
        text = _read(os.path.join(d, "all.log"))
        lines = text.splitlines()
        warn = [ln for ln in lines if "Invalid HTTP request received." in ln]
        assert len(warn) == 2, warn
        assert warn[0].endswith(" WARNING: uvicorn.error: Invalid HTTP request received."), warn
        assert warn[1].endswith("(4 more since the last such line)"), warn
        at = next(i for i, ln in enumerate(lines) if "ERROR: uvicorn.error: Exception in ASGI" in ln)
        assert lines[at + 1] == "Traceback (most recent call last):", lines[at:at + 3]
        assert "RuntimeError: handler broke" in text
        assert "Started server process" not in text
        assert "asyncio" not in text and "Exception in callback X()" in console.getvalue()
        assert text.count("not twice") == 1, "a ser2netweb logger's record is written once"
        print("library logs: uvicorn's warnings/errors in all.log with traceback, repeats "
              "counted; asyncio's to the console only  OK")
    finally:
        sys.stdout = saved_out
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


def test_malformed_request_on_a_real_server():
    with Server({"users": []}) as srv:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as s:
            s.sendall(b"NOT HTTP AT ALL\r\n\r\n")
            with __import__("contextlib").suppress(OSError):
                s.recv(4096)
        log = ""
        for _ in range(50):
            log = _read(os.path.join(srv.data, "all.log"))
            if "uvicorn" in log:
                break
            time.sleep(0.1)
        assert "WARNING: uvicorn.error: Invalid HTTP request received." in log, log[-1500:]
        assert Browser(srv.base).get("/healthz")[0] == 200
    print("real server: a request uvicorn can't parse is logged in all.log  OK")


async def _asgi_get(app, path, headers):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "client": ("10.1.2.3", 5555),
             "server": ("127.0.0.1", 8080),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    error = None
    try:
        await app(scope, receive, send)
    except Exception as e:  # what uvicorn then logs as "Exception in ASGI application"
        error = e
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, error


def test_failed_request_logged():
    from starlette.routing import Route
    d = tempfile.mkdtemp(prefix="s2n_500_")
    try:
        cfg = AppConfig.from_dict({"users": [{"username": "admin", "password_hash": "x",
                                              "role": "admin"}],
                                   "api_token_hash": auth.hash_token("s2n_test_token")})
        state = AppState(ConfigStore(os.path.join(d, "config.json")), cfg)
        app = build_app(state)

        async def boom(request):
            raise RuntimeError("the handler broke")
        app.router.routes.append(Route("/api/v1/boom", boom))
        status, error = asyncio.run(_asgi_get(app, "/api/v1/boom",
                                              {"Authorization": "Bearer s2n_test_token"}))
        assert status == 500 and isinstance(error, RuntimeError), (status, error)
        log = _read(state.log_path)
        assert "HTTP 10.1.2.3 GET /api/v1/boom -> 500 (RuntimeError: the handler broke)" in log, log
        print("a request whose handler fails: 500 and the exception in all.log  OK")
    finally:
        _close_loggers()
        shutil.rmtree(d, ignore_errors=True)


class _ResetWriter:
    def __init__(self):
        self.transport = self
        self.closed = False

    def write(self, data):
        pass

    async def drain(self):
        raise ConnectionResetError(104, "Connection reset by peer")

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def is_closing(self):
        return self.closed

    def abort(self):
        self.closed = True

    def get_extra_info(self, name, default=None):
        return default


def test_reset_during_banner():
    m = MappingConfig.from_dict({"name": "b", "serial": {"port": "/dev/ttyX"},
                                 "network": {"protocol": "telnet", "bind_ip": "127.0.0.1",
                                             "port": 1},
                                 "options": {"banner": "hello \\N\r\n"}})
    logs = []
    runner = bridge.MappingRunner(m, logger=logs.append)
    w = _ResetWriter()

    async def run():
        conn = bridge._ClientConn(runner, asyncio.StreamReader(), w, "10.0.0.9", 4242)
        await conn.run()  # returns: a normal disconnect
        return conn
    conn = asyncio.run(run())
    assert conn.close_reason == bridge.describe_disconnect(ConnectionResetError()), conn.close_reason
    assert w.closed and conn._closing
    print("client resets while the banner is written: a disconnect with its reason  OK")


def test_rotation_blocked():
    d = tempfile.mkdtemp(prefix="s2n_rotblock_")
    path = os.path.join(d, "all.log")
    real_replace = os.replace
    blocked = {"on": True}

    def replace(src, dst, *a, **k):
        if blocked["on"] and os.path.abspath(src) == path:
            raise PermissionError(13, "The process cannot access the file because it is "
                                      "being used by another process", src)
        return real_replace(src, dst, *a, **k)
    saved_retry = state_mod.ROLL_RETRY_S
    h = state_mod.RetainingFileHandler(path, maxBytes=300, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("t.rotation.blocked")
    lg.handlers.clear()
    lg.propagate = False
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        for i in (1, 2, 3):
            with open(f"{path}.{i}", "w") as fh:
                fh.write(f"backup {i}\n")
        os.replace = replace
        for i in range(40):
            lg.info("record %d %s", i, "x" * 30)
        text = _read(path)
        assert all(f"record {i} " in text for i in range(40)), "every record kept"
        assert text.count("could not be rotated") == 1, text
        for i in (1, 2, 3):
            assert _read(f"{path}.{i}") == f"backup {i}\n", "the backups are left as they were"
        assert not os.path.exists(path + ".4") and not os.path.exists(path + ".rotating")
        # it can be renamed again: rotated at the first record after the retry time
        blocked["on"] = False
        state_mod.ROLL_RETRY_S = 0.0
        h._retry_at = time.monotonic() - 1
        lg.info("after")
        assert "record 39" in _read(path + ".1") and _read(path + ".2") == "backup 1\n"
        assert _read(path + ".3") == "backup 2\n" and _read(path).strip() == "after"
        print("all.log held open by another program: every record and backup kept, said "
              "once, rotated once it can be  OK")
    finally:
        os.replace = real_replace
        state_mod.ROLL_RETRY_S = saved_retry
        h.close()
        lg.handlers.clear()
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_library_logs()
    test_failed_request_logged()
    test_reset_during_banner()
    test_rotation_blocked()
    test_malformed_request_on_a_real_server()
    print("\nPASS: errors outside ser2netweb's own lines reach all.log")


if __name__ == "__main__":
    main()
