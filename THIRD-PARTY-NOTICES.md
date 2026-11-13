# Third-Party Notices

ser2netweb is licensed under the AGPL-3.0-only or a commercial license (see `NOTICE`). It
ships with the third-party components below, each under its own license. These
components are not covered by ser2netweb's license.

**Full license texts:** every standalone executable carries the texts of everything it
contains — run `ser2netweb --licenses` — and each release attaches the same file as
`ser2net-<platform>.THIRD-PARTY-LICENSES.txt`. It is generated at build time from the
build environment (`tools/third_party.py`), so it always matches the bundled versions. The
release's SBOMs (`ser2net-<platform>.cdx.json`, CycloneDX) list the same components with SPDX
license ids, the interpreter's native libraries (OpenSSL, zlib, expat, mpdecimal, SQLite as
bundled) with their versions and CPEs, the OpenSSL that cryptography links in, and the
browser libraries below.

## Python dependencies (`requirements.txt`, wheels in `vendor/wheels/`)

| Component | License |
|---|---|
| pyserial | BSD-3-Clause |
| pyserial-asyncio-fast | BSD-3-Clause |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |
| websockets | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| Jinja2 | BSD-3-Clause |
| MarkupSafe | BSD-3-Clause |
| anyio | MIT |
| click | BSD-3-Clause |
| h11 | MIT |
| idna | BSD-3-Clause |
| typing_extensions | PSF-2.0 |
| psutil | BSD-3-Clause |
| exceptiongroup, taskgroup (Python 3.10 only) | MIT |
| colorama (Windows only) | BSD-3-Clause |

## Optional feature packages (`requirements-optional.txt`)

Not installed by the offline bootstrap; bundled into the standalone executables and the
Docker image. Used unmodified, through their public APIs.

| Component | Feature | License |
|---|---|---|
| paho-mqtt | MQTT publishing | EPL-2.0 OR BSD-3-Clause (EDL-1.0) |
| ldap3 | LDAP / AD sign-in | **LGPL-3.0-or-later** — see below |
| pyasn1 (ldap3 dependency) | LDAP / AD sign-in | BSD-2-Clause |
| Authlib | OIDC single sign-on | BSD-3-Clause |
| joserfc (Authlib dependency) | OIDC single sign-on | BSD-3-Clause |
| cryptography (links its own OpenSSL, Apache-2.0, into its module) | OIDC, self-signed certificates | Apache-2.0 OR BSD-3-Clause |
| cffi, pycparser (cryptography dependencies) | | MIT-0 / BSD-3-Clause |

## Inside the standalone executables

| Component | License |
|---|---|
| CPython 3.12 interpreter and standard library | PSF-2.0 |
| OpenSSL 3.5 (linked into the interpreter) | Apache-2.0 |
| libffi, expat, libX11/libXau/libxcb (Linux) | MIT |
| zlib / zlib-ng | Zlib |
| bzip2 | bzip2-1.0.6 |
| XZ Utils (liblzma) | 0BSD / public domain |
| SQLite | public domain |
| mpdecimal, libuuid, libedit, zstd (Linux) | BSD-2-Clause / BSD-3-Clause |
| ncurses (Linux) | X11-style |
| Tcl / Tk / Tix | TCL (BSD-style) |
| Microsoft Visual C++ runtime, Universal CRT (Windows) | Microsoft redistributable license |
| PyInstaller bootloader | GPL-2.0-or-later **with the bootloader exception**, which allows distributing it inside programs under any license |
| PyInstaller and pyinstaller-hooks-contrib runtime hooks | GPL-2.0-or-later with the bootloader exception / Apache-2.0 |

Both executables use a [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
interpreter (the native libraries above are linked into it), pinned to the same release
per platform so Windows and Linux ship the same CPython and a supported OpenSSL (3.5 LTS).
Each native library's own license text is included in `ser2netweb --licenses` and in the
`*.THIRD-PARTY-LICENSES.txt` attached to the release.

## Browser assets (`ser2netweb/web/static/`)

Copied unmodified from their npm releases; `vendor/npm/package.json` records the versions,
the files and their sha256, and the license texts are in `tools/licenses/npm/` (and in
`ser2netweb --licenses`).

| Component | npm package | License |
|---|---|---|
| htmx (`htmx.min.js`) | `htmx.org` | 0BSD |
| xterm.js (`xterm.js`, `xterm.css`) | `@xterm/xterm` | MIT |
| xterm.js fit addon (`xterm-addon-fit.js`) | `@xterm/addon-fit` | MIT |

## LGPL components

ldap3 is an LGPL library. ser2netweb uses it unmodified through its public API. The LGPL
lets you run the program with a modified version of the library; that right holds for
every distribution of ser2netweb — AGPL or commercial:

- **Source and Docker installs:** ldap3 is a normal Python package; install another
  version with `pip`.
- **Standalone executables:** ldap3 is stored as plain source files (`.py`), not inside
  the executable's compressed archive. Point `SER2NETWEB_LIB_OVERRIDE` at a directory that
  contains your version (`<dir>/ldap3/...`) and ser2netweb loads it instead of the bundled
  copy; `ser2netweb --about` shows which one is in use. The bundled source is the
  Corresponding Source of the library; the project is at https://github.com/cannatag/ldap3.

---

Preserve this file (and the `--licenses` text of a binary you redistribute) when
redistributing ser2netweb.
