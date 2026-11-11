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
  - a deadline (admin_ui.request_timeout_s) for every request head: from the accept and from
    the end of each response until a complete request head is in. A WebSocket (the
    console) is not timed once it is upgraded.
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any, Callable, Optional

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol

from ..engine import netguard


class AdminGuard:
    """What the admin UI's connections share (one per server)."""

    def __init__(self, admin: Any, log: Callable[[str], None],
                 ssl_context: Optional[ssl.SSLContext] = None) -> None:
        self.connections = netguard.Pool(admin.max_connections, admin.max_connections_per_ip)
        self.request_timeout = admin.request_timeout_s
        self.ssl = ssl_context
        self.server: Any = None  # the uvicorn.Server: should_exit = shutting down
        self.timeouts = 0  # connections closed for sending no complete request in time
        self._refused = netguard.LogEvery(log)
        self._failed = netguard.LogEvery(lambda msg: log(f"admin UI: {msg}"))
        self._handshakes: dict[Any, asyncio.Task] = {}

    def stopping(self) -> bool:
        return bool(self.server is not None and self.server.should_exit)

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
                                  self._failed)
        self._handshakes[transport] = task
        task.add_done_callback(lambda _t: self._handshakes.pop(transport, None))

    def close(self) -> None:
        """Shutting down: end the TLS handshakes still in progress."""
        for transport in list(self._handshakes):
            transport.abort()


class GuardedH11Protocol(H11Protocol):
    """H11Protocol with admission, TLS after admission and a request-head deadline."""

    guard: AdminGuard  # set by protocol_class()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._accepted: Any = None  # the accepted socket's transport (TLS: the one under it)
        self._served = False  # H11Protocol.connection_made ran: uvicorn serves it
        self._head_timer: Optional[asyncio.TimerHandle] = None

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
        self._arm()

    def data_received(self, data: bytes) -> None:
        if not self._served:
            return
        super().data_received(data)
        if self._head_timer is not None and self.conn.their_state is not h11.IDLE:
            self._disarm()  # a complete request head is in

    def on_response_complete(self) -> None:
        super().on_response_complete()
        if not self.transport.is_closing() and self.conn.their_state is h11.IDLE:
            self._arm()  # the next request head has the same time

    def handle_websocket_upgrade(self, event: h11.Request) -> None:
        self._disarm()
        super().handle_websocket_upgrade(event)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._disarm()
        if self._accepted is not None:
            self.guard.connections.discard(self._accepted)
        if self._served:
            super().connection_lost(exc)

    def _arm(self) -> None:
        self._disarm()
        if self.guard.request_timeout > 0:
            self._head_timer = self.loop.call_later(self.guard.request_timeout,
                                                    self._head_timeout)

    def _disarm(self) -> None:
        if self._head_timer is not None:
            self._head_timer.cancel()
            self._head_timer = None

    def _head_timeout(self) -> None:
        self._head_timer = None
        if not self.transport.is_closing():
            self.guard.timeouts += 1
            self.transport.close()


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
