#!/usr/bin/env python3
"""pyser2net — expose local serial ports over the network, managed from a web UI.

Entry point. Responsibilities kept deliberately thin:
  1. Make bundled dependencies importable (offline bootstrap into ./lib).
  2. Parse CLI args and locate the config/data directory.
  3. Hand off to app.runtime, which owns the event loop and orchestration.

Run:
    python3 ser2net.py                 # normal start (first run asks bind IP on console)
    python3 ser2net.py --reconfigure   # re-pick the admin UI bind IP/port on the console
    python3 ser2net.py --help
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ser2net.py",
        description="Serial-to-network bridge with a web config UI.",
    )
    p.add_argument(
        "--data-dir",
        default=os.path.join(ROOT, "data"),
        help="Directory for config.json, backups and logs (default: ./data).",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.json (default: <data-dir>/config.json).",
    )
    p.add_argument(
        "--reconfigure",
        action="store_true",
        help="Re-run the console picker for the admin UI bind IP/port, then start.",
    )
    p.add_argument(
        "--online",
        action="store_true",
        help="Allow the dependency bootstrap to fall back to PyPI if a wheel is missing.",
    )
    p.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Skip the dependency bootstrap (assume dependencies are already importable).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 1. dependencies
    sys.path.insert(0, ROOT)  # ensure local packages (app/, bootstrap) import first
    if not args.no_bootstrap:
        import bootstrap

        bootstrap.ensure(online=args.online)
    else:
        lib = os.path.join(ROOT, "lib")
        if os.path.isdir(lib):
            sys.path.insert(0, lib)

    # 2. resolve config path
    config_path = args.config or os.path.join(args.data_dir, "config.json")

    # 3. hand off
    from app import runtime

    return runtime.main(config_path=config_path, reconfigure=args.reconfigure)


if __name__ == "__main__":
    raise SystemExit(main())
