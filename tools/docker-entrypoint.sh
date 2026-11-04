#!/bin/sh
# Docker entrypoint: run ser2net as an unprivileged user, never as root.
#
# Started as root (the default), it picks the account that owns /data:
#   * /data owned by root — a volume written by an image before v2.8, which ran
#     as root, or a fresh root-owned bind mount: hand it to the "ser2net" user
#     (uid/gid 10001) and run as that user;
#   * /data owned by ser2net: run as ser2net (any stray root-owned file is taken
#     over too);
#   * /data owned by someone else (a bind mount of a host user's folder): run as
#     that uid/gid, so the host files keep their owner.
# The supplementary groups given with --group-add (e.g. dialout, for the serial
# devices) are kept; root's own group is not. Started as another user
# (docker run --user ...), it runs as that user unchanged.
set -eu
DATA=/data
run() { exec python /app/ser2net.py --no-bootstrap --data-dir "$DATA" "$@"; }

if [ "$(id -u)" != "0" ]; then
    run "$@"
fi
mkdir -p "$DATA"
uid=$(stat -c %u "$DATA")
gid=$(stat -c %g "$DATA")
if [ "$uid" = "0" ]; then
    echo "[ser2net] $DATA belongs to root (written by an image before v2.8, which ran as root):" \
         "handing it to user ser2net (uid 10001)"
    chown -R ser2net:ser2net "$DATA"
    uid=10001
    gid=10001
elif [ "$uid" = "10001" ] && [ -n "$(find "$DATA" ! -uid 10001 -print 2>/dev/null | head -n 1)" ]; then
    chown -R ser2net:ser2net "$DATA"
fi
extra=$(id -G | tr ' ' '\n' | grep -v -x -e 0 -e "$gid" | paste -sd, -) || extra=""
if [ -n "$extra" ]; then
    groups="--groups=$extra"
else
    groups="--clear-groups"
fi
exec setpriv --reuid="$uid" --regid="$gid" "$groups" \
    python /app/ser2net.py --no-bootstrap --data-dir "$DATA" "$@"
