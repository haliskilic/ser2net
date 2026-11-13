#!/bin/sh
# The ser2netweb package once it is removed (.deb postrm, .rpm %postun): systemd forgets
# the unit. The data directory /var/lib/ser2netweb and the account ser2netweb stay (also
# on a purge): delete them by hand if you want them gone.
set -e

case "${1:-}" in
    remove | purge | 0) ;;
    *) exit 0 ;;
esac
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi
exit 0
