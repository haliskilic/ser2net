#!/usr/bin/env bash
# ser2net launcher for Linux/macOS. Requires Python 3.11+.
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "[ser2net] Python 3.11+ not found (python3/python)." >&2
    exit 1
fi

exec "$PY" ser2net.py "$@"
