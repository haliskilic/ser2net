#!/usr/bin/env bash
# Build the standalone Linux executable dist/ser2netweb so it runs on any x86_64 or
# aarch64 (arm64) distribution with glibc >= 2.28 (RHEL/Alma/Rocky 8+, Debian 11+,
# Ubuntu 20.04+, Fedora, openSUSE Leap 15.x, Raspberry Pi OS 11+ 64-bit ...) with
# nothing else installed. Run it INSIDE an AlmaLinux 8 container (glibc 2.28) of the
# target architecture, so pip only picks wheels that work there:
#
#   docker run --rm -v "$PWD:/src" -w /src almalinux:8 bash tools/build_standalone_linux.sh
#   (on an arm64 host, or with --platform linux/arm64, the same builds the arm64 binary)
#
# The interpreter is a python-build-standalone CPython (glibc 2.17, shared libpython
# as PyInstaller needs, OpenSSL/zlib/libffi linked in), pinned by version + sha256.
# tests/test_standalone.py checks the result (glibc ceiling, bundled libraries,
# empty-PATH run, clean distribution containers).
set -euo pipefail

PBS_RELEASE=20261026
PBS_PYTHON=3.12.16
# sha256 of the install_only_stripped archive per architecture (from the release's SHA256SUMS)
PBS_SHA256_x86_64=ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20
PBS_SHA256_aarch64=bb4b6392670c5d3f93876996fcaa19858a4a413a0c7720144c18b0d6f1f0db2d
# the Python packages, PyInstaller included, come hash-locked from requirements-lock.txt and
# requirements-build.txt (tools/refresh_locks.py)

ARCH=$(uname -m)
case "$ARCH" in
  x86_64) PBS_SHA256=$PBS_SHA256_x86_64 ;;
  aarch64) PBS_SHA256=$PBS_SHA256_aarch64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# PyInstaller needs objdump/objcopy (binutils); ldd comes with glibc
dnf -y -q install binutils tar gzip >/dev/null
command -v curl >/dev/null || dnf -y -q install curl >/dev/null

tarball="cpython-${PBS_PYTHON}+${PBS_RELEASE}-${ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
curl -fsSL --retry 3 -o /tmp/python.tar.gz \
  "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${tarball}"
echo "${PBS_SHA256}  /tmp/python.tar.gz" | sha256sum -c -
tar -xzf /tmp/python.tar.gz -C /opt
PY=/opt/python/bin/python3

# every file checked against its sha256 in the lock, every dependency locked, wheels only
"$PY" -m pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -q \
  --require-hashes --only-binary=:all: -r requirements-lock.txt -r requirements-build.txt
# license texts of the native libraries python-build-standalone links into the
# interpreter (OpenSSL, libffi, zlib, …), from the same release tag
mkdir -p build/licenses-native
for lic in bzip2 cpython expat libX11 libXau libedit libffi liblzma libuuid libxcb mpdecimal \
           ncurses openssl-3 sqlite tcl tix zlib zlib-ng zstd; do
  curl -fsSL --retry 3 -o "build/licenses-native/LICENSE.${lic}.txt" \
    "https://raw.githubusercontent.com/astral-sh/python-build-standalone/${PBS_RELEASE}/LICENSE.${lic}.txt"
done
"$PY" tools/third_party.py licenses build/THIRD-PARTY-LICENSES.txt --strict --extra build/licenses-native
# fail the build if this interpreter's OpenSSL is an unsupported (EOL) series
"$PY" tools/release_guard.py interpreter --min-openssl 3.5
"$PY" -m PyInstaller --clean --noconfirm ser2netweb.spec
# after the build: the spec lists the binaries the executable carries
"$PY" tools/third_party.py sbom build/ser2netweb.cdx.json --strict --binaries build/bundled-binaries.txt
dist/ser2netweb --version
