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
  - on Linux: TCP_USER_TIMEOUT = idle + interval * count, so the same bound also
    applies while data is in flight to a dead peer (otherwise ~15 min of
    retransmissions, tcp_retries2). Windows already gives up after ~20 s there.
"""
from __future__ import annotations

import socket
import sys


def _ioctl_socket(sock):
    """The real socket.socket behind asyncio's TransportSocket (which has no ioctl)."""
    real = getattr(sock, "_sock", sock)
    return real if hasattr(real, "ioctl") else None


def enable_keepalive(sock, idle_s: int, interval_s: int, count: int) -> None:
    """Enable TCP keepalive on a connected TCP socket (a ``socket.socket`` or the
    ``TransportSocket`` from ``writer.get_extra_info("socket")``).

    Raises OSError if the OS rejects SO_KEEPALIVE or the timing; the probe count and
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
        if sys.platform.startswith("linux"):
            optional.append(("TCP_USER_TIMEOUT", (idle_s + interval_s * count) * 1000))
        for name, value in optional:
            opt = getattr(socket, name, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
                except OSError:
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
    writer's socket. Returns why it could not be enabled, or None (also when the
    mapping has keepalive turned off)."""
    if not net.tcp_keepalive:
        return None
    sock = writer.get_extra_info("socket")
    if sock is None:
        return "no underlying socket"
    try:
        enable_keepalive(sock, net.keepalive_idle_s, net.keepalive_interval_s,
                         net.keepalive_count)
    except OSError as e:
        return str(e)
    return None


def describe(net) -> str:
    """Keepalive settings as shown in the mapping's startup log line."""
    if not net.tcp_keepalive:
        return "keepalive off"
    return (f"keepalive {net.keepalive_idle_s}s/{net.keepalive_interval_s}s"
            f"x{net.keepalive_count}")
