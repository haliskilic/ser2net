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
- **3.0.0 and the floors:** the recipes build 3.0.0, whose `pyproject.toml` still had the
  ranges from before the floors above (Starlette 1.10, uvicorn 0.54, websockets 17.4,
  python-multipart 0.0.33, pyserial-asyncio-fast required). nixpkgs' recipe relaxes those
  four (`pythonRelaxDeps`); the other distributions don't check the ranges when they build.
  3.0.0 has no built-in serial transport yet, so where a distribution lacks
  pyserial-asyncio-fast, its recipe comes along (Void, Gentoo, MacPorts). From the first
  release with the floors on, `pythonRelaxDeps` can go.

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
  2. `ser2netweb: init at 3.0.0`: `package.nix` as `pkgs/by-name/se/ser2netweb/package.nix`
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
  account `_ser2netweb` in `dialout`, `/var/lib/ser2netweb` 0700, a `do_check` running three
  of the tests), its runit service `files/ser2netweb/run` (`conf` there may export
  `SER2NETWEB_BIND_IP` / `SER2NETWEB_PORT` or set `OPTS`) and `INSTALL.msg`;
  `srcpkgs/python3-pyserial-asyncio-fast` (new, with a patch that lets its test run on
  Python 3.14) and `srcpkgs/python3-python-multipart` (an update from 0.0.9).
- **Status:** builds with `xbps-src -Q` (checks included) at void-packages' master of
  2026-11-13, xlint clean, installs and runs under runit. **Missing in Void:**
  `python3-pyserial-asyncio-fast` (in this pull request) and python-multipart newer than
  0.0.9 (updated in it); Authlib (the OIDC extra) isn't packaged, ldap3 is 2.8.1.
- **Build it:** `git clone https://github.com/void-linux/void-packages`, copy
  `packaging/void/srcpkgs/*` into its `srcpkgs/`, then `./xbps-src binary-bootstrap`,
  `./xbps-src -Q pkg ser2netweb`, `sudo xbps-install -R hostdir/binpkgs ser2netweb` and
  `sudo ln -s /etc/sv/ser2netweb /var/service/`.
- **Submit** (a pull request to void-linux/void-packages from a fork): one commit per
  package, in this order, with Void's subjects: `python3-python-multipart: update to
  0.0.33.`, `New package: python3-pyserial-asyncio-fast-0.16`, `New package:
  ser2netweb-3.0.0`. Void asks that new packages be runtime tested (state how) and prefers
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
  (dynamic ids, as GURU's accounts have), and `dev-python/pyserial-asyncio-fast` (neither
  ::gentoo nor GURU has it).
- **Status:** `emerge --oneshot net-misc/ser2netweb` (USE=-test) from the overlay in
  `gentoo/stage3` with the portage snapshot of 2026-11-12; `pkgcheck scan` clean; the OpenRC
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
  (GLEP 76), then commit the four packages to `dev` (`acct-group/ser2netweb: new package,
  add 0`, `acct-user/ser2netweb: new package, add 0`, `dev-python/pyserial-asyncio-fast: new
  package, add 0.16`, `net-misc/ser2netweb: new package, add 3.0.0`) after `pkgcheck scan
  --commits` and `pkgdev manifest`. The metadata names the maintainer of this project.

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
  `ldap` and `tls` variants) and `python/py-pyserial-asyncio-fast/Portfile`.
- **Status:** `port lint --nitpick` clean (both), `port install` on macOS 26 (arm64) with
  MacPorts 2.12.7, `port load` starts it under launchd. **Missing in MacPorts:**
  `py-pyserial-asyncio-fast` (its Portfile is here, submitted with it); `py-authlib` (no OIDC
  variant); `py314-paho-mqtt` (`py-paho-mqtt` has Python 3.10 to 3.13 only: no MQTT
  variant until it gets 3.14).
- **Build it:** install MacPorts, put `file:///path/to/packaging/macports` above the
  `rsync://…` line in `/opt/local/etc/macports/sources.conf`, run `portindex` in that
  folder, then `port lint --nitpick ser2netweb`, `sudo port install ser2netweb` and `sudo
  port load ser2netweb`.
- **Submit** (a pull request to macports/macports-ports from a fork): two commits,
  `py-pyserial-asyncio-fast: new port, version 0.16` and `ser2netweb: new port, version
  3.0.0`, with the Portfiles at the same paths; the pull request's template asks for the
  macOS and Xcode versions tested on and `port lint --nitpick` / `sudo port -vst install`.
  The maintainer is given by GitHub handle (`@haliskilic`) with `openmaintainer`.
