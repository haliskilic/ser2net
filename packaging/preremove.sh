#!/bin/sh
# The ser2netweb package before its files go (.deb prerm, .rpm %preun): a removal stops
# and disables the service (an upgrade leaves it to the new package's postinstall).
set -e

case "${1:-}" in
    remove | 0) ;;  # .deb: remove; .rpm: no copy is left afterwards
    *) exit 0 ;;
esac
command -v systemctl >/dev/null 2>&1 || exit 0
if [ -d /run/systemd/system ]; then
    systemctl --no-reload disable --now ser2netweb.service || true
else
    systemctl --no-reload disable ser2netweb.service >/dev/null 2>&1 || true
fi
exit 0
