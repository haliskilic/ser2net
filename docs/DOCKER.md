# Running ser2net in Docker

ser2net ships an official `Dockerfile`, an example `docker-compose.yml` and a published
image for amd64, arm64 and arm/v7 (32-bit ARM: 32-bit Raspberry Pi OS, armv7 gateways;
since 2.9.0) with every release: `ghcr.io/haliskilic/ser2net` (`:latest` or a release tag).
The published image is the very build the release smoke-tested on each architecture (same
digest; arm/v7 under emulation, as no runner has that CPU); its tags `:X.Y.Z`, `:X.Y` and
`:latest` are added only after that. On arm/v7 the image builds psutil and cffi from their
hash-locked source releases (PyPI has no wheels of them for that CPU); everything else, and
every package on amd64 and arm64, is the hash-locked wheel. A pre-release (`X.Y.Z-rc.N`) gets only its own tag, never `:X.Y` or `:latest`.

**Between releases** the image is kept current. Every Monday
`.github/workflows/image-refresh.yml` scans `:latest` for known vulnerabilities (the HIGH and
CRITICAL ones with a fix go to the repository's code scanning) and checks whether the base
image (`python:3.12-slim-trixie`: CPython 3.12 on Debian 13) has moved since `:latest` was
built. If it has, it rebuilds the latest release from its tag on the new base — the same
ser2net code and the same hash-locked Python packages — smoke-tests it natively on both
architectures and moves `:X.Y` and `:latest` to it. `:X.Y.Z` always stays the image its
release published. So `:latest` / `:X.Y` get Debian, OpenSSL and CPython 3.12.x fixes within
a week; an auto-updater (Watchtower and the like) restarts the container when that happens.
Use `:X.Y.Z` (or a digest) to change only when you decide to. The base is a Debian codename
tag, so a refresh never switches to a new Debian major release; only a ser2net release does.
Each image records its base:
`docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.base.digest" }}' ghcr.io/haliskilic/ser2net:latest`.
Build provenance: `gh attestation verify oci://ghcr.io/haliskilic/ser2net:latest -R haliskilic/ser2net`
(a release's image is attested by `release.yml`, a refreshed one by `image-refresh.yml`).

## Quick start (compose)

1. Edit `docker-compose.yml` → `devices:` to list your serial port(s). Prefer a
   stable path so USB re-plug renumbering doesn't break the mapping:
   ```yaml
   devices:
     - "/dev/serial/by-id/usb-FTDI_...-if00-port0:/dev/ttyUSB0"
   ```
   A by-id name is made from vendor, model and serial number: identical adapters without
   a serial number of their own (CH340, most PL2303) share one, and udev gives it to only
   one of them (which one can change at a reboot). Pass those by USB socket instead
   (`/dev/serial/by-path/...:/dev/ttyUSB0`), and keep each adapter in its socket.
2. Edit `ports:` so it publishes the port of every mapping you are going to create
   (a new mapping gets 4001; [Ports and networking](#ports-and-networking)).
3. Start it:
   ```bash
   docker compose up -d
   ```
4. Open `http://<host>:8080` and set the admin password on first access — or create the
   first admin without the browser (next section). Inside the container `<host>` is not the
   machine's own name, so opened by a host name (not an IP address) the setup page asks for
   a one-time code, which keeps other web pages from claiming the new install:
   `docker exec ser2net cat /data/setup-code.txt`.

## First admin and account recovery

- **Headless first admin:** give the container the password in a file (a Docker secret)
  and point `SER2NET_ADMIN_PASSWORD_FILE` at it; `SER2NET_ADMIN_USER` sets the name
  (default `admin`). It is used only while there is no account yet, and the password is
  never logged:
  ```yaml
  services:
    ser2net:
      environment:
        SER2NET_ADMIN_USER: "admin"
        SER2NET_ADMIN_PASSWORD_FILE: /run/secrets/ser2net_admin
      secrets: [ser2net_admin]
  secrets:
    ser2net_admin:
      file: ./ser2net_admin.txt   # one line: the password (8+ characters)
  ```
- **Lost password / locked out:** set a new one on the running container — no restart,
  nothing else changes, and that account is signed out everywhere (a name that doesn't
  exist yet is created as an admin):
  ```bash
  docker exec -it ser2net python /app/ser2net.py --no-bootstrap --data-dir /data --set-password admin
  ```
  Non-interactive: `docker exec -i ser2net ... --set-password admin < password.txt`.
  `... --sign-out-all` ends every web UI session. `docker exec` runs as root; the command
  switches to the owner of `/data` before writing, so file ownership stays right.
- **`config.json` changed in the volume:** `docker kill -s HUP ser2net` (or
  `docker exec ser2net python /app/ser2net.py --no-bootstrap --data-dir /data --reload`, or
  Settings → *Configuration backups* → *Reload config.json*) has ser2net read it again
  without a restart: mappings whose settings didn't change keep their clients, a file that
  doesn't validate changes nothing (the log says why). The admin UI address comes from
  `SER2NET_BIND_IP` / `SER2NET_PORT`, which a reload keeps.

## Plain docker run

```bash
docker run -d --name ser2net --restart unless-stopped --init --stop-timeout 30 \
  -p 8080:8080 -p 4001:4001 \
  --device /dev/serial/by-id/usb-FTDI_...-if00-port0:/dev/ttyUSB0 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v ser2net-data:/data \
  ghcr.io/haliskilic/ser2net:latest
```

- `--restart unless-stopped` brings it back after a crash, a Docker restart or a power
  cut. A container started without it stays down until someone starts it by hand; add
  the policy to an existing one with `docker update --restart unless-stopped ser2net`.
- `--init` (compose: `init: true`) runs a small init as the container's first process,
  which passes `docker stop` on to ser2net; `--stop-timeout 30` (compose:
  `stop_grace_period: 30s`) gives the stop 30 s before Docker kills the container.
  ser2net finishes within that even when a device, an MQTT broker or a client stops
  answering: an admin UI response under way gets 5 s, the mappings stop side by side in
  at most 15 s, and output a serial device has not taken 2 s after its port closes is
  dropped. (Docker's default is 10 s, after which unsent trace data and the last log
  lines may be lost.)
- To build the image yourself, run `docker build -t ser2net .` in a checkout and use
  `ser2net` as the image name.

## Ports and networking

- **Mapping ports.** Only the ports you publish are reachable from outside the
  container, and every mapping listens on the port you give it in the UI (a new one
  gets 4001). Publish each one: `-p 4001:4001`, `-p 4002:4002`, `-p 4005:4005/udp` for a
  UDP mapping (compose: under `ports:`); changing them means recreating the container.
  Connect-out (client) mappings need nothing.
- **Host network** (`--network host`, compose `network_mode: host`, no `-p`): every
  mapping port is reachable as soon as it is created, and the LAN cluster's discovery
  works. Two things change: `SER2NET_BIND_IP` is then an address of the host itself
  (`0.0.0.0` = all its IPv4 interfaces, `*` = all of them, IPv4 and IPv6; set the LAN
  address to keep the UI off the others),
  and the unprivileged process can't open ports below 1024 there (Modbus TCP's 502): use
  1024 or above, the default network with `-p 502:502`, where Docker allows them, or
  `-e SER2NET_LOW_PORTS=1` (compose: under `environment:`). That keeps the
  `CAP_NET_BIND_SERVICE` capability for the server, and only it, so whoever may edit
  mappings can then open any free port below 1024 of the host. It needs the capability in
  the container (Docker's default set has it); with `--cap-drop ALL` or
  `--cap-drop NET_BIND_SERVICE` the start says it can't keep it. Without either, a mapping
  on such a port fails with a hint on the dashboard, and an admin UI port below 1024 is
  named in a start-up warning.
- **LAN cluster.** Discovery (UDP broadcast, port 41750) doesn't cross Docker's default
  network. Use the host network, or set Settings → LAN cluster → Advertise IP to the
  host's LAN address and list the other nodes under Manual peers (on every node);
  publish the UI with the same port inside and out (`8080:8080`), since that is the port
  a node advertises.
- **Bind address.** The admin UI binds to `0.0.0.0` *inside* the container (set via
  `SER2NET_BIND_IP`, with `SER2NET_PORT` for the port). The container network plus the
  published port are the real perimeter — don't publish `8080` to an untrusted network
  without TLS. These env vars win on every start, so compose stays the source of truth.

## Behind a reverse proxy

For TLS on a LAN or the Internet, a reverse proxy in front of the container is the usual
choice (or set `admin_ui.tls_*`). [REVERSE-PROXY.md](REVERSE-PROXY.md) has the settings and
examples for nginx, Apache, Caddy, Traefik and IIS, and a compose file with Caddy in front.
In short:

- the proxy passes the browser's address on as `Host` (or `X-Forwarded-Host`), passes
  WebSocket upgrades through and sends `X-Forwarded-For` / `X-Forwarded-Proto`;
- `SER2NET_TRUSTED_PROXIES` lists the proxy's address as the container sees it (a proxy
  container in the same network: that network's subnet; a proxy on the host: Docker's
  gateway, such as `172.17.0.1`), or Settings → Reverse proxy does; all.log names the
  address it ignored (`X-Forwarded-For from … is ignored`);
- `SER2NET_PUBLIC_URL` is the address users open (`https://ser2net.example.com`, or with a
  path the UI is served under: `https://gw.example.com/ser2net`);
- the UI port stays off the network: publish it to the host only (`127.0.0.1:8080:8080`),
  or not at all when the proxy runs in the same Docker network.

## Several instances

One container per instance, each with its own volume, `container_name` and published UI
port (inside, every one listens on 8080: `-p 8081:8080`; for the LAN cluster use the same
port inside and out, `SER2NET_PORT=8081` and `-p 8081:8081`, since that is the port a node
advertises). Give each a name with `SER2NET_NODE_NAME` (or Settings → Admin interface):
notifications, the browser tab, the top bar and the fleet view then tell them apart.
Without one they show the container's host name, which is its id unless `hostname:` /
`--hostname` sets one, and a new id comes with every re-creation (an image upgrade).
The browser keeps their sign-ins apart (on another port than 8080 the cookies' names carry
a tag of the port). Don't copy a volume to make another instance: start it empty and import
the mappings (Settings → Mappings export / import); a copy has the same cluster identity
and session key. README, *Several instances on one machine*, has the whole checklist.

## Serial devices

- **Access.** Pass each port with `--device` (compose `devices:`). A device keeps its
  host's numeric group inside the container — 20 on Debian/Ubuntu, 18 on Fedora/RHEL —
  and the entrypoint gives the server the group of every device it finds under a serial
  name (`/dev/ttyUSB*`, `ttyACM*`, `ttyS*`, `ttyAMA*`, `ttymxc*`, … `/dev/rfcomm*`,
  `/dev/serial/by-id/*`), so no `--group-add` is needed (a console's `tty` group is never
  added). A device passed under another name inside the container
  (`--device /dev/ttyUSB0:/dev/plc`) still needs `--group-add` with the host's numeric
  serial GID, read from the device (`--group-add "$(stat -L -c %g /dev/ttyUSB0)"`: `-L`
  follows a `/dev/serial/by-id` link, whose own GID is root's; `getent group dialout`
  finds nothing on Arch, whose serial group is `uucp`), and so does the 2.8.0 image. A
  device the server can't open (mode `0600`, say) is reported at start:
  `[ser2net] warning: uid … can't open /dev/…`.
- **A device missing at start** (an adapter unplugged at boot) makes Docker refuse to
  start the container, and the web UI is down with it. A renumbered adapter
  (`ttyUSB0` → `ttyUSB1`) isn't seen inside either. Both are what hot-plug avoids.
- **Hot-plug.** Bind the host's `/dev` and allow the device types instead of listing
  devices: `-v /dev:/dev --device-cgroup-rule='c 188:* rmw' --device-cgroup-rule='c 166:* rmw'`
  (USB-serial `ttyUSB*`, USB CDC `ttyACM*`) plus `--group-add <host serial GID>`
  (`stat -L -c %g` of a serial device on the host: 20 on Debian/Ubuntu, 18 on Fedora, 14 —
  `uucp` — on Arch), for an
  adapter plugged in after the start; the compose file has this variant. The port list
  and mappings bound to a USB identity then see a re-plugged adapter too.
- USB passthrough into containers is only reliable on Linux hosts (not Docker Desktop on
  Windows/macOS — there, run ser2net natively or bridge via RFC2217).

## Notes

- **Health.** The image has a `HEALTHCHECK` (`ser2net --healthcheck` asks `/healthz`,
  over HTTPS when `config.json` sets admin TLS, and over the other scheme when that one
  gets no answer): `docker ps` shows `healthy` once the UI answers — also before the
  first-run setup. The
  probe connects directly, so `HTTP_PROXY` / `HTTPS_PROXY` given to the container (for
  webhooks or SSO) don't affect it. Docker doesn't restart a container because it is
  unhealthy; only the restart policy brings back one that exited. If the UI's port can't
  be bound at start (with the host network: another program has it), the container stays
  up and `unhealthy` while the mappings run, and the UI comes up once the port is free.
  So that a stuck event loop doesn't leave a container that runs but forwards nothing,
  ser2net exits by itself when its loop has not run for 90 s (its threads' stacks in
  `/data/hang.log`), and the restart policy starts it again; `SER2NET_LOOP_WATCHDOG` sets
  the limit in seconds (`0` = off). A busy container is not affected: it gets back to its
  loop within about a second, and `docker pause` is not counted.
- **Logs.** Every log line goes to `/data/all.log` (rotated, 10 MB × 5) and to the
  container's output, which Docker keeps without a limit by default: cap it with
  `--log-opt max-size=10m --log-opt max-file=3` (compose: `logging:`), use the `local`
  driver (`--log-driver local`: compressed, capped at 100 MB by default), or set either for
  every container in `/etc/docker/daemon.json`. How long ser2net keeps its own logs, audit
  log and backups is set under Settings → *Retention* (off by default; see the README's
  *Retention*); the container's output is kept by Docker's settings alone. The lines name
  clients and the web UI's users by IP address and user name (see
  [PRIVACY.md](PRIVACY.md)); `-e SER2NET_LOG_STDOUT=off` keeps them in `/data/all.log`
  only (`docker logs` then shows just what isn't a log line, such as a start-up error), and
  Settings → Audit log → *Web UI requests in all.log* can leave out page views or every
  request line.
- **Clock.** A container has no clock of its own: it uses the host's. Keep the host's
  clock synchronised (NTP; README, *Time synchronisation*): ser2net never sets it, and a
  wrong clock puts wrong times in logs and backups and makes sign-ins and certificate
  checks fail. The start-up log warns when the clock reads earlier than the newest
  configuration backup.
- **Time zone.** The container runs in UTC unless `TZ` is set, so its log, audit and trace
  times are UTC, while the live hex view shows its times in the browser's zone. Log and audit
  lines say so with their offset (`2026-11-06 12:00:00+00:00`), trace lines don't. For the
  local time, set `TZ` (`-e TZ=Europe/Istanbul`, or under `environment:` in compose) or
  mount the host's zone (`-v /etc/localtime:/etc/localtime:ro`).
- **Runs as a non-root user.** The process runs as `ser2net` (uid/gid 10001), never
  as root. The entrypoint starts as root only to choose the account: a `/data` owned by
  root — a volume written by an image before v2.8, which ran as root — is handed to
  `ser2net` once (logged), so **upgrading needs no manual step** — except on the host
  network with a port below 1024, which root could open: add `SER2NET_LOW_PORTS=1`
  ([Ports and networking](#ports-and-networking)). A bind-mounted folder
  owned by another user is used as that user, so the folder keeps its owner; files in it
  that an image wrote (owned by root, as with the images before v2.8, or by uid 10001)
  are handed to the folder's owner first (logged). A volume or host folder mounted
  inside `/data` (`-v /etc/letsencrypt:/data/certs`) is never taken over. The groups
  given with `--group-add` are kept, root's group is not. With `docker run --user …` the
  entrypoint just runs as that user.
- **When files can't be taken over** (a read-only mount, NFS with `root_squash`), the
  start goes on and ser2net says what it can't use: a `config.json` it can't read stops
  the start with one line naming its owner and mode, the account ser2net runs as and the
  fix — run `sudo chown -R <uid>:<gid> <folder>` on the host, for the folder mounted at
  `/data`; a log it can't write is a warning.
- **Rootless Podman** maps your own user to root inside the container: your
  bind-mounted folder looks root-owned there and would be handed to uid 10001 (a
  sub-uid on the host). Start it with `--userns=keep-id` (plus `--group-add keep-groups`
  with crun, to keep your serial device group); ser2net then runs as you.
- **Persistence.** `config.json`, its automatic backups (`backups/`), logs, traces
  (`traces/`) and any generated TLS material live in the `/data` volume; back that up to
  preserve mappings and the admin password. `config.json` and `backups/` hold secrets in
  plain text (the LDAP, OIDC, SMTP and MQTT passwords, MQTT client and TLS key passwords,
  the webhook URL,
  the cluster key, the session key and two-factor sign-in keys; see the README's *Configuration & state files*), so keep a copy of
  `/data` like a password vault. `/data` must be writable: mount the whole
  directory, not `config.json` on its own (it is replaced atomically, which a single-file
  mount doesn't allow). A start rewrites `config.json` only when something changed (the
  `SER2NET_*` address, an upgrade).
- **Starting over.** `docker compose down -v` removes the container, its log and the
  `ser2net-data` volume — `config.json`, `backups/`, `all.log*`, `audit.log*`, `logs/`,
  `traces/` and `tls/` (plain `docker run`: `docker rm -f ser2net` and `docker volume rm`
  the volume; a bind-mounted folder: stop the container, then empty the folder). Deleting
  only `/data/config.json` resets the accounts, settings and mappings but keeps the rest,
  and whoever completes the first-run setup next sees the earlier audit log and backups in
  Settings. A lost password needs neither: see
  [First admin and account recovery](#first-admin-and-account-recovery).
- **A setting the image rejects doesn't stop it.** If `config.json` holds a value this
  version refuses (a mapping's TLS file that isn't mounted, a hand edit), the container
  still starts: the log names each one (`WARNING: config.json: mapping '…': …`), the
  mappings that can start run, and the value is kept as it is. Other changes can still be
  saved — a save is refused only for what it changes or adds — so sign in and fix or
  remove that part when you can. An unusable `SER2NET_BIND_IP` / `SER2NET_PORT`, an
  unreadable or broken `config.json` and a missing admin UI certificate still stop the
  start, with a one-line message. Moving a native install's data directory to `/data`: a
  certificate generated in its `tls/` folder is found in `/data/tls/` (the log says so and
  `config.json` is updated); TLS paths written relative (`tls/cert.pem`) are files in
  `/data`.
- **TLS.** Put a reverse proxy ([Behind a reverse proxy](#behind-a-reverse-proxy)) or set
  `admin_ui.tls_*` for LAN/Internet exposure; raw data bridges are plaintext unless
  per-mapping TLS is enabled.
- **Dependencies** are installed from PyPI into the image, so the container runs with
  `--no-bootstrap` (the bundled `vendor/wheels` offline path is for air-gapped hosts).
