# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit acct-user

DESCRIPTION="A user for net-misc/ser2netweb"
ACCT_USER_ID=-1
ACCT_USER_HOME=/var/lib/ser2netweb
ACCT_USER_HOME_PERMS=0700
# dialout and uucp: the serial ports (systemd's udev rules give them to dialout, others
# to uucp)
ACCT_USER_GROUPS=( ser2netweb dialout uucp )

acct-user_add_deps
