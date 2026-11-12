#!/usr/bin/env bash
# Smoke test of the systemd units as shipped (CI jobs "systemd unit" and
# "systemd unit (binary)"). It needs systemd as PID 1 and passwordless sudo, as on GitHub's
# ubuntu runners, and it installs a user, /opt/ser2net and a service: run it only on a
# throwaway machine.
#   tests/systemd_smoke.sh            # systemd/ser2net.service, a source tree in /opt/ser2net
#   tests/systemd_smoke.sh <binary>   # systemd/ser2net-standalone.service with that binary
# The source unit, installed verbatim:
# 1. a fresh tree (git archive: no lib/, no data/): the unit makes data/ and lib/ for the
#    ser2net user (0700), the first start installs lib/ offline, /healthz answers,
#    ActiveState=active, NRestarts=0;
# 2. --set-password as the service user while it runs: handed to the running server;
# 3. the sandbox from inside (a oneshot copy with only ExecStart= / Type= / Restart=
#    changed runs tests/systemd_probe.py): /proc/net hidden, no capability, the interfaces
#    listed by name and the beacons' directed broadcasts, a denied call fails with EPERM;
# 4. a mapping on a socat PTY moves data both ways through the sandbox;
# 5. with a low soft open-file limit (a LimitNOFILE= drop-in) ser2net raises it at start;
# 6. a mapping on port 502 fails with the CAP_NET_BIND_SERVICE hint in all.log; with the
#    README's drop-in (AmbientCapabilities=CAP_NET_BIND_SERVICE) it listens;
# 7. an upgrade: lib/ as 2.8 left it (no record, an old version's metadata, a package added
#    by hand) is refreshed on the next start, once; the added package stays;
# 8. stop: the clean shutdown line, no restart.
# The binary unit: /usr/local/bin/ser2net with its data in /var/lib/ser2net
# (StateDirectory=, 0700, the ser2net user's): /healthz, active, NRestarts=0, a clean stop.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
BINARY=${1:-}
UNIT=/etc/systemd/system/ser2net.service
DROPIN=/etc/systemd/system/ser2net.service.d
UI=http://127.0.0.1:8080
if [ -n "$BINARY" ]; then DATA=/var/lib/ser2net; else DATA=/opt/ser2net/data; fi
LIB=/opt/ser2net/lib
STOPPED="shutting down — stopping all mappings"

journal() { sudo journalctl -u "${1:-ser2net}" --no-pager -o cat -n "${2:-60}" || true; }
fail() {
    echo "FAIL: $*"
    sudo systemctl status ser2net --no-pager || true
    journal ser2net 80
    sudo tail -n 40 "$DATA/all.log" 2>/dev/null || true
    exit 1
}
prop() { systemctl show -p "$2" --value "$1"; }
wait_healthy() {
    for _ in $(seq 1 90); do
        curl -fsS "$UI/healthz" 2>/dev/null | grep -q ok && return 0
        [ "$(prop ser2net ActiveState)" = failed ] && fail "the service failed"
        sleep 1
    done
    fail "no /healthz after 90 s"
}
assert_running() {
    [ "$(prop ser2net ActiveState)" = active ] || fail "ActiveState $(prop ser2net ActiveState)"
    [ "$(prop ser2net NRestarts)" = 0 ] || fail "restarted $(prop ser2net NRestarts) times"
}
in_log() { sudo grep -q -F -- "$1" "$DATA/all.log"; }
count_log() { sudo grep -c -F -- "$1" "$DATA/all.log" || true; }
wait_log() {
    for _ in $(seq 1 30); do in_log "$1" && return 0; sleep 1; done
    fail "all.log never said: $1"
}
dropin() {  # dropin <name> <lines...>: a [Service] drop-in, then daemon-reload
    local name=$1
    shift
    sudo mkdir -p "$DROPIN"
    printf '[Service]\n' | sudo tee "$DROPIN/$name.conf" >/dev/null
    printf '%s\n' "$@" | sudo tee -a "$DROPIN/$name.conf" >/dev/null
    sudo systemctl daemon-reload
}
restart() {
    sudo systemctl restart ser2net || fail "systemctl restart failed"
    wait_healthy
    assert_running
}
clean_stop() {
    local before
    before=$(count_log "$STOPPED")
    sudo systemctl stop ser2net
    [ "$(prop ser2net ActiveState)" = inactive ] || fail "not stopped: $(prop ser2net ActiveState)"
    [ "$(count_log "$STOPPED")" -gt "$before" ] || fail "no clean shutdown line"
    [ "$(prop ser2net NRestarts)" = 0 ] || fail "restarted while stopping"
}

getent passwd ser2net >/dev/null && fail "a ser2net user exists already: not a throwaway machine?"
[ -e "$UNIT" ] && fail "$UNIT exists already: not a throwaway machine?"
systemd-analyze --version | head -n 1

if [ -n "$BINARY" ]; then
    echo "== binary unit: /usr/local/bin/ser2net, data in /var/lib/ser2net"
    sudo install -m 0755 "$BINARY" /usr/local/bin/ser2net
    sudo useradd --system --user-group --home-dir /var/lib/ser2net -M -s /usr/sbin/nologin ser2net
    sudo cp "$REPO/systemd/ser2net-standalone.service" "$UNIT"
    sudo systemctl daemon-reload
    sudo systemctl start ser2net || fail "systemctl start failed"
    wait_healthy
    assert_running
    [ "$(sudo stat -c '%U %a' /var/lib/ser2net)" = "ser2net 700" ] \
        || fail "/var/lib/ser2net: $(sudo stat -c '%U %a' /var/lib/ser2net)"
    sudo test -f /var/lib/ser2net/config.json || fail "no config.json in /var/lib/ser2net"
    systemd-analyze security --no-pager ser2net | tail -n 1 || true
    clean_stop
    echo "binary unit: StateDirectory 0700, /healthz, active without restarts, clean stop  OK"
    echo
    echo "PASS: systemd unit (binary)"
    exit 0
fi

echo "== 1. a fresh tree in /opt/ser2net, the unit as it is"
sudo useradd --system --user-group --home-dir /opt/ser2net -M -s /usr/sbin/nologin ser2net
sudo mkdir /opt/ser2net
git -C "$REPO" archive HEAD | sudo tar -x -C /opt/ser2net
[ ! -e "$LIB" ] && [ ! -e "$DATA" ] || fail "the archive has lib/ or data/"
sudo cp /opt/ser2net/systemd/ser2net.service "$UNIT"
sudo systemctl daemon-reload
started=$(date +%s)
sudo systemctl start ser2net || fail "systemctl start failed"
wait_healthy
assert_running
echo "healthy $(( $(date +%s) - started )) s after the start (the first start installs lib/)"
for d in "$DATA" "$LIB"; do
    [ "$(sudo stat -c '%U %a' "$d")" = "ser2net 700" ] || fail "$d: $(sudo stat -c '%U %a' "$d")"
done
sudo test -f "$LIB/.ser2net-deps.json" || fail "lib/ has no install record"
sudo find /opt/ser2net -path "$DATA" -prune -o -path "$LIB" -prune -o -user ser2net -print \
    | grep -q . && fail "ser2net owns files outside data/ and lib/"
systemd-analyze security --no-pager ser2net | tail -n 1 || true
echo "fresh tree: data/ and lib/ made for ser2net (0700), lib/ installed, active, no restarts  OK"

echo "== 2. --set-password as the service user while it runs"
out=$(printf 'systemd-pass-1\n' | sudo -u ser2net python3 /opt/ser2net/ser2net.py \
    --data-dir "$DATA" --set-password admin 2>&1) || fail "--set-password: $out"
grep -q "done by the running ser2net" <<<"$out" || fail "not handed to the running server: $out"
sudo find "$DATA" ! -user ser2net | grep -q . && fail "files in data/ not owned by ser2net"
echo "--set-password beside the running service  OK"

echo "== 3. the sandbox from inside"
sudo sed -e 's#^ExecStart=.*#ExecStart=/usr/bin/python3 /opt/ser2net/tests/systemd_probe.py#' \
    -e 's#^Type=simple#Type=oneshot#' -e 's#^Restart=on-failure#Restart=no#' \
    "$UNIT" | sudo tee /etc/systemd/system/ser2net-probe.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl start ser2net-probe || { journal ser2net-probe; fail "the sandbox probe failed"; }
journal ser2net-probe 20
journal ser2net-probe 20 | grep -q "PASS: systemd sandbox probe" || fail "no PASS from the probe"
echo "sandbox: interfaces, directed broadcasts, EPERM for a denied call  OK"

echo "== 4. a mapping on a PTY moves data both ways"
sudo mkdir -p /run/s2n-smoke
sudo socat PTY,link=/run/s2n-smoke/dev,raw,echo=0,user=ser2net,mode=600 \
    PTY,link=/run/s2n-smoke/peer,raw,echo=0 &
socat_pid=$!
trap 'sudo kill "$socat_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do [ -e /run/s2n-smoke/peer ] && break; sleep 0.5; done
[ -e /run/s2n-smoke/dev ] || fail "socat made no PTY"
sudo systemctl stop ser2net
sudo python3 - "$DATA/config.json" <<'EOF'
import json, os, sys
path = sys.argv[1]
with open(path) as fh:
    c = json.load(fh)
def m(mid, name, port, serial):
    return {"id": mid, "name": name, "enabled": True, "kind": "net",
            "serial": {"port": serial, "baudrate": 115200},
            "network": {"mode": "server", "protocol": "raw", "bind_ip": "127.0.0.1", "port": port}}
c["mappings"] = [m("smoke0000001", "pty", 4001, "/run/s2n-smoke/dev"),
                 m("smoke0000002", "low", 502, "/run/s2n-smoke/none")]
with open(path, "w") as fh:
    json.dump(c, fh, indent=2)
st = os.stat(os.path.dirname(path))
os.chown(path, st.st_uid, st.st_gid)
EOF
dropin lownofile 'LimitNOFILE=1024:65536'
restart
sudo python3 - <<'EOF'
import os, select, socket, time
peer = os.open("/run/s2n-smoke/peer", os.O_RDWR | os.O_NOCTTY)
end = time.time() + 15
while True:
    try:
        s = socket.create_connection(("127.0.0.1", 4001), timeout=5)
        break
    except OSError:
        if time.time() > end:
            raise SystemExit("the mapping does not listen on 4001")
        time.sleep(0.2)
s.settimeout(10)
got = b""
while not got.endswith(b"from-the-network\n"):  # again until the device side is open
    if time.time() > end:
        raise SystemExit(f"the device got {got!r}")
    s.sendall(b"from-the-network\n")
    while select.select([peer], [], [], 1)[0]:
        got += os.read(peer, 100)
        if got.endswith(b"\n"):
            break
os.write(peer, b"from-the-device\n")
got = b""
while not got.endswith(b"\n"):
    got += s.recv(100)
assert got == b"from-the-device\n", got
print("network -> device -> network  OK")
EOF
echo "a mapping on a PTY: data both ways  OK"

echo "== 5. the open-file limit is raised at start"
in_log "open files: limit raised from 1024 to 65536" || fail "the soft open-file limit was not raised"
echo "LimitNOFILE=1024:65536: ser2net raised the soft limit (prlimit, under the filter)  OK"

echo "== 6. a mapping on port 502"
wait_log "[low] start failed"
in_log "CAP_NET_BIND_SERVICE" || fail "no hint for the port below 1024"
dropin lowports 'AmbientCapabilities=CAP_NET_BIND_SERVICE' 'CapabilityBoundingSet=CAP_NET_BIND_SERVICE'
restart
for _ in $(seq 1 20); do ss -ltn | grep -q '127.0.0.1:502 ' && break; sleep 0.5; done
ss -ltn | grep -q '127.0.0.1:502 ' || fail "nothing listens on 502 with the capability"
sudo rm "$DROPIN/lowports.conf"
sudo systemctl daemon-reload
echo "port 502: refused with the hint; listens with the README's drop-in  OK"

echo "== 7. an upgrade: lib/ as 2.8 left it"
sudo systemctl stop ser2net
sudo rm "$LIB/.ser2net-deps.json"
sudo -u ser2net sh -c "cd $LIB && mkdir starlette-1.2.1.dist-info userpkg &&
    printf 'Name: starlette\nVersion: 1.2.1\n' > starlette-1.2.1.dist-info/METADATA &&
    echo 'X = 1' > userpkg/__init__.py"
sudo systemctl start ser2net || fail "systemctl start failed"
wait_healthy
assert_running
sudo test ! -e "$LIB/starlette-1.2.1.dist-info" || fail "lib/ was not refreshed"
sudo test -f "$LIB/userpkg/__init__.py" || fail "the package added to lib/ is gone"
record=$(sudo stat -c %Y "$LIB/.ser2net-deps.json") || fail "no install record after the refresh"
sleep 1
restart
[ "$(sudo stat -c %Y "$LIB/.ser2net-deps.json")" = "$record" ] || fail "lib/ was refreshed again"
echo "upgrade: lib/ refreshed once, old metadata gone, the added package kept  OK"

echo "== 8. stop"
clean_stop
echo "stop: clean, no restart  OK"
echo
echo "PASS: systemd unit"
