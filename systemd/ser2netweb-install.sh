#!/bin/sh
# Install (or upgrade) the standalone Linux binary of a release as a systemd service.
# Download ser2netweb-linux-x64 (or -arm64), ser2netweb-standalone.service, this script and
# SHA256SUMS from the release into one folder, then:
#   sudo sh ser2netweb-install.sh                     # 127.0.0.1:8080 until you say otherwise
#   sudo sh ser2netweb-install.sh --listen 0.0.0.0:8080
# It checks the files against SHA256SUMS (when it is there) and that the binary runs here,
# then installs (the same names as the README's manual steps):
#   /usr/local/bin/ser2netweb                   the binary
#   /etc/systemd/system/ser2netweb.service      ser2netweb-standalone.service
#   /var/lib/ser2netweb                         the data directory (0700, user ser2netweb)
# and the system user ser2netweb (its own group; the unit adds dialout for the serial
# ports), enables the service, (re)starts it and waits until it answers. Run again with a
# newer release to upgrade: the data directory and the user stay as they are. It refuses to
# hide another ser2netweb.service (a package's, or a source install in /opt/ser2netweb)
# unless --replace-unit is given.
#
# Up to 2.9.x the binary was called ser2net, and ser2net-install.sh installed it as
# ser2net.service (/usr/local/bin/ser2net, /var/lib/ser2net, the user ser2net). This script
# moves such an install over: it stops and disables that service, moves /var/lib/ser2net to
# /var/lib/ser2netweb for the user ser2netweb, keeps its --listen address and moves your
# drop-ins of it along, and once the new service answers it removes the old unit and the
# old binary. A ser2net.service that its content doesn't show to be that script's (the
# classic ser2net daemon's package, a source install, your own) is left alone, and so is
# the user ser2net unless --remove-old-user is given.
#
# Options:
#   --binary FILE     the binary (default: ser2netweb-linux-<x64|arm64> next to this script)
#   --unit FILE       the unit (default: ser2netweb-standalone.service next to this script)
#   --listen IP:PORT  the web UI's address (a drop-in setting SER2NETWEB_BIND_IP /
#                     SER2NETWEB_PORT; use TLS on a network address, README: Security)
#   --no-start        install and enable, but don't start
#   --replace-unit    install over another ser2netweb.service (stop and disable it first)
#   --remove-old-user also remove 2.9.x's user ser2net, unless something may still use it
#                     (the classic ser2net daemon is installed, or a process runs as it)
#   --uninstall       stop, disable and remove the service and the binary, and a 2.9.x
#                     install of ser2net-install.sh not moved over yet (the data
#                     directories and the users stay)
#   --root DIR        put the files under DIR only (a staged tree: no user is created and
#                     systemd is left alone; the commands to finish are printed)
set -eu

NAME=ser2netweb
BIN_DEST=/usr/local/bin/$NAME
UNIT_DEST=/etc/systemd/system/$NAME.service
DROPIN_DIR=/etc/systemd/system/$NAME.service.d
DATA=/var/lib/$NAME
CACHE=/var/cache/$NAME
USER_NAME=$NAME
# what ser2net-install.sh of 2.9.x installed
OLD=ser2net
OLD_BIN=/usr/local/bin/$OLD
OLD_UNIT=/etc/systemd/system/$OLD.service
OLD_DROPIN_DIR=/etc/systemd/system/$OLD.service.d
OLD_DATA=/var/lib/$OLD
OLD_CACHE=/var/cache/$OLD
OLD_USER=$OLD
# where a package (the classic ser2net daemon's, for ser2net.service) puts its unit
VENDOR_DIRS="/run/systemd/system /usr/local/lib/systemd/system /usr/lib/systemd/system /lib/systemd/system"
HERE=$(cd "$(dirname "$0")" && pwd)

recover=""  # once 2.9.x's service is stopped: how to go back to it if the rest fails
say() { echo "$NAME-install: $*"; }
die() {
    echo "$NAME-install: ERROR: $*" >&2
    if [ -n "$recover" ]; then echo "$NAME-install: $recover" >&2; fi
    exit 1
}
usage() { sed -n '2,/^set -eu$/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit "${1:-0}"; }

binary="" unit="" listen="" start=1 replace=0 uninstall=0 root="" remove_old_user=0
while [ $# -gt 0 ]; do
    case $1 in
        --binary) [ $# -ge 2 ] || die "--binary needs a file"; binary=$2; shift ;;
        --unit) [ $# -ge 2 ] || die "--unit needs a file"; unit=$2; shift ;;
        --listen) [ $# -ge 2 ] || die "--listen needs IP:PORT"; listen=$2; shift ;;
        --no-start) start=0 ;;
        --replace-unit) replace=1 ;;
        --remove-old-user) remove_old_user=1 ;;
        --uninstall) uninstall=1 ;;
        --root) [ $# -ge 2 ] || die "--root needs a folder"; root=${2%/}; shift ;;
        -h|--help) usage 0 ;;
        *) echo "$NAME-install: unknown option: $1" >&2; usage 2 >&2 ;;
    esac
    shift
done

valid_address() {  # $ip and $port make a listen address
    case $port in ''|*[!0-9]*) return 1 ;; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || return 1
    case $ip in ''|*[!0-9A-Fa-f.:]*) return 1 ;; esac
    return 0
}
ip="" port=""
if [ -n "$listen" ]; then
    port=${listen##*:}
    ip=${listen%:*}
    ip=${ip#[}
    ip=${ip%]}
    case $port in ''|*[!0-9]*) die "--listen $listen: give IP:PORT (e.g. 0.0.0.0:8080)" ;; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "--listen $listen: the port must be 1-65535"
    valid_address || die "--listen $listen: give an IP address (e.g. 0.0.0.0, ::)"
fi
listen_given=0
[ -z "$listen" ] || listen_given=1

if [ -n "$root" ]; then
    [ -d "$root" ] || die "--root $root is not a folder"
    root=$(cd "$root" && pwd)
    live=0
else
    [ "$(id -u)" = 0 ] || die "run it as root (sudo sh $0 ...)"
    [ -d /run/systemd/system ] || die "this system doesn't run systemd; start the binary another way (README: Install & run)"
    live=1
fi

# /tmp mounted noexec (hardened hosts): a binary unpacks itself on every start and can't
# load its libraries from there, so the service unpacks into its cache directory instead,
# and this script runs a binary with a scratch folder in /var/cache
noexec_tmp=0
if [ "$live" = 1 ] && awk '$2 == "/tmp" { split($4, o, ","); for (i in o) if (o[i] == "noexec") f = 1 } END { exit !f }' /proc/self/mounts; then
    noexec_tmp=1
fi
scratch=""
cleanup() {
    if [ -n "$scratch" ]; then rm -rf "$scratch"; fi
    rm -f "$root$BIN_DEST.new"
}
trap cleanup EXIT
if [ "$noexec_tmp" = 1 ]; then
    scratch=$(mktemp -d "/var/cache/$NAME-install.XXXXXX")
fi
version_of() {  # FILE: what it says to --version (it fails when it doesn't run)
    if command -v timeout >/dev/null 2>&1; then
        TMPDIR=${scratch:-${TMPDIR:-/tmp}} timeout 60 "$1" --version 2>&1
    else
        TMPDIR=${scratch:-${TMPDIR:-/tmp}} "$1" --version 2>&1
    fi
}

# --- what is installed now ----------------------------------------------------------
ours() {  # the installed ser2netweb.service is this script's (the binary's unit)
    [ -f "$root$UNIT_DEST" ] && grep -q "^ExecStart=$BIN_DEST " "$root$UNIT_DEST"
}
# ser2net.service in /etc/systemd/system is 2.9.x's ser2net-install.sh's only when its content
# says so: that release's binary unit, which runs /usr/local/bin/ser2net on /var/lib/ser2net
# as the user ser2net. The classic ser2net daemon's unit has the same name (and a hand-made
# copy of it may sit there): its name alone says nothing.
old_unit_ours() {
    [ -f "$root$OLD_UNIT" ] \
        && grep -qxF "ExecStart=$OLD_BIN --data-dir $OLD_DATA" "$root$OLD_UNIT" \
        && grep -qxF "User=$OLD_USER" "$root$OLD_UNIT" \
        && grep -qxF "Description=ser2net — serial-to-network bridge with web config UI" "$root$OLD_UNIT"
}
old_binary_ours() {  # /usr/local/bin/ser2net is 2.x's binary: it says "ser2net 2.<x>.<y>"
    [ -f "$root$OLD_BIN" ] && [ -x "$root$OLD_BIN" ] || return 1
    said=$(version_of "$root$OLD_BIN") || return 1
    case $said in "$OLD 2."[0-9]*) return 0 ;; esac  # (the classic daemon: "ser2net version …")
    return 1
}
old_dropin_ours() {  # FILE: a drop-in ser2net-install.sh wrote (its first line says so)
    head -n 1 "$1" 2>/dev/null | grep -q "^# written by $OLD-install\.sh"
}
other_ser2net() {  # another ser2net (the classic daemon, a source install) is there: prints where
    for d in $VENDOR_DIRS; do
        if [ -e "$root$d/$OLD.service" ]; then echo "$d/$OLD.service"; return 0; fi
    done
    if [ -e "$root$OLD_UNIT" ] && ! old_unit_ours; then echo "$OLD_UNIT"; return 0; fi
    for b in /usr/sbin/$OLD /usr/local/sbin/$OLD; do
        if [ -e "$root$b" ]; then echo "$b"; return 0; fi
    done
    return 1
}
old_user_removed=0
drop_old_user() {  # --remove-old-user: 2.9.x's account, when nothing else may use it
    if [ "$live" = 0 ]; then
        say "on the target system, once nothing runs as it any more: userdel $OLD_USER"
        return 0
    fi
    if ! getent passwd "$OLD_USER" >/dev/null; then
        say "there is no user $OLD_USER to remove"
        return 0
    fi
    home=$(getent passwd "$OLD_USER" | cut -d: -f6)
    why=""
    if [ "$home" != "$OLD_DATA" ]; then
        why="its home is $home, not the $OLD_DATA that $OLD-install.sh gave it"
    elif where=$(other_ser2net); then
        why="another ser2net is installed ($where: the classic daemon's, a source install's), which may use that account"
    elif command -v pgrep >/dev/null 2>&1 && pgrep -u "$OLD_USER" >/dev/null 2>&1; then
        why="processes still run as it"
    fi
    if [ -n "$why" ]; then
        say "kept the user $OLD_USER: $why"
        return 0
    fi
    userdel "$OLD_USER"
    if getent group "$OLD_USER" >/dev/null && [ -z "$(getent group "$OLD_USER" | cut -d: -f4)" ]; then
        groupdel "$OLD_USER" || true
    fi
    old_user_removed=1
    say "removed the user $OLD_USER"
}

if [ "$uninstall" = 1 ]; then
    if [ -e "$root$UNIT_DEST" ] && ! ours; then
        die "$UNIT_DEST is not the binary's unit (it doesn't run $BIN_DEST): not removing it"
    fi
    old_install=0 old_bin=0
    if old_unit_ours; then
        old_install=1
        if old_binary_ours; then old_bin=1; fi
    fi
    if [ "$live" = 1 ] && [ -e "$UNIT_DEST" ]; then
        systemctl disable --now "$NAME.service" || true
    fi
    rm -f "$root$UNIT_DEST" "$root$DROPIN_DIR/listen.conf" "$root$DROPIN_DIR/tmpdir.conf" "$root$BIN_DEST"
    rmdir "$root$DROPIN_DIR" 2>/dev/null || true
    removed="$BIN_DEST and $UNIT_DEST"
    kept="$DATA and the user $USER_NAME"
    if [ "$old_install" = 1 ]; then  # 2.9.x's, never moved over: it goes as well
        if [ "$live" = 1 ]; then systemctl disable --now "$OLD.service" || true; fi
        for f in "$root$OLD_DROPIN_DIR/listen.conf" "$root$OLD_DROPIN_DIR/tmpdir.conf"; do
            if [ -f "$f" ] && old_dropin_ours "$f"; then rm -f "$f"; fi
        done
        rmdir "$root$OLD_DROPIN_DIR" 2>/dev/null || true
        rm -f "$root$OLD_UNIT"
        removed="$removed, 2.9.x's $OLD_UNIT"
        if [ "$old_bin" = 1 ]; then
            rm -f "$root$OLD_BIN"
            removed="$removed and $OLD_BIN"
        elif [ -e "$root$OLD_BIN" ]; then
            say "note: kept $OLD_BIN: it doesn't say it is 2.9.x's binary (--version)"
        fi
        kept="$kept, and $OLD_DATA"
    fi
    [ "$live" = 0 ] || systemctl daemon-reload
    if [ "$remove_old_user" = 1 ]; then drop_old_user; fi
    if [ "$old_install" = 1 ] && [ "$old_user_removed" = 0 ]; then kept="$kept and the user $OLD_USER"; fi
    say "removed $removed; kept $kept"
    say "(to remove them too: sudo userdel $USER_NAME && sudo rm -rf $DATA)"
    exit 0
fi

if [ -e "$root$UNIT_DEST" ] && ! ours; then
    if grep -q "/opt/$NAME/$NAME.py" "$root$UNIT_DEST" 2>/dev/null; then
        what="a source install of $NAME (/opt/$NAME), whose data is in /opt/$NAME/data"
    else
        what="another program"
    fi
    [ "$replace" = 1 ] || die "$UNIT_DEST is $what. This would replace it; stop it first (systemctl disable --now $NAME), copy its data to $DATA if you want to keep it, then run this again with --replace-unit"
    say "replacing $UNIT_DEST ($what; a copy is kept as $UNIT_DEST.replaced)"
fi
# (the classic C ser2net daemon's package installs ser2net.service, which no longer clashes)
for d in $VENDOR_DIRS; do
    f=$d/$NAME.service
    [ -e "$root$f" ] || continue
    [ "$replace" = 1 ] || die "$f exists: most likely the $NAME package (.deb / .rpm), which installs the same service. This one would hide it; use the package's (systemctl enable --now $NAME), or stop and disable it first (systemctl disable --now $NAME), then run this again with --replace-unit"
    say "$f is hidden by $UNIT_DEST from now on"
done

# --- the files to install -----------------------------------------------------------
if [ -z "$binary" ]; then
    case $(uname -m) in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) die "there is no standalone binary for $(uname -m); run $NAME from source (README: Install & run) or use the Docker image" ;;
    esac
    binary=$HERE/ser2netweb-linux-$arch
fi
if [ -z "$unit" ]; then
    unit=$HERE/$NAME-standalone.service
fi
[ -f "$binary" ] || die "no binary at $binary (download it from the release next to this script, or name it with --binary)"
[ -f "$unit" ] || die "no unit at $unit (download $NAME-standalone.service from the release next to this script, or name it with --unit)"
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

# the new binary goes next to its place first (a download isn't executable yet), and must
# run here before anything else is changed
owner=""
[ "$(id -u)" != 0 ] || owner="-o root -g root"
mkdir -p "$root$(dirname "$BIN_DEST")" "$root$(dirname "$UNIT_DEST")"
# shellcheck disable=SC2086  # (owner: two options or none)
install -m 0755 $owner "$binary" "$root$BIN_DEST.new"
version=$(version_of "$root$BIN_DEST.new") || die "$binary doesn't run here: $version"
case $version in
    "$NAME "*) say "binary: $version" ;;
    *) die "$binary is not $NAME (--version said: $version)" ;;
esac

# --- 2.9.x's install, to move over (nothing is changed yet) -----------------------------
empty() { [ -z "$(ls -A "$1" 2>/dev/null)" ]; }
migrate=0 old_bin=0 old_cache=0
if old_unit_ours; then
    migrate=1
    if [ -e "$root$OLD_DATA" ] && ! empty "$root$OLD_DATA"; then
        if [ -e "$root$DATA" ] && ! empty "$root$DATA"; then
            die "both $OLD_DATA (the data of 2.9.x's $OLD.service) and $DATA hold files: keep the one this install is to use (move or remove the other), then run this again"
        fi
        if [ "$live" = 1 ] && command -v mountpoint >/dev/null 2>&1 && mountpoint -q "$OLD_DATA"; then
            die "$OLD_DATA is a mount point, which can't be moved to $DATA: mount it at $DATA instead (and change /etc/fstab), then run this again"
        fi
    fi
    if old_binary_ours; then old_bin=1; fi
    old_listen=$root$OLD_DROPIN_DIR/listen.conf
    if [ "$listen_given" = 0 ] && [ -f "$old_listen" ] && old_dropin_ours "$old_listen"; then
        # its address, from the drop-in --listen wrote: Environment=SER2NET_BIND_IP=… SER2NET_PORT=…
        line=$(sed -n '/^Environment=SER2NET_BIND_IP=/{p;q;}' "$old_listen")
        ip=${line#Environment=SER2NET_BIND_IP=}
        ip=${ip%% *}
        port=${line##* SER2NET_PORT=}
        if [ -n "$line" ] && valid_address; then
            case $ip in *:*) listen="[$ip]:$port" ;; *) listen="$ip:$port" ;; esac
        fi
    fi
    if [ -f "$root$OLD_DROPIN_DIR/tmpdir.conf" ] && old_dropin_ours "$root$OLD_DROPIN_DIR/tmpdir.conf"; then
        old_cache=1
    fi
    say "found 2.9.x's install ($OLD_UNIT, $OLD_DATA): moving it over to $NAME"
elif [ -f "$root$OLD_UNIT" ]; then
    if grep -q "/opt/$OLD/$OLD\.py" "$root$OLD_UNIT" 2>/dev/null; then
        say "note: $OLD_UNIT runs a source install in /opt/$OLD (2.9.x or older), which is left as it is; if this binary takes its place, stop it first (systemctl disable --now $OLD): both take 127.0.0.1:8080 unless told otherwise (README: Upgrading from 2.9.x)"
    else
        say "note: $OLD_UNIT is not the one $OLD-install.sh of 2.9.x installed (the classic ser2net daemon's?): left as it is"
    fi
elif [ -f "$root$OLD_DATA/config.json" ] && grep -q '"admin_ui"' "$root$OLD_DATA/config.json" 2>/dev/null; then
    say "note: $OLD_DATA looks like the data of a 2.x install whose service is gone; this install uses $DATA (to use that data: README, Upgrading from 2.9.x)"
fi

# --- the system user ----------------------------------------------------------------
if [ "$live" = 1 ]; then
    getent group dialout >/dev/null || die "this system has no 'dialout' group, which the unit gives the service for the serial ports (SupplementaryGroups=dialout); create it, or give the serial devices' group with a drop-in (systemctl edit $NAME)"
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
    say "the installed unit differs from this release's: the old one is kept as ${keep#"$root"} (put your changes in a drop-in: systemctl edit $NAME)"
fi
# shellcheck disable=SC2086
install -m 0644 $owner "$unit" "$root$UNIT_DEST"
say "installed $UNIT_DEST"

if [ -n "$listen" ] || [ "$noexec_tmp" = 1 ]; then
    mkdir -p "$root$DROPIN_DIR"
fi
if [ -n "$listen" ]; then
    printf '%s\n' "# written by $NAME-install.sh --listen (change it with: systemctl edit $NAME)" \
        "[Service]" "Environment=SER2NETWEB_BIND_IP=$ip SER2NETWEB_PORT=$port" > "$root$DROPIN_DIR/listen.conf"
    chmod 0644 "$root$DROPIN_DIR/listen.conf"
    if [ "$listen_given" = 1 ]; then
        say "the web UI listens on $listen ($DROPIN_DIR/listen.conf)"
    else
        say "the web UI listens on $listen, as 2.9.x's service did ($DROPIN_DIR/listen.conf)"
    fi
fi
if [ "$noexec_tmp" = 1 ]; then
    printf '%s\n' "# written by $NAME-install.sh: /tmp is mounted noexec here, and the binary" \
        "# unpacks itself on every start (into its cache directory instead)" \
        "[Service]" "CacheDirectory=$NAME" "CacheDirectoryMode=0700" \
        "Environment=TMPDIR=$CACHE" > "$root$DROPIN_DIR/tmpdir.conf"
    chmod 0644 "$root$DROPIN_DIR/tmpdir.conf"
    say "/tmp is mounted noexec: the service unpacks into $CACHE ($DROPIN_DIR/tmpdir.conf)"
fi

# --- moving 2.9.x's install over: its service, data and drop-ins --------------------
moved=0
if [ "$migrate" = 1 ]; then
    if [ "$live" = 1 ]; then
        systemctl disable --now "$OLD.service" || true
        recover="2.9.x's $OLD.service is still installed, stopped. To go back to it: sudo systemctl disable --now $NAME; sudo systemctl enable --now $OLD"
        say "stopped and disabled 2.9.x's $OLD.service"
    fi
    if [ -e "$root$OLD_DATA" ]; then
        if [ -d "$root$DATA" ] && empty "$root$DATA"; then rmdir "$root$DATA"; fi
        if [ -e "$root$DATA" ]; then
            rmdir "$root$OLD_DATA" 2>/dev/null || true  # (an empty one; $DATA holds the data)
        else
            mv "$root$OLD_DATA" "$root$DATA"
            chmod 0700 "$root$DATA"
            if [ "$live" = 1 ]; then chown -R "$USER_NAME:$USER_NAME" "$DATA"; fi
            moved=1
            if [ "$live" = 1 ]; then
                recover="${recover%"; sudo systemctl enable --now $OLD"}; sudo mv $DATA $OLD_DATA; sudo chown -R $OLD_USER:$OLD_USER $OLD_DATA; sudo systemctl enable --now $OLD"
                say "moved $OLD_DATA to $DATA (the user $USER_NAME's now)"
            else
                say "moved $OLD_DATA to $DATA"
            fi
        fi
    fi
    if [ -d "$root$OLD_DROPIN_DIR" ]; then
        for f in "$root$OLD_DROPIN_DIR"/*; do
            [ -e "$f" ] || [ -L "$f" ] || continue
            n=$(basename "$f")
            if old_dropin_ours "$f"; then
                # listen.conf: its address is in the new one (or --listen replaced it);
                # tmpdir.conf: written anew above where /tmp is noexec
                if [ "$n" = tmpdir.conf ] || { [ "$n" = listen.conf ] && [ -n "$listen" ]; }; then
                    rm -f "$f"
                    continue
                fi
            fi
            mkdir -p "$root$DROPIN_DIR"
            dest=$DROPIN_DIR/$n
            [ ! -e "$root$dest" ] || dest=$dest.from-$OLD  # (not read by systemd: merge it by hand)
            mv "$f" "$root$dest"
            say "moved the drop-in $OLD_DROPIN_DIR/$n to $dest: check it for 2.9.x's names ($OLD, $OLD_DATA)"
        done
        rmdir "$root$OLD_DROPIN_DIR" 2>/dev/null || true
    fi
fi

if [ "$moved" = 1 ]; then
    :
elif [ -d "$root$DATA" ]; then
    say "data directory $DATA exists: kept as it is"
elif [ "$live" = 1 ]; then
    install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$DATA"
    say "created $DATA"
else
    mkdir -p "$root$DATA"
    chmod 0700 "$root$DATA"
fi

finish_migration() {  # the new service is in place: 2.9.x's unit and binary go
    [ "$migrate" = 1 ] || return 0
    rm -f "$root$OLD_UNIT"
    gone=$OLD_UNIT
    if [ "$old_bin" = 1 ]; then
        rm -f "$root$OLD_BIN"
        gone="$gone and $OLD_BIN"
    elif [ -e "$root$OLD_BIN" ]; then
        say "note: kept $OLD_BIN: it doesn't say it is 2.9.x's binary (--version)"
    fi
    if [ "$old_cache" = 1 ] && [ -d "$root$OLD_CACHE" ]; then
        # what the binary unpacked there (PyInstaller's _MEI* folders), then the folder
        find "$root$OLD_CACHE" -mindepth 1 -maxdepth 1 -name '_MEI*' -exec rm -rf {} + || true
        rmdir "$root$OLD_CACHE" 2>/dev/null || true
    fi
    [ "$live" = 0 ] || systemctl daemon-reload
    recover=""
    say "removed 2.9.x's $gone"
    for d in $VENDOR_DIRS; do
        if [ -e "$root$d/$OLD.service" ]; then
            say "note: $d/$OLD.service (the classic ser2net daemon's) is no longer hidden; it stays disabled"
        fi
    done
    if [ "$remove_old_user" = 1 ]; then
        drop_old_user
    elif [ "$live" = 1 ] && getent passwd "$OLD_USER" >/dev/null; then
        say "the user $OLD_USER stays; once nothing uses it: sudo userdel $OLD_USER (or run this again with --remove-old-user, which checks that first)"
    fi
}

old_user_only() {  # --remove-old-user without a 2.9.x install to move over (any more)
    if [ "$migrate" = 0 ] && [ "$remove_old_user" = 1 ]; then drop_old_user; fi
}

if [ "$live" = 0 ]; then
    finish_migration
    old_user_only
    say "staged in $root. On the target system, finish with:"
    if [ "$migrate" = 1 ]; then
        echo "  systemctl disable --now $OLD    # first: 2.9.x's service"
    fi
    echo "  useradd --system --user-group --home-dir $DATA -M -s /usr/sbin/nologin $USER_NAME"
    if [ "$moved" = 1 ]; then
        echo "  chown -R $USER_NAME:$USER_NAME $DATA"
    else
        echo "  chown $USER_NAME:$USER_NAME $DATA"
    fi
    echo "  systemctl daemon-reload && systemctl enable --now $NAME"
    exit 0
fi

# --- systemd ------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable "$NAME.service"
if [ "$start" = 0 ]; then
    finish_migration
    old_user_only
    say "enabled; (re)start it with: sudo systemctl restart $NAME"
    exit 0
fi
systemctl restart "$NAME.service"
say "started; waiting for it to answer ..."
tmp=/tmp
[ "$noexec_tmp" = 0 ] || tmp=$CACHE
answer=""
for _ in $(seq 1 60); do
    if systemctl is-failed --quiet "$NAME.service"; then
        journalctl -u "$NAME.service" -n 30 --no-pager || true
        die "the service failed to start (above: journalctl -u $NAME)"
    fi
    if answer=$(runuser -u "$USER_NAME" -- env TMPDIR="$tmp" "$BIN_DEST" --data-dir "$DATA" --healthcheck 2>&1); then
        break
    fi
    answer=""
    sleep 1
done
if [ -z "$answer" ]; then
    journalctl -u "$NAME.service" -n 30 --no-pager || true
    die "the service runs but doesn't answer (journalctl -u $NAME)"
fi
say "running: $answer"
finish_migration
old_user_only
say "set the first password in the browser, or: sudo -u $USER_NAME $BIN_DEST --data-dir $DATA --set-password admin"
[ -n "$listen" ] || say "another address (the network, with TLS there): sudo sh $0 --listen 0.0.0.0:8080 (README: Run as a service)"
say "logs: journalctl -u $NAME, and $DATA/all.log"
