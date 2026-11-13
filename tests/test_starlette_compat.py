"""ser2netweb/web/starlette_compat.py: Starlette's request body limits, or the stand-ins for a
Starlette before 1.6 (a distribution's older Starlette, docs/PACKAGING.md).

  * with the Starlette installed here, the names are Starlette's own where it has them
    (the pinned set: nothing of ser2netweb's is used);
  * the copy of RequestBodyLimitMiddleware and the Route with max_body_size (_CopyRoute)
    answer every request as Starlette's own do: the application's limit, a route's larger
    or smaller one in its place, a body announced too large (Content-Length) or one that
    turns out too large while it is read (no Content-Length, several chunks), an endpoint
    that doesn't read the body; where the installed Starlette has its own (1.6+), both
    are run and compared. CI's job "minimum dependencies" runs the whole suite on a
    Starlette without them, where the copies are what the app uses.

In-process ASGI calls only (binds nothing).

Run: python3 tests/test_starlette_compat.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import starlette
from starlette import routing
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse

from ser2netweb.web import starlette_compat as sc

APP_LIMIT = 100


async def read_all(request):
    return PlainTextResponse(str(len(await request.body())))


async def no_read(request):
    return PlainTextResponse("not read")


def make_app(middleware, route):
    routes = [route("/default", read_all, methods=["POST"]),
              route("/small", read_all, methods=["POST"], max_body_size=10),
              route("/big", read_all, methods=["POST"], max_body_size=1000),
              route("/noread", no_read, methods=["POST"])]
    return Starlette(routes=routes, middleware=[Middleware(middleware, max_body_size=APP_LIMIT)])


async def call(app, path, chunks, content_length=True):
    body = b"".join(chunks)
    headers = [(b"host", b"t"), (b"content-type", b"application/octet-stream")]
    if content_length:
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80)}
    messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
                for i, c in enumerate(chunks)]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    return starts[0]["status"], b"".join(m.get("body", b"") for m in sent
                                         if m["type"] == "http.response.body")


# (path, chunks, with a Content-Length) -> (status, body)
CASES = [
    (("/default", [b"x" * 50], True), (200, b"50")),
    (("/default", [b"x" * 100], True), (200, b"100")),
    (("/default", [b"x" * 101], True), (413, b"Content Too Large")),
    (("/default", [b"x" * 60, b"x" * 60], False), (413, b"Content Too Large")),
    (("/default", [b"x" * 40, b"x" * 40], False), (200, b"80")),
    (("/small", [b"x" * 11], True), (413, b"Content Too Large")),
    (("/small", [b"x" * 6, b"x" * 6], False), (413, b"Content Too Large")),
    (("/small", [b"x" * 10], True), (200, b"10")),
    (("/big", [b"x" * 500], True), (200, b"500")),
    (("/big", [b"x" * 400] * 2, False), (200, b"800")),
    (("/big", [b"x" * 1001], True), (413, b"Content Too Large")),
    (("/noread", [b"x" * 50], True), (200, b"not read")),
    (("/noread", [b"x" * 200], True), (413, b"Content Too Large")),
]


def answers(app):
    return [asyncio.run(call(app, *args)) for args, _want in CASES]


def test_native_where_starlette_has_it():
    try:
        from starlette.middleware.body_limit import RequestBodyLimitMiddleware
    except ImportError:
        assert sc.RequestBodyLimitMiddleware is sc._CopyRequestBodyLimitMiddleware
        assert sc.Route is sc._CopyRoute and not sc.ROUTE_LIMIT_NATIVE
        print(f"Starlette {starlette.__version__}: no body limits of its own, ser2netweb's "
              "stand-ins are used  OK")
        return
    assert sc.RequestBodyLimitMiddleware is RequestBodyLimitMiddleware
    assert sc.ROUTE_LIMIT_NATIVE and sc.Route is routing.Route
    print(f"Starlette {starlette.__version__}: its own RequestBodyLimitMiddleware and Route "
          "are used  OK")


def test_copies_answer_as_starlette():
    got = answers(make_app(sc._CopyRequestBodyLimitMiddleware, sc._CopyRoute))
    for (args, want), have in zip(CASES, got):
        assert have == want, (args[0], len(b"".join(args[1])), args[2], have, want)
    compared = ""
    if sc.ROUTE_LIMIT_NATIVE:
        native = answers(make_app(sc.RequestBodyLimitMiddleware, routing.Route))
        assert native == got, [(a[0], n, g) for (a, _w), n, g in zip(CASES, native, got)
                               if n != g]
        mixed = answers(make_app(sc._CopyRequestBodyLimitMiddleware, routing.Route))
        assert mixed == got, "Starlette's Route with the copied middleware"
        compared = f", the same as Starlette {starlette.__version__}'s own"
    print(f"the stand-ins: the application's limit, a route's own one, announced or "
          f"streamed bodies, an endpoint that doesn't read ({len(CASES)} requests){compared}"
          "  OK")


if __name__ == "__main__":
    test_native_where_starlette_has_it()
    test_copies_answer_as_starlette()
    print("\nPASS: Starlette's body limits, or ser2netweb's stand-ins")
