#!/usr/bin/env bash
# Build the standalone Linux executable dist/ser2net so it runs on any x86_64
# distribution with glibc >= 2.28 (RHEL/Alma/Rocky 8+, Debian 10+, Ubuntu 20.04+,
# Fedora, openSUSE Leap 15.x ...) with nothing else installed. Run it INSIDE an
# AlmaLinux 8 container (glibc 2.28), so pip only picks wheels that work there:
#
#   docker run --rm -v "$PWD:/src" -w /src almalinux:8 bash tools/build_standalone_linux.sh
#
# The interpreter is a python-build-standalone CPython (glibc 2.17, shared libpython
# as PyInstaller needs, OpenSSL/zlib/libffi linked in), pinned by version + sha256.
# tests/test_standalone.py checks the result (glibc ceiling, bundled libraries,
# empty-PATH run, clean distribution containers).
set -euo pipefail

PBS_RELEASE=20261026
PBS_PYTHON=3.12.16
PBS_SHA256=ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20
PYINSTALLER=6.22.3

# PyInstaller needs objdump/objcopy (binutils); ldd comes with glibc
dnf -y -q install binutils tar gzip >/dev/null
command -v curl >/dev/null || dnf -y -q install curl >/dev/null

tarball="cpython-${PBS_PYTHON}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
curl -fsSL --retry 3 -o /tmp/python.tar.gz \
  "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${tarball}"
echo "${PBS_SHA256}  /tmp/python.tar.gz" | sha256sum -c -
tar -xzf /tmp/python.tar.gz -C /opt
PY=/opt/python/bin/python3

"$PY" -m pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore -q \
  -r requirements.txt paho-mqtt ldap3 authlib cryptography "pyinstaller==${PYINSTALLER}"
"$PY" -m PyInstaller --clean --noconfirm ser2net.spec
dist/ser2net --version
