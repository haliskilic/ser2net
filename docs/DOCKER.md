# Running ser2net in Docker

ser2net ships an official `Dockerfile` and an example `docker-compose.yml`.

## Quick start (compose)

1. Edit `docker-compose.yml` → `devices:` to list your serial port(s). Prefer a
   stable path so USB re-plug renumbering doesn't break the mapping:
   ```yaml
   devices:
     - "/dev/serial/by-id/usb-FTDI_...-if00-port0:/dev/ttyUSB0"
   ```
2. Start it:
   ```bash
   docker compose up -d
   ```
3. Open `http://<host>:8080` and set the admin password on first access — or create the
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
docker build -t ser2net .
docker run -d --name ser2net \
  -p 8080:8080 \
  --device /dev/ttyUSB0 \
  --group-add "$(getent group dialout | cut -d: -f3)" \
  -v ser2net-data:/data \
  ser2net
```

## Notes

- **Bind address.** The admin UI binds to `0.0.0.0` *inside* the container (set via
  `SER2NET_BIND_IP`, with `SER2NET_PORT` for the port). The container network plus the
  published port are the real perimeter — don't publish `8080` to an untrusted network
  without TLS. These env vars win on every start, so compose stays the source of truth.
- **Serial access.** Pass each port with `--device`, and grant the `dialout` group
  (`--group-add`) so the process may open it. USB passthrough into containers is only
  reliable on Linux hosts (not Docker Desktop on Windows/macOS — there, run ser2net
  natively or bridge via RFC2217).
- **Runs as a non-root user.** The process runs as `ser2net` (uid/gid 10001), never
  as root. The entrypoint starts as root only to choose the account: a `/data` owned by
  root — a volume written by an image before v2.8, which ran as root — is handed to
  `ser2net` once (logged), so **upgrading needs no manual step**; a bind-mounted folder
  owned by another user is used as that user, leaving the host files' owner alone. The
  groups given with `--group-add` (serial device access) are kept, root's group is not.
  With `docker run --user …` the entrypoint just runs as that user.
- **Health.** The image has a `HEALTHCHECK` (`ser2net --healthcheck` asks `/healthz`):
  `docker ps` shows `healthy` once the UI answers — also before the first-run setup. The
  probe connects directly, so `HTTP_PROXY` / `HTTPS_PROXY` given to the container (for
  webhooks or SSO) don't affect it.
- **Persistence.** `config.json`, its automatic backups (`backups/`), logs, traces
  (`traces/`) and any generated TLS material live in the `/data` volume; back that up to
  preserve mappings and the admin password. `/data` must be writable: mount the whole
  directory, not `config.json` on its own (it is replaced atomically, which a single-file
  mount doesn't allow). A start rewrites `config.json` only when something changed (the
  `SER2NET_*` address, an upgrade).
- **A setting the image rejects doesn't stop it.** If `config.json` holds a value this
  version refuses (a mapping's TLS file that isn't mounted, a hand edit), the container
  still starts: the log names each one (`WARNING: config.json: mapping '…': …`), the
  mappings that can start run, and the file is left as it is. Saving any change is
  refused until it is fixed — sign in and edit or remove that part. An unusable
  `SER2NET_BIND_IP` / `SER2NET_PORT`, an unreadable or broken `config.json` and a missing
  admin UI certificate still stop the start, with a one-line message.
- **TLS.** Put a reverse proxy (or set `admin_ui.tls_*`) in front for LAN/Internet
  exposure; raw data bridges are plaintext unless per-mapping TLS is enabled.
- **Dependencies** are installed from PyPI into the image, so the container runs with
  `--no-bootstrap` (the bundled `vendor/wheels` offline path is for air-gapped hosts).
```
