#!/bin/sh
# Smoke test of the package repositories tools/package_repo.py built (CI jobs "repository on
# ..." and "published repository on ..." in package-repo.yml), run from the repository's root:
#   tests/package_repo_smoke.sh SITE IMAGE VERSION
#       SITE (the built tree) served on 127.0.0.1 (python3 -m http.server) twice: as built,
#       its ser2netweb.sources / ser2netweb.repo pointing there instead of at GitHub Pages,
#       and a copy whose InRelease names other Packages files and whose repomd.xml files have
#       another revision, all after signing. In a clean IMAGE container (--network host;
#       debian*/ubuntu*: apt, opensuse*: zypper, else dnf), as root:
#         - the commands docs/PACKAGES.md gives (tools/package_repo.py commands, without
#           sudo, without questions) against the changed copy fail on its signature, and
#           ser2netweb is not installed;
#         - against the tree as built they install ser2netweb with no signature warning,
#           `ser2netweb --version` says VERSION, it comes from this repository (dnf prints
#           the key it imports; dnf and zypper check the package's signature too,
#           gpgcheck=1 and pkg_gpgcheck=1, as zypper's repository list says, and rpm names
#           the key that made it), the update commands run, and
#           the remove commands remove the package and the repository, after which the
#           package manager works as before.
#   tests/package_repo_smoke.sh --live SITE IMAGE VERSION
#       Once https://haliskilic.github.io/ser2netweb serves SITE's InRelease and repomd.xml
#       (GitHub Pages' cache may take some minutes), the same commands against it, as they
#       are (no changed copy).
set -eu

SITE_URL=https://haliskilic.github.io/ser2netweb

fail() {
    echo "FAIL: $*"
    exit 1
}

family_of() {
    case "$1" in
        debian* | ubuntu*) echo apt ;;
        opensuse*) echo zypper ;;
        *) echo dnf ;;
    esac
}

# --- in the container, as root ------------------------------------------------------------

prepare() {  # prepare FAMILY: curl (and the CA certificates) from the distribution itself
    case "$1" in
        apt)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y -qq --no-install-recommends curl ca-certificates >/dev/null
            ;;
        dnf) command -v curl >/dev/null || dnf install -y -q curl >/dev/null ;;
        zypper) command -v curl >/dev/null || zypper --non-interactive -q install curl >/dev/null ;;
    esac
}

run() {  # run NAME: the commands in /work/NAME.sh, the output into /tmp/NAME.log
    echo "--- $1:"
    sed 's/^/    /' "/work/$1.sh"
    sh -eux "/work/$1.sh" >"/tmp/$1.log" 2>&1
}

forget() {  # forget FAMILY: the repository the changed copy's commands added
    case "$1" in
        apt) rm -f /etc/apt/sources.list.d/ser2netweb.sources /usr/share/keyrings/ser2netweb-archive-keyring.gpg ;;
        dnf) rm -f /etc/yum.repos.d/ser2netweb.repo ;;
        zypper) zypper --non-interactive removerepo ser2netweb >/dev/null ;;
    esac
}

inside() {  # inside FAMILY VERSION BASE FINGERPRINT [live]
    family=$1 version=$2 base=$3 fingerprint=$4 live=${5:-}
    prepare "$family"
    signature='BADSIG|NO_PUBKEY|not signed|insecure|signature verification|bad (gpg|pgp) signature|signature.*(fail|invalid)'
    if [ -z "$live" ]; then
        if run install-bad; then
            cat /tmp/install-bad.log
            fail "the repository with a changed InRelease / repomd.xml was accepted"
        fi
        grep -Eiq "$signature" /tmp/install-bad.log || {
            cat /tmp/install-bad.log
            fail "the changed repository failed, but not on its signature"
        }
        ! command -v ser2netweb >/dev/null || fail "ser2netweb was installed from the changed repository"
        echo "OK: refused: $(grep -Ei -m1 "$signature" /tmp/install-bad.log)"
        forget "$family"
    fi

    run install || { cat /tmp/install.log; fail "the documented install failed"; }
    if grep -Ei "$signature" /tmp/install.log; then
        cat /tmp/install.log
        fail "a signature warning"
    fi
    got=$(ser2netweb --version)
    [ "$got" = "ser2netweb $version" ] || fail "--version says $got"
    case "$family" in
        apt)
            apt-cache policy ser2netweb | tee /tmp/policy.txt
            grep -q " $base/deb stable/main " /tmp/policy.txt || fail "not from $base/deb"
            ;;
        dnf)
            dnf info --installed ser2netweb | tee /tmp/info.txt
            grep -Eq '^From repo[a-z]* *: ser2netweb$' /tmp/info.txt || fail "not from the repository ser2netweb"
            # dnf imports the key (gpgkey=) on the first use and names its fingerprint
            if tr -d ' ' </tmp/install.log | grep -qi "$fingerprint"; then
                echo "OK: dnf imported the key $fingerprint"
            else
                echo "NOTE: dnf's output doesn't name the key $fingerprint:"
                grep -Ei 'key|fingerprint' /tmp/install.log || true
            fi
            ;;
        zypper)
            zypper --non-interactive --no-refresh info ser2netweb | tee /tmp/info.txt
            grep -Eq '^Repository *: ser2netweb$' /tmp/info.txt || fail "not from the repository ser2netweb"
            ;;
    esac
    case "$family" in
        dnf | zypper)
            # the package's own signature (gpgcheck=1), by the repository's key
            sig=$(rpm -q --qf '%{RSAHEADER:pgpsig}' ser2netweb)
            id=$(printf '%s' "$fingerprint" | cut -c25-40)
            printf '%s' "$sig" | grep -qi "key id $id" ||
                fail "the installed package isn't signed by $fingerprint: $sig"
            repo=/etc/yum.repos.d/ser2netweb.repo
            [ "$family" = dnf ] || repo=/etc/zypp/repos.d/ser2netweb.repo
            grep -q '^gpgcheck *= *1$' "$repo" || { cat "$repo"; fail "$repo has no gpgcheck=1"; }
            echo "OK: the package is signed: $sig"
            if [ "$family" = zypper ]; then
                # zypper checks a package's signature as well as the metadata's: its
                # "GPG Check" column says (rp) (pkg_gpgcheck=1; without it, "(r )")
                zypper --non-interactive repos | tee /tmp/repos.txt
                grep -Eq '\| ser2netweb +\|.*\(.p\) Yes' /tmp/repos.txt ||
                    fail "zypper doesn't check the packages' signatures"
                echo "OK: zypper checks the packages' signatures too"
            else
                # (pkg_gpgcheck: dnf 5 reads it as gpgcheck, dnf 4 passes over it quietly)
                ! grep -i 'unknown configuration option' /tmp/install.log ||
                    fail "dnf doesn't know an option of ser2netweb.repo"
            fi
            ;;
    esac
    echo "OK: ser2netweb $version from the repository"

    run update || { cat /tmp/update.log; fail "the documented update failed"; }
    [ "$(ser2netweb --version)" = "ser2netweb $version" ] || fail "the update changed the version"
    run remove || { cat /tmp/remove.log; fail "the documented removal failed"; }
    ! command -v ser2netweb >/dev/null || fail "ser2netweb is still installed"
    case "$family" in
        apt)
            [ ! -e /etc/apt/sources.list.d/ser2netweb.sources ] || fail "the .sources file stayed"
            apt-get update -qq || fail "apt-get update fails without the repository"
            ;;
        dnf)
            [ ! -e /etc/yum.repos.d/ser2netweb.repo ] || fail "the .repo file stayed"
            ! dnf repolist | grep -q '^ser2netweb ' || fail "dnf still lists the repository"
            ;;
        zypper)
            ! zypper --non-interactive repos | grep -q ' ser2netweb ' || fail "zypper still lists the repository"
            ;;
    esac
    echo "PASS: $family: the repository at $base"
}

# --- on the runner --------------------------------------------------------------------------

change() {  # change FILE SED: a new file (FILE may be a hard link to the built tree)
    sed -E "$2" "$1" >"$1.new"
    ! cmp -s "$1" "$1.new" || fail "nothing changed in $1"
    mv "$1.new" "$1"
}

commands() {  # commands DIR FAMILY BASE SUFFIX PART...: DIR/PART[SUFFIX].sh
    dir=$1 family=$2 url=$3 suffix=$4
    shift 4
    for part in "$@"; do
        python3 tools/package_repo.py commands "$family" --part "$part" --base "$url" --batch \
            --root >"$dir/$part$suffix.sh"
    done
}

key_of() {  # the fingerprint of SITE's keyring
    python3 tools/package_repo.py fingerprint "$1/ser2netweb-archive-keyring.asc"
}

local_test() {  # local_test SITE IMAGE VERSION
    site=$1 image=$2 version=$3
    family=$(family_of "$image")
    work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/repo-smoke.XXXXXX")
    trap 'kill "$server" 2>/dev/null || true; rm -rf "$work"' EXIT
    server=
    mkdir "$work/srv" "$work/cmd"
    port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    for tree in good bad; do
        cp -al "$site" "$work/srv/$tree" 2>/dev/null || cp -a "$site" "$work/srv/$tree"
        for f in ser2netweb.sources ser2netweb.repo; do
            sed "s|$SITE_URL|http://127.0.0.1:$port/$tree|" "$site/$f" >"$work/srv/$tree/$f.new"
            mv "$work/srv/$tree/$f.new" "$work/srv/$tree/$f"
        done
    done
    bad=$work/srv/bad
    change "$bad/deb/dists/stable/InRelease" \
        's/^ [0-9a-f]{64}( +[0-9]+ main\/binary-(amd64|arm64)\/Packages)$/ 0000000000000000000000000000000000000000000000000000000000000000\1/'
    for arch in x86_64 aarch64; do
        change "$bad/rpm/$arch/repodata/repomd.xml" 's|<revision>([0-9]+)</revision>|<revision>1\1</revision>|'
    done
    python3 -m http.server --bind 127.0.0.1 --directory "$work/srv" "$port" >"$work/http.log" 2>&1 &
    server=$!
    base=http://127.0.0.1:$port/good
    i=0
    until curl -fsS -o /dev/null "$base/index.html" 2>/dev/null; do
        i=$((i + 1))
        [ "$i" -lt 50 ] || fail "the server on $port doesn't answer"
        sleep 0.2
    done
    commands "$work/cmd" "$family" "$base" "" install update remove
    commands "$work/cmd" "$family" "http://127.0.0.1:$port/bad" -bad install
    docker run --rm --network host -v "$PWD:/src:ro" -v "$work/cmd:/work:ro" "$image" \
        sh /src/tests/package_repo_smoke.sh --inside "$family" "$version" "$base" "$(key_of "$site")"
}

live_test() {  # live_test SITE IMAGE VERSION
    site=$1 image=$2 version=$3
    family=$(family_of "$image")
    i=0
    for f in deb/dists/stable/InRelease rpm/x86_64/repodata/repomd.xml rpm/aarch64/repodata/repomd.xml; do
        until curl -fsS "$SITE_URL/$f" 2>/dev/null | cmp -s - "$site/$f"; do
            i=$((i + 1))
            [ "$i" -lt 60 ] || fail "$SITE_URL/$f is not this deployment's after 20 minutes"
            sleep 20
        done
    done
    work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/repo-live.XXXXXX")
    trap 'rm -rf "$work"' EXIT
    commands "$work" "$family" "$SITE_URL" "" install update remove
    docker run --rm -v "$PWD:/src:ro" -v "$work:/work:ro" "$image" \
        sh /src/tests/package_repo_smoke.sh --inside "$family" "$version" "$SITE_URL" "$(key_of "$site")" live
}

case "${1:-}" in
    --inside)
        shift
        inside "$@"
        ;;
    --live)
        shift
        live_test "$@"
        ;;
    "" | -*)
        echo "usage: $0 SITE IMAGE VERSION | --live SITE IMAGE VERSION" >&2
        exit 2
        ;;
    *)
        local_test "$@"
        ;;
esac
