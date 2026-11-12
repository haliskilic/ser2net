"""Calling the web app in this process (no socket): an HTTP request or a WebSocket handshake
from any client address, with any headers.

    with App(config_dict) as a:                 # AppState + build_app, data dir in a temp folder
        st, headers, body = a.call("GET", "/login", client="172.18.0.5",
                                   headers={"X-Forwarded-For": "198.51.100.7"})
        a.lines                                  # what the app logged (all.log lines)

`headers` (given and returned) are lists of (lower-case name, value); returned ones keep
every Set-Cookie. a.cookies(headers) -> {name: (value, attributes)}.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import urllib.parse

import _server  # noqa: F401  (puts the repo root and lib/ on sys.path)

from app.config import AppConfig, ConfigStore
from app.state import AppState
from app.web.server import build_app


def _close_loggers() -> None:
    for name in list(logging.root.manager.loggerDict):
        if name == "ser2net" or name.startswith("ser2net."):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _headers(headers) -> list[tuple[bytes, bytes]]:
    items = headers.items() if isinstance(headers, dict) else (headers or [])
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in items]


class App:
    def __init__(self, config: dict) -> None:
        self.tmp = tempfile.mkdtemp(prefix="s2n-asgi-")
        self.state = AppState(ConfigStore(os.path.join(self.tmp, "config.json")),
                              AppConfig.from_dict(config))
        self.lines: list[str] = []
        log = self.state.log
        self.state.log = lambda msg: (self.lines.append(msg), log(msg))[1]  # type: ignore[method-assign]
        self.app = build_app(self.state)
        self.loop = asyncio.new_event_loop()

    def __enter__(self) -> "App":
        return self

    def __exit__(self, *exc) -> None:
        # the mappings a test saved, and their bind retries (a port that was taken), end
        # on this loop instead of being destroyed with it
        self.loop.run_until_complete(self.state.supervisor.stop_all())
        self.loop.close()
        _close_loggers()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method: str, path: str, *, fields: dict | None = None, headers=None,
             client: str = "127.0.0.1", host: str = "127.0.0.1:8080"):
        """One request: (status, headers, body text). `fields`: a urlencoded form body."""
        return self.loop.run_until_complete(self._call(method, path, fields, headers, client, host))

    async def _call(self, method, path, fields, headers, client, host):
        path, _, query = path.partition("?")
        body = urllib.parse.urlencode(fields or {}).encode()
        hdrs = _headers(headers)
        if not any(k == b"host" for k, _v in hdrs):
            hdrs.insert(0, (b"host", host.encode()))
        if fields is not None:
            hdrs += [(b"content-type", b"application/x-www-form-urlencoded"),
                     (b"content-length", str(len(body)).encode())]
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
                 "headers": hdrs, "client": (client, 50000), "server": ("127.0.0.1", 8080)}
        done, sent = asyncio.Event(), [False]
        out: dict = {"status": 0, "headers": [], "body": b""}

        async def receive():
            if not sent[0]:
                sent[0] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = [(k.decode("latin-1").lower(), v.decode("latin-1"))
                                  for k, v in msg["headers"]]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")
                if not msg.get("more_body"):
                    done.set()

        await asyncio.wait_for(self.app(scope, receive, send), 60)
        return out["status"], out["headers"], out["body"].decode("utf-8", "replace")

    def websocket(self, path: str, *, headers=None, client: str = "127.0.0.1",
                  host: str = "127.0.0.1:8080") -> dict:
        """A WebSocket handshake: the app's first message ({'type': 'websocket.accept'}, or
        'websocket.close' with its code)."""
        return self.loop.run_until_complete(self._websocket(path, headers, client, host))

    async def _websocket(self, path, headers, client, host):
        hdrs = _headers(headers)
        if not any(k == b"host" for k, _v in hdrs):
            hdrs.insert(0, (b"host", host.encode()))
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "headers": hdrs,
                 "client": (client, 50000), "server": ("127.0.0.1", 8080), "subprotocols": []}
        first: asyncio.Future = asyncio.get_running_loop().create_future()
        connected = [False]

        async def receive():
            if not connected[0]:
                connected[0] = True
                return {"type": "websocket.connect"}
            await first
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(msg):
            if not first.done():
                first.set_result(msg)

        task = asyncio.ensure_future(self.app(scope, receive, send))
        msg = await asyncio.wait_for(first, 30)
        try:
            await asyncio.wait_for(task, 10)
        except Exception:
            task.cancel()
        return msg


def header(headers, name: str) -> str | None:
    return next((v for k, v in headers if k == name.lower()), None)


def cookies(headers) -> dict[str, tuple[str, str]]:
    """{name: (value, the rest of its Set-Cookie line in lower case)} of a response."""
    out = {}
    for k, v in headers:
        if k == "set-cookie":
            pair, _, attrs = v.partition(";")
            name, _, value = pair.partition("=")
            out[name.strip()] = (value.strip().strip('"'), attrs.lower())
    return out
