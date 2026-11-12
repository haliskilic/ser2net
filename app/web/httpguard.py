"""Connection limits for the admin UI's listener: uvicorn's h11 protocol, subclassed.

uvicorn's h11 server keeps a connection for as long as its client does while no complete
request arrives: no timer runs before the first request, and the keep-alive timer after a
response stops at the first byte of the next one. limit_concurrency answers requests with
503 but never closes a socket. So anyone who could reach the port could hold any number of
idle sockets, unauthenticated, until the process ran out of descriptors (on Windows, until
select() refused a socket and the event loop stopped).

``protocol_class(guard)`` is the class given to uvicorn.Config(http=...); ``AdminGuard``
holds what its connections share:
  - admission right after accept, before TLS: the process's budget (netguard.BUDGET), then
    admin_ui.max_connections in all and admin_ui.max_connections_per_ip from one address.
    A refused socket is closed, and logged at most once a minute;
  - TLS (admin_ui.tls_cert / tls_key) is done here once a connection is admitted, as on a
    mapping's listener (netguard.begin_tls: handshake timeout, handshakes in progress
    capped); uvicorn itself is given no certificate;
  - a deadline (admin_ui.request_timeout_s) for every request: from the accept and from
    the end of each response until a complete request head is in, then for its body,
    which has to keep coming at BODY_MIN_RATE on average (a body the app answered before
    reading too), until the whole request is in;
  - a send deadline: a connection whose client takes nothing of what it was sent for
    admin_ui.request_timeout_s is aborted (also while it closes after a response);
  - a connection that runs out of time is aborted, not closed (a TLS close would wait for
    the client). A WebSocket (the console) is not timed once it is upgraded.
"""
from __future__ import annotations

import asyncio
import contextlib
import ssl
from typing import Any, Callable, Optional

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol

from ..engine import netguard

# a request's body has admin_ui.request_timeout_s from the end of its head, and one second
# more for each BODY_MIN_RATE bytes of it that came in: a client that sends it slower than
# this on average runs out of time
BODY_MIN_RATE = 1024


class AdminGuard:
    """What the admin UI's connections share (one per server)."""

    def __init__(self, admin: Any, log: Callable[[str], None],
                 ssl_context: Optional[ssl.SSLContext] = None) -> None:
        self.connections = netguard.Pool(admin.max_connections, admin.max_connections_per_ip)
        self.request_timeout = admin.request_timeout_s
        self.ssl = ssl_context
        self.server: Any = None  # the uvicorn.Server: should_exit = shutting down
        self.timeouts = 0  # connections aborted: no complete request, or nothing taken, in time
        self._refused = netguard.LogEvery(log)
        self._failed = netguard.LogEvery(lambda msg: log(f"admin UI: {msg}"))
        self._handshakes: dict[Any, asyncio.Task] = {}

    def stopping(self) -> bool:
        return bool(self.server is not None and self.server.should_exit)

    def use_limits(self, admin: Any) -> None:
        """admin_ui's connection limits as they are now (a reload): from the next
        connection or request on."""
        self.connections.total = admin.max_connections
        self.connections.per_ip = admin.max_connections_per_ip
        self.request_timeout = admin.request_timeout_s

    def admit(self, transport: Any) -> bool:
        """Whether an accepted connection may in; a refused one is closed and logged."""
        ip = netguard.peer_ip(transport)
        why = netguard.BUDGET.headroom()
        if not why:
            why = self.connections.full(ip)
            if why:
                netguard.BUDGET.note_refused("admin_ui")
                why = f"too many connections to the admin UI ({why})"
        if not why and self.ssl is not None:
            why = netguard.BUDGET.handshake_room(ip)
        if why or self.stopping():
            if why:
                self._refused(f"admin UI: refused {netguard.peer_label(transport)}: {why}")
            transport.close()
            return False
        self.connections.add(transport, ip)
        return True

    def begin_tls(self, transport: Any, protocol: asyncio.Protocol) -> None:
        task = netguard.begin_tls(transport, self.ssl, protocol, netguard.peer_label(transport),
                                  self._failed, lambda msg: self._refused(f"admin UI: {msg}"))
        self._handshakes[transport] = task
        task.add_done_callback(lambda _t: self._handshakes.pop(transport, None))

    def close(self) -> None:
        """Shutting down: end the TLS handshakes still in progress (or waiting)."""
        for transport, task in list(self._handshakes.items()):
            transport.abort()
            task.cancel()


class GuardedH11Protocol(H11Protocol):
    """H11Protocol with admission, TLS after admission, a request deadline and a send
    deadline."""

    guard: AdminGuard  # set by protocol_class()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._accepted: Any = None  # the accepted socket's transport (TLS: the one under it)
        self._served = False  # H11Protocol.connection_made ran: uvicorn serves it
        self._upgraded = False  # a WebSocket now: not timed any more
        # the request deadline: for its head ("head"), then for its body ("body"), whose
        # deadline grows with each part of it that comes in (_body_since/_body_bytes)
        self._request_timer: Optional[asyncio.TimerHandle] = None
        self._waiting_for = ""
        self._body_since = 0.0
        self._body_bytes = 0
        # the send deadline (_watch_send)
        self._send_timer: Optional[asyncio.TimerHandle] = None
        self._send_mark: Optional[tuple[int, int]] = None
        self._resumed = 0  # how often the client took enough for writing to resume

    def connection_made(self, transport: Any) -> None:
        if self._accepted is not None:  # the TLS transport, from netguard.start_tls
            self._serve(transport)
            return
        if not self.guard.admit(transport):
            return
        self._accepted = transport
        if self.guard.ssl is None:
            self._serve(transport)
        else:
            self.guard.begin_tls(transport, self)

    def _serve(self, transport: Any) -> None:
        if self.guard.stopping():
            transport.close()
            return
        self._served = True
        super().connection_made(transport)
        self._follow_request(0, fresh=True)

    def data_received(self, data: bytes) -> None:
        if not self._served:
            return
        super().data_received(data)
        self._follow_request(len(data))

    def on_response_complete(self) -> None:
        super().on_response_complete()
        if self.transport.is_closing():
            self._watch_send()  # what is left of the response still has to go out
        self._follow_request(0, fresh=True)  # the next request has the same time

    def handle_websocket_upgrade(self, event: h11.Request) -> None:
        self._upgraded = True
        self._stop_timers()
        super().handle_websocket_upgrade(event)

    def pause_writing(self) -> None:
        super().pause_writing()
        self._watch_send()

    def resume_writing(self) -> None:
        super().resume_writing()
        self._resumed += 1

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._stop_timers()
        if self._accepted is not None:
            self.guard.connections.discard(self._accepted)
        if self._served:
            super().connection_lost(exc)

    def _stop_timers(self) -> None:
        self._stop_request_timer()
        if self._send_timer is not None:
            self._send_timer.cancel()
            self._send_timer = None

    def _abort(self) -> None:
        if not self.transport.is_closing():
            self.guard.timeouts += 1
        # abort, not close: nothing more can be sent on time, and closing a TLS connection
        # waits for the client's close_notify, which a silent client never sends (on Python
        # 3.10 for good)
        self.transport.abort()

    # -- the request deadline ------------------------------------------------------------
    def _follow_request(self, received: int, fresh: bool = False) -> None:
        """Set the request deadline for where the request is now (``received``: bytes
        that just came in; ``fresh``: a new connection, or a response just ended)."""
        if self._upgraded or self.guard.request_timeout <= 0:
            return
        state = self.conn.their_state
        if state is h11.IDLE:  # waiting for a request head, or part of one is in
            if fresh or self._waiting_for != "head":
                self._waiting_for = "head"
                self._set_request_timer(self.loop.time() + self.guard.request_timeout)
        elif state is h11.SEND_BODY:  # the head is in, the body is coming
            if self._waiting_for != "body":
                self._waiting_for, self._body_since, self._body_bytes = \
                    "body", self.loop.time(), 0
            else:
                self._body_bytes += received
            self._set_request_timer(self._body_since + self.guard.request_timeout
                                    + self._body_bytes / BODY_MIN_RATE)
        else:  # the whole request is in (or the connection is ending)
            self._stop_request_timer()

    def _set_request_timer(self, when: float) -> None:
        if self._request_timer is not None:
            self._request_timer.cancel()
        self._request_timer = self.loop.call_at(when, self._request_expired)

    def _stop_request_timer(self) -> None:
        self._waiting_for = ""
        if self._request_timer is not None:
            self._request_timer.cancel()
            self._request_timer = None

    def _request_expired(self) -> None:
        self._request_timer = None
        if self._waiting_for == "body" and (self.flow.read_paused or
                                            self.conn.they_are_waiting_for_100_continue):
            # the server holds the body up, not the client: the app has not taken what
            # came yet (uvicorn reads no more until it does), or the client waits for the
            # "100 Continue" sent once the app reads; the rest gets the full time again
            self._body_since = self.loop.time() - self._body_bytes / BODY_MIN_RATE
            self._follow_request(0)
            return
        self._abort()

    # -- the send deadline ---------------------------------------------------------------
    def _watch_send(self) -> None:
        """Abort the connection when the client takes nothing of what it was sent for a
        whole request_timeout_s. Judged only while nothing can be added to what is waiting
        to go out (writing is paused until the client takes some, or the response is over
        and the connection closing): if it did not shrink, the client took none of it."""
        if self._send_timer is None and self.guard.request_timeout > 0 and not self._upgraded:
            self._send_mark = self._send_state()
            self._send_timer = self.loop.call_later(self.guard.request_timeout,
                                                    self._check_send)

    def _send_state(self) -> Optional[tuple[int, int]]:
        """(times writing resumed, bytes waiting to go out) while nothing can be added to
        them, else None."""
        if not (self.flow.write_paused or self.transport.is_closing()):
            return None
        unsent = 0
        for transport in {self.transport, self._accepted}:  # TLS: both layers' buffers
            with contextlib.suppress(Exception):  # (one that is gone holds nothing)
                unsent += transport.get_write_buffer_size()
        return self._resumed, unsent

    def _check_send(self) -> None:
        self._send_timer = None
        before, now = self._send_mark, self._send_state()
        if now is None or not now[1]:
            return  # more may be written now, or all of it is out: a pause watches again
        if before is not None and now[0] == before[0] and now[1] >= before[1]:
            self._abort()  # the client took nothing in all that time
            return
        self._send_mark = now
        self._send_timer = self.loop.call_later(self.guard.request_timeout, self._check_send)


def protocol_class(guard: AdminGuard) -> type[GuardedH11Protocol]:
    """The http protocol class for uvicorn.Config(http=...), bound to ``guard``."""
    return type("AdminH11Protocol", (GuardedH11Protocol,), {"guard": guard})


def ssl_context(certfile: str, keyfile: str, config: Any) -> ssl.SSLContext:
    """The admin UI's TLS context, made as uvicorn makes it from ssl_certfile/ssl_keyfile
    (its defaults, read from ``config``, a uvicorn.Config, for the rest)."""
    from uvicorn.config import create_ssl_context
    return create_ssl_context(certfile=certfile, keyfile=keyfile,
                              password=config.ssl_keyfile_password,
                              ssl_version=config.ssl_version, cert_reqs=config.ssl_cert_reqs,
                              ca_certs=config.ssl_ca_certs, ciphers=config.ssl_ciphers)
