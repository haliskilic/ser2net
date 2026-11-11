#!/usr/bin/env bash
# Smoke test of the Docker image (CI job "docker"; needs Docker):
#   tests/docker_smoke.sh <image>
# 1. a fresh volume: the container gets healthy, runs as uid 10001, keeps a
#    --group-add group but not root's group, and the first-run setup works;
# 2. upgrade: a volume written as root (as by the images before v2.8) holding a
#    v2.7.1 config.json is taken over by the ser2net user and the old admin
#    password still signs in;
# 3. a bind mount owned by another uid keeps its owner (the server runs as it);
# 4. the first admin without the browser: `docker exec ... --set-password` on the running
#    container (run as root, it writes as the /data owner) and SER2NET_ADMIN_PASSWORD_FILE.
set -euo pipefail
IMAGE=${1:?usage: docker_smoke.sh <image>}
HERE=$(cd "$(dirname "$0")" && pwd)
PORT=18080
fail() { echo "FAIL: $*"; docker logs s2n-smoke 2>&1 | tail -40 || true; exit 1; }
cleanup() { docker rm -f s2n-smoke >/dev/null 2>&1 || true; docker volume rm -f s2n-smoke-a s2n-smoke-b >/dev/null 2>&1 || true; }
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
signs_in() {  # signs_in USER PASSWORD: exit 0 when the UI accepts them
    local jar csrf code
    jar=$(mktemp)
    curl -fsS -c "$jar" -b "$jar" -o /dev/null "http://127.0.0.1:$PORT/login"
    csrf=$(awk '$6=="ser2net_csrf"{print $7}' "$jar")
    code=$(curl -sS -c "$jar" -b "$jar" -o /dev/null -w '%{http_code}' \
        --data-urlencode "username=$1" --data-urlencode "password=$2" \
        --data-urlencode "_csrf=$csrf" "http://127.0.0.1:$PORT/login")
    rm -f "$jar"
    [ "$code" = 303 ]
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
out=$(printf 'smoke-pass-1\n' | docker exec -i s2n-smoke python /app/ser2net.py --no-bootstrap \
    --data-dir /data --set-password admin 2>&1) || fail "docker exec --set-password: $out"
grep -q "done by the running ser2net" <<<"$out" || fail "not handed to the running server: $out"
docker exec s2n-smoke sh -c '[ -z "$(find /data ! -uid 10001 | head -n 1)" ]' || fail "files not owned by 10001"
signs_in admin smoke-pass-1 || fail "the admin made with docker exec did not sign in"
docker rm -f s2n-smoke >/dev/null
echo "fresh volume: healthy, uid 10001, keeps --group-add, no root group; first admin via docker exec  OK"

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
jar=$(mktemp)
curl -fsS -c "$jar" -b "$jar" -o /dev/null "http://127.0.0.1:$PORT/login"
csrf=$(awk '$6=="ser2net_csrf"{print $7}' "$jar")
code=$(curl -sS -c "$jar" -b "$jar" -o /dev/null -w '%{http_code}' \
    --data-urlencode "username=admin" --data-urlencode "password=old-admin-pass-1" \
    --data-urlencode "_csrf=$csrf" "http://127.0.0.1:$PORT/login")
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

echo "== 4. first admin from SER2NET_ADMIN_PASSWORD_FILE"
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
signs_in ops env-pass-123 || fail "the admin from SER2NET_ADMIN_PASSWORD_FILE did not sign in"
docker rm -f -v s2n-smoke >/dev/null  # -v: its anonymous /data volume too
rm -rf "$secret_dir"
echo "SER2NET_ADMIN_PASSWORD_FILE: the first admin signs in, the password is not logged  OK"
echo
echo "PASS: Docker image smoke test"
