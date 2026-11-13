# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — build a standalone ser2net executable (no Python install needed).
#   pip install pyinstaller && pyinstaller ser2netweb.spec
# Produces dist/ser2net(.exe). Config/logs are written next to the binary (./data).
# Release builds: tools/build_standalone_linux.sh (old-glibc container) and
# tools/build_standalone_windows.ps1, from .github/workflows/standalone.yml;
# tests/test_standalone.py checks both.
import os
import subprocess
import sys

from PyInstaller.utils.hooks import collect_all

# the files the app reads at run time: page templates, static assets, UI translations
# (tests/test_standalone.py fails when a data file under ser2netweb/ is left out)
datas = [(d, d) for d in ("ser2netweb/web/templates", "ser2netweb/web/static", "ser2netweb/locale")]
# every bundled component's license (tools/third_party.py; `ser2net --licenses` prints it).
# The build scripts write it first; a plain `pyinstaller ser2netweb.spec` gets it written here,
# from this environment (the interpreter's native libraries' own texts only come with the
# build scripts' --extra), instead of an executable without it.
# (SPECPATH: PyInstaller is building; tests/test_standalone.py reads the spec without it)
BUILDING = "SPECPATH" in globals()
LICENSES = os.path.join("build", "THIRD-PARTY-LICENSES.txt")
if BUILDING and not os.path.isfile(LICENSES):
    print(f"ser2netweb.spec: {LICENSES} is missing; writing it with tools/third_party.py")
    subprocess.run([sys.executable, os.path.join("tools", "third_party.py"), "licenses", LICENSES],
                   check=True)
if os.path.isfile(LICENSES):
    datas.append((LICENSES, "."))
binaries = []
hiddenimports = ["ser2netweb", "ser2netweb.runtime"]

# uvicorn/starlette load submodules dynamically; the optional deps (paho/ldap3/
# authlib/cryptography) are bundled so the binary supports MQTT/LDAP/OIDC out of
# the box. Missing optional packages are skipped.
for pkg in ["uvicorn", "starlette", "jinja2", "serial", "serial_asyncio_fast",
            "websockets", "multipart", "anyio", "h11", "click", "psutil",
            "paho", "ldap3", "authlib", "pyasn1", "cryptography"]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["ser2netweb.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["bootstrap"],  # the frozen build skips the offline bootstrap (deps are inside)
    noarchive=False,
    # LGPL: ldap3 goes in as plain .py files (not into the archive), so a user can load a
    # modified version instead with SER2NETWEB_LIB_OVERRIDE=<dir> (see THIRD-PARTY-NOTICES.md)
    module_collection_mode={"ldap3": "py"},
)
# libgcc_s (wanted by cryptography's Rust module) is part of every glibc system, and
# manylinux wheels expect the system's copy; bundling the build machine's would only
# raise the minimum glibc version.
a.binaries = [b for b in a.binaries if not os.path.basename(b[0]).startswith("libgcc_s.so")]
# what the executable carries, for `tools/third_party.py sbom --binaries`: which of the
# interpreter's native libraries (expat, mpdecimal, SQLite ...) the SBOM lists
if BUILDING:
    os.makedirs("build", exist_ok=True)
    with open(os.path.join("build", "bundled-binaries.txt"), "w", encoding="utf-8") as fh:
        fh.write("".join(f"{b[0]}\n" for b in a.binaries))
pyz = PYZ(a.pure)
# Windows: stamp the exe with a version resource (tools/win_version_info.py writes
# build/win_version.txt from ser2netweb.__version__ at build time) and the ser2net icon, so
# Explorer's Details tab shows the version/publisher and the exe no longer carries
# PyInstaller's default console icon. Both are ignored on other platforms.
version_file = os.path.join("build", "win_version.txt")
icon_file = os.path.join("tools", "ser2net.ico")
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ser2net",
    debug=False, strip=False, upx=False, console=True,
    version=version_file if os.path.isfile(version_file) else None,
    icon=icon_file if os.path.isfile(icon_file) else None,
)
