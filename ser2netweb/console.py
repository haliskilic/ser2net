"""First-launch console picker for the admin UI bind IP/port.

On first run (or with --reconfigure) we ask, on the terminal, which local IP the
configuration web UI should listen on — chosen from the machine's own addresses
(every interface for IPv4, IPv6 or both, loopback, each NIC address) or a custom one.
Every interface of both families is stored as "" (asyncio's every interface). In a
non-interactive context (service/headless) we keep the address configured, else
127.0.0.1 (see choose_admin_bind).

The port offered is tried first (port_in_use): when another program has it — the
usual case is a second instance on the same machine — the picker says so and offers
the next free port, but takes a port typed in (it never refuses one: that program may
be gone by the time this one starts). Without a terminal the port stays as it is, with
a warning for the log.
"""
from __future__ import annotations

import errno
import os
import shlex
import socket
import sys

from . import envvars, layout
from .config import AdminUI, host_port, shown_host
from .engine import bindhint, netinfo

# how far past a taken port the picker looks for a free one
_FREE_PORT_TRIES = 50


def port_in_use(ip: str, port: int) -> bool:
    """Does another socket listen on ip:port now, so that the admin UI couldn't bind it?
    Tried as asyncio's create_server binds (uvicorn's listener): SO_REUSEADDR on POSIX,
    IPV6_V6ONLY for an IPv6 address; the socket is closed at once. Only "address in use"
    counts: an address this machine doesn't have is not this check's business. "" (every
    interface of both families) is tried as 0.0.0.0 and as ::, as httpguard.listen binds it."""
    if not ip:
        return port_in_use("0.0.0.0", port) or port_in_use("::", port)
    try:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    with sock:
        try:
            if os.name == "posix" and sys.platform != "cygwin":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((ip, port))
        except OSError as e:
            # Windows: WSAEADDRINUSE, or WSAEACCES for a port held exclusively / reserved
            return e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) in (10048, 10013)
    return False


def next_free_port(ip: str, port: int) -> int | None:
    """The first port after `port` that port_in_use() finds free on `ip`, or None."""
    for candidate in range(port + 1, min(port + _FREE_PORT_TRIES, 65535) + 1):
        if not port_in_use(ip, candidate):
            return candidate
    return None


def in_use_warning(ip: str, port: int) -> str:
    return (f"WARNING: port {port} on {shown_host(ip)} is in use (another ser2netweb instance or another "
            "program?) — the web UI can't listen there until it is free. Give this instance "
            "its own port: --reconfigure, or SER2NETWEB_BIND_IP + SER2NETWEB_PORT")


def _quoted(arg: str) -> str:
    if os.name == "nt":
        return f'"{arg}"' if " " in arg else arg
    return shlex.quote(arg)


def reconfigure_hint(data_dir: str, service_name: str = "", environ=None) -> tuple[str, str]:
    """How this installation changes the admin UI's address (for the Settings page):
    ("env", "") when SER2NETWEB_BIND_IP (or SER2NET_BIND_IP) sets it (a container: its
    environment wins at every start); ("service", command) for the Windows service, whose
    reinstall with --reconfigure asks for it again; else ("console", command), a start with
    --reconfigure."""
    if envvars.text("SER2NETWEB_BIND_IP", environ):
        return "env", ""
    if getattr(sys, "frozen", False):
        prog = _quoted(sys.executable)
    elif layout.kind() == layout.PIP:
        prog = "ser2netweb"  # the command pip / pipx installed
    else:
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "ser2netweb.py")
        prog = f"{_quoted(sys.executable)} {_quoted(script)}"
    if service_name:
        # (ser2net: 2.x's default service, which --install-service of the default moves over)
        name = ("" if service_name in ("ser2netweb", "ser2net")
                else f" --service-name {_quoted(service_name)}")
        return "service", f"{prog} --install-service --reconfigure{name}"
    return "console", f"{prog} --reconfigure --data-dir {_quoted(data_dir)}"


def choose_admin_bind(current: AdminUI | None = None,
                      notes: list[str] | None = None) -> tuple[str, int]:
    """(bind IP, port) for the admin UI, asked on the console. Without a terminal the
    current (or default) address. A warning for the start-up log goes to `notes` when the
    port kept is taken."""
    candidates = netinfo.list_ip_candidates()
    # (a configured "" is every interface, IPv4 and IPv6: a choice made here before)
    default_ip = current.bind_ip if current is not None else "127.0.0.1"
    default_port = (current.port if current else None) or 8080

    if not sys.stdin or not sys.stdin.isatty():
        # No terminal to prompt at (service/headless). Default to LOOPBACK so the
        # admin UI is never network-exposed without an explicit choice + password.
        where = "loopback" if default_ip in ("127.0.0.1", "::1", "localhost") else "as configured"
        print(f"[ser2netweb] non-interactive console; admin UI bind defaults to "
              f"{host_port(default_ip, default_port)} ({where}). Run with a terminal or "
              f"--reconfigure to expose it on the network.", flush=True)
        if port_in_use(default_ip, default_port):
            warning = in_use_warning(default_ip, default_port)
            print(f"[ser2netweb] {warning}", flush=True)
            if notes is not None:
                notes.append(warning)
        return default_ip, default_port

    print("\n=== ser2netweb — configuration interface setup ===")
    print("Choose which local IP address the web config UI should listen on:\n")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}) {c['label']}")
    print("  C) Custom IP address")

    ip = default_ip
    while True:
        raw = input(f"\nSelection [1-{len(candidates)} or C] (default 1): ").strip() or "1"
        if raw.lower() == "c":
            custom = input("  Enter custom IP: ").strip()
            try:
                ip = netinfo.validate_ip(custom)
                break
            except ValueError as e:
                print(f"  {e}")
                continue
        try:
            idx = int(raw)
            if 1 <= idx <= len(candidates):
                ip = candidates[idx - 1]["value"]
                if ip == netinfo.BOTH_FAMILIES:
                    ip = ""  # every interface, IPv4 and IPv6 (as asyncio reads "")
                break
        except ValueError:
            pass
        print("  Invalid selection, try again.")

    if port_in_use(ip, default_port):
        free = next_free_port(ip, default_port)
        print(f"  Port {default_port} on {shown_host(ip)} is in use (another ser2netweb instance or another "
              "program?)" + (f"; {free} is free." if free else "."))
        default_port = free or default_port
    while True:
        raw = input(f"Web UI TCP port [{default_port}]: ").strip() or str(default_port)
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                break
        except ValueError:
            pass
        print("  Invalid port, try again.")
    if port_in_use(ip, port):
        print(f"  NOTE: port {port} on {shown_host(ip)} is in use right now: the web UI can't listen "
              "there until the program that has it stops.")
        if notes is not None:
            notes.append(in_use_warning(ip, port))

    if note := bindhint.admin_port_note(port):
        print(f"  NOTE: {note.removeprefix('WARNING: ')}")
    print(f"\n[ser2netweb] Configuration UI will listen on  http://{host_port(ip, port)}")
    if ip not in ("127.0.0.1", "localhost", "::1"):
        print("[ser2netweb] NOTE: this is reachable from the network. You will set an "
              "admin password on first access.")
    print()
    return ip, port
