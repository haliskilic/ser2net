"""First-launch console picker for the admin UI bind IP/port.

On first run (or with --reconfigure) we ask, on the terminal, which local IP the
configuration web UI should listen on — chosen from the machine's own addresses
or a custom one. In a non-interactive context (service/headless) we default to
0.0.0.0:8080 so the UI is reachable from the network, matching the design intent.
"""
from __future__ import annotations

import sys

from .config import AdminUI
from .engine import netinfo


def choose_admin_bind(current: AdminUI | None = None) -> tuple[str, int]:
    candidates = netinfo.list_ip_candidates()
    default_ip = (current.bind_ip if current and current.bind_ip else None) or "127.0.0.1"
    default_port = (current.port if current else None) or 8080

    if not sys.stdin or not sys.stdin.isatty():
        # No terminal to prompt at (service/headless). Default to LOOPBACK so the
        # admin UI is never network-exposed without an explicit choice + password.
        if not (current and current.bind_ip):
            default_ip = "127.0.0.1"
        print(f"[ser2net] non-interactive console; admin UI bind defaults to "
              f"{default_ip}:{default_port} (loopback). Run with a terminal or "
              f"--reconfigure to expose it on the network.", flush=True)
        return default_ip, default_port

    print("\n=== ser2net — configuration interface setup ===")
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
                print(f"  Invalid IP: {e}")
                continue
        try:
            idx = int(raw)
            if 1 <= idx <= len(candidates):
                ip = candidates[idx - 1]["value"]
                break
        except ValueError:
            pass
        print("  Invalid selection, try again.")

    while True:
        raw = input(f"Web UI TCP port [{default_port}]: ").strip() or str(default_port)
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                break
        except ValueError:
            pass
        print("  Invalid port, try again.")

    print(f"\n[ser2net] Configuration UI will listen on  http://{ip}:{port}")
    if ip not in ("127.0.0.1", "localhost", "::1"):
        print("[ser2net] NOTE: this is reachable from the network. You will set an "
              "admin password on first access.")
    print()
    return ip, port
