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
3. Open `http://<host>:8080` and set the admin password on first access.

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
- **Persistence.** `config.json`, logs and any generated TLS material live in the
  `/data` volume; back that up to preserve mappings and the admin password.
- **TLS.** Put a reverse proxy (or set `admin_ui.tls_*`) in front for LAN/Internet
  exposure; raw data bridges are plaintext unless per-mapping TLS is enabled.
- **Dependencies** are installed from PyPI into the image, so the container runs with
  `--no-bootstrap` (the bundled `vendor/wheels` offline path is for air-gapped hosts).
```
