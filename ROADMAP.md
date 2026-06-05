# pyser2net — Roadmap

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

---

## Planned

### v2.1 (remaining) — UX & access
- Multi-user / RBAC: accounts + roles (admin / operator / viewer), per-mapping permissions
- i18n (TR/EN) for the UI
- keyboard polish; dark/light theme toggle; xterm fit-to-window addon

### v2.2 — packaging & distribution
- PyInstaller `--onedir` builds (Windows `.exe`, Linux ELF)
- Windows service installer (Shawl); `.deb` / `.rpm`
- Automated multi-platform wheelhouse (cp311–cp313 × OS)
- CI/CD: GitHub Actions lint + test matrix (Linux/Windows), release artifacts

### v2.3 — serial/industrial depth
- RS-485 hardware auto-RTS (`TIOCSRS485`) UI + Modbus RTU inter-frame awareness
- Virtual COM helper integration/docs (com0com / socat / `rfc2217://`)
- ser2net `ser2net.yaml` import/export for migration

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
