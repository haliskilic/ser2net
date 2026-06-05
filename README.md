# pyser2net

Expose locally-connected serial devices (COM ports / `ttyUSB*` / `ttyACM*` / `ttyS*`)
over the network (TCP), managed entirely from a web UI. A cross-platform
(Windows + Linux), pure-Python take on the classic `ser2net` — built to map **dozens**
of serial ports to IP:port endpoints, bidirectionally, from one screen.

- **Transports:** TCP **server** (listen), TCP **client** (connect-out), **UDP**, and
  **serial↔serial** bridging. Optional per-mapping **TLS** for TCP data bridges.
- **Protocols:** `raw`, `telnet` (RFC 854, 8-bit clean), `rfc2217` (remote clients can
  change baud/parity/etc. live, via pyserial's `PortManager`).
- **Observability:** per-mapping traffic trace (hex/timestamp), Prometheus `/metrics`,
  config-change audit log, and a per-mapping live log viewer.
- **Full serial config:** baud (incl. custom), data bits, parity, stop bits, flow control
  (none / RTS-CTS / XON-XOFF / DSR-DTR), RTS/DTR on open, exclusive open.
- **Live, refreshable port list** — polling baseline (no privileges to *list*) plus optional
  event-driven hotplug (pyudev / `WM_DEVICECHANGE`) that falls back to polling.
- **Bind IP picker** from the machine's own addresses (`0.0.0.0` / `127.0.0.1` / each LAN IP)
  or a custom IP — for both each mapping and the admin UI itself.
- **Access control per mapping:** allowed client IPs/CIDRs, **high-priority client IPs/CIDRs**
  (a priority client is admitted by evicting the oldest non-priority client when the port is at
  capacity, even without kick-old-client), max connections, kick-old-client (evicts the oldest),
  read-only, idle timeout, connect banner, open/close strings, and `closeon` (disconnect
  clients when the device emits a given string).
- **Supervised reconnect** with stable-id (VID/PID/serial) device re-resolution.
- **Password-protected** admin UI (set on first access, changeable later — a password
  change revokes all other sessions), CSRF, signed-cookie sessions, login rate-limiting,
  strict CSP/security headers, optional TLS.

## Requirements

- Python **3.11+** on the target machine.
- All third-party dependencies are bundled offline in `vendor/wheels/` — no internet needed.

## Quick start

**Linux / macOS**
```bash
python3 ser2net.py          # or: ./start.sh
```

**Windows**
```bat
start.bat
```

On first launch the **console** asks which local IP the configuration web UI should
listen on (one of the machine's IPs, or a custom one) and a port (default `8080`).
Then open the printed URL in a browser — the **first page asks you to set an admin
password**. After that, add mappings from the dashboard.

Re-pick the admin bind address anytime:
```bash
python3 ser2net.py --reconfigure
```

### Türkçe hızlı başlangıç
1. `python3 ser2net.py` çalıştırın (Windows'ta `start.bat`).
2. Konsolda arayüzün hangi IP'den erişileceğini seçin (makine IP'leri veya custom) ve portu girin.
3. Tarayıcıda açılan adrese gidin, ilk ekranda **admin parolasını** belirleyin.
4. Panodan **+ Add mapping** ile COM/tty portunu seçip IP:port'a eşleyin. Onlarca eşleme ekleyebilirsiniz.

## How it works

A single process runs one asyncio event loop hosting both the web admin (Starlette +
uvicorn) and every serial↔TCP bridge. Each mapping is a supervised task: it binds a TCP
listener on the chosen IP:port, keeps the serial port open (auto-reconnecting), and pumps
bytes both ways with per-client backpressure isolation. On Windows the loop is forced to a
`SelectorEventLoop` (required by `pyserial-asyncio-fast`); on Linux the default loop is used.

## Scale & platform notes

- **Concurrency:** verified at **24 simultaneous bridges** under realistic request/response
  load (thousands of byte-exact round-trips, ~3 ms p50, 0 reconnects, flat ~27 MB RSS) and at
  **24 bridges in simultaneous full-duplex bulk** (512 KB each way, byte-exact). See
  `tests/stress_24.py` and `tests/test_fullduplex.py`.
- **Per-client backpressure:** each client has its own bounded outbound queue
  (`client_queue_max`, default 2048 chunks ≈ up to ~64 KB × that per client); a client slower
  than the serial source is dropped and counted (`dropped_clients`/`queue_overflows` in status)
  rather than stalling the others. Raise it per mapping for bursty high-throughput links.
- **Windows:** the runtime forces a `SelectorEventLoop` (required by `pyserial-asyncio-fast`)
  and runs uvicorn inside it. That loop is capped at 512 sockets (FD_SETSIZE) — far above
  24–50 serial bridges + the admin server — so it is not a practical limit here. Serial I/O on
  Windows is polling-based; benchmark tight-timing links there before committing.

## Offline installation

`bootstrap.py` installs the bundled wheels into a local `./lib/` directory (added to
`sys.path` at startup) using `pip install --no-index --find-links vendor/wheels`. It runs
automatically on first launch; you can also run it manually:
```bash
python3 bootstrap.py            # offline
python3 bootstrap.py --online   # allow PyPI fallback if a wheel is missing
```

The wheelhouse ships pure-Python wheels (work everywhere) plus `win_amd64` + `manylinux`
binaries for `psutil` / `markupsafe` (cp311/cp312). To support other Python versions or
architectures, add matching wheels:
```bash
python3 -m pip download -r requirements.txt -d vendor/wheels \
  --platform win_amd64 --python-version 312 --only-binary=:all:
```

## Linux serial permissions

Listing ports needs no privileges, but **opening** a serial device for forwarding requires
membership in the `dialout` group (devices are `root:dialout`):
```bash
sudo usermod -aG dialout "$USER"   # then log out/in
```
Without it, mappings show `EACCES` / permission-denied. **Do not run as root.**

## Run as a service

- **Linux (systemd):** see `systemd/ser2net.service`. Runs as a dedicated unprivileged user
  with `SupplementaryGroups=dialout` and `Restart=on-failure`. Edit paths/user, then
  `sudo systemctl enable --now ser2net`.
- **Windows:** wrap with [Shawl](https://github.com/mtkennerly/shawl):
  `shawl add --name pyser2net -- "C:\path\to\python.exe" "C:\path\to\ser2net.py"` then
  `sc config pyser2net start= auto`.

## Security notes

The admin UI is **always** password-protected. By default it binds to `127.0.0.1` (loopback) —
including on headless/service first-run, so it is never network-exposed without an explicit
choice. To expose it on the LAN, pick a network IP at first launch (or `--reconfigure`); the app
logs a **warning** if bound to a network address without TLS. For LAN-exposed deployments, set TLS
(`admin_ui.tls_cert` / `tls_key` in `config.json` — both required, validated at load) and restrict
each mapping with `allowed_client_ips`. Raw TCP is plaintext — treat it as insecure on untrusted
networks. A bare `0.0.0.0` / `::` in an allowed/priority list means "any client".

## Configuration & state

All persistent state lives in the data dir (default `data/`):

- `config.json` — the admin UI bind IP (chosen on first launch), the admin password hash,
  and every serial→network mapping. Written atomically; editable from the UI.
- `all.log` — global activity/audit log: logins/logouts, serial port open/close, client
  connect/disconnect (with duration), mapping start/stop/edit, HTTP requests, and errors.
- `logs/<mapping-id>.log` — per-mapping history, viewable from each mapping's **Log** button
  in the UI (newest first, auto-refreshing, persists across restarts). Automatically maintained
  hourly: entries older than **15 days** are dropped and each file is capped at **100 MB**
  (oldest entries trimmed first).

Deleting `config.json` + `all.log` **fully resets the system**: the next launch starts fresh
(console asks for the bind IP again, the web UI asks for a new password), and any leftover
per-mapping logs are auto-pruned on startup because no mapping references them anymore.

## Project layout

```
ser2net.py            entry point (bootstrap, console first-run, hand off to runtime)
bootstrap.py          offline wheel installer -> ./lib
start.bat / start.sh  launchers
requirements.txt
vendor/wheels/        bundled dependency wheels (offline)
app/
  config.py           data model + atomic JSON persistence
  console.py          first-launch bind-IP picker
  runtime.py          owns the event loop; runs uvicorn + engine
  state.py            shared runtime state
  engine/
    serial_io.py      pyserial-asyncio-fast open + device resolution
    bridge.py         per-mapping runner, client fan-out, reconnect
    supervisor.py     mapping registry + lifecycle
    portlist.py       port enumeration + hotplug watcher
    netinfo.py        host IP enumeration
    protocols/        raw / telnet / rfc2217 codecs
  web/
    server.py         Starlette app + auth/CSRF/security middleware
    routes.py         pages + HTMX/JSON API
    auth.py           scrypt, sessions, CSRF, rate limiting
    templates/        Jinja2 (HTMX)
    static/           htmx.min.js, app.css, app.js
systemd/ser2net.service
tests/                end-to-end tests (use socat virtual serial ports)
```

## Testing

Tests use `socat` PTY pairs (Linux) so no hardware is needed:
```bash
python3 tests/test_bridge_raw.py   # raw bidirectional
python3 tests/test_telnet.py       # telnet IAC + negotiation
python3 tests/test_rfc2217.py      # RFC2217 + live baud change
python3 tests/test_web_e2e.py      # setup -> auth -> mapping CRUD -> live bridge
```

## License

MIT-style; bundled dependencies retain their own licenses (pyserial BSD-3, etc.).
