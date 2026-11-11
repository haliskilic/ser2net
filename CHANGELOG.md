# Changelog

All notable changes to ser2net. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Detailed notes for each version:
[GitHub Releases](https://github.com/haliskilic/ser2net/releases) and [ROADMAP.md](ROADMAP.md).

## [Unreleased]

## [2.8.1] — 2026-11-12
A security release. **Upgrading from 2.8.0 or 2.7.x:** `config.json` and `data/` are used as
they are, but several fixes change behaviour. Check what applies to you:
- **LAN cluster:** upgrade every node. *Legacy authentication* (Settings → LAN cluster, on by
  default) lets 2.8.1 nodes work with older ones meanwhile; once all run 2.8.1, turn it off on
  each node and change the key, which older versions sent in clear text. An older node that
  advertises a loopback address must be added as a manual peer.
- **LDAPS / StartTLS check the server certificate:** if this system doesn't trust the
  directory's CA (a private or AD CS CA, a self-signed certificate; common on Linux and in
  Docker), or the URI is `ldaps://<IP>` and the certificate lists only names, LDAP sign-in
  stops until you set **Settings → LDAP → CA certificate file** (`ldap.ca_file`) or use a
  listed name.
- **LDAP / OIDC:** a group setting that matched only part of a group name gives no role any
  more (a WARNING names it); a sign-in under a local account's name is refused (delete the
  local account to move someone to SSO); an LDAP and an OIDC account can't share a name any
  more (the method used last keeps it); OIDC providers must announce `https://` endpoints, and
  an OIDC sign-in in progress during the upgrade fails once ("SSO state mismatch"). Locked out?
  `ser2net --set-password NAME` creates a local admin.
- **Viewers** (and open-access visitors with the viewer role) no longer see what is sent to a
  device in the hex view, nor trace files: give the operator role to those who need them.
- **Read-only Modbus mappings** refuse writes from Modbus/TCP masters (exception 01) and from
  MQTT / Sparkplug: untick *Read-only* where they must write. A retained `/set` is no longer
  written again after a reconnect (tick *Re-apply a retained /set after every connect* to keep
  that); a JSON list on a coil's `/set` is refused; a Sparkplug DCMD switches a coil only with
  a Boolean.
- **Backup restore** keeps the current accounts, passwords, API token and cluster key unless
  you choose to restore them too (`accounts=on`).
- **First-run setup** through a host name that isn't this machine's (a reverse proxy, the
  Docker host) asks for the code in `data/setup-code.txt`; or use `SER2NET_ADMIN_PASSWORD_FILE`
  or `ser2net --set-password admin`.
- **Request bodies** over 2 MB get HTTP 413, REST API included; an import may be 16 MB, the
  sign-in, setup and sign-out forms 64 KB.
- **Windows service:** run the new exe with `--install-service` from an elevated prompt. It
  copies itself to `C:\Program Files\ser2net\` and upgrades the service in place, keeping its
  data directory, which afterwards only Administrators can change. A service left as it is
  warns if non-administrators can modify its folder.
- **One ser2net per data directory:** a second instance, or `--reconfigure` beside a running
  service, refuses to start.
- **Docker:** a setting the container rejects is logged (`WARNING: config.json: …`) and left
  off instead of stopping the start, so check `docker logs`; `config.json` is only rewritten
  when it changed; `--group-add dialout` is no longer needed; the image no longer has `tests/`,
  `vendor/` or `docs/`.
- **Monitoring:** `bytes_in` / `bytes_out` count at the device now (a one-time level change in
  `rate()` panels); failed sign-ins under unknown names are logged as `(unknown user)`, and
  several LDAP / OIDC log lines are worded differently.
- **Classic imports done with 2.8.0:** re-check those mappings' TLS, allowed IPs and direction.
- **Source installs updated in place** keep the packages already in `lib/`. With ser2net
  stopped, refresh them in the install folder:
  `python3 -m pip install --target lib --upgrade --no-index --find-links vendor/wheels -r requirements.txt`.
- **Admin UI connection limits:** the admin UI closes a connection that sends no complete
  request within 10 s (a request body must then keep coming at about 1 KiB/s) or takes nothing
  of its response for 10 s, and takes at most 64 connections per address and 256 in all (new
  optional `admin_ui.request_timeout_s`, `max_connections_per_ip` and `max_connections` in
  `config.json`, 0 = no limit). Behind a reverse proxy everyone shares one address: raise the
  per-address limit there.
- **TLS mappings:** a host not in `allowed_client_ips` is closed before the handshake, and a
  handshake must finish within 15 s; past 32 at once from one address, further connections wait
  for a slot.
- **Windows** keeps the selector event loop, which now refuses new connections near its
  512-socket limit instead of stopping; `SER2NET_WINDOWS_LOOP=proactor` opts in to the proactor
  loop, which has no such limit but is not yet soak-tested with serial devices.

### Security
- **Mapping ids are validated everywhere they enter, completing a 2.8.0 fix.** 2.8.0 said
  "Traces can no longer write outside the data directory", but the id, part of a mapping's log
  and trace file names, was not checked: through the mapping form, the cluster mapping save or
  JSON import, an operator or a holder of the cluster key could still have logs or traces
  written outside the data directory (as LocalSystem for the Windows service), add lines to the
  audit log or keep the service from starting. Ids are now 1–64 letters, digits or `_`; any
  other id is replaced with a stable one on load or import (with a note), edits apply only to
  existing mappings, and the log and trace paths refuse anything else (#42).
- **A serial port must be a device, not a URL:** pyserial URL handlers (`socket://`, `spy://`,
  …) let a mapping open a TCP connection or truncate a file instead. They are refused
  everywhere and never opened. The fleet view's edit proxy relays only validated mapping
  fields, for mappings that exist on the peer (#42).
- **The fleet view could send the cluster key to a host that isn't a node:** its proxies took
  the peer's scheme from the browser's request (a crafted link opened by an operator was
  enough), and peer calls used `HTTP(S)_PROXY` or the Windows proxy. Peers are now looked up
  among the known ones and called directly at their own address, without proxies or redirects;
  the remote edit form needs the CSRF token (#43).
- **Cluster nodes no longer send the cluster key with their requests.** It went out in clear
  text on every fleet-view refresh (TLS between peers isn't verified), and a replayed beacon
  could make a node send it to another host. 2.8.1 nodes sign requests and replies (with a time
  and a one-time nonce) and drop replayed beacons; older nodes get the raw key only with
  *Legacy authentication* on, and only at their own address. The key is no longer in the
  Settings page, weak keys are flagged, refused peer requests are audited, and copies of one
  installation, which shared a session key, are reported and can get a new identity (#43).
- **LDAPS and StartTLS check the directory server's certificate.** Any certificate was
  accepted, so whoever could intercept the connection could read users' and the search
  account's passwords and answer for the directory, groups included. The certificate must come
  from a CA the system trusts or from `ldap.ca_file` and name the host;
  `ldap.tls_verify: false` is an explicit opt-out. Referrals aren't followed over TLS (#44).
- **LDAP and OIDC group settings match whole group names.** A setting matched anywhere in a
  group DN or claim value, so `ser2net-admins-readonly`, an OU of that name or `sysadmins` (for
  `admins`) gave that role, admin included. A user whose groups no longer give a role is signed
  out at the next sign-in (#44).
- **An LDAP or OIDC sign-in can't take over a local account, or one of the other method:** an
  OIDC user named like a local account (e.g. `admin`) turned it into an SSO account with the
  provider's role, possibly leaving no admin, and LDAP and OIDC sign-ins of one name took the
  same account over from each other. OIDC also sends PKCE, accepts only `https://` provider
  endpoints, ignores e-mail addresses marked unverified and requires `exp`; LDAP names match in
  any letter case, so a demoted user keeps no role under another spelling; the SSO state cookie
  has a key of its own, so it can't pass for a session cookie (#44).
- **First-run setup only opens through this machine's own names**: a web page could claim a
  fresh install through DNS rebinding, even on 127.0.0.1. Other names need the code from
  `data/setup-code.txt` (#44).
- **Signing out ends the session on the server**; an account re-created under a deleted user's
  name no longer accepts the old cookies; LDAP / OIDC sessions end while that method is off.
  **Restoring a backup** no longer revives revoked tokens, deleted users, old passwords or
  ended sessions, and a downloaded backup leaves out the session key (#44).
- **Viewers no longer see what is sent to a device.** Since 2.8.0 the hex view showed viewers
  and open-access visitors both directions, and anyone signed in could download traces —
  including what users type, such as a password at a prompt that isn't echoed. Viewers now see
  the device's output only; traces need the operator role (#45).
- An `api_token_role` that isn't a role (`Viewer`, a typo) gave the REST API token full access;
  it now gives a read-only one, with a start-up note (#45).
- **Request bodies are limited, and failed sign-ins can't empty the audit log.** Without an
  account, one sign-in request could make ser2net hold about a gigabyte or fill the temp
  directory, and a few with huge user names pushed every other record out of `audit.log`.
  Bodies are capped (sign-in forms 64 KB, imports 16 MB, others 2 MB), long names aren't tried,
  audit fields are cut, and bursts are summed up in one record. A failed sign-in no longer
  records the typed name, often a password, unless it is an account's; earlier lines stay
  (#45).
- **Read-only Modbus mappings refuse writes.** A Read-only gateway forwarded every request of
  an allowed master, writes and slave-restart or listen-only diagnostics included, and MQTT
  `/set` and Sparkplug DCMD wrote too. Only requests known to read pass now; the rest get
  exception 01 (#42).
- **An MQTT `/set` writes only its own point:** a JSON list on a writable coil wrote up to
  1,968 coils, points not marked writable included (#42).
- **UDP mappings no longer buffer serial writes without limit:** any host (the allow-list is
  empty by default) could grow memory and stall the event loop all mappings share; the queue is
  now 64 KiB and the excess is dropped and counted. A fixed reply address can be set, since any
  allowed, or forged, source took over the device's output (#42).
- **An RFC 2217 client can't make ser2net hold an endless subnegotiation**, growing the process
  until the system stopped it: telnet and RFC 2217 subnegotiations are limited to 1 KiB (#42).
- **A classic ser2net import no longer turns protected connections into open ones.**
  `certauth`, TLS client certificates, DTLS and write-only connections came in enabled,
  unencrypted and both ways, a connection's own `remaddr` was added to the default one instead
  of replacing it, and output-only traces recorded both directions. Such connections now come
  in disabled with the reason, `ssl` keeps TLS when its files are on this host, and import
  files are limited so a crafted one can't tie up the service (#43).
- **The Windows service runs from an administrator-only location.** It ran as LocalSystem from
  wherever the exe or checkout was, so a standard user who could modify that folder (one under
  `C:\`, say) could gain SYSTEM. `--install-service` now installs into
  `C:\Program Files\ser2net\` with the data in `C:\ProgramData\ser2net\`, and refuses a
  checkout that non-administrators can modify (#46).
- **Windows: an elevated run no longer gives its account write access to the service's data
  directory.** It added a full-control entry for the account running ser2net elevated there (in
  2.8.0 already `--install-service`), so that account's unelevated programs could change the
  LocalSystem service's configuration. The service's data folder and the folders in it are
  now left as they are; a folder private to that account still gets its entry (#46).
- The Windows executable bundles OpenSSL 3.5 LTS and CPython 3.12.16 instead of the end-of-life
  OpenSSL 3.0 (#46).
- Docker: folders mounted inside `/data` (certificates, say) are no longer handed to uid 10001
  (#46).
- **Connections can no longer use up the process's file descriptors.** The admin UI kept
  connections that sent no request open without limit, and a mapping's TLS listener ran the
  handshake (60 s) before checking the allow-list, so anyone who reached those ports could,
  without an account, take the descriptors every bridge needs; on Windows, more than 512
  sockets stopped the process. The admin UI now closes a connection that doesn't send its
  request (body included) in time or takes nothing of its response, and takes at most 256
  connections, 64 per address; a TLS mapping closes a host not in its allow-list before the
  handshake, which must finish within 15 s (32 at once per address, 128 in all; more wait for a
  slot); near the open-file or the Windows event-loop limit, new connections are refused and
  logged (#42).

### Fixed
- **The Modbus gateway reopens a lost serial port**; it answered 0x0B and showed `running`
  until restarted. It reconnects like other mappings (USB identity, notifications, `/metrics`),
  masters get 0x0A at once meanwhile, and *Kick old client* and *High-priority client IPs*
  apply to it now (#42).
- **A serial device that stops taking data no longer freezes ser2net.** With CTS low, XOFF or a
  virtual port nobody reads, a write could spin the event loop (every mapping and the web UI
  stood still), and stopping the mapping or the service could hang. A write now returns at
  once, and data the device hasn't taken 2 s after its port is closed is dropped (#42).
- **Retained MQTT commands aren't written again** after a reconnect, broker restart, service
  start or save, which switched outputs unattended. A point whose name contains `/` can be
  written (#42).
- Traffic counters count at the device, once: not per client, without telnet bytes or dropped
  data, and both directions of a serial↔serial bridge (#42).
- **RFC 2217:** malformed values no longer end the session; a value the port refuses is put
  back; a client stays usable when the port is reopened; connect-out RFC 2217 waits for its
  port; telnet and RFC 2217 take far less CPU (#42).
- A port missing at start shows `reconnecting` at once; editing a connect-out or UDP mapping no
  longer resets its client buffer (#42).
- **Fleet view:** remote actions work for peers behind NAT or Docker, entered by host name or
  IPv6 address, or without a default route (they silently got 403); non-ASCII cluster keys
  work; the panel says why a peer can't be used; a node restarted with its clock behind no
  longer disappears; a mapping on a 2.7 / 2.8.0 node whose id this version wouldn't accept can
  be edited under the node's own id (#43).
- The Docker health check works with `HTTP_PROXY` set; non-ASCII cookies or CSRF tokens no
  longer cause a server error (#43).
- **Active Directory direct bind with `{username}@corp.local`** (the suggested template) or
  `CORP\{username}` reads the user's groups; such users were refused or got the default role.
  Turkish İ/I/ı/i in group names match in any case; refusals and failed searches are logged
  (#44).
- **OIDC sign-ins no longer fail when the clocks differ by a second** (2 minutes are allowed),
  and a provider's error is shown instead of "SSO state mismatch" (#44).
- Accounts named with `/`, `\`, `#`, `?` or `%` can be signed out and deleted; phones no longer
  capitalise user names; a package that fails to import is reported as such, not as "not
  installed" (#44).
- **The standalone executables show the Turkish UI again** (the 2.8.0 binaries lacked it) and
  **keep their web UI** when temp cleaners remove unpacked files: every page answered 500 after
  about 10 days on Fedora, RHEL, Debian 13 and Ubuntu 26.04 (#46).
- **A rejected setting no longer stops the start or blocks every save:** Docker restarted in a
  loop and most saves failed on any install. It is now logged, the rest runs, and a save is
  refused only for what it changes (#46).
- **Docker:** a bind-mounted `/data` written by a pre-2.8 image works; serial devices work
  where `dialout` isn't GID 20 (Fedora, RHEL); start-up errors are one line that names the fix
  (#46).
- **Serial ports and MQTT keep working with many files open.** For a descriptor of 1024 or
  above, pyserial's `select()`-based read failed silently (the mapping showed `running` but
  forwarded nothing and missed an unplug), and paho-mqtt reconnected forever. Serial reads no
  longer use `select()` and hold one descriptor per port instead of five; MQTT waits with
  `poll()`. Errors asyncio reports and a fatal error now reach `all.log` (#42).

### Added
- **UDP:** *Reply to IP / port* and *Peer timeout* (off by default); `bytes_dropped` in the
  status and in `/metrics` (#42).
- **RFC 2217:** *Restore the serial settings when the client leaves* (off by default) (#42).
- **Modbus over MQTT:** *Re-apply a retained /set after every connect* (off by default); a
  **CMD** tag, **CMD ⚠** when the broker connection lacks TLS or a user name (#42).
- **Account recovery and session control:** `ser2net --set-password NAME` and
  `ser2net --sign-out-all` (stopped or running, also via `docker exec`: a running ser2net is
  found through the data directory's lock and handed the change), a first admin from
  `SER2NET_ADMIN_PASSWORD_FILE`, *Sign out everyone*, per-user *Sign out* and *Set password*,
  *Sign out my other sessions* (#44).
- Backups: a restore preview of the access and cluster settings it changes, restoring accounts
  on request, *Only its mappings…* (#44).
- Settings → LDAP: *CA certificate file*, *Don't check the server certificate*, *Also count
  nested groups* (Active Directory) (#44).
- Settings → Users: each account's last sign-in; optional removal of LDAP / OIDC accounts
  unused for 30–3650 days (off by default) (#44).
- **Settings → LAN cluster:** *Generate a new key and save*, *Legacy authentication* and
  *auto-discovery* switches, *Give this node a new identity*, and planned key changes (the
  previous key stays valid for 7 days) (#43).
- A lock per data directory (`ser2net.lock`); a version resource and an icon on the Windows
  exe, whose `--install-service` upgrades in place; `docs/WINDOWS.md` (firewall, address,
  upgrades, SmartScreen) (#46).
- The open-file limit is raised at start (up to 65536, logged; the systemd unit sets
  `LimitNOFILE=65536`), with a warning when the mappings need close to it; `/metrics` gains
  `ser2net_open_fds`, `ser2net_fd_limit`, `ser2net_admin_connections`,
  `ser2net_tls_handshakes_in_progress`, `ser2net_tls_handshakes_waiting` and
  `ser2net_connections_refused_total`; `SER2NET_WINDOWS_LOOP=proactor` (opt-in); the mapping
  form warns when *Kick old client* is on without allowed client IPs (#42).

### Changed
- The mapping form hides Connect banner, Close-on and Client buffer for Modbus gateways, which
  don't use them (#42).
- Plain-MQTT register values may be decimal, `0x` hex, a fraction or an exponent (`0o`, `0b`
  and `_` are refused, `010` is decimal); an empty command message is ignored (#42).
- A failed TLS handshake (mapping or admin UI) is logged, at most once a minute (#42).
- The cluster panel caches peer answers for 4 s; a manual peer must be an IP address or host
  name; a blank key field keeps the key (#43).
- Log and audit lines name the account, not the typed name; new audit actions
  `ldap_login_denied`, `oidc_login_denied`, `user_expired`, `users_expiry`, `console_open`,
  `hex_view_open` (once per account, mapping and kind in 5 minutes, then summed up) (#44, #45).
- The Docker image holds only runtime files and license texts (3.3 MB smaller);
  `docker-compose.yml` publishes port 4001, caps the log and drops `group_add` (#46).
- CI runs the LDAP / OIDC tests with their packages and checks every route against one access
  matrix (#44, #45).
- Dependencies updated: click 8.5.1; paho-mqtt 2.2.0 and authlib 1.9.5 in the executables and
  the Docker image (#41, #46).
- Docs: broker ACLs as the authorization for MQTT commands, cluster authentication and key
  changes, LDAP over TLS, account recovery and what each role sees (READMEs, `SECURITY.md`,
  `docs/DOCKER.md`); new `docs/WINDOWS.md` (#42–#46).

## [2.8.0] — 2026-11-04
Upgrading from 2.7.x needs no change: `config.json` and `data/` are used as they are, and the
first configuration change keeps the old `config.json` in `data/backups/`. A trace path in an
old config becomes a file name under `data/traces/` (the start-up log says so); the Docker
image takes over a `/data` volume written by the old root image on its first start.

### Security
- **Traces can no longer write outside the data directory.** A mapping's trace was a free
  file path, so an operator (not only an admin) could have raw network bytes appended to
  any file the service may write — `config.json`, a startup script; as root in Docker.
  Traces now always go to `<data>/traces/<mapping id>-<file name>`; a path in an older
  config keeps only its file name, and the move is logged at start-up (#29).
- **starlette and anyio are updated past known vulnerabilities.** 2.7.x pinned starlette 1.2.1
  and anyio 4.13.0, which are affected by GHSA-82r6-8w77-94w6 (anyio, critical),
  GHSA-82w8-qh3p-5jfq (starlette, high: form size limits were not applied to URL-encoded
  bodies, which the public sign-in page accepts), GHSA-5p39-cfhj-2xmp (anyio, medium) and
  GHSA-jp82-jpqv-5vv3 (starlette, low). 2.8.0 updates both (see Changed) (#28).
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
- **Turkish web UI** next to English: TR / EN in the top bar (remembered per browser), else
  the browser's language or the admin's default (Settings → Web UI access), which notifications
  use too. Logs, the audit log and the REST API stay English (#32).
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
- **Sparkplug B:** a Modbus-polling mapping can publish as a Sparkplug B edge node whose
  devices are the Modbus units it polls (`unit1`, `unit2`, …): NBIRTH/NDEATH with `bdSeq`,
  DBIRTH/DDATA with `seq`, writes through DCMD, rebirth through NCMD; without a protobuf
  dependency (#31).
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
- `ser2net --about` prints the versions of Python, OpenSSL and the bundled components
  (for bug reports); `ser2net --licenses` prints the license texts of ser2net and every
  third-party component inside the executable. The releases also attach them as
  `*.THIRD-PARTY-LICENSES.txt` (#28).
- LGPL: the executables keep ldap3 as plain source files; `SER2NET_LIB_OVERRIDE=<dir>`
  loads a replacement version from that directory (#28).
- CI: mypy, pip-audit, coverage (the servers the tests start are measured too) and an
  offline-wheel check; Dependabot for pip and GitHub Actions (#28).
- `SECURITY.md`, `CONTRIBUTING.md`, this changelog, issue and pull request templates (#28).

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
Standalone executables for Linux x64 and Windows x64 (PyInstaller), built and attached to
each release by a release workflow; `--version`; the Docker image includes the MQTT, LDAP and
OIDC packages.

## [2.3.0] — 2026-06-11
REST API (`/api/v1`, bearer token with admin/operator/viewer roles), Modbus RTU↔TCP gateway and
Modbus → MQTT polling, MQTT publishing, user roles (RBAC), LDAP/AD and OIDC sign-in, light/dark
theme, Dockerfile + docker-compose and CI; UDP allow-lists and RFC 2217 read-only are enforced.

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

[Unreleased]: https://github.com/haliskilic/ser2net/compare/v2.8.1...HEAD
[2.8.1]: https://github.com/haliskilic/ser2net/releases/tag/v2.8.1
[2.8.0]: https://github.com/haliskilic/ser2net/releases/tag/v2.8.0
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
