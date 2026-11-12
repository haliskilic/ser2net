"""TLS certificates that change while ser2netweb runs: new connections get a context built from
the settings and files as they are now, connections already open keep theirs, and no
listener is restarted (the admin UI's AdminGuard and a mapping's netguard.Listener read
their context at each accept).

``prepare(build)`` is the one place a context is made for that: ``build()`` makes it from
the settings and their files (it raises when they can't be used) and runs in a worker
thread; the caller hands the result to the listener only when it is usable.
"""
from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Prepared:
    """What prepare() made: a context to use (None: no TLS), or why there is none."""
    context: Optional[ssl.SSLContext] = None
    error: str = ""   # the files can't be used (the change is refused, the context in use stays)
    later: str = ""   # usable only from the next start (no listener in this process)
    exc: Optional[BaseException] = None   # what made `error` (engine.tls.TlsFileError: the
    #                                       Settings page explains it in its language)

    @property
    def usable(self) -> bool:
        return not (self.error or self.later)


async def prepare(build: Callable[[], Optional[ssl.SSLContext]]) -> Prepared:
    """A new TLS context from ``build()`` (None for no TLS), made in a worker thread."""
    try:
        return Prepared(await asyncio.to_thread(build))
    except (OSError, ValueError) as e:  # (engine.tls.TlsFileError is a ValueError)  # ssl.SSLError is an OSError
        return Prepared(error=str(e) or type(e).__name__, exc=e)
