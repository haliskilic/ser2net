"""Test helpers for code that must work with many files open: count this process's open
descriptors, and hold enough of them that the next file opened gets a number >= 1024
(FD_SETSIZE, above which select() refuses a descriptor). POSIX only.
"""
import contextlib
import os


def open_fds() -> int:
    for d in ("/proc/self/fd", "/dev/fd"):
        with contextlib.suppress(OSError):
            return len(os.listdir(d))
    raise RuntimeError("cannot count open files")


class Skip(Exception):
    pass


@contextlib.contextmanager
def high_fds(at: int = 1024):
    """Hold descriptors until every number below ``at`` is taken, so the next file opened
    gets ``at`` or above (the soft open-file limit is raised for this if it must be)."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = at + 256
    raised = False
    if soft != resource.RLIM_INFINITY and soft < need:
        if hard != resource.RLIM_INFINITY and hard < need:
            raise Skip(f"the hard open-file limit ({hard}) is below {need}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
        raised = True
    held = []
    try:
        while not held or held[-1] < at - 1:
            held.append(os.open(os.devnull, os.O_RDONLY))
        yield
    finally:
        for fd in held:
            os.close(fd)
        if raised:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
