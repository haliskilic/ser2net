"""TCP socket options for the data bridges: keepalive-based dead-peer detection.

A peer that disappears WITHOUT a FIN/RST — power cut, cable pull, Wi-Fi/VPN drop,
NAT/firewall state expiry, a crashed VM — leaves our socket ESTABLISHED for as long
as nothing is sent on it. On a raw bridge to a quiet device that is forever, and the
dead client keeps its connection slot (max_connections defaults to 1, so every
reconnect is refused). TCP keepalive makes the kernel probe an idle connection and
abort it when the probes go unanswered; the bridge then sees an ordinary read error
and tears the client down.

``enable_keepalive`` sets, per socket:
  - SO_KEEPALIVE, plus idle / interval / count (Linux, Windows 10 1709+, macOS),
  - on Windows builds without TCP_KEEPIDLE: SIO_KEEPALIVE_VALS (idle + interval;
    the probe count is fixed by the OS at 10),
  - on Linux, when idle + interval * count is shorter than the kernel's own give-up
    time for unacknowledged data (~15 min, tcp_retries2): TCP_USER_TIMEOUT of that
    length, so a peer that dies while data is in flight (keepalive doesn't probe
    then) is dropped in the same window. Since Linux 5.11 this also drops a live
    client whose receive window stays closed (it stopped reading) that long — the
    bridge would drop such a client anyway once its output queue overflows.
    Windows' own retransmission limit is far shorter (tests/test_dead_peer.py).
"""
from __future__ import annotations

import socket
import sys

# Linux gives up on unacknowledged data after ~924 s by default (tcp_retries2 = 15);
# a longer TCP_USER_TIMEOUT would make things worse, not better.
_LINUX_DATA_GIVE_UP_S = 900

# what a bad option value can raise from setsockopt/ioctl (e.g. an int overflow)
_SOCKOPT_ERRORS = (OSError, ValueError, TypeError, OverflowError)


def _ioctl_socket(sock):
    """The real socket.socket behind asyncio's TransportSocket (which has no ioctl)."""
    real = getattr(sock, "_sock", sock)
    return real if hasattr(real, "ioctl") else None


def enable_keepalive(sock, idle_s: int, interval_s: int, count: int) -> None:
    """Enable TCP keepalive on a connected TCP socket (a ``socket.socket`` or the
    ``TransportSocket`` from ``writer.get_extra_info("socket")``).

    Raises if the OS rejects SO_KEEPALIVE or the timing; the probe count and
    TCP_USER_TIMEOUT are best-effort (older kernels/Windows builds lack them).
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None)
    if idle_opt is None and sys.platform == "darwin":
        idle_opt = getattr(socket, "TCP_KEEPALIVE", None)  # macOS name for the idle time
    if idle_opt is not None and hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, idle_opt, idle_s)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_s)
        optional = [("TCP_KEEPCNT", count)]
        reap_s = idle_s + interval_s * count
        if sys.platform.startswith("linux") and reap_s < _LINUX_DATA_GIVE_UP_S:
            optional.append(("TCP_USER_TIMEOUT", reap_s * 1000))
        for name, value in optional:
            opt = getattr(socket, name, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
                except _SOCKOPT_ERRORS:
                    pass
        return
    if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows before 10 1709
        target = _ioctl_socket(sock)
        dup = None
        if target is None:  # no private _sock: a duplicate handle shares the socket
            target = dup = sock.dup()
        try:
            target.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_s * 1000, interval_s * 1000))
        finally:
            if dup is not None:
                dup.close()


def apply_keepalive(writer, net) -> str | None:
    """Apply a mapping's keepalive settings (``NetworkSettings``) to a stream
    writer's socket. Never raises: returns why keepalive could not be enabled, or
    None (also when the mapping has keepalive turned off) — a socket-option problem
    must not take the connection down with it."""
    if not net.tcp_keepalive:
        return None
    sock = writer.get_extra_info("socket")
    if sock is None:
        return "no underlying socket"
    try:
        enable_keepalive(sock, net.keepalive_idle_s, net.keepalive_interval_s,
                         net.keepalive_count)
    except _SOCKOPT_ERRORS as e:
        return f"{type(e).__name__}: {e}"
    return None


def describe(net) -> str:
    """Keepalive settings as shown in the mapping's startup log line."""
    if not net.tcp_keepalive:
        return "keepalive off"
    return (f"keepalive {net.keepalive_idle_s}s/{net.keepalive_interval_s}s"
            f"x{net.keepalive_count}")
