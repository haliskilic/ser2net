# Build the standalone Windows executable dist\ser2net.exe with the same pinned
# python-build-standalone (PBS) interpreter as the Linux binaries, so Windows ships a
# supported OpenSSL (3.5 LTS) instead of the end-of-life OpenSSL 3.0 that a frozen
# actions/setup-python minor pins. The interpreter (CPython + OpenSSL/libffi/zlib...
# linked in, pip included) is pinned by version + sha256.
#
#   pwsh tools\build_standalone_windows.ps1
#
# tests\test_standalone.py checks the result (bundled DLLs, empty-PATH run, UI, the
# OpenSSL floor); .github\workflows\standalone.yml runs this on windows-latest.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Keep these in step with tools/build_standalone_linux.sh (one release for every platform).
$PBS_RELEASE = "20261026"
$PBS_PYTHON = "3.12.16"
# sha256 of the x86_64-pc-windows-msvc install_only_stripped archive (release SHA256SUMS)
$PBS_SHA256 = "1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149"
# The Python packages, PyInstaller included, come hash-locked from requirements-lock.txt and
# requirements-build.txt (tools/refresh_locks.py).
$MIN_OPENSSL = "3.5"

New-Item -ItemType Directory -Force -Path build | Out-Null
$archive = "build\python-windows.tar.gz"
$tarball = "cpython-$PBS_PYTHON+$PBS_RELEASE-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$url = "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_RELEASE/$tarball"
Write-Host "Downloading $url"
curl.exe -fsSL --retry 3 -o $archive $url
$got = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
if ($got -ne $PBS_SHA256) { throw "sha256 mismatch for $tarball`n  got      $got`n  expected $PBS_SHA256" }

$pbs = "build\pbs"
if (Test-Path $pbs) { Remove-Item -Recurse -Force $pbs }
New-Item -ItemType Directory -Force -Path $pbs | Out-Null
tar.exe -xzf $archive -C $pbs          # bsdtar ships with Windows 10/11 and the runner
$PY = Join-Path $pbs "python\python.exe"
if (-not (Test-Path $PY)) { throw "interpreter not found at $PY after extraction" }

# every file checked against its sha256 in the lock, every dependency locked, wheels only
& $PY -m pip install --no-cache-dir --disable-pip-version-check -q `
    --require-hashes --only-binary=:all: -r requirements-lock.txt -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# license texts of the native libraries PBS links into the interpreter (OpenSSL, libffi,
# zlib, ...), from the same release tag — so `ser2net --licenses` covers them on Windows
# too (the python.org build folded these into its own LICENSE.txt; PBS keeps them apart).
$licdir = "build\licenses-native"
New-Item -ItemType Directory -Force -Path $licdir | Out-Null
foreach ($lic in "bzip2","expat","libffi","liblzma","mpdecimal","openssl-3","sqlite","tcl","tix","zlib") {
    curl.exe -fsSL --retry 3 -o "$licdir\LICENSE.$lic.txt" `
        "https://raw.githubusercontent.com/astral-sh/python-build-standalone/$PBS_RELEASE/LICENSE.$lic.txt"
}

& $PY tools\win_version_info.py build\win_version.txt
if ($LASTEXITCODE -ne 0) { throw "version resource generation failed" }
& $PY tools\third_party.py licenses build\THIRD-PARTY-LICENSES.txt --strict --extra $licdir
if ($LASTEXITCODE -ne 0) { throw "license bundle failed" }
& $PY tools\release_guard.py interpreter --min-openssl $MIN_OPENSSL
if ($LASTEXITCODE -ne 0) { throw "release guard failed (OpenSSL below $MIN_OPENSSL)" }
& $PY -m PyInstaller --clean --noconfirm ser2netweb.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
# after the build: the spec lists the binaries the exe carries (pyexpat.pyd, _decimal.pyd ...)
& $PY tools\third_party.py sbom build\ser2net.cdx.json --strict --binaries build\bundled-binaries.txt
if ($LASTEXITCODE -ne 0) { throw "sbom failed" }
& dist\ser2net.exe --version
if ($LASTEXITCODE -ne 0) { throw "the built exe did not run" }
