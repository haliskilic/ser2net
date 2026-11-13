#!/bin/sh
# Smoke test of the .deb / .rpm packages (CI jobs "package on ..." in standalone.yml) that
# tools/build_packages.py wrote into packages/, run from the repository's root:
#   tests/package_smoke.sh IMAGE ARCH [systemd]
#       In a clean IMAGE container of the runner's ARCH (x64 / arm64; debian:12 and
#       ubuntu:24.04 get the .deb, fedora:latest and almalinux:9 the .rpm), installed with
#       apt / dnf as the README says. With "systemd", systemd is installed first, so the
#       package's scripts use systemd-sysusers / systemd-tmpfiles; without, their useradd /
#       mkdir way. Then: --version; the unit, the sysusers.d / tmpfiles.d entries and the
#       licences in place; the system account ser2netweb (home /var/lib/ser2netweb, no
#       shell) and /var/lib/ser2netweb (0700, the account's); the service not enabled; no
#       package named but its own (Provides / Conflicts / Replaces / Breaks / Obsoletes);
#       --about says it is the package's; --set-password as root without --data-dir
#       writes into /var/lib/ser2netweb as the account. Removed: the executable and the
#       unit are gone, the data and the account stay; installed again over them.
#   tests/package_smoke.sh --host
#       The .deb on the runner itself (systemd as PID 1, passwordless sudo: a throwaway
#       machine only): neither enabled nor started; systemctl enable --now answers on
#       /healthz, running as ser2netweb; --healthcheck as the account without --data-dir;
#       installed again (an upgrade): restarted; removed: stopped and disabled, the data
#       and the account kept.
set -eu

fail() {
    echo "FAIL: $*"
    exit 1
}

# the version as the packages have it inside: 3.0.0-rc.1 -> 3.0.0~rc.1
inner_version() { echo "$1" | sed 's/-/~/'; }

inside() {  # inside PACKAGE VERSION [systemd]: in the container, as root
    pkg=$1 version=$2 with=${3:-}
    case "$pkg" in
        *.deb)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            if [ "$with" = systemd ]; then
                apt-get install -y -qq --no-install-recommends systemd >/dev/null
            fi
            install_it() { apt-get install -y -qq "$pkg" >/dev/null; }
            remove_it() { apt-get remove -y -qq ser2netweb >/dev/null; }
            ;;
        *.rpm)
            if [ "$with" = systemd ]; then dnf install -y -q systemd >/dev/null; fi
            install_it() { dnf install -y -q "$pkg" >/dev/null; }
            remove_it() { dnf remove -y -q ser2netweb >/dev/null; }
            ;;
        *) fail "not a package: $pkg" ;;
    esac
    echo "systemd-sysusers: $(command -v systemd-sysusers || echo none)," \
        "systemd-tmpfiles: $(command -v systemd-tmpfiles || echo none)"
    install_it

    got=$(ser2netweb --version)
    [ "$got" = "ser2netweb $version" ] || fail "--version says $got"
    unit=/usr/lib/systemd/system/ser2netweb.service
    grep -qx 'ExecStart=/usr/bin/ser2netweb --data-dir /var/lib/ser2netweb' "$unit" ||
        fail "$unit doesn't run /usr/bin/ser2netweb --data-dir /var/lib/ser2netweb"
    grep -qx 'User=ser2netweb' "$unit" || fail "$unit doesn't run as ser2netweb"
    for f in /usr/lib/sysusers.d/ser2netweb.conf /usr/lib/tmpfiles.d/ser2netweb.conf \
        /usr/share/doc/ser2netweb/LICENSE /usr/share/doc/ser2netweb/NOTICE \
        /usr/share/doc/ser2netweb/THIRD-PARTY-LICENSES.txt; do
        [ -f "$f" ] || fail "missing: $f"
    done
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
    want=$(inner_version "$version")
    case "$pkg" in
        *.deb)
            [ "$(dpkg-query -W -f='${Version}' ser2netweb)" = "$want" ] || fail "version"
            others=$(dpkg-query -W -f='${Provides}${Conflicts}${Replaces}${Breaks}' ser2netweb)
            ;;
        *.rpm)
            [ "$(rpm -q --qf '%{VERSION}-%{RELEASE}' ser2netweb)" = "$want-1" ] || fail "version"
            others=$( (rpm -q --conflicts ser2netweb
                rpm -q --obsoletes ser2netweb
                rpm -q --provides ser2netweb | grep -v '^ser2netweb\b') | grep -v '^(none)$' || true)
            ;;
    esac
    [ -z "$others" ] || fail "the package names another: $others"
    ser2netweb --about | grep -qxF "Build: standalone executable, .deb / .rpm package" ||
        fail "--about: $(ser2netweb --about | grep Build:)"
    # (as root: the account commands switch to the data directory's owner)
    printf '%s\n' package-smoke-pass-1 | ser2netweb --set-password admin
    owner=$(stat -c %U /var/lib/ser2netweb/config.json)
    [ "$owner" = ser2netweb ] || fail "config.json belongs to $owner"

    remove_it
    [ ! -e /usr/bin/ser2netweb ] && [ ! -e "$unit" ] || fail "the removal left files"
    getent passwd ser2netweb >/dev/null || fail "the removal deleted the account"
    [ -f /var/lib/ser2netweb/config.json ] || fail "the removal deleted the data"
    install_it
    [ "$(getent passwd ser2netweb | cut -d: -f3)" = "$uid" ] || fail "a new account"
    mode=$(stat -c '%a %U %G' /var/lib/ser2netweb)
    [ "$mode" = "700 ser2netweb ser2netweb" ] || fail "installed again: /var/lib/ser2netweb is $mode"
    [ "$(stat -c %U /var/lib/ser2netweb/config.json)" = ser2netweb ] || fail "installed again"
    echo "PASS: $(basename "$pkg") (${with:-no systemd})"
}

host() {  # host PACKAGE VERSION: on the runner (systemd as PID 1)
    pkg=$1 version=$2
    show() { systemctl show -p "$1" --value ser2netweb; }
    diagnose() {
        sudo systemctl status ser2netweb --no-pager || true
        sudo journalctl -u ser2netweb --no-pager -o cat -n 60 || true
    }
    healthy() {
        for _ in $(seq 1 90); do
            curl -fsS http://127.0.0.1:8080/healthz 2>/dev/null | grep -q ok && return 0
            sleep 1
        done
        diagnose
        fail "no /healthz after 90 s"
    }
    sudo apt-get install -y -qq "./$pkg" >/dev/null
    [ "$(ser2netweb --version)" = "ser2netweb $version" ] || fail "--version"
    [ "$(systemctl is-enabled ser2netweb || true)" = disabled ] || fail "the install enabled it"
    [ "$(show ActiveState)" = inactive ] || fail "the install started it"
    sudo systemctl enable --now ser2netweb
    healthy
    pid=$(show MainPID)
    [ "$(stat -c %U "/proc/$pid")" = ser2netweb ] || fail "runs as $(stat -c %U "/proc/$pid")"
    sudo -u ser2netweb ser2netweb --healthcheck || fail "--healthcheck without --data-dir"
    sudo apt-get install -y -qq --reinstall "./$pkg" >/dev/null
    healthy
    [ "$(show MainPID)" != "$pid" ] || fail "an upgrade didn't restart it"
    [ "$(show ActiveState)" = active ] || { diagnose; fail "not active after the upgrade"; }
    sudo apt-get remove -y -qq ser2netweb >/dev/null
    [ "$(show ActiveState)" = inactive ] || fail "the removal left it $(show ActiveState)"
    [ ! -e /etc/systemd/system/multi-user.target.wants/ser2netweb.service ] ||
        fail "the removal left it enabled"
    sudo test -f /var/lib/ser2netweb/config.json || fail "the removal deleted the data"
    getent passwd ser2netweb >/dev/null || fail "the removal deleted the account"
    echo "PASS: $(basename "$pkg") on systemd: enable --now, upgrade, removal"
}

case "${1:-}" in
    --inside)
        shift
        inside "$@"
        ;;
    --host)
        version=$(python3 tools/build_packages.py version)
        host "packages/$(python3 tools/build_packages.py names --arch x64 --format deb)" "$version"
        ;;
    "" | -*)
        echo "usage: $0 IMAGE ARCH [systemd] | --host" >&2
        exit 2
        ;;
    *)
        image=$1 arch=$2 with=${3:-}
        case "$image" in debian* | ubuntu*) fmt=deb ;; *) fmt=rpm ;; esac
        version=$(python3 tools/build_packages.py version)
        name=$(python3 tools/build_packages.py names --arch "$arch" --format "$fmt")
        [ -f "packages/$name" ] || fail "no packages/$name"
        docker run --rm -v "$PWD:/src:ro" "$image" \
            sh /src/tests/package_smoke.sh --inside "/src/packages/$name" "$version" "$with"
        ;;
esac
