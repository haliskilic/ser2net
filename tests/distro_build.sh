#!/bin/sh
# Builds a distribution recipe of packaging/ from source in that distribution's container,
# lints it with the distribution's linter, installs the package and smoke-tests it (CI:
# .github/workflows/distro-packages.yml; docs/PACKAGING.md, "Recipes for distributions").
# From the repository's root:
#
#   sh tests/distro_build.sh prepare [vX.Y.Z]
#       On the host. build/distro/ser2netweb-<version>.tar.gz: the tag's tarball from GitHub,
#       or without a tag this checkout's HEAD as git archive writes the tag's (the same
#       tree and top folder, ser2netweb-<version>/); the recipes with their checksums for
#       it (and for a tag of another version moved to it) in build/distro/recipes/
#       (tools/bump_recipes.py --out: the checkout's packaging/ stays as it is).
#   docker run --rm -v "$PWD:/src" -w /src IMAGE sh tests/distro_build.sh TARGET
#       As root in a throwaway container of the distribution. TARGET and IMAGE:
#         debian    debian:unstable, debian:trixie, ubuntu:26.04: dpkg-buildpackage of the
#                   tarball repacked as uscan would (mk-origtargz, +ds), lintian (an error
#                   fails), the .deb installed, autopkgtest (debian/tests) on it
#         fedora    fedora:rawhide, fedora:latest: rpmbuild with the dynamic BuildRequires,
#                   rpmlint (an error fails), dnf install
#         epel      almalinux:10 with EPEL and CRB: the same as fedora
#         opensuse  opensuse/tumbleweed: rpmbuild, rpmlint, zypper install
#         arch      archlinux:latest: makepkg as a user, .SRCINFO = makepkg --printsrcinfo,
#                   namcap (an error fails), pacman -U
#         alpine    alpine:edge: abuild -r as a user with a key made for it, apkbuild-lint,
#                   apk add, and the OpenRC service started with rc-service
#       Every build runs the recipe's check step (the tests that bind nothing), every
#       install tests/distro_smoke.py. The packages and the linters' reports go to
#       build/distro/TARGET/.
set -eu

SRC=$(pwd)
DIST=$SRC/build/distro
WORK=/tmp/distro-build

fail() {
    echo "FAIL: $*"
    exit 1
}

say() {
    printf '\n==> %s\n' "$*"
}

prepare() {
    tag=${1:-}
    mkdir -p "$DIST"
    if [ -n "$tag" ]; then
        printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+' ||
            fail "not a release tag: $tag"
        version=${tag#v}
        curl -fsSL --retry 3 -o "$DIST/ser2netweb-$version.tar.gz" \
            "https://github.com/haliskilic/ser2netweb/archive/refs/tags/$tag.tar.gz"
    else
        version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' ser2netweb/__init__.py)
        git archive --format=tar.gz --prefix="ser2netweb-$version/" \
            -o "$DIST/ser2netweb-$version.tar.gz" HEAD
    fi
    rm -rf "$DIST/recipes"
    python3 tools/bump_recipes.py "$version" --tarball "$DIST/ser2netweb-$version.tar.gz" \
        --out "$DIST/recipes"
    printf '%s\n' "$version" >"$DIST/version"
    sha256sum "$DIST/ser2netweb-$version.tar.gz"
}

# recipe NAME DIR: packaging/NAME with the files prepare wrote for the tarball, in DIR
recipe() {
    rm -rf "$2"
    mkdir -p "$2"
    cp -R "packaging/$1/." "$2/"
    if [ -d "$DIST/recipes/packaging/$1" ]; then
        cp -R "$DIST/recipes/packaging/$1/." "$2/"
    fi
}

smoke() {
    say "tests/distro_smoke.py $*"
    python3 -I "$SRC/tests/distro_smoke.py" --version "$VERSION" "$@"
}

build_debian() {
    export DEBIAN_FRONTEND=noninteractive
    say "build tools"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends build-essential ca-certificates devscripts \
        equivs lintian autopkgtest python3 xz-utils >/dev/null
    (. /etc/os-release && echo "$PRETTY_NAME")
    dpkg-query -W -f='${Package} ${Version}\n' debhelper lintian
    useradd --create-home builder
    recipe debian "$WORK/recipe"
    cd "$WORK"
    say "orig tarball, repacked as uscan does (Files-Excluded)"
    mk-origtargz --package ser2netweb --version "$VERSION" --repack --repack-suffix +ds \
        --compression xz --copyright-file "$WORK/recipe/debian/copyright" \
        --directory "$WORK" --copy "$TARBALL"
    upstream="$VERSION+ds"
    tar -xf "ser2netweb_$upstream.orig.tar.xz"
    mv "ser2netweb-$VERSION" "ser2netweb-$upstream"
    tree="$WORK/ser2netweb-$upstream"
    [ ! -e "$tree/vendor/wheels" ] || fail "the repacked tarball still has vendor/wheels"
    cp -R "$WORK/recipe/debian" "$tree/debian"
    python3 -I "$SRC/packaging/debian/missing_sources.py" "$tree"
    cd "$tree"
    say "build dependencies"
    mk-build-deps --install --remove \
        --tool 'apt-get -y -qq --no-install-recommends -o Debug::pkgProblemResolver=yes' \
        debian/control >/dev/null
    chown -R builder "$WORK"
    say "dpkg-buildpackage (as a user)"
    runuser -u builder -- dpkg-buildpackage -us -uc
    say "lintian"
    changes=$(ls "$WORK"/ser2netweb_*.changes)
    code=0
    runuser -u builder -- lintian --profile debian --fail-on error --info --display-info \
        --pedantic "$changes" >"$OUT/lintian.txt" 2>&1 || code=$?
    cat "$OUT/lintian.txt"
    if [ "$code" -ne 0 ] || grep -q '^E: ' "$OUT/lintian.txt"; then
        fail "lintian: exit $code, $(grep -c '^E: ' "$OUT/lintian.txt" || true) errors"
    fi
    cp "$WORK"/ser2netweb_* "$OUT/"
    say "install"
    apt-get install -y -qq "$WORK"/ser2netweb_*_all.deb >/dev/null
    dpkg -s ser2netweb | sed -n 's/^\(Version\|Depends\|Recommends\): /\1: /p'
    smoke
    say "autopkgtest (debian/tests, on this container)"
    autopkgtest --no-built-binaries "$WORK"/ser2netweb_*_all.deb "$tree" -- null
    say "debian/watch"
    runuser -u builder -- uscan --report-status --verbose || echo "(uscan: exit $?)"
}

rpm_tools_dnf() {
    say "build tools"
    dnf -y -q install rpm-build rpmlint python3-devel systemd-rpm-macros util-linux \
        shadow-utils 'dnf-command(builddep)' >/dev/null
}

# rpm_build SPECDIR: rpmbuild of SPECDIR/ser2netweb.spec as the user builder; the dynamic
# BuildRequires (%generate_buildrequires) installed as rpmbuild -br asks for them
rpm_build() {
    top=/home/builder/rpmbuild
    mkdir -p "$top/SOURCES" "$top/SPECS"
    cp "$1"/* "$top/SOURCES/"
    cp "$1/ser2netweb.spec" "$top/SPECS/"
    cp "$TARBALL" "$top/SOURCES/ser2netweb-$VERSION.tar.gz"
    spec="$top/SPECS/ser2netweb.spec"
    # the sources beside the tarball that the SRPM carries (the browser libraries' npm
    # releases)
    for url in $(rpmspec -P "$spec" | sed -n 's|^Source[0-9]*: *\(https://registry\.npmjs\.org/.*\)$|\1|p'); do
        curl -fsSL --retry 3 -o "$top/SOURCES/${url##*/}" "$url"
    done
    chown -R builder "$top"
    say "build dependencies"
    rpmspec -q --buildrequires "$spec" | sed 's/^(\(.*\))$/\1/' >"$WORK/buildrequires"
    set --
    while IFS= read -r dep; do
        set -- "$@" "$dep"
    done <"$WORK/buildrequires"
    printf '  %s\n' "$@"
    pkg_install "$@"
    tries=0
    while :; do
        code=0
        runuser -u builder -- rpmbuild -br --define "_topdir $top" "$spec" >/dev/null 2>&1 || code=$?
        [ "$code" -eq 11 ] || break
        tries=$((tries + 1))
        [ "$tries" -le 5 ] || fail "rpmbuild -br keeps asking for build dependencies"
        pkg_builddep "$top"/SRPMS/ser2netweb-*.buildreqs.nosrc.rpm
    done
    say "rpmbuild (as a user)"
    runuser -u builder -- rpmbuild -ba --define "_topdir $top" "$spec"
    say "rpmlint"
    set -- "$spec" "$top"/SRPMS/ser2netweb-*.src.rpm "$top"/RPMS/noarch/ser2netweb-*.rpm
    # the recipe's rpmlint filters, if it has any (Fedora's name, openSUSE's)
    for rc in "$top/SOURCES/ser2netweb.rpmlintrc" "$top/SOURCES/ser2netweb-rpmlintrc"; do
        if [ -f "$rc" ]; then
            set -- -r "$rc" "$@"
        fi
    done
    code=0
    rpmlint "$@" >"$OUT/rpmlint.txt" 2>&1 || code=$?
    cat "$OUT/rpmlint.txt"
    cp "$top"/SRPMS/ser2netweb-*.src.rpm "$top"/RPMS/noarch/ser2netweb-*.rpm "$OUT/"
    if grep -Eq '^[^ ]+: E: ' "$OUT/rpmlint.txt"; then
        fail "rpmlint reports errors (exit $code)"
    fi
    rpm -qp --requires --recommends "$top"/RPMS/noarch/ser2netweb-*.noarch.rpm
}

build_fedora() {
    rpm_tools_dnf
    (. /etc/os-release && echo "$PRETTY_NAME")
    useradd --create-home builder
    recipe fedora "$WORK/recipe"
    pkg_install() { dnf -y install "$@" | tail -25; }
    pkg_builddep() { dnf -y builddep "$@"; }
    rpm_build "$WORK/recipe"
    say "install"
    dnf -y -q install "$OUT"/ser2netweb-"$VERSION"-*.noarch.rpm
    smoke
}

build_epel() {
    say "EPEL and CRB"
    dnf -y -q install epel-release dnf-plugins-core >/dev/null
    dnf config-manager --set-enabled crb
    build_fedora
}

build_opensuse() {
    say "build tools"
    zypper -n -q refresh
    zypper -n -q install --no-recommends rpm-build rpmlint python-rpm-macros sysuser-tools \
        systemd-rpm-macros fdupes shadow util-linux curl python3 >/dev/null
    (. /etc/os-release && echo "$PRETTY_NAME")
    useradd --create-home builder
    recipe opensuse "$WORK/recipe"
    # the build service appends ser2netweb.changes to the spec as its %changelog
    awk '/^-{60,}$/ { head = 1; next }
         head && NF { who = $0; sub(/^.* - /, "", who)
                      printf "* %s %s %s %s %s\n", $1, $2, $3, $6, who; head = 0; next }
         NF { print }' "$WORK/recipe/ser2netweb.changes" >>"$WORK/recipe/ser2netweb.spec"
    sed -n '/^%changelog/,$p' "$WORK/recipe/ser2netweb.spec"
    # and builds at the newest entry's time (file times and bytecode follow it)
    SOURCE_DATE_EPOCH=$(date -u -d "$(sed -n '2s/ - .*//p' "$WORK/recipe/ser2netweb.changes")" +%s)
    export SOURCE_DATE_EPOCH
    pkg_install() { zypper -n -q install --no-recommends "$@"; }
    pkg_builddep() { fail "no dynamic BuildRequires expected: $*"; }
    rpm_build "$WORK/recipe"
    say "install"
    zypper -n -q install --allow-unsigned-rpm --no-recommends "$OUT"/ser2netweb-"$VERSION"-*.noarch.rpm
    smoke
}

build_arch() {
    say "build tools"
    pacman -Syu --noconfirm --needed --noprogressbar base-devel namcap python >/dev/null
    useradd --create-home builder
    dir=/home/builder/ser2netweb
    recipe arch/ser2netweb "$dir"
    cp "$TARBALL" "$dir/ser2netweb-$VERSION.tar.gz"
    chown -R builder "$dir"
    cd "$dir"
    say "build dependencies"
    # shellcheck disable=SC2046
    pacman -S --noconfirm --needed --asdeps --noprogressbar $(bash -c \
        '. ./PKGBUILD; printf "%s\n" "${depends[@]}" "${makedepends[@]}" "${checkdepends[@]}"')
    say "makepkg (as a user)"
    runuser -u builder -- makepkg --noconfirm
    runuser -u builder -- makepkg --printsrcinfo >"$WORK.srcinfo"
    diff -u .SRCINFO "$WORK.srcinfo" || fail ".SRCINFO is not what makepkg --printsrcinfo says"
    echo ".SRCINFO = makepkg --printsrcinfo"
    say "namcap"
    namcap -i PKGBUILD ser2netweb-"$VERSION"-*.pkg.tar.zst >"$OUT/namcap.txt" 2>&1
    cat "$OUT/namcap.txt"
    if grep -q ' E: ' "$OUT/namcap.txt"; then
        fail "namcap reports errors"
    fi
    cp ser2netweb-"$VERSION"-*.pkg.tar.zst .SRCINFO "$OUT/"
    say "install"
    pacman -U --noconfirm --noprogressbar ser2netweb-"$VERSION"-*.pkg.tar.zst
    smoke --group uucp
}

build_alpine() {
    say "build tools"
    apk add --no-cache -q alpine-sdk atools python3 openrc
    (. /etc/os-release && echo "$PRETTY_NAME")
    apk info -e -v abuild atools
    adduser -D builder
    addgroup builder abuild
    mkdir -p /var/cache/distfiles
    chgrp abuild /var/cache/distfiles
    chmod g+w /var/cache/distfiles
    cp "$TARBALL" "/var/cache/distfiles/ser2netweb-$VERSION.tar.gz"
    su -l builder -c 'abuild-keygen -a -n'
    # (abuild keeps its keys in ~/.config/abuild, older ones in ~/.abuild)
    find /home/builder/.config/abuild /home/builder/.abuild -name '*.rsa.pub' \
        -exec cp {} /etc/apk/keys/ \; 2>/dev/null || true
    ls /etc/apk/keys/builder-*.rsa.pub
    dir=/home/builder/aports/testing/ser2netweb
    recipe alpine/ser2netweb "$dir"
    chown -R builder /home/builder/aports
    cd "$dir"
    say "apkbuild-lint"
    code=0
    apkbuild-lint APKBUILD >"$OUT/apkbuild-lint.txt" 2>&1 || code=$?
    cat "$OUT/apkbuild-lint.txt"
    [ "$code" -eq 0 ] || fail "apkbuild-lint: exit $code"
    say "abuild -r (as a user)"
    su -l builder -c "cd '$dir' && abuild -r"
    find /home/builder -name 'ser2netweb-*.apk' -exec cp {} "$OUT/" \;
    ls -l "$OUT"
    # (the repository abuild made: its REPODEST, <folder>/<arch>/APKINDEX.tar.gz)
    repo=$(dirname "$(dirname "$(find /home/builder -name APKINDEX.tar.gz | head -1)")")
    say "install"
    apk add -q --repository "$repo" ser2netweb ser2netweb-openrc
    apk info -a ser2netweb | sed -n '1,40p'
    smoke --init openrc --service
}

target=${1:-}
case "$target" in
    prepare)
        shift
        prepare "${1:-}"
        exit 0
        ;;
    debian | fedora | epel | opensuse | arch | alpine) ;;
    *)
        fail "usage: sh tests/distro_build.sh prepare [vX.Y.Z] | debian | fedora | epel | opensuse | arch | alpine"
        ;;
esac
[ "$(id -u)" -eq 0 ] || fail "run it as root in a throwaway container (docker run ... IMAGE)"
[ -f "$DIST/version" ] || fail "run 'sh tests/distro_build.sh prepare' first"
VERSION=$(cat "$DIST/version")
TARBALL=$DIST/ser2netweb-$VERSION.tar.gz
OUT=$DIST/$target
rm -rf "$OUT" "$WORK"
mkdir -p "$OUT" "$WORK"
"build_$target"
say "PASS: $target"
