#!/usr/bin/env bash
# ser2net launcher for Linux/macOS. Requires Python 3.10+.
# Uses python3 when it is 3.10 or newer, else the newest python3.1x on PATH (RHEL /
# AlmaLinux / Rocky 8 and 9 ship an older python3: sudo dnf install python3.11), else python.
set -euo pipefail
cd "$(dirname "$0")"

new_enough() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

PY=""
for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
    if new_enough "$candidate"; then
        PY=$candidate
        break
    fi
done
if [ -z "$PY" ]; then
    found=$(command -v python3 python 2>/dev/null | tr '\n' ' ' || true)
    echo "[ser2net] Python 3.10+ not found (tried python3, python3.14 ... python3.10, python;" \
         "found: ${found:-none})." >&2
    echo "          Install a newer Python (RHEL / AlmaLinux / Rocky: sudo dnf install" \
         "python3.11), or use the standalone binary." >&2
    exit 1
fi

exec "$PY" ser2netweb.py "$@"
