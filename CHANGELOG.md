# Changelog

All notable changes to ser2net. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Detailed notes for each version:
[GitHub Releases](https://github.com/haliskilic/ser2netweb/releases) and [ROADMAP.md](ROADMAP.md).

## [Unreleased]

## [3.0.0] — 2026-11-13
**ser2net is now ser2netweb.** The project was renamed to tell it apart from Corey Minyard's
ser2net, the C program that Debian, Ubuntu and Fedora package under that name: ser2netweb is an
independent Python project, not affiliated with or derived from it, that can import its
configuration files. The new name makes `.deb` / `.rpm` packages and a PyPI package possible.
Apart from the names, 3.0.0 works as 2.9.0 did: `config.json`, the data folder, the REST API
and the cluster protocol are unchanged. **Upgrading from 2.9.x:** what to do depends on how
ser2net is installed:
- **Docker:** nothing has to change: `ghcr.io/haliskilic/ser2net` gets the 3.x releases too, as
  the very same image and tags. The new path is `ghcr.io/haliskilic/ser2netweb`. The shipped
  `docker-compose.yml` now calls the service and the container `ser2netweb` and keeps the
  volume `ser2net-data`, which holds the data (a renamed volume would start an empty install);
  with it, `docker compose pull && docker compose up -d --remove-orphans` replaces the old
  `ser2net` container. Commands then name the new container
  (`docker exec -it ser2netweb python /app/ser2netweb.py --no-bootstrap --data-dir /data --set-password admin`).
  The image's account is `ser2netweb` with the same uid 10001, so volumes are used as they are
  (`docs/DOCKER.md` → *Upgrading from 2.9.x*) (#104, #106).
- **Standalone binary installed with 2.9.x's `ser2net-install.sh`:** put `ser2netweb-linux-x64`
  (or `-arm64`), `ser2netweb-standalone.service`, `ser2netweb-install.sh` and `SHA256SUMS` of
  this release in one folder and run `sudo sh ser2netweb-install.sh`. It recognises the
  `ser2net.service` that 2.9.x installed by its content, never by its name, stops and disables
  it, moves `/var/lib/ser2net` to `/var/lib/ser2netweb` for the new user `ser2netweb`, keeps
  the `--listen` address and your drop-ins, and removes the old unit and binary once
  `ser2netweb.service` answers; if a step fails, it prints how to go back. It never touches
  another `ser2net.service` (the classic daemon's) or a `/usr/local/bin/ser2net` that isn't
  2.x's, and it refuses, changing nothing, when `/var/lib/ser2netweb` already holds data or
  `/var/lib/ser2net` is a mount point. The user `ser2net` stays unless you add
  `--remove-old-user`, and even then while something else uses it. To use the `.deb` / `.rpm`
  package afterwards, run `sudo sh ser2netweb-install.sh --uninstall` (the data stays) and
  install the package: it never moves a 2.9.x install itself (#104, #106, #107).
- **Source checkout under systemd** (`/opt/ser2net`, `ser2net.service`, user `ser2net`): either
  keep the old names, `sudo git -C /opt/ser2net pull && sudo systemctl restart ser2net` (the
  old unit runs `ser2net.py`, which starts ser2netweb and says so once in its log), or move it
  over:
  ```sh
  sudo systemctl disable --now ser2net
  sudo useradd --system --user-group --home-dir /opt/ser2netweb -M -s /usr/sbin/nologin ser2netweb
  sudo mv /opt/ser2net /opt/ser2netweb && sudo git -C /opt/ser2netweb pull
  sudo chown -R ser2netweb:ser2netweb /opt/ser2netweb/data /opt/ser2netweb/lib
  sudo mv /etc/systemd/system/ser2net.service.d /etc/systemd/system/ser2netweb.service.d  # only if you have drop-ins
  sudo rm /etc/systemd/system/ser2net.service
  sudo cp /opt/ser2netweb/systemd/ser2netweb.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now ser2netweb
  ```
  Then the service is `ser2netweb` (`systemctl status ser2netweb`, `journalctl -u ser2netweb`),
  also for monitoring that checks the unit (#104, #106).
- **Template instances** (`ser2net@NAME`): once the checkout is in `/opt/ser2netweb` and owned
  by `ser2netweb` (the `useradd`, `mv` and `chown` lines above), run
  `sudo -u ser2netweb python3 /opt/ser2netweb/bootstrap.py` once, then for each instance:
  ```sh
  sudo systemctl disable --now ser2net@NAME
  sudo mkdir -p /var/lib/ser2netweb /etc/ser2netweb
  sudo mv /var/lib/ser2net/NAME /var/lib/ser2netweb/NAME
  sudo mv /etc/ser2net/NAME.env /etc/ser2netweb/NAME.env  # if there is one
  sudo cp /opt/ser2netweb/systemd/ser2netweb@.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now ser2netweb@NAME
  ```
  Move only the `.env` files, as `/etc/ser2net` is the classic daemon's folder too; systemd
  hands each moved data folder to `ser2netweb`. After the last instance,
  `sudo rm /etc/systemd/system/ser2net@.service` (#104, #106).
- **Windows service:** from an elevated prompt run
  `ser2netweb-windows-x64.exe --install-service`, without `--service-name` or `--data-dir`. It
  takes over 2.x's `ser2net` service when it recognises it by the program it runs: it stops it,
  moves its data from `C:\ProgramData\ser2net\` to `C:\ProgramData\ser2netweb\` when it was in
  that default place and the new folder doesn't exist yet (a custom data directory stays where
  it is), installs and starts `ser2netweb` from `C:\Program Files\ser2netweb\`, and only then
  deletes the old service and its exe; if that fails, it deletes the new service, puts the exe
  and the data back and starts the old one. A `ser2net` service it doesn't recognise is left
  alone, with a note. Named services: run `--install-service --service-name NAME` for each
  first, then the default one (named services keep their names and move to the new folders).
  Check absolute paths in `config.json` that point into the old data folder (TLS files in its
  `tls\` folder are found again). Re-add the firewall rule:
  `netsh advfirewall firewall add rule name="ser2netweb" dir=in action=allow program="C:\Program Files\ser2netweb\ser2netweb.exe" enable=yes`,
  then `netsh advfirewall firewall delete rule name="ser2net"` (a named service:
  `ser2net (NAME)`, `…\ser2netweb\NAME.exe`). The service, its display name and the Event Log
  source are `ser2netweb` (`sc stop ser2netweb`) (#104, #106).
- **Environment variables** have the prefix `SER2NETWEB_` (`SER2NETWEB_BIND_IP`,
  `SER2NETWEB_PORT`, …), and the `SER2NET_*` names still work. When both are set, the
  `SER2NETWEB_*` one wins, also when it is empty. The Docker image's defaults stay
  `SER2NET_BIND_IP` / `SER2NET_PORT`, and the template unit's `SER2NET_NODE_NAME=%i`, so that
  your setting under either name wins over them (#104, #105).
- **Release files have new names** (`ser2netweb-linux-x64`, `ser2netweb-linux-arm64`,
  `ser2netweb-windows-x64.exe`, `ser2netweb-install.sh`, …), and there are no files under the
  old names: a script that downloads `ser2net-linux-x64` must change (#104, #106).
- **`--version` prints `ser2netweb 3.0.0`** and `--help` shows `usage: ser2netweb`: scripts
  that match `ser2net ` must accept the new name (#104, #105).
- **User-Agent:** webhooks send `User-Agent: ser2netweb/<version>` (was `ser2net/…`), and OIDC
  and cluster peer requests now send it too (they sent Python's default): a receiver, proxy or
  WAF that filters on it needs the new value (#104, #105).
- **Python code** that imports `app.*` must import `ser2netweb.*`; the command line's functions
  are in `ser2netweb.cli` (#104, #105).
- **Unchanged:** the cluster protocol (2.9.x and 3.0 nodes work together), the Prometheus
  metric names `ser2net_*` (dashboards and alerts keep working), the MQTT and Sparkplug B
  defaults (Sparkplug group ID `ser2net`), the RFC 2217 signature `ser2net`, the `config.json`
  keys and the data folder's files, the REST API paths, the cookie names (nobody is signed out)
  and the default e-mail sender address `ser2net@<host>` (#104, #105).

### Added
- **`.deb` and `.rpm` packages** for x86_64 and arm64 with every release
  (`ser2netweb_X.Y.Z_amd64.deb`, `ser2netweb_X.Y.Z_arm64.deb`, `ser2netweb-X.Y.Z-1.x86_64.rpm`,
  `ser2netweb-X.Y.Z-1.aarch64.rpm`): `sudo apt install ./ser2netweb_X.Y.Z_amd64.deb` or
  `sudo dnf install ./ser2netweb-X.Y.Z-1.x86_64.rpm`. They install the standalone binary as
  `/usr/bin/ser2netweb`, its hardened unit `ser2netweb.service`, the account `ser2netweb` and
  `/var/lib/ser2netweb` (0700). Installing doesn't enable or start the service:
  `sudo systemctl enable --now ser2netweb`, then
  `sudo -u ser2netweb ser2netweb --set-password admin`. An upgrade restarts a running service;
  removing stops it and keeps the data and the account. The packages don't replace or conflict
  with the classic daemon's `ser2net` package (#104, #107).
- **PyPI package `ser2netweb`:** `pipx install ser2netweb` on any OS and CPU with Python 3.10
  or newer, or `pipx install "ser2netweb[all]"` for MQTT, LDAP, OIDC and in-process
  certificates (the extras `mqtt`, `ldap`, `oidc`, `tls`). A pip / pipx install keeps its data
  in the user's data folder, never next to the package, and can't be installed as a Windows
  service (#104, #107).
- **The default data folder follows the kind of installation:** a pip / pipx install uses
  `$XDG_DATA_HOME/ser2netweb` (else `~/.local/share/ser2netweb`),
  `~/Library/Application Support/ser2netweb` on macOS or `%LOCALAPPDATA%\ser2netweb` on
  Windows; the packaged `/usr/bin/ser2netweb` uses `/var/lib/ser2netweb`; a source checkout,
  the standalone binaries and Docker keep theirs (`data/` next to the checkout or the
  executable, `/data`). `--data-dir` and `--config` still choose another; `--help` shows this
  installation's default, and `--about` names the kind of installation (#104, #107).
- `python -m ser2netweb` runs the command line too (#104, #107).
- The READMEs, `NOTICE` and `COMMERCIAL_LICENSE.md` say that ser2netweb is an independent
  project, not affiliated with or derived from Corey Minyard's GPL-2.0 ser2net, and that the
  commercial licence covers only this project (#104, #105).
- **Release checks:** the release files must be exactly this version's `ser2netweb-*` files and
  four packages, each listed in `SHA256SUMS` and covered by the build provenance attestation,
  and every platform's SBOM must describe the same version; the PyPI files are uploaded by
  trusted publishing with PyPI's attestations (#104, #106, #107).

### Changed
- **The project is called ser2netweb** (it was ser2net up to 2.9.x): the web UI, `--version`,
  `--about`, `--help`, the logs, notifications, the OpenAPI title and the docs use the new
  name, and the repository is github.com/haliskilic/ser2netweb (the old links redirect) (#104,
  #105).
- **Start script `ser2netweb.py`:** a source install starts with `python3 ser2netweb.py`, as
  `start.sh` / `start.bat` and the Docker image do. `ser2net.py` keeps working and runs the
  same, so existing units, Windows services and scripts need no change; a server started with
  it says so once in its start-up log (#104, #105).
- **Environment variables `SER2NETWEB_*`**, with the old names still read; the READMEs have a
  table of them (#104, #105).
- The Python package is `ser2netweb` instead of `app` (the command line is
  `ser2netweb/cli.py`), and the PyInstaller spec is `ser2netweb.spec` (#104, #105).
- Notifications start with `ser2netweb <name>:` and e-mails come from `ser2netweb <name>`,
  signed `ser2netweb <version> on <host>`; filters that match `ser2net` still match, and the
  default sender address stays `ser2net@<host>` (#104, #105).
- New self-signed certificates have the CN `ser2netweb`, new two-factor enrolments the issuer
  `ser2netweb`, and exports download as `ser2netweb-mappings.json` / `ser2netweb-audit.csv`;
  existing certificates and authenticator entries are unchanged (#104, #105).
- **Release files `ser2netweb-*`:** `ser2netweb-linux-x64`, `ser2netweb-linux-arm64` and
  `ser2netweb-windows-x64.exe` with their `.cdx.json` and `.THIRD-PARTY-LICENSES.txt`,
  `ser2netweb-install.sh` and `ser2netweb-standalone.service` (#104, #106).
- **systemd units `ser2netweb.service`, `ser2netweb@.service`,
  `ser2netweb-standalone.service`** and `ser2netweb.service.d/notify.conf`: they run as the
  user `ser2netweb`, from `/opt/ser2netweb` (source) or as `/usr/local/bin/ser2netweb` with
  `/var/lib/ser2netweb` (binary); instances keep their data in `/var/lib/ser2netweb/<name>` and
  read `/etc/ser2netweb/<name>.env`. They no longer share the unit name and account of the
  classic ser2net daemon (#104, #106).
- **`ser2netweb-install.sh`** installs as above, moves a 2.9.x binary install over, and refuses
  to hide a packaged `ser2netweb.service` unless `--replace-unit` (#104, #106, #107).
- **Windows service `ser2netweb`**, installed in `C:\Program Files\ser2netweb\` with its data
  in `C:\ProgramData\ser2netweb\`; `--install-service` moves 2.x's `ser2net` service over, and
  `--uninstall-service` also finds it (#104, #106).
- **Docker image `ghcr.io/haliskilic/ser2netweb`**, also published as the same image and tags
  under `ghcr.io/haliskilic/ser2net`; the account in the image is `ser2netweb` (uid 10001), and
  the example compose service and container are `ser2netweb` (#104, #106).
- Messages, the Settings page and the docs name the new units, service, folders and image
  (`systemctl reload ser2netweb`, `sc stop ser2netweb`, `journalctl -u ser2netweb`,
  `docker exec ser2netweb …`) (#104, #106).

## [2.9.0] — 2026-11-12
The fixes for 2.8.0 that 2.8.1 left for this release, with the features they brought:
two-factor sign-in, named API tokens, reloading `config.json` without a restart, settings for
reverse proxies and for several instances on one machine, retention, more Modbus data types,
MQTT client certificates, listeners for IPv4 and IPv6. **Upgrading from 2.8.x:** `config.json`
and `data/` are used as they are; the start-up log notes each stored value this version reads
differently. Several fixes change behaviour on purpose. Check what applies to you (from 2.8.0
or older, read the 2.8.1 notes too):
- **Reverse proxy on the same machine:** list it as `127.0.0.1` under **Settings → Reverse
  proxy → Trusted proxies** (or `SER2NET_TRUSTED_PROXIES`; `FORWARDED_ALLOW_IPS` still works).
  `X-Forwarded-For` / `-Proto` are no longer believed from any local connection, so otherwise
  every user shows as 127.0.0.1 and all share one sign-in limit. A plain-HTTP proxy that
  rewrites `Host` must pass `X-Forwarded-Host`, or forms get 403 (`docs/REVERSE-PROXY.md`).
- **Stricter checks when a mapping is saved** (form, REST API, import, cluster edit, and a
  reload for the mappings it changes): two enabled mappings on one broker can't share MQTT
  command topics (`/set`, `/tx`), a client ID or a Sparkplug B edge node, and a disabled
  mapping that would clash can't be started; one serial port can't be used under two spellings
  (`COM3`, `com3`); a plain MQTT base topic or point name can't hold `+`, `#` or a null
  character (stored, it keeps MQTT off), and no point may be named `status` or `<point>/set`;
  no mapping may use the admin UI's address and port; TLS and MQTT TLS files must load, a
  disabled mapping's too; Modbus point values must have the right type; protective and
  only-true switches (below) take JSON booleans, not text. A stored mapping that breaks a rule
  still loads, with a note at start-up, but its next edit (a REST GET → PUT too) must fix it.
- **Text in `config.json` is read as its setting's type:** numeric text (`"9600"`) is
  converted, `"enabled": "false"` leaves the mapping stopped and `"writable": "false"` makes
  the point read-only. A switch that turns something on only when it is JSON `true`
  (`dual_stack`, `udp_broadcast`, `disconnect_without_device`, `poll_with_clients`, MQTT
  `tx_enabled`, `proxy_hsts`, `email_separately`) reads text as off; a protective switch
  (`read_only`, `tls`, `require_login`, `tls_verify`, `start_tls`, `mask_personal_data`, MQTT
  `tls_check_hostname`) reads text as on unless it is empty. A number that can't be read, a
  Modbus point's unit, address or scale included, takes its default. The start-up log notes
  each case. An `allowed_client_ips` list with no usable entry admits nobody instead of
  everyone.
- **Modbus gateway:** a reply that doesn't answer its request (function code, byte count, the
  exact echo of a write) gives the master exception 0x0B, so a slave that miscounts, doesn't
  echo FC5/FC6 exactly or answers reads of unit 0 stops working through it. A broadcast write
  (unit 0) is answered with its echo after 200 ms, a read of unit 0 gets 0x0B at once, and poll
  points on unit 0 aren't polled; a Modbus/TCP frame without a function code closes the
  master's connection.
- **Start no longer restarts a running mapping** (nor does an unchanged `PUT`): scripts that
  used `start` to resend the open string or reload a certificate must use `restart`.
- **A telnet BREAK now reaches the device** (PuTTY's *Special Command → Break*; it was
  dropped): a 0.25 s serial BREAK on telnet and RFC 2217 mappings that aren't read-only.
- **USB identity binding:** while its adapter is unplugged, a bound mapping shows
  `device missing` instead of opening another adapter that took its port name.
- **TLS mappings:** connect-out TLS now presents the configured certificate and key, which a
  server that rejects untrusted client certificates may refuse (clear them; they are optional
  for connect-out now). An encrypted key no longer asks for its pass phrase at the console: set
  the key password (`network.tls_key_password`; Settings → Admin TLS for the web UI), or it
  fails with the reason in all.log.
- **`kick_old_user` no longer evicts priority clients:** a newcomer replaces the oldest
  non-priority client and is refused while only priority clients are connected.
- **SIGHUP reloads `config.json`** instead of stopping ser2net: stop it with SIGTERM. A hand
  edit made while ser2net runs is noticed: apply it with *Reload config.json* in Settings,
  `ser2net --reload` or `systemctl reload ser2net` (a save before that keeps it in `backups/`).
  A reload is checked like an edit: a file that can't be read, or a change in it that an edit
  would refuse, changes nothing (all.log says why).
- **Imports:** a classic `ser2net.yaml` / `ser2net.conf` goes through the preview only (a
  one-step POST gets 400); merging one again keeps what was set up here since; `ipv6n4`
  accepters come in on IPv4 and IPv6; `flock` / `uucplock` come along, so an imported mapping
  refuses a port another program has locked. A JSON merge keeps the current value of a setting
  the file doesn't have (it was reset to its default).
- **Sign-in:** 10 failures in 15 minutes, from any address, give that user name HTTP 429 until
  they expire. Password hashes get a higher scrypt cost (a sign-in takes about 5 times the
  CPU), each renewed at its account's next sign-in: **if you go back to 2.8.1 or older**, those
  accounts need a new password there (`ser2net --set-password NAME` on 2.8.1; on 2.8.0 and
  older a `config.json` from `backups/` made before the upgrade).
- **The event loop watchdog is on by default:** when the event loop hasn't run for 90 s,
  ser2net writes every thread's stack to `data/hang.log` and exits with code 70 for systemd,
  the Windows service or Docker's restart policy to start it again. A machine that slept or a
  paused container doesn't count. `SER2NET_LOOP_WATCHDOG` sets the limit (0 = off).
- **Traces pause below 256 MB free** on the data disk (5% of a larger disk, at most 1 GB) and
  go on by themselves. The first start deletes the trace files of mappings that no longer
  exist, also another configuration's in a shared data directory, and logs how many.
- **New files are owner-only** on Linux and macOS (umask 077: logs, the audit log, traces): in
  a data directory that isn't private, another account (a log shipper) can no longer read them.
  Docker's `/data` is 0700 from the start. A `--config` file's folder that holds other files
  keeps its permissions (it was made 0700).
- **IPv6:** `::` takes IPv6 clients only, on every OS (a UDP mapping on `::` stored by an older
  release keeps doing what it did); the new *All interfaces (IPv4 + IPv6)* takes both. A bare
  `0.0.0.0` or `::` in a client list matches both families. Per-address limits count an IPv6
  /64 as one address: raise `admin_ui.max_connections_per_ip` for a large IPv6 LAN.
- **Admin UI:** a UI that can't listen (port taken, address not up yet) no longer ends ser2net
  with exit code 3: the mappings run and the UI retries. It listens before the mappings start,
  so check `/healthz`, not a bare TCP connect.
- **systemd:** copy the new `systemd/ser2net.service` to `/etc/systemd/system/` and run
  `systemctl daemon-reload`: it starts on a fresh install, allows netlink, turns a denied
  system call into an error and has `ExecReload`, which would end a ser2net before 2.9.0.
  `systemctl reload` works with all three units, the new `ser2net@.service` (several instances)
  and `ser2net-standalone.service` (the Linux binaries, installed by `ser2net-install.sh`)
  included. Optional: the drop-in `ser2net.service.d/notify.conf` (`Type=notify`; remove it
  before going back).
- **Docker:** add `init: true` and `stop_grace_period: 30s` to your compose file
  (`docker run --init --stop-timeout 30`). `:2.9` and `:latest` are rebuilt weekly when the
  base image changes (an auto-updater then restarts the container); pin `:2.9.0` to avoid that.
- **Scripts that use the web UI:** Settings forms answer `303` to `/settings#<section>` (`400`
  when refused; both were 200; a save that switches admin TLS on or off answers `200` with the
  page itself, as the scheme changed), and a new API token or cluster key is on the page that
  follows: follow redirects with the cookie jar (`curl -L -b jar -c jar`). Without a session a
  page redirects to `/login?next=<page>`, and `/metrics` answers 401 to clients that aren't
  browsers. A dashboard bulk action that can't change a mapping answers `400` with the reasons,
  after doing the others. The CSRF token changes at each sign-in and sign-out; on ports other
  than 8080 and over HTTPS the cookies have new names (nobody is signed out, but going back to
  2.8.1 costs one sign-in there). Console WebSockets close with a code (4001–4004, 1013). The
  UI's own endpoints (`/api/status`, …) changed: use `/api/v1`.
- **REST API:** a refused change answers `400 {"error": "validation"}` (start and restart gave
  500), a `config.json` that can't be written `500 {"error": "save_failed"}`, other errors
  `500 {"error": "internal"}`; answers carry `"changed"` and may carry `warnings`.
- **Logs:** times carry their UTC offset (`2026-11-06 12:00:00+03:00`), IPv6 peers read
  `[addr]:port`, control characters become spaces, and a repeated error is logged once and then
  summed up. all.log gains a line per REST API request (except the polls), one per refused
  `/metrics` scrape (once a minute per address and status), an `import warning:` line per
  import warning and the library warnings that went to stderr. Check log shipper patterns and
  scripts that count lines.
- **Notifications:** failed SSO sign-ins send *Failed sign-in*; a second connection from one
  host within the repeat interval isn't notified; the test message is wholly in the
  notification language (English with *auto*).
- **MQTT and Sparkplug B consumers:** `.../set/result` comes at QoS 1, in JSON for a JSON
  `/set`, and reads `error: no reply from unit N within T s` instead of
  `error: modbus exception 0x0b`. Sparkplug B sends a DDEATH for a unit that stops answering,
  stamps metrics with their read time, sends `bdSeq` as Int64, continued across restarts, and
  follows each DCMD with a DDATA.
- **LAN cluster:** mixed versions work (the fleet view tags older peers and offers them only
  the actions and settings their version has), but an edit between two nodes before 2.9.0 still
  loses settings such as the MQTT client ID: upgrade every node (README → *Upgrading a
  cluster*).
- **Windows:** serial writes are paced to the line (8 KiB per port at the driver); if a virtual
  COM driver (com0com, say) misbehaves, `SER2NET_WINDOWS_SERIAL=pyserial` restores the old
  write path. `--install-service` exits 1 when the service fails to start.
- **Source installs:** the first start after an upgrade refreshes `lib/` offline, so 2.8.1's
  manual `pip install --target lib` step isn't needed, and refuses a bundled wheel whose
  checksum differs; pip and Python 3.10 or newer are required. Under `ser2net@.service` the
  instances share one `lib/` and don't refresh it: run
  `sudo -u ser2net python3 /opt/ser2net/bootstrap.py` after each upgrade.
- **Going back to 2.8.x:** copy `data/` first. Older versions drop named API tokens, two-factor
  sign-in, `dual_stack`, the per-mapping TLS checks, MQTT TLS files, retention and the new
  Modbus point keys at their next save, and can't poll the new Modbus types (README → *Going
  back to an older version*). Put back that version's systemd unit (2.9's `ExecReload` would
  end it) and remove `notify.conf`.

### Security
- **Changes another site's page makes the browser send are refused:** a POST, PUT, PATCH or
  DELETE gets 403 when `Sec-Fetch-Site`, or an `Origin` that isn't the UI's address (port
  included), says so, before the CSRF token is checked: a page on another port of the same host
  could plant one. The token is renewed at every sign-in and sign-out; htmx runs without
  `eval`; the CSP adds `base-uri 'none'`, `form-action 'self'`, `object-src 'none'` (#60).
- **Secrets stay out of the browser:** pages are sent with `Cache-Control: no-store` (Back
  after a sign-out showed a new API token again), stored passwords (MQTT broker, MQTT client
  key, TLS key) are no longer written into the mapping form (blank keeps them), and password
  managers no longer fill the sign-in into the MQTT, LDAP, OIDC, SMTP, webhook and cluster key
  fields (#60).
- **`X-Forwarded-For` is believed only from trusted proxies:** any connection from 127.0.0.1 or
  ::1 (a local user, an SSH tunnel) could choose the address the sign-in limit and the logs
  see. Cookies are Secure whenever the browser used HTTPS, through a TLS proxy too (#79).
- **Sign-in limits hold:** concurrent wrong sign-ins from one address got past the 8 password
  checks per 5 minutes; an attempt is now counted before its check. Failures are also limited
  per user name, a wrong current password in *Change your password* counts, a name without an
  account takes as long as a wrong password, and hashes use scrypt N=2^14, r=8, p=5 (#59).
- **An allow-list with no usable entry refuses every client.** An `allowed_client_ips` of only
  host names or ranges (a hand edit) admitted everyone, Modbus writes included (#53).
- **Source installs no longer keep an earlier release's packages:** `lib/`, which `git pull`
  doesn't touch, kept 2.7's starlette and anyio with their advisories; the first start after an
  upgrade refreshes it and keeps packages you added (#51).
- **Per-mapping TLS can check the other side:** connect-out TLS checks the server against a CA
  file (`network.tls_ca`) or pinned SHA-256 fingerprints (`network.tls_fingerprint`), else it
  only encrypts; a TCP-server or Modbus-gateway listener with `network.tls_client_ca` admits
  only clients with a certificate from that CA (#65).
- **Log lines can't be forged:** line breaks, terminal escapes and other control characters in
  mapping names, directory user names or device text are written as spaces in all.log, stdout
  and the mapping logs (#69).
- **Consoles and hex views are limited:** any signed-in user (open access: any visitor) could
  open any number and slow every bridge. Now 4 per account (open access: per address) and
  mapping, 16 per mapping, 64 in all; one taking nothing for 30 s is closed (#74).
- **Builds and releases:** GitHub Actions are pinned to commit SHAs, files are attested and
  attached in separate jobs, builds install hash-locked dependencies,
  `vendor/wheels/SHA256SUMS` records the offline wheels; the self-signed certificate fallback
  runs `openssl` by its full path, and webhooks go only to `http(s)` URLs (#81, #82, #83).

### Fixed
- **Serial ports at baud rates without a standard constant (250000, 74880, …) open again** on
  Ubuntu 26.04, Fedora 43 and later with the Linux executables (glibc 2.42) (#48).
- **Stopping never waits for a device that takes no data**, and shutting down has time limits
  (15 s for the mappings, 3 s for an MQTT broker); a stop during start-up is no longer lost; a
  client that reads nothing is cut off 5 s after its session ends (#48).
- **Windows: serial writes wait for the device** as on Linux instead of filling the driver's
  queue without limit, and idle ports cost less CPU (#48).
- **Serial ports say what is wrong:** refused settings, RS-485 or RTS/DTR settings a port
  doesn't take, `device missing` while the device is gone; the same error is logged once, not
  at every retry. A re-plugged adapter opens at once, RTS/DTR `off` no longer pulses at open on
  Windows, and a serial↔serial bridge no longer resets the other board at each retry (#48,
  #63).
- **USB identity binding opens the configured adapter among identical ones** (CH340, PL2303,
  FT4232H channels), never another that took its port; an unknown `match` key no longer matches
  every port; `com3`, `COM3` and `\\.\COM3` are one port (#62).
- **A listener retries** an address not up yet or a port in use (at most 30 s apart, without
  pulsing DTR/RTS); the admin UI starts first, so a mapping on its port no longer restart-loops
  it; connection limits and `kick_old_user` hold for clients that arrive together. A mapping
  now listens before it opens its device, so a taken port fails without touching the device;
  what a client sends in the moment before the device is open is dropped (#61).
- **IPv6:** a mapping can listen on `::1`, a bare `0.0.0.0` or `::` in a client list matches
  every client as documented, addresses next to a port are in brackets, and classic `ipv6n4`
  accepters import for both families (#64).
- **TLS files:** an encrypted key no longer stalls ser2net at a console prompt; saving a TLS
  mapping, a disabled one too, checks that its files load; connect-out presents its certificate
  when the server asks; a moved or restored data directory finds its `tls/` files (#65).
- **Telnet and RFC 2217:** setting changes wait for the data before them, flow control is one
  mode at a time, ignored requests are answered, a BREAK left on ends with its client (#89).
- **Modbus RTU replies are no longer cut short** by USB adapters (FTDI's 16 ms latency timer)
  or the Windows executable's clock: a reply ends at the length its header gives, stale bytes
  are skipped (`ser>drop` in the trace), a late reply never goes to the next request, the
  timeout starts once the request has left, and a request on the bus when the port goes away
  gets 0x0A (#50).
- **Sparkplug B: a Modbus unit that stops answering gets a DDEATH** after 3 polls and a DBIRTH
  when it is back, and is polled one point per cycle; no DDATA before the NBIRTH; metrics carry
  their read time; `bdSeq` survives restarts (`data/sparkplug-bdseq.json`) (#55).
- **MQTT: `<topic>/status` reads `online` after every connect** (Home Assistant showed working
  bridges offline); connection problems are logged; a stop no longer freezes ser2net while the
  broker doesn't answer; QoS 1/2 backlogs stop at 10,000 messages per mapping (#56).
- **MQTT writes are made one at a time, in order and bounded** (32 waiting, 10 s at most); a
  stop answers every write; oversized commands and values nested too deeply are refused (on
  Python 3.14 such a value stopped the gateway's writes); a write is no longer followed by the
  value read before it (#57).
- **Two mappings can no longer share MQTT command topics, a client ID or a Sparkplug B edge
  node** (one command could switch equipment on two lines), and a **Copy** gets its own name,
  base topic and edge node ID, no client ID and a free port (#58).
- **Modbus points JSON:** invalid JSON is reported with its line and column and keeps what was
  typed; a misspelt key or a value of the wrong type is refused naming the point
  (`"writable": "false"` opened a write path); Turkish names are shown as written (#73).
- **Operations on one mapping take turns** (two at once could leave a runner holding the port
  until a restart), and **changes are all or nothing**; starts and stops no longer push out the
  50 configuration backups; a backup saved with the clock behind is kept (#52).
- **Configuration values are read as their setting's type**, and one that can't be read is
  refused with its path instead of a 500 that left the change in memory; an import file nested
  deeper than 64 levels is refused with a clear message (#53).
- **Editing a mapping keeps what the form doesn't show:** the MQTT client ID (now a field), the
  USB identity after a refused form, the settings of hidden blocks, banners with line breaks,
  RS-485 delays; also for fleet-view edits from older nodes (#54).
- **Traces:** downloading one that is being written no longer breaks off; one that can't be
  written pauses, says why (`trace_error`) and retries; the log files of mappings an import or
  a restore removed are closed at once (#59, #67).
- **The hourly log trim no longer reads each log into memory** (1 GB for a 100 MB log) nor
  drops recent lines after a clock jump; refused connections are logged once a minute (#68).
- **A console window no longer stops forwarding** (a click into a Windows console held every
  mapping); the web server's errors and library warnings reach all.log; the Windows service
  keeps its errors in `service.log` and the event log (#66).
- **A step of the system clock no longer changes uptime, connection times, the peer list or
  which client is kicked**; self-signed certificates are backdated by a day (#71).
- **Re-importing a classic ser2net file no longer undoes what was set up since** and never
  widens a bind IP or an allow-list; open and close strings with a byte of 0x80 or more come in
  disabled; framing words, class defaults, `\o` and `\\` are read as ser2net reads them (#72).
- E-mail for a mapping whose name holds a line break is sent again (#69). The READMEs' full
  reset leaves nothing behind, and `--uninstall-service` names what it keeps (#70).
- **The dashboard shows failed actions** (a saved mapping that doesn't start too) **and ended
  sessions**, asks before unsaved form changes are lost, scrolls panels into view, keeps the
  keyboard focus and ignores double clicks; a page left open while you signed in again
  elsewhere still saves (it gets the new CSRF token in an `X-CSRF-Token` header) (#49, #60).
- **The dashboard no longer redraws its table every 2 s** (on the event loop every bridge uses,
  losing selections and clicks); **bulk actions act only on the ticked rows shown**; the light
  theme's colours are readable (#75).
- **Settings forms no longer post again on a reload** (which made another API token), TR / EN
  works after a save, and a refused form keeps what was typed (#76).
- **Searches find Turkish names in any letter case** ('istanbul' finds 'İstanbul'), names sort
  naturally (COM2 before COM10), SMTP credentials and webhook URLs may hold Turkish letters,
  and a mistyped number is refused naming the field (#77).
- **Consoles work behind a reverse proxy that rewrites `Host`** (#78), reconnect after their
  mapping restarts and say why they ended; a Modbus gateway's opens read-only (#74).
- **Instances under one host name no longer sign each other out:** cookies on ports other than
  8080 carry a tag of the port, `__Host-` over HTTPS (#80).
- **Hand edits of `config.json` are no longer overwritten without a word** (a save keeps them
  in `backups/`); SIGHUP no longer ends ser2net; a pending TLS change no longer breaks cookies
  or the Docker health check; a restore restarts only what it changes (#75, #90).
- Passwords with decomposed accents or no-break spaces match (NFKC), and a client that keeps
  reconnecting no longer uses up the notification budget (#59). A form with a file part gets
  400, not 500 (#81). A blank SSO redirect URI works behind chained proxies (#79).
- **The systemd unit starts on a fresh install** (it stopped with status 226/NAMESPACE), lists
  the LAN addresses again, IPv6 too, and fails a denied system call instead of being killed;
  the dashboard says why the system refused a listener's port (#51).
- **Offline source installs work on ARM Linux, macOS, Alpine and ARM Windows**; missing pip or
  old Python is explained; **ser2net only makes its own data directory private** (#85).
- The SBOMs give SPDX license ids and list the native libraries; a build with
  `pyinstaller ser2net.spec` alone has the `--licenses` text again (#83).

### Added
- **Two-factor sign-in (TOTP) for local accounts** with any authenticator app;
  `ser2net --set-password NAME` turns it off. **Named REST API tokens**, each viewer or
  operator with an optional expiry, next to the existing token (#88).
- **REST API:** `PATCH /api/v1/mappings/{id}` (JSON merge patch), `POST /api/v1/import`
  (atomic, merge or replace, `dry_run=true`), `GET /api/v1/metrics` with a token,
  `GET /api/v1/info`; `ETag` / `If-Match` (412) on mappings, `Location` on create,
  `connected_s` and `connected_at` per client, settings that do nothing in `warnings`; API
  version 1.1.0, OpenAPI for every route and field (#52, #53, #54, #75, #88).
- **Reload `config.json` without a restart:** `systemctl reload` (all three systemd units),
  `kill -HUP`, `ser2net --reload` (Windows service and containers too) or Settings; unchanged
  mappings keep their clients. **Admin TLS without a restart:** a certificate saved or
  generated in Settings, an encrypted key with its key password too, applies to new connections
  at once, and a reload reads renewed ones (certbot `--deploy-hook "systemctl reload ser2net"`)
  (#90).
- **Modbus:** `word_order`, `int64` / `uint64` / `float64`, `string` and register bits; opt-in
  block reads; a gateway unit map (`255=1, 0=1`); `min`, `max`, `allowed` and write-only
  points; a field editor and **Read these points now**; the response timeout in the form; each
  unit's state in the status, `/metrics`, the dashboard and notifications (#50, #55, #57, #73).
- **MQTT:** a CA file (each certificate in it trusted), a client certificate (mutual TLS) and a
  host name check per mapping, relative paths in the data directory; the connection state in
  the status, the dashboard and `/metrics`; *Retain the data*; JSON values with read time and
  quality; JSON `/set` requests with an id; a unit status topic; a Sparkplug B **primary host
  ID**; a DDATA after each DCMD (#55, #56, #57).
- **Device server:** telnet BREAK, and *Send BREAK* in the console; *Disconnect clients while
  the serial device is unavailable*; UDP targets for the device's output; MQTT for serial
  mappings (line end, writes on `<topic>/tx`, a poll string); the RFC 2217 signature (#89).
- **IPv4 and IPv6 on one listener** (*All interfaces (IPv4 + IPv6)*, `"dual_stack": true`), for
  the admin UI too (`SER2NET_BIND_IP=*`) (#64).
- **Connect-out link status** on the dashboard, in `/api/v1/status`, `/metrics` and the fleet
  view, with `link_lost` / `link_back` notifications (#61).
- **Serial ports:** a typed device path, every port of a Linux kernel serial driver (i.MX,
  Jetson, Zynq, …) and `/dev/serial/by-path` names; why a port can't be opened (the device's
  group, who has it), also as an English sentence in the `hint` of `/api/v1/status`, which says
  too why the system refused a listener's port; ports of the kernel console, a login prompt or
  ModemManager are marked; opt-in `TIOCEXCL` and UUCP lock checks (#51, #62, #63).
- **Monitoring:** a no-data alarm per mapping; notifications for a cluster node offline or
  back, ser2net started and the data disk almost full; `/metrics` with HELP / TYPE, mapping
  state, build info, disk, trace and log sizes and the event loop's lag; an optional systemd
  `Type=notify` drop-in (#66, #68).
- **Retention:** Settings → Retention can delete traces, logs, the audit log and backups older
  than a number of days (off by default); the Trace panel deletes trace files (#67, #68).
- **Privacy:** `docs/PRIVACY.md`; deleting backups and a deleted account from them; a sign-in
  notice; *Leave out personal data* and one e-mail per recipient for notifications;
  `SER2NET_LOG_STDOUT=off`; a choice of which web UI requests all.log records (#70).
- **Audit log:** console sessions that wrote to a device, failed SSO sign-ins, exports, refused
  REST API tokens; a **CSV for Excel** export (#69, #77).
- **Reverse proxy:** Settings → Reverse proxy with *Public URL* (`SER2NET_PUBLIC_URL`, also for
  serving under a path), *Trusted proxies* and HSTS through the proxy; Admin TLS → HSTS
  max-age; the SSO redirect URI to register is shown; `docs/REVERSE-PROXY.md` (#78, #79).
- **Several instances on one machine:** *Name of this instance* (`SER2NET_NODE_NAME`) in
  notifications, the fleet view and the browser tab; `systemd/ser2net@.service` (#80).
- **Fleet view and edits:** nodes report their version and capabilities; a peer gets only the
  actions it supports, and a 2.7.1 or 2.8.x node's form only the settings it keeps; a mapping
  form opened before the mapping changed is refused and shown again with your values (#54).
- **Clock:** a start-up warning when the clock reads earlier than the newest backup; the server
  time and the admin certificate's validity in Settings (#71).
- **Dashboard and Settings:** *Select all shown*; confirmations before bulk actions, stops with
  clients connected and other destructive buttons; a result for each action; screen reader
  regions; *Change the role only* for the API token; Copy buttons (#49, #75).
- A console text encoding per mapping (ISO-8859-9, Windows-1254, CP857, …) (#74); an import
  preview that shows each mapping as it will be (#72) and says that a classic `openstr` goes
  out once per port open and which listening ports below 1024 ser2net can't use (#87);
  `written_by` in `config.json` (#53); classic `ssl(clientauth,CA=…)` imports with its CA file
  (#65).
- **Installing:** `ser2net-install.sh` and `ser2net-standalone.service` with every release; a
  Docker image for `linux/arm/v7`; `SER2NET_LOW_PORTS=1` for ports below 1024 in Docker;
  `SER2NET_WINDOWS_SERIAL=pyserial` (#48, #51, #85).
- **Every page names the licence and links the source code**; signed-in pages add the version
  and a *Licences* page with the licence texts this copy carries; `SER2NET_SOURCE_URL` points
  the link at a modified copy's source (AGPL-3.0 section 13) (#86).
- **New guides:** `docs/MODBUS-MQTT.md` (MQTT topics, Modbus points, Sparkplug B),
  `docs/MIGRATING-FROM-SER2NET.md`, `docs/SSO.md` (LDAP / Active Directory and OIDC) and
  `docs/LINUX-SERIAL.md`, linked from the READMEs, the mapping form, Settings and the OpenAPI
  document (#87).

### Changed
- **A release publishes only what passed:** its tag must match `app/__init__.py`, have a dated
  CHANGELOG section and be on `main`; the whole CI runs on it, the image is tagged only after
  its digest passed the smoke test, and a pre-release never becomes `latest`. Between releases
  the image is rebuilt weekly on an updated base (#82, #83).
- CI: one `ci-ok` gate, Python 3.14, a coverage floor, ruff's security rules, zizmor, the
  systemd units on systemd, paho-mqtt 1.6, upgrades from 2.8.0, serial stress and soak tests,
  offline first starts on ARM and macOS, a daily dependency audit (#51, #57, #81, #84, #85).
- `@xterm/xterm` 5.5.0, `@xterm/addon-fit` 0.10.0, htmx 2.0.11; static files are cached until
  they change; the image is based on `python:3.12-slim-trixie` (#83).
- ser2net's pyserial fixes live in its own classes instead of patching the library (#48);
  `bootstrap.py` always reinstalls (#51).
- A bulk action is one change: a mapping it can't change is named (`400` with the reasons) and
  the others are done. Every save keeps a backup of what it wrote, so the newest backup is
  `config.json` itself, and a save that changes nothing writes nothing (#52, #53).
- Password checks run at most 2 at a time (LDAP 4), and the thread pool has 32 threads (#59).
- The serial port list is refreshed when a port comes or goes (at least every minute) instead
  of every 2 s, without pyudev / pywin32; *Exclusive open* reads **Lock the port (flock)**
  (#62, #63).
- all.log names the REST API token of a request; fleet-view actions are audited as
  `cluster-peer:NODE/USER`; Discord no longer pings for mentions in names; `--about` hides
  account names in paths (#69, #70).
- Mappings exports are UTF-8 (#73); console traffic goes in 16 ms batches (#74); In / Out read
  B / KB / MB (#75); web UI errors are explained in the page's language (#77).
- Docs: both READMEs are checked against this release and gain an install table, a table of
  every command-line option and new screenshots in English and Turkish; with `docs/DOCKER.md`,
  `docs/WINDOWS.md` and `SECURITY.md` they cover upgrading a cluster, downgrading, several
  instances, time synchronisation, ports shared with the console or ModemManager, and
  reloading; `docs/VIRTUAL-COM.md` covers BREAK and has working Linux and Windows recipes
  (ttynvt, hub4com) (#51, #53, #54, #63, #71, #80, #86, #89, #90).
- The sign-in and first-run setup pages no longer show the version (#86).

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

[Unreleased]: https://github.com/haliskilic/ser2netweb/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v3.0.0
[2.9.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.9.0
[2.8.1]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.8.1
[2.8.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.8.0
[2.7.1]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.7.1
[2.7.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.7.0
[2.6.3]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.6.3
[2.6.2]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.6.2
[2.6.1]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.6.1
[2.6.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.6.0
[2.5.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.5.0
[2.4.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.4.0
[2.3.0]: https://github.com/haliskilic/ser2netweb/releases/tag/v2.3.0
[2.2]: https://github.com/haliskilic/ser2netweb/tree/v2.2
[2.1]: https://github.com/haliskilic/ser2netweb/tree/v2.1
[2.0]: https://github.com/haliskilic/ser2netweb/tree/v2.0
[1.2]: https://github.com/haliskilic/ser2netweb/tree/v1.2
[1.1]: https://github.com/haliskilic/ser2netweb/tree/v1.1
[1.0]: https://github.com/haliskilic/ser2netweb/tree/v1.0
