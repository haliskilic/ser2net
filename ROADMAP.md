# ser2netweb — Roadmap

> Current release: **v2.9.0**. CI is green across Linux + Windows × Python 3.10–3.14.

## Shipped

> Milestone labels vs. tags: v1.0–v2.0 are their tags; v2.1a is `v2.1`; v2.1b, v2.2 and v2.3
> shipped together as `v2.3.0` (the `v2.2` tag is Python 3.10 support and the offline
> bundle); v2.4 is `v2.4.0`; v2.6 is `v2.6.0` plus `v2.6.1` (remote edit); later labels
> match their `vX.Y.Z` tags.

### v1.0 — MVP
- Serial↔TCP server bridge: raw / telnet / rfc2217
- Full serial config; live (polled) port list; machine-IP bind picker
- Many-mapping CRUD web UI (Jinja2 + HTMX); atomic JSON config
- Password + CSRF + sessions; allowed + high-priority client IP/CIDR access control
- Per-mapping logs with 100 MB / 15-day retention; offline wheel install

### v1.1 — hardening & ops
- config_lock released before supervisor calls; status snapshot reads
- Session revocation on password change (`pwd_version`); IPv4-mapped IPv6 normalization
- Tight CSP (no `unsafe-inline`; inline *styles* are allowed again since the `v2.2` tag for
  the console, see v2.1a: `style-src 'self' 'unsafe-inline'`, scripts stay `'self'`); data/logs dirs `0700`; expanded systemd hardening
- `closeon`; narrowed serial_io exceptions

### v1.2 — scale & perf
- Per-mapping `client_queue_max`; serial read tuning (limit + `_max_read_size`)
- Telnet negotiation buffer guard; Windows fd-limit docs
- Verified 24 bridges full-duplex byte-exact (`tests/test_fullduplex.py`)

### v2.0 — transports, observability, ops
- **Transports:** TCP client (connect-out), UDP, serial↔serial bridge
- **Per-mapping TLS** for TCP data bridges (server/client)
- **Observability:** per-mapping traffic trace (hex/timestamp), Prometheus `/metrics`,
  config-change audit log
- **Event-driven hotplug** (pyudev / WM_DEVICECHANGE) with polling fallback
- **Admin TLS** via cert/key paths or `openssl` self-signed generation
- **Mappings export/import** (JSON; no accounts or session key, but each mapping's MQTT
  broker password, so treat the file as sensitive) + duplicate

### v2.1a — in-browser serial console
- xterm.js terminal over an authenticated WebSocket (`/api/mappings/{id}/console`):
  read live traffic, or type to the device (net mappings, not read-only)
- Per-mapping monitor sink in the engine; session-cookie auth + Origin/Host CSWSH guard
- CSP `style-src` allows `'unsafe-inline'` (since the `v2.2` tag): xterm.js's DOM renderer adds `<style>`
  elements and style attributes without a nonce (xtermjs/xterm.js#4445); scripts stay
  `'self'` only

### v2.1b — hardening, REST API, Docker & CI
- **Security/correctness fixes:** UDP `allowed_client_ips` enforcement; read-only
  blocks RFC2217 control commands; `stop()`/`start()` lifecycle fixes (3.12+ deadlock,
  serial-task leak); Windows config/log dir lockdown (`icacls`); HTMX validation-error
  visibility; edit no longer drops `match`/RS-485/open-close strings; TCP+UDP same-port
  allowed; cross-mapping serial-device collision check; `start.bat`/systemd fixes
- **Reliability/perf:** scrypt/config-save/log-read moved off the event loop;
  `all.log`/`audit.log` rotation; per-mapping log tail reads
- **REST API** (`/api/v1`): mapping CRUD + start/stop/restart + status + ports,
  bearer-token auth, OpenAPI 3.0 spec
- **Docker** image + `docker-compose` + headless bind env; **CI** (GitHub Actions:
  ruff + ubuntu/windows × Python 3.10–3.13); unified cross-platform test runner

### v2.2 — Modbus, RBAC, LDAP, MQTT (Phase 2)
- **Modbus RTU↔TCP gateway** (`protocol=modbus`): multi-master, bus-locked
  transactions, txn-id integrity, `0x0B` timeout; reply-unit validation. **Edge mode:**
  periodic register polling (uint/int/float 16/32, scaling) published to MQTT
- **Multi-user / RBAC**: `admin` / `operator` / `viewer` roles, server-side enforcement,
  Users panel; legacy single-password config auto-migrates to one admin
- **LDAP / Active Directory auth** (optional `ldap3`): direct or search+bind, LDAP
  group→role mapping, shadow accounts on the RBAC model
- **MQTT publishing** (optional `paho-mqtt`): per-mapping serial-line → broker with
  retained birth/death
- **Client-side virtual COM** recipes (`docs/VIRTUAL-COM.md`)

### v2.3 — UX & access polish
- **OIDC single sign-on** (authorization-code flow + issuer discovery; claim→role
  mapping, shadow accounts) — `ser2netweb/web/oidc_auth.py`, optional `authlib`
- **REST API token roles** (`admin` / `operator` / `viewer`; `viewer` is read-only)
- **Light / dark theme toggle** (persisted in `localStorage`); xterm fit-to-window addon
- _(i18n was dropped — the UI stays English by decision.)_

### v2.4 — packaging & distribution
- **PyInstaller standalone builds** (Windows `.exe`, Linux binary) bundling the optional
  MQTT/LDAP/OIDC deps; GitHub Actions release artifacts published on `v*` tags

### v2.5 — LAN cluster
- **Auto-discovery + unified fleet view**: nodes find each other via HMAC-signed UDP
  broadcast beacons (no mDNS) and one node aggregates every node's mappings into a
  single read-only table, each row tagged with its host (name + IP). Opt-in, off by
  default; trust = a shared cluster key. Server-side fan-out to peers' key-guarded
  `/api/cluster/local`; the browser only talks to the node it logged into.

### v2.6 — cluster depth
- **Remote control + edit** from the unified view: operators start/stop/restart **and edit**
  a peer's mappings. Key-guarded peer endpoints (`/api/cluster/control`, `/mapping-data`,
  `/mapping-save`); session-authed proxies validate the target against a known-address
  allowlist, and the edit **form is rendered on the controlling node** (so the CSRF token
  is the browser's) then proxied to the peer with the shared key.
- **Per-node health**: uptime · version · running/total, plus an online/offline indicator
  and a UI banner when UDP discovery can't bind (manual peers still work)
- **Manual peers** (`host:port`) for routed/L3 networks broadcast can't reach, aggregated
  alongside auto-discovered nodes

### v2.6.2 — review hardening
- Full multi-dimension review (engine correctness, web/security, config/packaging,
  design/docs/tests). Net result: no critical bugs — the engine's `_clients` snapshots
  are race-free (synchronous comprehensions), auth/RBAC/CSRF/SSRF model is sound.
- Validate `admin_ui.bind_ip` in `AppConfig.validate()` (clear error instead of a
  uvicorn bind crash on a hand-edited config)
- Closed the top test gap: **per-mapping TLS data-bridge e2e** (`test_tls_bridge.py` —
  verifies the channel is encrypted + plaintext is rejected) and a bind-IP validation case
- Doc accuracy: global logs are size-rotated (distinct from the per-mapping
  100 MB/15-day trim)

### v2.6.3 — RS-485 in the UI
- **RS-485 hardware auto-RTS** (`TIOCSRS485`) is now exposed in the mapping form
  (enable + RTS pre/post-TX delays + RTS-high-during-TX); the engine already applied it
  via pyserial `rs485_mode`. Edit-preservation updated so the form owns the RS-485 fields
  while still carrying over the two it doesn't expose (`hangup_when_done`, `nobreak`).

### v2.7 — connection lifecycle & optional login ([#24](https://github.com/haliskilic/ser2net/issues/24))
- **Fix: dead TCP peers are detected.** A client (or connect-out remote / Modbus master)
  that vanished without a FIN/RST — power cut, cable pull, Wi-Fi/VPN/NAT drop — stayed
  `ESTABLISHED` forever on a quiet raw bridge and, with `max_connections=1`, locked
  everyone else out ("Busy"). Every TCP data socket now gets **TCP keepalive**, on by
  default (60 s idle / 10 s interval / 3 probes, per mapping): SO_KEEPALIVE +
  TCP_KEEPIDLE/INTVL/CNT, `SIO_KEEPALIVE_VALS` on pre-1709 Windows, and on Linux
  `TCP_USER_TIMEOUT` (when shorter than the kernel's ~15 min) so the bound also holds
  while data is in flight.
- **Connect timeout** for connect-out (TCP + TLS handshake; default 10 s, 0 = OS default),
  **max connection time** (0 = unlimited), **idle timeout** now also on the Modbus
  gateway; every disconnect is logged with its reason. All in the mapping form.
- **Optional web login:** Settings → Web UI access — switch "Require login" off and give
  visitors a role; signing in still works, the REST API keeps its token, open access
  applies only via IP / localhost / own host name (DNS-rebinding guard, proxy headers
  included), other cluster nodes still need a real login, and open consoles close when
  access is revoked.
- Viewers can no longer read stored MQTT credentials (mapping form / export: operator+;
  viewer API tokens get them redacted). Legacy `idle_timeout_s` values from 2.6.x
  configs are normalised so headless/Docker boots don't fail validation.
- Tests: real **half-open TCP** end-to-end tests on Linux (iptables) **and Windows**
  (WinDivert) in CI — dead client / dead client while streaming / connect-out dead remote
  / dead Modbus master / SYN to a silent host / legacy Windows path, plus a keepalive-off
  control that reproduces the original bug; timer and open-access e2e tests.

### v2.7.1 — long names in the UI, truly standalone binaries ([#26](https://github.com/haliskilic/ser2net/issues/26))
- **Web UI:** long mapping names, `/dev/serial/by-id/...` paths, host names and error
  messages no longer push the row buttons out of the panel (they sat up to ~1000 px
  outside it and the page scrolled sideways). Tables wrap long text at separators, row
  buttons wrap inside their cell, below 940 px the lists become cards; header, log and
  console titles wrap too. Checked in a real browser at 1440–390 px in CI.
- **Linux binary runs on glibc ≥ 2.28** (RHEL 8+, Debian 11+, Ubuntu 20.04+…), not only on
  the newest distributions (it needed glibc 2.38): built in an AlmaLinux 8 container with
  a python-build-standalone CPython.
- **No external programs:** "Generate self-signed certificate" no longer needs the
  `openssl` command (made in-process with the bundled `cryptography`); the Linux binary
  finds the distribution's CA bundle on its own (Debian/RHEL/openSUSE layouts); `icacls`
  runs by its full System32 path.
- **Tests:** every PR and release checks the executables — bundle contents (nothing but
  glibc / Windows system DLLs from outside), an empty-`PATH` run, the libraries the running
  process loads, and clean AlmaLinux 8/9, Debian 11/12, Ubuntu 20.04/22.04/24.04,
  openSUSE and Fedora containers; release assets are published only after that passes.

### v2.8.0 — maintenance, security, distribution, features, Turkish UI (issues [#28](https://github.com/haliskilic/ser2net/issues/28)–[#32](https://github.com/haliskilic/ser2net/issues/32))
- **Maintenance (#28):** `routes.py` split into per-area modules; mypy, coverage, pip-audit
  and Dependabot in CI; dependencies updated (Python 3.10 keeps its last compatible line);
  GitHub Actions on Node 24 and no duplicate push+PR runs; `SECURITY.md`,
  `CONTRIBUTING.md`, `CHANGELOG.md`, templates; complete third-party license texts in the
  executables (`--licenses`) and a replaceable LGPL ldap3.
- **Security & reliability (#29):** trace files confined to `data/traces/` with a size
  limit, rotation and off-loop writes; the session timeout setting takes effect;
  automatic config backups with restore; import preview with merge; audit log viewer;
  Docker image runs as a non-root user with a HEALTHCHECK.
- **Distribution (#30):** Linux arm64 executable, Windows service mode
  (`--install-service`), multi-arch GHCR image, optional Authenticode signing,
  `SHA256SUMS` + build provenance + SBOM.
- **Features (#31):** bind a mapping to a USB device's identity, webhook / e-mail
  notifications, dashboard search/filter/bulk actions, trace download + live hex view,
  Modbus write (FC 5/6/15/16) + Sparkplug B, classic `ser2net.yaml` import.
- **Turkish UI (#32):** the web UI in Turkish and English (reverses the v2.3 decision); logs,
  audit log and REST API stay English.

### v2.8.1 — security release (PRs [#42](https://github.com/haliskilic/ser2net/pull/42)–[#46](https://github.com/haliskilic/ser2net/pull/46))
- **Engine inputs (#42):** mapping ids validated wherever they enter (completes 2.8.0's trace
  confinement), serial ports must be devices, read-only enforced for Modbus masters and
  MQTT / Sparkplug, single-point MQTT writes, bounded UDP queue and RFC 2217 subnegotiations,
  the Modbus gateway reconnects; file-descriptor and connection limits (admin UI deadlines and
  caps, allow-list before the TLS handshake, select-free serial reads).
- **Fleet & import (#43):** signed cluster requests and replies (raw key only for older nodes,
  switchable), peers called directly at their known address, planned key changes, a new
  identity for copied installs; the classic ser2net import keeps protections or imports
  disabled.
- **Accounts (#44):** LDAPS / StartTLS certificate checks, whole-name group matching, no SSO
  takeover of local or other-method accounts, OIDC hardening (PKCE, https-only), a guarded
  first-run setup, server-side sign-out, credential-safe backup restore, account recovery from
  the command line.
- **Access (#45):** viewers see device output only and no traces; a fail-closed API token role;
  request body limits and bounded failed-sign-in records; one route access matrix in CI.
- **Distribution (#46):** the Windows service in an admin-only location with in-place upgrades
  and an OpenSSL 3.5 build; one instance per data directory; Docker starts despite a rejected
  setting and keeps mounted folders' owners; the standalone executables have the Turkish UI
  again and survive temp cleaners.

### v2.9.0 — the rest of the 2.8.0 review (issues [#48](https://github.com/haliskilic/ser2net/issues/48)–[#90](https://github.com/haliskilic/ser2net/issues/90), PRs [#91](https://github.com/haliskilic/ser2net/pull/91)–[#98](https://github.com/haliskilic/ser2net/pull/98))
- **Serial and network (#91):** ser2net's own serial classes (paced Windows writes), listeners
  that bind before the device opens and retry, the configured adapter among identical USB
  adapters, IPv4 + IPv6 listeners, per-mapping TLS checks (CA, pins, client certificates, key
  passwords), telnet BREAK, UDP targets, MQTT for serial mappings.
- **Web security and proxies (#92):** stronger password hashes and per-name sign-in limits,
  cross-site refusals and a renewed CSRF token, trusted proxies and a public URL (serving under
  a path), several instances on one host, privacy controls and `docs/PRIVACY.md`.
- **API, audit and clock (#93):** named API tokens, two-factor sign-in, `PATCH` and a REST
  import, a complete one-line audit log, clock-step-proof durations and UTC offsets.
- **Web UI (#94):** visible errors and results, Settings post/redirect/get, console limits and
  reconnects, a live table that sends only changes, Turkish-aware text, an accurate classic
  import.
- **Config and storage (#95):** typed configuration, one-at-a-time all-or-nothing mapping
  changes, trace deletion, a storage budget and retention, `/metrics` and new notifications,
  an event loop watchdog, reloading `config.json` and admin TLS without a restart.
- **Modbus and MQTT (#96):** length-framed RTU replies, Sparkplug B unit health, a sturdier MQTT
  client with TLS files, bounded in-order writes, unique MQTT identities, typed Modbus points
  with more data types and an editor.
- **CI, releases, distribution (#97):** one `ci-ok` gate, SHA-pinned actions, gated and
  attested releases, SBOM licence ids, systemd units that start on a fresh install, an install
  script, offline installs on ARM and macOS, a `linux/arm/v7` image.
- **Docs (#98):** the READMEs and guides checked against the code, guides for MQTT / Modbus /
  Sparkplug B, moving from the classic ser2net, SSO and Linux serial ports.

---

## Planned

### Later
- Cluster: once fleets have moved to signed peer requests, make legacy (raw-key)
  authentication off by default and later drop it; optional TLS certificate pinning for
  peer requests (requests and replies are signed, but over plain HTTP a reply can still be
  read on the network); optional IPv6 (multicast) discovery
- `.deb` / `.rpm` packages (the Linux executable has an install script,
  `systemd/ser2net-install.sh`, since 2.9.0)
- RTU inter-frame tuning for the Modbus gateway

### Icebox / conditional
- Thread-per-port serial backend — only if a real Windows high-throughput /
  tight-timing need emerges (current asyncio path handles 24+ full-duplex fine)
- Pluggable persistent session store

> Note: mDNS/zeroconf advertising is intentionally **not** on the roadmap; LAN cluster
> discovery uses signed UDP broadcast instead.

---

## Per-release definition of done
What a change and a release need (tests, `tr.json`, both READMEs and the guides in `docs/`,
the CHANGELOG, upgrade fixtures, screenshots when the UI changed, the release steps):
[CONTRIBUTING.md](CONTRIBUTING.md#releases) and the pull request checklist.
