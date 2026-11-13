#!/bin/sh
# Test of the snap (.github/workflows/snap.yml) on the runner itself (snapd, passwordless
# sudo: a throwaway machine only), from the repository's root:
#   tests/snap_smoke.sh SNAP VERSION
# Installed (--dangerous: a local build), the command answers --version; the service is
# neither enabled nor started; raw-usb, hardware-observe and network-observe connect (the
# USB serial ports themselves need a device, which a runner lacks); --about names the
# standalone executable; enabled, the service answers on 127.0.0.1:8080 with its data in
# /var/snap/ser2netweb/common/data, --set-password works on that data while it runs;
# `snap set ser2netweb bind-ip=... port=...` moves the web UI (the configure hook restarts the
# service) and a bad port is refused; disabled and removed in the end.
set -eu

snap=${1:?usage: $0 SNAP VERSION}
version=${2:?usage: $0 SNAP VERSION}
data=/var/snap/ser2netweb/common/data

diagnose() {
    snap services ser2netweb || true
    sudo snap logs -n 60 ser2netweb.daemon || true
}
fail() {
    echo "FAIL: $*"
    diagnose
    exit 1
}
healthy() {  # healthy PORT: /healthz answers within 90 s
    for _ in $(seq 1 90); do
        if curl -fsS "http://127.0.0.1:$1/healthz" 2>/dev/null | grep -q ok; then return 0; fi
        sleep 1
    done
    fail "no /healthz on port $1 after 90 s"
}

sudo snap install --dangerous "$snap"
got=$(/snap/bin/ser2netweb --version)
[ "$got" = "ser2netweb $version" ] || fail "--version says $got"
snap services ser2netweb.daemon | grep -Eq 'ser2netweb\.daemon +disabled +inactive' ||
    fail "installing it enabled or started the service"
for plug in raw-usb hardware-observe network-observe; do
    sudo snap connect "ser2netweb:$plug"
done
snap connections ser2netweb
/snap/bin/ser2netweb --about | grep -q '^Build: standalone executable' ||
    fail "--about: $(/snap/bin/ser2netweb --about | grep Build:)"

sudo snap start --enable ser2netweb.daemon
healthy 8080
sudo test -f "$data/config.json" || fail "no $data/config.json"
printf '%s\n' snap-smoke-pass-1 |
    sudo /snap/bin/ser2netweb --data-dir "$data" --set-password admin
sudo snap set ser2netweb bind-ip=127.0.0.1 port=8091
healthy 8091
if sudo snap set ser2netweb port=99999 2>/dev/null; then fail "port=99999 was taken"; fi
if sudo snap set ser2netweb bind-ip='0.0.0.0;x' 2>/dev/null; then fail "a bad bind-ip was taken"; fi
sudo snap stop --disable ser2netweb.daemon
sudo snap remove --purge ser2netweb
echo "PASS: $(basename "$snap"): installed, connected, the service on 8080 and 8091, removed"
