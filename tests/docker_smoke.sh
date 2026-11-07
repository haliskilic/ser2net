#!/usr/bin/env bash
# Smoke test of the Docker image (CI job "docker"; needs Docker):
#   tests/docker_smoke.sh <image>
# 1. a fresh volume: the container gets healthy, runs as uid 10001, keeps a
#    --group-add group but not root's group, and the first-run setup works;
# 2. upgrade: a volume written as root (as by the images before v2.8) holding a
#    v2.7.1 config.json is taken over by the ser2net user and the old admin
#    password still signs in;
# 3. a bind mount owned by another uid keeps its owner (the server runs as it);
# 4. a config.json with a setting this version rejects still starts: the setting is
#    logged, the file is left as it is, the UI works (a restart rewrites nothing either);
# 5. an unusable SER2NET_BIND_IP stops the start with one line, not a traceback;
# 6. a host user's folder that a 2.7 image wrote to as root (bind mount): the files
#    the image left are handed to the folder's owner and the old config signs in;
#    mounted read-only, the start stops with the chown command to run on the host;
# 7. a serial device passed with --device keeps the host's group (here 18, Fedora's
#    dialout, not the image's 20): the server gets that group; a node it still can't
#    open is reported;
# 8. the first admin without the browser: `docker exec ... --set-password` on the running
#    container of 1. (run as root, it writes as the /data owner) and SER2NET_ADMIN_PASSWORD_FILE.
set -euo pipefail
IMAGE=${1:?usage: docker_smoke.sh <image>}
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=18080
fail() { echo "FAIL: $*"; docker logs s2n-smoke 2>&1 | tail -40 || true; exit 1; }
cleanup() { docker rm -f s2n-smoke >/dev/null 2>&1 || true; docker volume rm -f s2n-smoke-a s2n-smoke-b s2n-smoke-c >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

wait_healthy() {
    for _ in $(seq 1 60); do
        s=$(docker inspect -f '{{.State.Health.Status}}' s2n-smoke 2>/dev/null || echo gone)
        [ "$s" = healthy ] && return 0
        [ "$s" = gone ] && fail "container exited"
        sleep 1
    done
    fail "not healthy after 60 s (status: $s)"
}
proc_status() { docker exec s2n-smoke sh -c 'grep -E "^(Uid|Gid|Groups):" /proc/1/status'; }
# sign in through the form; prints the HTTP status (303 = signed in)
sign_in() {
    local jar csrf
    jar=$(mktemp)
    curl -fsS -c "$jar" -b "$jar" -o /dev/null "http://127.0.0.1:$PORT/login"
    csrf=$(awk '$6=="ser2net_csrf"{print $7}' "$jar")
    curl -sS -c "$jar" -b "$jar" -o /dev/null -w '%{http_code}' \
        --data-urlencode "username=$1" --data-urlencode "password=$2" \
        --data-urlencode "_csrf=$csrf" "http://127.0.0.1:$PORT/login"
    rm -f "$jar"
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
docker restart s2n-smoke >/dev/null
wait_healthy
after=$(docker exec s2n-smoke stat -c '%i %Y' /data/config.json)
[ "$before" = "$after" ] || fail "a restart rewrote the unchanged config.json ($before -> $after)"
docker exec s2n-smoke sh -c '[ -z "$(ls -A /data/backups 2>/dev/null)" ]' || fail "a restart made a backup"
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
grep -q "config.json was not updated at start-up" <<<"$logs" || fail "no 'not updated' note"
grep -q "Traceback" <<<"$logs" && fail "traceback in the log"
[ "$(docker exec s2n-smoke sha256sum /data/config.json)" = "$sum_before" ] || fail "config.json was rewritten"
code=$(sign_in admin old-admin-pass-1)
[ "$code" = 303 ] || fail "could not sign in to fix it ($code)"
docker rm -f s2n-smoke >/dev/null
echo "rejected setting: logged, config.json untouched, healthy, the UI signs in  OK"

echo "== 5. an unusable SER2NET_BIND_IP"
docker run -d --name s2n-smoke -e SER2NET_BIND_IP=not-an-ip "$IMAGE" >/dev/null
code=$(timeout 60 docker wait s2n-smoke) || fail "did not stop"
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
code=$(timeout 60 docker wait s2n-smoke) || fail "did not stop"
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
echo "bind mount upgrade: root / ser2net files handed to the folder's owner, old password, logs and audit work  OK"

echo "== 7. serial device groups"
dev_dir=$(mktemp -d)
# stand-ins for adapters (1:3 is /dev/null): one in group 18, one only root may open
sudo mknod -m 660 "$dev_dir/ttyFAKE0" c 1 3 && sudo chgrp 18 "$dev_dir/ttyFAKE0"
sudo mknod -m 600 "$dev_dir/ttyFAKE1" c 1 3
docker run -d --name s2n-smoke --health-interval=1s -v s2n-smoke-a:/data \
    --device "$dev_dir/ttyFAKE0:/dev/ttyUSB7" --device "$dev_dir/ttyFAKE1:/dev/ttyACM9" "$IMAGE" >/dev/null
wait_healthy
docker exec s2n-smoke stat -c '%n %u:%g %a' /dev/ttyUSB7 /dev/ttyACM9
st=$(proc_status); echo "$st"
echo "$st" | grep -E "^Groups:" | grep -q -w 18 || fail "the device's group 18 was not added"
echo "$st" | grep -E "^Groups:" | grep -q -w 0 && fail "root group kept"
logs=$(docker logs s2n-smoke 2>&1)
grep -q "can't open /dev/ttyACM9" <<<"$logs" || fail "no warning for the node it can't open"
grep -q "can't open /dev/ttyUSB7" <<<"$logs" && fail "false warning for /dev/ttyUSB7"
docker rm -f s2n-smoke >/dev/null
sudo rm -rf "$dev_dir"
echo "serial devices: the node's host group is added, an unusable node is reported  OK"

echo "== 8. first admin from SER2NET_ADMIN_PASSWORD_FILE"
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
echo
echo "PASS: Docker image smoke test"
