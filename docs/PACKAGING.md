# Packaging ser2netweb for a distribution

For packagers: what ser2netweb needs from a Linux or BSD distribution, which versions it
works with and why, and what each distribution still lacks. Users install ser2netweb with
the release files, the image or pip ([README](../README.en.md)).

ser2netweb is a pure-Python application: the `ser2netweb` package (templates, static
files and translations inside it), built with setuptools from `pyproject.toml` (PEP 517,
`python -m build` or a distribution's pyproject macros), and the command `ser2netweb`
(`ser2netweb.cli:main`, also `python -m ser2netweb`). It needs Python 3.10 or newer. A
package of it keeps its data where the distribution's service unit points `--data-dir`
(a `pip` install defaults to the user's data folder, `ser2netweb/layout.py`; a package that
writes `ser2netweb/distro.json`, below, to its service's data directory). The release's
git tag tarball has the tests; the sdist on PyPI doesn't.

## Telling ser2netweb it is a distribution's package

Installed into the system's `site-packages` with its `.dist-info`, a distribution's package
looks like a pip install to ser2netweb: without `--data-dir` it would keep its data in the
user's data folder, `--about` would say `Build: Python package (pip / pipx)`, and the hint
for a missing optional package would name `pip install "ser2netweb[mqtt]"`. Every recipe in
`packaging/` therefore writes `ser2netweb/distro.json` into the source tree before the build
(`pyproject.toml`'s package data takes it into the wheel, so the distribution's own tooling
packages it with the rest, and the check step's tests see it too):

```json
{"distribution": "Debian", "data_dir": "/var/lib/ser2netweb", "optional": "sudo apt install python3-{dist}"}
```

- `distribution` (required): the name `--about` gives, `Build: distribution package (Debian)`;
- `data_dir`: the data directory of the package's service, the default without `--data-dir`
  (`sudo -u ser2netweb ser2netweb --set-password admin` then works on the service's data);
  left out, the user's data folder, as for pip (nixpkgs, whose package also runs outside
  NixOS);
- `optional`: how the distribution installs an optional package, for the hint about a missing
  one: a command with `{dist}` (the PyPI name: `paho-mqtt`, `ldap3`, `authlib`,
  `cryptography`) or `{extra}` (`mqtt`, `ldap`, `oidc`, `tls`), or an object with a command per
  PyPI name, where a name left out means the distribution has no package of it.

Without the file (a package of another recipe, or one that can't be read) ser2netweb keeps
pip's layout. A marker in the package was chosen over the `.dist-info`'s `INSTALLER`: not
every build tool writes one (`python -m installer`, which Arch, Alpine, FreeBSD and MacPorts
use, doesn't; Fedora writes `rpm`, conda `conda`), and none of it says which distribution it
is, where the package's service keeps its data or how the distribution names a package.

## The dependencies and their floors

`pyproject.toml` gives each dependency a **floor**: the oldest release ser2netweb works
with, chosen as the oldest version one of the distributions below ships, where that is
reachable. There is no upper bound: one goes in only for a release known to break
ser2netweb, and none is.

| Package | Floor | What sets it | What ser2netweb uses it for |
|---|---|---|---|
| Python | 3.10 | the oldest Python ser2netweb supports | |
| pyserial | 3.5 | every distribution ships 3.5 (2020) | the serial ports (`ser2netweb/engine/serial_compat.py` overrides parts of it) |
| pyserial-asyncio-fast | 0.16, optional for a package | the release serial_compat's fixes and the built-in copy are for | the asyncio transport of a serial port; without it, ser2netweb's copy of the same code (below) |
| starlette | 0.46.1 | Debian 13 | the web UI and REST API; what came in 1.6 is stood in for (below) |
| uvicorn | 0.32.0 | Debian 13 | the HTTP server; `ser2netweb/web/httpguard.py` subclasses its h11 protocol |
| h11 | 0.14.0 | Debian 13, Ubuntu 26.04, RHEL/EPEL 10 | uvicorn's HTTP/1.1 parser, also used by httpguard |
| websockets | 14.2 | RHEL 10 | uvicorn's WebSocket implementation (the console, the hex view) |
| python-multipart | 0.0.20 | Debian 13, Ubuntu 26.04 | Starlette's form parser |
| jinja2 | 3.1.6 | what every distribution here ships | the page templates |
| markupsafe | 2.1.3 | RHEL 10 | escaping in the templates and pages |
| psutil | 5.9.8 | RHEL 10 | the network interface lists (the standard library is used without it) |
| exceptiongroup | 1.2.0 | Python 3.10 only | the backport of exception groups |
| taskgroup | 0.2.2 | Python 3.10 only | the backports of `asyncio.TaskGroup` / `asyncio.Runner` |

The extras (`ser2netweb[mqtt]`, `[ldap]`, `[oidc]`, `[tls]`, `[all]`): each feature says in
the UI and the log what is missing without its package.

| Extra | Package | Floor | What sets it |
|---|---|---|---|
| mqtt | paho-mqtt | 1.6.1 | Ubuntu 24.04, Arch, Alpine (1.x and 2.x both work) |
| ldap | ldap3 | 2.9.1 | its last release, which every distribution ships |
| oidc | authlib | 1.4.0 | Fedora 43; 1.3 refuses an ID token without a `kid` against the provider's key set |
| tls | cryptography | 43.0.0 | Debian 13 |

How the floors are kept true:

- **CI's job "minimum dependencies"** (`.github/workflows/ci.yml`) installs
  [`requirements-min.pins`](../requirements-min.pins) alone, which is every floor exactly
  and the oldest release of their own dependencies a distribution here ships (anyio 4.8.0,
  click 8.1.7, idna 3.7, typing_extensions 4.12.2 on Python 3.10), on Python 3.10, and runs
  the whole suite on it: the web UI, the REST API, the cluster, MQTT, LDAP and OIDC with
  their extras' floors, the serial data paths over socat. It runs without
  pyserial-asyncio-fast, as most distributions would. The job isn't a merge condition: a
  floor that stops holding shows there first. Then the floor goes up (`pyproject.toml`,
  `requirements-min.pins` and this page together), or the code gets a fallback.
- `tests/test_pyproject.py` checks that every pin of `requirements.txt` is at or above its
  floor and inside any upper bound, and that `requirements-min.pins` has exactly the floors
  of `pyproject.toml`.
- Without upper bounds, a newer release counts too. `.github/workflows/latest-deps.yml`
  runs the suite every week with the newest release of every dependency and extra (on the
  newest Python), so an upstream release that breaks ser2netweb shows within a week.
  `tests/test_dependency_internals.py` checks the undocumented parts of the dependencies
  ser2netweb builds on (uvicorn's h11 protocol, which `ser2netweb/web/httpguard.py`
  subclasses; pyserial's and pyserial-asyncio-fast's classes; Starlette's StaticFiles and
  Route hooks) against whichever release is installed, and names what moved. A
  distribution's own test run (`python3 tests/run_all.py`) runs it too.
- The floors are about what works, not about security. Some floor releases have published
  vulnerabilities fixed later (h11 before 0.16.0, for example: CVE-2025-43859); a
  distribution's own build carries its security fixes (Debian 13's
  `starlette 0.46.1-3+deb13u3` has backports for CVE-2025-62727 and four 2026 CVEs, for
  example), and pip installs the newest releases. The pins are what CI's pip-audit checks.

## The binaries, the image and the wheel keep the tested set

The floors widen what a distribution may build against; they don't change what the
release files run:

- the **standalone executables**, the **.deb / .rpm packages** made from them and the
  **Docker image** install the hash-locked `requirements-lock.txt` (Python 3.12): the same
  pins as before, whatever the floors are;
- a **source checkout** installs `requirements.txt`'s pins from `vendor/wheels` into
  `./lib` on its first start (`bootstrap.py`), pyserial-asyncio-fast included;
- the **PyPI wheel** has the ranges in its metadata, and pip installs the newest release
  of each: today's pins (Dependabot keeps them current, the test matrix runs them, and
  CI's job "pypi package" installs the wheel with its dependencies from PyPI);
- with the pinned set, the compatibility code below steps aside: Starlette's own body
  limits are used, and pyserial-asyncio-fast's own transport (the tests check both).

## Compatibility code for older releases

- **Starlette before 1.6** (Debian 13's 0.46, Ubuntu 26.04's 0.50, Fedora 43/44's 0.52,
  Alpine's 1.1): `ser2netweb/web/starlette_compat.py` brings `RequestBodyLimitMiddleware`
  (a copy of Starlette 1.10's, with its licence) and a `Route` with `max_body_size`, so
  every request body keeps its limit (413 above it). `tests/test_starlette_compat.py`
  answers the same requests through the copies and Starlette's own and compares them.
  Before Starlette 1.4 a form posted urlencoded (as the browser posts the sign-in form) is
  not held to the form's field count and field size; its body limit still holds (64 KiB
  for the sign-in forms).
- **No pyserial-asyncio-fast** (most distributions): `ser2netweb/engine/serial_transport.py`
  is pyserial-asyncio-fast 0.16's `SerialTransport`, copied unchanged with its BSD licence,
  and `serial_compat` builds on it when `serial_asyncio_fast` doesn't import. The start-up
  log says so once, and `--about` lists pyserial-asyncio-fast as "not installed (its
  transport is built in)". `tests/test_serial_compat.py` checks the copy against a hash of
  its text and an installed 0.16, and runs once more with pyserial-asyncio-fast hidden. A
  package may therefore leave the dependency out (drop the line from `pyproject.toml`'s
  `dependencies` in the build, or don't list it, depending on how the distribution derives
  its runtime dependencies). The older **pyserial-asyncio** 0.6 that several distributions
  ship is not a substitute and is not used: it writes and closes a port differently
  (blocking flushes in the event loop).
- **uvicorn before 0.51** serves WebSockets with websockets' legacy implementation (newer
  releases pick its sans-I/O one); both work. With websockets 14 or newer this raises
  DeprecationWarnings in uvicorn's code, which Python doesn't show by default.

## The distributions' versions

Looked up on 2026-11-13 in the distributions' package indexes (Debian's madison and
sources.debian.org, Launchpad, Fedora's mdapi, CentOS Stream's repositories for RHEL,
openSUSE's download server, archlinux.org, nixpkgs' `nixos-unstable` branch,
pkgs.alpinelinux.org, the FreeBSD ports tree, anaconda.org for conda-forge). The version is
upstream's, without the distribution's revision. **missing**: not packaged; ✗: below the
floor; †: from RHEL's own repositories (CentOS Stream), as EPEL doesn't carry it;
¹ not a dependency of ser2netweb's own, the release `requirements-min.pins` tests;
² only Python 3.10 needs it. The Python column is the distribution's default `python3`.

| | Python | pyserial | pyserial-asyncio-fast | starlette | uvicorn | h11 | websockets | python-multipart | jinja2 | markupsafe | psutil |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **floor** (pyproject.toml) | 3.10 | 3.5 | 0.16 (optional) | 0.46.1 | 0.32.0 | 0.14.0 | 14.2 | 0.0.20 | 3.1.6 | 2.1.3 | 5.9.8 |
| **pinned** (Python 3.12) | 3.12 | 3.5 | 0.16 | 1.10.0 | 0.54.0 | 0.16.0 | 17.4 | 0.0.33 | 3.1.6 | 3.0.4 | 7.2.2 |
| Debian 13 trixie (stable) | 3.13 | 3.5 | **missing** | 0.46.1 | 0.32.0 | 0.14.0 | 15.0.1 | 0.0.20 | 3.1.6 | 2.1.5 | 7.0.0 |
| Debian testing (forky) | 3.14 | 3.5 | **missing** | 1.9.0 | 0.53.0 | 0.16.0 | 17.1 | 0.0.33 | 3.1.6 | 3.0.3 | 7.2.2 |
| Ubuntu 24.04 LTS | 3.12 | 3.5 | **missing** | 0.31.1 ✗ | 0.27.1 ✗ | 0.14.0 | 10.4 ✗ | 0.0.9 ✗ | 3.1.2 ✗ | 2.1.5 | 5.9.8 |
| Ubuntu 26.04 LTS | 3.14 | 3.5 | **missing** | 0.50.0 | 0.38.0 | 0.14.0 | 15.0.1 | 0.0.20 | 3.1.6 | 3.0.3 | 7.1.0 |
| Ubuntu 26.10 | 3.14 | 3.5 | **missing** | 1.7.0 | 0.38.0 | 0.16.0 | 16.0 | 0.0.32 | 3.1.6 | 3.0.3 | 7.1.0 |
| Fedora 43 | 3.14 | 3.5 | **missing** | 0.52.1 | 0.35.0 | 0.16.0 | 15.0.1 | 0.0.33 | 3.1.6 | 3.0.2 | 7.0.0 |
| Fedora 44 | 3.14 | 3.5 | **missing** | 0.52.1 | 0.40.0 | 0.16.0 | 15.0.1 | 0.0.33 | 3.1.6 | 3.0.2 | 7.2.2 |
| Fedora 45 | 3.15 | 3.5 | **missing** | 1.7.0 | 0.40.0 | 0.16.0 | 15.0.1 | 0.0.33 | 3.1.6 | 3.0.2 | 7.2.2 |
| Fedora rawhide | 3.15 | 3.5 | **missing** | 1.10.0 | 0.40.0 | 0.16.0 | 15.0.1 | 0.0.33 | 3.1.6 | 3.0.4 | 7.2.2 |
| EPEL 9 (RHEL 9) | 3.9† | 3.4† ✗ | **missing** | **missing** | **missing** | 0.13.0 ✗ | 14.2† | 0.0.20 | 2.11.3† ✗ | 1.1.1† ✗ | 5.8.0† ✗ |
| EPEL 10 (RHEL 10) | 3.12† | 3.5† | **missing** | 0.47.0 | 0.35.0 | 0.14.0 | 14.2† | 0.0.33 | 3.1.6† | 2.1.3† | 5.9.8† |
| openSUSE Tumbleweed | 3.13 | 3.5 | **missing** | 1.10.0 | 0.51.0 | 0.16.0 | 16.0 | 0.0.32 | 3.1.6 | 3.0.3 | 7.2.2 |
| Arch Linux | 3.14 | 3.5 | **missing** | 1.10.0 | 0.54.0 | 0.16.0 | 17.2 | 0.0.33 | 3.1.6 | 3.0.4 | 7.2.2 |
| nixpkgs unstable | 3.14 | 3.5 | 0.16 | 1.7.0 | 0.51.0 | 0.16.0 | 17.0.1 | 0.0.32 | 3.1.6 | 3.0.4 | 7.2.2 |
| Alpine edge | 3.14 | 3.5 | **missing** | 1.1.0 | 0.44.0 | 0.16.0 | 17.3 | 0.0.32 | 3.1.6 | 3.0.3 | 7.2.2 |
| FreeBSD ports | 3.12 | 3.5 | 0.16 | 1.10.0 | 0.54.0 | 0.16.0 | 17.4 | 0.0.33 | 3.1.6 | 3.0.4 | 7.2.2 |
| conda-forge | 3.10+ | 3.5 | **missing** | 1.10.0 | 0.54.0 | 0.16.0 | 17.4 | 0.0.33 | 3.1.6 | 3.0.4 | 7.2.2 |

| | anyio | click | idna | typing_extensions | exceptiongroup | taskgroup | paho-mqtt | ldap3 | authlib | cryptography |
|---|---|---|---|---|---|---|---|---|---|---|
| **floor** (pyproject.toml) | 4.8.0 ¹ | 8.1.7 ¹ | 3.7 ¹ | 4.9.0 ¹ | 1.2.0 (3.10) | 0.2.2 (3.10) | 1.6.1 | 2.9.1 | 1.4.0 | 43.0.0 |
| **pinned** (Python 3.12) | 4.16.0 | 8.5.1 | 3.20 | 4.16.0 | 1.3.1 (3.10) | 0.2.2 (3.10) | 2.2.0 | 2.9.1 | 1.9.5 | 50.0.2 |
| Debian 13 trixie (stable) | 4.8.0 | 8.1.8 | 3.10 | 4.13.2 | 1.2.2 | not needed ² | 2.1.0 | 2.9.1 | 1.6.0 | 43.0.0 |
| Debian testing (forky) | 4.15.1 | 8.3.3 | 3.20 | 4.16.0 | not needed ² | not needed ² | 2.1.0 | 2.9.1 | 1.9.1 | 49.0.0 |
| Ubuntu 24.04 LTS | 4.2.0 ✗ | 8.1.6 ✗ | 3.6 ✗ | 4.10.0 | 1.2.0 | not needed ² | 1.6.1 | 2.9.1 | 1.3.0 ✗ | 41.0.7 ✗ |
| Ubuntu 26.04 LTS | 4.11.0 | 8.1.8 | 3.11 | 4.15.0 | not needed ² | not needed ² | 2.1.0 | 2.9.1 | 1.6.7 | 46.0.5 |
| Ubuntu 26.10 | 4.15.1 | 8.3.3 | 3.18 | 4.16.0 | not needed ² | not needed ² | 2.1.0 | 2.9.1 | 1.7.2 | 49.0.0 |
| Fedora 43 | 4.13.0 | 8.1.7 | 3.18 | 4.16.0 | 1.3.0 | not needed ² | 2.1.0 | 2.9.1 | 1.4.0 | 46.0.7 |
| Fedora 44 | 4.13.0 | 8.3.1 | 3.18 | 4.16.0 | 1.3.0 | not needed ² | 2.1.0 | 2.9.1 | 1.9.5 | 50.0.0 |
| Fedora 45 | 4.15.1 | 8.4.2 | 3.18 | 4.16.0 | 1.3.0 | not needed ² | 2.1.0 | 2.9.1 | 1.9.5 | 50.0.0 |
| Fedora rawhide | 4.15.1 | 8.4.2 | 3.20 | 4.16.0 | 1.3.0 | not needed ² | 2.2.0 | 2.9.1 | 1.9.5 | 50.0.0 |
| EPEL 9 (RHEL 9) | 3.5.0 ✗ | 8.0.3 ✗ | 2.10† ✗ | 4.16.0 | not needed ² | not needed ² | 1.6.1 | 2.8.1 ✗ | 1.0.1 ✗ | 36.0.1† ✗ |
| EPEL 10 (RHEL 10) | 4.8.0 | 8.1.7 | 3.7† | 4.9.0† | not needed ² | not needed ² | 2.1.0 | 2.9.1 | 1.3.2 ✗ | 49.0.0† |
| openSUSE Tumbleweed | 4.15.1 | 8.5.1 | 3.20 | 4.16.0 | 1.3.1 | not needed ² | 2.1.0 | 2.9.1 | 1.9.1 | 50.0.1 |
| Arch Linux | 4.16.0 | 8.5.1 | 3.20 | 4.16.0 | 1.3.1 | not needed ² | 1.6.1 | 2.9.1 | 1.9.3 | 50.0.2 |
| nixpkgs unstable | 4.15.1 | 8.4.2 | 3.20 | 4.16.0 | 1.3.1 | not needed ² | 2.1.0 | 2.9.1 | 1.9.1 | 50.0.2 |
| Alpine edge | 4.16.0 | 8.4.2 | 3.20 | 4.16.0 | not needed ² | not needed ² | 1.6.1 | 2.9.1 | 1.6.11 | 50.0.0 |
| FreeBSD ports | 4.16.0 | 8.5.0 | 3.20 | 4.16.0 | 1.3.1 | 0.2.2 | 2.1.0 | 2.9.1 | 1.9.5 | 50.0.2 |
| conda-forge | 4.16.0 | 8.5.1 | 3.19 | 4.16.0 | 1.3.1 | 0.2.2 | 2.2.0 | 2.9.1 | 1.9.5 | 50.0.2 |

## What each distribution lacks

Against the floors above (for the recipes of a package: `packaging/`):

- **Debian 13 (stable), Debian testing, Ubuntu 26.04 LTS, Ubuntu 26.10:** only
  pyserial-asyncio-fast, which a package can leave out. Everything else is in the archive
  (Debian's binary packages: `python3-serial`, `python3-starlette`, `python3-uvicorn`,
  `python3-h11`, `python3-websockets`, `python3-python-multipart` (not `python3-multipart`,
  a different library), `python3-jinja2`, `python3-markupsafe`, `python3-psutil`; the
  extras `python3-paho-mqtt`, `python3-ldap3`, `python3-authlib`, `python3-cryptography`).
- **Ubuntu 24.04 LTS:** too old to build against: Starlette 0.31, uvicorn 0.27,
  websockets 10.4, python-multipart 0.0.9 and Jinja 3.1.2 are below the floors (and authlib
  1.3, cryptography 41 below the extras'). Ubuntu 24.04 machines run the release's `.deb`,
  the standalone binary, the image or a pipx install.
- **Fedora 43, 44, 45, rawhide:** only pyserial-asyncio-fast (leave it out). Names:
  `python3-pyserial`, `python3-starlette`, `python3-uvicorn`, `python3-websockets`,
  `python3-python-multipart`, ….
- **EPEL 10 (RHEL 10 and its rebuilds):** pyserial-asyncio-fast (leave it out); authlib 1.3.2
  is below the `oidc` extra's floor, so OpenID Connect sign-in needs a newer
  `python3-authlib` there. Starlette, uvicorn, h11, python-multipart, anyio and click come
  from EPEL 10, the rest from RHEL 10's own repositories.
- **EPEL 9 (RHEL 9):** not a target: its `python3` is 3.9, and Starlette and uvicorn are
  not packaged. RHEL 9 machines run the release's `.rpm`, the standalone binary or the
  image.
- **openSUSE Tumbleweed:** only pyserial-asyncio-fast (leave it out); everything else is in
  the `python313-` / `python314-` flavours.
- **Arch Linux:** only pyserial-asyncio-fast (leave it out). uvicorn's package is named
  `uvicorn`, Jinja's `python-jinja`.
- **nixpkgs:** nothing: `pyserial-asyncio-fast` is in `python3Packages` too.
- **Alpine edge:** pyserial-asyncio-fast (leave it out). uvicorn's package is named
  `uvicorn` (community), the rest `py3-…`.
- **FreeBSD ports:** nothing: `comms/py-pyserial-asyncio-fast` is there too (the ports'
  default Python is 3.12).
- **conda-forge:** only pyserial-asyncio-fast (leave it out; its `pyserial-asyncio` is the
  old 0.6, which ser2netweb doesn't use).

No distribution here runs Python 3.10, so none needs exceptiongroup or taskgroup.

## Recipes for distributions

`packaging/` holds a source package recipe for each of these distributions. Each builds the
release tag's tarball (`https://github.com/haliskilic/ser2netweb/archive/refs/tags/vX.Y.Z.tar.gz`,
which has the tests) with only the distribution's own packages, leaves pyserial-asyncio-fast
out (the built-in transport), installs `/usr/bin/ser2netweb`, the `.deb` / `.rpm`'s hardened
unit (not enabled), the account `ser2netweb` (sysusers.d) and `/var/lib/ser2netweb`
(tmpfiles.d, 0700), and runs in its check step the tests that start no server and open no
network socket. No recipe names the classic C ser2net's package: the two install side by
side.

| Distribution | Recipe | Built in CI in | Linter |
|---|---|---|---|
| Debian, Ubuntu | `packaging/debian/debian/` (source and binary package `ser2netweb`) | `debian:unstable`, `debian:trixie`, `ubuntu:26.04` | lintian; autopkgtest runs `debian/tests` |
| Fedora, EPEL | `packaging/fedora/ser2netweb.spec` | `fedora:rawhide`, `fedora:latest`; `almalinux:10` with EPEL (may fail) | rpmlint |
| openSUSE | `packaging/opensuse/ser2netweb.spec`, `ser2netweb.changes` | `opensuse/tumbleweed` | rpmlint |
| Arch Linux | `packaging/arch/ser2netweb/PKGBUILD`, `.SRCINFO` | `archlinux:latest` | namcap |
| Alpine | `packaging/alpine/ser2netweb/APKBUILD` with an OpenRC service | `alpine:edge` | apkbuild-lint |

**Building one locally** (docker; nothing is installed on the machine). From the
repository's root:

```sh
sh tests/distro_build.sh prepare          # build/distro/: this commit's tree as the tag's tarball
                                          # (or: prepare v3.0.1, that tag's tarball from GitHub)
docker run --rm -v "$PWD:/src" -w /src debian:unstable     sh tests/distro_build.sh debian
docker run --rm -v "$PWD:/src" -w /src fedora:rawhide      sh tests/distro_build.sh fedora
docker run --rm -v "$PWD:/src" -w /src almalinux:10        sh tests/distro_build.sh epel
docker run --rm -v "$PWD:/src" -w /src opensuse/tumbleweed sh tests/distro_build.sh opensuse
docker run --rm -v "$PWD:/src" -w /src archlinux:latest    sh tests/distro_build.sh arch
docker run --rm -v "$PWD:/src" -w /src alpine:edge         sh tests/distro_build.sh alpine
```

Each builds as an unprivileged user, lints (a linter's error fails), installs the package
with the distribution's package manager and runs `tests/distro_smoke.py`: `--version`,
`--about`, the unit or OpenRC script, the account, no relation to a package `ser2net`, then
as the account `--set-password`, a start on 127.0.0.1 that answers `/healthz` and
`--healthcheck`, and a stop. The packages and the linter's report land in
`build/distro/<target>/`. `.github/workflows/distro-packages.yml` runs the same for a pull
request that changes a recipe, by hand (optionally for a release tag) and every Monday, so a
distribution's newer dependencies that break ser2netweb show up; it is not a merge
condition.

**After a release:** `python3 tools/bump_recipes.py X.Y.Z` sets every recipe's version, adds
the changelog entries (Debian, Fedora, openSUSE), writes the checksums of the tag's tarball
(Arch's sha256, Alpine's sha512) and Arch's `.SRCINFO`; commit the result.
`tests/test_distro_recipes.py` checks the recipes against `pyproject.toml` (the dependencies
and floors), `ser2netweb/__init__.py` (the version) and the `.deb` / `.rpm`'s unit, so a
changed dependency or unit fails there until the recipes follow. **3.0.1 is the first
release to submit:** 3.0.0's tag still had the old, narrow dependency ranges and no
built-in transport, so no distribution can build it with its own packages.

The check steps also run `tests/test_dependency_internals.py` and
`tests/test_serial_compat.py`, which check the dependencies' code ser2netweb builds on. In a
distribution's build (the recipe's `ser2netweb/distro.json` in the tree) they name instead
of failing what only a distribution's packages do: pyserial code that differs from the
pinned release's (Fedora's pyserial 3.5 carries upstream's later fixes for custom baud
rates, so the stand-in for glibc 2.42's refusal has nothing to refuse there), and
pyserial's Windows-only modules a package leaves out (openSUSE's has no `serialwin32`).
Everything else, the built-in transport's hash and every behaviour they check, is as strict
as in ser2netweb's own CI, where a changed pin still fails.

### Debian and Ubuntu

- **Layout:** `debhelper-compat (= 13)` with `dh-sequence-python3`,
  `pybuild-plugin-pyproject` and `dh-sequence-installsysusers`; `Rules-Requires-Root: no`;
  `3.0 (quilt)`; binary package `ser2netweb` (`Architecture: all`, section comm).
- **The tarball is repacked** without `vendor/wheels` (prebuilt wheels, some with compiled
  code): `Files-Excluded` in `debian/copyright`, version `X.Y.Z+ds-1`, `debian/watch` reads
  GitHub's tags API (`uscan`).
- **The browser libraries** in `ser2netweb/web/static/` (htmx, xterm.js, its fit addon) are
  minified. Their sources go into `debian/missing-sources/`, made by
  `python3 packaging/debian/missing_sources.py <source tree>` from the npm releases
  `vendor/npm/package.json` records (about 1 MB, not kept in this repository; CI makes them
  for every build). Debian's `libjs-htmx` is htmx 4 in unstable (ser2netweb is written for
  htmx 2), and Starlette's static file handler doesn't follow links out of the package, so
  the embedded copies stay.
- **Service:** `dh_installsystemd --no-enable`: installed, neither enabled nor started; an
  upgrade restarts it when it runs. `README.Debian` says how to turn it on.
- **Ubuntu** takes the package from Debian. 26.04 builds it as it is; 24.04 is too old
  (above).
- **To submit:** (1) file the ITP bug (package wnpp) and put its number into `debian/changelog`
  (`Closes: #NNNNNNN`; lintian warns `initial-upload-closes-no-bugs` until then); (2) a
  salsa.debian.org account, the packaging in a git-buildpackage repository (ideally under
  the Debian Python Team, with `Maintainer: Debian Python Team
  <team+python@tracker.debian.org>`, `Uploaders:` the maintainer, and `Vcs-Git` /
  `Vcs-Browser`); (3) upload to mentors.debian.net and ask for a sponsor (an RFS bug
  against `sponsorship-requests`, or the Python Team's IRC channel / list).

### Fedora and EPEL

- `%pyproject_buildrequires` / `%pyproject_wheel` / `%pyproject_install` /
  `%pyproject_save_files -l`; `%prep` deletes the pyserial-asyncio-fast line from
  `pyproject.toml`, so the generated Requires are the other nine dependencies with their
  floors. The extras are `Recommends`. `%check`: `%pyproject_check_import` and the tests.
- The bundled code is declared: `Provides: bundled(js-htmx)`, `bundled(js-xterm)`,
  `bundled(js-xterm-addon-fit)`, `bundled(python3dist(pyserial-asyncio-fast))`; the npm
  releases of the browser libraries are `Source4`–`Source6`, so the SRPM carries their
  sources.
- **EPEL 10 is blocked:** RHEL 10's `python3-setuptools` is 69, and `pyproject.toml` needs
  setuptools 77 (its licence fields, PEP 639): `%pyproject_buildrequires` finds no
  `python3dist(setuptools) >= 77` (the CI leg on AlmaLinux 10 shows it and may fail).
  `python3-authlib` 1.3.2 there is also below the OIDC extra's 1.4.0, a `Recommends` only.
  RHEL 10 machines use the release's `.rpm` or the DNF repository meanwhile.
- **To submit:** a Fedora account (FAS), `fedora-review -b <bug>` locally, a review request
  in Red Hat Bugzilla (product Fedora, component Package Review) with links to
  the spec and SRPM (a Copr build or fedorapeople space), and a sponsor into the `packager`
  group for a first package. EPEL branches are requested after the Fedora package exists.

### openSUSE

- An application package for the distribution's default Python (`%define pythons python3`),
  `%pyproject_wheel` / `%pyproject_install`, the account through `sysuser-tools`
  (`%sysusers_generate_pre`), the service through `%service_add_*` / `%service_del_*`
  (openSUSE's presets leave it disabled).
- **To submit:** an openSUSE account; `osc mkpac ser2netweb` in a home project
  (`home:<user>`), add the spec, `ser2netweb.changes`, the unit, sysusers and tmpfiles files
  and the tarball (`osc service runall download_files` or a download by hand), build for
  `openSUSE_Tumbleweed`, then `osc submitrequest` to a devel project that fits
  (`devel:languages:python` or `hardware`), and from there to `openSUSE:Factory`.

### Arch Linux

- A source PKGBUILD (`python-build`, `python-installer`, `python-wheel`,
  `python-setuptools`), the dependencies from the official repositories, the extras as
  `optdepends`. The unit runs in the group `uucp`, Arch's group of the serial ports.
- It and the AUR's `ser2netweb-bin` (`packaging/aur/`, the standalone executable) install
  the same files: this one has `conflicts=('ser2netweb-bin')`, that one
  `provides=('ser2netweb')` and `conflicts=('ser2netweb')`.
- **To submit:** as a second AUR package, `ser2netweb`: `git clone
  ssh://aur@aur.archlinux.org/ser2netweb.git`, copy the files of
  `packaging/arch/ser2netweb/` (with `.SRCINFO`) in, commit and push with the AUR account's
  SSH key. The official repositories need a Package Maintainer who adopts it.

### Alpine

- `py3-gpep517` / `py3-installer`, subpackages `-pyc`, `-doc`, `-openrc`. Alpine has no
  systemd: `ser2netweb.initd` (supervise-daemon, as `ser2netweb`, data directory made 0700
  at the start, `reload` sends SIGHUP) and `ser2netweb.confd` (data directory, bind address
  and port, open files) stand in for the unit, and `ser2netweb.pre-install` makes the
  account and adds it to `dialout`. Alpine has no optional dependencies: install
  `py3-paho-mqtt`, `py3-ldap3`, `py3-authlib` or `py3-cryptography` for the features that
  need them.
- **To submit:** an account on gitlab.alpinelinux.org, fork `alpine/aports`, add
  `testing/ser2netweb/` with the four files, commit as `testing/ser2netweb: new aport`,
  open a merge request; its CI builds every architecture.

## Recipes: nixpkgs, Void Linux, Gentoo (GURU), FreeBSD ports, MacPorts

Five recipes in this repository build ser2netweb the way each of these distributions does,
ready to submit there: `packaging/nix`, `packaging/void`, `packaging/gentoo`,
`packaging/freebsd` and `packaging/macports`. Each builds the release's git tag tarball
(`https://github.com/haliskilic/ser2netweb/archive/refs/tags/vX.Y.Z.tar.gz`, which has the
tests; the PyPI sdist doesn't) with the distribution's own Python packages, installs the
command `ser2netweb` and a service for the distribution's init system, and runs it as an
account of its own with the data in the distribution's place for it. None of them provides,
replaces or conflicts with `ser2net` (Corey Minyard's C program, unrelated): both can be
installed side by side.

- **CI:** `.github/workflows/distro-packages-2.yml` builds each recipe in that
  distribution's own tree (pinned by commit or image digest), installs the result and runs
  it: `ser2netweb --version` and `--about`, then its service under systemd (a NixOS VM),
  runit, OpenRC, rc.d or launchd until `ser2netweb --healthcheck` answers. Its first job
  checks every checksum against the tag's real tarball. It runs for pull requests that touch
  the recipes, by hand (Actions → Distribution packages → Run workflow, optionally with
  another release's tag, which it moves the recipes to first) and every Wednesday; it is not
  a merge condition.
- **A new release:** once its tag is out, `python3 tools/bump_recipes_2.py X.Y.Z` writes the
  version and the checksums into every recipe (fetchFromGitHub's NAR hash, Void's SHA-256,
  Gentoo's Manifest and ebuild name, FreeBSD's distinfo, MacPorts' checksums) and starts
  the revisions again; `--check` only compares. `tests/test_distro_recipes_2.py` (in the
  suite) checks that the recipes name one release, at or before `ser2netweb/__init__.py`'s
  version, pyproject.toml's dependencies, one SHA-256 and size, and nothing of `ser2net`.
- **The floors and the built-in transport:** the recipes build 3.0.1, the first release
  with the floors above, so nixpkgs' recipe needs no `pythonRelaxDeps`. Where a
  distribution lacks pyserial-asyncio-fast (Void, Gentoo, MacPorts), its recipe leaves the
  dependency out and ser2netweb runs on its built-in copy of the transport; nixpkgs and
  FreeBSD have it and depend on it. Two modules hold code copied from
  pyserial-asyncio-fast and Starlette (BSD-3-Clause), which each recipe's licence names.

The versions of the three distributions the table above doesn't have (2026-11-13; ✗: below
the floor):

| | Python | pyserial | pyserial-asyncio-fast | starlette | uvicorn | h11 | websockets | python-multipart | jinja2 | markupsafe | psutil | setuptools |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Void Linux | 3.14 | 3.5 | **missing** | 1.10.0 | 0.52.4 | 0.16.0 | 17.2 | 0.0.9 ✗ | 3.1.6 | 3.0.4 | 5.9.8 | 80.10.2 |
| Gentoo | 3.14 | 3.5 | **missing** (GURU too) | 1.10.0 (~arch) | 0.54.0 | 0.16.0 | 17.4 (~arch) | 0.0.33 (~arch) | 3.1.6 | 3.0.4 | 7.2.2 | 84.2.1 |
| MacPorts | 3.14 | 3.5 (py-serial) | **missing** | 1.10.0 | 0.54.0 | 0.16.0 | 17.2 | 0.0.33 | 3.1.6 | 3.0.4 | 7.2.2 | 84.2.1 |

The extras there: Void has paho-mqtt 2.1.0 (`python3-paho_mqtt`), ldap3 2.8.1 ✗ and
cryptography 48.0.0, no Authlib; Gentoo has paho-mqtt 2.2.0, ldap3 2.9.1, authlib 1.9.5 and
cryptography 50.0.2; MacPorts has ldap3 2.9.1 and cryptography 48.0.0 for Python 3.14,
paho-mqtt 2.1.0 only up to Python 3.13, and no Authlib. FreeBSD's `devel/py-setuptools` is
63.1.0, older than PEP 639's licence fields (setuptools 77): its port takes them out of
`pyproject.toml` before the build and installs the licence texts itself.

### nixpkgs

- **Files:** `package.nix` is `pkgs/by-name/se/ser2netweb/package.nix`
  (`buildPythonApplication`, `pyproject = true`, nixpkgs' `python3Packages`, an import check,
  `versionCheckHook`); `module.nix` is the NixOS module `services.ser2netweb`
  (`nixos/modules/services/networking/ser2netweb.nix`: a system user `ser2netweb` in
  `dialout`, `/var/lib/ser2netweb`, the release unit's hardening, `listenAddress` / `port` /
  `openFirewall` / `extraArgs`); `test.nix` is its VM test (`nixos/tests/ser2netweb.nix`:
  the service answers on `/healthz`, `--set-password` as the service user, `systemctl
  reload`). `ci.nix` only ties them to a pinned nixpkgs for CI.
- **Status:** builds and passes its checks against nixos-unstable (2026-11-11); the NixOS
  test passes. Every dependency is in nixpkgs.
- **Build it:**
  `nix-build -I nixpkgs=https://github.com/NixOS/nixpkgs/archive/<commit>.tar.gz -E 'with import <nixpkgs> {}; callPackage ./packaging/nix/package.nix {}'`;
  the VM test: `nix-build -I nixpkgs=… packaging/nix/ci.nix -A test` (needs KVM).
- **Submit** (a pull request to NixOS/nixpkgs from a fork, `master`):
  1. `maintainers: add haliskilic`: in `maintainers/maintainer-list.nix`, in alphabetical
     order, `haliskilic = { name = "Halis KILIÇ"; github = "haliskilic"; githubId = 16972550; };`
     (no e-mail is needed);
  2. `ser2netweb: init at 3.0.1`: `package.nix` as `pkgs/by-name/se/ser2netweb/package.nix`
     with `maintainers = with lib.maintainers; [ haliskilic ];`;
  3. `nixos/ser2netweb: init`: `module.nix` and `test.nix` at the paths above (their
     `meta.maintainers` likewise), the module in `nixos/modules/module-list.nix`, the test in
     `nixos/tests/all-tests.nix` (`ser2netweb = runTest ./ser2netweb.nix;`) and a line in the
     current release notes (`nixos/doc/manual/release-notes/rl-*.section.md`, "New
     Modules");
  4. before opening it: `nixfmt` on the files, `nix-build -A ser2netweb -A nixosTests.ser2netweb`,
     and `nixpkgs-review pr <number>` once it is open. The pull request's template asks what
     was tested; the text is in the report of this work.
  Later releases: r-ryantm (nixpkgs' update bot) opens the version bump itself.

### Void Linux

- **Files:** `srcpkgs/ser2netweb/template` (`build_style=python3-pep517`, the system
  account `_ser2netweb` in `dialout`, `/var/lib/ser2netweb` 0700, a `post_patch` that takes
  pyserial-asyncio-fast out of `pyproject.toml`, as xbps-src checks the installed metadata's
  dependencies against `depends`, a `do_check` running three of the tests), its runit
  service `files/ser2netweb/run` (`conf` there may export `SER2NETWEB_BIND_IP` /
  `SER2NETWEB_PORT` or set `OPTS`) and `INSTALL.msg`; `srcpkgs/python3-python-multipart`
  (an update from 0.0.9).
- **Status:** builds with `xbps-src -s -Q` (strict, as Void's CI; checks included) at
  void-packages' master of 2026-11-13, xlint clean, installs and runs under runit.
  **Missing in Void:** python-multipart newer than 0.0.9 (updated with it) and
  `python3-pyserial-asyncio-fast` (left out: the built-in transport); Authlib (the OIDC
  extra) isn't packaged, ldap3 is 2.8.1.
- **Build it:** `git clone https://github.com/void-linux/void-packages`, copy
  `packaging/void/srcpkgs/*` into its `srcpkgs/`, then `./xbps-src binary-bootstrap`,
  `./xbps-src -Q pkg ser2netweb`, `sudo xbps-install -R hostdir/binpkgs ser2netweb` and
  `sudo ln -s /etc/sv/ser2netweb /var/service/`.
- **Submit** (a pull request to void-linux/void-packages from a fork): one commit per
  package, in this order, with Void's subjects: `python3-python-multipart: update to
  0.0.33.` and `New package: ser2netweb-3.0.1`. Void asks that new packages be runtime tested (state how) and prefers
  them submitted by someone other than the software's author: the pull request should say
  why ser2netweb fits ("System": a system service), and a Void user may be the better
  maintainer. The `maintainer=` lines name the maintainer of this project; change them if
  someone else adopts the package.

### Gentoo (GURU)

- **Files:** `net-misc/ser2netweb` (EAPI 8, `distutils-r1` with
  `DISTUTILS_USE_PEP517=setuptools`, an OpenRC init script and conf.d, a systemd unit,
  `optfeature` for the extras, `metadata.xml`, `Manifest` with the DIST line),
  `acct-user/ser2netweb` (home `/var/lib/ser2netweb` 0700, groups `dialout` and `uucp`, the
  serial ports' groups under systemd's and other udev rules) and `acct-group/ser2netweb`
  (dynamic ids, as GURU's accounts have). pyserial-asyncio-fast, which neither ::gentoo nor
  GURU has, is left out (the built-in transport).
- **Status:** `emerge --oneshot net-misc/ser2netweb` (USE=-test) from the overlay in
  `gentoo/stage3` with the portage snapshot of 2026-11-12; `pkgcheck scan` without errors or warnings; the OpenRC
  service starts, reloads and stops. GURU is `~amd64` only; so are Gentoo's Starlette,
  websockets and python-multipart.
- **Build it:** make `packaging/gentoo` an overlay (`profiles/repo_name`,
  `metadata/layout.conf` with `masters = gentoo`, `thin-manifests = true`), add it in
  `/etc/portage/repos.conf`, accept `~amd64` for it, then `emerge --ask net-misc/ser2netweb`
  and `pkgcheck scan` in the overlay.
- **Submit:** GURU takes commits from its contributors on its `dev` branch
  (`git.gentoo.org/repo/proj/guru.git`); its trusted contributors move reviewed work to
  `master`. What the maintainer does: read [GURU's information for
  contributors](https://wiki.gentoo.org/wiki/Project:GURU/Information_for_Contributors),
  ask for access in `#gentoo-guru` (Libera.Chat) or on the gentoo-guru mailing list with an
  SSH key, sign commits with an OpenPGP key and a `Signed-off-by:` line under the real name
  (GLEP 76), then commit the three packages to `dev` (`acct-group/ser2netweb: new package,
  add 0`, `acct-user/ser2netweb: new package, add 0`, `net-misc/ser2netweb: new package, add
  3.0.1`) after `pkgcheck scan --commits` and `pkgdev manifest`. The metadata names the maintainer of this project.

### FreeBSD ports

- **Files:** `net/ser2netweb` (`Makefile`: `USES=python`, `USE_PYTHON=autoplist noflavors
  pep517`, `USE_GITHUB`, the dependencies from the ports tree, the extras as options, all on;
  `distinfo`, `pkg-descr`, the rc.d script `files/ser2netweb.in` (daemon(8), `reload` sends
  SIGHUP to ser2netweb, `ser2netweb_env` for the web UI's address), `files/pkg-message.in`),
  and the lines the port needs in the ports tree's `UIDs` and `GIDs` (`packaging/freebsd/UIDs`,
  `GIDs`): the user and group `ser2netweb`, 407 (free on 2026-11-13), home
  `/var/db/ser2netweb`, and `ser2netweb` added to `dialer` (the group of `/dev/cua*`).
- **Status:** `make package`, `check-plist`, `make test` and `portlint -AC` on FreeBSD 14.5
  with the ports tree of 2026-11-13; the package installs, `service ser2netweb start` /
  `reload` / `stop` work. Every dependency is in the ports tree. poudriere testport was not
  run in CI (it builds every dependency from source); run it before submitting.
- **Build it** (as root on FreeBSD 14): a ports tree (`git clone --depth 1
  https://git.FreeBSD.org/ports.git /usr/ports`), the lines of `packaging/freebsd/UIDs` and
  `GIDs` in `/usr/ports/UIDs` and `GIDs` (sorted by id; append `,ser2netweb` to `dialer`'s),
  `cp -R packaging/freebsd/net/ser2netweb /usr/ports/net/`, then in it `portlint -AC`, `make
  DEVELOPER=yes package` (or `poudriere testport -j <jail> -o net/ser2netweb`) and `pkg add
  work/pkg/ser2netweb-*.pkg`.
- **Submit:** with an account on bugs.freebsd.org (the maintainer opens it), a new bug in
  Ports & Packages → Individual Port(s), titled `[NEW PORT] net/ser2netweb: Serial-to-network
  bridge with a web UI`, with one `git format-patch` against the ports tree's `main` adding
  `net/ser2netweb`, `SUBDIR += ser2netweb` in `net/Makefile`, and the `UIDs` / `GIDs` lines;
  attach the poudriere testport log (amd64, a 14.x and a 15.x jail). A committer may give the
  account another free id. `MAINTAINER=` is the address bug reports for the port go to.

### MacPorts

- **Files:** `net/ser2netweb/Portfile` (the github and python PortGroups,
  `python.default_version 314`, `depends_lib` on the py314 ports, the user `_ser2netweb`,
  `/opt/local/var/db/ser2netweb` 0700, a launchd startup item running as that user, the
  `ldap` and `tls` variants).
- **Status:** `port lint --nitpick` clean, `port install` on macOS 26 (arm64) with
  MacPorts 2.12.7, `port load` starts it under launchd. **Missing in MacPorts:**
  `py-pyserial-asyncio-fast` (left out: the built-in transport); `py-authlib` (no OIDC
  variant); `py314-paho-mqtt` (`py-paho-mqtt` has Python 3.10 to 3.13 only: no MQTT
  variant until it gets 3.14).
- **Build it:** install MacPorts, put `file:///path/to/packaging/macports` above the
  `rsync://…` line in `/opt/local/etc/macports/sources.conf`, run `portindex` in that
  folder, then `port lint --nitpick ser2netweb`, `sudo port install ser2netweb` and `sudo
  port load ser2netweb`.
- **Submit** (a pull request to macports/macports-ports from a fork): one commit,
  `ser2netweb: new port, version 3.0.1`, with the Portfile at the same path; the pull request's template asks for the
  macOS and Xcode versions tested on and `port lint --nitpick` / `sudo port -vst install`.
  The maintainer is given by GitHub handle (`@haliskilic`) with `openmaintainer`.
