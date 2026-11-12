#!/usr/bin/env bash
# Smoke test of the Docker image (CI job "docker"; needs Docker):
#   tests/docker_smoke.sh <image>
# 1. a fresh volume: the container gets healthy, runs as uid 10001, keeps a
#    --group-add group but not root's group, and the first-run setup works; a restart
#    rewrites nothing;
# 2. upgrade: a volume written as root (as by the images before v2.8) holding a
#    v2.7.1 config.json is taken over by the ser2net user and the old admin
#    password still signs in;
# 3. a bind mount owned by another uid keeps its owner (the server runs as it);
# 4. a config.json with a setting this version rejects still starts: the setting is
#    logged and kept, and other changes can still be saved from the UI;
# 5. an unusable SER2NET_BIND_IP stops the start with one line, not a traceback;
# 6. a host user's folder that a 2.7 image wrote to as root (bind mount): the files
#    the image left are handed to the folder's owner and the old config signs in;
#    mounted read-only, the start stops with the chown command to run on the host;
# 7. a serial device passed with --device keeps the host's group (here 18, Fedora's
#    dialout, not the image's 20): the server gets that group (not a console's tty
#    group); a node it still can't open is reported;
# 8. the image holds what it runs and the license texts, not the tests, the offline
#    wheels or the docs, and the app is compiled already;
# 9. a read-only /data whose config.json needs updating still starts, with a warning;
# 10. a host folder mounted inside /data (certificates, say) is never taken over, and a new
#    volume with one is private (0700) without a warning;
# 11. the first admin without the browser: `docker exec ... --set-password` on the running
#    container of 1. (run as root, it writes as the /data owner) and SER2NET_ADMIN_PASSWORD_FILE;
# 12. upgrade: a volume a v2.8.0 image left (owned by uid 10001: config.json, backups with a
#    -2 name, audit.log, traces) starts as it is, keeps its owner and the 2.8.0 settings, and
#    the accounts 2.8.0 made sign in.
# 13. ports below 1024 on the host network: the server has no capability (the admin UI on
#    502 can't start, and the log says why) unless SER2NET_LOW_PORTS=1 keeps
#    CAP_NET_BIND_SERVICE for it (then 502 answers); --cap-drop NET_BIND_SERVICE wins.
# An image of another architecture runs emulated (QEMU) when DOCKER_DEFAULT_PLATFORM names it
# (CI: linux/arm/v7), much slower: DOCKER_SMOKE_WAIT (seconds, default 60) is how long a
# container may take to get healthy or to stop.
set -euo pipefail
IMAGE=${1:?usage: docker_smoke.sh <image>}
WAIT=${DOCKER_SMOKE_WAIT:-60}
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=18080
JAR=$(mktemp)
host_dir=""
dev_dir=""
fail() { echo "FAIL: $*"; docker logs s2n-smoke 2>&1 | tail -40 || true; exit 1; }
cleanup() {
    docker rm -f s2n-smoke >/dev/null 2>&1 || true
    docker volume rm -f s2n-smoke-a s2n-smoke-b s2n-smoke-c s2n-smoke-d s2n-smoke-e s2n-smoke-f \
        s2n-smoke-g >/dev/null 2>&1 || true
    for d in "$host_dir" "$dev_dir"; do [ -n "$d" ] && sudo rm -rf "$d"; done
    rm -f "$JAR"
}
trap cleanup EXIT
cleanup

wait_healthy() {
    for _ in $(seq 1 "$WAIT"); do
        s=$(docker inspect -f '{{.State.Health.Status}}' s2n-smoke 2>/dev/null || echo gone)
        [ "$s" = healthy ] && return 0
        [ "$s" = gone ] && fail "container exited"
        sleep 1
    done
    fail "not healthy after $WAIT s (status: $s)"
}
proc_status() { docker exec s2n-smoke sh -c 'grep -E "^(Uid|Gid|Groups):" /proc/1/status'; }
# the CSRF cookie's value in $JAR (on a port other than 8080 its name ends in _<8 hex>)
csrf_of_jar() { awk '$6 ~ /^ser2net_csrf(_[0-9a-f]+)?$/ {print $7}' "$JAR"; }
# sign in through the form (the session stays in $JAR); prints the HTTP status (303 = signed in)
sign_in() {
    local csrf
    : > "$JAR"
    curl -fsS -c "$JAR" -b "$JAR" -o /dev/null "http://127.0.0.1:$PORT/login"
    csrf=$(csrf_of_jar)
    curl -sS -c "$JAR" -b "$JAR" -o /dev/null -w '%{http_code}' \
        --data-urlencode "username=$1" --data-urlencode "password=$2" \
        --data-urlencode "_csrf=$csrf" "http://127.0.0.1:$PORT/login"
}
# an HTMX-style POST in that session; prints the HTTP status
ui_post() {
    curl -sS -c "$JAR" -b "$JAR" -o /dev/null -w '%{http_code}' -X POST \
        -H "X-CSRF-Token: $(csrf_of_jar)" "http://127.0.0.1:$PORT$1"
}

echo "== 1. fresh volume"
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    --group-add 20 -v s2n-smoke-a:/data "$IMAGE" >/dev/null
wait_healthy
st=$(proc_status); echo "$st"
echo "$st" | grep -q -E "^Uid:\s+10001\s" || fail "not running as uid 10001"
echo "$st" | grep -E "^Groups:" | grep -q -w 20 || fail "--group-add 20 lost"
echo "$st" | grep -E "^Groups:" | grep -q -w 0 && fail "root group kept"
curl -fsS "http://127.0.0.1:$PORT/healthz" | grep -q ok || fail "/healthz"
curl -fsS -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" | grep -q 303 || fail "no setup redirect"
docker exec s2n-smoke sh -c 'stat -c "%u %a" /data /data/config.json'
docker exec s2n-smoke sh -c '[ "$(stat -c %u /data/config.json)" = 10001 ]' || fail "config.json owner"
before=$(docker exec s2n-smoke stat -c '%i %Y' /data/config.json)
# (every save keeps what it wrote as the newest backup: the first start's save made one)
backups_before=$(docker exec s2n-smoke sh -c 'ls -A /data/backups 2>/dev/null || true')
docker restart s2n-smoke >/dev/null
wait_healthy
after=$(docker exec s2n-smoke stat -c '%i %Y' /data/config.json)
[ "$before" = "$after" ] || fail "a restart rewrote the unchanged config.json ($before -> $after)"
backups_after=$(docker exec s2n-smoke sh -c 'ls -A /data/backups 2>/dev/null || true')
[ "$backups_before" = "$backups_after" ] \
    || fail "a restart made a backup ($backups_before -> $backups_after)"
out=$(printf 'smoke-pass-1\n' | docker exec -i s2n-smoke python /app/ser2net.py --no-bootstrap \
    --data-dir /data --set-password admin 2>&1) || fail "docker exec --set-password: $out"
grep -q "done by the running ser2net" <<<"$out" || fail "not handed to the running server: $out"
docker exec s2n-smoke sh -c '[ -z "$(find /data ! -uid 10001 | head -n 1)" ]' || fail "files not owned by 10001"
[ "$(sign_in admin smoke-pass-1)" = 303 ] || fail "the admin made with docker exec did not sign in"
docker rm -f s2n-smoke >/dev/null
echo "fresh volume: healthy, uid 10001, keeps --group-add, no root group; a restart leaves config.json alone; first admin via docker exec  OK"

echo "== 2. upgrade from a root-owned v2.7.1 volume"
docker run --rm --user 0 --entrypoint sh -v s2n-smoke-b:/data -v "$HERE/fixtures:/fx:ro" "$IMAGE" -c \
  'cp /fx/config-2.7.1.json /data/config.json && mkdir -p /data/logs && echo old > /data/all.log \
   && chown -R 0:0 /data && chmod 700 /data && chmod 600 /data/config.json'
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -v s2n-smoke-b:/data "$IMAGE" >/dev/null
wait_healthy
# read the whole log first: with pipefail, `docker logs | grep -q` fails when grep exits at the
# match while docker logs is still writing (SIGPIPE), which made this check flaky
logs=$(docker logs s2n-smoke 2>&1)
grep -q "handing it to user ser2net" <<<"$logs" || fail "no takeover message"
docker exec s2n-smoke sh -c 'find /data ! -uid 10001 | head -5'
docker exec s2n-smoke sh -c '[ -z "$(find /data ! -uid 10001 | head -n 1)" ]' || fail "root-owned files left"
code=$(sign_in admin old-admin-pass-1)
[ "$code" = 303 ] || fail "old admin password did not sign in ($code)"
docker exec s2n-smoke sh -c 'ls /data/backups | head -3'
docker exec s2n-smoke sh -c 'ls /data/backups/config-*.json >/dev/null' || fail "no backup of the 2.7.1 config"
docker rm -f s2n-smoke >/dev/null
echo "upgrade: root-owned volume taken over, 2.7.1 config + password work, old config backed up  OK"

echo "== 3. bind mount owned by another uid"
host_dir=$(mktemp -d)
sudo chown 4321:4321 "$host_dir"
docker run -d --name s2n-smoke --health-interval=1s -v "$host_dir:/data" "$IMAGE" >/dev/null
wait_healthy
proc_status | grep -q -E "^Uid:\s+4321\s" || fail "should run as the folder's owner 4321"
[ "$(sudo stat -c %u "$host_dir")" = 4321 ] || fail "host folder owner changed"
docker rm -f s2n-smoke >/dev/null
sudo rm -rf "$host_dir"
host_dir=""
echo "bind mount: runs as the folder's owner, host ownership untouched  OK"

echo "== 4. a config.json with a setting this version rejects"
# the classic case: a mapping (here a disabled one) names TLS files that are not in the container
docker run --rm --user 0 --entrypoint python -v s2n-smoke-c:/data -v "$HERE/fixtures:/fx:ro" "$IMAGE" -c '
import json, os
c = json.load(open("/fx/config-2.7.1.json"))
c["mappings"][1]["network"].update(tls=True, tls_cert="/certs/console.pem", tls_key="/certs/console.key")
json.dump(c, open("/data/config.json", "w"), indent=2)
for p in ("/data", "/data/config.json"):
    os.chown(p, 10001, 10001)'
sum_before=$(docker run --rm -v s2n-smoke-c:/data --entrypoint sha256sum "$IMAGE" /data/config.json)
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -v s2n-smoke-c:/data "$IMAGE" >/dev/null
wait_healthy
logs=$(docker logs s2n-smoke 2>&1)
grep -F "WARNING: config.json: mapping 'Telnet console': TLS file not found: /certs/console.pem" <<<"$logs" \
    || fail "the rejected setting was not logged"
grep -q "Traceback" <<<"$logs" && fail "traceback in the log"
# the upgrade was saved as usual: the old file backed up, the rejected value kept as it was
sums=$(docker exec s2n-smoke sh -c 'sha256sum /data/backups/config-*.json')
grep -q "${sum_before%% *}" <<<"$sums" || fail "the original config.json was not backed up"
docker exec s2n-smoke grep -q '"tls_cert": "/certs/console.pem"' /data/config.json \
    || fail "the rejected setting was not kept"
code=$(sign_in admin old-admin-pass-1)
[ "$code" = 303 ] || fail "could not sign in ($code)"
code=$(ui_post /api/mappings/aaaa00000001/stop)
[ "$code" = 200 ] || fail "stopping a mapping was not saved ($code)"
docker exec s2n-smoke python -c 'import json, sys
m = json.load(open("/data/config.json"))["mappings"][0]
sys.exit(0 if m["id"] == "aaaa00000001" and m["enabled"] is False else 1)' \
    || fail "the stopped mapping is not saved as disabled"
docker rm -f s2n-smoke >/dev/null
echo "rejected setting: logged and kept, healthy, the UI signs in and saves other changes  OK"

echo "== 5. an unusable SER2NET_BIND_IP"
docker run -d --name s2n-smoke -e SER2NET_BIND_IP=not-an-ip "$IMAGE" >/dev/null
code=$(timeout "$WAIT" docker wait s2n-smoke) || fail "did not stop"
logs=$(docker logs s2n-smoke 2>&1)
echo "$logs" | tail -3
[ "$code" = 1 ] || fail "exit code $code"
grep -q "SER2NET_BIND_IP must be an IP address" <<<"$logs" || fail "no clear message"
grep -q "Traceback" <<<"$logs" && fail "traceback instead of a message"
docker rm -f -v s2n-smoke >/dev/null
echo "bad SER2NET_BIND_IP: exit 1 with one line, no traceback  OK"

echo "== 6. bind mount of a host user's folder that a 2.7 image wrote to as root"
host_dir=$(mktemp -d)
sudo cp "$HERE/fixtures/config-2.7.1.json" "$host_dir/config.json"
sudo mkdir -p "$host_dir/logs" "$host_dir/tls"
echo "2026-01-01 00:00:00 old" | sudo tee "$host_dir/all.log" "$host_dir/logs/aaaa00000001.log" >/dev/null
printf '2026-01-01 00:00:00\t127.0.0.1\tlogin\tadmin, local\n' | sudo tee "$host_dir/audit.log" >/dev/null
echo "not a real key" | sudo tee "$host_dir/tls/key.pem" >/dev/null
sudo chmod 600 "$host_dir/config.json" "$host_dir/all.log" "$host_dir/audit.log" "$host_dir/tls/key.pem"
sudo chmod 700 "$host_dir/logs" "$host_dir/tls"
sudo touch "$host_dir/left-by-2.8.0" && sudo chown 10001:10001 "$host_dir/left-by-2.8.0"
sudo chown 4321:4321 "$host_dir"  # the folder is the host user's, the files root's (2.7 ran as root)
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -v "$host_dir:/data:ro" "$IMAGE" >/dev/null
code=$(timeout "$WAIT" docker wait s2n-smoke) || fail "did not stop"
logs=$(docker logs s2n-smoke 2>&1)
echo "$logs" | tail -3
[ "$code" = 1 ] || fail "read-only: exit code $code"
grep -q "cannot read the configuration: /data/config.json: Permission denied" <<<"$logs" \
    || fail "read-only: no clear message"
grep -q "sudo chown -R 4321:4321 /data (in a container: on the host folder mounted there)" <<<"$logs" \
    || fail "read-only: no chown hint"
grep -q "Traceback" <<<"$logs" && fail "read-only: traceback instead of a message"
docker rm -f s2n-smoke >/dev/null
echo "read-only: stops with one line and the chown to run on the host  OK"
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -v "$host_dir:/data" "$IMAGE" >/dev/null
wait_healthy
logs=$(docker logs s2n-smoke 2>&1)
grep -q "handing the files an image wrote there (owned by root or ser2net) to 4321:4321" <<<"$logs" \
    || fail "no takeover message"
proc_status | grep -q -E "^Uid:\s+4321\s" || fail "should run as the folder's owner 4321"
left=$(sudo find "$host_dir" \( -uid 0 -o -uid 10001 \) -print)
[ -z "$left" ] || fail "files still owned by root / ser2net: $left"
[ "$(sudo stat -c %u "$host_dir")" = 4321 ] || fail "host folder owner changed"
code=$(sign_in admin old-admin-pass-1)
[ "$code" = 303 ] || fail "old admin password did not sign in ($code)"
sudo grep -q "AUDIT .* login" "$host_dir/all.log" || fail "all.log not written after the takeover"
[ "$(sudo grep -c login "$host_dir/audit.log")" = 2 ] || fail "audit.log not written after the takeover"
docker rm -f s2n-smoke >/dev/null
sudo rm -rf "$host_dir"
host_dir=""
echo "bind mount upgrade: root / ser2net files handed to the folder's owner, old password, logs and audit work  OK"

echo "== 7. serial device groups"
dev_dir=$(mktemp -d)
# stand-ins for adapters (1:3 is /dev/null): one in group 18, one only root may open
sudo mknod -m 660 "$dev_dir/ttyFAKE0" c 1 3 && sudo chgrp 18 "$dev_dir/ttyFAKE0"
sudo mknod -m 600 "$dev_dir/ttyFAKE1" c 1 3
# an i.MX UART name, and a console-style node in the tty group (5), which must not be added
sudo mknod -m 660 "$dev_dir/ttyFAKE2" c 1 3 && sudo chgrp 4243 "$dev_dir/ttyFAKE2"
sudo mknod -m 620 "$dev_dir/ttyFAKE3" c 1 3 && sudo chgrp 5 "$dev_dir/ttyFAKE3"
docker run -d --name s2n-smoke --health-interval=1s -v s2n-smoke-a:/data \
    --device "$dev_dir/ttyFAKE0:/dev/ttyUSB7" --device "$dev_dir/ttyFAKE1:/dev/ttyACM9" \
    --device "$dev_dir/ttyFAKE2:/dev/ttymxc3" --device "$dev_dir/ttyFAKE3:/dev/ttyS9" "$IMAGE" >/dev/null
wait_healthy
docker exec s2n-smoke stat -c '%n %u:%g %a' /dev/ttyUSB7 /dev/ttyACM9
st=$(proc_status); echo "$st"
echo "$st" | grep -E "^Groups:" | grep -q -w 18 || fail "the device's group 18 was not added"
echo "$st" | grep -E "^Groups:" | grep -q -w 4243 || fail "the ttymxc device's group was not added"
echo "$st" | grep -E "^Groups:" | grep -q -w 0 && fail "root group kept"
echo "$st" | grep -E "^Groups:" | grep -q -w 5 && fail "the tty group was added"
logs=$(docker logs s2n-smoke 2>&1)
grep -q "can't open /dev/ttyACM9" <<<"$logs" || fail "no warning for the node it can't open"
grep -q "can't open /dev/ttyUSB7" <<<"$logs" && fail "false warning for /dev/ttyUSB7"
docker rm -f s2n-smoke >/dev/null
sudo rm -rf "$dev_dir"
dev_dir=""
echo "serial devices: the node's host group is added, an unusable node is reported  OK"

echo "== 8. image contents"
docker run --rm --entrypoint python "$IMAGE" -c '
import glob, os, sys
os.chdir("/app")
need = ["ser2net.py", "bootstrap.py", "requirements.txt", "requirements-optional.txt",
        "requirements-lock.txt",
        "LICENSE", "NOTICE", "COMMERCIAL_LICENSE.md", "THIRD-PARTY-NOTICES.md",
        "tools/docker-entrypoint.sh", "tools/licenses/pyserial.txt", "tools/licenses/npm/xterm.txt",
        "app/locale/tr.json",
        "app/web/templates/base.html", "app/web/static/app.css"]
missing = [f for f in need if not os.path.isfile(f)]
extra = [d for d in ("tests", "vendor", "docs", ".github", "Dockerfile") if os.path.exists(d)]
sources = glob.glob("app/**/*.py", recursive=True)
pycs = glob.glob("app/**/__pycache__/*.pyc", recursive=True)
unhashed = [p for p in pycs if open(p, "rb").read(8)[4] != 3]  # 3 = checked-hash
print(f"{len(sources)} modules, {len(pycs)} compiled; missing {missing}, extra {extra}")
sys.exit(1 if missing or extra or len(pycs) < len(sources) or unhashed else 0)' \
    || fail "image contents"
echo "image: runtime files and license texts in, tests / wheels / docs out, app compiled  OK"

echo "== 9. a read-only /data"
docker run --rm --user 0 --entrypoint sh -v s2n-smoke-d:/data -v "$HERE/fixtures:/fx:ro" "$IMAGE" -c \
  'cp /fx/config-2.7.1.json /data/config.json && chown 10001:10001 /data/config.json'
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -v s2n-smoke-d:/data:ro "$IMAGE" >/dev/null
wait_healthy
logs=$(docker logs s2n-smoke 2>&1)
grep "could not update config.json at start-up" <<<"$logs" || fail "no warning about the failed write"
grep -q "Traceback" <<<"$logs" && fail "traceback in the log"
code=$(sign_in admin old-admin-pass-1)
[ "$code" = 303 ] || fail "could not sign in ($code)"
docker rm -f s2n-smoke >/dev/null
echo "read-only /data: starts and signs in, the failed write is a warning  OK"

echo "== 10. a host folder mounted inside /data"
host_dir=$(mktemp -d)
echo "not a real key" | sudo tee "$host_dir/key.pem" >/dev/null
sudo chown -R 0:0 "$host_dir" && sudo chmod 600 "$host_dir/key.pem"
docker run --rm --user 0 --entrypoint sh -v s2n-smoke-e:/data "$IMAGE" -c 'touch /data/stray'  # root's
docker run -d --name s2n-smoke --health-interval=1s -v s2n-smoke-e:/data \
    -v "$host_dir:/data/certs" "$IMAGE" >/dev/null
wait_healthy
docker exec s2n-smoke sh -c '[ "$(stat -c %u /data/stray)" = 10001 ]' || fail "the stray file was not taken over"
[ "$(sudo stat -c %u "$host_dir")" = 0 ] && [ "$(sudo stat -c %u "$host_dir/key.pem")" = 0 ] \
    || fail "the host folder mounted at /data/certs changed owner"
# ser2net leaves a folder holding other files as it is: the new volume is private already
[ "$(docker exec s2n-smoke stat -c %a /data)" = 700 ] || fail "/data is not 0700"
docker logs s2n-smoke 2>&1 | grep -q "WARNING: /data" && fail "a warning about /data"
docker rm -f s2n-smoke >/dev/null
sudo rm -rf "$host_dir"
host_dir=""
echo "nested mount: the volume taken over, the host folder mounted inside it untouched  OK"

echo "== 11. first admin from SER2NET_ADMIN_PASSWORD_FILE"
secret_dir=$(mktemp -d)
printf 'env-pass-123\n' > "$secret_dir/pw"
chmod 644 "$secret_dir/pw"
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -e SER2NET_ADMIN_USER=ops -e SER2NET_ADMIN_PASSWORD_FILE=/run/secrets/pw \
    -v "$secret_dir/pw:/run/secrets/pw:ro" "$IMAGE" >/dev/null
wait_healthy
logs=$(docker logs s2n-smoke 2>&1)
grep -q "admin account 'ops' created from SER2NET_ADMIN_PASSWORD_FILE" <<<"$logs" || fail "no admin from the env"
grep -q "env-pass-123" <<<"$logs" && fail "the password is in the log"
[ "$(sign_in ops env-pass-123)" = 303 ] || fail "the admin from SER2NET_ADMIN_PASSWORD_FILE did not sign in"
docker rm -f -v s2n-smoke >/dev/null  # -v: its anonymous /data volume too
rm -rf "$secret_dir"
echo "SER2NET_ADMIN_PASSWORD_FILE: the first admin signs in, the password is not logged  OK"

echo "== 12. upgrade from a v2.8.0 volume (owned by uid 10001)"
docker run --rm --user 0 --entrypoint python -v s2n-smoke-f:/data -v "$HERE/fixtures:/fx:ro" "$IMAGE" -c '
import json, os
for d in ("backups", "logs", "traces"):
    os.makedirs("/data/" + d, exist_ok=True)
text = open("/fx/config-2.8.0.json", encoding="utf-8").read()
open("/data/config.json", "w", encoding="utf-8").write(text)
# two saves in one second, before the last two mappings were added
for name, last in (("config-20261104T093000Z.json", -2), ("config-20261104T093000Z-2.json", -1)):
    earlier = json.loads(text)
    earlier["mappings"] = earlier["mappings"][:last]
    open("/data/backups/" + name, "w", encoding="utf-8").write(json.dumps(earlier, indent=2))
open("/data/audit.log", "w").write("2026-11-04 09:30:00\t10.0.0.8\tmapping_save\tPLC-1 raw\top1\n")
open("/data/traces/aaaa00000001-plc1.trace.1", "w").write("net>ser hello\n")
for top, dirs, files in os.walk("/data"):
    for p in [top] + [os.path.join(top, f) for f in files]:
        os.chown(p, 10001, 10001)
        os.chmod(p, 0o700 if os.path.isdir(p) else 0o600)'
sum_before=$(docker run --rm -v s2n-smoke-f:/data --entrypoint sha256sum "$IMAGE" /data/config.json)
docker run -d --name s2n-smoke -p 127.0.0.1:$PORT:8080 --health-interval=1s \
    -v s2n-smoke-f:/data "$IMAGE" >/dev/null
wait_healthy
logs=$(docker logs s2n-smoke 2>&1)
grep -q "Traceback" <<<"$logs" && fail "traceback in the log"
grep -q "handing" <<<"$logs" && fail "a volume owned by the image's user was handed over again"
docker exec s2n-smoke sh -c '[ -z "$(find /data ! -uid 10001 | head -n 1)" ]' || fail "files not owned by 10001"
docker exec s2n-smoke sh -c 'ls /data/backups'
sums=$(docker exec s2n-smoke sh -c 'sha256sum /data/backups/config-*.json')
grep -q "${sum_before%% *}" <<<"$sums" || fail "the 2.8.0 config.json was not kept in backups/"
grep -q "config-20261104T093000Z-2.json" <<<"$sums" || fail "the 2.8.0 backup with a -2 name is gone"
docker exec s2n-smoke python -c 'import json, sys
c = json.load(open("/data/config.json"))
n, gw = c["notifications"], c["mappings"][5]
sys.exit(0 if n["enabled"] and n["smtp_password"] == "dummy-smtp-pass-2.8.0"
         and c["admin_ui"]["language"] == "tr" and gw["mqtt"]["payload_format"] == "sparkplug_b"
         and len(gw["modbus_poll"]["points"]) == 5 else 1)' || fail "2.8.0 settings were lost"
[ "$(sign_in op28 new-in-2.8.0-pass-1)" = 303 ] || fail "the account made by 2.8.0 did not sign in"
[ "$(sign_in admin old-admin-pass-1)" = 303 ] || fail "the admin password did not sign in"
docker rm -f s2n-smoke >/dev/null
echo "v2.8.0 volume: starts as it is, owner kept, 2.8.0 file and its -2 backup kept, settings and accounts work  OK"

echo "== 13. ports below 1024 on the host network (SER2NET_LOW_PORTS)"
low() { docker run -d --name s2n-smoke --network host --health-interval=1s \
            -e SER2NET_BIND_IP=127.0.0.1 -e SER2NET_PORT=502 "$@" -v s2n-smoke-g:/data "$IMAGE" >/dev/null; }
caps() { docker exec s2n-smoke sh -c 'grep -E "^Cap(Eff|Amb):" /proc/1/status' | tr -s '\t ' ' '; }
low -e SER2NET_LOW_PORTS=1
wait_healthy
caps
caps | grep -q "^CapAmb: 0000000000000400$" || fail "CAP_NET_BIND_SERVICE not kept with SER2NET_LOW_PORTS=1"
caps | grep -q "^CapEff: 0000000000000400$" || fail "more than CAP_NET_BIND_SERVICE kept"
curl -fsS "http://127.0.0.1:502/healthz" | grep -q ok || fail "the admin UI on 502 does not answer"
docker rm -f s2n-smoke >/dev/null
low
for _ in $(seq 1 $((WAIT / 2))); do
    logs=$(docker logs s2n-smoke 2>&1 || true)
    grep -q "admin UI port 502 is below 1024" <<<"$logs" && break
    sleep 1
done
grep -q "admin UI port 502 is below 1024" <<<"$logs" || fail "no warning for the admin UI on 502"
grep -q "CAP_NET_BIND_SERVICE" <<<"$logs" || fail "the warning doesn't say what is missing"
docker rm -f s2n-smoke >/dev/null
low -e SER2NET_LOW_PORTS=1 --cap-drop NET_BIND_SERVICE
for _ in $(seq 1 $((WAIT / 2))); do
    logs=$(docker logs s2n-smoke 2>&1 || true)
    grep -q "admin UI port 502 is below 1024" <<<"$logs" && break
    sleep 1
done
grep -q "no CAP_NET_BIND_SERVICE to keep" <<<"$logs" || fail "--cap-drop NET_BIND_SERVICE not reported"
docker rm -f s2n-smoke >/dev/null
echo "ports below 1024: none without SER2NET_LOW_PORTS=1 (the log says why), 502 answers with it, --cap-drop wins  OK"
echo
echo "PASS: Docker image smoke test"
