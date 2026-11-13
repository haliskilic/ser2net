"""Port pools (classic ser2net's rotators): one listening TCP port for several mappings.

A pool (config.PortPool) listens on its own address and port. Each client that connects is
handed to the first member mapping that would take it right now, in the order of the
mappings (a pool with ``rotate`` begins after the member that got the last client, as
ser2net's rotator does): one that runs, lets the client's address in, has a connection left and (where a
session needs the serial port) has its port open or opens it for its clients
(bridge.MappingRunner.free_for). From then on the client is that mapping's, exactly as if
it had connected to the mapping's own port: its protocol, banner, timers, counters and log.
Nobody is kicked to make room for a pool's client. When no member is free the client is
told so in one line ("Busy: ...") and disconnected, and the refusal is counted and logged
(rate-limited).

The members are the TCP server mappings of the transparent bridge whose network.pool is
the pool's name (config.pool_member); a mapping with TLS is never one, since the pool's
port is plain TCP. A member keeps its own listening port.

A pool whose address can't be taken (in use, not on this machine yet) is tried again with
a backoff, as a mapping's listener is; its status says why meanwhile.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from dataclasses import asdict, replace
from typing import Any, Callable, Optional

from ..config import PortPool, host_port
from . import netguard
from .serial_link import Backoff

# a pool's listener that could not bind is tried again at most this many seconds apart
BIND_RETRY_MAX_S = 30.0
BUSY_TEXT = b"Busy: every port of the pool is in use.\r\n"


class PoolListener:
    """One pool while it runs. ``members()``: its member runners now, in order."""

    def __init__(self, pool: PortPool, members: Callable[[], list], log: Callable[[str], None]):
        self.pool = replace(pool)  # as it runs (the config's own may be changed in place)
        self._members = members
        self._log = lambda msg: log(f"[pool {pool.name}] {msg}")
        self._refusals = netguard.RefusalLog(self._log)
        self._server: Optional[netguard.Listener] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.state = "stopped"   # stopped | listening | error (its address can't be taken)
        self.last_error = ""
        self.handed = 0          # clients handed to a member
        self.refused = 0         # clients turned away: no member was free
        self.last_refused = 0.0  # time.time() of the last one (0: none)
        self._last = ""          # rotate: the mapping (id) that got the last client

    @property
    def where(self) -> str:
        return host_port(self.pool.bind_ip, self.pool.port)

    @property
    def sockets(self):
        """The listening sockets (empty while it does not listen)."""
        return self._server.sockets if self._server is not None else []

    async def start(self) -> None:
        """Begin to listen; when the address can't be taken, keep trying in the
        background. Returns once the first attempt is over."""
        self._stop.clear()
        if not await self._listen():
            self._task = asyncio.create_task(self._retry(), name=f"pool:{self.pool.name}")

    async def _listen(self) -> bool:
        try:
            self._server = await netguard.start_server(
                self._on_client, [self.pool.bind_ip], self.pool.port, log=self._log)
        except netguard.BindError as e:
            if self.last_error != str(e):
                self._log(f"can't listen on {self.where}: {e} — trying again until the "
                          f"address can be used (every {BIND_RETRY_MAX_S:g} s at most)")
            self.state, self.last_error = "error", str(e)
            return False
        self.state, self.last_error = "listening", ""
        self._log(f"listening on {self.where}")
        return True

    async def _retry(self) -> None:
        backoff = Backoff(cap=BIND_RETRY_MAX_S)
        while await backoff.wait(self._stop):
            if await self._listen():
                return

    async def stop(self) -> None:
        """Stop listening. The clients handed over stay with their mappings."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._server is not None:
            # close() frees the address at once; wait_closed() would wait for every client
            # handed over to leave (Python 3.12+), and they are their mappings' now
            self._server.close()
            self._server = None
            self._log("stopped")
        self.state = "stopped"

    async def _on_client(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        ip, port = netguard.peer_address(writer)
        label = netguard.client_label(ip, port)
        members = [] if self._stop.is_set() else self._members()
        for runner in self._in_turn(members):
            if runner.free_for(ip):
                self.handed += 1
                self._last = runner.mapping.id
                self._log(f"{label} -> mapping '{runner.mapping.name}'")
                await runner.take_client(reader, writer)
                return
        self.refused += 1
        self.last_refused = time.time()
        if not members:
            why = "the pool has no running mapping"
        elif not any(r.lets_in(ip) for r in members):
            why = "not in the allowed list of any mapping of the pool"
        else:
            why = "every port of the pool is in use"
            with contextlib.suppress(Exception):
                writer.write(BUSY_TEXT)
                await writer.drain()
        self._refusals(f"rejected {label}: {why}")
        writer.close()

    def _in_turn(self, members: list) -> list:
        """The members in the order they are tried: as they are, or (rotate) from the one
        after the member that got the last client, round to it."""
        ids = [r.mapping.id for r in members]
        if not self.pool.rotate or self._last not in ids:
            return members
        k = ids.index(self._last) + 1
        return members[k:] + members[:k]

    def status(self) -> dict:
        members = self._members()
        return {
            **asdict(self.pool),
            "state": self.state,
            "last_error": self.last_error,
            "members": [{"id": r.mapping.id, "name": r.mapping.name,
                         "state": r.status.state,
                         "clients": r.status.client_count,
                         "max_connections": r.mapping.network.max_connections}
                        for r in members],
            # members that would take no client now (whoever asks), of those that run
            "busy": sum(1 for r in members if r.full),
            "handed": self.handed,
            "refused": self.refused,
            "last_refused": int(self.last_refused),
        }


class PoolManager:
    """The pools that run (the supervisor owns one). ``members(name)``: a pool's member
    runners, in order."""

    def __init__(self, members: Callable[[str], list], log: Callable[[str], None]) -> None:
        self._members = members
        self._log = log
        self._pools: dict[str, PoolListener] = {}
        self._lock = asyncio.Lock()

    async def apply(self, pools: list[PortPool]) -> None:
        """Run exactly these pools: one that is gone, disabled or changed (its address or
        port) stops, a new or changed one starts; the others go on untouched, with their
        counters."""
        async with self._lock:
            wanted = {p.name: p for p in pools if p.enabled}
            for name, running in list(self._pools.items()):
                if wanted.get(name) != running.pool:
                    del self._pools[name]
                    await running.stop()
            for name, pool in wanted.items():
                if name not in self._pools:
                    listener = PoolListener(pool, functools.partial(self._members, name),
                                            self._log)
                    self._pools[name] = listener
                    await listener.start()

    async def stop_all(self) -> None:
        async with self._lock:
            pools, self._pools = self._pools, {}
            for listener in pools.values():
                await listener.stop()

    def get(self, name: str) -> Optional[PoolListener]:
        return self._pools.get(name)

    def status(self) -> dict[str, dict[str, Any]]:
        """Each running pool's status, by name (PoolListener.status)."""
        return {name: listener.status() for name, listener in list(self._pools.items())}
