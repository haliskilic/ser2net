# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — build a standalone ser2net executable (no Python install needed).
#   pip install pyinstaller && pyinstaller ser2net.spec
# Produces dist/ser2net(.exe). Config/logs are written next to the binary (./data).
from PyInstaller.utils.hooks import collect_all

datas = [("app/web/templates", "app/web/templates"), ("app/web/static", "app/web/static")]
binaries = []
hiddenimports = ["app", "app.runtime"]

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
    ["ser2net.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["bootstrap"],  # the frozen build skips the offline bootstrap (deps are inside)
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ser2net",
    debug=False, strip=False, upx=False, console=True,
)
