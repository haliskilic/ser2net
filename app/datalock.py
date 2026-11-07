"""One running ser2net per data directory.

Two instances on the same data directory — a second start while the Windows service or
the systemd unit runs, --reconfigure next to the running server — would each save their
own copy of config.json over the other's and lose each other's changes. runtime.main()
therefore takes an exclusive lock on <data>/ser2net.lock before it writes anything and
holds it until the server has stopped; a second start on that directory stops with one
line that names the process holding it. The operating system drops the lock when the
process ends, however it ends, so a crash leaves no stale lock behind (the file itself
stays; earlier releases ignore it).

Where the lock can't be taken — a read-only data directory, a network share without
lock support — the start goes on without it and the start-up log says so.
POSIX: flock(); Windows: a byte-range lock (msvcrt.locking).
"""
from __future__ import annotations

import errno
import os
import sys

LOCK_NAME = "ser2net.lock"
# A Windows byte-range lock also keeps other processes from reading that range: lock a
# byte far past the holder's process id (written at the start of the file), so a start
# that is refused can still name the holder.
_WIN_OFFSET = 1 << 30
_held: dict[str, int] = {}  # data directory -> fd that holds its lock


class Held(Exception):
    """Another process holds the data directory's lock."""

    def __init__(self, data_dir: str, pid: int | None):
        self.data_dir, self.pid = data_dir, pid
        who = f"another ser2net (process {pid})" if pid else "another ser2net"
        super().__init__(f"{who} is already running with the data directory {data_dir}")


def _lock(fd: int) -> bool:
    """Lock fd without waiting: True when locked, False when another process holds it.
    Any other OSError means this file system can't lock."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _holder_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 32).decode("ascii", "replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _open(path: str) -> tuple[int, bool]:
    """(fd, writable). Read-only is enough for the lock itself (the process id isn't
    written then); raises OSError when the file can't be opened at all."""
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600), True
    except OSError:
        if not os.path.isfile(path):
            raise
    return os.open(path, os.O_RDONLY), False


def acquire(data_dir: str) -> str | None:
    """Hold data_dir's lock until release() or the end of the process. Raises Held when
    another process holds it. Returns a warning for the start-up log when it can't be
    taken here (the start goes on without it), else None."""
    data_dir = os.path.abspath(data_dir)
    if data_dir in _held:
        return None
    path = os.path.join(data_dir, LOCK_NAME)
    try:
        fd, writable = _open(path)
    except OSError as e:
        return (f"WARNING: can't lock the data directory ({path}: {e.strerror or e}) — "
                "nothing keeps a second ser2net from using it at the same time")
    try:
        locked = _lock(fd)
    except OSError as e:
        os.close(fd)
        return (f"WARNING: can't lock the data directory ({path}: locking is not "
                f"supported there: {e.strerror or e}) — nothing keeps a second ser2net "
                "from using it at the same time")
    if not locked:
        pid = _holder_pid(fd)
        os.close(fd)
        raise Held(data_dir, pid)
    if writable:
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass  # the lock holds; only the name in a refusal is lost
    _held[data_dir] = fd
    return None


def release(data_dir: str) -> None:
    fd = _held.pop(os.path.abspath(data_dir), None)
    if fd is not None:
        os.close(fd)  # closing the file drops the lock


def holder(data_dir: str) -> int | None:
    """Without keeping it: the process id holding data_dir's lock (0 when it can't be
    read), or None when nobody holds it. For --install-service, which must leave the
    lock to the service it starts."""
    path = os.path.join(os.path.abspath(data_dir), LOCK_NAME)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None  # no lock file: no ser2net that locks has run here
    try:
        if _lock(fd):
            _unlock(fd)
            return None
        return _holder_pid(fd) or 0
    except OSError:
        return None
    finally:
        os.close(fd)
