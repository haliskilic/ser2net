# ser2net — Roadmap

> Current release: **v2.6.1**. CI is green across Linux + Windows × Python 3.10–3.13.

## Shipped

### v1.0 — MVP
- Serial↔TCP server bridge: raw / telnet / rfc2217
- Full serial config; live (polled) port list; machine-IP bind picker
- Many-mapping CRUD web UI (Jinja2 + HTMX); atomic JSON config
- Password + CSRF + sessions; allowed + high-priority client IP/CIDR access control
- Per-mapping logs with 100 MB / 15-day retention; offline wheel install

### v1.1 — hardening & ops
- config_lock released before supervisor calls; status snapshot reads
- Session revocation on password change (`pwd_version`); IPv4-mapped IPv6 normalization
- Tight CSP (no `unsafe-inline`); data/logs dirs `0700`; expanded systemd hardening
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
- **Mappings export/import** (JSON, no secrets) + duplicate

### v2.1a — in-browser serial console
- xterm.js terminal over an authenticated WebSocket (`/api/mappings/{id}/console`):
  read live traffic, or type to the device (net mappings, not read-only)
- Per-mapping monitor sink in the engine; session-cookie auth + Origin/Host CSWSH guard

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
  mapping, shadow accounts) — `app/web/oidc_auth.py`, optional `authlib`
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

---

## Planned

### v2.6.x — cluster hardening (from the v2.5 review)
- Validate/curb peer-advertised IPs before the server-side fetch (SSRF defense-in-depth)
- Optional TLS certificate pinning for peer fetch/control; a light rate-limit on the
  key-guarded peer endpoints; optional IPv6 (multicast) discovery

### v2.7 — industrial/IIoT depth
- **Sparkplug B** edge payloads (Modbus register + MQTT plumbing already in place)
- **Modbus write** support (FC 5/6/15/16), per-point MQTT→register control, RTU
  inter-frame tuning
- **RS-485 hardware auto-RTS** (`TIOCSRS485`) UI

### v2.8 — packaging & migration
- Windows service installer (Shawl); `.deb` / `.rpm` packages
- Automated multi-platform offline wheelhouse (cp310–cp313 × OS) for source installs
- Classic `ser2net.yaml` import for migration

### Maintenance
- Bump pinned GitHub Actions off Node 20 (deprecated 2026-06-16) to current
  `actions/*@v5` to clear the CI/release deprecation warnings

### Icebox / conditional
- Thread-per-port serial backend — only if a real Windows high-throughput /
  tight-timing need emerges (current asyncio path handles 24+ full-duplex fine)
- Per-mapping TLS client certificate verification (mTLS) for data bridges
- Pluggable persistent session store

> Note: mDNS/zeroconf advertising is intentionally **not** on the roadmap; LAN cluster
> discovery uses signed UDP broadcast instead.

---

## Per-release definition of done
Each item ships with: a socat/PTY-based integration test, updated offline wheels,
README/feature docs, a green full test suite + stress regression, and a git tag.
