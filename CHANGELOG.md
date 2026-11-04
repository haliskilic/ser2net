# Changelog

All notable changes to ser2net. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Detailed notes for each version:
[GitHub Releases](https://github.com/haliskilic/ser2net/releases) and [ROADMAP.md](ROADMAP.md).

## [Unreleased]

### Security
- **Traces can no longer write outside the data directory.** A mapping's trace was a free
  file path, so an operator (not only an admin) could have raw network bytes appended to
  any file the service may write — `config.json`, a startup script; as root in Docker.
  Traces now always go to `<data>/traces/<mapping id>-<file name>`; a path in an older
  config keeps only its file name, and the move is logged at start-up (#29).
- The Docker image runs as an unprivileged user (uid 10001) instead of root. A `/data`
  volume written by an older image is taken over automatically on the first start (#29).
- Audit records can't be forged with control characters in a user-supplied name (#29).

### Fixed
- The session lifetime setting (`session_timeout_s`) now takes effect; it was stored but
  sessions always lasted 8 hours. Settings → Web UI access → Session lifetime (#29).
- Writing a trace no longer blocks the event loop (a writer thread batches and flushes);
  trace files rotate at a size limit (default 10 MB, 3 old files kept) (#29).
- The Docker HEALTHCHECK no longer reports a fresh container unhealthy while it waits for
  the first-run setup (`/healthz` answers then too) (#29).

### Added
- **Bind a mapping to a USB device's identity** (VID:PID + serial number) from the mapping
  form, so re-plugging an adapter as another `ttyUSB*` / `COM*` keeps the mapping working;
  the port list shows Linux `/dev/serial/by-id` names (selectable) and hides the kernel's
  placeholder `ttyS` ports that have no hardware (#31).
- **Notifications:** webhook (JSON, Slack, Microsoft Teams Workflows, Discord, Telegram)
  and/or e-mail when a serial device is lost or comes back, a mapping fails to start,
  clients connect/disconnect, or a sign-in fails; repeat suppression, rate limit, test
  message (Settings → Notifications) (#31).
- **Dashboard:** search, state filter, sort, and start/stop/restart of selected mappings
  (`POST /api/mappings/bulk`); the table is only redrawn when it changed (the refresh
  answers 204 otherwise), so it no longer flickers or loses selections (#31).
- **Traces in the UI:** list and download a mapping's trace files (current + rotated);
  the console has a **hex view** with both directions and timestamps (a new read-only
  WebSocket); the text console's description now says what it shows (#31).
- **Modbus write from MQTT:** points with `"writable": true` take values from
  `<topic>/<point>/set` (coils FC5/FC15, holding registers FC6/FC16; the result goes to
  `.../set/result`); coils and discrete inputs (FC1/FC2) can be polled too (#31).
- **Sparkplug B:** a Modbus-polling mapping can publish as a Sparkplug B edge node
  (NBIRTH/NDATA/NDEATH with `bdSeq`/`seq`, writes and rebirth through NCMD), without a
  protobuf dependency (#31).
- **Import from the classic ser2net:** `ser2net.yaml` (4.x) and `ser2net.conf` (3.x) go
  through the import preview; options without an equivalent are listed as warnings. ser2net
  opens a device only while a client is connected, a mapping here keeps it open: of several
  ports on one device the first is imported enabled, the others disabled (#31).
- **Linux arm64 executable** (`ser2net-linux-arm64`, glibc ≥ 2.28: Raspberry Pi 3/4/5 with a
  64-bit OS, ARM gateways), tested like the x86_64 one in 9 clean distributions (#30).
- **Windows service** without extra software: `--install-service` / `--uninstall-service`
  (starts at boot, restarts after a failure, `--service-name` for several instances),
  through the Windows API (#30).
- **Docker image on GHCR** for amd64 + arm64 with every release (`ghcr.io/haliskilic/ser2net`),
  smoke-tested on both architectures (#30).
- Releases publish `SHA256SUMS`, a CycloneDX SBOM per executable and GitHub build provenance
  + SBOM attestations; the Windows exe is code-signed when a signing service is configured
  (SignPath or Azure Trusted Signing, `docs/SIGNING.md`) (#30).
- Automatic configuration backups: each change keeps the previous `config.json` in
  `data/backups/` (newest 50); admins can download or restore them in Settings, and a
  restore is itself backed up (#29).
- Import preview: the import shows what will be added / updated / removed first and then
  merges (same name = update, keeping the mapping's id, log and traces) or replaces all.
  A one-step POST without the preview step still replaces everything, as before (#29).
- Settings → Audit log: sign-ins and every change with user, IP, action and detail;
  search, filters, paging and CSV export. Records now carry the acting user (#29).
- `ser2net --healthcheck`, used by the Docker image's `HEALTHCHECK` (#29).

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
