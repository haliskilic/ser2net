# Changelog

All notable changes to ser2net. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Detailed notes for each version:
[GitHub Releases](https://github.com/haliskilic/ser2net/releases) and [ROADMAP.md](ROADMAP.md).

## [Unreleased]

### Changed
- Dependencies updated: starlette 1.10.0, uvicorn 0.54.0, websockets 17.4,
  python-multipart 0.0.33, anyio 4.16.0, click 8.5.0, idna 3.20, typing_extensions 4.16.0,
  markupsafe 3.0.4. Python 3.10 keeps starlette 1.7.0, websockets 16.1.1 and
  python-multipart 0.0.32, the last releases that support it. The offline wheels in
  `vendor/wheels/` are refreshed to match (#28).
- The optional packages bundled into the standalone executables and the Docker image are
  pinned in `requirements-optional.txt`; pyasn1 (ldap3) moves to 0.6.5, which fixes known
  vulnerabilities in 0.6.3 (#28).
- `app/web/routes.py` is split into one module per area under `app/web/routes/`; the
  REST API shares the mapping start/stop/delete code with the web UI (#28).

### Added
- `ser2net --about` prints the versions of Python, OpenSSL and the bundled components
  (for bug reports); `ser2net --licenses` prints the license texts of ser2net and every
  third-party component inside the executable. The releases also attach them as
  `*.THIRD-PARTY-LICENSES.txt` (#28).
- LGPL: the executables keep ldap3 as plain source files; `SER2NET_LIB_OVERRIDE=<dir>`
  loads a replacement version from that directory (#28).
- CI: mypy, pip-audit, coverage (the servers the tests start are measured too) and an
  offline-wheel check; Dependabot for pip and GitHub Actions (#28).
- `SECURITY.md`, `CONTRIBUTING.md`, this changelog, issue and pull request templates (#28).

### Removed
- `INCELEME-RAPORU.md` (an outdated internal review) (#28).

## [2.7.1] — 2026-11-03
Long names stay inside the web panel; standalone executables need nothing else installed
(glibc ≥ 2.28 on Linux); in-process self-signed certificates (#26).

## [2.7.0] — 2026-11-03
TCP keepalive detects dead peers; connect timeout, max connection time and idle timeout
everywhere; optional web login (#24).

## [2.6.3] — 2026-06-12
RS-485 hardware auto-RTS in the mapping form.

## [2.6.2] — 2026-06-12
Review hardening: admin bind-IP validation, TLS data-bridge end-to-end test, doc fixes.

## [2.6.1] — 2026-06-12
Cluster: remote edit of a peer's mapping.

## [2.6.0] — 2026-06-11
Cluster: remote start/stop/restart, manual peers, per-node health.

## [2.5.0] — 2026-06-11
LAN cluster: auto-discovery and a unified fleet view.

## [2.4.0] — 2026-06-11
Modbus RTU↔TCP gateway, Modbus → MQTT polling, roles (RBAC), LDAP/AD, standalone executables.

## [2.3.0] — 2026-06-11
REST API token roles, light/dark theme, OIDC single sign-on.

## [2.2] — 2026-06-06
Python 3.10 support and a complete offline bundle (Windows and Linux).

## [2.1] — 2026-06-06
In-browser serial console.

## [2.0] — 2026-06-06
TCP client, UDP and serial↔serial transports, per-mapping TLS, traces, Prometheus metrics,
audit log, hotplug, export/import.

## [1.2] — 2026-06-06
Scale and performance (24 full-duplex bridges).

## [1.1] — 2026-06-06
Hardening and operations.

## [1.0] — 2026-06-06
First release: serial↔TCP (raw, telnet, RFC 2217), web UI, access control, logs.

[Unreleased]: https://github.com/haliskilic/ser2net/compare/v2.7.1...HEAD
[2.7.1]: https://github.com/haliskilic/ser2net/releases/tag/v2.7.1
[2.7.0]: https://github.com/haliskilic/ser2net/releases/tag/v2.7.0
[2.6.3]: https://github.com/haliskilic/ser2net/releases/tag/v2.6.3
[2.6.2]: https://github.com/haliskilic/ser2net/releases/tag/v2.6.2
[2.6.1]: https://github.com/haliskilic/ser2net/releases/tag/v2.6.1
[2.6.0]: https://github.com/haliskilic/ser2net/releases/tag/v2.6.0
[2.5.0]: https://github.com/haliskilic/ser2net/releases/tag/v2.5.0
[2.4.0]: https://github.com/haliskilic/ser2net/releases/tag/v2.4.0
[2.3.0]: https://github.com/haliskilic/ser2net/releases/tag/v2.3.0
[2.2]: https://github.com/haliskilic/ser2net/tree/v2.2
[2.1]: https://github.com/haliskilic/ser2net/tree/v2.1
[2.0]: https://github.com/haliskilic/ser2net/tree/v2.0
[1.2]: https://github.com/haliskilic/ser2net/tree/v1.2
[1.1]: https://github.com/haliskilic/ser2net/tree/v1.1
[1.0]: https://github.com/haliskilic/ser2net/tree/v1.0
