#!/bin/sh
# Install (or upgrade) the standalone Linux binary of a release as a systemd service.
# Download ser2net-linux-x64 (or -arm64), ser2net-standalone.service, this script and
# SHA256SUMS from the release into one folder, then:
#   sudo sh ser2net-install.sh                     # 127.0.0.1:8080 until you say otherwise
#   sudo sh ser2net-install.sh --listen 0.0.0.0:8080
# It checks the files against SHA256SUMS (when it is there) and that the binary runs here,
# then installs (the same names as the README's manual steps):
#   /usr/local/bin/ser2net                   the binary
#   /etc/systemd/system/ser2net.service      ser2net-standalone.service
#   /var/lib/ser2net                         the data directory (0700, user ser2net)
# and the system user ser2net (its own group; the unit adds dialout for the serial ports),
# enables the service, (re)starts it and waits until it answers. Run again with a newer
# release to upgrade: the data directory and the user stay as they are. It refuses to hide
# another ser2net.service (the classic ser2net daemon's package, or a source install in
# /opt/ser2net) unless --replace-unit is given.
#
# Options:
#   --binary FILE     the binary (default: ser2net-linux-<x64|arm64> next to this script)
#   --unit FILE       the unit (default: ser2net-standalone.service next to this script)
#   --listen IP:PORT  the web UI's address (a drop-in setting SER2NET_BIND_IP / SER2NET_PORT;
#                     use TLS on a network address, README: Security)
#   --no-start        install and enable, but don't start
#   --replace-unit    install over another ser2net.service (stop and disable it first)
#   --uninstall       stop, disable and remove the service and the binary (the data
#                     directory and the user stay)
#   --root DIR        put the files under DIR only (a staged tree: no user is created and
#                     systemd is left alone; the commands to finish are printed)
set -eu

BIN_DEST=/usr/local/bin/ser2net
UNIT_DEST=/etc/systemd/system/ser2net.service
DROPIN_DIR=/etc/systemd/system/ser2net.service.d
DATA=/var/lib/ser2net
USER_NAME=ser2net
HERE=$(cd "$(dirname "$0")" && pwd)

say() { echo "ser2net-install: $*"; }
die() { echo "ser2net-install: ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -eu$/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit "${1:-0}"; }

binary="" unit="" listen="" start=1 replace=0 uninstall=0 root=""
while [ $# -gt 0 ]; do
    case $1 in
        --binary) [ $# -ge 2 ] || die "--binary needs a file"; binary=$2; shift ;;
        --unit) [ $# -ge 2 ] || die "--unit needs a file"; unit=$2; shift ;;
        --listen) [ $# -ge 2 ] || die "--listen needs IP:PORT"; listen=$2; shift ;;
        --no-start) start=0 ;;
        --replace-unit) replace=1 ;;
        --uninstall) uninstall=1 ;;
        --root) [ $# -ge 2 ] || die "--root needs a folder"; root=${2%/}; shift ;;
        -h|--help) usage 0 ;;
        *) echo "ser2net-install: unknown option: $1" >&2; usage 2 >&2 ;;
    esac
    shift
done

if [ -n "$listen" ]; then
    port=${listen##*:}
    ip=${listen%:*}
    ip=${ip#[}
    ip=${ip%]}
    case $port in ''|*[!0-9]*) die "--listen $listen: give IP:PORT (e.g. 0.0.0.0:8080)" ;; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "--listen $listen: the port must be 1-65535"
    case $ip in ''|*[!0-9A-Fa-f.:]*) die "--listen $listen: give an IP address (e.g. 0.0.0.0, ::)" ;; esac
fi

if [ -n "$root" ]; then
    [ -d "$root" ] || die "--root $root is not a folder"
    root=$(cd "$root" && pwd)
    live=0
else
    [ "$(id -u)" = 0 ] || die "run it as root (sudo sh $0 ...)"
    [ -d /run/systemd/system ] || die "this system doesn't run systemd; start the binary another way (README: Install & run)"
    live=1
fi

# --- what is installed now ----------------------------------------------------------
ours() {  # the installed ser2net.service is this script's (the binary's unit)
    [ -f "$root$UNIT_DEST" ] && grep -q "^ExecStart=$BIN_DEST " "$root$UNIT_DEST"
}

if [ "$uninstall" = 1 ]; then
    if [ -e "$root$UNIT_DEST" ] && ! ours; then
        die "$UNIT_DEST is not the binary's unit (it doesn't run $BIN_DEST): not removing it"
    fi
    if [ "$live" = 1 ] && [ -e "$UNIT_DEST" ]; then
        systemctl disable --now ser2net.service || true
    fi
    rm -f "$root$UNIT_DEST" "$root$DROPIN_DIR/listen.conf" "$root$DROPIN_DIR/tmpdir.conf" "$root$BIN_DEST"
    rmdir "$root$DROPIN_DIR" 2>/dev/null || true
    [ "$live" = 0 ] || systemctl daemon-reload
    say "removed $BIN_DEST and $UNIT_DEST; kept $DATA and the user $USER_NAME"
    say "(to remove them too: sudo userdel $USER_NAME && sudo rm -rf $DATA)"
    exit 0
fi

if [ -e "$root$UNIT_DEST" ] && ! ours; then
    if grep -q "/opt/ser2net/ser2net.py" "$root$UNIT_DEST" 2>/dev/null; then
        what="a source install of ser2net (/opt/ser2net), whose data is in /opt/ser2net/data"
    else
        what="another program"
    fi
    [ "$replace" = 1 ] || die "$UNIT_DEST is $what. This would replace it; stop it first (systemctl disable --now ser2net), copy its data to $DATA if you want to keep it, then run this again with --replace-unit"
    say "replacing $UNIT_DEST ($what; a copy is kept as $UNIT_DEST.replaced)"
fi
for f in /run/systemd/system/ser2net.service /usr/local/lib/systemd/system/ser2net.service \
         /usr/lib/systemd/system/ser2net.service /lib/systemd/system/ser2net.service; do
    [ -e "$root$f" ] || continue
    [ "$replace" = 1 ] || die "$f exists: most likely the classic ser2net daemon's package, which uses the same service name. This one would hide it, so both can't run. Stop and disable it first (systemctl disable --now ser2net), then run this again with --replace-unit"
    say "$f is hidden by $UNIT_DEST from now on"
done

# --- the files to install -----------------------------------------------------------
if [ -z "$binary" ]; then
    case $(uname -m) in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) die "there is no standalone binary for $(uname -m); run ser2net from source (README: Install & run) or use the Docker image" ;;
    esac
    binary=$HERE/ser2net-linux-$arch
fi
if [ -z "$unit" ]; then
    unit=$HERE/ser2net-standalone.service
fi
[ -f "$binary" ] || die "no binary at $binary (download it from the release next to this script, or name it with --binary)"
[ -f "$unit" ] || die "no unit at $unit (download ser2net-standalone.service from the release next to this script, or name it with --unit)"
grep -q "^ExecStart=$BIN_DEST " "$unit" || die "$unit is not the standalone binary's unit (no ExecStart=$BIN_DEST)"

verify() {  # verify FILE: against the SHA256SUMS next to it, when there is one
    sums=$(dirname "$1")/SHA256SUMS
    name=$(basename "$1")
    if [ ! -f "$sums" ]; then
        say "note: no SHA256SUMS next to $name: not verified"
        return 0
    fi
    want=$(awk -v n="$name" '$2 == n || $2 == "*" n { print $1; exit }' "$sums")
    if [ -z "$want" ]; then
        say "note: $name is not listed in $sums: not verified"
        return 0
    fi
    got=$(sha256sum "$1" | cut -d' ' -f1)
    [ "$got" = "$want" ] || die "$1 doesn't match $sums (sha256 $got, listed $want): download it again"
    say "$name: sha256 matches SHA256SUMS"
}
verify "$binary"
verify "$unit"

# /tmp mounted noexec (hardened hosts): the binary unpacks itself on every start and can't
# load its libraries from there, so the service unpacks into its cache directory instead
noexec_tmp=0
if [ "$live" = 1 ] && awk '$2 == "/tmp" { split($4, o, ","); for (i in o) if (o[i] == "noexec") f = 1 } END { exit !f }' /proc/self/mounts; then
    noexec_tmp=1
fi
# the new binary goes next to its place first (a download isn't executable yet), and must
# run here before anything else is changed
owner=""
[ "$(id -u)" != 0 ] || owner="-o root -g root"
scratch=""
cleanup() {
    [ -z "$scratch" ] || rm -rf "$scratch"
    rm -f "$root$BIN_DEST.new"
}
trap cleanup EXIT
mkdir -p "$root$(dirname "$BIN_DEST")" "$root$(dirname "$UNIT_DEST")"
# shellcheck disable=SC2086  # (owner: two options or none)
install -m 0755 $owner "$binary" "$root$BIN_DEST.new"
if [ "$noexec_tmp" = 1 ]; then
    scratch=$(mktemp -d /var/cache/ser2net-install.XXXXXX)
fi
version=$(TMPDIR=${scratch:-${TMPDIR:-/tmp}} "$root$BIN_DEST.new" --version 2>&1) \
    || die "$binary doesn't run here: $version"
case $version in
    "ser2net "*) say "binary: $version" ;;
    *) die "$binary is not ser2net (--version said: $version)" ;;
esac

# --- the system user ----------------------------------------------------------------
if [ "$live" = 1 ]; then
    getent group dialout >/dev/null || die "this system has no 'dialout' group, which the unit gives the service for the serial ports (SupplementaryGroups=dialout); create it, or give the serial devices' group with a drop-in (systemctl edit ser2net)"
    if getent passwd "$USER_NAME" >/dev/null; then
        say "user $USER_NAME exists"
    else
        nologin=$(command -v nologin || echo /usr/sbin/nologin)
        useradd --system --user-group --home-dir "$DATA" -M -s "$nologin" "$USER_NAME"
        say "created the system user $USER_NAME"
    fi
fi

# --- the files ----------------------------------------------------------------------
mv -f "$root$BIN_DEST.new" "$root$BIN_DEST"  # (a running copy keeps its old file)
say "installed $BIN_DEST"

if [ -e "$root$UNIT_DEST" ] && ! ours; then
    cp -p "$root$UNIT_DEST" "$root$UNIT_DEST.replaced"
elif [ -e "$root$UNIT_DEST" ] && ! cmp -s "$unit" "$root$UNIT_DEST"; then
    keep=$root$UNIT_DEST.$(date +%Y%m%d%H%M%S)
    cp -p "$root$UNIT_DEST" "$keep"
    say "the installed unit differs from this release's: the old one is kept as ${keep#"$root"} (put your changes in a drop-in: systemctl edit ser2net)"
fi
# shellcheck disable=SC2086
install -m 0644 $owner "$unit" "$root$UNIT_DEST"
say "installed $UNIT_DEST"

if [ -n "$listen" ] || [ "$noexec_tmp" = 1 ]; then
    mkdir -p "$root$DROPIN_DIR"
fi
if [ -n "$listen" ]; then
    printf '%s\n' "# written by ser2net-install.sh --listen (change it with: systemctl edit ser2net)" \
        "[Service]" "Environment=SER2NET_BIND_IP=$ip SER2NET_PORT=$port" > "$root$DROPIN_DIR/listen.conf"
    chmod 0644 "$root$DROPIN_DIR/listen.conf"
    say "the web UI listens on $listen ($DROPIN_DIR/listen.conf)"
fi
if [ "$noexec_tmp" = 1 ]; then
    printf '%s\n' "# written by ser2net-install.sh: /tmp is mounted noexec here, and the binary" \
        "# unpacks itself on every start (into its cache directory instead)" \
        "[Service]" "CacheDirectory=ser2net" "CacheDirectoryMode=0700" \
        "Environment=TMPDIR=/var/cache/ser2net" > "$root$DROPIN_DIR/tmpdir.conf"
    chmod 0644 "$root$DROPIN_DIR/tmpdir.conf"
    say "/tmp is mounted noexec: the service unpacks into /var/cache/ser2net ($DROPIN_DIR/tmpdir.conf)"
fi

if [ -d "$root$DATA" ]; then
    say "data directory $DATA exists: kept as it is"
elif [ "$live" = 1 ]; then
    install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$DATA"
    say "created $DATA"
else
    mkdir -p "$root$DATA"
    chmod 0700 "$root$DATA"
fi

if [ "$live" = 0 ]; then
    say "staged in $root. On the target system, finish with:"
    echo "  useradd --system --user-group --home-dir $DATA -M -s /usr/sbin/nologin $USER_NAME"
    echo "  chown $USER_NAME:$USER_NAME $DATA"
    echo "  systemctl daemon-reload && systemctl enable --now ser2net"
    exit 0
fi

# --- systemd ------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable ser2net.service
if [ "$start" = 0 ]; then
    say "enabled; (re)start it with: sudo systemctl restart ser2net"
    exit 0
fi
systemctl restart ser2net.service
say "started; waiting for it to answer ..."
tmp=/tmp
[ "$noexec_tmp" = 0 ] || tmp=/var/cache/ser2net
answer=""
for _ in $(seq 1 60); do
    if systemctl is-failed --quiet ser2net.service; then
        journalctl -u ser2net.service -n 30 --no-pager || true
        die "the service failed to start (above: journalctl -u ser2net)"
    fi
    if answer=$(runuser -u "$USER_NAME" -- env TMPDIR="$tmp" "$BIN_DEST" --data-dir "$DATA" --healthcheck 2>&1); then
        break
    fi
    answer=""
    sleep 1
done
if [ -z "$answer" ]; then
    journalctl -u ser2net.service -n 30 --no-pager || true
    die "the service runs but doesn't answer (journalctl -u ser2net)"
fi
say "running: $answer"
say "set the first password in the browser, or: sudo -u $USER_NAME $BIN_DEST --data-dir $DATA --set-password admin"
[ -n "$listen" ] || say "another address (the network, with TLS there): sudo sh $0 --listen 0.0.0.0:8080 (README: Run as a service)"
say "logs: journalctl -u ser2net, and $DATA/all.log"
