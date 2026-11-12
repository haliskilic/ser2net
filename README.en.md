<!-- ser2net — README (English). Türkçe (default): README.md -->

**🌐 [Türkçe](README.md) · [English](README.en.md)**

# ser2net

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE) [![Commercial license](https://img.shields.io/badge/Commercial-license%20available-green.svg)](COMMERCIAL_LICENSE.md) [![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-db61a2?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/haliskilic)

**A cross-platform (Windows + Linux), pure-Python serial-to-network bridge with a
web management UI** — a modern, web-managed take on the classic `ser2net`. Map dozens
of serial ports (COM / ttyUSB / ttyACM / ttyS) to TCP/UDP endpoints, bidirectionally,
with low latency.

---

## 📸 Screenshots

**Dashboard — dozens of mappings, live status, detected ports and the LAN cluster panel:**

![Dashboard](docs/screenshots/02-dashboard.png)

**LAN cluster — every node's mappings in one table; per-node health + remote start/stop:**

![Cluster](docs/screenshots/06-cluster.png)

| Add mapping (all serial/network options) | In-browser serial console (xterm.js) |
|---|---|
| ![Mapping form](docs/screenshots/03-add-mapping.png) | ![Console](docs/screenshots/05-console.png) |

| Settings (password · TLS · cluster · backup) | Login |
|---|---|
| ![Settings](docs/screenshots/04-settings.png) | ![Login](docs/screenshots/01-login.png) |

---

## 📌 Purpose

Industrial devices, PLCs, instruments, GPS/modems, microcontrollers and console ports
usually speak **serial** (RS-232/485/USB-serial). `ser2net` bridges each serial port to
a **TCP/UDP endpoint** so any computer on the network can reach it, and manages
everything from a **password-protected web UI** — no CLI or hand-edited config files.

**Typical use cases:**
- Expose 10+ USB-serial devices attached to one server to networked applications
- Reach SCADA / Modbus-RTU devices from remote clients (raw or RFC2217)
- Network access to device consoles (switches, routers, embedded boards)
- RFC2217 for devices that need remote baud/parity changes
- Bridge two serial ports together (serial↔serial)

---

## ✨ Features

- **Transports:** TCP **server** (listen), TCP **client** (connect-out), **UDP**, and
  **serial↔serial** bridging; optional per-mapping **TLS** for TCP bridges. UDP sends the
  device's output to the sender of the last datagram from an allowed IP, or always to a
  fixed **reply address** (IP + port); a **peer timeout** forgets a sender that went quiet.
  Datagrams wait for the serial port in a queue of at most 64 KiB: what a sender faster
  than the line sends beyond that is dropped, counted and logged.
- **Protocols:** `raw`, `telnet` (8-bit clean), `rfc2217` (remote live changes to
  baud/parity/data bits/stop bits/flow control), `modbus` (RTU↔TCP gateway). An RFC 2217
  request the port can't carry out (a malformed or reserved value, or one the serial port
  refuses) is answered with the current setting, as RFC 2217 says, and leaves the port as
  it was; the client stays connected. An RFC 2217 client also stays connected while a lost
  serial port is reopened, and the reopened port gets its settings; optionally the port
  returns to the mapping's settings when the client leaves (see
  [`docs/VIRTUAL-COM.md`](docs/VIRTUAL-COM.md)). A telnet or RFC 2217 subnegotiation longer
  than 1 KiB (real ones are a few bytes) ends that client's session, and the log says why.
- **Modbus gateway:** bridges Modbus/TCP masters to serial Modbus/RTU slaves —
  multiple masters, transaction serialization on the shared bus + transaction-id
  integrity, and a `0x0B` (gateway-timeout) exception when an RTU slave doesn't answer.
  If the serial port goes away (a USB adapter unplugged), masters get `0x0A` (gateway path
  unavailable) at once meanwhile and the port is reopened like any mapping's (state
  `reconnecting`, device lost/back notifications). Masters are admitted like other
  clients (allowed IPs, max connections, priority IPs, kick-old-user); a **read-only**
  gateway answers every request that is not a read (writes such as FC5/6/15/16/22/23,
  diagnostics that restart or silence a slave, unknown function codes) with exception
  `0x01` without putting it on the bus. Banner, open/close strings and `closeon` don't
  apply (they would break the Modbus framing).
  **Edge mode:** also polls coils and registers on an interval and publishes the decoded
  values to MQTT (bool, uint/int/float 16/32, scaling); `"writable": true` points take
  values from MQTT (`<topic>/<point>/set`, one value for that point only: FC5 for a coil,
  FC6/FC16 for a holding register's own one or two registers; a JSON list or other text
  gets `error: …` on `.../set/result`). **Read-only** refuses these writes too (`error:
  mapping is read-only`; a refused DCMD is logged), and the dashboard marks a mapping that
  takes MQTT writes with **CMD** (see Security: the broker decides who may send them).
  Optionally as a **Sparkplug B** edge node with one device per Modbus unit (`unit1`, …):
  NBIRTH/NDEATH with `bdSeq`, DBIRTH/DDATA with `seq`, writes via DCMD, rebirth via NCMD.
  A command is written once, when it is published: a retained `/set` that the broker sends
  again after a reconnect, a restart or a configuration save is ignored (and logged)
  unless **Re-apply a retained /set after every connect** is ticked; a retained Sparkplug
  NCMD/DCMD is always ignored, an empty message (clearing a retained one) is not a
  command, and a command published while ser2net is not connected is not delivered later.
- **Client-side virtual COM:** recipes to surface a remote port as a local `COMx`/`tty`
  (pySerial `rfc2217://`, Linux `socat`, Windows com0com+com2tcp / HW VSP3) — see
  [`docs/VIRTUAL-COM.md`](docs/VIRTUAL-COM.md).
- **Full serial config:** baud (incl. custom), data/stop bits, parity, flow control
  (none/RTS-CTS/XON-XOFF/DSR-DTR), RTS/DTR on open, exclusive open, and RS-485 hardware
  auto-RTS (Linux `TIOCSRS485`: enable + pre/post-TX delays + RTS-during-TX level).
- **Live port list:** privilege-free polling + optional event hotplug (Linux pyudev /
  Windows WM_DEVICECHANGE), falling back to polling. On Linux the kernel's placeholder
  `ttyS` ports without hardware are hidden and the stable `/dev/serial/by-id` names are
  shown and selectable. **IP picker** from the machine's own addresses (localhost / LAN /
  0.0.0.0) or custom.
- **Find a device by its USB identity:** tick the box in the mapping form and the adapter
  is bound by VID:PID (+ serial number), so re-plugging it as `ttyUSB1` or under another
  COM number still opens the right device.
- **Dozens of mappings:** add/edit/delete/start/stop from one screen, with live status;
  search, state filter, sorting and start/stop/restart of the selected mappings. The
  table only redraws when it changed (no flicker, selections stay).
- **Per-mapping access control:** allowed IP/CIDR list, **high-priority** client IPs
  (a priority client evicts the oldest when the port is full), max connections,
  kick-old-user, read-only, banner, open/close strings, `closeon` (a Modbus gateway uses
  no banner, open/close strings or `closeon`).
- **Connection lifecycle (per mapping, TCP):** **dead-peer detection via TCP keepalive**
  — a client or remote server that vanishes without a FIN/RST (power cut, cable pull,
  Wi-Fi/VPN/NAT drop) no longer stays `ESTABLISHED` forever: with the defaults (on ·
  60 s idle · 10 s interval · 3 probes) it is dropped within ~90 s and its slot freed —
  also while the device keeps sending (Linux `TCP_USER_TIMEOUT`). Plus a **connect
  timeout** for connect-out (default 10 s), an **idle timeout** and a **max connection
  time** (e.g. 24 h); every disconnect is logged with its reason.
- **Observability:** per-mapping traffic trace (hex/timestamp, rotated files, downloadable
  from the UI), Prometheus `/metrics`, an audit log page, live log viewer, an in-browser
  **serial console** (xterm.js over WebSocket — watch traffic or type to the device) and a
  timestamped **live hex view** of both directions. The dashboard's **In / Out** counters
  (also `bytes_in` / `bytes_out` in `/api/v1/status`, `ser2net_mapping_bytes_in_total` /
  `ser2net_mapping_bytes_out_total` in `/metrics`) are the bytes read from and written to
  the serial device, counted once however many clients share it (serial↔serial: A → B /
  B → A). Data from the network that could not be written to the device (its port was not
  open, or a UDP datagram found the queue full) is counted as dropped (`bytes_dropped`,
  `ser2net_mapping_bytes_dropped_total`), flagged on the dashboard and logged.
- **Notifications:** a webhook (JSON, Slack, Microsoft Teams, Discord, Telegram) and/or
  e-mail (SMTP) when a device is lost or comes back, a mapping fails to start, clients
  come and go, or a sign-in fails; repeats are suppressed, a test message can be sent.
- **Security:** password (set on first access, scrypt), CSRF, signed-cookie sessions,
  login rate-limiting, strict security headers; sessions end at logout, on a password
  change, per user or for everyone (Settings), and a lost password is reset with
  `ser2net --set-password` (see Security).
  For trusted networks the login requirement can be switched off under **Settings → Web
  UI access** (visitors then use the UI with a chosen role; see Security).
- **Multi-user / RBAC:** `admin` / `operator` / `viewer` roles — viewer is read-only and
  watches what the devices send; operator manages mappings and also sees what is sent to
  the devices (hex view, trace files: where a password typed at a device prompt ends up);
  admin does everything plus user management. Existing single-password setups migrate
  automatically to one `admin` user.
- **LDAP / Active Directory auth (optional):** users authenticate by binding to the
  directory; LDAP group membership maps to a role. Works alongside local accounts;
  needs `ldap3`. LDAPS / StartTLS check the server's certificate (the system's CAs or
  your own CA file).
- **OIDC single sign-on (optional):** authorization-code flow with PKCE, with Keycloak /
  Entra ID / Google / Okta; the id_token is validated (signature/iss/aud/exp/nonce) and a
  claim maps to a role. Needs `authlib`.
- **REST API:** a JSON API (`/api/v1`) for automation — mapping CRUD, start/stop/restart,
  status and ports; **bearer-token** auth (with a role — `viewer` is read-only); OpenAPI
  3.0 (`/api/v1/openapi.json`). The token is generated in Settings.
- **LAN cluster (fleet management):** nodes on the same network **auto-discover** each
  other (signed UDP broadcast — no mDNS) and one node shows **every node's mappings in a
  single table**, each row tagged with its host (name + IP) and each node's **health**
  (uptime · version · running/total). Operators can **remotely start/stop/restart and edit**
  a peer's mappings from that view. For routed/L3 networks broadcast can't reach, add
  **manual peers** (host:port). Trust = the **same shared key** on every node (any
  characters, Turkish letters too; it is Unicode-normalised (NFKC) before use, so the same
  key typed or pasted elsewhere matches); off by default, enable under Settings → LAN cluster.
  Requests between nodes are **signed** with the key instead of carrying it (only nodes
  running 2.8.0 or older still get the key itself; see Security → LAN cluster). Peers are
  always contacted directly, never through an HTTP proxy (`HTTP(S)_PROXY` / `ALL_PROXY`
  apply to webhooks and SSO only).
- **Deployment:** official **Docker** image + `docker-compose`; **systemd** unit;
  Linux+Windows × Python 3.10–3.13 **CI** (GitHub Actions).
- **MQTT publishing (optional):** publish each mapping's serial lines to an MQTT
  broker (host/port/topic/QoS/TLS/credentials) with a retained birth/death status.
  Needs `paho-mqtt` (optional; the bridge runs normally without it).
- **Migrating from classic ser2net:** `ser2net.yaml` (4.x) and `ser2net.conf` (3.x) files
  import through Settings with a preview; options without an equivalent are listed as
  warnings, and a connection that would be more open here (a gtlssh login or TLS client
  certificates, TLS whose certificate is not on this host, a write-only device) comes in
  disabled.
- **Turkish / English UI:** pick **TR / EN** in the top bar (remembered per browser);
  otherwise the browser's language or the admin's default (Settings → Web UI access), which
  notifications use as well. Logs, the audit log and the REST API stay English.
- **Fully offline:** all dependencies bundled as wheels; no internet required.

---

## 🧰 Requirements

- **Python 3.10+** installed. Nothing else — dependencies ship in `vendor/wheels/` and
  install to `./lib` on first run (offline).
- Linux: membership in the `dialout` group to **open** serial ports (listing ports
  needs no privilege):
  ```bash
  sudo usermod -aG dialout "$USER"   # then log out/in
  ```
- Windows: no extra privileges for COM ports.
- Optional: `openssl` (self-signed TLS); `pyudev` (Linux) / `pywin32` (Windows) for
  event-driven hotplug — polling is used otherwise.

---

## 🚀 Install & run

```bash
# Linux / macOS
python3 ser2net.py            # or: ./start.sh

# Windows
start.bat
```

### Standalone binary (nothing else to install)
Every release attaches a Windows `.exe` and a Linux binary to **GitHub Releases**. Each
is one file with everything inside — Python, OpenSSL, the VC++ runtime (Windows), MQTT /
LDAP / OIDC support — so no Python, `openssl` or other package is needed. Download and
run; config/logs are written to `data/` next to the binary.

| File | Runs on |
|---|---|
| `ser2net-windows-x64.exe` | Windows 10/11, Windows Server 2016 or newer (x64) |
| `ser2net-linux-x64` | x86_64 Linux with glibc ≥ 2.28: RHEL / AlmaLinux / Rocky 8+, Debian 11+, Ubuntu 20.04+, Fedora, openSUSE Leap 15 (not musl/Alpine — use Docker there) |
| `ser2net-linux-arm64` | 64-bit ARM Linux with glibc ≥ 2.28: Raspberry Pi 3/4/5 (64-bit Raspberry Pi OS 11+ or Ubuntu), ARM industrial gateways, AWS Graviton, the same distributions |

Every release also publishes `SHA256SUMS`, a CycloneDX SBOM per binary (`*.cdx.json`) and
the license texts, and the binaries carry GitHub build provenance:
`sha256sum -c SHA256SUMS --ignore-missing`,
`gh attestation verify ser2net-linux-x64 -R haliskilic/ser2net`. The Windows exe is **not
code-signed yet** (it will be once a signing service is configured —
[`docs/SIGNING.md`](docs/SIGNING.md)), so SmartScreen may warn on first run; verify it on
Windows with `Get-FileHash` against `SHA256SUMS` and `gh attestation verify`, then *More
info → Run anyway* ([`docs/WINDOWS.md`](docs/WINDOWS.md)).

Every pull request and release proves it (`tests/test_standalone.py`): the bundle may
need nothing but OS-base libraries (glibc ≤ 2.28 / Windows system DLLs; Linux on x86_64
and arm64), the binary must
work with an empty `PATH` (no Python, no `openssl`), and the Linux one must run in clean
AlmaLinux 8/9, Debian 11/12, Ubuntu 20.04/22.04/24.04, openSUSE Leap 15.6 and Fedora
containers.

Linux notes: `chmod +x ser2net-linux-x64`; serial ports usually need the `dialout`
group. The binary unpacks itself to `$TMPDIR` (default `/tmp`) on start — if `/tmp` is
mounted `noexec`, point `TMPDIR` at a directory that isn't. While it runs it keeps that
copy locked and its access times fresh, so tmp cleaners (systemd-tmpfiles, tmpreaper)
don't delete it under a long-running instance; a copy left behind by a killed process
still ages out as usual. For certificate checks (MQTT over TLS, OIDC, LDAPS / StartTLS) it
uses the distribution's CA bundle, or `SSL_CERT_FILE` if set.

`ser2net --about` shows the versions (Python, OpenSSL, components) — add it to bug
reports. `ser2net --licenses` prints the license text of every component inside the
executable (releases also attach it as `*.THIRD-PARTY-LICENSES.txt`).

Build it yourself — Windows: `pip install -r requirements.txt -r requirements-optional.txt pyinstaller && pyinstaller ser2net.spec`;
Linux (in an old-glibc container, so the result runs on older distributions too):
`docker run --rm -v "$PWD:/src" -w /src almalinux:8 bash tools/build_standalone_linux.sh`.

On first run:
1. The **console** asks which local IP the admin UI binds to (machine IPs or custom)
   and a port (default 8080). Headless/service default is the safe **127.0.0.1**.
2. Open the printed URL; **set the admin password** on the first screen. Open it by the
   machine's IP address or `localhost`: reached through another host name (a reverse
   proxy, the Docker host's name) the setup page also asks for the one-time code in
   `data/setup-code.txt`, so that no other web page can claim a new install (DNS
   rebinding). No browser at hand: `ser2net --set-password admin` (see Security).
3. From the dashboard, **+ Add mapping**: pick a COM/tty and map it to an IP:port.

Change the bind IP later:
```bash
python3 ser2net.py --reconfigure
```

### Offline install (no-internet machine)
Dependencies live in `vendor/wheels/`; `ser2net.py` installs them to `./lib` on first
launch (`pip install --no-index`). No internet needed. The set covers Python 3.10–3.14 on
Linux x86_64 and Windows x64; after a `requirements.txt` change, refresh it with
`python3 tools/refresh_wheels.py` (`tests/test_vendor_wheels.py` checks it).
To enable the optional features (MQTT/LDAP/OIDC) on an offline source install, download
their wheels too: `pip download -r requirements-optional.txt -d vendor/wheels`, then
`pip install --no-index --find-links vendor/wheels -r requirements-optional.txt`. (The
standalone binary already bundles them.)

### Docker
```bash
docker compose up -d        # edit `devices:` and `ports:` in docker-compose.yml first
```
Without compose, use the `docker run` command in
[`docs/DOCKER.md`](docs/DOCKER.md#plain-docker-run) (the published image,
`--restart unless-stopped`, the mapping ports, a `/dev/serial/by-id` device, log limits).
Published image (amd64 + arm64, every release): `docker pull ghcr.io/haliskilic/ser2net:latest`
(or `:2.8`, `:2.8.1`); `docker-compose.yml` uses it.
Inside the container the UI binds to `0.0.0.0` (set via `SER2NET_BIND_IP` /
`SER2NET_PORT` — the interactive picker can't run headless). Publish each mapping's port
too (a new mapping gets 4001), or use the host network. A serial device's group is taken
from the device itself, so `--group-add` isn't needed for the usual device names. The
process runs as the
`ser2net` user (uid 10001), not root; a `/data` volume written by an older (root-running)
image is taken over on the first start, and in a bind-mounted folder the files an older
image wrote as root are handed to the folder's owner, so upgrading needs no manual step.
The image has a `HEALTHCHECK` (`ser2net --healthcheck`). Details:
[`docs/DOCKER.md`](docs/DOCKER.md).

---

## 🖥️ Usage

- **Add mapping:** name, type (Serial↔Network / Serial↔Serial), serial port +
  parameters, network mode (server/client/udp), protocol, bind/remote IP:port, access
  rules.
- **Start/Stop/Restart/Copy/Delete:** per row.
- **Log:** the mapping's history (newest first, persists across restarts).
- **Monitor:** live serial terminal in the browser (xterm.js); operators type to the
  device on network mappings. The hex view shows both directions; viewers see only what
  the device sends.
- **Trace** (operators): the mapping's trace files, to list and download.
- **Settings:** change password, **web UI access** (login required / open access with a
  role), admin TLS (provide paths or generate self-signed), generate/revoke the **REST API
  token**, mappings **backup/restore** (JSON), status.
- **/metrics:** Prometheus-format metrics (authenticated): per mapping whether it runs,
  its clients, bytes in / out / dropped, reconnects and dropped slow clients; for the
  process its open files and their limit (`ser2net_open_fds`, `ser2net_fd_limit`, POSIX),
  the admin UI's connections, TLS handshakes in progress and waiting for one to end, and
  the connections refused right after accept (`ser2net_connections_refused_total`, by
  reason).

---

## 🔌 REST API

Alongside the browser UI, a JSON API at `/api/v1` for automation. Authenticate with
`Authorization: Bearer <token>`; generate the token under **Settings → REST API token**
(shown only once). Full description: `GET /api/v1/openapi.json`.

```bash
TOKEN="s2n_..."   # generated in Settings
# all mappings (config + live status)
curl -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings
# create a mapping
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"PLC-1","kind":"net","serial":{"port":"/dev/ttyUSB0","baudrate":9600},
       "network":{"mode":"server","bind_ip":"0.0.0.0","port":4001}}' \
  http://HOST:8080/api/v1/mappings
# start / stop / restart
curl -X POST -H "Authorization: Bearer $TOKEN" http://HOST:8080/api/v1/mappings/<id>/stop
```
Endpoints: `GET/POST /mappings`, `GET/PUT/DELETE /mappings/{id}`,
`POST /mappings/{id}/{start|stop|restart}`, `GET /status`, `GET /ports`,
`GET /health` (unauthenticated), `GET /openapi.json`.
The token has the role chosen when it was generated: a `viewer` token may only read (GET)
and gets stored secrets (the MQTT password) masked as `********`; a `PUT` that sends that
mask back keeps the stored password. An `api_token_role` in `config.json` that is not a
role counts as `viewer`, and the start-up log says so.

---

## 🔒 Security

The UI is **password-protected by default** and binds to **127.0.0.1** by default.
Exposing it to the network requires an explicit IP choice at startup and warns when
bound without TLS. For LAN deployments, use TLS (`admin_ui.tls_*`) and per-mapping
`allowed_client_ips`. Raw TCP is plaintext — be careful on untrusted networks. A bare
`0.0.0.0`/`::` in an allow/priority list means "any client". A mapping's serial port is a
device path or COM name only; pyserial URL handlers (`socket://`, `rfc2217://`, `spy://`,
`loop://`, ...) are not accepted as a port.

**Connection limits:** on a TLS mapping, a host not in `allowed_client_ips` is closed before
the TLS handshake; a handshake must be over within 15 s, and at most 32 run at once from one
address (128 in all). More connections wait up to 15 s for one of those to end (at most 64
from one address, 256 in all), so a client's burst of reconnects is served rather than
refused. Capacity, `kick_old_user` and the "Busy" line still come after the handshake. The admin UI closes a connection that sends no complete request head within
`admin_ui.request_timeout_s` (10 s) of connecting or of its last response, whose request
body then stops coming (the body gets that time plus a second per KiB that arrives), or
whose client takes nothing of a response for that long (a console WebSocket is not
timed), and takes at most `admin_ui.max_connections` (256) connections
at once, `admin_ui.max_connections_per_ip` (64) from one address; its TLS handshake has the
same limits as a mapping's. These keys are in `config.json` only (0 = no limit); behind a
reverse proxy every user has the proxy's address, so raise the per-address limit if
needed. When the process gets close to its open-file limit, new connections (to any
mapping or the UI) are refused with a log line, so serial ports, logs and the
configuration keep the files they need.

**UDP mappings:** `allowed_client_ips` is checked for every datagram, but what it checks is
the datagram's source address, which a sender on the network can forge, so for UDP it is no
authentication. Without a reply address, any allowed sender (or one forging an allowed
address) takes the device's output over with a single datagram, even on a read-only
mapping; the log notes each change of sender (at most once a minute). Set a **reply
address** to keep the output on the right host. On untrusted networks use a TCP mapping with
TLS, or a firewall.

**MQTT commands (Modbus edge mode):** writable points take values from any client the
broker lets publish to `<topic>/<point>/set` or, for Sparkplug B, to
`spBv1.0/<group>/NCMD/<node>` and `spBv1.0/<group>/DCMD/<node>/<device>`. ser2net does not
authenticate these clients itself, and `allowed_client_ips` covers only the Modbus/TCP port,
so the broker's access rules (ACLs) are the authorization: let only the clients that must
switch outputs publish to the command topics, and connect to the broker with TLS and a
username/password. **Read-only** refuses every write. The dashboard marks a mapping that
takes MQTT writes with **CMD**, and with **CMD ⚠** (plus a warning in its log at start)
when its broker connection has no TLS or no username. A Mosquitto `acl_file` for the base
topic `plant/line3`, for example:
```
user ser2net
topic readwrite plant/line3/#
user scada
topic write plant/line3/+/set
topic read plant/line3/#
user historian
topic read plant/line3/#
```

**Open access (optional):** for trusted networks only, an admin can untick "Require
login" under **Settings → Web UI access** and give visitors a role (`viewer` read-only /
`operator` / `admin`). Signing in still works (you get your own role), RBAC and CSRF
apply unchanged, and the **REST API always needs its token**. Open access is honoured
only when the UI is reached by **IP address, `localhost` or this machine's own name** —
requests via any other host name (e.g. a malicious page doing DNS rebinding) are sent to
the login page. Behind a reverse proxy, the proxy must pass the original name in
`X-Forwarded-Host` (otherwise keep login on). **Other LAN-cluster nodes** are only shown
and managed after signing in, and open consoles are closed when access is revoked or the
role changes. While it is on, the UI shows a warning and startup logs it.

**Requests from other sites:** a change (any POST, PUT, PATCH or DELETE of the web UI) is
refused with 403 when the browser says a page of another site sent it: `Sec-Fetch-Site`
other than `same-origin`, or an `Origin` that isn't the address the UI was reached by —
another port of the same host and a sibling subdomain count as other sites. This comes on
top of the CSRF token, which is new after every sign-in and sign-out and lasts as long as
the session. Behind a reverse proxy, the proxy must pass the address the browser uses as
`Host` or `X-Forwarded-Host`, port included (all.log names the refused `Origin` and the
address the UI saw). Scripts that send no `Origin` header, the REST API and cluster peers
are not affected.

**Passwords** of local accounts are stored as scrypt hashes (N=2^14, r=8, p=5, OWASP's
minimum; the hash names its cost, so the cost can be raised later). A hash made by 2.8.1 or
older keeps working and is renewed with today's cost at that account's next sign-in (its
sessions stay; `config.json` is saved, the previous one goes to `backups/`). Passwords are
compared as Unicode NFKC: a letter with its accent as one character or two (text pasted
from macOS or a PDF), a no-break space and a space, full-width forms are the same password.
A directory (LDAP) gets the password exactly as typed. **Going back to 2.8.1 or older:**
those versions can't read the renewed hashes, so an account that signed in with 2.9 can't
sign in there: give it a new password with `ser2net --set-password NAME` (2.8.1), or, on
2.8.0 and older, which have no such command, put back a `config.json` from `backups/` made
before the upgrade.

**Sessions:** logging out ends that session on the server (a copy of the cookie stops
working too). Under **Settings → Users** an admin can sign a user out everywhere or set a
local user's new password (which signs them out as well); everyone can *Sign out my other
sessions*. **Settings → Web UI access → Sign out everyone** makes a new session key and
ends every session at once, the admin's own excepted — use it when a copy of
`config.json` or a backup may have leaked (downloaded backups leave the key out). While
LDAP or OIDC is switched off, its accounts' sessions don't count.

**Failed sign-ins** are limited to 8 per address in 5 minutes (an IPv6 address counts by
its /64; behind a reverse proxy all clients share the proxy's address) and to 10 per user
name in 15 minutes, from any address and letter case aside; past either limit that address
or that name gets HTTP 429 until its oldest failures are older than that — nothing is locked
for good, and with LDAP the directory is asked no more often, which keeps such attempts
below its own lock-out. A wrong current password in *Change your password* counts under the
account's name too, and is logged and recorded in the audit log (`password_change_failed`).
Password checks run two at a time and directory sign-ins four; while many wait already, a
sign-in gets HTTP 429 (*Too many sign-ins at the same time*) without being tried. A name
without a local account takes as long to refuse as a wrong password. all.log has a line for
each failed sign-in, and the audit log records them; a user name that is no account's is
recorded as `(unknown user)`, never as typed (it may be a password typed into the wrong
field). Past 20
failed sign-ins in 10 minutes the audit log only counts them and sums them up in one record
at the end of those 10 minutes, so a flood of attempts can't push the other records out.
Opening a mapping's console or hex view is recorded once per account (with open access: per
address), mapping and kind in 5 minutes, and at most 50 such records in 10 minutes, summed up
the same way (all.log keeps a line for every one).
Names over 256 characters aren't tried. Request bodies are limited too: the sign-in, setup
and sign-out forms to 64 KB, a configuration import to 16 MB and everything else (forms,
the REST API) to 2 MB; a larger one gets HTTP 413.

**LDAP / OIDC groups → roles:** a group setting (admin / operator / viewer group) names one
group and matches only its whole name, never a part of it. For LDAP that is the group's
name (its CN, e.g. `ser2net-admins`), the start of its DN (`cn=ser2net-admins,ou=groups`)
or its full DN; for OIDC the value as the provider sends it, or its last part
(`ser2net-admins` matches Keycloak's `/team/ser2net-admins` and `CORP\ser2net-admins`).
Letter case doesn't matter, Turkish İ/I/ı/i included (`Bilgi İşlem` = `BİLGİ İŞLEM`). A
setting that is only part of a group's name (`admins` for `ser2net-admins`, or just an OU of
its DN) gives no role: all.log names the setting and the group in a WARNING at that user's
sign-in, and Settings shows it until you save that section. If your only admins came from
such a group, sign in with a local admin created by `ser2net --set-password NAME` (see
below) and correct the setting. When a directory user's groups give no role any more,
their next sign-in also ends the sessions they still had. AD direct bind with
`{username}@corp.local` or `CORP\{username}` finds the account by userPrincipalName /
sAMAccountName under the user search base (blank: its whole domain) to read its groups;
*Also count nested groups* (AD only) adds the groups a user is in through other groups.

**LDAP / OIDC accounts:** someone who signs in through the directory (LDAP) or the identity
provider (OIDC) for the first time gets an account here without a password; its role is
worked out from their groups at every sign-in — also when that takes the last admin role
away (all.log then warns; `ser2net --set-password NAME` makes a local admin). Local accounts
come first: an LDAP/OIDC sign-in under the name of a local account (letter case aside) is
refused and written to all.log and the audit log (`ldap_login_denied` /
`oidc_login_denied`); it never takes over the local account, turns it into an SSO account or
demotes it. To move someone from a local account to SSO, delete the local account first
(another admin must remain). The same holds between LDAP and OIDC: a sign-in under the name
of an account of the other method is refused (the two may be different people); delete that
account to move the person. Like directories, ser2net ignores letter case in LDAP names
(Turkish İ/ı too): `hkilic`, `Hkilic` and `HKILIC` are one account; the accounts earlier
versions made for each spelling merge at that person's next sign-in (the other spellings'
sessions end), and *Sign out* and *Delete* cover all of them. OIDC names are compared
exactly. A new local account's name can't differ from an existing one only in letter case.
An LDAP/OIDC name can be any printable text (`CORP\jdoe`, `jdoe@corp.local`, names with `/`
or `#`; such accounts can be signed out and deleted in the Users panel too); a sign-in under
a name with control or invisible characters, longer than 255 characters or in parentheses is
refused (all.log says why).

**Unused LDAP / OIDC accounts:** **Settings → Users** shows each account's last sign-in
(from the audit log and the sign-ins since; not written to config.json). Removing unused
LDAP/OIDC accounts is **off by default**: an account that stops signing in stays until you
delete it. To turn it on, set *Remove LDAP / OIDC accounts not signed in for (days)* in the
same place (`external_users_expire_days`: 30–3650 days; 0 = off): LDAP/OIDC accounts not
signed in for that long are then removed at once, at start-up and every hour, each written
to all.log and the audit log (`user_expired`). Admins, local accounts and the last remaining
accounts are never removed, and an account without any sign-in in the audit log only when
the log goes back that far for its kind of sign-in. Deleting (or removing) an LDAP/OIDC
account ends its sessions at once, but access is up to the directory: while a mapped group
or the default role still applies, the person's next sign-in makes a new account.

**OIDC (SSO):** the code request carries PKCE (S256). Every address the provider's discovery
document gives (authorization, token, JWKS) and every redirect on the way must be https://
(http:// only to this machine's loopback address): if a Keycloak behind a TLS-terminating
proxy announces http:// addresses, SSO sign-in is refused and all.log says why (fix
Keycloak's proxy / frontend URL settings), because the client secret, the code and the
signing keys would travel in clear text. When the provider sends the user back with an error
instead of a code (e.g. the user is not assigned to the application: `access_denied`), the
error and its description go to all.log and the user sees a message for that error code;
the token endpoint's error answers are logged too. An e-mail address the provider calls
unverified (`email_verified: false`) is never used as the user name. If the discovery
document names another issuer than the configured one, all.log warns once. The id_token's
times (iat / nbf / exp) are accepted with up to 2 minutes of clock difference: keep the
clocks of this server and of the provider synchronised (NTP); when a token is refused for its
times, all.log says how many seconds this server's clock is behind or ahead of the
provider's.

**LDAP over TLS (LDAPS / StartTLS):** the directory server's certificate is checked. It
must come from a CA the system trusts (Linux / Docker: its CA bundle; Windows: its
certificate store, where a domain member usually has the AD CA already) and name the host
in the server URI: use the name the certificate was issued for (`ldaps://dc01.corp.local`;
an IP address only if the certificate lists it). For your own CA (Active Directory
Certificate Services) or a self-signed certificate, set **Settings → LDAP → CA certificate
file** to that CA's certificate, PEM or DER as Windows exports it (Docker: put it in the
`/data` volume); then only the certificates in that file are trusted. A refused certificate
is logged in all.log with the reason and what to set, and Settings shows it; nothing is
sent over that connection. *Don't check the server certificate* turns the check off, which
is insecure — anyone on the network between ser2net and the directory could read the
passwords and sign in as anyone; Settings and the start-up log warn while it is off, and
while passwords go unencrypted (`ldap://` without StartTLS). Over TLS ser2net doesn't
follow a referral to another server (that server's certificate would go unchecked): point
the URI at a server that holds the users and their groups. **After upgrading from 2.8 or
older**, LDAP sign-in stops if the certificate isn't trusted: set the CA certificate file
(local accounts keep working; without a local admin, create one with
`ser2net --set-password NAME`).

**Lost password, locked out, no browser (account recovery):**
`ser2net --set-password NAME` sets a new password for the local account `NAME` (asked
twice on the terminal; piped, the first line of stdin) and signs that account out
everywhere. A name that doesn't exist yet is created as an **admin** — the way back in
when the only admin is an LDAP/OIDC account and the directory is unreachable.
`ser2net --sign-out-all` ends every web UI session. Both work with ser2net stopped or
running: a running instance applies the change at once, no restart needed. Nothing else
in the configuration changes, the previous `config.json` stays in `backups/`, the audit
log records it, and nothing secret is printed or logged. Use the same `--data-dir` as the
service and run it as the account ser2net runs as (as root, it switches to the data
directory's owner by itself):

| Install | Command |
|---|---|
| source / Linux binary (systemd) | `sudo -u ser2net python3 /opt/ser2net/ser2net.py --data-dir /opt/ser2net/data --set-password admin` (binary: `./ser2net-linux-x64 --set-password admin`) |
| Windows (elevated prompt, also while the service runs) | `ser2net-windows-x64.exe --data-dir C:\ProgramData\ser2net --set-password admin` (the service's data directory: `sc qc ser2net` shows it) |
| Docker | `docker exec -it ser2net python /app/ser2net.py --no-bootstrap --data-dir /data --set-password admin` |

**First admin without the browser:** run `ser2net --set-password admin` before the first
start (it creates `config.json`; the UI then listens on `127.0.0.1:8080` unless
`--reconfigure` or `SER2NET_BIND_IP` says otherwise), or — Docker — point
`SER2NET_ADMIN_PASSWORD_FILE` at a file (a Docker secret) holding the password, with
`SER2NET_ADMIN_USER` for the name (default `admin`). The variable is used only while there
is no account at all; later starts ignore it.

### LAN cluster

- **Signed requests:** every request between nodes is signed with (a key derived from)
  the cluster key — method, path, body, both nodes' ids, a time and a one-time nonce —
  and so is every reply. Between such nodes the key itself is never sent, a captured
  request can't be sent again (to that node or another), and a reply can't be altered. The nodes' clocks need not
  agree: a node learns each peer's clock from its signed beacons and replies. Refused
  requests are logged once per address and change (`cluster: refused …`) and recorded in the
  audit log (`cluster_auth_failed`).
- **Older nodes (legacy authentication):** nodes running 2.8.0 or older only know the raw
  key, sent in every request. While *Legacy authentication* is ticked (Settings → LAN
  cluster; on by default, so a rolling upgrade doesn't split the fleet) a node accepts it and
  uses it with exactly those nodes: ones whose beacon says they are older (at the address the
  beacon advertises), and manual peers that refuse a signed request the way older releases do
  (never one that has answered a signed request). An older node that advertises no LAN
  address (no default route: its beacon says 127.0.0.1) is only managed from other nodes once
  it is added there as a manual peer; the fleet view says so. The fleet view tags these nodes
  **unsigned key** and lists them in a banner. **Once every node is upgraded, untick it on
  every node.**
- **The key:** anyone on the network can try to guess a weak key offline from the beacons,
  and the key lets a node start, stop and change the mappings of every other node: use at
  least 16 random characters (*Generate a new key and save* makes one, shown once). The
  Settings page never shows the stored key; a blank field keeps it.
- **Changing the key:** *planned* — on each node enter the new key (or generate it on one and
  copy it) with *keep accepting the current one for 7 days* ticked; nodes not changed yet keep
  working, and the fleet view tags them **previous key**. When every node has it, tick *Stop
  accepting the previous key* on each. *Leaked* — untick *keep accepting*, so the old key is
  refused at once, and change it on every node; until then the fleet view shows **cluster key
  rejected** for the nodes not changed yet, and a banner lists the nodes that broadcast with a
  different key. *Auto-discovery* can be switched off where only manual peers are used.

---

## ⚙️ Run as a service

- **Docker:** official `Dockerfile` + `docker-compose.yml` (see above and
  [`docs/DOCKER.md`](docs/DOCKER.md)) — `restart: unless-stopped`, `/data` volume. A plain
  `docker run` needs `--restart unless-stopped` for that (an existing container:
  `docker update --restart unless-stopped ser2net`).
- **Linux (systemd):** `systemd/ser2net.service` — a dedicated unprivileged user,
  `SupplementaryGroups=dialout`, `Restart=on-failure`, hardening directives.
  **Do not run as root.**
- **Windows:** nothing else needed. From an elevated ("Run as administrator") prompt:
  ```bat
  ser2net-windows-x64.exe --install-service
  ```
  The exe copies itself to `C:\Program Files\ser2net\` (admin-only, so a standard user
  can't replace the file the service runs as SYSTEM) and keeps its data in
  `C:\ProgramData\ser2net\` (both writable by SYSTEM and Administrators only). The
  service starts at boot, needs no signed-in user
  (LocalSystem) and restarts 5 s after it crashes or exits with an error; the console asks
  once for the UI address. Remove it with `--uninstall-service` (the data folder is kept);
  run several instances via `--service-name`. Stop/start: `sc stop ser2net` /
  `sc start ser2net` or *Services* (services.msc). Re-running `--install-service` from a
  newer exe upgrades it in place and keeps the data directory the service uses (a 2.8.0
  service too). **[`docs/WINDOWS.md`](docs/WINDOWS.md)** covers the
  firewall rule you need to reach it from other hosts, changing the address, upgrading and
  the SmartScreen warning on unsigned builds.
- **Many mappings:** a running mapping keeps about three files open (its serial port, its
  listener and its log), plus one per connected client. At start ser2net raises its soft
  open-file limit toward the hard limit (at most 65536) and logs the result (`open files:
  …`); the systemd unit sets `LimitNOFILE=65536`, and a low hard limit in Docker is raised
  with `--ulimit nofile=65536`. The start-up log warns when the configured mappings need
  close to the limit.
- **Many connections on Windows:** the event loop ser2net uses there (`select()`) watches at
  most 512 sockets (listeners, clients, the UI's connections). Near that, new connections
  are refused with a log line, and the service keeps running. The environment variable
  `SER2NET_WINDOWS_LOOP=proactor` (for the service: a system environment variable) switches
  to the proactor loop, which has no such limit; it is new, so test serial timing (e.g.
  Modbus RTU) with it before relying on it. The start-up log names the loop in use.

---

## 🗂️ Configuration & state files

All state lives in the **data dir** (default `data/`). The data directory is locked to
owner-only (POSIX `0700`; Windows `icacls` owner/SYSTEM/Administrators), so the files
inside aren't readable by other users:
- `config.json` — admin IP, password hashes, all mappings (atomic write), and the secrets
  ser2net has to use itself, in plain text (see *Secrets* below).
- `backups/` — the previous `config.json` before every change (newest 50). Download or
  restore one under Settings → *Configuration backups*. A restore first shows what changes
  for sign-in and access, then brings back the mappings and settings; the user accounts
  and passwords, the REST API token and the cluster key stay as they are unless you tick
  the box to restore them too (that also signs everyone else out). *Only its mappings*
  goes through the import preview. A restore can be undone too; a downloaded backup
  leaves out the session key.
- `all.log` — global activity (size-rotated); `audit.log` — sign-ins, config changes,
  console / hex view opens and trace downloads: who/from where/what (rotated). Searchable
  under Settings → *Audit log*, CSV export.
- `logs/<id>.log` — per-mapping history (hourly maintenance: trimmed >15 days / >100 MB).
- `traces/<id>-<name>` — a mapping's traffic trace. Traces are kept **only here** and
  rotate at their size limit (default 10 MB × 3 old files). Full paths from v2.7 configs
  are reduced to the file name, which is logged at start-up.
- `tls/` — certificate/key if self-signed is generated.
- `setup-code.txt` — the one-time code for first-run setup through a host name; it exists
  only until the first admin account does.

**Secrets:** `config.json` holds, in plain text, the LDAP bind password, the OIDC client
secret, the SMTP password, the webhook URL (which often carries a token), each mapping's
MQTT broker password, the cluster key (enough to read and change the mappings of every
node) and the session key `secret_key` (with the accounts in the same file, enough to make
a valid sign-in). Local passwords are kept as scrypt hashes and the REST API token only as a
hash. `backups/` holds earlier copies of the same file (a downloaded backup leaves out the
session key), and the mappings export holds the MQTT passwords. On this machine only the
account ser2net runs as can read them; treat every copy — a copied data folder or Docker
volume, a disk or VM image, a downloaded backup, an export — like a password vault.
[SECURITY.md](SECURITY.md) (*Stored secrets*) lists what to change after such a copy leaked.

Mapping ids (the `<id>` above, also used by the REST API) are 1–64 letters, digits or `_`;
new mappings get 12 hex digits. An id in any other form (hand-edited or imported) is
replaced when the config is loaded or the file is imported; the start-up log or the import
preview says so.

Deleting `config.json` + `all.log` fully resets the system (orphan logs auto-pruned;
`backups/` is never restored by itself — delete it too if you want it gone). To get back in
after a lost password, use `--set-password` instead (see Security): it keeps everything else.
`admin-requests/` only holds a command-line request until the running instance takes it.

**Copying an installation** (a disk or VM image, a copied data folder or Docker volume):
`config.json` holds the node's identity — its cluster id (`instance_id`) and the key that
signs sign-ins (`secret_key`). Copies would hide each other in the cluster, and a sign-in to
one would be valid on the other. Delete both entries from `config.json` before taking the
image (new ones are made at the next start). Copies that already run are reported in the log,
the fleet view and Settings → LAN cluster; there, *Give this node a new identity* gives one of
them a new id and session key (everyone signed in to it is signed out).

**Session lifetime:** Settings → *Web UI access* → *Session lifetime* (default 8 hours).
**Import:** shows a preview first; choose *merge* (update mappings with the same name, add
the new ones) or *replace all*.

---

## 🧪 Testing

Unified test runner — the portable suite (no hardware, no socat) runs on every OS:
```bash
python3 tests/run_all.py            # portable suite (Windows + Linux)
python3 tests/run_all.py --socat    # + socat-based data-path tests (Linux)
python3 tests/run_all.py -v         # also show the output of passing tests
```
The socat tests use virtual serial ports (Linux); no hardware needed. Individual
files still run directly, e.g. `python3 tests/test_rest_api.py`. On every pull request
CI (GitHub Actions) runs ruff, mypy, `pip-audit`, the offline-wheel check, the full
matrix (ubuntu/windows × Python 3.10–3.13) and coverage (`python3 tests/coverage_run.py`,
which also measures the servers the tests start). Development setup and rules:
[CONTRIBUTING.md](CONTRIBUTING.md).

`tests/test_dead_peer.py` also exercises dead-peer detection against **real half-open
TCP connections**: with `SER2NET_BLACKHOLE_TESTS=1` it drops the packets of a loopback
flow (Linux: root/passwordless sudo + `iptables`; Windows: Administrator +
`pip install pydivert`). Since that edits the firewall it is opt-in locally (on Linux,
best run inside a throwaway network namespace); CI always runs it on both OSes.

`tests/test_ui_layout.py` opens the web UI in a real browser (Playwright + Chromium)
with very long mapping names / device paths at desktop-to-phone widths and fails if
anything sticks out of its panel (`pip install playwright && python -m playwright
install chromium`; CI job `ui`). `tests/test_standalone.py` checks a built executable
(`SER2NET_STANDALONE_BIN=dist/ser2net`; CI workflow `standalone.yml`, also used by
every release).

---

## 📜 License

ser2net is **dual-licensed**:

- **Open source — [GNU AGPL-3.0](LICENSE):** free to use, study, modify and share. If you distribute it, or run a modified version as a network service, you must make your complete source code available under the AGPL-3.0.
- **Commercial license:** for closed-source products, devices or SaaS without the AGPL-3.0 obligations — see [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) or email **mail@haliskilic.com.tr**.

Bundled third-party components keep their own licenses — see [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md);
the full texts inside an executable: `ser2net --licenses`. The LGPL-licensed ldap3 stays
in the executable as plain source files; `SER2NET_LIB_OVERRIDE=<dir>` loads another
version of it. Reporting a vulnerability: [SECURITY.md](SECURITY.md).

Contributions are welcome! On your first pull request you'll be asked to sign a one-time [Contributor License Agreement](https://gist.github.com/haliskilic/7a7fdcd355f54d1db6943959d7b76823); you keep the copyright to your work.

Copyright © 2026 Halis KILIÇ.

## ❤️ Support the project

If ser2net is useful to you, please consider **[sponsoring my work on GitHub Sponsors ❤️](https://github.com/sponsors/haliskilic)** — sponsorships pay for test hardware and the time to keep the project maintained.

No GitHub account? You can also **[buy me a coffee ☕](https://buymeacoffee.com/haliskilic)**.

Roadmap: [`ROADMAP.md`](ROADMAP.md) · Türkçe: [`README.md`](README.md)
