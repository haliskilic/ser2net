# Packaging ser2netweb for a distribution

For packagers: what ser2netweb needs from a Linux or BSD distribution, which versions it
works with and why, and what each distribution still lacks. Users install ser2netweb with
the release files, the image or pip ([README](../README.en.md)).

ser2netweb is a pure-Python application: the `ser2netweb` package (templates, static
files and translations inside it), built with setuptools from `pyproject.toml` (PEP 517,
`python -m build` or a distribution's pyproject macros), and the command `ser2netweb`
(`ser2netweb.cli:main`, also `python -m ser2netweb`). It needs Python 3.10 or newer. A
package of it keeps its data where the distribution's service unit points `--data-dir`
(a `pip` install defaults to the user's data folder, `ser2netweb/layout.py`). The release's
git tag tarball has the tests; the sdist on PyPI doesn't.

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
