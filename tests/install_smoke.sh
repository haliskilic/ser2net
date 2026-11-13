#!/usr/bin/env bash
# The Linux install script for real (CI job "install script (binary)"). It needs systemd as
# PID 1, passwordless sudo and the network (2.9.0's release files), as on GitHub's ubuntu
# runners, and it installs users, /usr/local/bin/ser2net(web), /var/lib/ser2net(web) and
# services: run it only on a throwaway machine.
#   tests/install_smoke.sh <ser2netweb-linux-x64 or -arm64>
# With the files as a release download has them (the binary, ser2netweb-standalone.service,
# ser2netweb-install.sh, SHA256SUMS):
# 0. 2.9.0 installed by its own release files (ser2net-install.sh --listen), with a drop-in of
#    the user's: running as ser2net.service from /var/lib/ser2net;
# 1. ser2netweb-install.sh without options moves it over: ser2net.service, its binary and
#    /var/lib/ser2net gone, /var/lib/ser2netweb (0700, ser2netweb's) with the same
#    config.json, the address and the user's drop-in kept, active without restarts, /healthz
#    on that address; the user ser2net stays;
# 2. run again (an upgrade): still active, config.json kept;
# 3. a file that doesn't match SHA256SUMS: refused, the running service untouched;
# 4. --uninstall --remove-old-user: the service, its unit and the binary gone, the data and
#    the user ser2netweb kept, 2.9.x's user ser2net removed;
# 5. a fresh install with /tmp mounted noexec (hardened hosts): the user and a 0700 data
#    directory made, the binary unpacks itself in its cache directory, and the service runs;
# 6. a ser2net.service that isn't 2.9.x's (a stand-in for the classic ser2net daemon's) runs
#    beside it: an install and --uninstall leave it running and unchanged.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
BINARY=${1:?usage: install_smoke.sh <binary>}
UNIT=/etc/systemd/system/ser2netweb.service
OLD_UNIT=/etc/systemd/system/ser2net.service
DATA=/var/lib/ser2netweb
OLD_DATA=/var/lib/ser2net
UI=http://127.0.0.1:18092
OLD_RELEASE=https://github.com/haliskilic/ser2netweb/releases/download/v2.9.0
case $(uname -m) in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; *) echo "unsupported"; exit 1 ;; esac

fail() {
    echo "FAIL: $*"
    sudo systemctl status ser2netweb ser2net --no-pager || true
    sudo journalctl -u ser2netweb -u ser2net --no-pager -o cat -n 60 || true
    exit 1
}
prop() { systemctl show -p "$1" --value "${2:-ser2netweb}"; }
healthy() { curl -fsS "$UI/healthz" 2>/dev/null | grep -q ok; }
wait_healthy() {
    for _ in $(seq 1 60); do healthy && return 0; sleep 1; done
    fail "no /healthz on $UI after 60 s"
}

for u in ser2netweb ser2net; do
    getent passwd "$u" >/dev/null && fail "a $u user exists already: not a throwaway machine?"
done
for f in "$UNIT" "$OLD_UNIT" "$DATA" "$OLD_DATA"; do
    [ -e "$f" ] && fail "$f exists already: not a throwaway machine?"
done

rel=$(mktemp -d)
cp "$BINARY" "$rel/ser2netweb-linux-$arch"
cp "$REPO/systemd/ser2netweb-standalone.service" "$REPO/systemd/ser2netweb-install.sh" "$rel/"
(cd "$rel" && sha256sum ser2netweb-linux-$arch ser2netweb-standalone.service ser2netweb-install.sh > SHA256SUMS)

echo "== 0. 2.9.0, installed by its own release files"
old=$(mktemp -d)
for f in "ser2net-linux-$arch" ser2net-standalone.service ser2net-install.sh SHA256SUMS; do
    curl -fsSL --retry 3 -o "$old/$f" "$OLD_RELEASE/$f" || fail "could not download 2.9.0's $f"
done
(cd "$old" && sha256sum -c SHA256SUMS --ignore-missing) || fail "2.9.0's files don't match its SHA256SUMS"
sudo sh "$old/ser2net-install.sh" --listen 127.0.0.1:18092 || fail "2.9.0's install failed"
sudo mkdir -p "$OLD_UNIT.d"
printf '[Service]\nEnvironment=TZ=UTC\n' | sudo tee "$OLD_UNIT.d/override.conf" >/dev/null
sudo systemctl daemon-reload && sudo systemctl restart ser2net
wait_healthy
[ "$(prop ActiveState ser2net)" = active ] || fail "2.9.0 is not running"
sudo test -f "$OLD_DATA/config.json" || fail "2.9.0 made no config.json"
config=$(sudo sha256sum "$OLD_DATA/config.json" | cut -d' ' -f1)
echo "2.9.0: ser2net.service from /var/lib/ser2net, /healthz on $UI  OK"

echo "== 1. ser2netweb-install.sh moves it over"
sudo sh "$rel/ser2netweb-install.sh" | tee "$rel/out" || fail "the move failed"
[ ! -e "$OLD_UNIT" ] && [ ! -e /usr/local/bin/ser2net ] && [ ! -e "$OLD_DATA" ] \
    || fail "2.9.0's unit, binary or data directory is still there"
[ ! -e "$OLD_UNIT.d" ] || fail "2.9.0's drop-in folder is still there"
[ "$(systemctl show -p LoadState --value ser2net)" = not-found ] || fail "ser2net.service is still loaded"
[ "$(sudo stat -c '%U %a' "$DATA")" = "ser2netweb 700" ] || fail "$DATA: $(sudo stat -c '%U %a' "$DATA")"
[ "$(sudo sha256sum "$DATA/config.json" | cut -d' ' -f1)" = "$config" ] || fail "config.json changed"
[ -z "$(sudo find "$DATA" ! -user ser2netweb)" ] || fail "files in $DATA not handed to ser2netweb"
grep -q "^Environment=SER2NETWEB_BIND_IP=127.0.0.1 SER2NETWEB_PORT=18092$" "$UNIT.d/listen.conf" \
    || fail "2.9.0's address was not kept"
grep -q "^Environment=TZ=UTC$" "$UNIT.d/override.conf" || fail "the user's drop-in was not moved"
[ "$(prop ActiveState)" = active ] && [ "$(prop NRestarts)" = 0 ] || fail "not running"
[ "$(systemctl is-enabled ser2netweb)" = enabled ] || fail "not enabled"
healthy || fail "no /healthz on $UI, the address kept from 2.9.0"
getent passwd ser2net >/dev/null || fail "the user ser2net went without --remove-old-user"
grep -q "the user ser2net stays" "$rel/out" || fail "no word about the user ser2net"
echo "moved over: 2.9.0's unit, binary and data folder gone, the data, address and drop-in kept  OK"

echo "== 2. run again (an upgrade)"
before=$(sudo stat -c '%i %Y' "$DATA/config.json")
sudo sh "$rel/ser2netweb-install.sh" --listen 127.0.0.1:18092 || fail "the second run failed"
[ "$(prop ActiveState)" = active ] && [ "$(prop NRestarts)" = 0 ] || fail "not running after the upgrade"
healthy || fail "no /healthz after the upgrade"
[ "$(sudo stat -c '%i %Y' "$DATA/config.json")" = "$before" ] || fail "config.json was rewritten"
echo "upgrade: restarted, the data kept  OK"

echo "== 3. a file that doesn't match SHA256SUMS"
pid=$(prop MainPID)
printf '\n' >> "$rel/ser2netweb-standalone.service"
if sudo sh "$rel/ser2netweb-install.sh" 2>"$rel/err"; then fail "a changed unit was installed"; fi
grep -q "doesn't match" "$rel/err" || fail "no checksum message: $(cat "$rel/err")"
[ "$(prop MainPID)" = "$pid" ] || fail "the running service was touched"
cp "$REPO/systemd/ser2netweb-standalone.service" "$rel/"
echo "a changed file: refused, the service left running  OK"

echo "== 4. --uninstall --remove-old-user"
sudo sh "$rel/ser2netweb-install.sh" --uninstall --remove-old-user || fail "--uninstall failed"
[ ! -e "$UNIT" ] && [ ! -e /usr/local/bin/ser2netweb ] || fail "the unit or the binary is still there"
[ "$(prop ActiveState)" != active ] || fail "still running"
sudo test -f "$DATA/config.json" || fail "the data went too"
getent passwd ser2netweb >/dev/null || fail "the user ser2netweb went too"
getent passwd ser2net >/dev/null && fail "2.9.0's user ser2net is still there"
echo "uninstall: service, unit and binary gone, data and user kept, 2.9.0's user removed  OK"

echo "== 5. a fresh install, /tmp mounted noexec"
sudo userdel ser2netweb
sudo rm -rf "$DATA"
sudo mount --bind /tmp /tmp
sudo mount -o remount,bind,noexec /tmp
trap 'sudo umount /tmp || true' EXIT
sudo sh "$rel/ser2netweb-install.sh" --listen 127.0.0.1:18092 || fail "the install with a noexec /tmp failed"
getent passwd ser2netweb >/dev/null || fail "no ser2netweb user"
cmp -s "$REPO/systemd/ser2netweb-standalone.service" "$UNIT" || fail "$UNIT is not the release's unit"
[ "$(sudo stat -c '%U %a' "$DATA")" = "ser2netweb 700" ] || fail "$DATA: $(sudo stat -c '%U %a' "$DATA")"
grep -q "^Environment=TMPDIR=/var/cache/ser2netweb$" "$UNIT.d/tmpdir.conf" || fail "no tmpdir drop-in"
[ "$(prop ActiveState)" = active ] && [ "$(prop NRestarts)" = 0 ] || fail "not running with a noexec /tmp"
healthy || fail "no /healthz with a noexec /tmp"
sudo test -f "$DATA/config.json" || fail "no config.json"
sudo umount /tmp
trap - EXIT
echo "fresh install: user, unit, 0700 data directory; noexec /tmp: unpacks in /var/cache/ser2netweb  OK"

echo "== 6. a ser2net.service that isn't 2.9.x's"
printf '%s\n' "[Unit]" "Description=Serial port to network proxy (a stand-in)" "[Service]" \
    "ExecStart=/bin/sleep infinity" "[Install]" "WantedBy=multi-user.target" \
    | sudo tee "$OLD_UNIT" >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now ser2net
theirs=$(sudo sha256sum "$OLD_UNIT" | cut -d' ' -f1)
pid=$(prop MainPID ser2net)
sudo sh "$rel/ser2netweb-install.sh" --listen 127.0.0.1:18092 | tee "$rel/out" || fail "the install beside it failed"
grep -q "is not the one ser2net-install.sh of 2.9.x installed" "$rel/out" || fail "no note about it"
sudo sh "$rel/ser2netweb-install.sh" --uninstall || fail "--uninstall beside it failed"
[ "$(sudo sha256sum "$OLD_UNIT" | cut -d' ' -f1)" = "$theirs" ] || fail "its unit was changed"
[ "$(prop MainPID ser2net)" = "$pid" ] && [ "$(prop ActiveState ser2net)" = active ] \
    || fail "it was stopped or restarted"
sudo systemctl disable --now ser2net
sudo rm "$OLD_UNIT"
echo "another ser2net.service: left running and unchanged  OK"
echo
echo "PASS: the Linux install script"
