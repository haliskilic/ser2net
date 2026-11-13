#!/bin/sh
# Build-and-install test of the AUR package ser2netweb-bin (.github/workflows/aur.yml), in a
# clean archlinux container, as root, from the repository's root:
#   tests/aur_smoke.sh DIR
# DIR holds what packaging/aur/make_pkgbuild.py wrote for a release (PKGBUILD, .SRCINFO, the
# unit and the account files). As an unprivileged user (makepkg refuses root): .SRCINFO is
# what makepkg --printsrcinfo says, makepkg downloads the sources and checks their sha256,
# builds the package; namcap looks at the PKGBUILD and the package (its errors become
# warnings of the run). Then, as root: pacman -U installs it; --version; the unit (the
# packaged executable, the group uucp, Arch's), sysusers.d / tmpfiles.d, the licences; the
# system account ser2netweb (pacman's systemd hooks made it, home /var/lib/ser2netweb, no
# shell) and /var/lib/ser2netweb (0700, the account's); the service not enabled; no package
# named but ser2netweb (Provides / Conflicts / Replaces); --about says it is a package,
# --help that the data directory is /var/lib/ser2netweb, and --set-password run as the
# account writes config.json there. Removed: the executable and the unit go, the data and
# the account stay.
set -eu

fail() {
    echo "FAIL: $*"
    exit 1
}

dir=${1:?usage: $0 DIR}
[ -f "$dir/PKGBUILD" ] || fail "no $dir/PKGBUILD"
pacman -Syu --noconfirm --needed base-devel namcap zlib >/dev/null
id builder >/dev/null 2>&1 || useradd -m builder
work=/home/builder/ser2netweb-bin
rm -rf "$work"
cp -r "$dir" "$work"
chown -R builder: "$work"
as_builder() { su builder -s /bin/sh -c "cd $work && $*"; }

as_builder makepkg --printsrcinfo > /tmp/srcinfo
diff -u "$work/.SRCINFO" /tmp/srcinfo || fail ".SRCINFO is not what makepkg --printsrcinfo says"
as_builder makepkg --noconfirm --cleanbuild
pkg=$(ls "$work"/ser2netweb-bin-*-x86_64.pkg.tar.zst)
echo "built: $(basename "$pkg")"
for what in "$work/PKGBUILD" "$pkg"; do
    namcap "$what" | tee /tmp/namcap.txt
    sed -n 's/^.* E: /::warning::namcap: /p' /tmp/namcap.txt
done

version=$(sed -n 's/^pkgver=//p' "$work/PKGBUILD")
pacman -U --noconfirm "$pkg" >/dev/null
got=$(ser2netweb --version)
[ "$got" = "ser2netweb $version" ] || fail "--version says $got"
unit=/usr/lib/systemd/system/ser2netweb.service
grep -qx 'ExecStart=/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb' "$unit" ||
    fail "$unit doesn't run /usr/bin/ser2netweb --data-dir /var/lib/ser2netweb"
grep -qx 'User=ser2netweb' "$unit" || fail "$unit doesn't run as ser2netweb"
grep -qx 'SupplementaryGroups=uucp' "$unit" || fail "$unit: not the group uucp"
getent group uucp >/dev/null || fail "no group uucp"
for f in /usr/lib/sysusers.d/ser2netweb.conf /usr/lib/tmpfiles.d/ser2netweb.conf \
    /usr/share/licenses/ser2netweb-bin/LICENSE /usr/share/licenses/ser2netweb-bin/NOTICE \
    /usr/share/licenses/ser2netweb-bin/COMMERCIAL_LICENSE.md \
    /usr/share/licenses/ser2netweb-bin/THIRD-PARTY-NOTICES.md \
    /usr/share/licenses/ser2netweb-bin/THIRD-PARTY-LICENSES.txt; do
    [ -s "$f" ] || fail "missing: $f"
done
cmp /usr/bin/ser2netweb "$work/ser2netweb-linux-x64-$version" ||
    fail "/usr/bin/ser2netweb is not the release's executable (stripped?)"
entry=$(getent passwd ser2netweb) || fail "no account ser2netweb"
uid=$(echo "$entry" | cut -d: -f3)
home=$(echo "$entry" | cut -d: -f6)
shell=$(echo "$entry" | cut -d: -f7)
[ "$home" = /var/lib/ser2netweb ] || fail "the account's home is $home"
case "$shell" in */nologin | */false) ;; *) fail "the account's shell is $shell" ;; esac
[ "$uid" -lt 1000 ] || fail "ser2netweb is not a system account (uid $uid)"
mode=$(stat -c '%a %U %G' /var/lib/ser2netweb)
[ "$mode" = "700 ser2netweb ser2netweb" ] || fail "/var/lib/ser2netweb is $mode"
[ ! -e /etc/systemd/system/multi-user.target.wants/ser2netweb.service ] ||
    fail "the install enabled the service"
others=$(pacman -Qi ser2netweb-bin | sed -n 's/^\(Provides\|Conflicts With\|Replaces\) *: //p' |
    tr ' ' '\n' | grep -v -e '^ser2netweb$' -e '^None$' -e '^$' || true)
[ -z "$others" ] || fail "the package names another: $others"
build=$(ser2netweb --about | grep '^Build: ')
case "$build" in "Build: standalone executable, "*package*) ;; *) fail "--about: $build" ;; esac
COLUMNS=500 ser2netweb --help | grep -qF "here: /var/lib/ser2netweb." ||
    fail "--help names another default data directory"
printf '%s\n' aur-smoke-pass-1 | su -s /bin/sh -c 'ser2netweb --set-password admin' ser2netweb
owner=$(stat -c %U /var/lib/ser2netweb/config.json)
[ "$owner" = ser2netweb ] || fail "config.json belongs to $owner"

pacman -R --noconfirm ser2netweb-bin >/dev/null
[ ! -e /usr/bin/ser2netweb ] && [ ! -e "$unit" ] || fail "the removal left files"
getent passwd ser2netweb >/dev/null || fail "the removal deleted the account"
[ -f /var/lib/ser2netweb/config.json ] || fail "the removal deleted the data"
echo "PASS: $(basename "$pkg")"
