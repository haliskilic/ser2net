#!/bin/sh
# The ser2netweb package, once its files are in place (.deb postinst, .rpm %post), on an
# install and on an upgrade: the account and the data directory of its service, then
# systemd reads the unit again. The service is neither enabled nor started here
# (sudo systemctl enable --now ser2netweb); an upgrade restarts it if it was running.
set -e

if command -v systemd-sysusers >/dev/null 2>&1; then
    systemd-sysusers /usr/lib/sysusers.d/ser2netweb.conf
elif ! getent passwd ser2netweb >/dev/null; then
    # (no systemd-sysusers: a container, a chroot, an old systemd) as sysusers.conf says
    getent group ser2netweb >/dev/null || groupadd --system ser2netweb
    useradd --system --gid ser2netweb --home-dir /var/lib/ser2netweb --no-create-home \
        --shell /usr/sbin/nologin --comment "ser2netweb serial-to-network bridge" ser2netweb
fi
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/ser2netweb.conf
else
    # as tmpfiles.conf says
    mkdir -p /var/lib/ser2netweb
    chown ser2netweb:ser2netweb /var/lib/ser2netweb
    chmod 0700 /var/lib/ser2netweb
fi

# a running systemd only (not in a container or chroot)
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
    upgrade=
    case "${1:-}" in
        configure) [ -z "${2:-}" ] || upgrade=1 ;;  # .deb: configure <the version it replaces>
        "" | *[!0-9]*) ;;
        *) [ "$1" -lt 2 ] || upgrade=1 ;;           # .rpm: how many are installed afterwards
    esac
    if [ -n "$upgrade" ]; then
        systemctl try-restart ser2netweb.service || true
    fi
fi
exit 0
