#!/bin/sh
# Docker entrypoint: run ser2netweb as an unprivileged user, never as root.
#
# Started as root (the default), it picks the account that owns /data:
#   * /data owned by root — a volume written by an image before v2.8, which ran
#     as root, or a new root-owned folder: hand it to the "ser2netweb" user
#     (uid/gid 10001) and run as that user;
#   * /data owned by ser2netweb (uid 10001; called ser2net in the images up to 2.9.x, the
#     same uid): run as ser2netweb (any stray file of another owner is taken over too);
#   * /data owned by someone else (a bind mount of a host user's folder): run as
#     that uid/gid, so the host files keep their owner. Files in it that only an
#     image can have written there — owned by root (an image before v2.8) or by
#     ser2netweb — are handed to that owner first, or the server couldn't read them.
# Handing files over is best effort: when it fails (a read-only mount, NFS with
# root_squash), the start goes on and ser2netweb names the file it can't read. Nothing
# below a mount point inside /data (a volume or host folder mounted there) is touched,
# and nothing at all when the mount points can't be read from /proc/self/mountinfo.
# The supplementary groups given with --group-add are kept; root's own group is not.
# The group of each serial device passed in (--device, compose `devices:`) is added
# too: the node keeps the host's numeric group, which need not be the image's
# dialout (20) — Fedora/RHEL use 18 — so --group-add dialout alone can miss it.
# SER2NETWEB_LOW_PORTS=1 (or SER2NET_LOW_PORTS=1, its name up to 2.9.x; the new name wins
# when both are set) keeps the CAP_NET_BIND_SERVICE capability for the server, so it may
# listen on ports below 1024 where Docker doesn't allow that to everyone: the host network
# (in its own network Docker allows them anyway). Only when the container has it to give
# (Docker's default set does; not with --cap-drop ALL or --cap-drop NET_BIND_SERVICE).
# Started as another user (docker run --user ...), it runs as that user unchanged.
set -eu
DATA=/data
MOUNTINFO=/proc/self/mountinfo
SERIAL="/dev/tty[A-Z]* /dev/ttymxc* /dev/rfcomm* /dev/serial/by-id/* /dev/serial/by-path/*"
run() { exec python /app/ser2netweb.py --no-bootstrap --data-dir "$DATA" "$@"; }
say() { echo "[ser2netweb] $*"; }
# in_data <find expression>: find in $DATA, but not below a mount point inside it
# ($mounts, read once below; mountinfo writes " " as \040)
in_data() {
    while IFS= read -r mp; do
        if [ -n "$mp" ]; then
            set -- -path "$(printf '%b' "$mp")" -prune -o "$@"
        fi
    done <<EOF
$mounts
EOF
    find "$DATA" "$@"
}
# take_over <uid:gid> [find tests]: hand the matching entries of $DATA to that owner.
# chown runs through find's -execdir, from the directory holding each entry, so a
# folder swapped for a symbolic link while find walks can't lead it outside $DATA; and
# nothing is changed when the mount points inside $DATA aren't known.
take_over() {
    owner=$1
    shift
    if [ "$mounts_known" != 1 ]; then
        say "warning: $MOUNTINFO can't be read, so folders mounted inside $DATA can't be" \
            "told apart: not changing the owner of anything in $DATA"
        return 1
    fi
    if [ -L "$DATA" ] || [ ! -d "$DATA" ]; then
        say "warning: $DATA is not a plain directory: not changing the owner of anything in it"
        return 1
    fi
    in_data "$@" -execdir chown -h "$owner" {} +
}

if [ "$(id -u)" != "0" ]; then
    run "$@"
fi
mkdir -p "$DATA"
# the mount points inside $DATA (a volume or host folder mounted there): never taken over
if mounts=$(awk -v d="$DATA/" 'index($5, d) == 1 { print $5 }' "$MOUNTINFO" 2>/dev/null); then
    mounts_known=1
else
    mounts=""
    mounts_known=0
fi
uid=$(stat -c %u "$DATA")
gid=$(stat -c %g "$DATA")
if [ "$uid" = "0" ]; then
    say "$DATA belongs to root (written by an image before v2.8, which ran as root, or a" \
        "new root-owned folder): handing it to user ser2netweb (uid 10001)"
    # in a user namespace (rootless Podman) a host user's own folder shows up as root's
    if [ -r /proc/self/uid_map ] && ! grep -qE '^ *0 +0 +4294967295 *$' /proc/self/uid_map; then
        say "if $DATA is a folder of yours, start the container so that it keeps your uid" \
            "(Podman: --userns=keep-id); the folder then keeps its owner"
    fi
    take_over ser2netweb:ser2netweb || say "warning: could not hand all of $DATA to ser2netweb"
    uid=10001
    gid=10001
elif [ "$uid" = "10001" ]; then
    if [ -n "$(in_data ! -uid 10001 -print 2>/dev/null | head -n 1)" ]; then
        take_over ser2netweb:ser2netweb ! -uid 10001 \
            || say "warning: could not hand all of $DATA to ser2netweb"
    fi
elif [ -n "$(in_data \( -uid 0 -o -uid 10001 \) -print 2>/dev/null | head -n 1)" ]; then
    say "$DATA belongs to uid $uid: handing the files an image wrote there (owned by root" \
        "or ser2netweb) to $uid:$gid"
    take_over "$uid:$gid" \( -uid 0 -o -uid 10001 \) \
        || say "warning: could not hand every file to $uid:$gid; fix it on the host:" \
               "sudo chown -R $uid:$gid <the folder mounted at $DATA>"
fi
extra=$(id -G | tr ' ' '\n' | grep -v -x -e 0 -e "$gid" | paste -sd, -) || extra=""
# the serial devices' groups: not root's 0, not tty's 5 (consoles), not 65534 (a group
# unmapped in a user namespace)
for dev in $SERIAL; do
    [ -c "$dev" ] || continue
    g=$(stat -L -c %g "$dev")
    case ",0,5,65534,$gid,$extra," in
        *",$g,"*) ;;
        *) extra="${extra:+$extra,}$g" ;;
    esac
done
if [ -n "$extra" ]; then
    groups="--groups=$extra"
else
    groups="--clear-groups"
fi
for dev in $SERIAL; do
    [ -c "$dev" ] || continue
    # the shell's own test, as the server's account (some test(1) builds skip the groups)
    # shellcheck disable=SC2016
    setpriv --reuid="$uid" --regid="$gid" "$groups" sh -c '[ -r "$1" ] && [ -w "$1" ]' sh "$dev" \
        || say "warning: uid $uid can't open $dev ($(stat -L -c 'owner %u:%g, mode %a' "$dev"));" \
               "give its group read and write access on the host (e.g. a udev rule, MODE=\"0660\")"
done
caps=""
low_ports=SER2NETWEB_LOW_PORTS
[ -n "${SER2NETWEB_LOW_PORTS+set}" ] || low_ports=SER2NET_LOW_PORTS
if [ "${SER2NETWEB_LOW_PORTS-${SER2NET_LOW_PORTS:-}}" = 1 ]; then
    # CAP_NET_BIND_SERVICE is bit 10 of the capability sets
    prm=$(awk '$1 == "CapPrm:" { print $2 }' /proc/self/status 2>/dev/null) || prm=""
    if [ -n "$prm" ] && [ $(( 0x$prm >> 10 & 1 )) = 1 ]; then
        caps="--inh-caps=+net_bind_service --ambient-caps=+net_bind_service"
        say "$low_ports=1: the server keeps CAP_NET_BIND_SERVICE (ports below 1024)"
    else
        say "warning: $low_ports=1, but this container has no CAP_NET_BIND_SERVICE" \
            "to keep (--cap-drop?): ports below 1024 stay closed"
    fi
fi
# shellcheck disable=SC2086  # $caps: two options or none
exec setpriv --reuid="$uid" --regid="$gid" "$groups" $caps \
    python /app/ser2netweb.py --no-bootstrap --data-dir "$DATA" "$@"
