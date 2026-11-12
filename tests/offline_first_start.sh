#!/bin/sh
# The first start of a source checkout without network (CI jobs "offline first start ...":
# Linux arm64, macOS, and 32-bit ARM in an emulated Debian container), where vendor/wheels
# has no psutil wheel for the machine:
#   tests/offline_first_start.sh [python]     # from a checkout without lib/
# 1. bootstrap.py installs vendor/wheels into ./lib with pip --no-index; without a psutil
#    wheel for this machine it installs the set without it, and says so;
# 2. the app imports its dependencies from ./lib, and ser2netweb.py --about runs;
# 3. ser2net starts from ./lib on a fresh data directory and answers /healthz (the
#    interface lists then come from the standard library); then it is stopped.
# ./lib and the data directory are removed again.
set -eu
PY=${1:-python3}
cd "$(dirname "$0")/.."
[ ! -e lib ] || { echo "FAIL: ./lib exists already (run this on a fresh checkout)"; exit 1; }
data=$(mktemp -d)
pid=""
cleanup() {
    [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
    rm -rf lib "$data"
}
trap cleanup EXIT

"$PY" --version
echo "== 1. bootstrap.py (pip --no-index, vendor/wheels)"
"$PY" bootstrap.py > "$data/bootstrap.log" 2>&1 || { cat "$data/bootstrap.log"; exit 1; }
cat "$data/bootstrap.log"

echo "== 2. the app imports from ./lib; --about"
psutil=$("$PY" -c "import os, sys; sys.path[:0] = ['lib', '.']
import importlib.util, serial, starlette, uvicorn, jinja2, ser2netweb.runtime, ser2netweb.web.server
lib = os.path.abspath('lib')
for m in (serial, starlette, uvicorn, jinja2):
    assert os.path.abspath(m.__file__).startswith(lib), m.__file__
spec = importlib.util.find_spec('psutil')
print('yes' if spec and os.path.abspath(spec.origin).startswith(lib) else 'no')")
if [ "$psutil" = no ]; then
    grep -q "installed without psutil" "$data/bootstrap.log" \
        || { echo "FAIL: psutil missing from ./lib, and bootstrap.py didn't say so"; exit 1; }
fi
echo "psutil in ./lib: $psutil"
"$PY" ser2netweb.py --about

echo "== 3. a start from ./lib answers /healthz"
SER2NETWEB_BIND_IP=127.0.0.1 SER2NETWEB_PORT=18091 "$PY" ser2netweb.py --data-dir "$data/d" \
    > "$data/server.log" 2>&1 &
pid=$!
ok=""
for _ in $(seq 1 120); do  # (emulated: a start takes much longer)
    if "$PY" ser2netweb.py --no-bootstrap --data-dir "$data/d" --healthcheck >/dev/null 2>&1; then
        ok=1
        break
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
[ -n "$ok" ] || { echo "FAIL: no /healthz"; cat "$data/server.log"; exit 1; }
kill "$pid"
wait "$pid" || true
pid=""
if grep -q "Traceback" "$data/server.log"; then
    cat "$data/server.log"
    echo "FAIL: a traceback in the server's output"
    exit 1
fi
echo "PASS: offline first start (psutil in ./lib: $psutil)"
