# ser2net — Roadmap

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

---

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

### v2.2 — commercial features (Phase 2)
- **REST API** (`/api/v1`): bearer-token JSON API — mapping CRUD, start/stop/restart,
  status, ports, OpenAPI 3.0 spec
- **Modbus RTU↔TCP gateway** (`protocol=modbus`): multi-master, bus-locked
  transactions, txn-id integrity, `0x0B` timeout; reply-unit validation. **Edge mode:**
  periodic register polling (uint/int/float 16/32, scaling) published to MQTT
- **Multi-user / RBAC**: `admin` / `operator` / `viewer` roles, server-side enforcement,
  Users panel; legacy single-password config auto-migrates to one admin
- **LDAP / Active Directory auth** (optional `ldap3`): direct or search+bind, LDAP
  group→role mapping, shadow accounts on the RBAC model
- **MQTT publishing** (optional `paho-mqtt`): per-mapping serial-line → broker with
  retained birth/death
- **Client-side virtual COM** recipes (`docs/VIRTUAL-COM.md`); refreshed UI screenshots

---

## Planned

### v2.3 — UX & access polish
- **OIDC / SAML browser-SSO** (next auth step after LDAP; auth-code flow + JWKS)
- **REST API token scopes/roles** (currently a single admin-level token) + per-token
- i18n (TR/EN) for the UI; dark/light theme toggle; xterm fit-to-window addon

### v2.4 — packaging & distribution
- PyInstaller `--onedir` builds (Windows `.exe`, Linux ELF) + Windows service installer
- `.deb` / `.rpm`; automated multi-platform wheelhouse (cp311–cp313 × OS)
- **Bundle the optional wheels** (`paho-mqtt`, `ldap3`) so MQTT/LDAP work on air-gapped
  installs (today they need internet to `pip install`; the UI now warns when missing)
- GitHub Actions release artifacts

### v2.5 — industrial/IIoT depth
- **Sparkplug B** edge payloads (Modbus register + MQTT plumbing already in place)
- Modbus: write support (FC 5/6/15/16), per-point MQTT→register control, RTU inter-frame
  tuning; RS-485 hardware auto-RTS (`TIOCSRS485`) UI
- Multi-host **fleet dashboard** (manage several instances; subscription tier)
- classic `ser2net.yaml` import for migration

### Icebox / conditional
- Thread-per-port serial backend — only if a real Windows high-throughput /
  tight-timing need emerges (current asyncio path handles 24+ full-duplex fine)
- Per-mapping TLS client certificate verification (mTLS) for data bridges
- Pluggable persistent session store

> Note: mDNS/zeroconf advertising has been removed from the roadmap.

---

## Per-release definition of done
Each item ships with: a socat/PTY-based integration test, updated offline wheels,
README/feature docs, a green full test suite + stress regression, and a git tag.
