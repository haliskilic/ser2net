#!/bin/sh
# Docker entrypoint: run ser2net as an unprivileged user, never as root.
#
# Started as root (the default), it picks the account that owns /data:
#   * /data owned by root — a volume written by an image before v2.8, which ran
#     as root, or a new root-owned folder: hand it to the "ser2net" user
#     (uid/gid 10001) and run as that user;
#   * /data owned by ser2net: run as ser2net (any stray file of another owner is
#     taken over too);
#   * /data owned by someone else (a bind mount of a host user's folder): run as
#     that uid/gid, so the host files keep their owner. Files in it that only an
#     image can have written there — owned by root (an image before v2.8) or by
#     ser2net — are handed to that owner first, or the server couldn't read them.
# Handing files over is best effort: when it fails (a read-only mount, NFS with
# root_squash), the start goes on and ser2net names the file it can't read.
# The supplementary groups given with --group-add (e.g. dialout, for the serial
# devices) are kept; root's own group is not. Started as another user
# (docker run --user ...), it runs as that user unchanged.
set -eu
DATA=/data
run() { exec python /app/ser2net.py --no-bootstrap --data-dir "$DATA" "$@"; }
say() { echo "[ser2net] $*"; }

if [ "$(id -u)" != "0" ]; then
    run "$@"
fi
mkdir -p "$DATA"
uid=$(stat -c %u "$DATA")
gid=$(stat -c %g "$DATA")
if [ "$uid" = "0" ]; then
    say "$DATA belongs to root (written by an image before v2.8, which ran as root, or a" \
        "new root-owned folder): handing it to user ser2net (uid 10001)"
    # in a user namespace (rootless Podman) a host user's own folder shows up as root's
    if [ -r /proc/self/uid_map ] && ! grep -qE '^ *0 +0 +4294967295 *$' /proc/self/uid_map; then
        say "if $DATA is a folder of yours (rootless Podman), start the container with" \
            "--userns=keep-id instead, so it keeps its owner"
    fi
    chown -R ser2net:ser2net "$DATA" || say "warning: could not hand all of $DATA to ser2net"
    uid=10001
    gid=10001
elif [ "$uid" = "10001" ]; then
    if [ -n "$(find "$DATA" ! -uid 10001 -print 2>/dev/null | head -n 1)" ]; then
        chown -R ser2net:ser2net "$DATA" || say "warning: could not hand all of $DATA to ser2net"
    fi
elif [ -n "$(find "$DATA" \( -uid 0 -o -uid 10001 \) -print 2>/dev/null | head -n 1)" ]; then
    say "$DATA belongs to uid $uid: handing the files an image wrote there (owned by root" \
        "or ser2net) to $uid:$gid"
    find "$DATA" \( -uid 0 -o -uid 10001 \) -exec chown -h "$uid:$gid" {} + \
        || say "warning: could not hand every file to $uid:$gid; fix it on the host:" \
               "sudo chown -R $uid:$gid <the folder mounted at $DATA>"
fi
extra=$(id -G | tr ' ' '\n' | grep -v -x -e 0 -e "$gid" | paste -sd, -) || extra=""
if [ -n "$extra" ]; then
    groups="--groups=$extra"
else
    groups="--clear-groups"
fi
exec setpriv --reuid="$uid" --regid="$gid" "$groups" \
    python /app/ser2net.py --no-bootstrap --data-dir "$DATA" "$@"
