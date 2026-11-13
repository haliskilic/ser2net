"""What ser2netweb uses of Starlette that older Starlette releases lack.

Linux distributions build ser2netweb against the Starlette they ship, which may be older
than the pinned one (docs/PACKAGING.md: Debian 13 has 0.46). Each name here is
Starlette's own wherever the installed release has it (the pinned set: Starlette's,
unchanged), and otherwise a stand-in that does the same:
  - ``RequestBodyLimitMiddleware`` (Starlette 1.6): a request whose body is over the limit
    gets 413 Content Too Large; a route's own limit replaces the application's. The
    stand-in is Starlette 1.10's starlette/middleware/body_limit.py, copied (below, with
    its licence);
  - ``Route`` with ``max_body_size=`` (Starlette 1.6): the route's endpoint wrapped in the
    middleware above, as Starlette's own Route does it (``_CopyRoute``).
tests/test_starlette_compat.py checks the stand-ins against Starlette's own, and CI's
job "minimum dependencies" runs the suite with the oldest Starlette pyproject.toml allows.
"""
from __future__ import annotations

import inspect
from typing import Any, Union, cast

from starlette import routing as _routing
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------- the copy (Starlette 1.10)
# starlette/middleware/body_limit.py of Starlette 1.10.0, unchanged but for the names
# (_Copy...), the typing of the casts (Union, for Python 3.10), the noqa markers and
# these comments:
#
# Copyright © 2018, Encode OSS Ltd (https://www.encode.io/).
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

MAX_BODY_SIZE_SCOPE_KEY = "starlette.max_body_size"
_BODY_LIMIT_RESPONDER_SCOPE_KEY = "starlette._body_limit_responder"


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


class _RequestBodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Content Too Large")


class _RequestBodyLimitResponseSent(Exception):
    pass


class _CopyRequestBodyLimitMiddleware:
    """Limit the total size of an HTTP request body."""

    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        responder = _CopyRequestBodyLimitResponder(self.app, self.max_body_size)
        await responder(scope, receive, send)


class _CopyRequestBodyLimitResponder:
    def __init__(self, app: ASGIApp, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size
        self._scope: Scope | None = None
        self._receive: Receive | None = None
        self._send: Send | None = None
        self.content_length: int | None = None
        self.total_size = 0
        self.response_started = False

    @property
    def scope(self) -> Scope:
        assert self._scope is not None  # noqa: S101
        return self._scope

    @property
    def receive(self) -> Receive:
        assert self._receive is not None  # noqa: S101
        return self._receive

    @property
    def send(self) -> Send:
        assert self._send is not None  # noqa: S101
        return self._send

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        previous_scope_limit = cast(Union[int, _Missing],
                                    scope.get(MAX_BODY_SIZE_SCOPE_KEY, _MISSING))
        scope[MAX_BODY_SIZE_SCOPE_KEY] = self.max_body_size

        active_responder = cast(Union[_CopyRequestBodyLimitResponder, None],
                                scope.get(_BODY_LIMIT_RESPONDER_SCOPE_KEY))
        if active_responder is not None:
            active_responder.max_body_size = self.max_body_size
            if active_responder.total_size > active_responder.max_body_size:
                raise _RequestBodyTooLarge
            return await self.app(scope, receive, send)

        self._scope = scope
        self._receive = receive
        self._send = send
        self.content_length = _get_content_length(scope)
        scope[_BODY_LIMIT_RESPONDER_SCOPE_KEY] = self

        try:
            await self.app(scope, self.receive_with_limit, self.send_with_limit)
        except _RequestBodyTooLarge:
            if self.response_started:
                raise
            response = PlainTextResponse("Content Too Large", status_code=413)
            await response(scope, receive, send)
        except _RequestBodyLimitResponseSent:
            pass
        finally:
            scope.pop(_BODY_LIMIT_RESPONDER_SCOPE_KEY, None)
            if isinstance(previous_scope_limit, _Missing):
                scope.pop(MAX_BODY_SIZE_SCOPE_KEY, None)
            else:
                scope[MAX_BODY_SIZE_SCOPE_KEY] = previous_scope_limit

    async def receive_with_limit(self) -> Message:
        if self.content_length is not None and self.content_length > self.max_body_size:
            raise _RequestBodyTooLarge

        message = await self.receive()
        if message["type"] == "http.request":
            self.total_size += len(message.get("body", b""))
            if self.total_size > self.max_body_size:
                raise _RequestBodyTooLarge
        return message

    async def send_with_limit(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.response_started = True
            if self.content_length is not None and self.content_length > self.max_body_size:
                response = PlainTextResponse("Content Too Large", status_code=413)
                await response(self.scope, self.receive, self.send)
                raise _RequestBodyLimitResponseSent
        await self.send(message)


def _get_content_length(scope: Scope) -> int | None:
    content_length = Headers(scope=scope).get("content-length")
    if content_length is None:
        return None
    try:
        return int(content_length)
    except ValueError:
        return None
# ------------------------------------------------------------- end of the copy


try:
    from starlette.middleware.body_limit import RequestBodyLimitMiddleware
except ImportError:  # Starlette before 1.6
    RequestBodyLimitMiddleware = _CopyRequestBodyLimitMiddleware  # type: ignore[misc,assignment]


class _CopyRoute(_routing.Route):
    """Starlette's Route, with Starlette 1.6's max_body_size: the endpoint (after the
    route's middleware) inside RequestBodyLimitMiddleware, as Starlette 1.6 does it."""

    def __init__(self, path: str, endpoint: Any, *, max_body_size: int | None = None,
                 **kwargs: Any) -> None:
        super().__init__(path, endpoint, **kwargs)
        if max_body_size is not None:
            self.app = RequestBodyLimitMiddleware(self.app, max_body_size=max_body_size)


# Starlette 1.6: Route(max_body_size=...)
ROUTE_LIMIT_NATIVE = "max_body_size" in inspect.signature(_routing.Route.__init__).parameters
if ROUTE_LIMIT_NATIVE:
    from starlette.routing import Route
else:
    Route = _CopyRoute  # type: ignore[misc]
