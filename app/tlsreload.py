"""TLS certificates that change while ser2net runs: new connections get a context built from
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


class NeedsRestart(Exception):
    """The files can be used only when ser2net starts."""


def refuse_passphrase(keyfile: str) -> None:
    """Raise NeedsRestart for a PEM key protected by a passphrase. OpenSSL would ask for it
    on the terminal (and wait there) if it were loaded while ser2net runs; at start-up it
    is asked for on the console, as before. A file that can't be read is left to the load,
    which says why."""
    try:
        with open(keyfile, "rb") as fh:
            head = fh.read(64 * 1024)
    except (OSError, TypeError, ValueError):
        return
    if b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in head or b"Proc-Type: 4,ENCRYPTED" in head:
        raise NeedsRestart(f"the key {keyfile} has a passphrase, which ser2net asks for on the "
                           "console when it starts")


@dataclass
class Prepared:
    """What prepare() made: a context to use (None: no TLS), or why there is none."""
    context: Optional[ssl.SSLContext] = None
    error: str = ""   # the files can't be used (the change is refused, the context in use stays)
    later: str = ""   # usable only from the next start (the context in use stays until then)

    @property
    def usable(self) -> bool:
        return not (self.error or self.later)


async def prepare(build: Callable[[], Optional[ssl.SSLContext]]) -> Prepared:
    """A new TLS context from ``build()`` (None for no TLS), made in a worker thread."""
    try:
        return Prepared(await asyncio.to_thread(build))
    except NeedsRestart as e:
        return Prepared(later=str(e))
    except (OSError, ValueError) as e:  # ssl.SSLError is an OSError
        return Prepared(error=str(e) or type(e).__name__)
