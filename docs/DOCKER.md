# Running ser2net in Docker

ser2net ships an official `Dockerfile`, an example `docker-compose.yml` and a published
image for amd64 + arm64 with every release: `ghcr.io/haliskilic/ser2net` (`:latest` or a
release tag).

## Quick start (compose)

1. Edit `docker-compose.yml` → `devices:` to list your serial port(s). Prefer a
   stable path so USB re-plug renumbering doesn't break the mapping:
   ```yaml
   devices:
     - "/dev/serial/by-id/usb-FTDI_...-if00-port0:/dev/ttyUSB0"
   ```
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

## Plain docker run

```bash
docker run -d --name ser2net --restart unless-stopped \
  -p 8080:8080 -p 4001:4001 \
  --device /dev/serial/by-id/usb-FTDI_...-if00-port0:/dev/ttyUSB0 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v ser2net-data:/data \
  ghcr.io/haliskilic/ser2net:latest
```

- `--restart unless-stopped` brings it back after a crash, a Docker restart or a power
  cut. A container started without it stays down until someone starts it by hand; add
  the policy to an existing one with `docker update --restart unless-stopped ser2net`.
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
  (`0.0.0.0` = all its interfaces; set the LAN address to keep the UI off the others),
  and the unprivileged process can't open ports below 1024 there (Modbus TCP's 502): use
  1024 or above, or the default network with `-p 502:502`, where Docker allows them.
- **LAN cluster.** Discovery (UDP broadcast, port 41750) doesn't cross Docker's default
  network. Use the host network, or set Settings → LAN cluster → Advertise IP to the
  host's LAN address and list the other nodes under Manual peers (on every node);
  publish the UI with the same port inside and out (`8080:8080`), since that is the port
  a node advertises.
- **Bind address.** The admin UI binds to `0.0.0.0` *inside* the container (set via
  `SER2NET_BIND_IP`, with `SER2NET_PORT` for the port). The container network plus the
  published port are the real perimeter — don't publish `8080` to an untrusted network
  without TLS. These env vars win on every start, so compose stays the source of truth.

## Serial devices

- **Access.** Pass each port with `--device` (compose `devices:`). A device keeps its
  host's numeric group inside the container — 20 on Debian/Ubuntu, 18 on Fedora/RHEL —
  and the entrypoint gives the server the group of every device it finds under a serial
  name (`/dev/ttyUSB*`, `ttyACM*`, `ttyS*`, `ttyAMA*`, `ttymxc*`, … `/dev/rfcomm*`,
  `/dev/serial/by-id/*`), so no `--group-add` is needed (a console's `tty` group is never
  added). A device passed under another name inside the container
  (`--device /dev/ttyUSB0:/dev/plc`) still needs `--group-add` with the host's numeric
  serial GID (`getent group dialout | cut -d: -f3`), and so does the 2.8.0 image. A
  device the server can't open (mode `0600`, say) is reported at start:
  `[ser2net] warning: uid … can't open /dev/…`.
- **A device missing at start** (an adapter unplugged at boot) makes Docker refuse to
  start the container, and the web UI is down with it. A renumbered adapter
  (`ttyUSB0` → `ttyUSB1`) isn't seen inside either. Both are what hot-plug avoids.
- **Hot-plug.** Bind the host's `/dev` and allow the device types instead of listing
  devices: `-v /dev:/dev --device-cgroup-rule='c 188:* rmw' --device-cgroup-rule='c 166:* rmw'`
  (USB-serial `ttyUSB*`, USB CDC `ttyACM*`) plus `--group-add <host serial GID>`, for an
  adapter plugged in after the start; the compose file has this variant. The port list
  and mappings bound to a USB identity then see a re-plugged adapter too.
- USB passthrough into containers is only reliable on Linux hosts (not Docker Desktop on
  Windows/macOS — there, run ser2net natively or bridge via RFC2217).

## Notes

- **Health.** The image has a `HEALTHCHECK` (`ser2net --healthcheck` asks `/healthz`):
  `docker ps` shows `healthy` once the UI answers — also before the first-run setup. A
  certificate set or generated under Settings → Admin TLS is used from the next restart;
  until then the probe finds the UI on plain http and still reports it healthy. The
  probe connects directly, so `HTTP_PROXY` / `HTTPS_PROXY` given to the container (for
  webhooks or SSO) don't affect it. Docker doesn't restart a container because it is
  unhealthy; only the restart policy brings back one that exited.
- **Logs.** Every log line goes to `/data/all.log` (rotated, 10 MB × 5) and to the
  container's output, which Docker keeps without a limit by default: cap it with
  `--log-opt max-size=10m --log-opt max-file=3` (compose: `logging:`), or for every
  container in `/etc/docker/daemon.json`.
- **Runs as a non-root user.** The process runs as `ser2net` (uid/gid 10001), never
  as root. The entrypoint starts as root only to choose the account: a `/data` owned by
  root — a volume written by an image before v2.8, which ran as root — is handed to
  `ser2net` once (logged), so **upgrading needs no manual step**. A bind-mounted folder
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
  preserve mappings and the admin password. `/data` must be writable: mount the whole
  directory, not `config.json` on its own (it is replaced atomically, which a single-file
  mount doesn't allow). A start rewrites `config.json` only when something changed (the
  `SER2NET_*` address, an upgrade).
- **A setting the image rejects doesn't stop it.** If `config.json` holds a value this
  version refuses (a mapping's TLS file that isn't mounted, a hand edit), the container
  still starts: the log names each one (`WARNING: config.json: mapping '…': …`), the
  mappings that can start run, and the value is kept as it is. Other changes can still be
  saved — a save is refused only for what it changes or adds — so sign in and fix or
  remove that part when you can. An unusable `SER2NET_BIND_IP` / `SER2NET_PORT`, an
  unreadable or broken `config.json` and a missing admin UI certificate still stop the
  start, with a one-line message.
- **TLS.** Put a reverse proxy (or set `admin_ui.tls_*`) in front for LAN/Internet
  exposure; raw data bridges are plaintext unless per-mapping TLS is enabled.
- **Dependencies** are installed from PyPI into the image, so the container runs with
  `--no-bootstrap` (the bundled `vendor/wheels` offline path is for air-gapped hosts).
