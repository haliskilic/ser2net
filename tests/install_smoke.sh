#!/usr/bin/env bash
# The Linux install script for real (CI job "install script (binary)"). It needs systemd as
# PID 1 and passwordless sudo, as on GitHub's ubuntu runners, and it installs a user,
# /usr/local/bin/ser2netweb, /var/lib/ser2netweb and a service: run it only on a throwaway machine.
#   tests/install_smoke.sh <ser2netweb-linux-x64 or -arm64>
# With the files as a release download has them (the binary, ser2netweb-standalone.service,
# ser2netweb-install.sh, SHA256SUMS):
# 1. a fresh install with --listen: the user, the unit, /var/lib/ser2netweb (0700, ser2netweb's),
#    active without restarts, /healthz on that address;
# 2. run again (an upgrade): still active, config.json kept;
# 3. a file that doesn't match SHA256SUMS: refused, the running service untouched;
# 4. --uninstall: the service, its unit and the binary gone, the data and the user kept;
# 5. /tmp mounted noexec (hardened hosts): the script has the binary unpack itself in its
#    cache directory, and the service runs.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
BINARY=${1:?usage: install_smoke.sh <binary>}
UNIT=/etc/systemd/system/ser2netweb.service
DATA=/var/lib/ser2netweb
UI=http://127.0.0.1:18092
case $(uname -m) in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; *) echo "unsupported"; exit 1 ;; esac

fail() {
    echo "FAIL: $*"
    sudo systemctl status ser2netweb --no-pager || true
    sudo journalctl -u ser2netweb --no-pager -o cat -n 60 || true
    exit 1
}
prop() { systemctl show -p "$1" --value ser2netweb; }
healthy() { curl -fsS "$UI/healthz" 2>/dev/null | grep -q ok; }

getent passwd ser2netweb >/dev/null && fail "a ser2netweb user exists already: not a throwaway machine?"
[ -e "$UNIT" ] && fail "$UNIT exists already: not a throwaway machine?"

rel=$(mktemp -d)
cp "$BINARY" "$rel/ser2netweb-linux-$arch"
cp "$REPO/systemd/ser2netweb-standalone.service" "$REPO/systemd/ser2netweb-install.sh" "$rel/"
(cd "$rel" && sha256sum ser2netweb-linux-$arch ser2netweb-standalone.service ser2netweb-install.sh > SHA256SUMS)

echo "== 1. a fresh install"
sudo sh "$rel/ser2netweb-install.sh" --listen 127.0.0.1:18092 || fail "the install failed"
getent passwd ser2netweb >/dev/null || fail "no ser2netweb user"
cmp -s "$REPO/systemd/ser2netweb-standalone.service" "$UNIT" || fail "$UNIT is not the release's unit"
[ "$(sudo stat -c '%U %a' "$DATA")" = "ser2netweb 700" ] || fail "$DATA: $(sudo stat -c '%U %a' "$DATA")"
[ "$(prop ActiveState)" = active ] && [ "$(prop NRestarts)" = 0 ] || fail "not running"
[ "$(systemctl is-enabled ser2netweb)" = enabled ] || fail "not enabled"
healthy || fail "no /healthz on $UI"
sudo test -f "$DATA/config.json" || fail "no config.json"
echo "fresh install: user, unit, 0700 data directory, enabled, /healthz on --listen's address  OK"

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

echo "== 4. --uninstall"
sudo sh "$rel/ser2netweb-install.sh" --uninstall || fail "--uninstall failed"
[ ! -e "$UNIT" ] && [ ! -e /usr/local/bin/ser2netweb ] || fail "the unit or the binary is still there"
[ "$(prop ActiveState)" != active ] || fail "still running"
sudo test -f "$DATA/config.json" || fail "the data went too"
getent passwd ser2netweb >/dev/null || fail "the user went too"
echo "uninstall: service, unit and binary gone, data and user kept  OK"

echo "== 5. /tmp mounted noexec"
sudo mount --bind /tmp /tmp
sudo mount -o remount,bind,noexec /tmp
trap 'sudo umount /tmp || true' EXIT
sudo sh "$rel/ser2netweb-install.sh" --listen 127.0.0.1:18092 || fail "the install with a noexec /tmp failed"
grep -q "^Environment=TMPDIR=/var/cache/ser2netweb$" "$UNIT.d/tmpdir.conf" || fail "no tmpdir drop-in"
[ "$(prop ActiveState)" = active ] && [ "$(prop NRestarts)" = 0 ] || fail "not running with a noexec /tmp"
healthy || fail "no /healthz with a noexec /tmp"
sudo systemctl stop ser2netweb
echo "noexec /tmp: the binary unpacks in /var/cache/ser2netweb, the service runs  OK"
echo
echo "PASS: the Linux install script"
