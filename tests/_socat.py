"""A socat-linked PTY pair for the data-path tests (Linux; needs the socat binary).

    from _socat import pty_pair, pty_pair_sync
    proc, port, device = await pty_pair()     # asyncio: an asyncio subprocess
    proc, port, device = pty_pair_sync()      # blocking: a subprocess.Popen

socat links two pseudo-terminals and reports each on stderr ("PTY is /dev/pts/N", -d -d):
one end is the mapping's serial port, the other the test's device. If socat exits before
it reported both (no PTYs left, an option this socat refuses), its stderr ends: that fails
at once with what socat said, instead of reading the empty stream for good; one that
reports nothing within ``timeout`` seconds fails too. Nothing is read from stderr after
that: socat writes a line or two per connection there, far less than a pipe holds.
Stop it with proc.terminate() (and wait()).
"""
from __future__ import annotations

import asyncio
import re
import select
import subprocess
import time

ARGS = ["socat", "-d", "-d", "pty,raw,echo=0", "pty,raw,echo=0"]
_PTY = re.compile(rb"PTY is (\S+)")


def _failed(said: bytes, why: str) -> RuntimeError:
    return RuntimeError(f"socat {why}: " + said.decode("utf-8", "replace").strip()[-500:])


async def pty_pair(timeout: float = 10.0):
    """(process, port end, device end) of a new socat PTY pair."""
    proc = await asyncio.create_subprocess_exec(*ARGS, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    devs: list[str] = []
    said = b""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    try:
        while len(devs) < 2:
            try:
                line = await asyncio.wait_for(proc.stderr.readline(), max(0.0, end - loop.time()))
            except asyncio.TimeoutError:
                raise _failed(said, f"reported no PTYs within {timeout} s") from None
            if not line:
                raise _failed(said, "exited before it reported its PTYs")
            said += line
            m = _PTY.search(line)
            if m:
                devs.append(m.group(1).decode())
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return proc, devs[0], devs[1]


def pty_pair_sync(timeout: float = 10.0):
    """pty_pair for a test without an event loop."""
    # unbuffered: a line read ahead into a buffer would be invisible to select()
    proc = subprocess.Popen(ARGS, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, bufsize=0)
    devs: list[str] = []
    said = b""
    end = time.monotonic() + timeout
    try:
        while len(devs) < 2:
            ready, _w, _x = select.select([proc.stderr], [], [], max(0.0, end - time.monotonic()))
            if not ready:
                raise _failed(said, f"reported no PTYs within {timeout} s")
            line = proc.stderr.readline()
            if not line:
                raise _failed(said, "exited before it reported its PTYs")
            said += line
            m = _PTY.search(line)
            if m:
                devs.append(m.group(1).decode())
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return proc, devs[0], devs[1]
